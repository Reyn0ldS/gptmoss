"""Human scope and capability approval lifecycle coordination."""

import time
from typing import Awaitable, Callable, Optional

from gptmoss.core.adaptive import tool_call_fingerprint
from gptmoss.core.event_bus import Event, EventBus
from gptmoss.core.state import StateEngine


class ApprovalCoordinator:
    def __init__(self, state_engine: StateEngine, event_bus: EventBus,
                 execute: Callable[[str, str], Awaitable[object]]):
        self.state_engine = state_engine
        self.event_bus = event_bus
        self.execute = execute

    def _resume(self, execution_id: str, task: str) -> None:
        # ExecutionEngine.start_execution owns and de-duplicates this task.
        self.execute(execution_id, task)

    async def resolve_scope(self, execution_id: str, decision: str,
                            reason: Optional[str] = None) -> None:
        state = self.state_engine.get_execution(execution_id)
        pending = state.variables.get("pending_scope_approval")
        if state.status != "paused" or not isinstance(pending, dict):
            raise ValueError(f"Execution {execution_id} has no pending scope approval.")
        decision_record = {
            "contract_sha256": pending.get("contract_sha256"), "decision": decision,
            "reason": reason or "", "decided_at": time.time(),
        }
        if pending.get("scope_changes_sha256"):
            decision_record["scope_changes_sha256"] = pending.get("scope_changes_sha256")
        state.variables.setdefault("scope_decisions", []).append(decision_record)
        state.variables.pop("pending_scope_approval", None)
        if decision != "allow":
            self.state_engine.transition_execution(
                state, "failed", reason="scope reduction rejected", actor="user"
            )
            state.results["error"] = "Proposed scope reduction was rejected by the user."
            self.state_engine.save_to_disk()
            await self.event_bus.publish(Event(type="ExecutionFailed", payload={
                "execution_id": execution_id, "error": state.results["error"],
            }))
            return
        state.variables["approved_scope_contract_sha256"] = pending.get("contract_sha256")
        if pending.get("scope_changes_sha256"):
            state.variables["approved_scope_changes_sha256"] = pending.get(
                "scope_changes_sha256"
            )
        self.state_engine.transition_execution(
            state, "running", reason="scope reduction approved", actor="user"
        )
        self.state_engine.save_to_disk()
        await self.event_bus.publish(Event(type="ScopeApproved", payload={
            "execution_id": execution_id, "reason": reason or "",
        }))
        self._resume(execution_id, str(state.variables.get("task") or ""))

    async def resolve_capability(self, execution_id: str, decision: str,
                                 reason: Optional[str] = None) -> None:
        state = self.state_engine.get_execution(execution_id)
        if state.status != "paused":
            raise ValueError(f"Execution {execution_id} is not paused.")
        pending = state.variables.get("pending_approval")
        if not pending:
            raise ValueError(f"No pending approval found for execution {execution_id}.")

        child_id = pending.get("child_execution_id")
        if child_id:
            child = self.state_engine.get_execution(child_id)
            child_pending = child.variables.get("pending_approval")
            if child.status != "paused" or not isinstance(child_pending, dict):
                state.variables.pop("pending_approval", None)
                self.state_engine.transition_execution(
                    state, "running", reason="stale child approval cleared", actor="runtime"
                )
                self.state_engine.save_to_disk()
                await self.event_bus.publish(Event(type="StaleDelegatedApprovalCleared", payload={
                    "execution_id": execution_id, "child_execution_id": child_id,
                }))
                self._resume(execution_id, str(state.variables.get("task") or ""))
                return
            child_pending.update({"decision": decision, "reason": reason or ""})
            fingerprint = child_pending.get("fingerprint") or tool_call_fingerprint(
                child_pending["capability"], child_pending["action"], child_pending["arguments"]
            )
            child.variables.setdefault("approval_decisions", {})[fingerprint] = decision
            self.state_engine.transition_execution(
                child, "running", reason="delegated approval resolved", actor="user"
            )
            state.variables.pop("pending_approval", None)
            self.state_engine.transition_execution(
                state, "running", reason="delegated approval resolved", actor="user"
            )
            self.state_engine.save_to_disk()
            await self.event_bus.publish(Event(type="ExecutionResumed", payload={
                "execution_id": execution_id, "child_execution_id": child_id,
                "decision": decision,
            }))
            self._resume(child_id, str(child.variables.get("task") or ""))
            self._resume(execution_id, str(state.variables.get("task") or ""))
            return

        pending.update({"decision": decision, "reason": reason or ""})
        self.state_engine.transition_execution(
            state, "running", reason="approval resolved", actor="user"
        )
        parent_id = state.variables.get("parent_execution_id")
        parent = self.state_engine.executions.get(parent_id) if parent_id else None
        if parent is not None:
            parent_pending = parent.variables.get("pending_approval")
            if isinstance(parent_pending, dict) and parent_pending.get("child_execution_id") == execution_id:
                parent.variables.pop("pending_approval", None)
                if parent.status == "paused":
                    self.state_engine.transition_execution(
                        parent, "running", reason="child approval resolved", actor="user"
                    )
                self._resume(parent_id, str(parent.variables.get("task") or ""))
        self.state_engine.save_to_disk()
        await self.event_bus.publish(Event(type="ExecutionResumed", payload={
            "execution_id": execution_id, "decision": decision,
        }))
        task = state.variables.get("task") or self.state_engine.get_conversation(
            execution_id
        ).messages[0]["content"]
        self._resume(execution_id, task[6:] if task.startswith("Task: ") else task)
