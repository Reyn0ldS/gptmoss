import pytest
import asyncio
from gptmoss.core.event_bus import EventBus
from gptmoss.core.state import StateEngine
from gptmoss.core.context import ContextEngine
from gptmoss.core.execution import ExecutionEngine
from gptmoss.planners.simple import SimplePlanner
from gptmoss.policies.simple import SimplePolicyProvider
from gptmoss.capabilities.shell import ShellCapability
from tests.mock_llm import MockLLMProvider
from gptmoss.memory.ram import RAMMemoryProvider

@pytest.mark.asyncio
async def test_policy_approval_and_resume_flow():
    event_bus = EventBus()
    state_engine = StateEngine()
    
    # Setup mock LLM
    mock_llm = MockLLMProvider()
    # 1. Planner
    mock_llm.add_response(
        content='{"steps": [{"id": 0, "description": "Run shell script", "status": "pending"}], "rationale": "Run shell"}'
    )
    # 2. Tool call
    mock_llm.add_response(
        tool_calls=[{
            "id": "call_shell_1",
            "type": "function",
            "function": {
                "name": "shell__execute",
                "arguments": {"command": "echo 'Hello MOSS'"}
            }
        }]
    )
    # 3. Final text completion
    mock_llm.add_response(content="Shell execution finished successfully.")

    memory = RAMMemoryProvider()
    context_engine = ContextEngine(state_engine, memory)
    planner = SimplePlanner(mock_llm)
    
    # Force shell to require approval
    policy = SimplePolicyProvider(approval_required_capabilities=["shell"])

    engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context_engine,
        llm_provider=mock_llm,
        planner=planner,
        policy_provider=policy
    )
    engine.register_capability("shell", ShellCapability("."))

    exec_id = "test-exec-policy-approval"
    
    # Register event tracker
    approval_events = []
    async def track_events(event):
        if event.type == "ApprovalRequested":
            approval_events.append(event)
    event_bus.subscribe("ApprovalRequested", track_events)

    # Start task
    await engine.execute_task(exec_id, "Run echo Hello MOSS")

    # Verify task was paused
    exec_state = state_engine.get_execution(exec_id)
    assert exec_state.status == "paused"
    assert "pending_approval" in exec_state.variables
    assert len(approval_events) == 1
    assert approval_events[0].payload["capability"] == "shell"
    assert approval_events[0].payload["action"] == "execute"

    # Resume the execution with approval
    await engine.resume_with_decision(exec_id, decision="allow", reason="User clicked yes")

    # Let the loop execute the command and finish
    for _ in range(10):
        await asyncio.sleep(0.1)
        if exec_state.status == "completed":
            break

    assert exec_state.status == "completed"
    assert "pending_approval" not in exec_state.variables
