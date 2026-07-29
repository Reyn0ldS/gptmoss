import asyncio
import base64
import json

import httpx
import pytest
from gptmoss.api.server import app, init_app
from gptmoss.core import EventBus, StateEngine, ContextEngine, ExecutionEngine, RuntimeKernel
from gptmoss.planners import SimplePlanner
from gptmoss.policies import SimplePolicyProvider
from gptmoss.memory import RAMMemoryProvider
from tests.mock_llm import MockLLMProvider


class ASGIClient:
    """Small synchronous adapter around HTTPX's maintained ASGI transport."""

    def __init__(self, app):
        self.app = app

    def request(self, method, url, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(send())

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

def test_api_submit_and_query_flow():
    # Setup test dependencies
    event_bus = EventBus()
    state_engine = StateEngine()
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    
    mock_llm = MockLLMProvider()
    # Simple planner response
    mock_llm.add_response(
        content='{"steps": [], "rationale": "Empty plan for api test"}'
    )

    planner = SimplePlanner(mock_llm)
    policy = SimplePolicyProvider()

    exec_engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=planner,
        policy_provider=policy
    )
    kernel = RuntimeKernel(
        event_bus=event_bus,
        state_engine=state_engine,
        execution_engine=exec_engine
    )

    init_app(kernel, exec_engine, state_engine, event_bus)
    client = ASGIClient(app)

    # Submit task
    response = client.post("/executions", json={"task": "Api test task"})
    assert response.status_code == 201
    body = response.json()
    assert "execution_id" in body
    assert body["status"] == "running"

    exec_id = body["execution_id"]

    # Query details
    response_get = client.get(f"/executions/{exec_id}")
    assert response_get.status_code == 200
    body_get = response_get.json()
    assert body_get["execution_id"] == exec_id

    response_metrics = client.get(f"/executions/{exec_id}/metrics")
    assert response_metrics.status_code == 200
    assert "counts" in response_metrics.json()

    # List all executions
    response_list = client.get("/executions")
    assert response_list.status_code == 200
    body_list = response_list.json()
    assert len(body_list) >= 1
    assert any(x["execution_id"] == exec_id for x in body_list)

def test_api_settings_flow(tmp_path):
    # Setup test dependencies
    event_bus = EventBus()
    state_engine = StateEngine()
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    
    mock_llm = MockLLMProvider()
    planner = SimplePlanner(mock_llm)
    policy = SimplePolicyProvider()

    exec_engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=planner,
        policy_provider=policy
    )
    from gptmoss.capabilities.filesystem import FilesystemCapability
    exec_engine.register_capability("filesystem", FilesystemCapability(str(tmp_path)))

    kernel = RuntimeKernel(
        event_bus=event_bus,
        state_engine=state_engine,
        execution_engine=exec_engine
    )

    init_app(kernel, exec_engine, state_engine, event_bus)
    client = ASGIClient(app)

    # Get settings
    response_get = client.get("/api/settings")
    assert response_get.status_code == 200
    body_get = response_get.json()
    assert "api_key" not in body_get
    assert "base_url" in body_get

    # Post new settings
    new_settings = {
        "api_key": "new-test-key",
        "base_url": "https://test.api.url",
        "model_name": "test-qwen-model",
        "ssl_verify": True,
        "ssl_cert_path": "test_path.pem",
        "denied_capabilities": ["shell"],
        "approval_required_capabilities": ["filesystem"],
        "workspace_path": str(tmp_path),
        "restrict_to_workspace": True,
        "allow_subfolders": True,
        "projects": [{"id": "proj-default", "name": "Projet Par Défaut"}],
        "confirm_sensitive": True
    }
    response_post = client.post("/api/settings", json=new_settings)
    assert response_post.status_code == 200
    assert response_post.json()["status"] == "success"

    # Verify settings updated in memory
    assert mock_llm.api_key == "new-test-key"
    assert mock_llm.base_url == "https://test.api.url"
    assert mock_llm.default_model == "test-qwen-model"
    assert "shell" in policy.denied
    assert "filesystem" in policy.approval_required

    response_get_after_save = client.get("/api/settings")
    assert "api_key" not in response_get_after_save.json()


