from abc import ABC, abstractmethod
from typing import Dict, Any
from pydantic import BaseModel

class PolicyDecision(BaseModel):
    decision: str  # "allow", "deny", "approval"
    reason: str
    details: Dict[str, Any] = {}

class PolicyProvider(ABC):
    """
    Interface for Policies.
    Controls what actions are permitted before execution.
    """

    @abstractmethod
    async def check_action(
        self,
        execution_id: str,
        capability: str,
        action: str,
        arguments: Dict[str, Any],
        context: Dict[str, Any],
        **kwargs
    ) -> PolicyDecision:
        """
        Evaluate if a capability action execution should be allowed, denied, or needs human approval.
        
        Args:
            execution_id: ID of the running execution.
            capability: Name of the capability.
            action: Name of the action.
            arguments: Arguments for the action.
            context: Current execution context.
            **kwargs: Extra parameters.
            
        Returns:
            PolicyDecision object containing decision and reason.
        """
        pass
