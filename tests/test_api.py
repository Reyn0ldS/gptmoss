import asyncio

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

def test_api_settings_flow():
    import os
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
    exec_engine.register_capability("filesystem", FilesystemCapability("."))

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
        "workspace_path": ".",
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

    # Clean up test config.json file
    if os.path.exists("./config.json"):
        os.remove("./config.json")

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
    invalid["max_context_chars"] = 100
    assert client.post("/api/settings", json=invalid).status_code == 422


def test_gui_uses_sanitized_markdown_renderer():
    from pathlib import Path

    gui = (Path(__file__).parents[1] / "gptmoss" / "api" / "gui.html").read_text(encoding="utf-8")
    assert "function renderSafeMarkdown" in gui
    assert 'contentHtml = renderSafeMarkdown(msg.content || "");' in gui
    assert 'contentHtml = marked.parse(msg.content || "");' not in gui
    assert "--bg-card:" in gui
    assert "--text-normal:" in gui
