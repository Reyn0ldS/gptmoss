import pytest
import os
import shutil
from gptmoss.core.event_bus import EventBus
from gptmoss.core.state import StateEngine
from gptmoss.core.context import ContextEngine
from gptmoss.core.execution import ExecutionEngine
from gptmoss.planners.simple import SimplePlanner
from gptmoss.policies.simple import SimplePolicyProvider
from gptmoss.capabilities.filesystem import FilesystemCapability
from tests.mock_llm import MockLLMProvider

@pytest.fixture
def test_workspace():
    workspace_dir = "./test_execution_workspace"
    os.makedirs(workspace_dir, exist_ok=True)
    yield workspace_dir
    if os.path.exists(workspace_dir):
        shutil.rmtree(workspace_dir)

@pytest.mark.asyncio
async def test_execution_engine_filesystem_flow(test_workspace):
    event_bus = EventBus()
    state_engine = StateEngine()
    
    # Initialize mock LLM
    mock_llm = MockLLMProvider()
    # First LLM call: Planner (SimplePlanner) outputs steps
    mock_llm.add_response(
        content='{"steps": [{"id": 0, "description": "Write info to hello.txt", "status": "pending"}], "rationale": "Direct file creation"}'
    )
    # Second LLM call: Execution loop calls tool
    mock_llm.add_response(
        tool_calls=[{
            "id": "call_123",
            "type": "function",
            "function": {
                "name": "filesystem__write",
                "arguments": {"path": "hello.txt", "content": "MOSS System"}
            }
        }]
    )
    # Third LLM call: Finish step response
    mock_llm.add_response(content="Written successfully.")

    from gptmoss.memory.ram import RAMMemoryProvider
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    planner = SimplePlanner(mock_llm)
    policy = SimplePolicyProvider()

    engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=planner,
        policy_provider=policy
    )
    engine.register_capability("filesystem", FilesystemCapability(test_workspace))

    exec_id = "test-exec-1"
    await engine.execute_task(exec_id, "Write MOSS System to hello.txt")

    # Verify file was written
    target_file = os.path.join(test_workspace, "hello.txt")
    assert os.path.exists(target_file)
    with open(target_file, "r") as f:
        assert f.read() == "MOSS System"

    # Verify execution state
    exec_state = state_engine.get_execution(exec_id)
    assert exec_state.status == "completed"
    assert exec_state.current_step == 1

@pytest.mark.asyncio
async def test_sub_agent_parent_task_injection(test_workspace):
    from gptmoss.memory.ram import RAMMemoryProvider
    event_bus = EventBus()
    state_engine = StateEngine()
    mock_llm = MockLLMProvider()
    
    # Simple planner output
    mock_llm.add_response(
        content='{"steps": [], "rationale": "Empty plan for test"}'
    )
    
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    planner = SimplePlanner(mock_llm)
    policy = SimplePolicyProvider()
    
    engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=planner,
        policy_provider=policy
    )
    
    # 1. Start parent execution
    parent_id = "parent-id-abc"
    state_engine.get_execution(parent_id)
    
    # Execute task sets parent_task variable in state and convo
    await engine.execute_task(parent_id, "Create a SQLite Database Browser")
    
    parent_state = state_engine.get_execution(parent_id)
    assert parent_state.variables["parent_task"] == "Create a SQLite Database Browser"
    
    # 2. Simulate sub-agent spawn by setting variables and spawning
    child_id = "child-id-xyz"
    child_state = state_engine.get_execution(child_id)
    child_state.variables["parent_execution_id"] = parent_id
    child_state.variables["role_name"] = "Architecte"
    child_state.variables["parent_task"] = parent_state.variables["parent_task"]
    
    # ExecutionStarted logic initializes convo with parent task context
    await engine.execute_task(child_id, "Architect/Analyst: Analyze needs and write technical specifications (specs.md)")
    
    child_convo = state_engine.get_conversation(child_id)
    assert len(child_convo.messages) >= 1
    initial_msg = child_convo.messages[0]["content"]
    assert "Main Project Task: Create a SQLite Database Browser" in initial_msg
    assert "Your Specific Subtask: Architect/Analyst: Analyze needs and write technical specifications (specs.md)" in initial_msg

@pytest.mark.asyncio
async def test_sub_agent_system_prompt_customization(test_workspace):
    from gptmoss.memory.ram import RAMMemoryProvider
    event_bus = EventBus()
    state_engine = StateEngine()
    mock_llm = MockLLMProvider()
    
    captured_messages = []
    async def on_llm_request(event):
        if event.type == "LLMRequest":
            captured_messages.append(event.payload["messages"])
            
    event_bus.subscribe_all(on_llm_request)
    mock_llm.add_response(content="Design specs completed.")
    
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    planner = SimplePlanner(mock_llm)
    policy = SimplePolicyProvider()
    
    engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=planner,
        policy_provider=policy
    )
    
    exec_id = "test-coder-exec"
    state = state_engine.get_execution(exec_id)
    state.status = "pending"
    state.variables["role_name"] = "Développeur"
    state.current_plan = {
        "steps": [{"id": 0, "description": "Write source code for calculator", "status": "pending"}],
        "rationale": "Test custom system prompt"
    }
    state.current_step = 0
    
    await engine.execute_task(exec_id, "Write source code for calculator")
    
    assert len(captured_messages) > 0
    first_llm_run_messages = captured_messages[0]
    assert first_llm_run_messages[0]["role"] == "system"
    assert "Specialized Developer/Coder Agent" in first_llm_run_messages[0]["content"]

