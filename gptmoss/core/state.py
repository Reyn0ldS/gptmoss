import asyncio
import json
import logging
import os
import threading
import time
import warnings
from contextlib import suppress
from enum import Enum
from typing import Dict, Any, Optional, List

from pydantic import BaseModel, ConfigDict, Field

from gptmoss.core.durable_io import write_text_atomic


logger = logging.getLogger("gptmoss.state")
STATE_SCHEMA_VERSION = 1
DEFAULT_MAX_TRANSITIONS_PER_EXECUTION = 2_000


class ExecutionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_PROVIDER = "waiting_provider"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"

    def __str__(self) -> str:
        """Preserve the historical string contract across Python versions."""
        return self.value


class InvalidExecutionTransition(ValueError):
    pass


class ExecutionTransition(BaseModel):
    previous_status: ExecutionStatus
    status: ExecutionStatus
    reason: str = "runtime"
    actor: str = "runtime"
    correlation_id: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)


ALLOWED_EXECUTION_TRANSITIONS = {
    ExecutionStatus.PENDING: {
        ExecutionStatus.RUNNING, ExecutionStatus.PAUSED,
        ExecutionStatus.WAITING_PROVIDER, ExecutionStatus.CANCELLED,
        ExecutionStatus.COMPLETED, ExecutionStatus.FAILED,
    },
    ExecutionStatus.RUNNING: {
        ExecutionStatus.PENDING, ExecutionStatus.PAUSED,
        ExecutionStatus.WAITING_PROVIDER, ExecutionStatus.CANCELLED,
        ExecutionStatus.COMPLETED, ExecutionStatus.FAILED,
    },
    ExecutionStatus.PAUSED: {
        ExecutionStatus.RUNNING, ExecutionStatus.WAITING_PROVIDER,
        ExecutionStatus.CANCELLED, ExecutionStatus.FAILED,
    },
    ExecutionStatus.WAITING_PROVIDER: {
        ExecutionStatus.PENDING, ExecutionStatus.RUNNING,
        ExecutionStatus.PAUSED, ExecutionStatus.CANCELLED,
        ExecutionStatus.FAILED,
    },
    # A failed top-level execution may be explicitly resumed after correction.
    ExecutionStatus.FAILED: {
        ExecutionStatus.PENDING, ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.CANCELLED: set(),
    ExecutionStatus.COMPLETED: set(),
}

class ConversationState(BaseModel):
    messages: List[Dict[str, Any]] = Field(default_factory=list)

class ExecutionState(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    execution_id: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    current_step: Optional[int] = None
    current_plan: Optional[Dict[str, Any]] = None
    variables: Dict[str, Any] = Field(default_factory=dict)
    results: Dict[str, Any] = Field(default_factory=dict)
    transitions: List[ExecutionTransition] = Field(default_factory=list)

class AgentState(BaseModel):
    agent_id: str
    config: Dict[str, Any] = Field(default_factory=dict)

class WorkspaceState(BaseModel):
    cwd: str = "."
    files_tracked: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)

class KnowledgeState(BaseModel):
    facts: List[str] = Field(default_factory=list)
    rules: List[str] = Field(default_factory=list)

class UserState(BaseModel):
    user_id: str
    preferences: Dict[str, Any] = Field(default_factory=dict)

class StateEngine:
    """
    State Engine to manage partitioned states across various boundaries.
    """
    def __init__(self, persist_path: Optional[str] = None,
                 max_transitions_per_execution: int = DEFAULT_MAX_TRANSITIONS_PER_EXECUTION):
        self.persist_path = persist_path
        self.conversations: Dict[str, ConversationState] = {}
        self.executions: Dict[str, ExecutionState] = {}
        self.agents: Dict[str, AgentState] = {}
        self.workspaces: Dict[str, WorkspaceState] = {}
        self.knowledges: Dict[str, KnowledgeState] = {}
        self.users: Dict[str, UserState] = {}
        self._save_lock = threading.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._flush_event_bus = None
        self._flush_callback = None
        self.max_transitions_per_execution = max(100, int(max_transitions_per_execution))
        self.corrupt_backup_path: Optional[str] = None
        self._load_from_disk()

    @staticmethod
    def _migrate_snapshot(data: Dict[str, Any]) -> Dict[str, Any]:
        """Upgrade known legacy snapshots and refuse unknown future schemas."""
        version = int(data.get("schema_version", 0))
        if version > STATE_SCHEMA_VERSION:
            raise ValueError(
                f"State schema {version} is newer than supported schema {STATE_SCHEMA_VERSION}."
            )
        migrated = dict(data)
        while version < STATE_SCHEMA_VERSION:
            if version == 0:
                migrated.setdefault("conversations", {})
                migrated.setdefault("executions", {})
                migrated.setdefault("agents", {})
                migrated.setdefault("workspaces", {})
                migrated.setdefault("knowledges", {})
                migrated.setdefault("users", {})
                version = 1
                migrated["schema_version"] = version
                continue
            raise ValueError(f"No state migration is available from schema {version}.")
        return migrated

    def _quarantine_invalid_snapshot(self) -> None:
        if not self.persist_path or not os.path.exists(self.persist_path):
            return
        backup = f"{self.persist_path}.corrupt-{int(time.time() * 1000)}"
        try:
            os.replace(self.persist_path, backup)
            self.corrupt_backup_path = backup
            logger.error("Invalid state snapshot quarantined at %s", backup)
        except OSError as error:
            logger.error("Unable to quarantine invalid state snapshot: %s", error)

    def _load_from_disk(self):
        if not self.persist_path or not os.path.exists(self.persist_path):
            return
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                data = self._migrate_snapshot(json.load(f))
                
            # Load conversations
            for k, v in data.get("conversations", {}).items():
                self.conversations[k] = ConversationState(**v)
                
            # Load executions
            for k, v in data.get("executions", {}).items():
                self.executions[k] = ExecutionState(**v)
                
            # Load agents
            for k, v in data.get("agents", {}).items():
                self.agents[k] = AgentState(**v)
                
            # Load workspaces
            for k, v in data.get("workspaces", {}).items():
                self.workspaces[k] = WorkspaceState(**v)
                
            # Load knowledges
            for k, v in data.get("knowledges", {}).items():
                self.knowledges[k] = KnowledgeState(**v)
                
            # Load users
            for k, v in data.get("users", {}).items():
                self.users[k] = UserState(**v)
        except Exception as e:
            logger.error(f"Failed to load state from disk: {e}")
            self._quarantine_invalid_snapshot()

    def _snapshot(self) -> Dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "conversations": {k: v.model_dump() for k, v in self.conversations.items()},
            "executions": {k: v.model_dump() for k, v in self.executions.items()},
            "agents": {k: v.model_dump() for k, v in self.agents.items()},
            "workspaces": {k: v.model_dump() for k, v in self.workspaces.items()},
            "knowledges": {k: v.model_dump() for k, v in self.knowledges.items()},
            "users": {k: v.model_dump() for k, v in self.users.items()},
        }

    def save_to_disk(self) -> bool:
        """Atomically persist a complete snapshot, preserving the previous file on failure."""
        if not self.persist_path:
            return True

        with self._save_lock:
            try:
                content = json.dumps(self._snapshot(), indent=2, ensure_ascii=False)
                write_text_atomic(self.persist_path, content)
                return True
            except Exception as e:
                logger.error(f"Failed to save state to disk: {e}")
                return False

    def get_conversation(self, convo_id: str) -> ConversationState:
        if convo_id not in self.conversations:
            self.conversations[convo_id] = ConversationState()
            self.save_to_disk()
        return self.conversations[convo_id]

    def get_execution(self, exec_id: str) -> ExecutionState:
        if exec_id not in self.executions:
            self.executions[exec_id] = ExecutionState(execution_id=exec_id)
            self.save_to_disk()
        return self.executions[exec_id]

    def transition_execution(
        self,
        execution: str | ExecutionState,
        status: str | ExecutionStatus,
        *,
        reason: str = "runtime",
        actor: str = "runtime",
        correlation_id: Optional[str] = None,
    ) -> ExecutionState:
        """Apply and audit one valid execution lifecycle transition."""
        state = self.get_execution(execution) if isinstance(execution, str) else execution
        previous = ExecutionStatus(state.status)
        target = ExecutionStatus(status)
        if previous == target:
            return state
        if target not in ALLOWED_EXECUTION_TRANSITIONS[previous]:
            raise InvalidExecutionTransition(
                f"Execution {state.execution_id} cannot transition from "
                f"{previous.value} to {target.value}."
            )
        state.status = target
        state.transitions.append(ExecutionTransition(
            previous_status=previous,
            status=target,
            reason=reason,
            actor=actor,
            correlation_id=correlation_id,
        ))
        overflow = len(state.transitions) - self.max_transitions_per_execution
        if overflow > 0:
            del state.transitions[:overflow]
        return state

    def get_agent(self, agent_id: str) -> AgentState:
        if agent_id not in self.agents:
            self.agents[agent_id] = AgentState(agent_id=agent_id)
            self.save_to_disk()
        return self.agents[agent_id]

    def get_workspace(self, workspace_id: str) -> WorkspaceState:
        warnings.warn(
            "WorkspaceState is a legacy compatibility partition; use project configuration.",
            DeprecationWarning, stacklevel=2,
        )
        if workspace_id not in self.workspaces:
            self.workspaces[workspace_id] = WorkspaceState()
            self.save_to_disk()
        return self.workspaces[workspace_id]

    def get_knowledge(self, knowledge_id: str) -> KnowledgeState:
        warnings.warn(
            "KnowledgeState is a legacy compatibility partition; use governed memory.",
            DeprecationWarning, stacklevel=2,
        )
        if knowledge_id not in self.knowledges:
            self.knowledges[knowledge_id] = KnowledgeState()
            self.save_to_disk()
        return self.knowledges[knowledge_id]

    def get_user(self, user_id: str) -> UserState:
        warnings.warn(
            "UserState is a legacy compatibility partition; use scoped governed memory.",
            DeprecationWarning, stacklevel=2,
        )
        if user_id not in self.users:
            self.users[user_id] = UserState(user_id=user_id)
            self.save_to_disk()
        return self.users[user_id]

    def start_db_flush_loop(self, event_bus):
        """Starts a debounced background state saving loop."""
        from gptmoss.core.event_bus import Event

        if self._flush_task and not self._flush_task.done():
            return self._flush_task
        if self._flush_event_bus and self._flush_callback:
            self._flush_event_bus.unsubscribe_all(self._flush_callback)

        state_changed = asyncio.Event()

        async def save_state_on_event(event: Event):
            state_changed.set()

        event_bus.subscribe_all(save_state_on_event)

        async def db_flush_loop():
            while True:
                try:
                    await state_changed.wait()
                    state_changed.clear()
                    await asyncio.sleep(1.0)
                    await asyncio.to_thread(self.save_to_disk)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error in db_flush_loop: {e}")
                    await asyncio.sleep(1.0)

        self._flush_event_bus = event_bus
        self._flush_callback = save_state_on_event
        self._flush_task = asyncio.create_task(db_flush_loop())
        return self._flush_task

    async def stop_db_flush_loop(self, *, flush: bool = True) -> None:
        """Detach the persistence callback, flush once, and stop the worker."""
        if self._flush_event_bus and self._flush_callback:
            self._flush_event_bus.unsubscribe_all(self._flush_callback)
        self._flush_event_bus = None
        self._flush_callback = None

        if flush:
            await asyncio.to_thread(self.save_to_disk)

        task = self._flush_task
        self._flush_task = None
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
