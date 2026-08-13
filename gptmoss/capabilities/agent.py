import asyncio
import logging
from typing import Optional, Dict, Any
from gptmoss.interfaces.capability import capability, action

logger = logging.getLogger("gptmoss.capabilities.agent")

INHERITED_VARIABLE_KEYS = (
    "project_id",
    "project_path",
    "project_domains",
    "attachment_ids",
    "corpus_ids",
    "corpus_auto_workflow",
    "planning_mode",
)
TERMINAL_EXECUTION_STATUSES = frozenset({"completed", "failed", "cancelled"})
SUBTASK_WAIT_POLLS = 3_600


def _status_value(state) -> str:
    status = getattr(state, "status", state)
    return str(getattr(status, "value", status))


def child_agent_config(
    state_engine,
    parent_id: Optional[str],
    *,
    system_prompt: Optional[str] = None,
    role_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a child agent_config that inherits project context from its ancestry."""
    config: Dict[str, Any] = {}
    if system_prompt:
        config["system_prompt"] = system_prompt
    if role_name:
        config["role_name"] = role_name
    if not parent_id or state_engine is None:
        return config

    config["parent_execution_id"] = parent_id
    inherited: Dict[str, Any] = {}
    skills = None
    current_id = parent_id
    seen: set[str] = set()
    executions = getattr(state_engine, "executions", {})
    while current_id and current_id not in seen:
        seen.add(current_id)
        state = executions.get(current_id)
        if not state:
            break
        variables = state.variables or {}
        for key in INHERITED_VARIABLE_KEYS:
            if key in variables and key not in inherited:
                inherited[key] = variables[key]
        if skills is None:
            requested = variables.get("requested_skills")
            agent_config = variables.get("agent_config") if isinstance(variables.get("agent_config"), dict) else {}
            candidate = requested or agent_config.get("skills")
            if candidate:
                skills = list(candidate)
        current_id = variables.get("parent_execution_id")

    if inherited:
        config["variables"] = inherited
    if skills:
        config["skills"] = skills
    return config


@capability(name="agent", description="Manage, spawn, and monitor sub-agents to delegate tasks.")
class AgentCapability:
    """
    Capability to spawn and delegate tasks to sub-agents.
    """
    def __init__(self, kernel=None, workspace_root: str = "."):
        # Store runtime kernel reference to submit tasks (can be set post-initialization)
        self.kernel = kernel
        self.workspace_root = workspace_root

    def update_workspace_config(self, workspace_root: str):
        self.workspace_root = workspace_root

    def _state_engine(self):
        if not self.kernel:
            return None
        return getattr(self.kernel, "state_engine", None) or getattr(
            getattr(self.kernel, "execution_engine", None), "state_engine", None
        )

    @action(name="spawn", description="Spawn a new sub-agent to run a task in the background. Returns the sub-agent's execution_id.")
    async def spawn(self, task: str, system_prompt: Optional[str] = None, role_name: Optional[str] = None, context: Optional[Dict[str, Any]] = None) -> str:
        """Launches a sub-agent task asynchronously."""
        if not self.kernel:
            return "Error: Runtime kernel reference not set on AgentCapability."

        parent_id = context.get("execution_id") if context else None
        agent_config = child_agent_config(
            self._state_engine(),
            parent_id,
            system_prompt=system_prompt or "You are a helpful MOSS sub-agent assisting a parent agent.",
            role_name=role_name,
        )
        try:
            exec_id = await self.kernel.submit_task(task, agent_config)
            return f"Sub-agent spawned successfully. Execution ID: {exec_id}. Status: running."
        except Exception as e:
            return f"Error spawning sub-agent: {e}"

    @action(name="status", description="Get the current execution status and latest response of a sub-agent by its execution_id.")
    def status(self, execution_id: str) -> str:
        """Gets current status and last response from sub-agent."""
        if not self.kernel:
            return "Error: Runtime kernel reference not set on AgentCapability."
            
        try:
            state_engine = self.kernel.execution_engine.state_engine
            if execution_id not in state_engine.executions:
                return f"Error: Sub-agent execution_id {execution_id} not found."
                
            state = state_engine.get_execution(execution_id)
            convo = state_engine.get_conversation(execution_id)
            
            # Find last assistant message if any
            last_response = "No response yet."
            for msg in reversed(convo.messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    last_response = msg["content"]
                    break
                    
            status_summary = (
                f"Execution ID: {execution_id}\n"
                f"Status: {state.status}\n"
                f"Current Step: {state.current_step}\n"
                f"Last Response: {last_response}"
            )
            return status_summary
        except Exception as e:
            return f"Error checking sub-agent status: {e}"

    @action(name="execute_subtask", description="Delegate a task to a sub-agent and wait for it to complete. Returns the final result.")
    async def execute_subtask(self, task: str, system_prompt: Optional[str] = None, role_name: Optional[str] = None, context: Optional[Dict[str, Any]] = None) -> str:
        """Launches a sub-agent and waits synchronously until completion."""
        if not self.kernel:
            return "Error: Runtime kernel reference not set on AgentCapability."

        parent_id = context.get("execution_id") if context else None
        agent_config = child_agent_config(
            self._state_engine(),
            parent_id,
            system_prompt=system_prompt or "You are a helpful MOSS sub-agent assisting a parent agent.",
            role_name=role_name,
        )
        try:
            exec_id = await self.kernel.submit_task(task, agent_config)
            state_engine = self.kernel.execution_engine.state_engine

            polls = 0
            while True:
                await asyncio.sleep(1.0)
                state = state_engine.executions.get(exec_id) or state_engine.get_execution(exec_id)
                if parent_id:
                    parent = state_engine.executions.get(parent_id)
                    if parent and _status_value(parent) == "cancelled":
                        return (
                            f"Parent execution was cancelled while waiting for subtask. "
                            f"Execution ID: {exec_id}."
                        )
                status = _status_value(state)
                if status in TERMINAL_EXECUTION_STATUSES:
                    break
                polls += 1
                if polls >= SUBTASK_WAIT_POLLS:
                    return (
                        f"Subtask is still {status} after waiting. "
                        f"Execution ID: {exec_id}."
                    )

            convo = state_engine.get_conversation(exec_id)
            if status == "completed":
                last_response = "Subtask completed."
                for msg in reversed(convo.messages):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        last_response = msg["content"]
                        break
                return f"Subtask completed successfully. Result:\n{last_response}"
            return f"Subtask failed or was cancelled. Final status: {status}."
        except Exception as e:
            return f"Error executing subtask: {e}"
