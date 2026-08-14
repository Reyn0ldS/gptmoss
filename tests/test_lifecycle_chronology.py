import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from gptmoss.api.server import ConnectionManager, app, init_app
from gptmoss.capabilities.filesystem import FilesystemCapability
from gptmoss.core import ContextEngine, Event, EventBus, ExecutionEngine, RuntimeKernel, StateEngine
from gptmoss.memory import RAMMemoryProvider
from gptmoss.planners import SimplePlanner
from gptmoss.policies import SimplePolicyProvider
from tests.mock_llm import MockLLMProvider


def _runtime(tmp_path):
    event_bus = EventBus()
    events = []
    event_bus.subscribe_all(events.append)
    state = StateEngine(persist_path=str(tmp_path / "state.json"))
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state, ContextEngine(state, RAMMemoryProvider()), llm,
        SimplePlanner(llm), SimplePolicyProvider(),
    )
    engine.register_capability("filesystem", FilesystemCapability(str(tmp_path), state))
    engine.execute_task = AsyncMock()
    kernel = RuntimeKernel(event_bus, state, engine)
    init_app(kernel, engine, state, event_bus)
    return event_bus, events, state, engine


@pytest.mark.asyncio
async def test_execution_control_api_preserves_transition_chronology(tmp_path):
    _, events, state_engine, engine = _runtime(tmp_path)
    execution = state_engine.get_execution("lifecycle")
    execution.status = "running"
    execution.variables["task"] = "Chronological task"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        paused = await client.post("/executions/lifecycle/pause")
        resumed = await client.post("/executions/lifecycle/resume")
        await asyncio.sleep(0)
        cancelled = await client.post("/executions/lifecycle/cancel")
        deleted = await client.delete("/executions/lifecycle")
        state_engine.get_execution("remaining-a").status = "completed"
        state_engine.get_execution("remaining-b").status = "failed"
        cleared = await client.post("/executions/clear-all")

    assert [response.status_code for response in (paused, resumed, cancelled, deleted, cleared)] == [200] * 5
    assert paused.json()["status"] == "paused"
    assert resumed.json()["status"] == "running"
    assert cancelled.json() == {"status": "cancelled", "execution_ids": ["lifecycle"]}
    assert not state_engine.executions and not state_engine.conversations
    engine.execute_task.assert_awaited_once_with("lifecycle", "Chronological task")

    lifecycle_types = [event.type for event in events if event.type in {
        "ExecutionPaused", "ExecutionResumed", "ExecutionCancelled", "TaskDeleted", "TasksCleared"
    }]
    assert lifecycle_types == [
        "ExecutionPaused", "ExecutionResumed", "ExecutionCancelled", "TaskDeleted", "TasksCleared"
    ]
    assert [event.timestamp for event in events] == sorted(event.timestamp for event in events)
    assert [event.payload.get("execution_id") for event in events[:-1]] == ["lifecycle"] * 4


