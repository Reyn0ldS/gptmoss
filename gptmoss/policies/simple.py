from typing import Dict, Any, List, Optional
from gptmoss.interfaces.policy import PolicyProvider, PolicyDecision

class SimplePolicyProvider(PolicyProvider):
    """
    Simple policy checking capability execution.
    By default:
    - 'shell' actions require human approval.
    - specific capability actions can be blacklisted.
    """
    def __init__(
        self,
        approval_required_capabilities: Optional[List[str]] = None,
        denied_capabilities: Optional[List[str]] = None
    ):
        self.approval_required = [c.lower() for c in (approval_required_capabilities or ["shell"])]
        self.denied = [c.lower() for c in (denied_capabilities or [])]

    def update_policy(self, approval_required: List[str], denied: List[str]):
        self.approval_required = [c.lower() for c in approval_required]
        self.denied = [c.lower() for c in denied]

    async def check_action(
        self,
        execution_id: str,
        capability: str,
        action: str,
        arguments: Dict[str, Any],
        context: Dict[str, Any],
        **kwargs
    ) -> PolicyDecision:
        cap_lower = capability.lower()
        act_lower = action.lower()
        
        # Check explicit denials
        if cap_lower in self.denied or f"{cap_lower}.{act_lower}" in self.denied:
            return PolicyDecision(
                decision="deny",
                reason=f"Action '{capability}.{action}' is blacklisted by policy.",
                details={"capability": capability, "action": action}
            )
            
        # Check approval required
        if cap_lower in self.approval_required or f"{cap_lower}.{act_lower}" in self.approval_required:
            return PolicyDecision(
                decision="approval",
                reason=f"Action '{capability}.{action}' requires human confirmation before running.",
                details={"capability": capability, "action": action, "arguments": arguments}
            )
            
        # Default allow
        return PolicyDecision(
            decision="allow",
            reason=f"Action '{capability}.{action}' is allowed by default policies.",
            details={"capability": capability, "action": action}
        )