def test_api_delete_cascade_flow():
    # Setup test dependencies
    event_bus = EventBus()
    state_engine = StateEngine()
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    
    mock_llm = MockLLMProvider()
    planner = SimplePlanner(mock_llm)
    policy = SimplePolicyProvider()

    exec_engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=planner,
        policy_provider=policy
    )
    kernel = RuntimeKernel(
        event_bus=event_bus,
        state_engine=state_engine,
        execution_engine=exec_engine
    )

    init_app(kernel, exec_engine, state_engine, event_bus)
    client = ASGIClient(app)

    # Manually populate a parent-child execution relationship in the state engine
    parent_id = "parent-uuid-1234"
    child_id = "child-uuid-5678"
    
    state_engine.get_execution(parent_id)
    state_engine.get_conversation(parent_id)
    
    child_state = state_engine.get_execution(child_id)
    child_state.variables["parent_execution_id"] = parent_id
    state_engine.get_conversation(child_id)

    # Verify they exist
    assert parent_id in state_engine.executions
    assert child_id in state_engine.executions

    # Call DELETE on the parent
    response = client.delete(f"/executions/{parent_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "deleted"

    # Verify both parent and child are popped and deleted cascaded
    assert parent_id not in state_engine.executions
    assert child_id not in state_engine.executions

def test_api_unified_feed_flow():
    # Setup test dependencies
    event_bus = EventBus()
    state_engine = StateEngine()
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    
    mock_llm = MockLLMProvider()
    planner = SimplePlanner(mock_llm)
    policy = SimplePolicyProvider()

    exec_engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=planner,
        policy_provider=policy
    )
    kernel = RuntimeKernel(
        event_bus=event_bus,
        state_engine=state_engine,
        execution_engine=exec_engine
    )

    init_app(kernel, exec_engine, state_engine, event_bus)
    client = ASGIClient(app)

    # Manually populate parent and child executions with messages containing timestamps
    parent_id = "parent-uuid"
    child_id = "child-uuid"
    
    parent_convo = state_engine.get_conversation(parent_id)
    parent_convo.messages.append({"role": "user", "content": "Parent user msg", "timestamp": 10.0})
    parent_convo.messages.append({"role": "assistant", "content": "Parent assistant msg", "timestamp": 30.0})
    
    state_engine.get_execution(parent_id)
    
    child_convo = state_engine.get_conversation(child_id)
    child_convo.messages.append({"role": "assistant", "content": "Child assistant msg", "timestamp": 20.0})
    
    child_state = state_engine.get_execution(child_id)
    child_state.variables["parent_execution_id"] = parent_id
    child_state.variables["role_name"] = "Architecte"

    # Call unified-feed on parent
    response = client.get(f"/executions/{parent_id}/unified-feed")
    assert response.status_code == 200
    feed = response.json()
    
    # Assert there are 3 messages in the feed
    assert len(feed) == 3
    
    # Verify the chronological sorting: timestamp 10.0 (Parent), then 20.0 (Child), then 30.0 (Parent)
    assert feed[0]["content"] == "Parent user msg"
    assert feed[0]["sender_role"] == "Coordinateur"
    
    assert feed[1]["content"] == "Child assistant msg"
    assert feed[1]["sender_role"] == "Architecte"
    assert feed[1]["execution_id"] == child_id
    
    assert feed[2]["content"] == "Parent assistant msg"
    assert feed[2]["sender_role"] == "Coordinateur"

