"""Small deterministic scheduler used for delayed runtime work and tests."""

from __future__ import annotations

import asyncio
import heapq
import inspect
import itertools
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


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


class Scheduler:
    """Schedule callables, run due work in order, and retry explicit failures."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._sequence = itertools.count()
        self._queue: list[ScheduledJob] = []
        self._jobs: Dict[str, ScheduledJob] = {}

    def schedule(self, callback: Callable[[], Any], *, delay: float = 0,
                 run_at: Optional[float] = None, max_retries: int = 0,
                 retry_delay: float = 1.0, job_id: Optional[str] = None) -> str:
        if not callable(callback):
            raise TypeError("Scheduled callback must be callable.")
        identifier = job_id or str(uuid.uuid4())
        if identifier in self._jobs:
            raise ValueError(f"Scheduled job already exists: {identifier}")
        due = float(run_at) if run_at is not None else self.clock() + max(0.0, float(delay))
        job = ScheduledJob(
            due, next(self._sequence), identifier, callback,
            max(0, int(max_retries)), max(0.0, float(retry_delay)),
        )
        self._jobs[identifier] = job
        heapq.heappush(self._queue, job)
        return identifier

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.pop(job_id, None)
        if not job:
            return False
        job.cancelled = True
        return True

    def pending(self) -> list[dict[str, Any]]:
        return [
            {"job_id": job.job_id, "run_at": job.run_at, "attempts": job.attempts}
            for job in sorted(self._queue)
            if not job.cancelled and job.job_id in self._jobs
        ]

    async def run_due(self, *, now: Optional[float] = None) -> list[str]:
        boundary = self.clock() if now is None else float(now)
        completed = []
        while self._queue and self._queue[0].run_at <= boundary:
            job = heapq.heappop(self._queue)
            if job.cancelled or job.job_id not in self._jobs:
                continue
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
                    continue
                self._jobs.pop(job.job_id, None)
                raise
            self._jobs.pop(job.job_id, None)
            completed.append(job.job_id)
        return completed

    async def serve(self, *, poll_interval: float = 0.25) -> None:
        while True:
            await self.run_due()
            await asyncio.sleep(max(0.01, float(poll_interval)))
