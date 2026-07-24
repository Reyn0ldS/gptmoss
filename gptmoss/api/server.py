import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# GUI HTML path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
GUI_FILE_PATH = os.path.join(CURRENT_DIR, "gui.html")

from gptmoss.core import EventBus, Event, StateEngine, RuntimeKernel, ExecutionEngine, DEFAULT_SYSTEM_PROMPT
from gptmoss.core.artifacts import ArtifactStore

logger = logging.getLogger("gptmoss.api")

# Models for request/response validation
class SubmitTaskRequest(BaseModel):
    task: str
    agent_config: Optional[Dict[str, Any]] = None
    project_id: Optional[str] = None
    attachment_ids: List[str] = []

class UploadArtifactRequest(BaseModel):
    filename: str
    content_base64: str
    content_type: str

class DecisionRequest(BaseModel):
    reason: Optional[str] = None

class SkillRequest(BaseModel):
    name: str
    description: str = ""
    instructions: str
    allowed_capabilities: List[str] = Field(default_factory=list)

class SettingsRequest(BaseModel):
    api_key: str = ""
    base_url: str
    model_name: str
    ssl_verify: bool
    ssl_cert_path: str
    denied_capabilities: List[str]
    approval_required_capabilities: List[str]
    workspace_path: str
    restrict_to_workspace: bool
    allow_subfolders: bool
    projects: List[Dict[str, Any]]
    max_step_iterations: int = Field(default=30, ge=1, le=100)
    max_context_chars: int = Field(default=12_000, ge=2_000, le=100_000)
    safe_shell_mode: bool = True
    shell_timeout_seconds: int = Field(default=60, ge=1, le=600)
    shell_max_output_chars: int = Field(default=12_000, ge=1_000, le=100_000)
    default_skills: List[str] = Field(default_factory=list)

class AppState:
    kernel: Optional[RuntimeKernel] = None
    execution_engine: Optional[ExecutionEngine] = None
    state_engine: Optional[StateEngine] = None
    event_bus: Optional[EventBus] = None
    flush_task: Optional[asyncio.Task] = None
    subscribed_event_bus: Optional[EventBus] = None

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

    try:
        yield
    finally:
        if app_state.flush_task:
            app_state.flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await app_state.flush_task
            app_state.flush_task = None

app = FastAPI(title="MOSS Agent Runtime Platform API", version="0.1.0", lifespan=lifespan)

# CORS middleware for potential frontend clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
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

@app.get("/", response_class=HTMLResponse)
async def get_gui():
    if not os.path.exists(GUI_FILE_PATH):
        raise HTTPException(status_code=404, detail="GUI file not found.")
    with open(GUI_FILE_PATH, "r", encoding="utf-8") as f:
        return f.read()

@app.post("/executions", status_code=201)
async def submit_task(req: SubmitTaskRequest):
    if not app_state.kernel:
        raise HTTPException(status_code=500, detail="Runtime kernel not initialized.")
    
    agent_config = req.agent_config or {"system_prompt": DEFAULT_SYSTEM_PROMPT}
    exec_id = await app_state.kernel.submit_task(req.task, agent_config)
    
    state = app_state.state_engine.get_execution(exec_id)
    if state:
        project_id = req.project_id or "proj-default"
        state.variables["project_id"] = project_id
        state.variables["attachment_ids"] = req.attachment_ids
        
        # Resolve and store custom project path from config.json
        try:
            filesystem_cap = app_state.execution_engine.get_capability("filesystem")
            if filesystem_cap:
                workspace_root = filesystem_cap.workspace_root
                config_path = os.path.join(workspace_root, "config.json")
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8") as f:
                        config_data = json.load(f)
                    projects = config_data.get("projects") or []
                    for p in projects:
                        if p.get("id") == project_id and p.get("path"):
                            state.variables["project_path"] = p.get("path")
                            break
        except Exception as e:
            logger = logging.getLogger("gptmoss.api")
            logger.error(f"Error looking up project custom path: {e}")
        
    return {"execution_id": exec_id, "status": "running"}

