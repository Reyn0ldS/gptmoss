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
        denied_capabilities: Optional[List[str]] = None,
        workspace_full_autonomy: bool = False,
    ):
        approvals = ["shell"] if approval_required_capabilities is None else approval_required_capabilities
        self.approval_required = [c.lower() for c in approvals]
        self.denied = [c.lower() for c in (denied_capabilities or [])]
        self.workspace_full_autonomy = bool(workspace_full_autonomy)

    def update_policy(self, approval_required: List[str], denied: List[str],
                      workspace_full_autonomy: Optional[bool] = None):
        self.approval_required = [c.lower() for c in approval_required]
        self.denied = [c.lower() for c in denied]
        if workspace_full_autonomy is not None:
            self.workspace_full_autonomy = bool(workspace_full_autonomy)

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

        # Explicit opt-in: all current and future shell commands are
        # pre-authorized. Capability and shell-level workspace/safety checks
        # still apply, as do the explicit denials evaluated above.
        if self.workspace_full_autonomy and cap_lower == "shell":
            return PolicyDecision(
                decision="allow",
                reason="Shell command pre-authorized by workspace full autonomy mode.",
                details={"capability": capability, "action": action, "workspace_scoped": True},
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
