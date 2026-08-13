"""Durable provider retry and process-restart recovery for executions."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict

from gptmoss.core.event_bus import Event, EventBus
from gptmoss.core.state import StateEngine
from gptmoss.core.scheduler import Scheduler
from gptmoss.interfaces.llm import LLMProvider


class ProviderUnavailableError(RuntimeError):
    def __init__(self, message: str, original_error: Exception):
        super().__init__(message)
        self.original_error = original_error


class ProviderConfigurationError(RuntimeError):
    def __init__(self, original_error: Exception):
        super().__init__(
            "Authentification LLM refusée (HTTP 401/403). Ouvrez Paramètres, "
            "corrigez la clé API, utilisez Tester la connexion, puis reprenez "
            "l'exécution parente."
        )
        self.original_error = original_error


class ProviderRecoveryCoordinator:
    """Own provider classification, retry timing, and durable resumes."""

    def __init__(self, event_bus: EventBus, state_engine: StateEngine,
                 llm_provider: LLMProvider,
                 execute: Callable[[str, str], Awaitable[Any]], max_attempts: int,
                 scheduler: Scheduler):
        self.event_bus = event_bus
        self.state_engine = state_engine
        self.llm_provider = llm_provider
        self.execute = execute
        self.max_attempts = max(1, int(max_attempts))
        self.scheduler = scheduler
        self.jobs: Dict[str, str] = {}

    @staticmethod
    def is_permanent(error: Exception) -> bool:
        text = (error.__class__.__name__ + " " + str(error)).lower()
        return any(marker in text for marker in (
            "authentication", "permissiondenied", "invalid api key", "401", "403",
        ))

    @classmethod
    def is_transient(cls, error: Exception) -> bool:
        text = (error.__class__.__name__ + " " + str(error)).lower()
        markers = (
            "connection", "timeout", "timed out", "ratelimit", "rate limit", "429",
            "internalserver", "server error", "502", "503", "504", "temporar", "unavailable",
        )
        return not cls.is_permanent(error) and any(marker in text for marker in markers)

    async def completion(self, execution_id: str, **kwargs) -> Dict[str, Any]:
        consecutive_errors = 0
        while True:
            try:
                return await self.llm_provider.completion(**kwargs)
            except Exception as error:
                if self.is_permanent(error):
                    raise ProviderConfigurationError(error) from error
                if not self.is_transient(error):
                    raise
                if consecutive_errors >= min(4, self.max_attempts):
                    raise ProviderUnavailableError(
                        "LLM provider is temporarily unavailable; execution state was preserved.", error
                    ) from error
                consecutive_errors += 1
                delay = min(30, 2 ** min(consecutive_errors - 1, 5))
                await self.event_bus.publish(Event(type="LLMRetryScheduled", payload={
                    "execution_id": execution_id, "attempt": consecutive_errors,
                    "delay_seconds": delay, "error_type": error.__class__.__name__,
                }))
                await self.scheduler.wait(
                    delay, job_id=f"provider-delay:{execution_id}:{consecutive_errors}"
                )

    def schedule(self, execution_id: str, delay_seconds: int = 30) -> None:
        job_id = f"provider-resume:{execution_id}"
        if self.scheduler.has(job_id):
            return

        async def resume_later() -> None:
            self.jobs.pop(execution_id, None)
            state = self.state_engine.get_execution(execution_id)
            if state.status != "waiting_provider":
                return
            self.state_engine.transition_execution(
                state, "running", reason="provider retry", actor="provider recovery"
            )
            state.variables["provider_resume_attempts"] = int(
                state.variables.get("provider_resume_attempts", 0)
            ) + 1
            await self.event_bus.publish(Event(type="ExecutionProviderRetry", payload={
                "execution_id": execution_id,
                "attempt": state.variables["provider_resume_attempts"],
            }))
            # ExecutionEngine owns the returned task. If the provider remains
            # unavailable, execute_task schedules the next durable resume.
            self.execute(execution_id, str(state.variables.get("task") or ""))

        self.jobs[execution_id] = self.scheduler.schedule(
            resume_later,
            delay=max(1, min(int(delay_seconds), 300)),
            job_id=job_id,
            metadata={"kind": "provider-resume", "execution_id": execution_id},
        )
        self.scheduler.start()

    def resume_persisted(self) -> None:
        for execution_id, state in self.state_engine.executions.items():
            if state.status == "waiting_provider":
                self.schedule(execution_id, delay_seconds=1)

    def cancel(self, execution_id: str) -> bool:
        """Cancel the durable resume job owned by one execution."""
        job_id = self.jobs.pop(execution_id, None)
        return self.scheduler.cancel(job_id) if job_id else False

    async def stop(self) -> None:
        for job_id in list(self.jobs.values()):
            self.scheduler.cancel(job_id)
        self.jobs.clear()