@pytest.mark.asyncio
async def test_dag_execution_scheduling(test_workspace):
    from gptmoss.memory.ram import RAMMemoryProvider
    event_bus = EventBus()
    state_engine = StateEngine()
    mock_llm = MockLLMProvider()
    
    started_steps = []
    async def on_step_started(event):
        if event.type == "StepStarted" and event.payload["execution_id"] == exec_id:
            started_steps.append(event.payload["step_index"])
            
    event_bus.subscribe_all(on_step_started)
    
    mock_llm.add_response(content="Architecture specs created.")
    mock_llm.add_response(content="Code core implemented.")
    mock_llm.add_response(content="Tests written and passed.")
    
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    planner = SimplePlanner(mock_llm)
    policy = SimplePolicyProvider()
    
    engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=planner,
        policy_provider=policy
    )
    
    exec_id = "test-dag-exec"
    state = state_engine.get_execution(exec_id)
    state.status = "pending"
    state.current_plan = {
        "steps": [
            {"id": 10, "description": "Architect: spec designs", "dependencies": [], "status": "pending"},
            {"id": 20, "description": "Coder: write source code", "dependencies": [10], "status": "pending"},
            {"id": 30, "description": "QA: run checks", "dependencies": [20], "status": "pending"}
        ],
        "rationale": "DAG test plan"
    }
    state.current_step = 0
    
    await engine.execute_task(exec_id, "Test DAG scheduler execution order")
    
    assert len(started_steps) == 3
    assert started_steps == [0, 1, 2]
    assert state.status == "completed"

def test_sub_agent_capabilities_filtering():
    from gptmoss.capabilities.agent import AgentCapability
    from gptmoss.capabilities.filesystem import FilesystemCapability
    
    event_bus = EventBus()
    state_engine = StateEngine()
    mock_llm = MockLLMProvider()
    policy = SimplePolicyProvider()
    
    engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=None,
        llm_provider=mock_llm,
        planner=None,
        policy_provider=policy
    )
    
    engine.register_capability("agent", AgentCapability(kernel=None))
    engine.register_capability("filesystem", FilesystemCapability(r"C:\workspace"))
    
    parent_schemas = engine.get_capabilities_schemas(is_sub_agent=False)
    parent_actions = [s["function"]["name"] for s in parent_schemas]
    assert any("agent__spawn" in act for act in parent_actions)
    assert any("filesystem__read" in act for act in parent_actions)
    
    child_schemas = engine.get_capabilities_schemas(is_sub_agent=True)
    child_actions = [s["function"]["name"] for s in child_schemas]
    assert not any("agent__" in act for act in child_actions)
    assert any("filesystem__read" in act for act in child_actions)

@pytest.mark.asyncio
async def test_resumption_recovery_from_stuck_steps(test_workspace):
    from gptmoss.memory.ram import RAMMemoryProvider
    event_bus = EventBus()
    state_engine = StateEngine()
    mock_llm = MockLLMProvider()
    
    mock_llm.add_response(content="Recovered step output.")
    
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    planner = SimplePlanner(mock_llm)
    policy = SimplePolicyProvider()
    
    engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=planner,
        policy_provider=policy
    )
    
    exec_id = "test-recovery-exec"
    state = state_engine.get_execution(exec_id)
    state.status = "running"
    state.current_plan = {
        "steps": [
            {"id": 1, "description": "Write source code for calculator", "status": "running"}
        ],
        "rationale": "Recovery test"
    }
    state.current_step = 0
    
    await engine.execute_task(exec_id, "Write source code for calculator")
    
    assert state.current_plan["steps"][0]["status"] == "completed"
    assert state.status == "completed"

@pytest.mark.asyncio
async def test_react_loop_no_early_exit(test_workspace):
    from gptmoss.memory.ram import RAMMemoryProvider
    event_bus = EventBus()
    state_engine = StateEngine()
    mock_llm = MockLLMProvider()
    
    mock_llm.add_response(content="I will explore first...")
    mock_llm.add_response(content="Now writing the README.", tool_calls=[])
    
    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    planner = SimplePlanner(mock_llm)
    policy = SimplePolicyProvider()
    
    engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=planner,
        policy_provider=policy
    )
    
    exec_id = "test-no-early-exit-exec"
    state = state_engine.get_execution(exec_id)
    state.status = "pending"
    state.current_plan = {
        "steps": [
            {"id": 1, "description": "Just do some calculations", "status": "pending"}
        ],
        "rationale": "Early exit test"
    }
    state.current_step = 0
    
    await engine.execute_task(exec_id, "Write readme file")
    
    convo = state_engine.get_conversation(exec_id)
    assert len(convo.messages) == 5
    assert convo.messages[2]["content"] == "I will explore first..."
    assert "System: You did not call any tools" in convo.messages[3]["content"]
    assert convo.messages[4]["content"] == "Now writing the README."
    assert state.status == "completed"
