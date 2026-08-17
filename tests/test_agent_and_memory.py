import os
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import asyncio
from gptmoss.memory.json_store import JSONMemoryProvider
from gptmoss.capabilities.agent import AgentCapability
from gptmoss.core.event_bus import EventBus
from gptmoss.core.state import StateEngine
from gptmoss.core.execution import ExecutionEngine
from gptmoss.core.kernel import RuntimeKernel
from gptmoss.policies.simple import SimplePolicyProvider
from tests.mock_llm import MockLLMProvider

TEMP_DIR = "tests/temp_test_workspace"

@pytest.fixture(autouse=True)
def setup_teardown():
    os.makedirs(TEMP_DIR, exist_ok=True)
    yield
    if os.path.exists(TEMP_DIR):
        shutil.rmtree(TEMP_DIR)

@pytest.mark.asyncio
async def test_json_memory_persistence():
    file_path = os.path.join(TEMP_DIR, "memories.json")
    
    # 1. Instantiate and store memory
    provider = JSONMemoryProvider(file_path=file_path)
    mem_id = await provider.store("Testing persistent storage of facts.", validated=True)
    assert mem_id is not None
    
    # Verify it saved to disk
    assert os.path.exists(file_path)
    
    # 2. Re-instantiate from the same file
    provider2 = JSONMemoryProvider(file_path=file_path)
    results = await provider2.search("Testing persistent")
    assert len(results) >= 1
    assert results[0]["value"] == "Testing persistent storage of facts."

@pytest.mark.asyncio
async def test_hybrid_memory_requires_validation_and_tracks_provenance():
    provider = JSONMemoryProvider(file_path=os.path.join(TEMP_DIR, "hybrid_memories.json"))
    memory_id = await provider.store(
        "Use the project convention for release notes.",
        provenance={"source": "review", "execution_id": "exec-1"},
    )

    assert await provider.search("release notes") == []
    assert await provider.validate(memory_id, validated_by="reviewer")
    results = await provider.search("release notes")
    assert results[0]["provenance"]["source"] == "review"
    assert results[0]["validated_by"] == "reviewer"

@pytest.mark.asyncio
async def test_hybrid_memory_session_and_expiration():
    provider = JSONMemoryProvider(file_path=os.path.join(TEMP_DIR, "hybrid_memories.json"))
    await provider.store_session("exec-session", "The current task uses a blue theme.")
    assert (await provider.search("blue theme", session_id="exec-session"))[0]["value"].startswith("The current")
    await provider.clear_session("exec-session")
    assert await provider.search("blue theme", session_id="exec-session") == []

    await provider.store("Expired memory", validated=True, ttl_seconds=-1)
    assert await provider.search("expired") == []

@pytest.mark.asyncio
async def test_agent_capability_delegation():
    # Setup mock MOSS runtime stack
    event_bus = EventBus()
    state_engine = StateEngine()
    memory_provider = JSONMemoryProvider(file_path=os.path.join(TEMP_DIR, "memories.json"))
    
    llm = MockLLMProvider()
    policy = SimplePolicyProvider()
    
    exec_engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=None,
        llm_provider=llm,
        planner=None,
        policy_provider=policy
    )
    
    agent_cap = AgentCapability(kernel=None, workspace_root=TEMP_DIR)
    exec_engine.register_capability("agent", agent_cap)
    
    kernel = RuntimeKernel(
        event_bus=event_bus,
        state_engine=state_engine,
        execution_engine=exec_engine
    )
    agent_cap.kernel = kernel
    
    # Verify execution of spawn action
    result = await agent_cap.spawn(task="Write a greeting file.", system_prompt="Test system prompt")
    assert "Sub-agent spawned successfully" in result
    assert "Execution ID" in result


