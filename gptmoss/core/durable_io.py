"""Small, dependency-free helpers for reliable writes on local and UNC storage."""

from __future__ import annotations

import os
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Callable, TypeVar


_T = TypeVar("_T")
IO_RETRY_DELAYS = (0.05, 0.10, 0.25, 0.50, 1.00)


def _retry(operation: Callable[[], _T]) -> _T:
    """Retry transient filesystem failures without hiding a persistent error."""
    for attempt, delay in enumerate((*IO_RETRY_DELAYS, 0.0)):
        try:
            return operation()
        except OSError:
            if attempt >= len(IO_RETRY_DELAYS):
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def _write_once(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)


def _replace_once(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def write_bytes_atomic(path: str | Path, payload: bytes) -> None:
    """Write a complete file and atomically publish it beside its destination."""
    destination = Path(path)
    _retry(lambda: destination.parent.mkdir(parents=True, exist_ok=True))
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        _retry(lambda: _write_once(temporary, payload))
        _retry(lambda: _replace_once(temporary, destination))
    finally:
        with suppress(OSError):
            unlink_resilient(temporary)


def write_text_atomic(path: str | Path, content: str, *, encoding: str = "utf-8") -> None:
    write_bytes_atomic(path, content.encode(encoding))


def unlink_resilient(path: str | Path) -> None:
    target = Path(path)

    def remove() -> None:
        target.unlink(missing_ok=True)

    _retry(remove)
