from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field

class ConversationState(BaseModel):
    messages: List[Dict[str, Any]] = Field(default_factory=list)

class ExecutionState(BaseModel):
    execution_id: str
    status: str = "pending"  # pending, running, paused, waiting_provider, cancelled, completed, failed
    current_step: Optional[int] = None
    current_plan: Optional[Dict[str, Any]] = None
    variables: Dict[str, Any] = Field(default_factory=dict)
    results: Dict[str, Any] = Field(default_factory=dict)

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
    def __init__(self, persist_path: Optional[str] = None):
        self.persist_path = persist_path
        self.conversations: Dict[str, ConversationState] = {}
        self.executions: Dict[str, ExecutionState] = {}
        self.agents: Dict[str, AgentState] = {}
        self.workspaces: Dict[str, WorkspaceState] = {}
        self.knowledges: Dict[str, KnowledgeState] = {}
        self.users: Dict[str, UserState] = {}
        self._load_from_disk()

    def _load_from_disk(self):
        import json
        import os
        if not self.persist_path or not os.path.exists(self.persist_path):
            return
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
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
            import logging
            logging.getLogger("gptmoss.state").error(f"Failed to load state from disk: {e}")

    def save_to_disk(self):
        import json
        import os
        import threading
        import time
        if not hasattr(self, "_save_lock"):
            self._save_lock = threading.Lock()
            
        if not self.persist_path:
            return
            
        with self._save_lock:
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    persist_dir = os.path.dirname(self.persist_path)
                    if persist_dir:
                        os.makedirs(persist_dir, exist_ok=True)
                    data = {
                        "conversations": {k: v.model_dump() for k, v in self.conversations.items()},
                        "executions": {k: v.model_dump() for k, v in self.executions.items()},
                        "agents": {k: v.model_dump() for k, v in self.agents.items()},
                        "workspaces": {k: v.model_dump() for k, v in self.workspaces.items()},
                        "knowledges": {k: v.model_dump() for k, v in self.knowledges.items()},
                        "users": {k: v.model_dump() for k, v in self.users.items()}
                    }
                    # Write to a temp file first then rename to prevent corruption
                    temp_path = self.persist_path + ".tmp"
                    with open(temp_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                    
                    # Windows safe replacement fallback
                    if os.path.exists(self.persist_path):
                        try:
                            os.remove(self.persist_path)
                        except Exception:
                            pass
                    os.replace(temp_path, self.persist_path)
                    break # Success!
                except PermissionError as e:
                    if attempt == max_retries - 1:
                        import logging
                        logging.getLogger("gptmoss.state").error(f"Failed to save state to disk after {max_retries} attempts: {e}")
                    else:
                        time.sleep(0.05 * (attempt + 1))
                except Exception as e:
                    import logging
                    logging.getLogger("gptmoss.state").error(f"Failed to save state to disk: {e}")
                    break

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

    def get_agent(self, agent_id: str) -> AgentState:
        if agent_id not in self.agents:
            self.agents[agent_id] = AgentState(agent_id=agent_id)
            self.save_to_disk()
        return self.agents[agent_id]

    def get_workspace(self, workspace_id: str) -> WorkspaceState:
        if workspace_id not in self.workspaces:
            self.workspaces[workspace_id] = WorkspaceState()
            self.save_to_disk()
        return self.workspaces[workspace_id]

    def get_knowledge(self, knowledge_id: str) -> KnowledgeState:
        if knowledge_id not in self.knowledges:
            self.knowledges[knowledge_id] = KnowledgeState()
            self.save_to_disk()
        return self.knowledges[knowledge_id]

    def get_user(self, user_id: str) -> UserState:
        if user_id not in self.users:
            self.users[user_id] = UserState(user_id=user_id)
            self.save_to_disk()
        return self.users[user_id]

    def start_db_flush_loop(self, event_bus):
        """Starts a debounced background state saving loop."""
        import asyncio
        import logging
        from gptmoss.core.event_bus import Event
        
        logger = logging.getLogger("gptmoss.state")
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
                except Exception as e:
                    logger.error(f"Error in db_flush_loop: {e}")
                    await asyncio.sleep(1.0)
                    
        return asyncio.create_task(db_flush_loop())
