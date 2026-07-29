"""Adaptive runtime sizing and stable tool-call identities."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Dict


def tool_call_fingerprint(capability: str, action: str, arguments: Dict[str, Any]) -> str:
    """Return an ID that is stable across model-generated tool-call IDs."""
    payload = json.dumps(
        {
            "capability": str(capability).strip().lower(),
            "action": str(action).strip().lower(),
            "arguments": arguments if isinstance(arguments, dict) else {},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class AdaptiveRuntimePolicy:
    """Scale execution budgets from the actual step contract.

    The configured values are baselines rather than ceilings when adaptive
    sizing is enabled. Real progress may continue indefinitely; stagnation
    and repeated rejected actions remain bounded.
    """

    baseline_stagnation_iterations: int = 30
    baseline_retries: int = 2
    adaptive: bool = True

    @staticmethod
    def _step_weight(task: str, step: Dict[str, Any]) -> int:
        fields = (
            "dependencies",
            "expertise",
            "required_artifacts",
            "acceptance_criteria",
            "verification_commands",
            "requirement_ids",
            "owned_paths",
        )
        contract_items = sum(
            len(step.get(field) or []) for field in fields
            if isinstance(step.get(field) or [], list)
        )
        text_size = len(str(task or "")) + len(str(step.get("description") or ""))
        return max(1, 1 + contract_items + math.ceil(text_size / 1_500))

    def stagnation_budget(self, task: str, step: Dict[str, Any]) -> int:
        baseline = max(1, int(self.baseline_stagnation_iterations))
        if not self.adaptive:
            return baseline
        weight = self._step_weight(task, step)
        return baseline + math.ceil(math.sqrt(weight) * max(2, baseline / 5))

    def retry_budget(self, task: str, step: Dict[str, Any]) -> int:
        baseline = max(0, int(self.baseline_retries))
        if not self.adaptive:
            return baseline
        weight = self._step_weight(task, step)
        return max(baseline, math.ceil(math.log2(weight + 1)))

    @staticmethod
    def context_budget(baseline: int, task: str, plan: Dict[str, Any] | None) -> int:
        """Use an explicit value as a floor and grow for larger contracts."""
        floor = max(1, int(baseline))
        steps = (plan or {}).get("steps") if isinstance(plan, dict) else []
        step_count = len(steps) if isinstance(steps, list) else 0
        requirement_count = len((plan or {}).get("requirements") or []) if isinstance(plan, dict) else 0
        contract_chars = len(str(task or "")) + step_count * 500 + requirement_count * 250
        return max(floor, floor + contract_chars)
