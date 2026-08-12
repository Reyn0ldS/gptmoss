import asyncio

import pytest

from gptmoss.core.delivery_coordinator import DeliveryCoordinator
from gptmoss.core.event_bus import EventBus
from gptmoss.core.provider_recovery import ProviderRecoveryCoordinator
from gptmoss.core.state import StateEngine
from tests.mock_llm import MockLLMProvider


def test_delivery_coordinator_collects_descendant_evidence_once():
    state = StateEngine()
    parent = state.get_execution("parent")
    parent.variables["tool_call_history"] = [{"result": "parent"}]
    child = state.get_execution("child")
    child.variables.update({
        "parent_execution_id": "parent",
        "tool_call_history": [{"result": "child"}],
    })
    coordinator = DeliveryCoordinator(state, lambda name: None)

    assert coordinator.histories("parent") == [
        {"result": "parent"}, {"result": "child"},
    ]
    assert coordinator.workspace("parent") is None


def test_provider_recovery_classifies_permanent_and_transient_errors():
    assert ProviderRecoveryCoordinator.is_permanent(Exception("HTTP 401 invalid API key"))
    assert not ProviderRecoveryCoordinator.is_transient(Exception("HTTP 401 invalid API key"))
    assert ProviderRecoveryCoordinator.is_transient(ConnectionError("provider unavailable"))
    assert not ProviderRecoveryCoordinator.is_transient(ValueError("invalid payload"))


@pytest.mark.asyncio
async def test_provider_resume_schedule_is_idempotent_and_stoppable():
    state = StateEngine()
    execution = state.get_execution("waiting")
    state.transition_execution(execution, "waiting_provider")
    executed = []

    async def execute(execution_id, task):
        executed.append((execution_id, task))

    coordinator = ProviderRecoveryCoordinator(
        EventBus(), state, MockLLMProvider(), execute, max_attempts=2
    )
    coordinator.schedule("waiting", delay_seconds=30)
    first = coordinator.tasks["waiting"]
    coordinator.schedule("waiting", delay_seconds=30)
    assert coordinator.tasks["waiting"] is first
    await coordinator.stop()
    assert first.done()
    assert coordinator.tasks == {}
    assert executed == []
