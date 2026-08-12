"""Delivery state traversal and independent assurance coordination."""

from typing import Any, Callable, Dict, List, Optional

from gptmoss.core.delivery import evaluate_delivery
from gptmoss.core.state import StateEngine


class DeliveryCoordinator:
    def __init__(self, state_engine: StateEngine, capability: Callable[[str], Any]):
        self.state_engine = state_engine
        self.capability = capability

    def histories(self, execution_id: str) -> List[Dict[str, Any]]:
        histories, queue, visited = [], [execution_id], set()
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            histories.extend(
                self.state_engine.get_execution(current).variables.get("tool_call_history", [])
            )
            queue.extend(
                child_id for child_id, child in self.state_engine.executions.items()
                if child.variables.get("parent_execution_id") == current
            )
        return histories

    def workspace(self, execution_id: str) -> Optional[str]:
        filesystem = self.capability("filesystem")
        if not filesystem or not hasattr(filesystem, "_get_workspace_for_execution"):
            return None
        try:
            return filesystem._get_workspace_for_execution(execution_id)
        except (OSError, PermissionError, ValueError):
            return None

    def assurance(self, execution_id: str, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        state = self.state_engine.get_execution(execution_id)
        contract = state.variables.get("delivery_contract")
        workspace = self.workspace(execution_id)
        if isinstance(contract, dict) and not contract.get("software_delivery") and not workspace:
            return {"schema_version": 1, "contract_sha256": contract.get("contract_sha256"),
                    "passed": True, "checks": [{"name": "direct_task_contract", "passed": True}],
                    "failures": []}
        if isinstance(contract, dict) and not workspace:
            has_artifacts = any(step.get("required_artifacts") for step in steps)
            has_commands = bool(contract.get("verification_commands") or contract.get("launch_commands"))
            if not has_artifacts and not has_commands:
                return {"schema_version": 1, "contract_sha256": contract.get("contract_sha256"),
                        "passed": True, "checks": [{"name": "scheduler_only_contract", "passed": True}],
                        "failures": []}
        if not isinstance(contract, dict) or not workspace:
            return {"schema_version": 1, "passed": False, "checks": [],
                    "failures": ["delivery contract or workspace is unavailable"]}
        return evaluate_delivery(workspace, contract, steps, self.histories(execution_id))
