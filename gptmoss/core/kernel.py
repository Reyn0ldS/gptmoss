import uuid
import asyncio
import logging
import time
from typing import Dict, Any
from gptmoss.core.event_bus import EventBus, Event
from gptmoss.core.state import StateEngine
from gptmoss.core.execution import ExecutionEngine
from gptmoss.planners.complexity import normalize_planning_mode, task_title_from_text

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

    async def submit_task(self, task: str, agent_config: Dict[str, Any], *,
                          delay_seconds: float = 0, run_at: float | None = None) -> str:
        """
        Receives task, loads config, and initializes the execution.
        
        Args:
            task: Task description string.
            agent_config: Dictionary representing the agent instructions/settings.
            
        Returns:
            The execution ID of the running task.
        """
        task = str(task or "").strip()
        if not task:
            raise ValueError("Task description cannot be empty.")
        agent_config = dict(agent_config or {})
        parent_execution_id = agent_config.get("parent_execution_id")
        parent_state = (
            self.state_engine.executions.get(parent_execution_id)
            if parent_execution_id else None
        )
        normalized_task = " ".join(task.lower().split())
        delegation_depth = 0
        delegation_lineage = [normalized_task]
        if parent_state:
            delegation_depth = int(parent_state.variables.get("delegation_depth", 0)) + 1
            delegation_lineage = list(parent_state.variables.get("delegation_lineage") or [])
            if not delegation_lineage:
                delegation_lineage = [
                    " ".join(str(parent_state.variables.get("task") or "").lower().split())
                ]
            if normalized_task in delegation_lineage:
                raise ValueError(
                    "Recursive delegation cycle rejected: this normalized task already exists in its ancestry."
                )
            maximum_depth = int(
                getattr(self.execution_engine, "max_delegation_depth", 0) or 0
            )
            if maximum_depth and delegation_depth > maximum_depth:
                raise ValueError(
                    f"Delegation depth {delegation_depth} exceeds the configured maximum {maximum_depth}."
                )
            delegation_lineage.append(normalized_task)
        execution_id = str(uuid.uuid4())
        
        # Setup agent state
        agent_state = self.state_engine.get_agent("default_agent")
        agent_state.config = agent_config
        
        # Mark state execution as pending
        exec_state = self.state_engine.get_execution(execution_id)
        self.state_engine.transition_execution(
            exec_state, "pending", reason="task submitted", actor="kernel"
        )
        exec_state.variables["task"] = task
        exec_state.variables["task_title"] = task_title_from_text(task)
        scheduled_for = float(run_at) if run_at is not None else time.time() + max(0.0, float(delay_seconds))
        exec_state.variables["scheduled_for"] = scheduled_for
        exec_state.variables["delegation_depth"] = delegation_depth
        exec_state.variables["delegation_lineage"] = delegation_lineage
        exec_state.variables["agent_config"] = {
            key: value for key, value in agent_config.items() if key != "variables"
        }
        initial_variables = agent_config.get("variables")
        if isinstance(initial_variables, dict):
            exec_state.variables.update(initial_variables)
        if "role_name" in agent_config:
            exec_state.variables["role_name"] = agent_config["role_name"]
        if "parent_execution_id" in agent_config:
            exec_state.variables["parent_execution_id"] = agent_config["parent_execution_id"]
        if "skills" in agent_config:
            exec_state.variables["requested_skills"] = agent_config["skills"]
        exec_state.variables["planning_mode"] = normalize_planning_mode(
            exec_state.variables.get("planning_mode")
            or agent_config.get("planning_mode")
        )
        
        # Emit TaskCreated
        await self.event_bus.publish(Event(
            type="TaskCreated",
            payload={
                "execution_id": execution_id,
                "task": task,
                "agent_id": "default_agent"
            }
        ))
        await self.event_bus.publish(Event(
            type="TaskScheduled",
            payload={"execution_id": execution_id, "run_at": scheduled_for},
        ))
        self.execution_engine.schedule_execution(
            execution_id, task, run_at=scheduled_for,
        )
        
        return execution_id