@app.post("/artifacts", status_code=201)
async def upload_artifact(req: UploadArtifactRequest):
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    filesystem = app_state.execution_engine.get_capability("filesystem")
    if not filesystem:
        raise HTTPException(status_code=500, detail="Filesystem capability not initialized.")
    try:
        metadata = ArtifactStore(filesystem.workspace_root).save_base64(
            req.filename, req.content_base64, req.content_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {key: metadata[key] for key in ("id", "filename", "content_type", "size_bytes", "sha256", "created_at")}


@app.get("/artifacts")
async def list_artifacts():
    filesystem = app_state.execution_engine.get_capability("filesystem")
    store = ArtifactStore(filesystem.workspace_root)
    items = []
    for path in store.root.glob("*.json"):
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
            items.append({key: metadata[key] for key in ("id", "filename", "content_type", "size_bytes", "sha256", "created_at")})
        except (OSError, KeyError, json.JSONDecodeError):
            continue
    return sorted(items, key=lambda item: item["created_at"], reverse=True)

@app.delete("/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str):
    filesystem = app_state.execution_engine.get_capability("filesystem")
    store = ArtifactStore(filesystem.workspace_root)
    try:
        metadata = store.get(artifact_id)
        Path(metadata["path"]).unlink(missing_ok=True)
        (store.root / f"{artifact_id}.json").unlink(missing_ok=True)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Artifact not found.") from exc
    return {"status": "deleted"}

@app.post("/skills", status_code=201)
async def save_skill(req: SkillRequest):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", req.name.lower()):
        raise HTTPException(status_code=400, detail="Invalid skill name.")
    registry = app_state.execution_engine.skill_registry
    filesystem = app_state.execution_engine.get_capability("filesystem")
    skill_dir = Path(filesystem.workspace_root) / "skills" / req.name.lower()
    skill_dir.mkdir(parents=True, exist_ok=True)
    allowed = [cap.lower() for cap in req.allowed_capabilities if cap.lower() in {"filesystem", "shell", "agent", "devteam"}]
    content = "---\nname: %s\ndescription: %s\nallowed_capabilities: [%s]\n---\n\n%s\n" % (req.name.lower(), req.description.strip(), ", ".join(allowed), req.instructions.strip())
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    registry.discover(str(Path(filesystem.workspace_root) / "skills"))
    skill = registry.skills[req.name.lower()]
    return {"name": skill.name, "description": skill.description, "allowed_capabilities": skill.allowed_capabilities, "digest": skill.digest}

@app.delete("/skills/{name}")
async def delete_skill(name: str):
    registry = app_state.execution_engine.skill_registry
    skill = registry.skills.get(name.lower()) if registry else None
    filesystem = app_state.execution_engine.get_capability("filesystem")
    workspace_skills = (Path(filesystem.workspace_root) / "skills").resolve()
    if not skill or workspace_skills not in Path(skill.source_path).resolve().parents:
        raise HTTPException(status_code=404, detail="Only workspace skills can be deleted.")
    Path(skill.source_path).unlink(missing_ok=True)
    Path(skill.source_path).parent.rmdir()
    registry.skills.pop(name.lower(), None)
    return {"status": "deleted"}

@app.get("/memory")
async def list_memory():
    provider = app_state.execution_engine.context_engine.memory_provider
    return getattr(provider, "memories", [])

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
    return [
        {"name": skill.name, "description": skill.description, "allowed_capabilities": skill.allowed_capabilities, "digest": skill.digest}
        for skill in registry.skills.values()
    ]

@app.get("/executions")
async def list_executions():
    if not app_state.state_engine:
        raise HTTPException(status_code=500, detail="State engine not initialized.")
    
    results = []
    for exec_id, state in app_state.state_engine.executions.items():
        results.append({
            "execution_id": exec_id,
            "status": state.status,
            "current_step": state.current_step,
            "steps_count": len(state.current_plan.get("steps", [])) if state.current_plan else 0,
            "parent_execution_id": state.variables.get("parent_execution_id"),
            "role_name": state.variables.get("role_name"),
            "project_id": state.variables.get("project_id", "proj-default")
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
        "variables": state.variables,
        "messages": convo.messages
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
        
    # Resume with approval
    await app_state.execution_engine.resume_with_decision(execution_id, decision="allow", reason=req.reason)
    return {"status": "resumed", "decision": "allow"}

@app.post("/executions/{execution_id}/reject")
async def reject_execution(execution_id: str, req: DecisionRequest):
    if not app_state.execution_engine or not app_state.state_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    state = app_state.state_engine.get_execution(execution_id)
    if state.status != "paused":
        raise HTTPException(status_code=400, detail="Execution is not in paused state.")
        
    # Resume with rejection
    await app_state.execution_engine.resume_with_decision(execution_id, decision="reject", reason=req.reason)
    return {"status": "resumed", "decision": "reject"}

@app.post("/executions/{execution_id}/pause")
async def pause_execution(execution_id: str):
    if not app_state.state_engine or not app_state.event_bus:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    state = app_state.state_engine.get_execution(execution_id)
    if state.status != "running":
        raise HTTPException(status_code=400, detail=f"Cannot pause execution in status '{state.status}'.")
        
    state.status = "paused"
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
    if state.status != "paused":
        raise HTTPException(status_code=400, detail=f"Cannot resume execution in status '{state.status}'.")
        
    # If paused on approval, the user must use /approve or /reject.
    # Otherwise, if it was manually paused, just set back to running and resume.
    if "pending_approval" in state.variables:
        raise HTTPException(
            status_code=400,
            detail="Execution is paused waiting for capability approval. Use /approve or /reject endpoint."
        )
        
    state.status = "running"
    await app_state.event_bus.publish(Event(
        type="ExecutionResumed",
        payload={"execution_id": execution_id, "decision": "manual"}
    ))
    
    convo = app_state.state_engine.get_conversation(execution_id)
    task = convo.messages[0]["content"]
    if task.startswith("Task: "):
        task = task[6:]
        
    # Rerun loop
    asyncio.create_task(app_state.execution_engine.execute_task(execution_id, task))
    return {"status": "running"}

@app.post("/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str):
    if not app_state.state_engine or not app_state.event_bus:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    state = app_state.state_engine.get_execution(execution_id)
    if state.status not in ("running", "paused", "pending"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel execution in status '{state.status}'.")
        
    state.status = "cancelled"
    await app_state.event_bus.publish(Event(
        type="ExecutionCancelled",
        payload={"execution_id": execution_id}
    ))
    return {"status": "cancelled"}

@app.delete("/executions/{execution_id}")
async def delete_execution(execution_id: str):
    if not app_state.state_engine or not app_state.event_bus:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    if execution_id not in app_state.state_engine.executions:
        raise HTTPException(status_code=404, detail="Execution not found.")
        
    # Cascade delete all descendants
    to_delete = [execution_id]
    deleted = []
    while to_delete:
        curr = to_delete.pop(0)
        deleted.append(curr)
        for child_id, child_state in list(app_state.state_engine.executions.items()):
            parent_id = child_state.variables.get("parent_execution_id")
            if parent_id == curr and child_id not in deleted and child_id not in to_delete:
                to_delete.append(child_id)
                
    for exec_id in deleted:
        app_state.state_engine.executions.pop(exec_id, None)
        app_state.state_engine.conversations.pop(exec_id, None)
        
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

@app.get("/api/settings")
async def get_settings():
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
    
    workspace_root = app_state.execution_engine.get_capability("filesystem").workspace_root
    config_path = os.path.join(workspace_root, "config.json")
    
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        config.pop("api_key", None)
        config.setdefault("max_step_iterations", 30)
        config.setdefault("max_context_chars", 12_000)
        config.setdefault("safe_shell_mode", True)
        config.setdefault("shell_timeout_seconds", 60)
        config.setdefault("shell_max_output_chars", 12_000)
        config.setdefault("default_skills", [])
        return config
            
    # Fallback to current memory values
    llm = app_state.execution_engine.llm_provider
    policy = app_state.execution_engine.policy_provider
    fs = app_state.execution_engine.get_capability("filesystem")
    return {
        "base_url": getattr(llm, "base_url", ""),
        "model_name": getattr(llm, "default_model", ""),
        "ssl_verify": False,
        "ssl_cert_path": "",
        "denied_capabilities": getattr(policy, "denied", []),
        "approval_required_capabilities": getattr(policy, "approval_required", []),
        "workspace_path": getattr(fs, "workspace_root", "."),
        "restrict_to_workspace": getattr(fs, "restrict_to_workspace", True),
        "allow_subfolders": getattr(fs, "allow_subfolders", True),
        "projects": [{"id": "proj-default", "name": "Projet Par Défaut"}],
        "max_step_iterations": 30,
        "max_context_chars": 12_000,
        "safe_shell_mode": True,
        "shell_timeout_seconds": 60,
        "shell_max_output_chars": 12_000,
        "default_skills": []
    }

@app.post("/api/settings")
async def update_settings(req: SettingsRequest):
    if not app_state.execution_engine:
        raise HTTPException(status_code=500, detail="Engine not initialized.")
        
    workspace_root = app_state.execution_engine.get_capability("filesystem").workspace_root
    config_path = os.path.join(workspace_root, "config.json")
    
    llm = app_state.execution_engine.llm_provider
    # A blank settings form must not erase an existing secret.
    api_key = req.api_key or getattr(llm, "api_key", "")
    config_data = {
        "api_key": api_key,
        "base_url": req.base_url,
        "model_name": req.model_name,
        "ssl_verify": req.ssl_verify,
        "ssl_cert_path": req.ssl_cert_path,
        "denied_capabilities": req.denied_capabilities,
        "approval_required_capabilities": req.approval_required_capabilities,
        "workspace_path": req.workspace_path,
        "restrict_to_workspace": req.restrict_to_workspace,
        "allow_subfolders": req.allow_subfolders,
        "projects": req.projects,
        "max_step_iterations": req.max_step_iterations,
        "max_context_chars": req.max_context_chars,
        "safe_shell_mode": req.safe_shell_mode,
        "shell_timeout_seconds": req.shell_timeout_seconds,
        "shell_max_output_chars": req.shell_max_output_chars,
        "default_skills": [skill.lower() for skill in req.default_skills]
    }
    
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)
        
    policy = app_state.execution_engine.policy_provider
    
    if hasattr(llm, "update_config"):
        llm.update_config(
            api_key=api_key,
            base_url=req.base_url,
            ssl_verify=req.ssl_verify,
            ssl_cert_path=req.ssl_cert_path,
            model_name=req.model_name
        )
    if hasattr(policy, "update_policy"):
        policy.update_policy(
            approval_required=req.approval_required_capabilities,
            denied=req.denied_capabilities
        )
    shell = app_state.execution_engine.get_capability("shell")
    if shell and hasattr(shell, "update_safety_config"):
        shell.update_safety_config(
            safe_mode=req.safe_shell_mode,
            timeout_seconds=req.shell_timeout_seconds,
            max_output_chars=req.shell_max_output_chars,
        )
    app_state.execution_engine.default_skills = [skill.lower() for skill in req.default_skills]
        
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
                
    return {"status": "success", "message": "Settings updated and persisted successfully."}

def init_app(kernel: RuntimeKernel, exec_engine: ExecutionEngine, state_engine: StateEngine, event_bus: EventBus):
    """Binds runtime dependencies to the FastAPI app state."""
    app_state.kernel = kernel
    app_state.execution_engine = exec_engine
    app_state.state_engine = state_engine
    app_state.event_bus = event_bus
    return app