def test_api_settings_preserve_secret_and_context_budget(tmp_path):
    event_bus = EventBus()
    state_engine = StateEngine()
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    mock_llm = MockLLMProvider()
    policy = SimplePolicyProvider()
    exec_engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=SimplePlanner(mock_llm),
        policy_provider=policy,
    )
    from gptmoss.capabilities.filesystem import FilesystemCapability
    exec_engine.register_capability("filesystem", FilesystemCapability(str(tmp_path)))
    kernel = RuntimeKernel(event_bus=event_bus, state_engine=state_engine, execution_engine=exec_engine)
    init_app(kernel, exec_engine, state_engine, event_bus)
    client = ASGIClient(app)

    settings = {
        "api_key": "secret-key",
        "base_url": "https://example.test/v1",
        "model_name": "test-model",
        "ssl_verify": True,
        "ssl_cert_path": "",
        "denied_capabilities": [],
        "approval_required_capabilities": ["shell"],
        "workspace_path": str(tmp_path),
        "restrict_to_workspace": True,
        "allow_subfolders": True,
        "projects": [{"id": "proj-default", "name": "Default"}],
        "max_step_iterations": 12,
        "max_context_chars": 24000,
        "safe_shell_mode": False,
        "shell_timeout_seconds": 45,
        "shell_max_output_chars": 20000,
        "default_skills": ["code-review"],
        "confirm_sensitive": True,
    }
    assert client.post("/api/settings", json=settings).status_code == 200

    public_settings = client.get("/api/settings").json()
    assert "api_key" not in public_settings
    assert public_settings["max_context_chars"] == 24000
    assert public_settings["safe_shell_mode"] is False
    assert public_settings["shell_timeout_seconds"] == 45
    assert public_settings["default_skills"] == ["code-review"]

    # This mirrors the quick-project UI flow: the GET response has no secret.
    public_settings["projects"].append({"id": "proj-ui", "name": "Created from UI"})
    public_settings["confirm_sensitive"] = True
    response = client.post("/api/settings", json=public_settings)
    assert response.status_code == 200
    assert mock_llm.api_key == "secret-key"
    assert exec_engine.default_skills == ["code-review"]
    persisted = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert '"max_context_chars": 24000' in persisted

    invalid = dict(public_settings)
    invalid["max_context_chars"] = 0
    assert client.post("/api/settings", json=invalid).status_code == 422

    created_project = client.post("/projects", json={"id": "proj-atomic", "name": "Atomic Project"})
    assert created_project.status_code == 201
    assert (tmp_path / "projects" / "proj-atomic").is_dir()
    assert client.post("/projects", json={"id": "proj-atomic", "name": "Duplicate"}).status_code == 409
    private_config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert private_config["api_key"] == "secret-key"
    assert any(project["id"] == "proj-atomic" for project in private_config["projects"])

    # The bootstrap config remains authoritative when agent files move elsewhere.
    moved_workspace = tmp_path / "agent-files"
    moved_settings = client.get("/api/settings").json()
    moved_settings["workspace_path"] = str(moved_workspace)
    moved_settings["confirm_sensitive"] = True
    assert client.post("/api/settings", json=moved_settings).status_code == 200
    assert client.get("/api/settings").json()["workspace_path"] == str(moved_workspace)
    assert exec_engine.max_step_iterations == 12
    moved_project = client.post("/projects", json={"id": "proj-moved", "name": "Moved Project"})
    assert moved_project.status_code == 201
    assert (moved_workspace / "projects" / "proj-moved").is_dir()
    assert not (moved_workspace / "config.json").exists()
    private_config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert any(project["id"] == "proj-moved" for project in private_config["projects"])

    execution_count = len(state_engine.executions)
    missing_project = client.post("/executions", json={"task": "Must not start", "project_id": "proj-missing"})
    assert missing_project.status_code == 404
    assert len(state_engine.executions) == execution_count


def test_gui_uses_sanitized_markdown_renderer():
    from pathlib import Path

    gui = (Path(__file__).parents[1] / "gptmoss" / "api" / "gui.html").read_text(encoding="utf-8")
    assert "function renderSafeMarkdown" in gui
    assert 'contentHtml = renderSafeMarkdown(msg.content || "");' in gui
    assert 'contentHtml = marked.parse(msg.content || "");' not in gui
    assert "--bg-card:" in gui
    assert "--text-normal:" in gui


