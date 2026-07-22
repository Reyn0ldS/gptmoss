import os
import shutil
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
    mem_id = await provider.store("Testing persistent storage of facts.")
    assert mem_id is not None
    
    # Verify it saved to disk
    assert os.path.exists(file_path)
    
    # 2. Re-instantiate from the same file
    provider2 = JSONMemoryProvider(file_path=file_path)
    results = await provider2.search("Testing persistent")
    assert len(results) >= 1
    assert results[0]["value"] == "Testing persistent storage of facts."

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
