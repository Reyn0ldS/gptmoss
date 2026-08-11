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