@pytest.mark.asyncio
async def test_agent_status_and_execute_subtask_cover_terminal_modes(monkeypatch):
    state_engine = StateEngine()
    submissions = []
    parent = state_engine.get_execution("parent-1")
    parent.status = "running"
    parent.variables.update({
        "project_id": "site-demo",
        "attachment_ids": ["att-1"],
        "requested_skills": ["documentation"],
        "project_path": "D:/trusted/site-demo",
    })

    class ImmediateKernel:
        def __init__(self):
            self.state_engine = state_engine
            self.execution_engine = SimpleNamespace(state_engine=state_engine)

        async def submit_task(self, task, agent_config):
            execution_id = f"sub-{len(submissions)}"
            submissions.append((execution_id, task, agent_config))
            state = state_engine.get_execution(execution_id)
            state.current_step = 2
            if "provider" in task:
                state.status = "waiting_provider"
            elif "cancel" in task:
                state.status = "cancelled"
            else:
                state.status = "completed"
                state_engine.get_conversation(execution_id).messages.append({
                    "role": "assistant", "content": f"result for {task}",
                })
            return execution_id

    async def advance_then_sleep(_delay):
        for state in state_engine.executions.values():
            if state.status == "waiting_provider":
                state.status = "completed"
                state_engine.get_conversation(state.execution_id).messages.append({
                    "role": "assistant", "content": "result after provider recovery",
                })

    sleep = AsyncMock(side_effect=advance_then_sleep)
    monkeypatch.setattr("gptmoss.capabilities.agent.asyncio.sleep", sleep)
    capability = AgentCapability(kernel=ImmediateKernel(), workspace_root=TEMP_DIR)

    completed = await capability.execute_subtask(
        "complete work", system_prompt="specialist prompt", role_name="Reviewer",
        context={"execution_id": "parent-1"},
    )
    recovered = await capability.execute_subtask("provider unavailable")
    cancelled = await capability.execute_subtask("cancel work")

    assert completed.endswith("result for complete work")
    assert recovered.endswith("result after provider recovery")
    assert cancelled.endswith("Final status: cancelled.")
    assert "Status: completed" in capability.status("sub-0")
    assert "Current Step: 2" in capability.status("sub-0")
    assert "result for complete work" in capability.status("sub-0")
    assert capability.status("missing") == "Error: Sub-agent execution_id missing not found."
    assert submissions[0][2]["system_prompt"] == "specialist prompt"
    assert submissions[0][2]["role_name"] == "Reviewer"
    assert submissions[0][2]["parent_execution_id"] == "parent-1"
    assert submissions[0][2]["variables"]["project_id"] == "site-demo"
    assert submissions[0][2]["variables"]["attachment_ids"] == ["att-1"]
    assert submissions[0][2]["skills"] == ["documentation"]
    assert sleep.await_count == 3

@pytest.mark.asyncio
async def test_developer_team_capability_wiring():
    from gptmoss.capabilities.devteam import DeveloperTeamCapability
    
    event_bus = EventBus()
    state_engine = StateEngine()
    
    llm = MockLLMProvider()
    policy = SimplePolicyProvider()
    
    exec_engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=None,
        llm_provider=llm,
        planner=None,
        policy_provider=policy
    )
    
    devteam_cap = DeveloperTeamCapability(kernel=None, workspace_root=TEMP_DIR)
    exec_engine.register_capability("devteam", devteam_cap)
    
    kernel = RuntimeKernel(
        event_bus=event_bus,
        state_engine=state_engine,
        execution_engine=exec_engine
    )
    devteam_cap.kernel = kernel
    
    assert devteam_cap.kernel is not None
    assert exec_engine.get_capability("devteam") == devteam_cap

@pytest.mark.asyncio
async def test_state_engine_persistence():
    file_path = os.path.join(TEMP_DIR, "state_store.json")
    
    # 1. Save state
    engine = StateEngine(persist_path=file_path)
    convo = engine.get_conversation("test_convo")
    convo.messages.append({"role": "user", "content": "Hello World"})
    exec_state = engine.get_execution("test_exec")
    exec_state.status = "completed"
    
    engine.save_to_disk()
    assert os.path.exists(file_path)
    
    # 2. Reload state
    engine2 = StateEngine(persist_path=file_path)
    assert "test_convo" in engine2.conversations
    assert engine2.conversations["test_convo"].messages[0]["content"] == "Hello World"
    assert engine2.executions["test_exec"].status == "completed"
