import asyncio

import pytest

from gptmoss.core.delivery_coordinator import DeliveryCoordinator
from gptmoss.core.event_bus import EventBus
from gptmoss.core.provider_recovery import ProviderRecoveryCoordinator
from gptmoss.core.scheduler import Scheduler
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


def test_ssl_certificate_error_is_configuration_not_transient():
    error = Exception(
        "APIConnectionError [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
        "self-signed certificate"
    )
    assert ProviderRecoveryCoordinator.is_tls_configuration(error)
    assert ProviderRecoveryCoordinator.is_permanent(error)
    assert not ProviderRecoveryCoordinator.is_transient(error)
    assert ProviderRecoveryCoordinator.is_transient(ConnectionError("provider unavailable"))
    assert not ProviderRecoveryCoordinator.is_tls_configuration(
        ConnectionError("provider unavailable")
    )
    missing_ca = Exception("SSLError: Could not find a suitable TLS CA certificate bundle")
    assert ProviderRecoveryCoordinator.is_tls_configuration(missing_ca)


def test_openai_wrapped_tls_error_walks_the_exception_cause():
    wrapped = Exception("Connection error.")
    wrapped.__cause__ = Exception(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
    )
    assert ProviderRecoveryCoordinator.is_tls_configuration(wrapped)
    assert not ProviderRecoveryCoordinator.is_transient(wrapped)


def test_vision_rejection_is_configuration_not_transient():
    error = Exception("BadRequestError: this model does not support image_url content")
    assert ProviderRecoveryCoordinator.is_vision_rejected(error)
    assert ProviderRecoveryCoordinator.is_permanent(error)
    assert not ProviderRecoveryCoordinator.is_transient(error)
    timeout = Exception("APITimeoutError timed out while sending image_url payload")
    assert not ProviderRecoveryCoordinator.is_vision_rejected(timeout)
    assert ProviderRecoveryCoordinator.is_transient(timeout)
    assert not ProviderRecoveryCoordinator.is_vision_rejected(
        Exception("unknown part type in tool schema")
    )


@pytest.mark.asyncio
async def test_provider_resume_schedule_is_idempotent_and_stoppable():
    state = StateEngine()
    execution = state.get_execution("waiting")
    state.transition_execution(execution, "waiting_provider")
    executed = []

    async def execute(execution_id, task):
        executed.append((execution_id, task))

    scheduler = Scheduler()
    coordinator = ProviderRecoveryCoordinator(
        EventBus(), state, MockLLMProvider(), execute, max_attempts=2,
        scheduler=scheduler,
    )
    coordinator.schedule("waiting", delay_seconds=30)
    first = coordinator.jobs["waiting"]
    coordinator.schedule("waiting", delay_seconds=30)
    assert coordinator.jobs["waiting"] == first
    assert scheduler.has(first)
    await coordinator.stop()
    assert not scheduler.has(first)
    assert coordinator.jobs == {}
    assert executed == []
    await scheduler.stop(cancel_pending=True)


@pytest.mark.asyncio
async def test_resume_persisted_requeues_waiting_provider_executions():
    state = StateEngine()
    waiting = state.get_execution("waiting")
    state.transition_execution(waiting, "waiting_provider")
    running = state.get_execution("running")
    state.transition_execution(running, "running")
    scheduler = Scheduler()
    coordinator = ProviderRecoveryCoordinator(
        EventBus(), state, MockLLMProvider(), lambda *_args: None, max_attempts=2,
        scheduler=scheduler,
    )

    coordinator.resume_persisted()

    assert "waiting" in coordinator.jobs
    assert "running" not in coordinator.jobs
    assert scheduler.has(coordinator.jobs["waiting"])
    await coordinator.stop()
    await scheduler.stop(cancel_pending=True)
