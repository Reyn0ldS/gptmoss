from abc import ABC, abstractmethod
from typing import Dict, Any, List

class PlannerProvider(ABC):
    """
    Interface for Planners.
    Responsible for generating steps, workflows, or sub-tasks from a task and context.
    """

    @abstractmethod
    async def plan(
        self,
        task: str,
        context: Dict[str, Any],
        capabilities_schemas: List[Dict[str, Any]],
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate a plan consisting of steps or execution items.
        
        Args:
            task: The main instruction or goal.
            context: Compiled context from the Context Engine.
            capabilities_schemas: Schemas of actions available.
            **kwargs: Extra planner settings.
            
        Returns:
            Dict representing the generated plan, containing:
                - "steps": List of step dicts: [{"id": 1, "description": "...", "status": "pending"}]
                - "rationale": Description of why this plan was chosen.
        """
        pass
