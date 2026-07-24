"""Local, privacy-aware telemetry for agent executions."""

import json
import os
import time
from typing import Any, Dict, List, Optional


_SECRET_KEYS = {"api_key", "authorization", "token", "password", "secret"}


def _sanitize(value: Any, limit: int = 2_000) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key.lower() in _SECRET_KEYS else _sanitize(item, limit)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item, limit) for item in value]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"\\n… [truncated {len(value) - limit} chars]"
    return value


class TraceRecorder:
    """Records compact execution events and exposes aggregate runtime metrics."""

    def __init__(self, file_path: Optional[str] = None, max_events: int = 2_000):
        self.file_path = os.path.abspath(file_path) if file_path else None
        self.max_events = max_events
        self.events: List[Dict[str, Any]] = []
        if self.file_path:
            directory = os.path.dirname(self.file_path)
            if directory:
                os.makedirs(directory, exist_ok=True)

    def record(self, event_type: str, execution_id: str, **payload: Any) -> None:
        event = {
            "timestamp": time.time(),
            "event_type": event_type,
            "execution_id": execution_id,
            "payload": _sanitize(payload),
        }
        self.events.append(event)
        if len(self.events) > self.max_events:
            self.events.pop(0)
        if self.file_path:
            try:
                with open(self.file_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\\n")
            except OSError:
                # Telemetry must never interrupt the agent's primary work.
                pass

    def metrics(self, execution_id: Optional[str] = None) -> Dict[str, Any]:
        events = [event for event in self.events if not execution_id or event["execution_id"] == execution_id]
        counts: Dict[str, int] = {}
        for event in events:
            counts[event["event_type"]] = counts.get(event["event_type"], 0) + 1
        return {"events": len(events), "counts": counts}
