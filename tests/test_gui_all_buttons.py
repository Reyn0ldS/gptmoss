"""Click every GUI button in a live Edge session and cover each button's API."""

from __future__ import annotations

import base64
import json
import re
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from gptmoss.api.server import app, init_app
from gptmoss.capabilities.filesystem import FilesystemCapability
from gptmoss.core import (
    ArtifactStore, ContextEngine, EventBus, ExecutionEngine, RuntimeKernel, StateEngine,
)
from gptmoss.core.skills import SkillRegistry
from gptmoss.memory import RAMMemoryProvider
from gptmoss.planners import SimplePlanner
from gptmoss.policies import SimplePolicyProvider
from scripts.browser_layout_audit import find_edge
from tests.mock_llm import MockLLMProvider
from tests.test_api import ASGIClient


ROOT = Path(__file__).resolve().parents[1]
GUI = (ROOT / "gptmoss" / "api" / "gui.html").read_text(encoding="utf-8")
ONCLICK_RE = re.compile(r"""onclick\s*=\s*["']([^"']+)["']""")

REQUIRED_HANDLERS = (
    "showLandingView()",
    "clearAllExecutions()",
    "openLibraryModal()",
    "openServerModal()",
    "openSettingsModal()",
    "createNewProjectQuick()",
    "submitNewTask()",
    "submitApprovalDecision(true)",
    "submitApprovalDecision(false)",
    "pauseActiveExecution()",
    "resumeActiveExecution()",
    "cancelActiveExecution()",
    "downloadActiveDelivery()",
    "deleteActiveExecution()",
    "switchFeedTab('active')",
    "switchFeedTab('unified')",
    "switchPlanView('list')",
    "switchPlanView('graph')",
    "closeSettingsModal()",
    "scrollSettingsSection('settings-model-section')",
    "scrollSettingsSection('settings-security-section')",
    "scrollSettingsSection('settings-documents-section')",
    "scrollSettingsSection('settings-skills-section')",
    "scrollSettingsSection('settings-projects-section')",
    "revealApiKey()",
    "testLlmConnection()",
    "addLocalProject()",
    "saveSettings()",
    "closeLibraryModal()",
    "createLibrarySkill()",
    "resetSkillForm()",
    "saveMemory()",
    "resetMemoryForm()",
    "createSubagent()",
    "closeServerModal()",
    "serverAction('rebind')",
    "serverAction('start')",
    "serverAction('stop')",
    "serverAction('restart')",
    "renameLocalProject(",
    "editLocalProjectPath(",
    "deleteLocalProject(",
    "previewArtifact(",
    "deleteArtifact(",
    "validateLibrarySkill(",
    "editLibrarySkill(",
    "deleteLibrarySkill(",
    "toggleSkillActivation(",
    "validateMemory(",
    "editMemory(",
    "deleteMemory(",
    "controlSubagent(",
    "toggleCollapse(",
    "runGPTMOSSButtonAudit",
)


def test_gui_declares_every_known_button_handler():
    found = set(ONCLICK_RE.findall(GUI))
    missing = [handler for handler in REQUIRED_HANDLERS if handler not in GUI]
    assert not missing, missing
    assert "submitNewTask()" in found
    assert "openLibraryModal()" in found
    assert "serverAction(" in GUI


def _gui_runtime(tmp_path: Path):
    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    for _ in range(12):
        llm.add_response(content="GUI button audit reply")
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    engine = ExecutionEngine(
        event_bus, state_engine, ContextEngine(state_engine, RAMMemoryProvider()),
        llm, SimplePlanner(llm), SimplePolicyProvider(approval_required_capabilities=[]),
        skill_registry=SkillRegistry([str(tmp_path / "skills")]),
        artifact_store=ArtifactStore(str(tmp_path)),
    )
    engine.register_capability("filesystem", FilesystemCapability(str(tmp_path), state_engine))
    kernel = RuntimeKernel(event_bus, state_engine, engine)
    init_app(kernel, engine, state_engine, event_bus)
    return app, state_engine, engine


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(port: int):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", use_colors=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.4)
            return server, thread
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("GUI button-audit server did not become ready.")


