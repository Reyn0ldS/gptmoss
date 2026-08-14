import asyncio
import base64
import inspect
import json
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Dict, Any, List, Optional
from urllib.parse import urlsplit
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

# GUI HTML path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
GUI_FILE_PATH = os.path.join(CURRENT_DIR, "gui.html")

from gptmoss.core import EventBus, Event, StateEngine, RuntimeKernel, ExecutionEngine, DEFAULT_SYSTEM_PROMPT, RuntimeSettings, ProjectDomainRegistry
from gptmoss.core.artifacts import ArtifactStore
from gptmoss.core.document_model import DocumentModelStore
from gptmoss.core.corpus_policy import build_corpus_policy
from gptmoss.capabilities.documents import DocumentCapability
from gptmoss.core.evolution import AgentProfileRegistry, AutonomousSkillLifecycle
from gptmoss.core.skills import SkillRegistry
from gptmoss.core.settings import (
    DEFAULT_MAX_ATTACHMENT_TEXT_CHARS,
    DEFAULT_MAX_TRANSITIONS_PER_EXECUTION,
    DEFAULT_MAX_UPLOAD_BYTES,
)
from gptmoss.capabilities.agent import child_agent_config
from gptmoss.planners.complexity import normalize_planning_mode, task_title_from_text

logger = logging.getLogger("gptmoss.api")

# Models for request/response validation
class SubmitTaskRequest(BaseModel):
    task: str = Field(min_length=1)
    agent_config: Optional[Dict[str, Any]] = None
    project_id: Optional[str] = None
    planning_mode: str = "auto"
    attachment_ids: List[str] = Field(default_factory=list)
    corpus_ids: List[str] = Field(default_factory=list)
    corpus_auto_workflow: bool = True
    delay_seconds: float = Field(default=0, ge=0, le=31_536_000)
    run_at: Optional[float] = Field(default=None, ge=0)

class ProjectRequest(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=200)
    path: Optional[str] = Field(default=None, max_length=2_000)
    domains: Dict[str, List[str]] = Field(default_factory=dict)

class UploadArtifactRequest(BaseModel):
    filename: str
    content_base64: str
    content_type: str

class CreateCorpusRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    root_label: str = Field(min_length=1, max_length=1_000)
    resume: bool = True

class CorpusIssue(BaseModel):
    relative_path: str = Field(min_length=1, max_length=1_000)
    reason: Optional[str] = Field(default=None, max_length=1_000)
    error: Optional[str] = Field(default=None, max_length=2_000)

class FinalizeCorpusRequest(BaseModel):
    present_paths: List[str] = Field(default_factory=list, max_length=10_000)
    # Keep the finalization limits aligned with the documented folder limit.
    # Otherwise a successful import can fail only when its manifest is closed.
    skipped: List[CorpusIssue] = Field(default_factory=list, max_length=10_000)
    errors: List[CorpusIssue] = Field(default_factory=list, max_length=10_000)

class DecisionRequest(BaseModel):
    reason: Optional[str] = None

class SkillRequest(BaseModel):
    name: str
    description: str = ""
    instructions: str
    allowed_capabilities: List[str] = Field(default_factory=list)

class SkillImportRequest(BaseModel):
    content: str = Field(min_length=1)

class MemoryRequest(BaseModel):
    value: Any
    metadata: Dict[str, Any] = Field(default_factory=dict)
    provenance: Dict[str, Any] = Field(default_factory=lambda: {"source": "gui"})
    validated: bool = False
    ttl_seconds: Optional[float] = Field(default=None, ge=1, le=31_536_000)
    kind: str = Field(default="fact", pattern=r"^(fact|decision|preference|constraint|lesson)$")
    scope: str = Field(default="project", pattern=r"^(project|user|global)$")
    project_id: Optional[str] = "proj-default"
    user_id: Optional[str] = None
    source_execution_id: Optional[str] = None
    source_artifacts: List[str] = Field(default_factory=list)
    supersedes_id: Optional[str] = None

class SubAgentRequest(BaseModel):
    task: str = Field(min_length=1)
    role_name: str = Field(default="Sous-agent", min_length=1, max_length=100)
    system_prompt: str = Field(default="You are a helpful MOSS sub-agent assisting a parent agent.", min_length=1)

class ConfirmationRequest(BaseModel):
    confirm: bool = False

class SettingsRequest(RuntimeSettings):
    api_key: str = ""
    confirm_sensitive: bool = False

class AppState:
    kernel: Optional[RuntimeKernel] = None
    execution_engine: Optional[ExecutionEngine] = None
    state_engine: Optional[StateEngine] = None
    event_bus: Optional[EventBus] = None
    flush_task: Optional[asyncio.Task] = None
    subscribed_event_bus: Optional[EventBus] = None
    config_path: Optional[Path] = None

