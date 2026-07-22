import asyncio
import logging
from typing import Optional, Dict, Any
from gptmoss.interfaces.capability import capability, action

logger = logging.getLogger("gptmoss.capabilities.agent")

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

    @action(name="spawn", description="Spawn a new sub-agent to run a task in the background. Returns the sub-agent's execution_id.")
    async def spawn(self, task: str, system_prompt: Optional[str] = None, role_name: Optional[str] = None, context: Optional[Dict[str, Any]] = None) -> str:
        """Launches a sub-agent task asynchronously."""
        if not self.kernel:
            return "Error: Runtime kernel reference not set on AgentCapability."
            
        agent_config = {
            "system_prompt": system_prompt or "You are a helpful MOSS sub-agent assisting a parent agent."
        }
        if role_name:
            agent_config["role_name"] = role_name
        parent_id = context.get("execution_id") if context else None
        if parent_id:
            agent_config["parent_execution_id"] = parent_id
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
            
        agent_config = {
            "system_prompt": system_prompt or "You are a helpful MOSS sub-agent assisting a parent agent."
        }
        if role_name:
            agent_config["role_name"] = role_name
        parent_id = context.get("execution_id") if context else None
        if parent_id:
            agent_config["parent_execution_id"] = parent_id
        try:
            exec_id = await self.kernel.submit_task(task, agent_config)
            state_engine = self.kernel.execution_engine.state_engine
            
            # Poll until finished
            while True:
                await asyncio.sleep(1.0)
                state = state_engine.get_execution(exec_id)
                if state.status in ("completed", "failed", "cancelled"):
                    break
                    
            convo = state_engine.get_conversation(exec_id)
            if state.status == "completed":
                # Find last assistant message as result
                last_response = "Subtask completed."
                for msg in reversed(convo.messages):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        last_response = msg["content"]
                        break
                return f"Subtask completed successfully. Result:\n{last_response}"
            else:
                return f"Subtask failed or was cancelled. Final status: {state.status}."
        except Exception as e:
            return f"Error executing subtask: {e}"