@pytest.mark.asyncio
async def test_cancel_interrupts_inflight_execution_and_clears_owned_task(tmp_path):
    event_bus = EventBus()
    state = StateEngine(persist_path=str(tmp_path / "state.json"))

    class BlockingLLM(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def completion(self, *args, **kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    llm = BlockingLLM()
    engine = ExecutionEngine(
        event_bus, state, ContextEngine(state, RAMMemoryProvider()), llm,
        SimplePlanner(llm), SimplePolicyProvider(),
    )
    engine.register_capability("filesystem", FilesystemCapability(str(tmp_path), state))
    kernel = RuntimeKernel(event_bus, state, engine)
    init_app(kernel, engine, state, event_bus)
    execution = state.get_execution("blocked")
    execution.status = "running"
    execution.variables["task"] = "Wait for cancellation"
    execution.current_plan = {
        "steps": [{
            "id": 0,
            "role": "coordinator",
            "specialist": "Blocking specialist",
            "description": "Wait for the provider",
            "dependencies": [],
            "status": "pending",
            "acceptance_criteria": ["Provider returned."],
        }],
    }
    engine.start_execution("blocked", "Wait for cancellation")
    await asyncio.wait_for(llm.started.wait(), timeout=2)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/executions/blocked/cancel")

    assert response.status_code == 200
    await asyncio.wait_for(llm.cancelled.wait(), timeout=2)
    assert state.get_execution("blocked").status == "cancelled"
    assert "blocked" not in engine._active_execution_tasks


@pytest.mark.asyncio
async def test_pause_interrupts_inflight_execution_without_cancelling_state(tmp_path):
    event_bus = EventBus()
    state = StateEngine(persist_path=str(tmp_path / "state.json"))

    class BlockingLLM(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def completion(self, *args, **kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    llm = BlockingLLM()
    engine = ExecutionEngine(
        event_bus, state, ContextEngine(state, RAMMemoryProvider()), llm,
        SimplePlanner(llm), SimplePolicyProvider(),
    )
    engine.register_capability("filesystem", FilesystemCapability(str(tmp_path), state))
    init_app(RuntimeKernel(event_bus, state, engine), engine, state, event_bus)
    execution = state.get_execution("paused-blocked")
    execution.status = "running"
    execution.variables["task"] = "Pause me"
    execution.current_plan = {
        "steps": [{
            "id": 0, "role": "coordinator", "specialist": "Blocking specialist",
            "description": "Wait", "dependencies": [], "status": "pending",
            "acceptance_criteria": ["Wait completed."],
        }],
    }
    engine.start_execution("paused-blocked", "Pause me")
    await asyncio.wait_for(llm.started.wait(), timeout=2)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/executions/paused-blocked/pause")

    assert response.status_code == 200
    await asyncio.wait_for(llm.cancelled.wait(), timeout=2)
    assert state.get_execution("paused-blocked").status == "paused"
    assert execution.current_plan["steps"][0]["status"] == "pending"
    assert "paused-blocked" not in engine._active_execution_tasks


@pytest.mark.asyncio
async def test_scheduler_remains_available_while_execution_task_runs(tmp_path):
    _, _, _, engine = _runtime(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    timer_fired = asyncio.Event()

    async def blocked_execute(execution_id, task):
        started.set()
        await release.wait()

    engine.execute_task = blocked_execute
    engine.schedule_execution("scheduled-running", "Long task")
    await asyncio.wait_for(started.wait(), timeout=2)
    engine.scheduler.schedule(timer_fired.set, delay=0, job_id="independent-timer")

    await asyncio.wait_for(timer_fired.wait(), timeout=2)
    release.set()
    await engine.stop_runtime_services()


@pytest.mark.asyncio
async def test_stop_runtime_services_interrupts_all_owned_executions(tmp_path):
    _, _, state, engine = _runtime(tmp_path)
    started = asyncio.Event()

    async def blocked_execute(execution_id, task):
        started.set()
        await asyncio.Event().wait()

    engine.execute_task = blocked_execute
    task = engine.start_execution("shutdown", "Stop me")
    await asyncio.wait_for(started.wait(), timeout=2)

    await engine.stop_runtime_services()

    assert task.cancelled()
    assert not engine._active_execution_tasks


@pytest.mark.asyncio
async def test_large_dag_keeps_all_steps_but_bounds_the_active_wave():
    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus,
        state_engine,
        ContextEngine(state_engine, RAMMemoryProvider()),
        llm,
        SimplePlanner(llm),
        SimplePolicyProvider(),
        max_parallel_plan_steps=3,
    )
    state = state_engine.get_execution("large-dag")
    state.status = "running"
    state.variables["parent_execution_id"] = "test-parent"
    steps = [
        {"id": index, "status": "pending", "dependencies": [], "role": "architect"}
        for index in range(80)
    ]
    active = 0
    maximum = 0

    async def run_step(step):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        step["status"] = "completed"
        active -= 1

    await engine._coordinate_plan_execution(
        "large-dag", state, steps, "Long bounded task", run_step, {}
    )

    assert len(steps) == 80
    assert all(step["status"] == "completed" for step in steps)
    assert maximum == 3
    assert state.variables["plan_parallelism_limit"] == 3
    assert state.status == "completed"


@pytest.mark.asyncio
async def test_approval_endpoints_record_ordered_scope_decisions(tmp_path):
    _, events, state_engine, engine = _runtime(tmp_path)
    tool = state_engine.get_execution("tool-approval")
    tool.status = "paused"
    tool.variables.update({
        "task": "Use a reviewed tool",
        "pending_approval": {
            "capability": "shell", "action": "execute",
            "arguments": {"command": "python -m pytest -q"}, "fingerprint": "tool-fp",
        },
    })
    rejected_scope = state_engine.get_execution("scope-reject")
    rejected_scope.status = "paused"
    rejected_scope.variables.update({
        "task": "Do not reduce scope",
        "pending_scope_approval": {"contract_sha256": "reject-sha", "changes": ["REQ-004"]},
    })
    approved_scope = state_engine.get_execution("scope-allow")
    approved_scope.status = "paused"
    approved_scope.variables.update({
        "task": "Accept explicit scope",
        "pending_scope_approval": {"contract_sha256": "allow-sha", "changes": ["REQ-005"]},
    })

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        approved_tool = await client.post("/executions/tool-approval/approve", json={"reason": "reviewed"})
        rejected = await client.post("/executions/scope-reject/reject", json={"reason": "mandatory"})
        approved = await client.post("/executions/scope-allow/approve", json={"reason": "accepted"})
        await asyncio.sleep(0)

    assert approved_tool.json() == {"status": "resumed", "decision": "allow"}
    assert tool.status == "running"
    assert tool.variables["pending_approval"]["decision"] == "allow"
    assert tool.variables["pending_approval"]["reason"] == "reviewed"
    assert rejected.json() == {"status": "failed", "decision": "reject"}
    assert rejected_scope.status == "failed"
    assert rejected_scope.variables["scope_decisions"][-1] == pytest.approx({
        "contract_sha256": "reject-sha", "decision": "reject", "reason": "mandatory",
        "decided_at": rejected_scope.variables["scope_decisions"][-1]["decided_at"],
    })
    assert approved.json() == {"status": "resumed", "decision": "allow"}
    assert approved_scope.variables["approved_scope_contract_sha256"] == "allow-sha"
    assert approved_scope.variables["scope_decisions"][-1]["decision"] == "allow"
    assert [event.type for event in events] == ["ExecutionResumed", "ExecutionFailed", "ScopeApproved"]
    assert [event.timestamp for event in events] == sorted(event.timestamp for event in events)
    assert engine.execute_task.await_count == 2


class _WebSocket:
    def __init__(self, fail=False):
        self.accepted = False
        self.messages = []
        self.fail = fail

    async def accept(self):
        self.accepted = True

    async def send_text(self, message):
        if self.fail:
            raise ConnectionError("closed")
        self.messages.append(json.loads(message))


@pytest.mark.asyncio
async def test_connection_manager_routes_events_in_publication_order():
    manager = ConnectionManager()
    global_ws = _WebSocket()
    selected_ws = _WebSocket()
    other_ws = _WebSocket()
    closed_ws = _WebSocket(fail=True)
    await manager.connect_global(global_ws)
    await manager.connect_global(closed_ws)
    await manager.connect_execution("exec-a", selected_ws)
    await manager.connect_execution("exec-b", other_ws)

    await manager.broadcast_event(Event(type="StepStarted", payload={"execution_id": "exec-a", "step_id": 1}))
    await manager.broadcast_event(Event(type="StepCompleted", payload={"execution_id": "exec-a", "step_id": 1}))

    assert all(ws.accepted for ws in (global_ws, selected_ws, other_ws, closed_ws))
    assert [message["type"] for message in global_ws.messages] == ["StepStarted", "StepCompleted"]
    assert [message["type"] for message in selected_ws.messages] == ["StepStarted", "StepCompleted"]
    assert other_ws.messages == []
    assert closed_ws not in manager.global_connections