app_state = AppState()

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Attach runtime services when the API starts and stop them cleanly."""
    if app_state.event_bus and app_state.subscribed_event_bus is not app_state.event_bus:
        app_state.event_bus.subscribe_all(manager.broadcast_event)
        app_state.subscribed_event_bus = app_state.event_bus
        logger.info("Subscribed websocket manager to event bus")

    if app_state.state_engine and app_state.event_bus and not app_state.flush_task:
        app_state.flush_task = app_state.state_engine.start_db_flush_loop(app_state.event_bus)
        logger.info("Started debounced state persistence loop")
    if app_state.execution_engine:
        app_state.execution_engine.scheduler.start()
        app_state.execution_engine.resume_waiting_provider_executions()
        app_state.execution_engine.resume_interrupted_executions()

    try:
        yield
    finally:
        if app_state.execution_engine:
            await app_state.execution_engine.stop_runtime_services()
        if app_state.flush_task:
            if app_state.state_engine:
                await app_state.state_engine.stop_db_flush_loop(flush=True)
            else:
                app_state.flush_task.cancel()
                with suppress(asyncio.CancelledError):
                    await app_state.flush_task
            app_state.flush_task = None

app = FastAPI(title="MOSS Agent Runtime Platform API", version="0.1.0", lifespan=lifespan)

# Local GUI may be rebound to any loopback port by the supervisor.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_origin_regex=r"https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ACTIVE_EXECUTION_STATUSES = frozenset({"pending", "running", "paused", "waiting_provider"})
PUBLIC_EXECUTION_VARIABLE_KEYS = (
    "task",
    "task_title",
    "planning_mode",
    "project_id",
    "project_path",
    "project_domains",
    "attachment_ids",
    "corpus_ids",
    "corpus_auto_workflow",
    "corpus_policy",
    "corpus_summaries",
    "role_name",
    "parent_execution_id",
    "pending_approval",
    "pending_scope_approval",
    "scheduled_for",
    "document_model_checkpoint",
    "active_skills",
    "delivery_contract",
    "plan_parallelism_limit",
    "requested_skills",
)

# Store active websocket connections
class ConnectionManager:
    def __init__(self):
        self.global_connections: List[WebSocket] = []
        self.execution_connections: Dict[str, List[WebSocket]] = {}

    async def connect_global(self, websocket: WebSocket):
        await websocket.accept()
        self.global_connections.append(websocket)

    def disconnect_global(self, websocket: WebSocket):
        if websocket in self.global_connections:
            self.global_connections.remove(websocket)

    async def connect_execution(self, exec_id: str, websocket: WebSocket):
        await websocket.accept()
        if exec_id not in self.execution_connections:
            self.execution_connections[exec_id] = []
        self.execution_connections[exec_id].append(websocket)

    def disconnect_execution(self, exec_id: str, websocket: WebSocket):
        if exec_id in self.execution_connections and websocket in self.execution_connections[exec_id]:
            self.execution_connections[exec_id].remove(websocket)

    async def broadcast_event(self, event: Event):
        # Broadcaster callback for EventBus
        data = event.model_dump()
        json_str = json.dumps(data)

        # 1. Global broadcast
        for ws in list(self.global_connections):
            try:
                await ws.send_text(json_str)
            except Exception:
                self.disconnect_global(ws)

        # 2. Execution-specific broadcast
        exec_id = event.payload.get("execution_id")
        if exec_id and exec_id in self.execution_connections:
            for ws in list(self.execution_connections[exec_id]):
                try:
                    await ws.send_text(json_str)
                except Exception:
                    self.disconnect_execution(exec_id, ws)

manager = ConnectionManager()

DEFAULT_PROJECT = {"id": "proj-default", "name": "Projet Par Défaut"}


def _status_value(state) -> str:
    status = getattr(state, "status", state)
    return str(getattr(status, "value", status))


def _descendant_ids(state_engine, execution_id: str) -> List[str]:
    to_visit = [execution_id]
    collected: List[str] = []
    while to_visit:
        current = to_visit.pop(0)
        collected.append(current)
        for child_id, child_state in state_engine.executions.items():
            parent_id = child_state.variables.get("parent_execution_id")
            if parent_id == current and child_id not in collected and child_id not in to_visit:
                to_visit.append(child_id)
    return collected


def _has_active_execution(state_engine, execution_ids: Optional[List[str]] = None) -> bool:
    if execution_ids is None:
        states = state_engine.executions.values()
    else:
        states = (
            state_engine.executions[item]
            for item in execution_ids
            if item in state_engine.executions
        )
    return any(_status_value(state) in ACTIVE_EXECUTION_STATUSES for state in states)


def _public_execution_variables(variables: Dict[str, Any]) -> Dict[str, Any]:
    public = {
        key: variables[key]
        for key in PUBLIC_EXECUTION_VARIABLE_KEYS
        if key in variables
    }
    pending = public.get("pending_approval")
    if isinstance(pending, dict):
        pending = dict(pending)
        arguments = pending.get("arguments")
        if isinstance(arguments, dict):
            trimmed = dict(arguments)
            content = trimmed.get("content")
            if isinstance(content, str) and len(content) > 8_000:
                trimmed["content"] = content[:8_000] + "\n… [truncated]"
            pending["arguments"] = trimmed
        public["pending_approval"] = pending
    return public

def _filesystem_capability():
    if not app_state.execution_engine:
        return None
    return app_state.execution_engine.get_capability("filesystem")

def _artifact_store() -> Optional[ArtifactStore]:
    if not app_state.execution_engine:
        return None
    store = app_state.execution_engine.artifact_store
    if store:
        _synchronize_document_capability(store)
        return store
    filesystem = _filesystem_capability()
    if not filesystem:
        return None
    store = ArtifactStore(filesystem.workspace_root)
    app_state.execution_engine.artifact_store = store
    _synchronize_document_capability(store)
    return store

def _synchronize_document_capability(store: ArtifactStore) -> None:
    if not app_state.execution_engine:
        return
    capability = app_state.execution_engine.get_capability("documents")
    if capability and hasattr(capability, "update_store"):
        capability.update_store(store)
    elif not capability:
        app_state.execution_engine.register_capability(
            "documents",
            DocumentCapability(store),
        )

def _runtime_config_path() -> Optional[Path]:
    if app_state.config_path:
        return app_state.config_path
    filesystem = _filesystem_capability()
    return Path(filesystem.workspace_root).resolve() / "config.json" if filesystem else None

def _load_runtime_config() -> Dict[str, Any]:
    config_path = _runtime_config_path()
    if not config_path or not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Runtime configuration is unreadable.") from exc

def _configured_projects() -> List[Dict[str, Any]]:
    projects = _load_runtime_config().get("projects") or [dict(DEFAULT_PROJECT)]
    return [project for project in projects if isinstance(project, dict) and project.get("id")]

def _write_runtime_config(config: Dict[str, Any]) -> None:
    config_path = _runtime_config_path()
    if not config_path:
        raise HTTPException(status_code=500, detail="Filesystem capability not initialized.")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, config_path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Unable to persist runtime configuration.") from exc

def _project_by_id(project_id: str) -> Optional[Dict[str, Any]]:
    return next((project for project in _configured_projects() if project.get("id") == project_id), None)

@app.get("/", response_class=HTMLResponse)
async def get_gui():
    if not os.path.exists(GUI_FILE_PATH):
        raise HTTPException(status_code=404, detail="GUI file not found.")
    with open(GUI_FILE_PATH, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "runtime_initialized": bool(
            app_state.kernel and app_state.execution_engine and app_state.state_engine
        ),
    }


@app.get("/api/runtime-control")
async def runtime_control(response: Response):
    """Return the loopback supervisor connection inherited by this process."""
    supervisor_url = os.environ.get("GPTMOSS_SUPERVISOR_URL", "").rstrip("/")
    supervisor_token = os.environ.get("GPTMOSS_SUPERVISOR_TOKEN", "")
    parsed = urlsplit(supervisor_url) if supervisor_url else None
    available = bool(
        parsed
        and parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.port
        and supervisor_token
        and os.environ.get("GPTMOSS_SUPERVISOR_MANAGED") == "1"
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "available": available,
        "supervisor_url": supervisor_url if available else "",
        "token": supervisor_token if available else "",
    }


@app.get("/readiness")
async def readiness():
    ready = bool(
        app_state.kernel
        and app_state.execution_engine
        and app_state.state_engine
        and app_state.event_bus
    )
    if not ready:
        raise HTTPException(status_code=503, detail="Runtime services are not initialized.")
    return {"status": "ready"}

@app.post("/executions", status_code=201)
async def submit_task(req: SubmitTaskRequest):
    if not app_state.kernel or not app_state.state_engine:
        raise HTTPException(status_code=500, detail="Runtime kernel not initialized.")
    project_id = req.project_id or "proj-default"
    project = _project_by_id(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' does not exist.")

    filesystem = _filesystem_capability()
    requested_attachments = list(dict.fromkeys(req.attachment_ids))
    requested_corpora = list(dict.fromkeys(req.corpus_ids))
    if (requested_attachments or requested_corpora) and (
        not filesystem or not app_state.execution_engine.artifact_store
    ):
        raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
    for corpus_id in requested_corpora:
        try:
            corpus = app_state.execution_engine.artifact_store.get_corpus(corpus_id)
        except (ValueError, FileNotFoundError, OSError, KeyError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=404, detail=f"Corpus '{corpus_id}' does not exist.") from exc
        if corpus.get("state") not in {"ready", "partial"}:
            raise HTTPException(status_code=409, detail=f"Corpus '{corpus_id}' is not finalized.")
        requested_attachments.extend(
            str(entry["artifact_id"])
            for entry in dict(corpus.get("entries") or {}).values()
            if isinstance(entry, dict) and entry.get("artifact_id")
        )
    requested_attachments = list(dict.fromkeys(requested_attachments))
    for attachment_id in requested_attachments:
        try:
            app_state.execution_engine.artifact_store.get(attachment_id)
        except (ValueError, FileNotFoundError, OSError, KeyError) as exc:
            raise HTTPException(status_code=404, detail=f"Attachment '{attachment_id}' does not exist.") from exc

    agent_config = dict(req.agent_config or {})
    agent_config.setdefault("system_prompt", DEFAULT_SYSTEM_PROMPT)
    variables = dict(agent_config.get("variables") or {})
    planning_mode = normalize_planning_mode(
        req.planning_mode or variables.get("planning_mode") or agent_config.get("planning_mode")
    )
    corpus_summaries = []
    for corpus_id in requested_corpora:
        corpus = app_state.execution_engine.artifact_store.get_corpus(corpus_id)
        corpus_summaries.append(_public_corpus(corpus))
    effective_task = req.task.strip()
    corpus_auto_workflow = bool(req.corpus_auto_workflow) and bool(
        requested_corpora or requested_attachments
    )
    corpus_policy = build_corpus_policy(
        enabled=corpus_auto_workflow,
        source_kind="corpus" if requested_corpora else "attachments",
        # A selected folder explicitly requests the professional corpus path.
        # Loose attachments remain useful evidence without forcing a report.
        professional_delivery=bool(requested_corpora),
    )
    variables.update({
        "project_id": project_id,
        "attachment_ids": requested_attachments,
        "corpus_ids": requested_corpora,
        "corpus_summaries": corpus_summaries,
        "corpus_auto_workflow": corpus_auto_workflow,
        "corpus_policy": corpus_policy,
        "planning_mode": planning_mode,
        "task_title": task_title_from_text(req.task.strip()),
    })
    agent_config["planning_mode"] = planning_mode
    if project.get("path"):
        variables["project_path"] = str(Path(str(project["path"])).resolve())
    if project.get("domains"):
        variables["project_domains"] = project["domains"]
    agent_config["variables"] = variables
    exec_id = await app_state.kernel.submit_task(
        effective_task, agent_config,
        delay_seconds=req.delay_seconds, run_at=req.run_at,
    )
    state = app_state.state_engine.get_execution(exec_id)
    return {
        "execution_id": exec_id,
        "status": "scheduled" if float(state.variables.get("scheduled_for", 0)) > time.time() else "running",
        "scheduled_for": state.variables.get("scheduled_for"),
    }

@app.get("/projects")
async def list_projects():
    return _configured_projects()

@app.post("/projects", status_code=201)
async def create_project(req: ProjectRequest):
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    config = _load_runtime_config()
    projects = config.get("projects") or [dict(DEFAULT_PROJECT)]
    if any(isinstance(project, dict) and project.get("id") == req.id for project in projects):
        raise HTTPException(status_code=409, detail=f"Project '{req.id}' already exists.")
    project = {"id": req.id, "name": req.name.strip()}
    if req.domains:
        registry = ProjectDomainRegistry()
        try:
            for name, markers in req.domains.items():
                registry.register(name, markers)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        project["domains"] = req.domains
    if req.path and req.path.strip():
        raw_path = Path(req.path.strip()).expanduser()
        if ".." in raw_path.parts:
            raise HTTPException(status_code=400, detail="A custom project path cannot contain parent-directory segments.")
        if not raw_path.is_absolute():
            raise HTTPException(status_code=400, detail="A custom project path must be absolute.")
        path = raw_path.resolve()
        project["path"] = str(path)
        path.mkdir(parents=True, exist_ok=True)
    else:
        filesystem = _filesystem_capability()
        (Path(filesystem.workspace_root) / "projects" / req.id).mkdir(parents=True, exist_ok=True)
    projects.append(project)
    config["projects"] = projects
    _write_runtime_config(config)
    await app_state.event_bus.publish(Event(type="ProjectCreated", payload={"project_id": req.id, "name": project["name"]}))
    return project

@app.post("/artifacts", status_code=201)
async def upload_artifact(req: UploadArtifactRequest):
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    filesystem = app_state.execution_engine.get_capability("filesystem")
    if not filesystem:
        raise HTTPException(status_code=500, detail="Filesystem capability not initialized.")
    try:
        store = _artifact_store()
        if not store:
            raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
        metadata = store.save_base64(
            req.filename, req.content_base64, req.content_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.warning(
            "Artifact persistence failed after filesystem retries: %s",
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Le stockage du workspace est momentanément indisponible. "
                "Vérifiez le partage réseau puis relancez uniquement les fichiers en échec."
            ),
        ) from exc
    public_fields = (
        "id", "filename", "source_name", "content_type", "size_bytes", "sha256", "created_at",
        "document_title", "document_blocks", "document_parser",
        "document_parser_version", "document_chunks",
    )
    return {key: metadata[key] for key in public_fields if key in metadata}


def _public_corpus(corpus: Dict[str, Any], *, include_entries: bool = False) -> Dict[str, Any]:
    entries = dict(corpus.get("entries") or {})
    result = {
        key: corpus.get(key)
        for key in (
            "id", "name", "root_label", "source_kind", "state",
            "created_at", "updated_at", "skipped", "errors",
            "skipped_count", "error_count",
        )
    }
    result["skipped_count"] = int(
        corpus.get("skipped_count", len(corpus.get("skipped") or [])) or 0
    )
    result["error_count"] = int(
        corpus.get("error_count", len(corpus.get("errors") or [])) or 0
    )
    result["file_count"] = len(entries)
    result["document_count"] = sum(
        1 for entry in entries.values()
        if str(entry.get("content_type") or "") in ArtifactStore.DOCUMENT_TYPES
    )
    result["image_count"] = sum(
        1 for entry in entries.values()
        if str(entry.get("content_type") or "") in ArtifactStore.IMAGE_TYPES
    )
    result["size_bytes"] = sum(int(entry.get("size_bytes") or 0) for entry in entries.values())
    result["attachment_ids"] = list(dict.fromkeys(
        str(entry["artifact_id"]) for entry in entries.values() if entry.get("artifact_id")
    ))
    if include_entries:
        result["entries"] = entries
    return result


@app.post("/corpora", status_code=201)
async def create_corpus(req: CreateCorpusRequest):
    store = _artifact_store()
    if not store:
        raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
    try:
        corpus, resumed = store.create_corpus(
            req.name, root_label=req.root_label, resume=req.resume
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = _public_corpus(corpus, include_entries=True)
    result["resumed"] = resumed
    return result


@app.get("/corpora")
async def list_corpora():
    store = _artifact_store()
    if not store:
        raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
    return [_public_corpus(corpus) for corpus in store.list_corpora()]


@app.get("/corpora/{corpus_id}")
async def get_corpus(corpus_id: str):
    store = _artifact_store()
    if not store:
        raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
    try:
        return _public_corpus(store.get_corpus(corpus_id), include_entries=True)
    except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="Corpus not found.") from exc


@app.delete("/corpora/{corpus_id}")
async def delete_corpus(corpus_id: str):
    store = _artifact_store()
    if not store:
        raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
    try:
        corpus = store.delete_corpus(corpus_id)
    except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=404, detail="Corpus not found.") from exc
    return {"status": "deleted", "id": corpus["id"], "retained_artifacts": len(corpus.get("entries") or {})}


@app.put("/corpora/{corpus_id}/files", status_code=201)
async def upload_corpus_file(
    corpus_id: str,
    request: Request,
    relative_path: str,
    last_modified: int = 0,
):
    store = _artifact_store()
    if not store:
        raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
    try:
        store.get_corpus(corpus_id)
        payload = await request.body()
        metadata = await asyncio.to_thread(
            store.save_bytes,
            Path(relative_path.replace("\\", "/")).name,
            payload,
            request.headers.get("content-type", "application/octet-stream"),
            corpus_id=corpus_id,
            relative_path=relative_path,
            last_modified=last_modified,
            expected_sha256=request.headers.get("x-content-sha256", ""),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Corpus not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Corpus storage is temporarily unavailable.") from exc
    return {
        key: metadata[key]
        for key in (
            "id", "filename", "source_name", "content_type", "size_bytes", "sha256",
            "document_title", "document_blocks", "document_parser", "document_chunks",
            "deduplicated",
        )
        if key in metadata
    }


@app.post("/corpora/{corpus_id}/finalize")
async def finalize_corpus(corpus_id: str, req: FinalizeCorpusRequest):
    store = _artifact_store()
    if not store:
        raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
    try:
        corpus = store.finalize_corpus(
            corpus_id,
            present_paths=req.present_paths,
            skipped=[item.model_dump(exclude_none=True) for item in req.skipped],
            errors=[item.model_dump(exclude_none=True) for item in req.errors],
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Corpus not found.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _public_corpus(corpus, include_entries=True)


@app.get("/artifacts")
async def list_artifacts():
    filesystem = app_state.execution_engine.get_capability("filesystem")
    store = _artifact_store()
    if not store:
        raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
    items = []
    for path in store.root.glob("*.json"):
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            required_fields = {
                "id", "filename", "content_type", "size_bytes", "sha256",
                "created_at",
            }
            if not required_fields.issubset(metadata):
                continue
            public_fields = (
                "id", "filename", "source_name", "content_type", "size_bytes", "sha256",
                "created_at", "document_title", "document_blocks",
                "document_parser", "document_parser_version",
                "document_chunks",
            )
            items.append(
                {key: metadata[key] for key in public_fields if key in metadata}
            )
        except (OSError, KeyError, json.JSONDecodeError):
            continue
    return sorted(items, key=lambda item: item["created_at"], reverse=True)

@app.get("/artifacts/search")
async def search_artifacts(
    q: str,
    limit: int = 8,
    artifact_id: Optional[List[str]] = None,
    content_type: Optional[List[str]] = None,
    heading: Optional[str] = None,
    kind: Optional[List[str]] = None,
):
    store = _artifact_store()
    if not store:
        raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="A non-empty local search query is required.")
    results = store.search_documents(
        query,
        limit=max(1, min(int(limit), 100)),
        artifact_ids=artifact_id,
        content_types=content_type,
        heading=heading,
        kinds=kind,
    )
    return {
        "query": query,
        "results": results,
        "index": store.document_index.stats(),
    }

@app.get("/artifacts/{artifact_id}/preview")
async def preview_artifact(artifact_id: str):
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    filesystem = app_state.execution_engine.get_capability("filesystem")
    if not filesystem:
        raise HTTPException(status_code=500, detail="Filesystem capability not initialized.")
    store = _artifact_store()
    if not store:
        raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
    try:
        metadata = store.get(artifact_id)
        path = Path(metadata["path"])
        if metadata["content_type"] in ArtifactStore.DOCUMENT_TYPES:
            document = store.document(artifact_id)
            return {
                "id": metadata["id"],
                "filename": metadata["filename"],
                "preview_type": "document",
                "content_type": metadata["content_type"],
                "text": document.to_markdown().rstrip("\n"),
                "document": {
                    "id": document.id,
                    "title": document.title,
                    "parser": document.parser,
                    "parser_version": document.parser_version,
                    "block_count": len(document.blocks),
                },
            }
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"id": metadata["id"], "filename": metadata["filename"], "preview_type": "image", "content_type": metadata["content_type"], "data_url": f"data:{metadata['content_type']};base64,{encoded}"}
    except (ValueError, FileNotFoundError, OSError, KeyError) as exc:
        raise HTTPException(status_code=404, detail="Artifact not found.") from exc

@app.delete("/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str):
    filesystem = app_state.execution_engine.get_capability("filesystem")
    store = _artifact_store()
    if not store:
        raise HTTPException(status_code=500, detail="Artifact storage not initialized.")
    try:
        store.delete(artifact_id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Artifact not found.") from exc
    return {"status": "deleted"}

@app.post("/skills", status_code=201)
async def save_skill(req: SkillRequest):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", req.name.lower()):
        raise HTTPException(status_code=400, detail="Invalid skill name.")
    registry = app_state.execution_engine.skill_registry
    filesystem = app_state.execution_engine.get_capability("filesystem")
    if not registry or not filesystem:
        raise HTTPException(status_code=500, detail="Skill registry not initialized.")
    skill_dir = Path(filesystem.workspace_root) / "skills" / req.name.lower()
    skill_dir.mkdir(parents=True, exist_ok=True)
    supported = {"filesystem", "shell", "agent", "devteam"}
    requested = {cap.lower() for cap in req.allowed_capabilities}
    unsupported = sorted(requested - supported)
    if unsupported:
        raise HTTPException(status_code=400, detail=f"Unsupported capabilities: {', '.join(unsupported)}")
    if not req.instructions.strip():
        raise HTTPException(status_code=400, detail="Skill instructions are required.")
    content = "---\nname: %s\ndescription: %s\nallowed_capabilities: [%s]\n---\n\n%s\n" % (
        req.name.lower(), json.dumps(req.description.strip(), ensure_ascii=False),
        ", ".join(sorted(requested)), req.instructions.strip(),
    )
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    registry.discover(str(Path(filesystem.workspace_root) / "skills"))
    skill = registry.skills[req.name.lower()]
    return _skill_payload(skill, filesystem.workspace_root)

@app.post("/skills/import", status_code=201)
async def import_skill(req: SkillImportRequest):
    if not app_state.execution_engine or not app_state.execution_engine.skill_registry:
        raise HTTPException(status_code=500, detail="Skill registry not initialized.")
    fields, instructions = app_state.execution_engine.skill_registry._frontmatter(req.content)
    try:
        parsed = SkillRequest(name=str(fields.get("name") or ""), description=str(fields.get("description") or ""), instructions=instructions, allowed_capabilities=list(fields.get("allowed_capabilities") or []))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid SKILL.md frontmatter.") from exc
    return await save_skill(parsed)

@app.post("/skills/{name}/validate")
async def validate_skill(name: str):
    if not app_state.execution_engine or not app_state.execution_engine.skill_registry:
        raise HTTPException(status_code=500, detail="Skill registry not initialized.")
    registry = app_state.execution_engine.skill_registry
    skill = registry.skills.get(name.lower())
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found.")
    report = registry.compatibility_report(skill.source_path)
    report.update({"name": skill.name, "valid": bool(skill.instructions.strip()) and not report["unsupported"], "digest": skill.digest, "allowed_capabilities": skill.allowed_capabilities})
    return report

@app.delete("/skills/{name}")
async def delete_skill(name: str):
    registry = app_state.execution_engine.skill_registry
    skill = registry.skills.get(name.lower()) if registry else None
    filesystem = app_state.execution_engine.get_capability("filesystem")
    workspace_skills = (Path(filesystem.workspace_root) / "skills").resolve()
    if not skill or workspace_skills not in Path(skill.source_path).resolve().parents:
        raise HTTPException(status_code=404, detail="Only workspace skills can be deleted.")
    skill_directory = Path(skill.source_path).resolve().parent
    shutil.rmtree(skill_directory)
    registry.skills.pop(name.lower(), None)
    return {"status": "deleted"}

@app.get("/memory")
async def list_memory(
    q: str = "", validated: Optional[bool] = None, scope: str = "",
    project_id: Optional[str] = None, kind: str = "", include_global: bool = False,
):
    provider = app_state.execution_engine.context_engine.memory_provider
    if hasattr(provider, "list_memories"):
        return provider.list_memories(
            query=q, validated=validated, scope=scope or None,
            project_id=project_id, kind=kind or None, include_global=include_global,
        )
    items = list(getattr(provider, "memories", []))
    if q:
        query = q.casefold()
        items = [item for item in items if query in str(item.get("value", "")).casefold()]
    if validated is not None:
        items = [item for item in items if bool(item.get("validated", False)) is validated]
    return items

@app.post("/memory", status_code=201)
async def create_memory(req: MemoryRequest):
    provider = app_state.execution_engine.context_engine.memory_provider
    memory_id = await provider.store(
        req.value, metadata=req.metadata, provenance=req.provenance,
        validated=req.validated, ttl_seconds=req.ttl_seconds, kind=req.kind,
        scope=req.scope, project_id=req.project_id, user_id=req.user_id,
        source_execution_id=req.source_execution_id,
        source_artifacts=req.source_artifacts, supersedes_id=req.supersedes_id,
    )
    return {"id": memory_id, "status": "created"}

@app.put("/memory/{memory_id}")
async def update_memory(memory_id: str, req: MemoryRequest):
    provider = app_state.execution_engine.context_engine.memory_provider
    if not hasattr(provider, "update") or not await provider.update(
        memory_id, value=req.value, metadata=req.metadata,
        provenance=req.provenance, validated=req.validated,
        ttl_seconds=req.ttl_seconds, kind=req.kind, scope=req.scope,
        project_id=req.project_id, user_id=req.user_id,
        source_execution_id=req.source_execution_id,
        source_artifacts=req.source_artifacts, supersedes_id=req.supersedes_id,
    ):
        raise HTTPException(status_code=404, detail="Memory not found.")
    return {"status": "updated"}

@app.post("/memory/{memory_id}/validate")
async def validate_memory(memory_id: str):
    provider = app_state.execution_engine.context_engine.memory_provider
    if not await provider.validate(memory_id, validated_by="gui"):
        raise HTTPException(status_code=404, detail="Memory not found.")
    return {"status": "validated"}

@app.delete("/memory/{memory_id}")
async def delete_memory(memory_id: str):
    provider = app_state.execution_engine.context_engine.memory_provider
    if not await provider.delete(memory_id):
        raise HTTPException(status_code=404, detail="Memory not found.")
    return {"status": "deleted"}

@app.get("/skills")
async def list_skills():
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    registry = app_state.execution_engine.skill_registry
    if not registry:
        return []
    filesystem = app_state.execution_engine.get_capability("filesystem")
    return sorted([_skill_payload(skill, filesystem.workspace_root) for skill in registry.skills.values()], key=lambda item: item["name"])

@app.get("/agent-profiles")
async def list_agent_profiles():
    if not app_state.execution_engine or not app_state.execution_engine.agent_profile_registry:
        return []
    registry = app_state.execution_engine.agent_profile_registry
    registry.discover()
    return sorted(registry.profiles.values(), key=lambda item: str(item.get("name", "")).lower())

@app.get("/evolution")
async def evolution_status():
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    lifecycle = app_state.execution_engine.skill_lifecycle
    return lifecycle.diagnostics() if lifecycle else {"creation_enabled": False, "generated_skills": []}

def _skill_payload(skill, workspace_root: str) -> Dict[str, Any]:
    workspace_skills = (Path(workspace_root) / "skills").resolve()
    source = Path(skill.source_path).resolve()
    editable = workspace_skills == source.parent or workspace_skills in source.parents
    return {"name": skill.name, "description": skill.description, "instructions": skill.instructions, "allowed_capabilities": skill.allowed_capabilities, "digest": skill.digest, "editable": editable}

@app.get("/executions")
async def list_executions():
    if not app_state.state_engine:
        raise HTTPException(status_code=500, detail="State engine not initialized.")
    
    results = []
    for exec_id, state in app_state.state_engine.executions.items():
        task_text = str(state.variables.get("task") or "").strip()
        results.append({
            "execution_id": exec_id,
            "status": state.status,
            "current_step": state.current_step,
            "steps_count": len(state.current_plan.get("steps", [])) if state.current_plan else 0,
            "parent_execution_id": state.variables.get("parent_execution_id"),
            "role_name": state.variables.get("role_name"),
            "project_id": state.variables.get("project_id", "proj-default"),
            "planning_mode": normalize_planning_mode(state.variables.get("planning_mode")),
            "task_title": state.variables.get("task_title") or task_title_from_text(task_text),
            "task": task_text[:160],
        })
    return results

@app.get("/executions/{execution_id}")
async def get_execution(execution_id: str):
    if not app_state.state_engine:
        raise HTTPException(status_code=500, detail="State engine not initialized.")
    
    if execution_id not in app_state.state_engine.executions:
        raise HTTPException(status_code=404, detail="Execution not found.")
        
    state = app_state.state_engine.get_execution(execution_id)
    convo = app_state.state_engine.get_conversation(execution_id)
    
    return {
        "execution_id": execution_id,
        "status": state.status,
        "current_step": state.current_step,
        "plan": state.current_plan,
        "variables": _public_execution_variables(state.variables),
        "results": state.results,
        "messages": convo.messages
    }

@app.get("/executions/{execution_id}/delivery")
async def get_execution_delivery(execution_id: str, download: bool = False):
    if not app_state.state_engine or execution_id not in app_state.state_engine.executions:
        raise HTTPException(status_code=404, detail="Execution not found.")
    state = app_state.state_engine.get_execution(execution_id)
    package = state.results.get("delivery_package")
    if not isinstance(package, dict) or not package.get("archive_path"):
        raise HTTPException(status_code=404, detail="No professional delivery package is available.")
    archive = Path(str(package["archive_path"])).resolve()
    filesystem = _filesystem_capability()
    if not filesystem:
        raise HTTPException(status_code=500, detail="Filesystem capability not initialized.")
    workspace = Path(filesystem._get_workspace_for_execution(execution_id)).resolve()
    delivery_root = (workspace / ".gptmoss" / "deliveries" / execution_id).resolve()
    if archive.parent != delivery_root or not archive.is_file():
        raise HTTPException(status_code=404, detail="Delivery archive is unavailable.")
    if download:
        return FileResponse(archive, media_type="application/zip", filename=archive.name)
    return {
        key: value for key, value in package.items()
        if key not in {"docx_path", "manifest_path", "archive_path"}
    } | {"download_url": f"/executions/{execution_id}/delivery?download=true"}


@app.get("/executions/{execution_id}/document")
async def get_execution_document_state(execution_id: str):
    """Return restartable long-document progress for the GUI and integrations."""
    if not app_state.state_engine or execution_id not in app_state.state_engine.executions:
        raise HTTPException(status_code=404, detail="Execution not found")
    state = app_state.state_engine.get_execution(execution_id)
    checkpoint = state.variables.get("document_model_checkpoint")
    model = None
    checkpoint_available = False
    filesystem = _filesystem_capability()
    if checkpoint and filesystem:
        workspace = Path(filesystem._get_workspace_for_execution(execution_id)).resolve()
        checkpoint_root = workspace / ".gptmoss" / "document-state"
        path = checkpoint_root / f"{execution_id}.document.json"
        expected_relative = str(Path(".gptmoss") / "document-state" / path.name).replace("\\", "/")
        if (
            checkpoint_root.is_dir()
            and str(checkpoint).replace("\\", "/") == expected_relative
            and path.is_file()
        ):
            try:
                store = DocumentModelStore(checkpoint_root)
                loaded = store.load(execution_id)
                model = loaded.to_dict() if loaded is not None else None
                checkpoint_available = model is not None
            except ValueError:
                model = None
    if model is None:
        model = {
            "status": "not_initialized",
            "execution_id": execution_id,
            "sections": state.variables.get("document_sections", []),
        }
    sections = model.get("sections", []) if isinstance(model, dict) else []
    completed = sum(
        1 for item in sections
        if isinstance(item, dict) and (item.get("content") or item.get("status") == "complete")
    )
    return {
        "execution_id": execution_id,
        "status": model.get("status", "unknown"),
        "checkpoint_available": checkpoint_available,
        "revision": model.get("revision", 0),
        "section_count": len(sections),
        "completed_sections": completed,
        "progress": round(completed / len(sections), 3) if sections else 0.0,
        "sections": [
            {
                "section_id": item.get("contract", {}).get("section_id", item.get("section_id", "")),
                "heading": item.get("contract", {}).get("heading", item.get("heading", "")),
                "status": item.get("contract", {}).get("status", item.get("status", "pending")),
                "word_count": item.get("word_count", 0),
            }
            for item in sections if isinstance(item, dict)
        ],
    }

@app.get("/executions/{execution_id}/subagents")
async def list_subagents(execution_id: str):
    if not app_state.state_engine or execution_id not in app_state.state_engine.executions:
        raise HTTPException(status_code=404, detail="Parent execution not found.")
    return [
        {
            "execution_id": child_id, "status": state.status,
            "current_step": state.current_step,
            "role_name": state.variables.get("role_name", "Sous-agent"),
        }
        for child_id, state in app_state.state_engine.executions.items()
        if state.variables.get("parent_execution_id") == execution_id
    ]

@app.post("/executions/{execution_id}/subagents", status_code=201)
async def create_subagent(execution_id: str, req: SubAgentRequest):
    if not app_state.kernel or not app_state.state_engine:
        raise HTTPException(status_code=500, detail="Runtime kernel not initialized.")
    if execution_id not in app_state.state_engine.executions:
        raise HTTPException(status_code=404, detail="Parent execution not found.")
    child_id = await app_state.kernel.submit_task(
        req.task.strip(),
        child_agent_config(
            app_state.state_engine,
            execution_id,
            system_prompt=req.system_prompt.strip(),
            role_name=req.role_name.strip(),
        ),
    )
    return {"execution_id": child_id, "parent_execution_id": execution_id, "status": "running"}

@app.get("/api/diagnostics")
async def get_diagnostics():
    if not app_state.execution_engine or not app_state.state_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    engine = app_state.execution_engine
    capabilities = []
    for name, instance in sorted(engine._capabilities.items()):
        capabilities.append({
            "name": name,
            "description": getattr(instance.__class__, "__capability_description__", ""),
            "actions": sorted(getattr(instance, "actions", {}).keys()),
        })
    statuses: Dict[str, int] = {}
    for state in app_state.state_engine.executions.values():
        statuses[state.status] = statuses.get(state.status, 0) + 1
    events = list(engine.telemetry.events[-100:])
    return {
        "model": getattr(engine.llm_provider, "default_model", ""),
        "base_url": getattr(engine.llm_provider, "base_url", ""),
        "supports_vision": bool(getattr(engine.llm_provider, "supports_vision", False)),
        "vision_mode": getattr(engine.llm_provider, "vision_mode", "auto"),
        "native_tool_calling": getattr(engine.llm_provider, "_native_tools_supported", None),
        "learned_context_chars": getattr(engine.llm_provider, "_learned_context_chars", None),
        "configured_context_window_tokens": getattr(engine.llm_provider, "context_window_tokens", 0),
        "learned_context_window_tokens": getattr(engine.llm_provider, "_learned_context_tokens", None),
        "effective_context_window_tokens": getattr(engine.llm_provider, "effective_context_window_tokens", None),
        "context_input_budget_tokens": getattr(engine.llm_provider, "context_input_budget_tokens", None),
        "context_output_reserve_tokens": getattr(engine.llm_provider, "context_output_reserve_tokens", None),
        "capabilities": capabilities,
        "execution_statuses": statuses,
        "metrics": engine.telemetry.metrics(),
        "recent_events": events,
        "errors": [event for event in events if "fail" in event.get("event_type", "").lower() or "error" in event.get("event_type", "").lower()],
    }

@app.get("/executions/{execution_id}/metrics")
async def get_execution_metrics(execution_id: str):
    if not app_state.execution_engine or not app_state.state_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    if execution_id not in app_state.state_engine.executions:
        raise HTTPException(status_code=404, detail="Execution not found.")
    return app_state.execution_engine.telemetry.metrics(execution_id)

@app.get("/executions/{execution_id}/unified-feed")
async def get_unified_feed(execution_id: str):
    if not app_state.state_engine:
        raise HTTPException(status_code=500, detail="State engine not initialized.")
        
    if execution_id not in app_state.state_engine.executions:
        raise HTTPException(status_code=404, detail="Execution not found.")
        
    # Gather execution and all descendants (BFS traversal)
    to_visit = [execution_id]
    visited = []
    
    while to_visit:
        curr = to_visit.pop(0)
        visited.append(curr)
        for child_id, child_state in app_state.state_engine.executions.items():
            parent_id = child_state.variables.get("parent_execution_id")
            if parent_id == curr and child_id not in visited and child_id not in to_visit:
                to_visit.append(child_id)
                
    # Gather and annotate all messages
    unified_messages = []
    for exec_id in visited:
        state = app_state.state_engine.get_execution(exec_id)
        convo = app_state.state_engine.get_conversation(exec_id)
        
        role_name = state.variables.get("role_name") or "Coordinateur"
        for idx, msg in enumerate(convo.messages):
            # Skip synthetic context injection prompts for sub-agents to avoid duplicating main tasks in the conversation log
            if exec_id != execution_id and msg.get("role") == "user":
                continue
                
            msg_copy = dict(msg)
            msg_copy["sender_role"] = role_name
            msg_copy["execution_id"] = exec_id
            # Default timestamp to idx offset if not present to keep sequential messages ordered
            if "timestamp" not in msg_copy:
                msg_copy["timestamp"] = 0.0 + (idx * 0.000001)
            unified_messages.append(msg_copy)
            
    # Sort chronologically by timestamp
    unified_messages.sort(key=lambda m: m.get("timestamp", 0.0))
    return unified_messages

@app.post("/executions/{execution_id}/approve")
async def approve_execution(execution_id: str, req: DecisionRequest):
    if not app_state.execution_engine or not app_state.state_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    state = app_state.state_engine.get_execution(execution_id)
    if state.status != "paused":
        raise HTTPException(status_code=400, detail="Execution is not in paused state.")
        
    if state.variables.get("pending_scope_approval"):
        await app_state.execution_engine.resolve_scope_approval(
            execution_id, decision="allow", reason=req.reason
        )
    else:
        await app_state.execution_engine.resume_with_decision(
            execution_id, decision="allow", reason=req.reason
        )
    return {"status": "resumed", "decision": "allow"}

@app.post("/executions/{execution_id}/reject")
async def reject_execution(execution_id: str, req: DecisionRequest):
    if not app_state.execution_engine or not app_state.state_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    state = app_state.state_engine.get_execution(execution_id)
    if state.status != "paused":
        raise HTTPException(status_code=400, detail="Execution is not in paused state.")
        
    # Resume with rejection
    if state.variables.get("pending_scope_approval"):
        await app_state.execution_engine.resolve_scope_approval(
            execution_id, decision="reject", reason=req.reason
        )
        return {"status": "failed", "decision": "reject"}
    await app_state.execution_engine.resume_with_decision(
        execution_id, decision="reject", reason=req.reason
    )
    return {"status": "resumed", "decision": "reject"}

@app.post("/executions/{execution_id}/pause")
async def pause_execution(execution_id: str):
    if not app_state.state_engine or not app_state.event_bus:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    state = app_state.state_engine.get_execution(execution_id)
    if state.status != "running":
        raise HTTPException(status_code=400, detail=f"Cannot pause execution in status '{state.status}'.")
        
    app_state.state_engine.transition_execution(
        state, "paused", reason="manual pause", actor="api"
    )
    if app_state.execution_engine:
        await app_state.execution_engine.cancel_active_execution(execution_id)
    await app_state.event_bus.publish(Event(
        type="ExecutionPaused",
        payload={"execution_id": execution_id}
    ))
    return {"status": "paused"}

@app.post("/executions/{execution_id}/resume")
async def resume_execution(execution_id: str):
    if not app_state.state_engine or not app_state.execution_engine or not app_state.event_bus:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    state = app_state.state_engine.get_execution(execution_id)
    if state.status not in ("paused", "waiting_provider", "running", "failed"):
        raise HTTPException(status_code=400, detail=f"Cannot resume execution in status '{state.status}'.")

    if state.status == "failed" and state.variables.get("parent_execution_id"):
        raise HTTPException(
            status_code=400,
            detail="Failed delegated executions cannot be resumed directly; resume their top-level parent.",
        )
        
    # If paused on approval, the user must use /approve or /reject.
    # Otherwise, if it was manually paused, just set back to running and resume.
    if "pending_approval" in state.variables:
        raise HTTPException(
            status_code=400,
            detail="Execution is paused waiting for capability approval. Use /approve or /reject endpoint."
        )
    if "pending_scope_approval" in state.variables:
        raise HTTPException(
            status_code=400,
            detail="Execution is paused waiting for scope approval. Use /approve or /reject endpoint."
        )

    # Pausing an active delegated step cancels its in-flight child task while
    # preserving the parent step as pending.  Never reuse that terminal child
    # on resume: a fresh specialist must inherit the durable workspace edits
    # and the current runtime/tool schemas.
    for step in (state.current_plan or {}).get("steps", []):
        if step.get("status") != "pending":
            continue
        assigned_id = step.get("assigned_execution_id")
        assigned = app_state.state_engine.executions.get(assigned_id) if assigned_id else None
        if assigned is not None and assigned.status == "cancelled":
            step.pop("assigned_execution_id", None)
        
    if state.status == "failed":
        steps = (state.current_plan or {}).get("steps", [])
        failed_step = next(
            (step for step in steps if step.get("status") == "failed"),
            None,
        )
        if failed_step:
            failed_step["status"] = "pending"
            failed_step.pop("assigned_execution_id", None)
            failed_step.pop("error", None)
            failed_step["manual_retry_count"] = int(
                failed_step.get("manual_retry_count", 0)
            ) + 1
            # A failed loop may have exhausted its persisted iteration or
            # stagnation budget.  Manual resume is an explicit fresh attempt,
            # so carrying that runtime forward would fail again immediately
            # before the model can make progress.
            step_runtime = state.variables.get("step_runtime")
            if isinstance(step_runtime, dict):
                step_runtime.pop(str(failed_step.get("id")), None)
        state.results.pop("error", None)
        state.variables["manual_failure_resumes"] = int(
            state.variables.get("manual_failure_resumes", 0)
        ) + 1

    app_state.state_engine.transition_execution(
        state, "running", reason="manual resume", actor="api"
    )
    await app_state.event_bus.publish(Event(
        type="ExecutionResumed",
        payload={"execution_id": execution_id, "decision": "manual"}
    ))
    
    convo = app_state.state_engine.get_conversation(execution_id)
    task = state.variables.get("task") or convo.messages[0]["content"]
    if task.startswith("Task: "):
        task = task[6:]
        
    # Rerun loop
    app_state.execution_engine.start_execution(execution_id, task)
    return {"status": "running"}

@app.post("/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str):
    if not app_state.state_engine or not app_state.event_bus or not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    state = app_state.state_engine.get_execution(execution_id)
    if state.status not in ("running", "paused", "pending", "waiting_provider"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel execution in status '{state.status}'.")
        
    to_cancel = [execution_id]
    cancelled = []
    while to_cancel:
        current_id = to_cancel.pop(0)
        current = app_state.state_engine.get_execution(current_id)
        if current.status in {"running", "paused", "pending", "waiting_provider"}:
            app_state.state_engine.transition_execution(
                current, "cancelled", reason="manual cancellation", actor="api"
            )
            current.variables.pop("pending_approval", None)
            current.variables.pop("pending_scope_approval", None)
            cancelled.append(current_id)
            shell = app_state.execution_engine.get_capability("shell")
            if shell and hasattr(shell, "cancel_execution"):
                shell.cancel_execution(current_id)
            await app_state.event_bus.publish(Event(
                type="ExecutionCancelled",
                payload={"execution_id": current_id}
            ))
        for child_id, child in app_state.state_engine.executions.items():
            if (
                child.variables.get("parent_execution_id") == current_id
                and child_id not in cancelled
                and child_id not in to_cancel
            ):
                to_cancel.append(child_id)
    await asyncio.gather(*(
        app_state.execution_engine.cancel_active_execution(exec_id)
        for exec_id in cancelled
    ))
    app_state.state_engine.save_to_disk()
    return {"status": "cancelled", "execution_ids": cancelled}

@app.delete("/executions/{execution_id}")
async def delete_execution(execution_id: str):
    if not app_state.state_engine or not app_state.event_bus:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    if execution_id not in app_state.state_engine.executions:
        raise HTTPException(status_code=404, detail="Execution not found.")

    subtree = _descendant_ids(app_state.state_engine, execution_id)
    if _has_active_execution(app_state.state_engine, subtree):
        raise HTTPException(
            status_code=409,
            detail="Cannot delete an active execution. Cancel it first.",
        )

    deleted = []
    for exec_id in subtree:
        app_state.state_engine.executions.pop(exec_id, None)
        app_state.state_engine.conversations.pop(exec_id, None)
        deleted.append(exec_id)
        
    app_state.state_engine.save_to_disk()
    
    await app_state.event_bus.publish(Event(
        type="TaskDeleted",
        payload={"execution_id": execution_id}
    ))
    return {"status": "deleted"}

@app.post("/executions/clear-all")
async def clear_all_executions():
    if not app_state.state_engine or not app_state.event_bus:
        raise HTTPException(status_code=500, detail="Engine not initialized.")

    if _has_active_execution(app_state.state_engine):
        raise HTTPException(
            status_code=409,
            detail="Cannot clear history while an execution is still active. Cancel running tasks first.",
        )

    app_state.state_engine.executions.clear()
    app_state.state_engine.conversations.clear()
    app_state.state_engine.save_to_disk()
    
    await app_state.event_bus.publish(Event(
        type="TasksCleared",
        payload={}
    ))
    return {"status": "all_cleared"}

# WebSocket Endpoints
@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket):
    await manager.connect_global(websocket)
    try:
        while True:
            # Keep-alive loop, discard incoming client messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_global(websocket)

@app.websocket("/ws/executions/{execution_id}")
async def ws_execution_events(websocket: WebSocket, execution_id: str):
    await manager.connect_execution(execution_id, websocket)
    try:
        while True:
            # Keep-alive loop, discard incoming client messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_execution(execution_id, websocket)

def _audit_path() -> Path:
    filesystem = app_state.execution_engine.get_capability("filesystem")
    return Path(filesystem.workspace_root).resolve() / "settings_audit.jsonl"

def _append_audit(action: str, changed_fields: Optional[List[str]] = None, sensitive: bool = False) -> None:
    event = {
        "timestamp": time.time(), "action": action,
        "changed_fields": sorted(field for field in (changed_fields or []) if field != "api_key"),
        "secret_changed": "api_key" in (changed_fields or []), "sensitive": sensitive,
    }
    try:
        path = _audit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        logger.warning("Unable to write the local settings audit log.")

def _validate_provider_url(base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="Base URL must be a plain HTTP(S) provider URL without credentials, query, or fragment.")

@app.get("/api/audit")
async def get_audit():
    try:
        path = _audit_path()
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines()[-200:]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Unable to read audit log.") from exc

@app.post("/api/settings/reveal-secret")
async def reveal_secret(req: ConfirmationRequest, request: Request, response: Response):
    if not req.confirm:
        raise HTTPException(status_code=409, detail="Explicit confirmation is required.")
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(status_code=403, detail="Secrets can only be revealed from the local machine.")
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    _append_audit("secret_revealed", sensitive=True)
    return {"api_key": getattr(app_state.execution_engine.llm_provider, "api_key", "")}

@app.get("/api/settings")
async def get_settings():
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    
    config_path = _runtime_config_path()
    if config_path and config_path.exists():
        config = _load_runtime_config()
        config.pop("api_key", None)
        config.setdefault("max_step_iterations", 30)
        config.setdefault("vision_mode", "auto")
        config.setdefault("max_step_retries", 2)
        config.setdefault("max_parallel_plan_steps", 0)
        config.setdefault("max_context_chars", 12_000)
        config.setdefault("context_window_tokens", 0)
        config.setdefault("context_output_reserve_tokens", 8_192)
        config.setdefault("max_upload_bytes", DEFAULT_MAX_UPLOAD_BYTES)
        config.setdefault("max_attachment_text_chars", DEFAULT_MAX_ATTACHMENT_TEXT_CHARS)
        config.setdefault("max_transitions_per_execution", DEFAULT_MAX_TRANSITIONS_PER_EXECUTION)
        config.setdefault("safe_shell_mode", True)
        config.setdefault("shell_timeout_seconds", 0)
        config.setdefault("shell_max_output_chars", 12_000)
        config.setdefault("default_skills", [])
        config.setdefault("workspace_full_autonomy", False)
        config.setdefault("continue_while_progress", True)
        config.setdefault("adaptive_resource_management", True)
        config.setdefault("strict_skill_capabilities", False)
        config.setdefault("allow_nested_delegation", True)
        config.setdefault("max_delegation_depth", 0)
        config.setdefault("autonomous_specialization", True)
        config.setdefault("autonomous_skill_creation", True)
        config.setdefault("autonomous_skill_improvement", True)
        config.setdefault("skill_coverage_threshold", 4)
        config.setdefault("max_autonomous_skills_per_execution", 0)
        config.setdefault("document_engine_enabled", True)
        config.setdefault("document_checkpoint_enabled", True)
        config.setdefault("document_target_section_words", 450)
        config.setdefault("diagram_rendering", True)
        config.setdefault("docx_embed_diagrams", True)
        return config
            
    # Fallback to current memory values
    llm = app_state.execution_engine.llm_provider
    policy = app_state.execution_engine.policy_provider
    fs = app_state.execution_engine.get_capability("filesystem")
    return {
        "base_url": getattr(llm, "base_url", ""),
        "model_name": getattr(llm, "default_model", ""),
        "vision_mode": getattr(llm, "vision_mode", "auto"),
        "ssl_verify": bool(getattr(llm, "ssl_verify", True)),
        "ssl_cert_path": getattr(llm, "ssl_cert_path", "") or "",
        "denied_capabilities": getattr(policy, "denied", []),
        "approval_required_capabilities": getattr(policy, "approval_required", []),
        "workspace_full_autonomy": getattr(policy, "workspace_full_autonomy", False),
        "continue_while_progress": getattr(app_state.execution_engine, "continue_while_progress", True),
        "adaptive_resource_management": getattr(app_state.execution_engine, "adaptive_resource_management", True),
        "strict_skill_capabilities": getattr(app_state.execution_engine, "strict_skill_capabilities", False),
        "allow_nested_delegation": getattr(app_state.execution_engine, "allow_nested_delegation", True),
        "max_delegation_depth": getattr(app_state.execution_engine, "max_delegation_depth", 0),
        "autonomous_specialization": getattr(app_state.execution_engine, "autonomous_specialization", True),
        "autonomous_skill_creation": getattr(getattr(app_state.execution_engine, "skill_lifecycle", None), "creation_enabled", True),
        "autonomous_skill_improvement": getattr(getattr(app_state.execution_engine, "skill_lifecycle", None), "improvement_enabled", True),
        "skill_coverage_threshold": getattr(getattr(app_state.execution_engine, "skill_lifecycle", None), "coverage_threshold", 4),
        "max_autonomous_skills_per_execution": getattr(getattr(app_state.execution_engine, "skill_lifecycle", None), "max_skills_per_execution", 0),
        "document_engine_enabled": getattr(app_state.execution_engine, "document_engine_enabled", True),
        "document_checkpoint_enabled": getattr(app_state.execution_engine, "document_checkpoint_enabled", True),
        "document_target_section_words": getattr(app_state.execution_engine, "document_target_section_words", 450),
        "diagram_rendering": getattr(app_state.execution_engine, "diagram_rendering", True),
        "docx_embed_diagrams": getattr(app_state.execution_engine, "docx_embed_diagrams", True),
        "workspace_path": getattr(fs, "workspace_root", "."),
        "restrict_to_workspace": getattr(fs, "restrict_to_workspace", True),
        "allow_subfolders": getattr(fs, "allow_subfolders", True),
        "projects": [{"id": "proj-default", "name": "Projet Par Défaut"}],
        "max_step_iterations": getattr(app_state.execution_engine, "max_step_iterations", 30),
        "max_step_retries": getattr(app_state.execution_engine, "max_step_retries", 2),
        "max_parallel_plan_steps": getattr(
            app_state.execution_engine, "max_parallel_plan_steps", 0
        ),
        "max_context_chars": getattr(app_state.execution_engine.context_engine, "max_history_chars", 12_000),
        "context_window_tokens": getattr(llm, "context_window_tokens", 0),
        "context_output_reserve_tokens": getattr(llm, "context_output_reserve_tokens", 8_192),
        "max_upload_bytes": getattr(_artifact_store(), "max_bytes", 0),
        "max_attachment_text_chars": getattr(_artifact_store(), "max_text_chars", 0),
        "max_transitions_per_execution": getattr(
            app_state.state_engine, "max_transitions_per_execution",
            DEFAULT_MAX_TRANSITIONS_PER_EXECUTION,
        ),
        "safe_shell_mode": True,
        "shell_timeout_seconds": getattr(app_state.execution_engine.get_capability("shell"), "timeout_seconds", 0),
        "shell_max_output_chars": getattr(app_state.execution_engine.get_capability("shell"), "max_output_chars", 12_000),
        "default_skills": []
    }

@app.post("/api/settings/test-connection")
async def test_connection(req: SettingsRequest):
    _validate_provider_url(req.base_url)
    if not req.model_name.strip():
        raise HTTPException(status_code=400, detail="Base URL and model are required.")
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    api_key = req.api_key or getattr(app_state.execution_engine.llm_provider, "api_key", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    verify: Any = req.ssl_cert_path.strip() if req.ssl_verify and req.ssl_cert_path.strip() else req.ssl_verify
    url = req.base_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), verify=verify, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
            chat_response = await client.post(
                req.base_url.rstrip("/") + "/chat/completions",
                headers=headers,
                json={
                    "model": req.model_name,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "max_tokens": 1,
                    "temperature": 0,
                },
            )
            chat_response.raise_for_status()
            chat_payload = chat_response.json()
            if not isinstance(chat_payload.get("choices"), list) or not chat_payload["choices"]:
                raise ValueError("Provider returned no chat completion choice.")
    except httpx.HTTPStatusError as exc:
        endpoint = "chat completions" if exc.request.method == "POST" else "model catalog"
        raise HTTPException(
            status_code=502,
            detail=(
                f"Provider rejected {endpoint} (HTTP {exc.response.status_code}). "
                "Verify the API key and its inference permissions."
            ),
        ) from exc
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"Unable to connect to the provider: {exc}") from exc
    models = [str(item.get("id")) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]
    return {
        "status": "connected",
        "model_available": req.model_name in models,
        "models_count": len(models),
        "chat_completion": True,
    }

@app.post("/api/settings")
async def update_settings(req: SettingsRequest):
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    workspace_root = app_state.execution_engine.get_capability("filesystem").workspace_root
    config_path = _runtime_config_path()
    if not config_path:
        raise HTTPException(status_code=500, detail="Runtime configuration path is unavailable.")
    _validate_provider_url(req.base_url)
    
    policy = app_state.execution_engine.policy_provider
    current_approvals = {str(item).lower() for item in getattr(policy, "approval_required", [])}
    requested_approvals = {item.lower() for item in req.approval_required_capabilities}
    requested_workspace = Path(req.workspace_path).resolve()
    current_workspace = Path(workspace_root).resolve()
    outside_project = any(
        project.get("path") and requested_workspace != Path(str(project["path"])).resolve() and requested_workspace not in Path(str(project["path"])).resolve().parents
        for project in req.projects
    )
    if req.workspace_full_autonomy and (not req.restrict_to_workspace or outside_project):
        raise HTTPException(
            status_code=400,
            detail="Workspace full autonomy requires workspace restriction and project paths inside that workspace.",
        )
    sensitive = (
        not req.ssl_verify or not req.restrict_to_workspace or not req.safe_shell_mode
        or "shell" not in requested_approvals or bool(current_approvals - requested_approvals)
        or req.workspace_full_autonomy != bool(getattr(policy, "workspace_full_autonomy", False))
        or requested_workspace != current_workspace or outside_project
    )
    if sensitive and not req.confirm_sensitive:
        raise HTTPException(status_code=409, detail="Sensitive configuration requires explicit confirmation.")

    llm = app_state.execution_engine.llm_provider
    # A blank settings form must not erase an existing secret.
    api_key = req.api_key or getattr(llm, "api_key", "")
    previous: Dict[str, Any] = _load_runtime_config() if config_path.exists() else {}
    config_data = {
        "api_key": api_key,
        "base_url": req.base_url,
        "model_name": req.model_name,
        "vision_mode": req.vision_mode,
        "ssl_verify": req.ssl_verify,
        "ssl_cert_path": req.ssl_cert_path,
        "denied_capabilities": req.denied_capabilities,
        "approval_required_capabilities": req.approval_required_capabilities,
        "workspace_full_autonomy": req.workspace_full_autonomy,
        "continue_while_progress": req.continue_while_progress,
        "adaptive_resource_management": req.adaptive_resource_management,
        "strict_skill_capabilities": req.strict_skill_capabilities,
        "allow_nested_delegation": req.allow_nested_delegation,
        "max_delegation_depth": req.max_delegation_depth,
        "autonomous_specialization": req.autonomous_specialization,
        "autonomous_skill_creation": req.autonomous_skill_creation,
        "autonomous_skill_improvement": req.autonomous_skill_improvement,
        "skill_coverage_threshold": req.skill_coverage_threshold,
        "max_autonomous_skills_per_execution": req.max_autonomous_skills_per_execution,
        "document_engine_enabled": req.document_engine_enabled,
        "document_checkpoint_enabled": req.document_checkpoint_enabled,
        "document_target_section_words": req.document_target_section_words,
        "diagram_rendering": req.diagram_rendering,
        "docx_embed_diagrams": req.docx_embed_diagrams,
        "workspace_path": req.workspace_path,
        "restrict_to_workspace": req.restrict_to_workspace,
        "allow_subfolders": req.allow_subfolders,
        "projects": req.projects,
        "max_step_iterations": req.max_step_iterations,
        "max_step_retries": req.max_step_retries,
        "max_parallel_plan_steps": req.max_parallel_plan_steps,
        "max_context_chars": req.max_context_chars,
        "context_window_tokens": req.context_window_tokens,
        "context_output_reserve_tokens": req.context_output_reserve_tokens,
        "max_upload_bytes": req.max_upload_bytes,
        "max_attachment_text_chars": req.max_attachment_text_chars,
        "max_transitions_per_execution": req.max_transitions_per_execution,
        "safe_shell_mode": req.safe_shell_mode,
        "shell_timeout_seconds": req.shell_timeout_seconds,
        "shell_max_output_chars": req.shell_max_output_chars,
        "default_skills": [skill.lower() for skill in req.default_skills]
    }
    
    _write_runtime_config(config_data)
        
    if hasattr(llm, "update_config"):
        provider_values = {
            "api_key": api_key,
            "base_url": req.base_url,
            "ssl_verify": req.ssl_verify,
            "ssl_cert_path": req.ssl_cert_path,
            "model_name": req.model_name,
            "context_window_tokens": req.context_window_tokens,
            "context_output_reserve_tokens": req.context_output_reserve_tokens,
        }
        parameters = inspect.signature(llm.update_config).parameters
        llm.update_config(**{
            key: value for key, value in provider_values.items()
            if key in parameters
        })
    if hasattr(llm, "set_vision_mode"):
        llm.set_vision_mode(req.vision_mode)
    if hasattr(policy, "update_policy"):
        policy.update_policy(
            approval_required=req.approval_required_capabilities,
            denied=req.denied_capabilities,
            workspace_full_autonomy=req.workspace_full_autonomy,
        )
    shell = app_state.execution_engine.get_capability("shell")
    if shell and hasattr(shell, "update_safety_config"):
        shell.update_safety_config(
            safe_mode=req.safe_shell_mode,
            timeout_seconds=req.shell_timeout_seconds,
            max_output_chars=req.shell_max_output_chars,
        )
    app_state.execution_engine.default_skills = [skill.lower() for skill in req.default_skills]
    app_state.execution_engine.max_step_iterations = req.max_step_iterations
    app_state.execution_engine.max_step_retries = req.max_step_retries
    app_state.execution_engine.max_parallel_plan_steps = req.max_parallel_plan_steps
    app_state.execution_engine.document_engine_enabled = req.document_engine_enabled
    app_state.execution_engine.document_checkpoint_enabled = req.document_checkpoint_enabled
    app_state.execution_engine.document_target_section_words = req.document_target_section_words
    app_state.execution_engine.diagram_rendering = req.diagram_rendering
    app_state.execution_engine.docx_embed_diagrams = req.docx_embed_diagrams
    app_state.execution_engine.continue_while_progress = req.continue_while_progress
    app_state.execution_engine.adaptive_resource_management = req.adaptive_resource_management
    app_state.execution_engine.strict_skill_capabilities = req.strict_skill_capabilities
    app_state.execution_engine.allow_nested_delegation = req.allow_nested_delegation
    app_state.execution_engine.max_delegation_depth = req.max_delegation_depth
    app_state.execution_engine.context_engine.adaptive = req.adaptive_resource_management
    app_state.execution_engine.context_engine.max_history_chars = req.max_context_chars
    if app_state.state_engine:
        app_state.state_engine.max_transitions_per_execution = req.max_transitions_per_execution
    workspace_changed = Path(workspace_root).resolve() != Path(req.workspace_path).resolve()
    if workspace_changed:
        app_state.execution_engine.artifact_store = ArtifactStore(
            req.workspace_path,
            max_bytes=req.max_upload_bytes,
            max_text_chars=req.max_attachment_text_chars,
        )
    elif app_state.execution_engine.artifact_store:
        app_state.execution_engine.artifact_store.update_limits(
            req.max_upload_bytes,
            req.max_attachment_text_chars,
        )
    if app_state.execution_engine.artifact_store:
        _synchronize_document_capability(
            app_state.execution_engine.artifact_store
        )
    app_state.execution_engine.autonomous_specialization = req.autonomous_specialization
    if workspace_changed or not app_state.execution_engine.agent_profile_registry:
        app_state.execution_engine.agent_profile_registry = AgentProfileRegistry(req.workspace_path)
    lifecycle = app_state.execution_engine.skill_lifecycle
    if workspace_changed or not lifecycle:
        if workspace_changed:
            bundled_skills = Path(CURRENT_DIR).resolve().parent / "skills"
            app_state.execution_engine.skill_registry = SkillRegistry([
                str(bundled_skills), str(Path(req.workspace_path) / "skills"),
            ])
        elif not app_state.execution_engine.skill_registry:
            app_state.execution_engine.skill_registry = SkillRegistry([
                str(Path(CURRENT_DIR).resolve().parent / "skills"),
                str(Path(req.workspace_path) / "skills"),
            ])
        else:
            app_state.execution_engine.skill_registry.discover(str(Path(req.workspace_path) / "skills"))
        lifecycle = AutonomousSkillLifecycle(req.workspace_path, app_state.execution_engine.skill_registry)
        app_state.execution_engine.skill_lifecycle = lifecycle
    lifecycle.creation_enabled = req.autonomous_skill_creation
    lifecycle.improvement_enabled = req.autonomous_skill_improvement
    lifecycle.coverage_threshold = req.skill_coverage_threshold
    lifecycle.max_skills_per_execution = req.max_autonomous_skills_per_execution
        
    for cap_name in ["filesystem", "shell", "agent", "devteam"]:
        cap = app_state.execution_engine.get_capability(cap_name)
        if cap:
            if cap_name == "filesystem" and hasattr(cap, "update_workspace_config"):
                cap.update_workspace_config(
                    workspace_root=req.workspace_path,
                    restrict_to_workspace=req.restrict_to_workspace,
                    allow_subfolders=req.allow_subfolders
                )
            elif hasattr(cap, "update_workspace_config"):
                cap.update_workspace_config(req.workspace_path)
                
    changed_fields = [key for key, value in config_data.items() if previous.get(key) != value]
    _append_audit("settings_updated", changed_fields=changed_fields, sensitive=sensitive)
    return {"status": "success", "message": "Settings updated and persisted successfully.", "changed_fields": [field for field in changed_fields if field != "api_key"], "secret_changed": "api_key" in changed_fields}

def init_app(kernel: RuntimeKernel, exec_engine: ExecutionEngine, state_engine: StateEngine, event_bus: EventBus):
    """Binds runtime dependencies to the FastAPI app state."""
    app_state.kernel = kernel
    app_state.execution_engine = exec_engine
    app_state.state_engine = state_engine
    app_state.event_bus = event_bus
    if state_engine.persist_path:
        app_state.config_path = Path(state_engine.persist_path).resolve().parent / "config.json"
    else:
        filesystem = exec_engine.get_capability("filesystem")
        app_state.config_path = Path(filesystem.workspace_root).resolve() / "config.json" if filesystem else None
    store = _artifact_store()
    if store:
        _synchronize_document_capability(store)
    return app
