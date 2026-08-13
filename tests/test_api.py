import asyncio
import base64
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import AsyncMock
from zipfile import ZIP_DEFLATED, ZipFile

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


def _api_docx_payload() -> str:
    payload = BytesIO()
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Dossier API</w:t></w:r></w:p>
        <w:p><w:r><w:t>Contenu DOCX réellement extrait.</w:t></w:r></w:p>
      </w:body>
    </w:document>"""
    with ZipFile(payload, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return base64.b64encode(payload.getvalue()).decode("ascii")


def test_resume_failed_root_requeues_failed_step_without_resuming_child():
    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state_engine,
        ContextEngine(state_engine, RAMMemoryProvider()), llm,
        SimplePlanner(llm), SimplePolicyProvider(),
    )
    engine.execute_task = AsyncMock()
    kernel = RuntimeKernel(event_bus, state_engine, engine)
    init_app(kernel, engine, state_engine, event_bus)
    client = ASGIClient(app)

    root = state_engine.get_execution("failed-root")
    root.status = "failed"
    root.variables["task"] = "Repair the project"
    root.results["error"] = "repair child was cancelled"
    root.variables["step_runtime"] = {
        "1": {
            "iterations": 52,
            "stagnant_iterations": 52,
            "stagnation_nudge_level": 2,
        },
        "2": {"iterations": 3, "stagnant_iterations": 1},
    }
    root.current_plan = {"steps": [
        {"id": 0, "status": "completed"},
        {
            "id": 1, "status": "failed", "error": "cancelled",
            "assigned_execution_id": "old-child",
        },
        {"id": 2, "status": "pending"},
    ]}
    child = state_engine.get_execution("old-child")
    child.status = "cancelled"
    child.variables["parent_execution_id"] = "failed-root"

    response = client.post("/executions/failed-root/resume")

    assert response.status_code == 200
    assert root.status == "running"
    assert root.current_plan["steps"][0]["status"] == "completed"
    assert root.current_plan["steps"][1]["status"] == "pending"
    assert "assigned_execution_id" not in root.current_plan["steps"][1]
    assert root.current_plan["steps"][1]["manual_retry_count"] == 1
    assert "1" not in root.variables["step_runtime"]
    assert root.variables["step_runtime"]["2"]["iterations"] == 3
    assert child.status == "cancelled"
    assert "error" not in root.results
    engine.execute_task.assert_called_once_with("failed-root", "Repair the project")


def test_resume_failed_delegated_execution_is_rejected():
    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state_engine,
        ContextEngine(state_engine, RAMMemoryProvider()), llm,
        SimplePlanner(llm), SimplePolicyProvider(),
    )
    kernel = RuntimeKernel(event_bus, state_engine, engine)
    init_app(kernel, engine, state_engine, event_bus)
    client = ASGIClient(app)
    child = state_engine.get_execution("failed-child")
    child.status = "failed"
    child.variables["parent_execution_id"] = "root"

    response = client.post("/executions/failed-child/resume")

    assert response.status_code == 400
    assert child.status == "failed"

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
    listed = next(item for item in body_list if item["execution_id"] == exec_id)
    assert listed["task_title"] == "Api test task"
    assert listed["planning_mode"] == "auto"
    assert listed["task"] == "Api test task"
    assert body_get["variables"]["planning_mode"] == "auto"
    assert body_get["variables"]["task_title"] == "Api test task"


def test_api_can_schedule_execution_without_running_it_early():
    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state_engine, ContextEngine(state_engine, RAMMemoryProvider()),
        llm, SimplePlanner(llm), SimplePolicyProvider(),
    )
    kernel = RuntimeKernel(event_bus, state_engine, engine)
    init_app(kernel, engine, state_engine, event_bus)
    client = ASGIClient(app)

    response = client.post("/executions", json={
        "task": "Scheduled task", "delay_seconds": 3600,
    })

    assert response.status_code == 201
    assert response.json()["status"] == "scheduled"
    execution_id = response.json()["execution_id"]
    assert state_engine.get_execution(execution_id).status == "pending"
    assert engine.scheduler.has(f"execution:{execution_id}")


def test_api_accepts_explicit_planning_mode():
    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state_engine, ContextEngine(state_engine, RAMMemoryProvider()),
        llm, SimplePlanner(llm), SimplePolicyProvider(),
    )
    kernel = RuntimeKernel(event_bus, state_engine, engine)
    init_app(kernel, engine, state_engine, event_bus)
    client = ASGIClient(app)

    response = client.post("/executions", json={
        "task": "Translate this sentence into French.",
        "planning_mode": "direct",
    })
    assert response.status_code == 201
    execution_id = response.json()["execution_id"]
    stored = state_engine.get_execution(execution_id)
    assert stored.variables["planning_mode"] == "direct"
    listed = client.get("/executions").json()
    assert any(
        item["execution_id"] == execution_id and item["planning_mode"] == "direct"
        for item in listed
    )

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
    assert body_get["ssl_verify"] is True

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
    cancelled = client.post(f"/executions/{parent_id}/cancel")
    assert cancelled.status_code == 200
    assert set(cancelled.json()["execution_ids"]) == {parent_id, child_id}
    assert state_engine.get_execution(parent_id).status == "cancelled"
    assert state_engine.get_execution(child_id).status == "cancelled"

    response = client.delete(f"/executions/{parent_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "deleted"

    # Verify both parent and child are popped and deleted cascaded
    assert parent_id not in state_engine.executions
    assert child_id not in state_engine.executions


def _bare_api_client():
    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state_engine,
        ContextEngine(state_engine, RAMMemoryProvider()), llm,
        SimplePlanner(llm), SimplePolicyProvider(),
    )
    kernel = RuntimeKernel(event_bus, state_engine, engine)
    init_app(kernel, engine, state_engine, event_bus)
    return ASGIClient(app), state_engine, engine


def test_api_delete_and_clear_all_refuse_active_executions():
    client, state_engine, _engine = _bare_api_client()
    running = state_engine.get_execution("active-root")
    running.status = "running"
    child = state_engine.get_execution("active-child")
    child.status = "paused"
    child.variables["parent_execution_id"] = "active-root"

    denied_delete = client.delete("/executions/active-root")
    assert denied_delete.status_code == 409
    assert "active-root" in state_engine.executions
    assert "active-child" in state_engine.executions

    denied_clear = client.post("/executions/clear-all")
    assert denied_clear.status_code == 409
    assert "active-root" in state_engine.executions

    cancelled = client.post("/executions/active-root/cancel")
    assert cancelled.status_code == 200
    assert client.delete("/executions/active-root").status_code == 200
    assert "active-root" not in state_engine.executions
    leftover = state_engine.get_execution("done-item")
    leftover.status = "completed"
    assert client.post("/executions/clear-all").status_code == 200
    assert not state_engine.executions


def test_api_get_execution_omits_internal_tool_history():
    client, state_engine, _engine = _bare_api_client()
    state = state_engine.get_execution("public-vars")
    state.status = "completed"
    state.variables.update({
        "task": "Inspect",
        "project_id": "proj-default",
        "role_name": "Coordinateur",
        "tool_call_history": [{"capability": "shell", "action": "execute", "result": "secret-output"}],
        "pending_approval": {
            "capability": "shell",
            "action": "execute",
            "arguments": {"command": "python -m pytest -q", "content": "x" * 9000},
        },
    })

    body = client.get("/executions/public-vars").json()
    assert body["variables"]["task"] == "Inspect"
    assert body["variables"]["pending_approval"]["capability"] == "shell"
    assert "tool_call_history" not in body["variables"]
    assert len(body["variables"]["pending_approval"]["arguments"]["content"]) < 9000


def test_api_loopback_cors_allows_any_local_port():
    client, _state, _engine = _bare_api_client()
    allowed = client.get("/health", headers={"Origin": "http://127.0.0.1:8123"})
    denied = client.get("/health", headers={"Origin": "http://evil.example"})
    assert allowed.status_code == 200
    assert allowed.headers.get("access-control-allow-origin") == "http://127.0.0.1:8123"
    assert "access-control-allow-origin" not in {key.lower() for key in denied.headers.keys()} or denied.headers.get("access-control-allow-origin") != "http://evil.example"


def test_api_subagent_inherits_parent_project_context():
    client, state_engine, engine = _bare_api_client()
    parent = state_engine.get_execution("inherit-parent")
    parent.status = "running"
    parent.variables.update({
        "project_id": "site-demo",
        "attachment_ids": ["doc-1"],
        "requested_skills": ["code-review"],
        "task": "Parent work",
        "planning_mode": "short_team",
    })
    engine.execute_task = AsyncMock()
    response = client.post("/executions/inherit-parent/subagents", json={
        "task": "Inspect", "role_name": "Reviewer", "system_prompt": "Review.",
    })
    assert response.status_code == 201
    child_id = response.json()["execution_id"]
    child = state_engine.get_execution(child_id)
    assert child.variables["project_id"] == "site-demo"
    assert child.variables["attachment_ids"] == ["doc-1"]
    assert child.variables["requested_skills"] == ["code-review"]
    assert child.variables["planning_mode"] == "short_team"


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

    assert client.get("/health").json() == {
        "status": "ok", "runtime_initialized": True,
    }
    assert client.get("/readiness").json() == {"status": "ready"}

    unmanaged = client.get("/api/runtime-control")
    assert unmanaged.status_code == 200
    assert unmanaged.json() == {"available": False, "supervisor_url": "", "token": ""}

    settings = {
        "api_key": "secret-key",
        "base_url": "https://example.test/v1",
        "model_name": "test-model",
        "vision_mode": "disabled",
        "ssl_verify": True,
        "ssl_cert_path": "",
        "denied_capabilities": ["documents.read_chunk"],
        "approval_required_capabilities": ["shell"],
        "workspace_full_autonomy": False,
        "continue_while_progress": False,
        "adaptive_resource_management": False,
        "strict_skill_capabilities": True,
        "allow_nested_delegation": False,
        "max_delegation_depth": 4,
        "autonomous_specialization": False,
        "autonomous_skill_creation": True,
        "autonomous_skill_improvement": False,
        "skill_coverage_threshold": 7,
        "max_autonomous_skills_per_execution": 3,
        "workspace_path": str(tmp_path),
        "restrict_to_workspace": True,
        "allow_subfolders": True,
        "projects": [{"id": "proj-default", "name": "Default"}],
        "max_step_iterations": 12,
        "max_step_retries": 5,
        "document_engine_enabled": True,
        "document_checkpoint_enabled": False,
        "document_target_section_words": 900,
        "diagram_rendering": False,
        "docx_embed_diagrams": False,
        "max_context_chars": 24000,
        "context_window_tokens": 262144,
        "context_output_reserve_tokens": 16384,
        "max_upload_bytes": 100000,
        "max_attachment_text_chars": 5000,
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
    assert public_settings["context_window_tokens"] == 262144
    assert public_settings["context_output_reserve_tokens"] == 16384
    assert public_settings["safe_shell_mode"] is False
    assert public_settings["shell_timeout_seconds"] == 45
    assert public_settings["default_skills"] == ["code-review"]
    assert public_settings["vision_mode"] == "disabled"
    assert public_settings["denied_capabilities"] == ["documents.read_chunk"]
    assert public_settings["max_step_retries"] == 5
    assert public_settings["max_upload_bytes"] == 100000
    assert public_settings["max_attachment_text_chars"] == 5000
    assert public_settings["document_checkpoint_enabled"] is False
    assert public_settings["document_target_section_words"] == 900
    assert public_settings["diagram_rendering"] is False
    assert public_settings["docx_embed_diagrams"] is False
    assert policy.denied == ["documents.read_chunk"]
    assert exec_engine.continue_while_progress is False
    assert exec_engine.adaptive_resource_management is False
    assert exec_engine.strict_skill_capabilities is True
    assert exec_engine.allow_nested_delegation is False
    assert exec_engine.max_delegation_depth == 4
    assert exec_engine.max_step_retries == 5
    assert exec_engine.document_engine_enabled is True
    assert exec_engine.document_checkpoint_enabled is False
    assert exec_engine.document_target_section_words == 900
    assert exec_engine.diagram_rendering is False
    assert exec_engine.docx_embed_diagrams is False
    assert exec_engine.context_engine.adaptive is False
    assert exec_engine.context_engine.max_history_chars == 24000
    assert exec_engine.artifact_store.max_bytes == 100000
    assert exec_engine.artifact_store.max_text_chars == 5000
    assert state_engine.max_transitions_per_execution == 2000
    assert exec_engine.autonomous_specialization is False
    assert exec_engine.skill_lifecycle.creation_enabled is True
    assert exec_engine.skill_lifecycle.improvement_enabled is False
    assert exec_engine.skill_lifecycle.coverage_threshold == 7
    assert exec_engine.skill_lifecycle.max_skills_per_execution == 3

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

    created_project = client.post("/projects", json={
        "id": "proj-atomic", "name": "Atomic Project",
        "domains": {"legal-operations": ["dossier contentieux"]},
    })
    assert created_project.status_code == 201
    assert (tmp_path / "projects" / "proj-atomic").is_dir()
    assert client.post("/projects", json={"id": "proj-atomic", "name": "Duplicate"}).status_code == 409
    private_config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert private_config["api_key"] == "secret-key"
    atomic_project = next(
        project for project in private_config["projects"]
        if project["id"] == "proj-atomic"
    )
    assert atomic_project["domains"] == {"legal-operations": ["dossier contentieux"]}
    invalid_domains = client.post("/projects", json={
        "id": "proj-invalid-domain", "name": "Invalid",
        "domains": {"x": []},
    })
    assert invalid_domains.status_code == 422

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
    assert "thinkingLabel.textContent = thinkingText;" in gui
    assert "${thinkingText}</span>" not in gui
    assert "escapeHTML(String(msg.name ||" in gui
    assert 'escapeHTML(String(proj.name || ""))' in gui
    assert '["assistant", "user", "tool", "system"].includes(msg.role)' in gui
    assert "--bg-card:" in gui
    assert "--text-normal:" in gui


def test_runtime_control_only_exposes_a_managed_loopback_supervisor(monkeypatch):
    client = ASGIClient(app)
    monkeypatch.setenv("GPTMOSS_SUPERVISOR_MANAGED", "1")
    monkeypatch.setenv("GPTMOSS_SUPERVISOR_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("GPTMOSS_SUPERVISOR_TOKEN", "local-token")

    response = client.get("/api/runtime-control")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "available": True,
        "supervisor_url": "http://127.0.0.1:8765",
        "token": "local-token",
    }

    monkeypatch.setenv("GPTMOSS_SUPERVISOR_URL", "http://192.0.2.10:8765")
    assert client.get("/api/runtime-control").json() == {
        "available": False,
        "supervisor_url": "",
        "token": "",
    }


def test_folder_corpus_api_import_finalize_and_execution_scope(tmp_path):
    from gptmoss.capabilities.filesystem import FilesystemCapability

    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state_engine,
        ContextEngine(state_engine, RAMMemoryProvider()), llm,
        SimplePlanner(llm), SimplePolicyProvider(),
    )
    engine.register_capability("filesystem", FilesystemCapability(str(tmp_path)))
    engine.execute_task = AsyncMock()
    kernel = RuntimeKernel(event_bus, state_engine, engine)
    init_app(kernel, engine, state_engine, event_bus)
    client = ASGIClient(app)

    created = client.post("/corpora", json={
        "name": "sources", "root_label": "sources", "resume": True,
    })
    assert created.status_code == 201
    corpus_id = created.json()["id"]
    payload = b"# REQ-001\nThe professional report must be traceable."
    imported = client.request(
        "PUT",
        f"/corpora/{corpus_id}/files?relative_path=sources%2Frequirements%2Fscope.md&last_modified=123",
        content=payload,
        headers={"Content-Type": "text/markdown"},
    )
    assert imported.status_code == 201
    artifact_id = imported.json()["id"]
    assert imported.json()["source_name"] == "sources/requirements/scope.md"

    finalized = client.post(f"/corpora/{corpus_id}/finalize", json={
        "present_paths": ["sources/requirements/scope.md"],
        "skipped": [{"relative_path": "sources/program.exe", "reason": "unsupported"}],
        "errors": [],
    })
    assert finalized.status_code == 200
    assert finalized.json()["state"] == "ready"
    assert finalized.json()["document_count"] == 1
    assert finalized.json()["attachment_ids"] == [artifact_id]
    assert client.get(f"/corpora/{corpus_id}").json()["file_count"] == 1
    assert any(item["id"] == corpus_id for item in client.get("/corpora").json())

    submitted = client.post("/executions", json={
        "task": "Produce the requested professional report.",
        "corpus_ids": [corpus_id],
        "planning_mode": "auto",
    })
    assert submitted.status_code == 201
    state = state_engine.get_execution(submitted.json()["execution_id"])
    assert state.variables["corpus_ids"] == [corpus_id]
    assert state.variables["attachment_ids"] == [artifact_id]
    assert state.variables["corpus_summaries"][0]["file_count"] == 1
    assert "Mandatory local corpus workflow" in state.variables["task"]
    public = client.get(f"/executions/{submitted.json()['execution_id']}").json()
    assert public["variables"]["corpus_summaries"][0]["root_label"] == "sources"
    deleted = client.delete(f"/corpora/{corpus_id}")
    assert deleted.status_code == 200
    assert deleted.json()["retained_artifacts"] == 1
    assert client.get(f"/corpora/{corpus_id}").status_code == 404
    assert engine.artifact_store.get(artifact_id)["id"] == artifact_id


def test_folder_corpus_api_rejects_traversal_and_unfinalized_submission(tmp_path):
    from gptmoss.capabilities.filesystem import FilesystemCapability

    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state_engine,
        ContextEngine(state_engine, RAMMemoryProvider()), llm,
        SimplePlanner(llm), SimplePolicyProvider(),
    )
    engine.register_capability("filesystem", FilesystemCapability(str(tmp_path)))
    kernel = RuntimeKernel(event_bus, state_engine, engine)
    init_app(kernel, engine, state_engine, event_bus)
    client = ASGIClient(app)
    corpus_id = client.post("/corpora", json={
        "name": "unsafe", "root_label": "sources", "resume": False,
    }).json()["id"]

    traversal = client.request(
        "PUT", f"/corpora/{corpus_id}/files?relative_path=..%2Fsecret.md",
        content=b"secret", headers={"Content-Type": "text/markdown"},
    )
    assert traversal.status_code == 400
    blocked = client.post("/executions", json={"task": "Analyze", "corpus_ids": [corpus_id]})
    assert blocked.status_code == 409


def test_folder_corpus_api_finalizes_more_than_one_thousand_skipped_files(tmp_path):
    from gptmoss.capabilities.filesystem import FilesystemCapability

    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state_engine,
        ContextEngine(state_engine, RAMMemoryProvider()), llm,
        SimplePlanner(llm), SimplePolicyProvider(),
    )
    engine.register_capability("filesystem", FilesystemCapability(str(tmp_path)))
    init_app(RuntimeKernel(event_bus, state_engine, engine), engine, state_engine, event_bus)
    client = ASGIClient(app)
    corpus_id = client.post("/corpora", json={
        "name": "large", "root_label": "sources", "resume": False,
    }).json()["id"]

    response = client.post(f"/corpora/{corpus_id}/finalize", json={
        "present_paths": [],
        "skipped": [
            {"relative_path": f"sources/cache/{index}.bin", "reason": "unsupported"}
            for index in range(1_005)
        ],
        "errors": [],
    })

    assert response.status_code == 200
    assert response.json()["skipped_count"] == 1_005
    assert len(response.json()["skipped"]) == 1_000


def test_professional_delivery_download_route_is_scoped_to_execution(tmp_path):
    from gptmoss.capabilities.filesystem import FilesystemCapability

    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state_engine, ContextEngine(state_engine, RAMMemoryProvider()),
        llm, SimplePlanner(llm), SimplePolicyProvider(),
    )
    engine.register_capability("filesystem", FilesystemCapability(str(tmp_path)))
    init_app(RuntimeKernel(event_bus, state_engine, engine), engine, state_engine, event_bus)
    state = state_engine.get_execution("delivery-test")
    delivery_dir = tmp_path / ".gptmoss" / "deliveries" / "delivery-test"
    delivery_dir.mkdir(parents=True)
    archive = delivery_dir / "report-delivery.zip"
    archive.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    state.results["delivery_package"] = {
        "archive_path": str(archive), "profile": "professional-local",
        "archive_sha256": "digest", "archive_size_bytes": archive.stat().st_size,
    }
    client = ASGIClient(app)

    metadata = client.get("/executions/delivery-test/delivery")
    assert metadata.status_code == 200
    assert metadata.json()["download_url"].endswith("?download=true")
    downloaded = client.get("/executions/delivery-test/delivery?download=true")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "application/zip"
    assert downloaded.content == archive.read_bytes()

    state.results["delivery_package"]["archive_path"] = str(tmp_path / "outside.zip")
    assert client.get("/executions/delivery-test/delivery?download=true").status_code == 404


def test_gui_management_api_complete_flow(tmp_path, monkeypatch):
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

    uploaded_docx = client.post("/artifacts", json={
        "filename": "architecture.docx",
        "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "content_base64": _api_docx_payload(),
    })
    assert uploaded_docx.status_code == 201
    docx_metadata = uploaded_docx.json()
    assert docx_metadata["document_title"] == "Dossier API"
    assert docx_metadata["document_blocks"] == 2
    docx_preview = client.get(f"/artifacts/{docx_metadata['id']}/preview")
    assert docx_preview.status_code == 200
    assert docx_preview.json()["preview_type"] == "document"
    assert "Contenu DOCX réellement extrait." in docx_preview.json()["text"]
    assert docx_preview.json()["document"]["block_count"] == 2
    assert any(
        item["id"] == docx_metadata["id"] and item["document_title"] == "Dossier API"
        for item in client.get("/artifacts").json()
    )
    search = client.get("/artifacts/search?q=contenu+extrait")
    assert search.status_code == 200
    assert search.json()["index"]["documents"] >= 2
    assert search.json()["results"][0]["artifact_id"] == docx_metadata["id"]
    assert search.json()["results"][0]["provenance"][0]["source_name"] == "architecture.docx"
    assert client.get("/artifacts/search?q=").status_code == 400

    with monkeypatch.context() as patch:
        def fail_unc_write(*_args, **_kwargs):
            raise OSError(22, "transient UNC redirector failure")

        patch.setattr(exec_engine.artifact_store, "save_base64", fail_unc_write)
        unavailable = client.post("/artifacts", json={
            "filename": "retry.md",
            "content_type": "text/markdown",
            "content_base64": base64.b64encode(b"retry").decode(),
        })
        assert unavailable.status_code == 503
        assert "workspace" in unavailable.json()["detail"]

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
        reject_chat = False

        def do_GET(self):
            payload = json.dumps({"data": [{"id": "model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length) or b"{}")
            assert self.path == "/v1/chat/completions"
            assert request["model"] == "model"
            assert request["max_tokens"] == 1
            if type(self).reject_chat:
                payload = json.dumps({"error": "Unauthorized"}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            payload = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            }).encode()
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
        assert connection.json() == {
            "status": "connected",
            "model_available": True,
            "models_count": 1,
            "chat_completion": True,
        }
        ModelsHandler.reject_chat = True
        denied = client.post("/api/settings/test-connection", json={
            **settings, "base_url": f"http://127.0.0.1:{provider.server_port}/v1"
        })
        assert denied.status_code == 502
        assert "chat completions (HTTP 401)" in denied.json()["detail"]
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=2)

    assert client.delete(f"/memory/{memory_id}").status_code == 200
    assert client.delete("/skills/gui-review").status_code == 200
    assert client.delete("/skills/imported-skill").status_code == 200
    assert client.delete(f"/artifacts/{docx_metadata['id']}").status_code == 200
    assert client.get("/artifacts/search?q=contenu+extrait").json()["results"] == []
    assert client.delete(f"/artifacts/{artifact_id}").status_code == 200


def test_gui_contains_complete_management_controls():
    from pathlib import Path

    gui = (Path(__file__).parents[1] / "gptmoss" / "api" / "gui.html").read_text(encoding="utf-8")
    for marker in (
        "previewArtifact", "importLibrarySkill", "validateLibrarySkill", "saveMemory",
        "createSubagent", "library-diagnostics", "library-audit", "revealApiKey",
        "testLlmConnection", "collectSettingsPayload", ".docx,.pptx",
        "document_blocks", "overflow-wrap:anywhere", "openServerModal",
        "serverAction", "refreshServerStatus", 'id="server-modal"',
        'id="server-start"', 'id="server-stop"', 'id="server-restart"',
        'id="server-rebind"', "/api/runtime-control",
        'id="settings-document-engine"', 'id="settings-document-checkpoint"',
        'id="settings-document-target-words"', 'id="settings-diagram-rendering"',
        'id="settings-docx-embed-diagrams"',
        'id="library-document-search"', 'id="library-document-results"',
        "/artifacts/search", 'id="library-agent-profiles"',
        'id="library-evolution"', "/agent-profiles", "/evolution",
        "uploadedTaskAttachments", "resetTaskAttachmentUploads",
        "resumableFailure", "Authentification LLM refusée",
        'id="task-planning-mode"', "appendLlmStream", "clearLlmStream",
        "planning_mode", "task_title",
        'id="task-corpus-folder"', "webkitdirectory", "uploadSelectedCorpusFolder",
        'id="task-corpus-name"',
        '"pdf":"application/pdf"', 'requestApi("/corpora"',
        'id="library-corpora"', "toggleCorpusAttachment", "selectedLibraryCorpora", "deleteCorpus",
        "applyConversationScroll", "scheduleFetchExecutionDetails",
        "lastConversationSignature", "chatFollowLatest",
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
        "overflow-x: hidden;",
        "grid-template-columns: repeat(3, minmax(0, 1fr));",
        "#task-attachments",
    ):
        assert marker in gui

    assert "grid-template-columns: minmax(420px, 3fr) minmax(300px, 2fr);" not in gui
