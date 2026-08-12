"""Small deterministic scheduler used for delayed runtime work and tests."""

from __future__ import annotations

import asyncio
import heapq
import inspect
import itertools
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


logger = logging.getLogger("gptmoss.scheduler")


@dataclass(order=True)
class ScheduledJob:
    run_at: float
    sequence: int
    job_id: str = field(compare=False)
    callback: Callable[[], Any] = field(compare=False, repr=False)
    max_retries: int = field(default=0, compare=False)
    retry_delay: float = field(default=1.0, compare=False)
    attempts: int = field(default=0, compare=False)
    cancelled: bool = field(default=False, compare=False)
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False)


class Scheduler:
    """Schedule callables, run due work in order, and retry explicit failures."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._sequence = itertools.count()
        self._queue: list[ScheduledJob] = []
        self._jobs: Dict[str, ScheduledJob] = {}
        self._service_task: Optional[asyncio.Task] = None
        self._wakeup: Optional[asyncio.Event] = None

    def schedule(self, callback: Callable[[], Any], *, delay: float = 0,
                 run_at: Optional[float] = None, max_retries: int = 0,
                 retry_delay: float = 1.0, job_id: Optional[str] = None,
                 metadata: Optional[Dict[str, Any]] = None) -> str:
        if not callable(callback):
            raise TypeError("Scheduled callback must be callable.")
        identifier = job_id or str(uuid.uuid4())
        if identifier in self._jobs:
            raise ValueError(f"Scheduled job already exists: {identifier}")
        due = float(run_at) if run_at is not None else self.clock() + max(0.0, float(delay))
        job = ScheduledJob(
            due, next(self._sequence), identifier, callback,
            max(0, int(max_retries)), max(0.0, float(retry_delay)),
            metadata=dict(metadata or {}),
        )
        self._jobs[identifier] = job
        heapq.heappush(self._queue, job)
        if self._wakeup:
            self._wakeup.set()
        return identifier

    def has(self, job_id: str) -> bool:
        return job_id in self._jobs and not self._jobs[job_id].cancelled

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.pop(job_id, None)
        if not job:
            return False
        job.cancelled = True
        if self._wakeup:
            self._wakeup.set()
        return True

    def pending(self) -> list[dict[str, Any]]:
        return [
            {"job_id": job.job_id, "run_at": job.run_at, "attempts": job.attempts,
             "metadata": dict(job.metadata)}
            for job in sorted(self._queue)
            if not job.cancelled and job.job_id in self._jobs
        ]

    async def run_due(self, *, now: Optional[float] = None,
                      raise_errors: bool = True) -> list[str]:
        boundary = self.clock() if now is None else float(now)
        completed = []
        while self._queue and self._queue[0].run_at <= boundary:
            job = heapq.heappop(self._queue)
            if job.cancelled or job.job_id not in self._jobs:
                continue
            self._jobs.pop(job.job_id, None)
            job.attempts += 1
            try:
                result = job.callback()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                if job.attempts <= job.max_retries:
                    job.run_at = boundary + job.retry_delay
                    job.sequence = next(self._sequence)
                    heapq.heappush(self._queue, job)
                    self._jobs[job.job_id] = job
                    continue
                logger.exception("Scheduled job failed permanently: %s", job.job_id)
                if raise_errors:
                    raise
                continue
            completed.append(job.job_id)
        return completed

    def start(self) -> asyncio.Task:
        """Start the single timing service, idempotently."""
        if self._service_task and not self._service_task.done():
            return self._service_task
        self._wakeup = asyncio.Event()
        self._service_task = asyncio.create_task(self.serve())
        return self._service_task

    async def wait(self, delay: float, *, job_id: Optional[str] = None) -> None:
        """Wait through the scheduler instead of creating an independent timer."""
        loop = asyncio.get_running_loop()
        completed = loop.create_future()
        identifier = job_id or f"delay:{uuid.uuid4()}"
        self.schedule(lambda: completed.set_result(None), delay=delay, job_id=identifier,
                      metadata={"kind": "delay"})
        self.start()
        try:
            await completed
        finally:
            if not completed.done():
                completed.cancel()
            self.cancel(identifier)

    async def serve(self, *, poll_interval: float = 0.25) -> None:
        interval = max(0.01, float(poll_interval))
        if self._wakeup is None:
            self._wakeup = asyncio.Event()
        while True:
            self._wakeup.clear()
            await self.run_due(raise_errors=False)
            timeout = interval
            if self._queue:
                timeout = max(0.0, min(interval, self._queue[0].run_at - self.clock()))
            if self._wakeup.is_set():
                continue
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def stop(self, *, cancel_pending: bool = False) -> None:
        task = self._service_task
        self._service_task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if cancel_pending:
            for job in self._jobs.values():
                job.cancelled = True
            self._jobs.clear()
            self._queue.clear()
        self._wakeup = None
