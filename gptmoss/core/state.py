import asyncio
import json
import logging
import os
import re
import threading
import time
import warnings
from contextlib import suppress
from enum import Enum
from pathlib import Path
from typing import Dict, Any, Optional, List

from pydantic import BaseModel, ConfigDict, Field

from gptmoss.core.durable_io import unlink_resilient, write_text_atomic


logger = logging.getLogger("gptmoss.state")
STATE_SCHEMA_VERSION = 2
DEFAULT_MAX_TRANSITIONS_PER_EXECUTION = 2_000
TRANSIENT_FLUSH_EVENT_TYPES = frozenset({"LLMDelta"})
_SAFE_STATE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


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
        self._persisted_execution_ids: set[str] = set()
        self._persisted_conversation_ids: set[str] = set()
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
            if version == 1:
                version = 2
                migrated["schema_version"] = version
                continue
            raise ValueError(f"No state migration is available from schema {version}.")
        return migrated

    def _state_root(self) -> Optional[Path]:
        if not self.persist_path:
            return None
        return Path(self.persist_path).resolve().parent

    def _partition_dir(self, name: str) -> Optional[Path]:
        root = self._state_root()
        return None if root is None else root / name

    @staticmethod
    def _sidecar_filename(key: str) -> str:
        text = str(key or "").strip()
        if _SAFE_STATE_ID.fullmatch(text):
            return f"{text}.json"
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", text)[:128]
        return f"{safe or 'unnamed'}.json"

    @staticmethod
    def _has_embedded_records(records: Any) -> bool:
        if not isinstance(records, dict) or not records:
            return False
        sample = next(iter(records.values()))
        return isinstance(sample, dict) and (
            "status" in sample or "variables" in sample or "messages" in sample
            or "execution_id" in sample
        )

    def _load_sidecar_records(self, directory: Optional[Path], keys: List[str]) -> Dict[str, Dict[str, Any]]:
        loaded: Dict[str, Dict[str, Any]] = {}
        if directory is None:
            return loaded
        for key in keys:
            path = directory / self._sidecar_filename(key)
            if not path.is_file():
                logger.warning("Missing state sidecar for %s at %s", key, path)
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as error:
                logger.error("Failed to load state sidecar %s: %s", path, error)
                continue
            if isinstance(payload, dict):
                loaded[key] = payload
        return loaded

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

            conversations = data.get("conversations") or {}
            executions = data.get("executions") or {}
            if int(data.get("schema_version", 0)) >= 2 and not (
                self._has_embedded_records(executions) or self._has_embedded_records(conversations)
            ):
                execution_ids = [
                    str(item) for item in (data.get("execution_ids") or list(executions))
                ]
                conversation_ids = [
                    str(item) for item in (data.get("conversation_ids") or list(conversations))
                ]
                executions = self._load_sidecar_records(
                    self._partition_dir("state_executions"), execution_ids
                )
                conversations = self._load_sidecar_records(
                    self._partition_dir("state_conversations"), conversation_ids
                )

            # Load conversations
            for k, v in conversations.items():
                self.conversations[k] = ConversationState(**v)
                
            # Load executions
            for k, v in executions.items():
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
            self._persisted_execution_ids = set(self.executions)
            self._persisted_conversation_ids = set(self.conversations)
        except Exception as e:
            logger.error(f"Failed to load state from disk: {e}")
            self._quarantine_invalid_snapshot()

    def _index_snapshot(self) -> Dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "execution_ids": sorted(self.executions),
            "conversation_ids": sorted(self.conversations),
            "agents": {k: v.model_dump() for k, v in self.agents.items()},
            "workspaces": {k: v.model_dump() for k, v in self.workspaces.items()},
            "knowledges": {k: v.model_dump() for k, v in self.knowledges.items()},
            "users": {k: v.model_dump() for k, v in self.users.items()},
        }

    def _snapshot(self) -> Dict[str, Any]:
        """Compatibility snapshot used by tests that inspect in-memory persistence shape."""
        snapshot = self._index_snapshot()
        snapshot["conversations"] = {k: v.model_dump() for k, v in self.conversations.items()}
        snapshot["executions"] = {k: v.model_dump() for k, v in self.executions.items()}
        return snapshot

    def _write_sidecars(
        self, directory: Path, records: Dict[str, Any], previous_ids: set[str]
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        current_names = set()
        for key, value in records.items():
            filename = self._sidecar_filename(key)
            current_names.add(filename)
            payload = value.model_dump() if hasattr(value, "model_dump") else dict(value)
            write_text_atomic(directory / filename, json.dumps(payload, indent=2, ensure_ascii=False))
        for stale in previous_ids - set(records):
            unlink_resilient(directory / self._sidecar_filename(stale))

    def save_to_disk(self) -> bool:
        """Atomically persist sidecars first, then the compact index."""
        if not self.persist_path:
            return True

        with self._save_lock:
            try:
                executions_dir = self._partition_dir("state_executions")
                conversations_dir = self._partition_dir("state_conversations")
                if executions_dir is not None:
                    self._write_sidecars(
                        executions_dir, self.executions, self._persisted_execution_ids
                    )
                if conversations_dir is not None:
                    self._write_sidecars(
                        conversations_dir, self.conversations, self._persisted_conversation_ids
                    )
                content = json.dumps(self._index_snapshot(), indent=2, ensure_ascii=False)
                write_text_atomic(self.persist_path, content)
                self._persisted_execution_ids = set(self.executions)
                self._persisted_conversation_ids = set(self.conversations)
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
            if event.type in TRANSIENT_FLUSH_EVENT_TYPES:
                return
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
