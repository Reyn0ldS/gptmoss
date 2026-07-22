import uuid
import asyncio
import logging
from typing import Dict, Any
from gptmoss.core.event_bus import EventBus, Event
from gptmoss.core.state import StateEngine
from gptmoss.core.execution import ExecutionEngine

logger = logging.getLogger("gptmoss.kernel")

class RuntimeKernel:
    """
    Runtime Kernel is the tiny orchestrator.
    It receives tasks, loads the agent state, and delegates running to the Execution Engine.
    """
    def __init__(
        self,
        event_bus: EventBus,
        state_engine: StateEngine,
        execution_engine: ExecutionEngine
    ):
        self.event_bus = event_bus
        self.state_engine = state_engine
        self.execution_engine = execution_engine

    async def submit_task(self, task: str, agent_config: Dict[str, Any]) -> str:
        """
        Receives task, loads config, and initializes the execution.
        
        Args:
            task: Task description string.
            agent_config: Dictionary representing the agent instructions/settings.
            
        Returns:
            The execution ID of the running task.
        """
        execution_id = str(uuid.uuid4())
        
        # Setup agent state
        agent_state = self.state_engine.get_agent("default_agent")
        agent_state.config = agent_config
        
        # Mark state execution as pending
        exec_state = self.state_engine.get_execution(execution_id)
        exec_state.status = "pending"
        if "role_name" in agent_config:
            exec_state.variables["role_name"] = agent_config["role_name"]
        if "parent_execution_id" in agent_config:
            exec_state.variables["parent_execution_id"] = agent_config["parent_execution_id"]
        
        # Emit TaskCreated
        await self.event_bus.publish(Event(
            type="TaskCreated",
            payload={
                "execution_id": execution_id,
                "task": task,
                "agent_id": "default_agent"
            }
        ))
        
        # Run execution loop as a background task
        asyncio.create_task(self.execution_engine.execute_task(execution_id, task))
        
        return execution_id