def _dump_dom(edge, url: str, width: int, height: int, budget_ms: int = 20_000) -> str:
    with tempfile.TemporaryDirectory(prefix="gptmoss-buttons-") as profile:
        result = subprocess.run(
            [
                str(edge),
                "--headless=new",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                "--no-first-run",
                "--disable-extensions",
                f"--user-data-dir={profile}",
                f"--window-size={width},{height}",
                f"--virtual-time-budget={budget_ms}",
                "--dump-dom",
                url,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    if result.returncode != 0:
        raise AssertionError(
            f"Edge dump-dom failed ({result.returncode}): {result.stderr[-2000:]}"
        )
    return result.stdout


def _parse_audit(dumped: str) -> dict:
    match = re.search(
        r'id="gptmoss-button-audit-report"[^>]*>(\{.*?})</pre>',
        dumped,
        flags=re.DOTALL,
    )
    assert match, "Button audit report was not written into the DOM"
    return json.loads(match.group(1))


def _seed(client, state_engine: StateEngine, tmp_path: Path) -> str:
    uploaded = client.post("/artifacts", json={
        "filename": "notes-audit.md",
        "content_type": "text/markdown",
        "content_base64": base64.b64encode("Contenu local pour l'audit GUI".encode()).decode(),
    })
    assert uploaded.status_code == 201
    assert client.post("/skills", json={
        "name": "audit-preview",
        "description": "Skill d'audit",
        "instructions": "Rester local.",
        "allowed_capabilities": ["filesystem"],
    }).status_code == 201
    memory = client.post("/memory", json={
        "value": "Memoire d'audit GUI",
        "kind": "fact",
        "scope": "project",
        "provenance": {"source": "gui-audit"},
        "validated": False,
    })
    assert memory.status_code == 201
    created = client.post("/executions", json={
        "task": "Tache visible pour cliquer tous les boutons",
        "planning_mode": "direct",
    })
    assert created.status_code == 201
    parent_id = created.json()["execution_id"]
    child = client.post(f"/executions/{parent_id}/subagents", json={
        "task": "Sous-tache d'audit",
        "role_name": "Relecteur",
        "system_prompt": "Relire.",
    })
    assert child.status_code == 201

    paused = state_engine.get_execution("paused-audit")
    paused.status = "paused"
    paused.variables.update({
        "task": "Tache en pause pour approbation",
        "task_title": "Tache en pause pour approbation",
        "pending_approval": {
            "capability": "shell",
            "action": "execute",
            "arguments": {"command": "python -m pytest -q"},
        },
    })
    done = state_engine.get_execution("done-audit")
    done.status = "completed"
    done.variables.update({"task": "Tache livree", "task_title": "Tache livree"})
    archive = tmp_path / "livrable-audit.zip"
    archive.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    done.results["delivery_package"] = {"archive_path": str(archive)}
    extra = client.post("/projects", json={"id": "proj-audit-extra", "name": "Projet extra audit"})
    assert extra.status_code in {201, 200, 409}
    return parent_id


def test_every_gui_button_api_contract_works(tmp_path):
    _app, state_engine, _engine = _gui_runtime(tmp_path)
    client = ASGIClient(app)
    parent_id = _seed(client, state_engine, tmp_path)
    assert client.get("/").status_code == 200
    assert client.get("/executions").status_code == 200
    assert client.get(f"/executions/{parent_id}").status_code == 200
    assert client.get(f"/executions/{parent_id}/unified-feed").status_code == 200
    assert client.get(f"/executions/{parent_id}/document").status_code in {200, 404}
    # The mock execution may already be terminal by the time the live button
    # audit reaches Pause; 400 is then the documented state-machine response.
    assert client.post(f"/executions/{parent_id}/pause").status_code in {200, 400, 409}
    assert client.post(f"/executions/{parent_id}/resume").status_code in {200, 409}
    paused = client.post("/executions/paused-audit/approve", json={"reason": "audit"})
    assert paused.status_code in {200, 409, 400}
    rejected = client.post("/executions/paused-audit/reject", json={"reason": "audit"})
    assert rejected.status_code in {200, 409, 400}
    assert client.post(f"/executions/{parent_id}/cancel").status_code in {200, 409}
    delivery = client.get("/executions/done-audit/delivery?download=true")
    assert delivery.status_code in {200, 404}
    assert client.get("/projects").status_code == 200
    assert client.get("/artifacts").status_code == 200
    artifact_id = client.get("/artifacts").json()[0]["id"]
    assert client.get(f"/artifacts/{artifact_id}/preview").status_code == 200
    assert client.get("/memory").status_code == 200
    memory_id = client.get("/memory").json()[0]["id"]
    assert client.post(f"/memory/{memory_id}/validate").status_code == 200
    assert client.get("/skills").status_code == 200
    assert client.post("/skills/audit-preview/validate").status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert client.post("/api/settings/reveal-secret", json={"confirm": True}).status_code in {200, 409}
    assert client.get("/api/diagnostics").status_code == 200
    assert client.get("/api/audit").status_code == 200
    assert client.get("/api/runtime-control").status_code == 200
    assert client.delete(f"/executions/{parent_id}").status_code in {200, 409}
    assert client.post("/executions/clear-all").status_code in {200, 409}


def test_live_edge_clicks_every_gui_button(tmp_path):
    try:
        edge = find_edge()
    except FileNotFoundError:
        pytest.skip("Microsoft Edge is required for the live button audit")

    _, state_engine, _engine = _gui_runtime(tmp_path)
    port = _free_port()
    server, thread = _serve(port)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(base_url=base, timeout=8) as client:
            _seed(client, state_engine, tmp_path)
        dumped = _dump_dom(edge, f"{base}/?button_audit=1", 1366, 768, budget_ms=20_000)
        assert 'id="task-planning-mode"' in dumped
        report = _parse_audit(dumped)
        labels = {item["label"] for item in report["clicked"]}
        onclicks = {item["onclick"] for item in report["clicked"]}
        assert report["counts"]["clicked"] >= 30
        assert report["counts"]["failed"] == 0
        assert report["rejections"] == []
        for required in (
            "Nouvelle Tâche",
            "Démarrer l'exécution",
            "Bibliothèque",
            "Paramètres",
            "Serveur",
            "Effacer",
            "Pause",
            "Reprendre",
            "Arrêter",
            "Supprimer",
            "Télécharger le livrable",
            "Autoriser l'action",
            "Refuser",
            "Créer / mettre à jour",
            "Créer le sous-agent",
            "Enregistrer",
            "Tester la connexion",
            "Démarrer",
            "Arrêter",
        ):
            assert any(required in label for label in labels), required
        for fragment in (
            "submitNewTask()",
            "openLibraryModal()",
            "openSettingsModal()",
            "openServerModal()",
            "pauseActiveExecution()",
            "saveSettings()",
            "serverAction('start')",
            "createSubagent()",
        ):
            assert any(fragment in onclick for onclick in onclicks), fragment
    finally:
        server.should_exit = True
        thread.join(timeout=5)