def test_gui_management_api_complete_flow(tmp_path):
    """The GUI management endpoints work together and never expose secrets in audit data."""
    from gptmoss.capabilities.filesystem import FilesystemCapability
    from gptmoss.core.skills import SkillRegistry

    event_bus = EventBus()
    state_engine = StateEngine()
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    mock_llm = MockLLMProvider()
    policy = SimplePolicyProvider()
    exec_engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=SimplePlanner(mock_llm),
        policy_provider=policy,
        skill_registry=SkillRegistry(),
    )
    exec_engine.register_capability("filesystem", FilesystemCapability(str(tmp_path)))
    kernel = RuntimeKernel(event_bus=event_bus, state_engine=state_engine, execution_engine=exec_engine)
    init_app(kernel, exec_engine, state_engine, event_bus)
    client = ASGIClient(app)

    uploaded = client.post("/artifacts", json={
        "filename": "notes.md", "content_type": "text/markdown",
        "content_base64": base64.b64encode("Bonjour GPTMOSS".encode()).decode(),
    })
    assert uploaded.status_code == 201
    artifact_id = uploaded.json()["id"]
    preview = client.get(f"/artifacts/{artifact_id}/preview")
    assert preview.status_code == 200
    assert preview.json()["text"] == "Bonjour GPTMOSS"

    skill = {"name": "gui-review", "description": "Review", "instructions": "Review carefully.", "allowed_capabilities": ["filesystem"]}
    assert client.post("/skills", json=skill).status_code == 201
    listed_skill = next(item for item in client.get("/skills").json() if item["name"] == "gui-review")
    assert listed_skill["editable"] is True
    assert listed_skill["instructions"] == "Review carefully."
    assert client.post("/skills/gui-review/validate").json()["valid"] is True
    assert client.get("/agent-profiles").json() == []
    assert client.get("/evolution").json()["creation_enabled"] is False
    imported = "---\nname: imported-skill\ndescription: Imported\nallowed_capabilities: [shell]\n---\n\nUse safe commands.\n"
    assert client.post("/skills/import", json={"content": imported}).status_code == 201

    created_memory = client.post("/memory", json={
        "value": "Préférence GUI", "metadata": {"kind": "preference"},
        "provenance": {"source": "test-gui"}, "validated": False,
    })
    assert created_memory.status_code == 201
    memory_id = created_memory.json()["id"]
    assert len(client.get("/memory?q=gui").json()) == 1
    updated = client.request("PUT", f"/memory/{memory_id}", json={
        "value": "Préférence GUI mise à jour", "metadata": {},
        "provenance": {"source": "test-gui"}, "validated": False,
    })
    assert updated.status_code == 200
    assert client.post(f"/memory/{memory_id}/validate").status_code == 200

    parent_id = "parent-for-gui"
    state_engine.get_execution(parent_id)
    state_engine.get_conversation(parent_id)
    child = client.post(f"/executions/{parent_id}/subagents", json={"task": "Inspect", "role_name": "Reviewer", "system_prompt": "Review."})
    assert child.status_code == 201
    assert len(client.get(f"/executions/{parent_id}/subagents").json()) == 1
    diagnostics = client.get("/api/diagnostics").json()
    assert diagnostics["supports_vision"] is False
    assert any(cap["name"] == "filesystem" for cap in diagnostics["capabilities"])

    settings = {
        "api_key": "audit-secret", "base_url": "https://example.test/v1", "model_name": "model",
        "ssl_verify": True, "ssl_cert_path": "", "denied_capabilities": [],
        "approval_required_capabilities": ["shell"], "workspace_path": str(tmp_path),
        "restrict_to_workspace": True, "allow_subfolders": True,
        "projects": [{"id": "proj-default", "name": "Défaut"}], "confirm_sensitive": False,
    }
    assert client.post("/api/settings", json=settings).status_code == 200
    assert client.get("/api/settings").json()["autonomous_skill_creation"] is True
    assert client.get("/evolution").json()["creation_enabled"] is True
    audit = client.get("/api/audit").json()
    assert audit and audit[-1]["secret_changed"] is True
    assert "audit-secret" not in json.dumps(audit)
    assert client.post("/api/settings/reveal-secret", json={"confirm": False}).status_code == 409
    assert client.post("/api/settings/reveal-secret", json={"confirm": True}).json()["api_key"] == "audit-secret"
    assert client.post("/api/settings/test-connection", json={**settings, "base_url": "ftp://invalid"}).status_code == 400

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class ModelsHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps({"data": [{"id": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
    provider_thread = Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    try:
        connection = client.post("/api/settings/test-connection", json={
            **settings, "base_url": f"http://127.0.0.1:{provider.server_port}/v1"
        })
        assert connection.status_code == 200
        assert connection.json() == {"status": "connected", "model_available": True, "models_count": 1}
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=2)

    assert client.delete(f"/memory/{memory_id}").status_code == 200
    assert client.delete("/skills/gui-review").status_code == 200
    assert client.delete("/skills/imported-skill").status_code == 200
    assert client.delete(f"/artifacts/{artifact_id}").status_code == 200


def test_gui_contains_complete_management_controls():
    from pathlib import Path

    gui = (Path(__file__).parents[1] / "gptmoss" / "api" / "gui.html").read_text(encoding="utf-8")
    for marker in (
        "previewArtifact", "importLibrarySkill", "validateLibrarySkill", "saveMemory",
        "createSubagent", "library-diagnostics", "library-audit", "revealApiKey",
        "testLlmConnection", "collectSettingsPayload",
    ):
        assert marker in gui


def test_gui_layout_stays_inside_narrow_viewports_and_keeps_scroll_fallbacks():
    """Key layout containers may shrink, wrap, and scroll instead of being clipped."""
    from pathlib import Path

    gui = (Path(__file__).parents[1] / "gptmoss" / "api" / "gui.html").read_text(encoding="utf-8")

    for marker in (
        "height: 100dvh;",
        "overflow-x: auto;",
        "width: clamp(220px, 24vw, 320px);",
        "grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);",
        ".modal-overlay { max-width:100%; max-height:100%; padding:12px; overflow:auto; }",
        "max-height:calc(100dvh - 24px);",
        "scrollbar-gutter: stable;",
        "@media (max-width: 480px)",
        ".sidebar-footer .btn-control",
        "auditGPTMOSSLayout",
        "layoutGlobalOverflow",
        "layoutOffenderCount",
    ):
        assert marker in gui

    assert "grid-template-columns: minmax(420px, 3fr) minmax(300px, 2fr);" not in gui
