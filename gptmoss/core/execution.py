import asyncio
import ast
import hashlib
import json
import time
import logging
import inspect
import os
import re
import shlex
import sys
from typing import Dict, Any, List, Optional
from gptmoss.core.event_bus import Event, EventBus
from gptmoss.core.state import StateEngine
from gptmoss.core.context import ContextEngine
from gptmoss.interfaces.llm import LLMProvider
from gptmoss.interfaces.planner import PlannerProvider
from gptmoss.interfaces.policy import PolicyProvider, PolicyDecision
from gptmoss.interfaces.capability import generate_action_schema, get_actions
from gptmoss.core.observability import TraceRecorder
from gptmoss.core.skills import SkillRegistry
from gptmoss.core.artifacts import ArtifactStore
from gptmoss.core.artifact_validation import validate_artifact
from gptmoss.core.evolution import AgentProfileRegistry, AutonomousSkillLifecycle
from gptmoss.core.delivery import (
    build_delivery_contract,
    commands_equivalent,
    evaluate_delivery,
    path_is_owned,
)
from gptmoss.core.adaptive import AdaptiveRuntimePolicy, tool_call_fingerprint
from gptmoss.core.professional_delivery import apply_professional_profile
from gptmoss.core.delivery_package import build_delivery_package
from gptmoss.core.delivery_coordinator import DeliveryCoordinator
from gptmoss.core.approval_coordinator import ApprovalCoordinator
from gptmoss.core.provider_recovery import (
    ProviderConfigurationError as RecoveryProviderConfigurationError,
    ProviderRecoveryCoordinator,
    ProviderUnavailableError as RecoveryProviderUnavailableError,
)
from gptmoss.core.scheduler import Scheduler
from gptmoss.core.long_document_engine import LongDocumentEngine

ROLE_DISPLAY_NAMES = {
    "architect": "Architecte",
    "security": "Analyste Sécurité",
    "developer": "Développeur",
    "qa": "Testeur QA",
    "debugger": "Débugueur",
    "writer": "Rédacteur Technique",
    "coordinator": "Coordinateur",
}

ROLE_ALIASES = {
    "architect": "architect", "architecte": "architect", "analyst": "architect", "analyste": "architect",
    "security": "security", "sécurité": "security", "reviewer": "security", "analyste sécurité": "security",
    "developer": "developer", "développeur": "developer", "coder": "developer", "codeur": "developer",
    "qa": "qa", "tester": "qa", "testeur": "qa", "testeur qa": "qa",
    "debugger": "debugger", "debug": "debugger", "débugueur": "debugger", "bug fixer": "debugger",
    "writer": "writer", "rédacteur": "writer", "rédacteur technique": "writer", "documentation": "writer",
    "coordinator": "coordinator", "coordinateur": "coordinator", "summary": "coordinator",
}

def canonical_step_role(value: Any) -> Optional[str]:
    if value is None:
        return None
    return ROLE_ALIASES.get(str(value).strip().lower())

def infer_step_role(description: str) -> Optional[str]:
    desc_lower = str(description or "").lower()
    # Debugger descriptions commonly contain "tests"; match them before QA.
    if any(marker in desc_lower for marker in ("debug", "bug fixer", "débug", "corriger les erreurs")):
        return "debugger"
    if any(marker in desc_lower for marker in ("architect", "architecte", "technical specification", "spécification technique")):
        return "architect"
    if any(marker in desc_lower for marker in ("security", "sécurité", "compliance reviewer", "revue de conformité")):
        return "security"
    if any(marker in desc_lower for marker in ("qa", "tester", "testeur", "testing engineer", "unit tests")):
        return "qa"
    if any(marker in desc_lower for marker in ("developer", "coder", "développeur", "codeur")):
        return "developer"
    if any(marker in desc_lower for marker in ("technical writer", "writer", "rédacteur", "documentation")):
        return "writer"
    return None

def parse_step_role(description: str) -> Optional[str]:
    role = infer_step_role(description)
    return ROLE_DISPLAY_NAMES.get(role) if role else None

def normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a planner response and normalize its stable execution contract."""
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        raise ValueError("A plan must contain a list of steps.")
    steps = plan["steps"]
    identifiers = []
    identifier_keys = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"Plan step {index} must be an object.")
        step_id = step.get("id", index)
        identifier_key = str(step_id)
        if (
            isinstance(step_id, bool) or not isinstance(step_id, (int, str))
            or identifier_key in identifier_keys
        ):
            raise ValueError(f"Plan step {index} has an invalid or duplicate id.")
        identifiers.append(step_id)
        identifier_keys.add(identifier_key)
        step["id"] = step_id
        step["description"] = str(step.get("description") or "").strip()
        if not step["description"]:
            raise ValueError(f"Plan step {step_id} has no description.")
        dependencies = step.get("dependencies") or []
        if (
            not isinstance(dependencies, list)
            or any(isinstance(dep, bool) or not isinstance(dep, (int, str)) for dep in dependencies)
            or len(set(map(str, dependencies))) != len(dependencies)
        ):
            raise ValueError(f"Plan step {step_id} has invalid dependencies.")
        step["dependencies"] = dependencies
        requested_role = step.get("role")
        role = canonical_step_role(requested_role) if requested_role is not None else infer_step_role(step["description"])
        if requested_role is not None and not role:
            raise ValueError(f"Plan step {step_id} has unsupported role '{requested_role}'.")
        if role:
            step["role"] = role
        specialist = str(step.get("specialist") or "").strip()
        if specialist:
            if len(specialist) > 160:
                raise ValueError(f"Plan step {step_id} has an excessively long specialist title.")
            step["specialist"] = specialist
        for field in (
            "expertise", "required_artifacts", "acceptance_criteria",
            "verification_commands", "requirement_ids", "owned_paths",
        ):
            values = step.get(field) or []
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"Plan step {step_id} has an invalid {field} list.")
            step[field] = [value.strip() for value in values if value.strip()]
        step["status"] = step.get("status", "pending")

    identifier_set = set(identifiers)
    for step in steps:
        if step["id"] in step["dependencies"] or any(dep not in identifier_set for dep in step["dependencies"]):
            raise ValueError(f"Plan step {step['id']} references an invalid dependency.")

    completed = set()
    while len(completed) < len(steps):
        ready = [step["id"] for step in steps if step["id"] not in completed and set(step["dependencies"]) <= completed]
        if not ready:
            raise ValueError("Plan contains cyclical dependencies.")
        completed.update(ready)
    return plan


def merge_inherited_requirements(
    plan: Dict[str, Any], inherited: Any
) -> Dict[str, Any]:
    """Keep parent requirement identifiers valid in delegated child plans."""
    if not isinstance(inherited, list) or not inherited:
        return plan
    requirements = plan.get("requirements")
    if not isinstance(requirements, list):
        requirements = []
    else:
        requirements = list(requirements)
    known = {
        str(item.get("id"))
        for item in requirements
        if isinstance(item, dict) and item.get("id")
    }
    requirements.extend(
        dict(item) for item in inherited
        if isinstance(item, dict) and item.get("id") and str(item["id"]) not in known
    )
    plan["requirements"] = requirements
    return plan


def requirements_for_delegation(
    parent_requirements: Any, requirement_ids: Any
) -> List[Dict[str, Any]]:
    """Select complete requirement records, never bare identifiers, for a specialist."""
    if not isinstance(parent_requirements, list):
        return []
    requirements = [
        dict(requirement) for requirement in parent_requirements
        if isinstance(requirement, dict) and requirement.get("id")
    ]
    selected_ids = {
        str(requirement_id) for requirement_id in (requirement_ids or [])
        if str(requirement_id).strip()
    }
    if selected_ids:
        return [
            requirement for requirement in requirements
            if str(requirement.get("id")) in selected_ids
        ]
    return [
        requirement for requirement in requirements
        if requirement.get("mandatory", True)
    ]


def requirement_validation_commands(requirements: Any) -> List[str]:
    """Extract explicit machine-validation commands quoted in requirement text."""
    if not isinstance(requirements, list):
        return []
    commands = []
    validation_pattern = re.compile(
        r"(?i)(?:\bpytest\b|\bunittest\b|\bcompileall\b|"
        r"\bnpm\s+(?:run\s+)?test\b|\bcargo\s+test\b|\bgo\s+test\b|"
        r"\bdotnet\s+test\b|\bmvn(?:\.cmd)?\s+test\b|"
        r"\bgradle(?:w)?\s+test\b|\bruff\s+check\b|\bmypy\b|\btsc\b)"
    )
    delimiter = chr(96)
    quoted_command = re.compile(
        re.escape(delimiter) + r"([^" + re.escape(delimiter) + r"\r\n]+)"
        + re.escape(delimiter)
    )
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        texts = [str(requirement.get("statement") or "")]
        acceptance = requirement.get("acceptance")
        if isinstance(acceptance, list):
            texts.extend(str(item) for item in acceptance)
        for text in texts:
            for candidate in quoted_command.findall(text):
                command = candidate.strip()
                if validation_pattern.search(command) and command not in commands:
                    commands.append(command)
    return commands


def requirements_request_mutation(requirements: Any) -> bool:
    """Return whether the assignment explicitly asks for a durable edit."""
    if not isinstance(requirements, list):
        return False
    explicit_filesystem_edit = re.compile(
        r"(?i)\buse\s+(?:the\s+)?filesystem\s+(?:write|edit)\b"
    )
    direct_file_edit = re.compile(
        r"(?i)^\s*(?:please\s+)?(?:edit|modify|write|create|fix|repair|update|"
        r"delete|remove)\b[^\r\n]*(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+"
    )
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        texts = [str(requirement.get("statement") or "")]
        acceptance = requirement.get("acceptance")
        if isinstance(acceptance, list):
            texts.extend(str(item) for item in acceptance)
        if any(
            explicit_filesystem_edit.search(text) or direct_file_edit.search(text)
            for text in texts
        ):
            return True
    return False


logger = logging.getLogger("gptmoss.execution")


class ProviderUnavailableError(RuntimeError):
    """A transient provider outage that must suspend, not destroy, an execution."""

    def __init__(self, message: str, original_error: Exception):
        super().__init__(message)
        self.original_error = original_error


class ProviderConfigurationError(RuntimeError):
    """A permanent provider refusal that requires a settings change before retry."""

    def __init__(self, original_error: Exception):
        super().__init__(
            "Authentification LLM refusée (HTTP 401/403). Ouvrez Paramètres, "
            "corrigez la clé API, utilisez Tester la connexion, puis reprenez "
            "l'exécution parente."
        )
        self.original_error = original_error


class ExecutionEngine:
    """
    Execution Engine handles the execution loop of tasks step-by-step.
    Orchestrates LLM calls, capability execution, policy checks, and human approval flows.
    """
    def __init__(
        self,
        event_bus: EventBus,
        state_engine: StateEngine,
        context_engine: ContextEngine,
        llm_provider: LLMProvider,
        planner: PlannerProvider,
        policy_provider: PolicyProvider,
        telemetry: Optional[TraceRecorder] = None,
        skill_registry: Optional[SkillRegistry] = None,
        artifact_store: Optional[ArtifactStore] = None,
        default_skills: Optional[List[str]] = None,
        max_step_iterations: int = 30,
        max_step_retries: int = 2,
        continue_while_progress: bool = True,
        agent_profile_registry: Optional[AgentProfileRegistry] = None,
        skill_lifecycle: Optional[AutonomousSkillLifecycle] = None,
        autonomous_specialization: bool = True,
        adaptive_resource_management: bool = True,
        strict_skill_capabilities: bool = False,
        allow_nested_delegation: bool = True,
        max_delegation_depth: int = 0,
        scheduler: Optional[Scheduler] = None,
        document_engine_enabled: bool = True,
        document_checkpoint_enabled: bool = True,
        document_target_section_words: int = 450,
        diagram_rendering: bool = True,
        docx_embed_diagrams: bool = True,
    ):
        self.event_bus = event_bus
        self.state_engine = state_engine
        self.context_engine = context_engine
        self.llm_provider = llm_provider
        self.planner = planner
        self.policy_provider = policy_provider
        self.telemetry = telemetry or TraceRecorder()
        self.skill_registry = skill_registry
        self.artifact_store = artifact_store
        self.default_skills = [str(skill).lower() for skill in (default_skills or [])]
        self.max_step_iterations = max(1, int(max_step_iterations))
        self.max_step_retries = max(0, int(max_step_retries))
        self.continue_while_progress = bool(continue_while_progress)
        self.adaptive_resource_management = bool(adaptive_resource_management)
        self.strict_skill_capabilities = bool(strict_skill_capabilities)
        self.allow_nested_delegation = bool(allow_nested_delegation)
        self.max_delegation_depth = max(0, int(max_delegation_depth))
        self.document_engine_enabled = bool(document_engine_enabled)
        self.document_checkpoint_enabled = bool(document_checkpoint_enabled)
        self.document_target_section_words = max(80, int(document_target_section_words))
        self.diagram_rendering = bool(diagram_rendering)
        self.docx_embed_diagrams = bool(docx_embed_diagrams)
        self.runtime_policy = AdaptiveRuntimePolicy(
            baseline_stagnation_iterations=self.max_step_iterations,
            baseline_retries=self.max_step_retries,
            adaptive=self.adaptive_resource_management,
        )
        self.agent_profile_registry = agent_profile_registry
        self.skill_lifecycle = skill_lifecycle
        self.autonomous_specialization = bool(autonomous_specialization)
        self._capabilities: Dict[str, Any] = {}  # capability_name -> instance
        self._execution_locks: Dict[str, asyncio.Lock] = {}
        self._path_locks: Dict[str, asyncio.Lock] = {}
        self.scheduler = scheduler or Scheduler()
        self.provider_recovery = ProviderRecoveryCoordinator(
            event_bus, state_engine, llm_provider,
            lambda execution_id, task: self.execute_task(execution_id, task),
            self.max_step_iterations, self.scheduler,
        )
        self.delivery_coordinator = DeliveryCoordinator(state_engine, self.get_capability)
        self.approval_coordinator = ApprovalCoordinator(
            state_engine, event_bus,
            lambda execution_id, task: self.execute_task(execution_id, task),
        )

    def register_capability(self, capability_name: str, instance: Any):
        """Register instantiated capability."""
        self._capabilities[capability_name.lower()] = instance
        # Ensure standard action methods are populated on instance
        instance.actions = get_actions(instance.__class__)
        logger.info(f"Registered capability: {capability_name}")

    def get_capability(self, capability_name: str) -> Optional[Any]:
        """Retrieve a registered capability by name."""
        return self._capabilities.get(capability_name.lower())

    @staticmethod
    def _is_permanent_llm_error(error: Exception) -> bool:
        return ProviderRecoveryCoordinator.is_permanent(error)

    @classmethod
    def _is_transient_llm_error(cls, error: Exception) -> bool:
        return ProviderRecoveryCoordinator.is_transient(error)

    async def _completion_with_recovery(self, execution_id: str, **kwargs) -> Dict[str, Any]:
        """Keep durable task state through temporary local/provider outages."""
        try:
            return await self.provider_recovery.completion(execution_id, **kwargs)
        except RecoveryProviderConfigurationError as error:
            raise ProviderConfigurationError(error.original_error) from error
        except RecoveryProviderUnavailableError as error:
            raise ProviderUnavailableError(str(error), error.original_error) from error

    def _schedule_provider_resume(self, execution_id: str, delay_seconds: int = 30) -> None:
        self.provider_recovery.schedule(execution_id, delay_seconds)

    def resume_waiting_provider_executions(self) -> None:
        """Restore automatic retries after a process restart."""
        self.provider_recovery.resume_persisted()

    def resume_interrupted_executions(self) -> None:
        """Resume top-level work that was persisted while the process stopped."""
        changed = False
        for state in self.state_engine.executions.values():
            if (
                state.variables.get("parent_execution_id") is not None
                and state.status == "running"
            ):
                self.state_engine.transition_execution(
                    state, "pending", reason="interrupted child recovery", actor="runtime"
                )
                state.variables["interrupted_resume_count"] = (
                    int(state.variables.get("interrupted_resume_count", 0)) + 1
                )
                for step in (state.current_plan or {}).get("steps", []):
                    if step.get("status") == "running":
                        step["status"] = "pending"
                changed = True
        if changed:
            self.state_engine.save_to_disk()
        for execution_id, state in self.state_engine.executions.items():
            if state.variables.get("parent_execution_id") is not None:
                continue
            if state.status not in {"pending", "running"}:
                continue
            task = str(state.variables.get("task") or "").strip()
            if task:
                scheduled_for = float(state.variables.get("scheduled_for") or 0)
                delay = max(0.0, scheduled_for - time.time())
                self.schedule_execution(execution_id, task, delay=delay)

    def schedule_execution(self, execution_id: str, task: str, *, delay: float = 0,
                           run_at: Optional[float] = None) -> str:
        """Schedule one execution through the shared runtime timing service."""
        job_id = f"execution:{execution_id}"
        if self.scheduler.has(job_id):
            return job_id
        self.scheduler.schedule(
            lambda: self.execute_task(execution_id, task),
            delay=delay,
            run_at=run_at,
            job_id=job_id,
            metadata={"kind": "execution", "execution_id": execution_id},
        )
        self.scheduler.start()
        return job_id

    async def stop_provider_resume_tasks(self) -> None:
        await self.provider_recovery.stop()

    async def stop_runtime_services(self) -> None:
        await self.provider_recovery.stop()
        await self.scheduler.stop(cancel_pending=True)
        close_provider = getattr(self.llm_provider, "close", None)
        if close_provider:
            result = close_provider()
            if inspect.isawaitable(result):
                await result

    def get_capabilities_schemas(
        self,
        is_sub_agent: bool = False,
        allowed_capabilities: Optional[set[str]] = None,
        delegation_depth: int = 0,
        suppress_delegation: bool = False,
    ) -> List[Dict[str, Any]]:
        """Generate JSON schemas for all registered capabilities."""
        schemas = []
        for name, inst in self._capabilities.items():
            if allowed_capabilities is not None and name.lower() not in allowed_capabilities:
                continue
            delegation_blocked = (
                suppress_delegation
                or (is_sub_agent and not self.allow_nested_delegation)
                or (
                    bool(self.max_delegation_depth)
                    and int(delegation_depth) >= self.max_delegation_depth
                )
            )
            if delegation_blocked and name.lower() in ("agent", "devteam"):
                continue
            for act_name, method in inst.actions.items():
                schemas.append(generate_action_schema(name, act_name, method))
        return schemas

    def _active_skills(self, state, task: str):
        if not self.skill_registry:
            return []
        requested = state.variables.get("requested_skills")
        selected = self.skill_registry.select(task, requested=requested, preferred=self.default_skills)
        state.variables["active_skills"] = [{"name": skill.name, "digest": skill.digest} for skill in selected]
        return selected

    def _evolution_capabilities(self) -> set[str]:
        denied = {str(item).lower().split(".", 1)[0] for item in getattr(self.policy_provider, "denied", [])}
        return set(self._capabilities) - denied

    async def _prepare_autonomous_specialization(self, execution_id: str, state,
                                                 step: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a novel specialist and synthesize a missing procedural skill."""
        if not self.autonomous_specialization or not self.agent_profile_registry:
            return {}
        profile = self.agent_profile_registry.ensure(step, self._evolution_capabilities())
        retry_count = int(step.get("retry_count", 0))
        if retry_count and step.get("profile_revision_attempt") != retry_count:
            revision_result = await self.agent_profile_registry.improve(
                profile["id"], str(step.get("retry_context") or "Previous delivery gates failed."),
                lambda **kwargs: self._completion_with_recovery(execution_id, **kwargs),
            )
            if revision_result.get("improved"):
                profile = revision_result["profile"]
            step["profile_revision_attempt"] = retry_count
        step["agent_profile_id"] = profile["id"]
        step["agent_profile_revision"] = profile.get("revision", 1)
        requested = set(step.get("autonomous_skill_names", [])) | set(profile.get("skill_names", []))
        lifecycle_result: Dict[str, Any] = {}
        if self.skill_lifecycle:
            if retry_count and step.get("skill_revision_attempt") != retry_count:
                for skill_name in sorted(requested):
                    await self.skill_lifecycle.improve(
                        execution_id, skill_name, profile, step,
                        str(step.get("retry_context") or "Previous specialist failed delivery gates."),
                        self._evolution_capabilities(),
                        lambda **kwargs: self._completion_with_recovery(execution_id, **kwargs),
                    )
                step["skill_revision_attempt"] = retry_count
            synthesis_round = retry_count + 1
            if step.get("skill_synthesis_round") != synthesis_round:
                lifecycle_result = await self.skill_lifecycle.ensure_for_step(
                    execution_id, profile, step, self._evolution_capabilities(),
                    lambda **kwargs: self._completion_with_recovery(execution_id, **kwargs),
                )
                step["skill_synthesis_round"] = synthesis_round
                step["skill_lifecycle_status"] = {
                    key: lifecycle_result.get(key) for key in
                    ("created", "reused", "rejected", "budget_exhausted") if key in lifecycle_result
                }
                requested.update(lifecycle_result.get("skill_names", []))
        for skill_name in sorted(requested):
            self.agent_profile_registry.attach_skill(profile["id"], skill_name)
        step["autonomous_skill_names"] = sorted(requested)
        state.variables.setdefault("agent_profiles", {})[str(step.get("id"))] = profile["id"]
        await self.event_bus.publish(Event(type="AutonomousSpecializationPrepared", payload={
            "execution_id": execution_id, "step_id": step.get("id"), "profile_id": profile["id"],
            "skill_names": sorted(requested), "skill_created": bool(lifecycle_result.get("created")),
        }))
        return {"profile": profile, "skill_names": sorted(requested), "lifecycle": lifecycle_result}

    def _record_specialization_outcome(self, execution_id: str, step: Dict[str, Any],
                                       success: bool, feedback: str = "") -> None:
        profile_id = str(step.get("agent_profile_id") or "")
        if profile_id and self.agent_profile_registry:
            self.agent_profile_registry.record_outcome(profile_id, success)
        if profile_id and self.skill_lifecycle:
            self.skill_lifecycle.record_outcome(
                execution_id, profile_id, step.get("autonomous_skill_names", []), success, feedback,
            )

    def _allowed_capabilities(self, skills) -> Optional[set[str]]:
        # Skills describe useful procedures. They only become capability
        # sandboxes when the operator explicitly opts into strict mode.
        if not skills or not self.strict_skill_capabilities:
            return None
        return set().union(*(set(skill.allowed_capabilities) for skill in skills))

    def _step_stagnation_budget(self, task: str, step: Dict[str, Any]) -> int:
        self.runtime_policy.baseline_stagnation_iterations = self.max_step_iterations
        self.runtime_policy.adaptive = self.adaptive_resource_management
        return self.runtime_policy.stagnation_budget(task, step)

    def _step_retry_budget(self, task: str, step: Dict[str, Any]) -> int:
        self.runtime_policy.baseline_retries = self.max_step_retries
        self.runtime_policy.adaptive = self.adaptive_resource_management
        return self.runtime_policy.retry_budget(task, step)

    @staticmethod
    def _cached_approval_decision(state, capability: str, action: str, arguments: Dict[str, Any]):
        fingerprint = tool_call_fingerprint(capability, action, arguments)
        cached = state.variables.get("approval_decisions", {}).get(fingerprint)
        if cached in ("allow", "reject"):
            return fingerprint, PolicyDecision(
                decision="allow" if cached == "allow" else "deny",
                reason="Reused the human decision for this exact normalized action.",
                details={"cached_human_decision": True},
            )
        return fingerprint, None

    def _artifact_exists(self, execution_id: str, path: str) -> bool:
        filesystem = self.get_capability("filesystem")
        if not filesystem or not hasattr(filesystem, "_resolve_path"):
            return False
        try:
            resolved = filesystem._resolve_path(path, execution_id)
            return os.path.isfile(resolved) and os.path.getsize(resolved) > 0
        except (OSError, PermissionError, ValueError):
            return False

    def _missing_artifacts(self, execution_id: str, step: Dict[str, Any]) -> List[str]:
        return [path for path in step.get("required_artifacts", [])
                if not self._artifact_exists(execution_id, path)]

    def _step_artifact_validation_issues(
        self, execution_id: str, step: Dict[str, Any]
    ) -> List[str]:
        """Validate each completed step artifact before allowing downstream reuse."""
        state = self.state_engine.get_execution(execution_id)
        specifications = {
            str(item.get("path") or "").replace("\\", "/"): item
            for item in (state.current_plan or {}).get("artifact_validations", [])
            if isinstance(item, dict) and item.get("path")
        }
        filesystem = self.get_capability("filesystem")
        if not filesystem or not hasattr(filesystem, "_resolve_path"):
            return []
        issues = []
        for path in step.get("required_artifacts", []):
            normalized = str(path).replace("\\", "/")
            if not self._artifact_exists(execution_id, normalized):
                continue
            specification = specifications.get(normalized, {})
            suffix = os.path.splitext(normalized)[1].lower()
            validator = specification.get("validator")
            constraints = dict(specification.get("constraints") or {})
            # Even a planner without an explicit policy must not advance a
            # generated text artifact containing placeholders or model-thought tags.
            if not specification and suffix in {".md", ".txt", ".html"}:
                validator = "document"
                constraints["forbid_placeholders"] = True
            try:
                resolved = filesystem._resolve_path(normalized, execution_id)
                report = validate_artifact(
                    resolved, validator=validator, constraints=constraints,
                )
            except (OSError, PermissionError, TypeError, ValueError) as error:
                issues.append(f"{normalized}: validation could not run: {error}")
                continue
            if not report.get("valid", False):
                failures = report.get("failures") or ["artifact validation failed"]
                issues.extend(f"{normalized}: {message}" for message in failures[:12])
        return issues

    def _document_coverage_issues(
        self, execution_id: str, step: Dict[str, Any]
    ) -> List[str]:
        """Require tool evidence for assignments claiming exhaustive corpus inventory."""
        state = self.state_engine.get_execution(execution_id)
        attached = {
            str(item) for item in state.variables.get("attachment_ids", []) if item
        }
        if not attached or not self.artifact_store:
            return []
        assignment = " ".join([
            str(step.get("description") or ""),
            " ".join(str(item) for item in step.get("acceptance_criteria", [])),
        ]).casefold()
        inventory_markers = ("inventory", "inventor", "inventaire")
        exhaustive_markers = ("every", "all ", "complete", "exhaust", "integr")
        exhaustive_assignment = any(
            marker in assignment for marker in exhaustive_markers
        ) or bool(re.search(r"\bint.gr", assignment))
        if not (
            any(marker in assignment for marker in inventory_markers)
            and exhaustive_assignment
        ):
            return []
        covered: Dict[str, set[int]] = {artifact_id: set() for artifact_id in attached}
        history = state.variables.get("tool_call_history", [])
        for item in history:
            if item.get("capability") != "documents" or item.get("action") != "read":
                continue
            try:
                payload = json.loads(str(item.get("result") or ""))
            except (TypeError, ValueError):
                continue
            artifact_id = str(payload.get("artifact_id") or "")
            if artifact_id not in covered:
                continue
            for block in payload.get("blocks") or []:
                try:
                    covered[artifact_id].add(int(block["order"]))
                except (KeyError, TypeError, ValueError):
                    continue
        issues = []
        inventory = {
            str(item.get("artifact_id")): item
            for item in self.artifact_store.document_index.inventory()
            if str(item.get("artifact_id")) in attached
        }
        for artifact_id in sorted(attached):
            item = inventory.get(artifact_id, {})
            try:
                total = int(item.get("block_count") or len(
                    self.artifact_store.document(artifact_id).blocks
                ))
            except (OSError, KeyError, TypeError, ValueError):
                issues.append(f"prove complete document coverage for attachment {artifact_id}")
                continue
            missing = sorted(set(range(total)) - covered.get(artifact_id, set()))
            if missing:
                display = ", ".join(str(index + 1) for index in missing[:20])
                if len(missing) > 20:
                    display += f", and {len(missing) - 20} more"
                filename = str(item.get("filename") or artifact_id)
                issues.append(
                    f"read every normalized block of {filename}; "
                    f"missing 1-based block(s): {display}"
                )
        return issues

    def _capability_gaps(self, state) -> List[Dict[str, Any]]:
        """Describe unavailable input modalities without pretending to use them."""
        gaps = []
        if not self.artifact_store:
            return gaps
        image_attachments = []
        for artifact_id in state.variables.get("attachment_ids", []):
            try:
                metadata = self.artifact_store.get(artifact_id)
            except (ValueError, FileNotFoundError, OSError, KeyError):
                continue
            if str(metadata.get("content_type") or "").startswith("image/"):
                image_attachments.append(metadata.get("filename"))
        if image_attachments and not getattr(self.llm_provider, "supports_vision", False):
            gaps.append({
                "capability": "vision",
                "required_for": "Interpret attached image content",
                "inputs": image_attachments,
                "available": False,
                "resolution": (
                    "Configure a vision-capable provider, or restrict execution to "
                    "documented adapters, configuration, routines, and validators."
                ),
            })
        return gaps

    def _progress_signature(self, execution_id: str, step: Dict[str, Any]) -> tuple:
        """Fingerprint durable work without counting repeated reads or failed commands."""
        filesystem = self.get_capability("filesystem")
        files = []
        if filesystem and hasattr(filesystem, "_get_workspace_for_execution"):
            try:
                root = filesystem._get_workspace_for_execution(execution_id)
                ignored_directories = {".git", ".pytest_cache", "__pycache__", "node_modules", ".mypy_cache"}
                for directory, directory_names, filenames in os.walk(root):
                    directory_names[:] = sorted(
                        name for name in directory_names if name not in ignored_directories
                    )
                    for filename in sorted(filenames):
                        if filename.endswith((".pyc", ".pyo")):
                            continue
                        full_path = os.path.join(directory, filename)
                        relative = os.path.relpath(full_path, root).replace(os.sep, "/")
                        basename = filename.lower()
                        if (
                            re.match(r"^(?:tmp|temp)(?:[_-]|\.|$)", basename)
                            or re.match(
                                r"^(?:test|pytest)[_-]?(?:output|results?)(?:[_-].*)?\.",
                                basename,
                            )
                            or basename in {
                                "test_output.txt", "test_results.txt",
                                ".coverage", "coverage.xml", "junit.xml",
                            }
                            or basename.endswith((".tmp", ".bak", ".log"))
                        ):
                            continue
                        digest = hashlib.sha256()
                        text_extensions = {
                            ".py", ".pyi", ".md", ".txt", ".json", ".jsonl",
                            ".yaml", ".yml", ".toml", ".ini", ".cfg", ".html",
                            ".css", ".js", ".ts", ".tsx", ".jsx", ".xml",
                            ".csv", ".sh", ".ps1", ".bat", ".cmd",
                        }
                        if os.path.splitext(filename)[1].lower() in text_extensions:
                            try:
                                with open(
                                    full_path, "r", encoding="utf-8", newline=None
                                ) as source:
                                    while True:
                                        chunk = source.read(1024 * 1024)
                                        if not chunk:
                                            break
                                        digest.update(chunk.encode("utf-8"))
                            except UnicodeDecodeError:
                                digest = hashlib.sha256()
                                with open(full_path, "rb") as source:
                                    while True:
                                        chunk = source.read(1024 * 1024)
                                        if not chunk:
                                            break
                                        digest.update(chunk)
                        else:
                            with open(full_path, "rb") as source:
                                while True:
                                    chunk = source.read(1024 * 1024)
                                    if not chunk:
                                        break
                                    digest.update(chunk)
                        files.append((relative, digest.hexdigest()))
                        if len(files) >= 2_000:
                            break
                    if len(files) >= 2_000:
                        break
            except (OSError, PermissionError, ValueError):
                files = []

        execution_state = self.state_engine.get_execution(execution_id)
        history = execution_state.variables.get("tool_call_history", [])
        role_key = (
            canonical_step_role(step.get("role"))
            or infer_step_role(step.get("description", ""))
        )
        current_commands = requirement_validation_commands(
            (execution_state.current_plan or {}).get("requirements", [])
        )
        inherited_commands = (
            requirement_validation_commands(
                execution_state.variables.get("inherited_requirements", [])
            )
            if role_key in {"developer", "qa", "debugger", "coordinator"}
            else []
        )
        declared_commands = [
            str(command).strip() for command in step.get("verification_commands", [])
            if str(command).strip()
        ]
        declared_commands.extend(
            command for command in [*current_commands, *inherited_commands]
            if command not in declared_commands
        )
        successful_commands = sorted({
            declared
            for item in history
            for declared in declared_commands
            if item.get("capability") == "shell" and item.get("action") == "execute"
            and "EXIT_CODE: 0" in str(item.get("result") or "")
            and commands_equivalent(
                declared, str(item.get("arguments", {}).get("command") or "")
            )
        })
        latest_failure_count = None
        for item in reversed(history):
            if item.get("capability") != "shell" or item.get("action") != "execute":
                continue
            observed_command = str(item.get("arguments", {}).get("command") or "").strip()
            if declared_commands:
                if not any(
                    commands_equivalent(declared, observed_command)
                    for declared in declared_commands
                ):
                    continue
            elif not re.search(
                r"(?i)(?:\bpytest\b|\bunittest\b|\bnpm\s+(?:run\s+)?test\b|"
                r"\bcargo\s+test\b|\bgo\s+test\b|\bdotnet\s+test\b|"
                r"\bmvn(?:\.cmd)?\s+test\b|\bgradle(?:w)?\s+test\b|"
                r"\bcompileall\b|\bruff\s+check\b|\bmypy\b|\btsc\b)",
                observed_command,
            ):
                continue
            result_text = str(item.get("result") or "")
            if "EXIT_CODE: 0" in result_text:
                latest_failure_count = 0
            else:
                counts = [int(value) for value in re.findall(
                    r"(\d+)\s+(?:failed|error|errors|failure|failures)",
                    result_text,
                    flags=re.IGNORECASE,
                )]
                latest_failure_count = sum(counts) if counts else 1_000_000
            break
        return (
            tuple(files),
            tuple(successful_commands),
            tuple(sorted(self._missing_artifacts(execution_id, step))),
            latest_failure_count,
        )

    def _quality_improved(self, execution_id: str, previous: tuple, current: tuple) -> tuple[bool, str]:
        """Reward measurable delivery improvement, with bounded credit for code churn."""
        previous_files = dict(previous[0])
        current_files = dict(current[0])
        new_files = set(current_files) - set(previous_files)
        if new_files:
            return True, "new_artifact"
        if set(current[1]) - set(previous[1]):
            return True, "new_successful_verification"
        if len(current[2]) < len(previous[2]):
            return True, "required_artifact_completed"
        previous_failures = previous[3] if len(previous) > 3 else None
        current_failures = current[3] if len(current) > 3 else None
        if (previous_failures is not None and current_failures is not None
                and current_failures < previous_failures):
            return True, "fewer_machine_failures"

        changed = sorted(
            path for path in set(previous_files) & set(current_files)
            if previous_files[path] != current_files[path]
        )
        if changed:
            state = self.state_engine.get_execution(execution_id)
            credits = state.variables.setdefault("quality_edit_credits", {})
            credited = [path for path in changed if int(credits.get(path, 0)) < 2]
            if credited:
                for path in credited:
                    credits[path] = int(credits.get(path, 0)) + 1
                return True, "bounded_productive_edit"
        return False, "no_quality_delta"

    @staticmethod
    def _normalize_tool_arguments(capability: str, action: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Recover common local-LLM wrappers and aliases before dispatch."""
        normalized = dict(arguments or {})
        wrappers = ("arguments", "parameters", "kwargs", "input", "data")
        while len(normalized) == 1:
            wrapper_name, wrapper_value = next(iter(normalized.items()))
            if wrapper_name not in wrappers or not isinstance(wrapper_value, dict):
                break
            normalized = dict(wrapper_value)

        if capability.lower() == "shell" and action.lower() == "execute":
            if "command" not in normalized:
                for alias in ("cmd", "shell_command", "script"):
                    if normalized.get(alias):
                        normalized["command"] = normalized.pop(alias)
                        break
            for workspace_hint in ("path", "cwd", "working_directory", "workdir"):
                normalized.pop(workspace_hint, None)

        if capability.lower() == "filesystem":
            if "path" not in normalized:
                for alias in ("file_path", "filepath", "filename", "file"):
                    if normalized.get(alias):
                        normalized["path"] = normalized.pop(alias)
                        break
            if action.lower() == "write" and "content" not in normalized:
                for alias in ("text", "source", "body"):
                    if alias in normalized:
                        normalized["content"] = normalized.pop(alias)
                        break
            if action.lower() == "write" and not normalized.get("path"):
                content = normalized.get("content")
                if isinstance(content, str):
                    first_line = content.strip().splitlines()[0] if content.strip() else ""
                    candidate = first_line.removeprefix("File:").strip().strip(chr(34) + chr(39) + "`")
                    if re.fullmatch(r"[\w .()/-]+\.[A-Za-z0-9]{1,10}", candidate.replace(chr(92), "/")):
                        normalized["path"] = candidate
                        normalized["content"] = ExecutionEngine._strip_code_fence(
                            "\n".join(content.strip().splitlines()[1:]), candidate,
                        )
        return normalized

    def _fake_dependency_packages(self, execution_id: str) -> List[str]:
        filesystem = self.get_capability("filesystem")
        if not filesystem or not hasattr(filesystem, "_get_workspace_for_execution"):
            return []
        root = filesystem._get_workspace_for_execution(execution_id)
        try:
            from importlib.metadata import packages_distributions
            installed_imports = set(packages_distributions())
        except (ImportError, OSError):
            installed_imports = set()
        installed_imports.update({"numpy", "torch", "cv2", "trimesh", "PIL", "scipy"})
        ignored_project_packages = {
            "test", "tests", "testing", "docs", "doc", "examples", "scripts", "tools",
        }
        return sorted(
            entry.name for entry in os.scandir(root)
            if entry.is_dir()
            and entry.name.lower() not in ignored_project_packages
            and not entry.name.startswith(".")
            and entry.name in installed_imports
            and os.path.isfile(os.path.join(entry.path, "__init__.py"))
        )

    def _integration_contract_issues(self, execution_id: str) -> List[str]:
        """Detect package-layout defects that can create duplicate Python class identities."""
        filesystem = self.get_capability("filesystem")
        if not filesystem or not hasattr(filesystem, "_get_workspace_for_execution"):
            return []
        try:
            root = filesystem._get_workspace_for_execution(execution_id)
        except (OSError, PermissionError, ValueError):
            return []
        issues = []
        collisions = []
        for directory, directory_names, filenames in os.walk(root):
            directory_names[:] = [
                name for name in directory_names
                if name not in {"__pycache__", ".pytest_cache", ".git"}
            ]
            modules = {
                os.path.splitext(filename)[0]
                for filename in filenames
                if filename.endswith(".py") and filename != "__init__.py"
            }
            packages = {
                name for name in directory_names
                if os.path.isfile(os.path.join(directory, name, "__init__.py"))
            }
            for name in sorted(modules & packages):
                relative = os.path.relpath(
                    os.path.join(directory, name), root
                ).replace(os.sep, "/")
                collisions.append(relative)
        if collisions:
            issues.append(
                "resolve Python module/package name collisions that make imports ambiguous: "
                + ", ".join(collisions[:20])
            )

        source_root = os.path.join(root, "src")

        packages = sorted(
            name for name in os.listdir(source_root)
            if os.path.isdir(os.path.join(source_root, name))
            and os.path.isfile(os.path.join(source_root, name, "__init__.py"))
        ) if os.path.isdir(source_root) else []
        invalid_imports = []
        for directory, directory_names, filenames in os.walk(root):
            directory_names[:] = [name for name in directory_names if name not in {"__pycache__", ".pytest_cache"}]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                full_path = os.path.join(directory, filename)
                try:
                    with open(full_path, "r", encoding="utf-8") as source:
                        content = source.read()
                except (OSError, UnicodeError):
                    continue
                if any(
                    re.search(rf"(?:from|import)\s+src\.{re.escape(package)}\b", content)
                    for package in packages
                ):
                    invalid_imports.append(os.path.relpath(full_path, root).replace(os.sep, "/"))
        if invalid_imports:
            issues.append(
                "replace src.<package> imports with the canonical installed package identity in: "
                + ", ".join(sorted(invalid_imports)[:20])
            )

        pytest_path = os.path.join(root, "pytest.ini")
        if os.path.isfile(pytest_path):
            try:
                with open(pytest_path, "r", encoding="utf-8") as config_file:
                    pytest_config = config_file.read()
                if re.search(r"(?m)^\s*python_paths\s*=", pytest_config):
                    issues.append("replace unsupported pytest.ini option python_paths with pythonpath")
                testpaths_match = re.search(
                    r"(?m)^\s*testpaths\s*=\s*(.+)$", pytest_config
                )
                if testpaths_match:
                    configured_paths = {
                        token.replace("\\", "/").strip("/")
                        for token in shlex.split(testpaths_match.group(1), posix=False)
                        if token.strip()
                    }
                    discovered_directories = set()
                    for directory, directory_names, filenames in os.walk(root):
                        directory_names[:] = [
                            name for name in directory_names
                            if name not in {"__pycache__", ".pytest_cache", ".git"}
                        ]
                        if any(
                            filename.startswith("test_") and filename.endswith(".py")
                            for filename in filenames
                        ):
                            relative = os.path.relpath(directory, root).replace(os.sep, "/")
                            discovered_directories.add(relative)
                    excluded = sorted(
                        directory for directory in discovered_directories
                        if not any(
                            directory == configured
                            or directory.startswith(configured + "/")
                            for configured in configured_paths
                        )
                    )
                    if excluded:
                        issues.append(
                            "expand pytest.ini testpaths so no discovered validation suite is hidden: "
                            + ", ".join(excluded[:20])
                        )
            except (OSError, UnicodeError):
                pass
        return issues

    @staticmethod
    def _strip_code_fence(content: str, path: str = "") -> str:
        text = str(content or "").strip()
        suffix = os.path.splitext(path)[1].lower()
        if suffix != ".md" and "```" in text:
            fenced = re.findall(r"```[^\r\n]*\r?\n(.*?)\r?\n```", text, flags=re.DOTALL)
            if fenced:
                text = max(fenced, key=len).strip()
        elif text.startswith("```"):
            first_newline = text.find("\n")
            if first_newline >= 0:
                text = text[first_newline + 1:]
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
        lines = text.strip().splitlines()
        if lines and path and lines[0].strip().replace(chr(92), "/") == path.replace(chr(92), "/"):
            lines.pop(0)
        return "\n".join(lines).strip() + "\n"

    def _source_contract_summary(self, execution_id: str) -> str:
        filesystem = self.get_capability("filesystem")
        if not filesystem or not hasattr(filesystem, "_get_workspace_for_execution"):
            return ""
        root = filesystem._get_workspace_for_execution(execution_id)
        summaries = []
        source_root = os.path.join(root, "src")
        if not os.path.isdir(source_root):
            return ""
        for directory, _, filenames in os.walk(source_root):
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                full_path = os.path.join(directory, filename)
                relative = os.path.relpath(full_path, root).replace(os.sep, "/")
                try:
                    with open(full_path, "r", encoding="utf-8") as source_file:
                        tree = ast.parse(source_file.read())
                except (OSError, UnicodeError, SyntaxError):
                    continue
                def signature(node):
                    positional = list(node.args.posonlyargs) + list(node.args.args)
                    defaults = [None] * (len(positional) - len(node.args.defaults)) + list(node.args.defaults)
                    parameters = []
                    for argument, default in zip(positional, defaults):
                        parameter = argument.arg
                        if default is not None:
                            parameter += "=" + ast.unparse(default)[:80]
                        parameters.append(parameter)
                    if node.args.vararg:
                        parameters.append("*" + node.args.vararg.arg)
                    elif node.args.kwonlyargs:
                        parameters.append("*")
                    for argument, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                        parameter = argument.arg
                        if default is not None:
                            parameter += "=" + ast.unparse(default)[:80]
                        parameters.append(parameter)
                    if node.args.kwarg:
                        parameters.append("**" + node.args.kwarg.arg)
                    return node.name + "(" + ", ".join(parameters) + ")"

                entries = []
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        entries.append("function " + signature(node))
                    elif isinstance(node, ast.ClassDef):
                        methods = [signature(child) for child in node.body
                                   if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and not child.name.startswith("__")]
                        entries.append("class " + node.name + " methods=" + ",".join(methods[:20]))
                summaries.append(relative + ": " + "; ".join(entries))
        return "\n".join(summaries)[:6000]

    @staticmethod
    def _rescue_content_issues(path: str, content: str) -> List[str]:
        issues = []
        suffix = os.path.splitext(path)[1].lower()
        if suffix == ".py":
            try:
                ast.parse(content)
            except SyntaxError as exc:
                issues.append(f"invalid Python syntax at line {exc.lineno}: {exc.msg}")
        normalized_path = path.replace(chr(92), "/").lower()
        if normalized_path.startswith("tests/") or "/tests/" in normalized_path:
            lower = content.lower()
            if re.search(r"(?:from|import)\s+src\.[a-z_]\w*", lower):
                issues.append("tests import src.<package> instead of the canonical package identity")
            if any(marker in lower for marker in ("mockmesh", "magicmock", "unittest.mock", "# mocking", "np.random")):
                issues.append("tests contain mocks, replicated implementation, or random data")
            if "def test_" not in lower:
                issues.append("test file contains no pytest test function")
        geometry_markers = re.search(r"\b(?:mesh|geometry|vertex|vertices|face|faces|topology)\b", content, re.IGNORECASE)
        if geometry_markers:
            if re.search(r"\b(?:np\.random|numpy\.random|random\.random|random\.randint)\b", content):
                issues.append("geometry implementation uses random output")
        return issues

    async def _rescue_missing_artifacts(self, execution_id: str, step: Dict[str, Any],
                                        prerequisite_outputs: List[Dict[str, Any]]) -> List[str]:
        """Use a clean, concise LLM context when a tool loop stalls before creating files."""
        state = self.state_engine.get_execution(execution_id)
        missing = self._missing_artifacts(execution_id, step)
        text_suffixes = {".py", ".md", ".txt", ".json", ".html", ".css", ".js",
                         ".toml", ".yaml", ".yml", ".bat", ".ps1", ".sh"}
        candidates = [path for path in missing if os.path.splitext(path)[1].lower() in text_suffixes][:4]
        if state.variables.get("attachment_ids"):
            # A detached rescue prompt does not contain the complete attached
            # corpus and therefore cannot honestly synthesize grounded prose.
            source_document_suffixes = {".md", ".txt", ".json", ".html"}
            candidates = [
                path for path in candidates
                if os.path.splitext(path)[1].lower() not in source_document_suffixes
            ]
        contracts = self._source_contract_summary(execution_id)
        rescued = []
        for path in candidates:
            rescue_messages = [
                {"role": "system", "content": (
                    "You are GPTMOSS's artifact rescue engineer. Return only the complete raw file content requested: "
                    "no markdown fence, no explanation, no placeholder, no TODO. The file must be runnable, dependency-light, "
                    "deterministic, and honest about unavailable external models."
                )},
                {"role": "user", "content": (
                    f"Main outcome: {state.variables.get('parent_task', state.variables.get('task', ''))}\n"
                    f"Specialist: {step.get('specialist', state.variables.get('role_name', ''))}\n"
                    f"Assignment: {step.get('description', '')}\n"
                    f"Expertise: {json.dumps(step.get('expertise', []), ensure_ascii=False)}\n"
                    f"Acceptance criteria: {json.dumps(step.get('acceptance_criteria', []), ensure_ascii=False)}\n"
                    f"All required files: {json.dumps(step.get('required_artifacts', []), ensure_ascii=False)}\n"
                    f"Generate this missing file now: {path}\n"
                    f"Actual neighboring source contracts (import and test these; do not replicate or mock them):\n{contracts}\n"
                    f"Prerequisite delivery summaries:\n{json.dumps(prerequisite_outputs, ensure_ascii=False)[:3000]}\n"
                    "It must integrate with the stated neighboring modules through clear contracts."
                )},
            ]
            content = ""
            for attempt in range(2):
                await self.event_bus.publish(Event(
                    type="ArtifactRescueRequested",
                    payload={"execution_id": execution_id, "path": path, "attempt": attempt + 1},
                ))
                response = await self._completion_with_recovery(
                    execution_id, messages=rescue_messages, temperature=0.1,
                )
                content = self._strip_code_fence(response.get("content", ""), path)
                content_issues = self._rescue_content_issues(path, content)
                if len(content.strip()) >= 20 and not content_issues:
                    break
                content = ""
                rescue_messages.append({
                    "role": "user",
                    "content": "Regenerate the complete file. Previous output was rejected: " + "; ".join(content_issues or ["content was empty or too short"]),
                })
            if not content:
                continue
            policy = await self.policy_provider.check_action(
                execution_id=execution_id, capability="filesystem", action="write",
                arguments={"path": path, "content": content}, context={"artifact_rescue": True},
            )
            if policy.decision != "allow":
                continue
            result = await self._call_tool(execution_id, "filesystem", "write", {"path": path, "content": content})
            self._record_tool_result(execution_id, "filesystem", "write", {"path": path}, result)
            if self._artifact_exists(execution_id, path):
                rescued.append(path)
                await self.event_bus.publish(Event(
                    type="ArtifactRescued", payload={"execution_id": execution_id, "path": path},
                ))
        return rescued

    @staticmethod
    def _is_structured_delivery(response: str) -> bool:
        text = str(response or "").strip()
        candidates = [text]
        first, last = text.find("{"), text.rfind("}")
        if first >= 0 and last > first:
            candidates.append(text[first:last + 1])
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict) and all(
                key in parsed for key in ("summary", "artifacts", "evidence", "risks", "next_action")
            ):
                return True
        return False

    def _step_completion_issues(self, execution_id: str, step: Dict[str, Any], response: str) -> List[str]:
        """Evaluate machine-checkable delivery gates before accepting prose as completion."""
        issues = []
        execution_state = self.state_engine.get_execution(execution_id)
        current_requirements = (
            (execution_state.current_plan or {}).get("requirements", [])
        )
        history = execution_state.variables.get("tool_call_history", [])
        quality_contract = bool(
            step.get("specialist") or step.get("required_artifacts")
            or step.get("acceptance_criteria") or step.get("verification_commands")
        )
        role_key = canonical_step_role(step.get("role")) or infer_step_role(step.get("description", ""))
        if role_key == "coordinator":
            # Final coordinators validate the whole workflow.  Successful
            # machine evidence normally belongs to delegated QA/integration
            # executions, so limiting the gate to the coordinator's local
            # history needlessly forces expensive duplicate test runs.
            history = self._delivery_histories(execution_id)
        if quality_contract and role_key != "coordinator" and not self._is_structured_delivery(response):
            issues.append("return the required structured JSON delivery contract")

        missing = [path for path in step.get("required_artifacts", [])
                   if not self._artifact_exists(execution_id, path)]
        if missing:
            issues.append("create non-empty required artifacts: " + ", ".join(missing))
        issues.extend(self._document_coverage_issues(execution_id, step))
        issues.extend(self._step_artifact_validation_issues(execution_id, step))

        if role_key in {"qa", "debugger", "coordinator"}:
            fake_packages = self._fake_dependency_packages(execution_id)
            if fake_packages:
                issues.append(
                    "remove local packages impersonating third-party dependencies and use real code contracts: "
                    + ", ".join(fake_packages)
                )
            issues.extend(self._integration_contract_issues(execution_id))

        required_commands = [
            str(command).strip() for command in step.get("verification_commands", [])
            if str(command).strip()
        ]
        # Task-level acceptance commands belong to implementation and
        # validation work.  Planning, security, and documentation specialists
        # must not be forced to make a knowingly failing final suite pass
        # before the dependent implementation steps have even started.
        if role_key in {None, "developer", "qa", "debugger", "coordinator"}:
            required_commands.extend(
                command for command in requirement_validation_commands(current_requirements)
                if command not in required_commands
            )
        if role_key in {"developer", "qa", "debugger", "coordinator"}:
            required_commands.extend(
                command for command in requirement_validation_commands(
                    execution_state.variables.get("inherited_requirements", [])
                )
                if command not in required_commands
            )
        if (
            execution_state.variables.get("parent_execution_id")
            and requirements_request_mutation(current_requirements)
        ):
            durable_mutation = any(
                (
                    item.get("capability") == "filesystem"
                    and item.get("action") in {"write", "delete"}
                    and "Error" not in str(item.get("result") or "")
                )
                or (
                    item.get("capability") == "shell"
                    and item.get("action") == "execute"
                    and "EXIT_CODE: 0" in str(item.get("result") or "")
                    and self._shell_mutation_paths(
                        str(item.get("arguments", {}).get("command") or "")
                    )
                )
                for item in history
            )
            if not durable_mutation:
                issues.append(
                    "make at least one durable filesystem mutation required by the assignment"
                )
        if required_commands:
            missing_commands = []
            for command in required_commands:
                matched = any(
                    item.get("capability") == "shell" and item.get("action") == "execute"
                    and commands_equivalent(
                        command,
                        str(item.get("arguments", {}).get("command") or ""),
                    )
                    and "EXIT_CODE: 0" in str(item.get("result") or "")
                    for item in history
                )
                if not matched:
                    missing_commands.append(command)
            if missing_commands:
                issues.append(
                    "run required exact verification command(s) successfully from project_workspace: "
                    + ", ".join(missing_commands)
                )
        return issues

    def _record_tool_result(self, execution_id: str, capability: str, action: str,
                            arguments: Dict[str, Any], result: str) -> None:
        state = self.state_engine.get_execution(execution_id)
        state.variables.setdefault("tool_call_history", []).append({
            "capability": capability.lower(), "action": action.lower(),
            "arguments": dict(arguments), "result": str(result),
        })

    def _can_engine_finalize(self, execution_id: str, step: Dict[str, Any]) -> bool:
        """Detect converged work even when a model keeps calling tools or formats its finale badly."""
        role_key = canonical_step_role(step.get("role")) or infer_step_role(step.get("description", ""))
        if role_key == "coordinator":
            state = self.state_engine.get_execution(execution_id)
            plan_steps = list((state.current_plan or {}).get("steps") or [])
            step_id = step.get("id")
            is_terminal = not any(
                step_id in (candidate.get("dependencies") or [])
                for candidate in plan_steps
                if candidate.get("id") != step_id
            )
            predecessors_complete = all(
                candidate.get("status") == "completed"
                for candidate in plan_steps
                if candidate.get("id") != step_id
            )
            if not is_terminal or not predecessors_complete:
                return False
            assurance = self._independent_delivery_report(execution_id, plan_steps)
            if not assurance.get("passed", False):
                return False
        valid_contract = json.dumps({
            "summary": "checked", "artifacts": [], "evidence": [], "risks": [], "next_action": "",
        })
        if self._step_completion_issues(execution_id, step, valid_contract):
            return False
        if role_key in {"developer", "qa", "debugger"}:
            history = self.state_engine.get_execution(execution_id).variables.get("tool_call_history", [])
            shell_results = [item for item in history
                             if item.get("capability") == "shell" and item.get("action") == "execute"]
            if not shell_results or "EXIT_CODE: 0" not in str(shell_results[-1].get("result") or ""):
                return False
        return True

    def _engine_delivery(self, execution_id: str, step: Dict[str, Any]) -> str:
        state = self.state_engine.get_execution(execution_id)
        artifacts = list(step.get("required_artifacts", []))
        evidence = [f"verified non-empty artifact: {path}" for path in artifacts]
        for item in state.variables.get("tool_call_history", []):
            if item.get("capability") == "shell" and "EXIT_CODE: 0" in str(item.get("result") or ""):
                evidence.append(
                    "EXIT_CODE: 0 for " + str(item.get("arguments", {}).get("command") or "shell command")
                )
        role_key = canonical_step_role(step.get("role")) or infer_step_role(step.get("description", ""))
        if role_key == "coordinator":
            assurance = self._independent_delivery_report(
                execution_id, list((state.current_plan or {}).get("steps") or [])
            )
            evidence.extend(
                f"independent assurance passed: {check.get('name')}"
                for check in assurance.get("checks", [])
                if check.get("passed")
            )
        return json.dumps({
            "summary": (
                "GPTMOSS independently verified the completed workflow and its delivery contract."
                if role_key == "coordinator" else
                "GPTMOSS verified the specialist's converged workspace delivery after tool execution."
            ),
            "artifacts": artifacts, "evidence": evidence[-8:],
            "risks": ["The specialist did not return a clean final contract; GPTMOSS synthesized it from machine evidence."],
            "next_action": (
                "Deliver the independently assured result."
                if role_key == "coordinator" else
                "Validate this delivery in its dependent integration and acceptance steps."
            ),
        }, ensure_ascii=False)

    def _delivery_histories(self, execution_id: str) -> List[Dict[str, Any]]:
        return self.delivery_coordinator.histories(execution_id)

    def _delivery_workspace(self, execution_id: str) -> Optional[str]:
        return self.delivery_coordinator.workspace(execution_id)

    def _initialize_document_state(self, execution_id: str, task: str, plan: Dict[str, Any], state) -> None:
        """Create/resume the provider-neutral long-document checkpoint."""
        if not self.document_engine_enabled or not self.document_checkpoint_enabled:
            return
        if not any(marker in str(task).casefold() for marker in (
            "dossier", "rapport", "livrable", "long-form", "document-analysis",
            "rédige", "redige", "write a document", "professional document",
        )):
            return
        workspace = self._delivery_workspace(execution_id)
        if not workspace:
            return
        checkpoint_root = os.path.join(workspace, ".gptmoss", "document-state")
        engine = LongDocumentEngine(checkpoint_root)
        model = engine.resume(execution_id)
        if model is None:
            primary = str(plan.get("primary_artifact") or "deliverable.md")
            model = engine.create_model(execution_id, task, os.path.join(workspace, primary), plan.get("requirements", []))
            headings: list[str] = []
            for policy in plan.get("artifact_validations", []):
                if policy.get("path") == primary:
                    headings = [str(item) for item in policy.get("constraints", {}).get("required_headings", [])]
                    break
            if not headings:
                headings = [
                    str(step.get("specialist") or f"Section {index}")
                    for index, step in enumerate(plan.get("steps", []), 1)
                    if step.get("role") in {"architect", "security", "writer"}
                ]
            engine.plan_sections(
                model,
                headings or ["Executive Summary", "Architecture", "Conclusion"],
                requirements=plan.get("requirements", []),
                target_words=self.document_target_section_words,
            )
        state.variables["document_model_checkpoint"] = str(engine.store.path_for(execution_id))
        state.variables["document_sections"] = [
            {
                "section_id": section.contract.section_id,
                "heading": section.contract.heading,
                "status": section.contract.status,
                "word_count": section.word_count,
            }
            for section in model.sections
        ]
        state.variables["document_memory"] = engine.memory(model).as_prompt()

    def _sync_document_checkpoint(self, execution_id: str, state) -> None:
        """Reconcile written Markdown into section checkpoints after each step."""
        checkpoint = state.variables.get("document_model_checkpoint")
        if not checkpoint:
            return
        try:
            checkpoint_path = Path(str(checkpoint)).resolve()
            engine = LongDocumentEngine(checkpoint_path.parent)
            model = engine.resume(execution_id)
            if model is None:
                return
            output = Path(model.output_path)
            if not output.is_file():
                return
            markdown = output.read_text(encoding="utf-8")
            matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", markdown))
            for index, match in enumerate(matches):
                heading = match.group(1).strip()
                end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
                section = next((item for item in model.sections if item.contract.heading.casefold() == heading.casefold()), None)
                if section is not None:
                    section.record(markdown[match.end():end].strip())
            model.status = "complete" if model.sections and all(item.content for item in model.sections) else "writing"
            model.revision += 1
            engine.store.save(model)
            state.variables["document_sections"] = [
                {"section_id": item.contract.section_id, "heading": item.contract.heading,
                 "status": item.contract.status, "word_count": item.word_count}
                for item in model.sections
            ]
            state.variables["document_memory"] = engine.memory(model).as_prompt()
        except (OSError, ValueError, UnicodeError):
            logger.debug("Unable to synchronize document checkpoint", exc_info=True)

    def _independent_delivery_report(self, execution_id: str, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.delivery_coordinator.assurance(execution_id, steps)

    async def execute_task(self, execution_id: str, task: str):
        """Run an execution once, even if resume/reconnect schedules it repeatedly."""
        lock = self._execution_locks.setdefault(execution_id, asyncio.Lock())
        if lock.locked():
            self.telemetry.record("duplicate_execution_skipped", execution_id, task=task)
            return
        async with lock:
            state = self.state_engine.get_execution(execution_id)
            if state.status in ("completed", "failed", "cancelled"):
                return
            try:
                await self._execute_task_unlocked(execution_id, task)
            except asyncio.CancelledError:
                raise
            except ProviderUnavailableError as exc:
                self.state_engine.transition_execution(
                    state, "waiting_provider", reason="provider unavailable", actor="runtime"
                )
                state.variables["provider_wait"] = {
                    "error": str(exc.original_error),
                    "error_type": exc.original_error.__class__.__name__,
                    "suspended_at": time.time(),
                }
                state.results.pop("error", None)
                self.state_engine.save_to_disk()
                self.telemetry.record("execution_waiting_provider", execution_id)
                await self.event_bus.publish(Event(
                    type="ExecutionWaitingProvider",
                    payload={
                        "execution_id": execution_id,
                        "error_type": exc.original_error.__class__.__name__,
                    },
                ))
                self._schedule_provider_resume(execution_id)
            except Exception as exc:
                self.state_engine.transition_execution(
                    state, "failed", reason="unhandled execution error", actor="runtime"
                )
                state.results["error"] = str(exc)
                self.telemetry.record("execution_failed", execution_id, error=str(exc))
                await self.event_bus.publish(Event(
                    type="ExecutionFailed",
                    payload={"execution_id": execution_id, "error": str(exc)},
                ))

    async def _execute_task_unlocked(self, execution_id: str, task: str):
        """
        Main execution loop for a task.
        """
        state = self.state_engine.get_execution(execution_id)
        convo = self.state_engine.get_conversation(execution_id)
        state.variables.setdefault("task", task)
        self.telemetry.record("execution_started", execution_id, task=task)
        skills = self._active_skills(state, task)
        allowed_capabilities = self._allowed_capabilities(skills)
        state.variables["capability_gaps"] = self._capability_gaps(state)

        # 1. Initialize states if new
        if state.status == "pending":
            self.state_engine.transition_execution(
                state, "running", reason="execution started", actor="runtime"
            )
            
            parent_task = state.variables.get("parent_task")
            if not parent_task:
                parent_id = state.variables.get("parent_execution_id")
                if parent_id and parent_id in self.state_engine.executions:
                    parent_exec = self.state_engine.get_execution(parent_id)
                    parent_task = parent_exec.variables.get("parent_task")
            if not parent_task:
                parent_task = task
                
            state.variables["parent_task"] = parent_task
            
            await self.event_bus.publish(Event(
                type="ExecutionStarted",
                payload={"execution_id": execution_id, "task": task}
            ))
            
            # Do not duplicate the original assignment when a persisted child is
            # normalized from running to pending during process recovery.
            if not convo.messages:
                if parent_task and parent_task != task:
                    convo.messages.append({
                        "role": "user",
                        "content": f"Main Project Task: {parent_task}\nYour Specific Subtask: {task}",
                        "timestamp": time.time()
                    })
                else:
                    convo.messages.append({
                        "role": "user", "content": f"Task: {task}", "timestamp": time.time()
                    })

        # 2. Plan generation (if not already planned)
        if not state.current_plan:
            is_sub_agent = state.variables.get("parent_execution_id") is not None
            schemas = self.get_capabilities_schemas(
                is_sub_agent=is_sub_agent,
                allowed_capabilities=allowed_capabilities,
                delegation_depth=int(state.variables.get("delegation_depth", 0)),
            )
            context = await self.context_engine.compile_context(
                execution_id=execution_id,
                conversation_id=execution_id,
                agent_id="default_agent",
                capabilities_schemas=schemas,
                extra_query=task
            )
            context["skills"] = [{"name": skill.name, "description": skill.description} for skill in skills]
            await self.event_bus.publish(Event(
                type="ContextBuilt",
                payload={"execution_id": execution_id, "context_summary": "Initial context compiled."}
            ))

            planning_started = time.perf_counter()
            try:
                plan_result = await self.planner.plan(
                    task, context, schemas,
                    parent_execution_id=state.variables.get("parent_execution_id"),
                    delegated_step=state.variables.get("delegated_step"),
                    project_domains=state.variables.get("project_domains"),
                )
            except ProviderUnavailableError:
                raise
            except Exception as error:
                if self._is_permanent_llm_error(error):
                    raise ProviderConfigurationError(error) from error
                if self._is_transient_llm_error(error):
                    raise ProviderUnavailableError(
                        "LLM provider is temporarily unavailable; execution state was preserved.",
                        error,
                    ) from error
                raise
            plan_result = normalize_plan(plan_result)
            plan_result = merge_inherited_requirements(
                plan_result, state.variables.get("inherited_requirements")
            )
            inherited_validations = state.variables.get("inherited_artifact_validations")
            if isinstance(inherited_validations, list) and inherited_validations:
                plan_result["artifact_validations"] = [
                    dict(item) for item in inherited_validations if isinstance(item, dict)
                ]
            if state.variables.get("capability_gaps"):
                scope_changes = plan_result.setdefault("scope_changes", [])
                known_statements = {
                    str(item.get("statement") or "") for item in scope_changes
                    if isinstance(item, dict)
                }
                for gap in state.variables["capability_gaps"]:
                    statement = (
                        f"Execution cannot consume the unavailable {gap['capability']} "
                        "capability; it is limited to configuration, adapter routines, "
                        "and independently verifiable outputs until that capability is configured."
                    )
                    if statement not in known_statements:
                        scope_changes.append({
                            "kind": "capability_gap",
                            "statement": statement,
                            "reason": gap["resolution"],
                            "requirement_ids": [],
                        })
                    routine_name = f"configure-{gap['capability']}-capability"
                    routines = plan_result.setdefault("execution_routines", [])
                    if not any(
                        isinstance(item, dict) and item.get("name") == routine_name
                        for item in routines
                    ):
                        routines.append({
                            "name": routine_name,
                            "purpose": gap["required_for"],
                            "prerequisites": ["Provider credentials and a compatible model/service"],
                            "configuration": {
                                "base_url": "<provider OpenAI-compatible base URL>",
                                "model_name": "<model identifier supporting the required modality>",
                                "capability_mode": "enabled",
                            },
                            "steps": [
                                "Open GPTMOSS settings and configure the provider endpoint and model.",
                                "Set the capability mode explicitly or retain auto-detection when metadata is reliable.",
                                "Run the provider connection test and save the configuration.",
                                "Confirm the capability is available in the diagnostics panel before resuming the project.",
                            ],
                            "expected_outputs": ["Saved runtime configuration", "Positive capability diagnostic"],
                            "validation": [
                                f"Diagnostics report {gap['capability']} as available.",
                                "A minimal representative input is consumed without a capability-gap warning.",
                            ],
                            "failure_handling": [
                                "Keep the project paused or approve only an adapter/configuration deliverable.",
                            ],
                        })
            plan_result = apply_professional_profile(
                plan_result,
                self.artifact_store,
                state.variables.get("attachment_ids", []),
            )
            self.telemetry.record("plan_generated", execution_id, duration_ms=round((time.perf_counter() - planning_started) * 1000, 2), steps=len(plan_result.get("steps", [])))
            state.current_plan = plan_result
            self._initialize_document_state(execution_id, task, plan_result, state)
            state.variables["delivery_contract"] = build_delivery_contract(
                state.current_plan, task
            )
            state.current_step = 0
            await self.event_bus.publish(Event(
                type="PlanGenerated",
                payload={"execution_id": execution_id, "plan": plan_result}
            ))

        state.current_plan = normalize_plan(state.current_plan)
        if "document_model_checkpoint" not in state.variables:
            self._initialize_document_state(execution_id, task, state.current_plan, state)
        if not isinstance(state.variables.get("delivery_contract"), dict):
            state.variables["delivery_contract"] = build_delivery_contract(
                state.current_plan, task
            )
        delivery_contract = state.variables["delivery_contract"]
        scope_changes = delivery_contract.get("scope_changes", [])
        approved_contract = state.variables.get("approved_scope_contract_sha256")
        if (not state.variables.get("parent_execution_id") and scope_changes
                and approved_contract != delivery_contract.get("contract_sha256")):
            self.state_engine.transition_execution(
                state, "paused", reason="scope approval required", actor="runtime"
            )
            state.variables["pending_scope_approval"] = {
                "contract_sha256": delivery_contract.get("contract_sha256"),
                "changes": scope_changes,
            }
            self.state_engine.save_to_disk()
            await self.event_bus.publish(Event(
                type="ScopeApprovalRequested",
                payload={
                    "execution_id": execution_id,
                    "contract_sha256": delivery_contract.get("contract_sha256"),
                    "changes": scope_changes,
                },
            ))
            await self.event_bus.publish(Event(
                type="ExecutionPaused",
                payload={"execution_id": execution_id, "reason": "scope_change"},
            ))
            return
        steps = state.current_plan.get("steps", [])

        # Ensure all steps have a status, resetting stuck 'running' states to 'pending' for resumption
        for step in steps:
            if "status" not in step or step.get("status") == "running":
                step["status"] = "pending"

        # Maintain a map of running asyncio Tasks keying by step ID
        running_tasks = {}
        
        async def run_step(step):
            return await self._run_plan_step(
                execution_id, state, steps, task, step
            )

        await self._coordinate_plan_execution(
            execution_id, state, steps, task, run_step, running_tasks
        )

    async def _coordinate_plan_execution(
        self, execution_id: str, state, steps, task: str, run_step, running_tasks
    ) -> None:
        """Schedule ready plan steps and own terminal delivery transitions."""
        while state.status in ("running", "pending"):
            if state.status == "pending":
                self.state_engine.transition_execution(
                    state, "running", reason="plan execution resumed", actor="runtime"
                )
                
            parent_state = self.state_engine.get_execution(execution_id)
            if parent_state.status in ("paused", "cancelled", "completed", "failed"):
                break
                
            # Find steps ready to execute (all dependencies completed)
            ready_steps = []
            for step in steps:
                step_id = step.get("id")
                if step.get("status") == "pending" and step_id not in running_tasks:
                    deps = step.get("dependencies", [])
                    deps_satisfied = True
                    for dep_id in deps:
                        dep_step = next((s for s in steps if s.get("id") == dep_id), None)
                        if not dep_step or dep_step.get("status") != "completed":
                            deps_satisfied = False
                            break
                    if deps_satisfied:
                        ready_steps.append(step)
                        
            # If no steps ready and none running, planning is done
            if not ready_steps and not running_tasks:
                all_completed = all(s.get("status") == "completed" for s in steps)
                if all_completed:
                    assurance_report = (
                        self._independent_delivery_report(execution_id, steps)
                        if not state.variables.get("parent_execution_id")
                        else {"schema_version": 1, "passed": True, "checks": [], "failures": [],
                              "delegated": True}
                    )
                    state.results["delivery_assurance"] = assurance_report
                    self.telemetry.record(
                        "delivery_assurance_completed", execution_id,
                        passed=bool(assurance_report.get("passed")),
                        failure_count=len(assurance_report.get("failures", [])),
                    )
                    await self.event_bus.publish(Event(
                        type="DeliveryAssuranceCompleted",
                        payload={
                            "execution_id": execution_id,
                            "passed": assurance_report.get("passed", False),
                            "failures": assurance_report.get("failures", []),
                        },
                    ))
                    if not assurance_report.get("passed", False):
                        repair_round = int(state.variables.get("assurance_repair_round", 0))
                        repair_step = next(
                            (item for item in reversed(steps)
                             if canonical_step_role(item.get("role")) == "debugger"),
                            None,
                        )
                        repair_budget = (
                            self._step_retry_budget(task, repair_step)
                            if repair_step is not None else self.max_step_retries
                        )
                        if repair_step is not None and repair_round < repair_budget:
                            state.variables["assurance_repair_round"] = repair_round + 1
                            repair_step["status"] = "pending"
                            repair_step.pop("assigned_execution_id", None)
                            repair_step["retry_context"] = (
                                "Independent delivery assurance rejected the assembled project. "
                                "Fix these machine-observed defects without redoing validated work:\n"
                                + json.dumps(assurance_report, ensure_ascii=False)[:10_000]
                            )
                            for downstream in steps:
                                if canonical_step_role(downstream.get("role")) == "coordinator":
                                    downstream["status"] = "pending"
                                    downstream.pop("assigned_execution_id", None)
                            await self.event_bus.publish(Event(
                                type="DeliveryRepairScheduled",
                                payload={
                                    "execution_id": execution_id,
                                    "round": repair_round + 1,
                                    "step_id": repair_step.get("id"),
                                },
                            ))
                            continue
                        self.state_engine.transition_execution(
                            state, "failed", reason="delivery assurance failed", actor="runtime"
                        )
                        state.results["error"] = (
                            "Independent delivery assurance failed: "
                            + "; ".join(assurance_report.get("failures", []))
                        )
                        await self.event_bus.publish(Event(
                            type="ExecutionFailed",
                            payload={
                                "execution_id": execution_id,
                                "error": state.results["error"],
                            },
                        ))
                        break
                    self.state_engine.transition_execution(
                        state, "completed", reason="delivery completed", actor="runtime"
                    )
                    workspace = self._delivery_workspace(execution_id)
                    if workspace:
                        package = build_delivery_package(
                            workspace, execution_id, state.current_plan,
                            assurance_report,
                        )
                        if package:
                            state.results["delivery_package"] = package
                    state.results["deliveries"] = [
                        state.results.get("steps", {}).get(str(step.get("id"))) for step in steps
                    ]
                    state.results["deliveries"] = [item for item in state.results["deliveries"] if item]
                    if steps:
                        state.results["final_output"] = steps[-1].get("delivery") or steps[-1].get("result")
                    self.telemetry.record("execution_completed", execution_id, completed_steps=len(steps))
                    state.results["telemetry"] = self.telemetry.metrics(execution_id)
                    await self.event_bus.publish(Event(
                        type="ExecutionCompleted",
                        payload={"execution_id": execution_id, "results": state.results}
                    ))
                break
                
            # Launch all ready steps concurrently
            for step in ready_steps:
                step_id = step.get("id")
                running_tasks[step_id] = asyncio.create_task(run_step(step))
                
            # Wait for at least one step to complete
            if running_tasks:
                done, pending_tasks = await asyncio.wait(
                    running_tasks.values(),
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                step_failure = None
                step_suspended = False
                provider_suspended = False
                
                for task_obj in done:
                    step_id = next((sid for sid, tobj in running_tasks.items() if tobj == task_obj), None)
                    if step_id is not None:
                        del running_tasks[step_id]
                        
                    try:
                        res = task_obj.result()
                        if res == "suspended":
                            step_suspended = True
                        elif res == "suspended_provider":
                            provider_suspended = True
                    except Exception as exc:
                        if isinstance(exc, ProviderUnavailableError):
                            provider_suspended = True
                            state.variables["provider_wait"] = {
                                "error": str(exc.original_error),
                                "error_type": exc.original_error.__class__.__name__,
                                "suspended_at": time.time(),
                            }
                        else:
                            step_failure = exc
                        
                # Update current step count
                state.current_step = sum(1 for s in steps if s.get("status") == "completed")
                
                if step_suspended:
                    self.state_engine.transition_execution(
                        state, "paused", reason="step suspended", actor="runtime"
                    )
                    for t in running_tasks.values():
                        t.cancel()
                    return

                if provider_suspended:
                    self.state_engine.transition_execution(
                        state, "waiting_provider", reason="step waiting for provider", actor="runtime"
                    )
                    for t in running_tasks.values():
                        t.cancel()
                    await asyncio.gather(*running_tasks.values(), return_exceptions=True)
                    self.state_engine.save_to_disk()
                    self._schedule_provider_resume(execution_id)
                    return
                    
                if step_failure:
                    self.state_engine.transition_execution(
                        state, "failed", reason="step failed", actor="runtime"
                    )
                    state.results["error"] = str(step_failure)
                    self.telemetry.record("execution_failed", execution_id, error=str(step_failure))
                    for t in running_tasks.values():
                        t.cancel()
                    await asyncio.gather(*running_tasks.values(), return_exceptions=True)
                    for child in self.state_engine.executions.values():
                        if (child.variables.get("parent_execution_id") == execution_id
                                and child.status in ("pending", "running", "paused")):
                            self.state_engine.transition_execution(
                                child, "cancelled", reason="parent step failed", actor="runtime"
                            )
                            await self.event_bus.publish(Event(
                                type="ExecutionCancelled", payload={"execution_id": child.execution_id}
                            ))
                    await self.event_bus.publish(Event(
                        type="ExecutionFailed",
                        payload={"execution_id": execution_id, "error": str(step_failure)}
                    ))
                    return
            else:
                logger.error(f"Execution {execution_id} has unresolvable cyclical step dependencies.")
                self.state_engine.transition_execution(
                    state, "failed", reason="cyclical plan dependencies", actor="runtime"
                )
                await self.event_bus.publish(Event(
                    type="ExecutionFailed",
                    payload={"execution_id": execution_id, "error": "Cyclical step dependencies detected in plan."}
                ))
                break


    async def _run_plan_step(self, execution_id: str, state, steps, task: str, step):
        """Execute one plan step while the outer coordinator owns scheduling."""
        sub_id = None
        step["status"] = "running"
        step_index = steps.index(step)
        await self.event_bus.publish(Event(
            type="StepStarted",
            payload={"execution_id": execution_id, "step_index": step_index, "description": step.get("description")}
        ))

        try:
            specialization = await self._prepare_autonomous_specialization(execution_id, state, step)
            role_key = canonical_step_role(step.get("role")) or infer_step_role(step.get("description", ""))
            generic_role_name = ROLE_DISPLAY_NAMES.get(role_key) if role_key else None
            role_name = step.get("specialist") or generic_role_name
            is_sub_agent = state.variables.get("parent_execution_id") is not None

            if role_name and role_key != "coordinator" and not is_sub_agent:
                # Persist the assignment before scheduling it. A resumed parent
                # reuses the same child instead of performing the step twice.
                import uuid
                sub_id = step.get("assigned_execution_id") or str(uuid.uuid4())
                is_new_assignment = "assigned_execution_id" not in step
                step["assigned_execution_id"] = sub_id

                dependency_results = []
                for dependency_id in step.get("dependencies", []):
                    dependency_step = next(item for item in steps if item.get("id") == dependency_id)
                    dependency_results.append({
                        "step_id": dependency_id,
                        "role": dependency_step.get("role"),
                        "description": dependency_step.get("description"),
                        "delivery": dependency_step.get("delivery") or dependency_step.get("result"),
                    })
                handoff = json.dumps(dependency_results, ensure_ascii=False)
                if len(handoff) > 8_000:
                    handoff = handoff[:8_000] + "\n… [dependency handoff truncated]"
                sub_task = step["description"]
                if step.get("retry_context"):
                    sub_task += (
                        "\n\nAUTONOMOUS RETRY: A previous specialist attempt did not satisfy its delivery gates. "
                        "Inspect and reuse any valid partial artifacts, correct the root cause, and complete the assignment. "
                        + str(step["retry_context"])
                    )
                if dependency_results:
                    sub_task += (
                        "\n\nValidated outputs from prerequisite steps are provided below. "
                        "Reuse them; do not redo their work.\n" + handoff
                    )

                sub_exec = self.state_engine.get_execution(sub_id)
                if is_new_assignment:
                    self.state_engine.transition_execution(
                        sub_exec, "pending", reason="delegated task assigned", actor="runtime"
                    )
                sub_exec.variables["role_name"] = role_name
                sub_exec.variables["role_key"] = role_key
                sub_exec.variables["generic_role_name"] = generic_role_name
                sub_exec.variables["parent_execution_id"] = execution_id
                sub_exec.variables["delegation_depth"] = (
                    int(state.variables.get("delegation_depth", 0)) + 1
                )
                lineage = list(state.variables.get("delegation_lineage") or [])
                normalized_subtask = " ".join(sub_task.lower().split())
                if normalized_subtask not in lineage:
                    lineage.append(normalized_subtask)
                sub_exec.variables["delegation_lineage"] = lineage
                sub_exec.variables["project_id"] = state.variables.get("project_id", "proj-default")
                sub_exec.variables["parent_task"] = state.variables.get("parent_task") or task
                sub_exec.variables["task"] = sub_exec.variables.get("task") or sub_task
                sub_exec.variables["plan_step_id"] = step.get("id")
                sub_exec.variables["dependency_results"] = dependency_results
                sub_exec.variables["specialist"] = step.get("specialist") or role_name
                sub_exec.variables["expertise"] = list(step.get("expertise", []))
                sub_exec.variables["agent_profile_id"] = step.get("agent_profile_id")
                sub_exec.variables["agent_profile_prompt"] = specialization.get("profile", {}).get("system_prompt", "")
                sub_exec.variables["requested_skills"] = sorted({
                    str(item).lower() for item in [
                        *state.variables.get("requested_skills", []),
                        *step.get("autonomous_skill_names", []),
                    ] if isinstance(item, str)
                })
                sub_exec.variables["delegated_step"] = {
                    key: value for key, value in step.items()
                    if key not in {"id", "dependencies", "status", "assigned_execution_id", "delivery", "result", "error"}
                }
                sub_exec.variables["attachment_ids"] = state.variables.get("attachment_ids", [])
                owned_artifacts = {
                    str(path).replace("\\", "/")
                    for path in step.get("required_artifacts", [])
                }
                sub_exec.variables["inherited_artifact_validations"] = [
                    dict(item)
                    for item in (state.current_plan or {}).get("artifact_validations", [])
                    if isinstance(item, dict)
                    and str(item.get("path") or "").replace("\\", "/") in owned_artifacts
                ]
                parent_requirements = (
                    state.variables.get("delivery_contract", {}).get("requirements", [])
                )
                sub_exec.variables["inherited_requirements"] = requirements_for_delegation(
                    parent_requirements, step.get("requirement_ids", [])
                )
                sub_exec.variables["agent_config"] = {
                    "system_prompt": f"You are the {role_name}, a domain specialist accountable for verified delivery.",
                    "role_name": role_name,
                    "role_key": role_key,
                    "expertise": list(step.get("expertise", [])),
                }
                if state.variables.get("project_path"):
                    sub_exec.variables["project_path"] = state.variables["project_path"]

                if is_new_assignment:
                    await self.event_bus.publish(Event(
                        type="TaskCreated",
                        payload={
                            "execution_id": sub_id,
                            "parent_execution_id": execution_id,
                            "plan_step_id": step.get("id"),
                            "role": role_key,
                            "specialist": role_name,
                            "task": sub_exec.variables["task"],
                            "agent_id": "default_agent"
                        }
                    ))

                if sub_exec.status in ("pending", "running"):
                    asyncio.create_task(self.execute_task(sub_id, sub_exec.variables["task"]))

                # Wait for sub-agent completion
                while True:
                    await asyncio.sleep(0.1)

                    parent_state = self.state_engine.get_execution(execution_id)
                    sub_state = self.state_engine.get_execution(sub_id)

                    if parent_state.status == "cancelled":
                        if sub_state.status in ("running", "paused", "pending"):
                            self.state_engine.transition_execution(
                                sub_state, "cancelled", reason="parent cancelled", actor="runtime"
                            )
                            await self.event_bus.publish(Event(
                                type="ExecutionCancelled",
                                payload={"execution_id": sub_id}
                            ))
                        break
                    elif parent_state.status == "paused":
                        if sub_state.status == "running":
                            self.state_engine.transition_execution(
                                sub_state, "paused", reason="parent paused", actor="runtime"
                            )
                            await self.event_bus.publish(Event(
                                type="ExecutionPaused",
                                payload={"execution_id": sub_id}
                            ))
                        continue

                    # Resume child if parent is resumed
                    if parent_state.status == "running" and sub_state.status == "paused" and not sub_state.variables.get("pending_approval"):
                        self.state_engine.transition_execution(
                            sub_state, "running", reason="parent resumed", actor="runtime"
                        )
                        asyncio.create_task(self.execute_task(sub_id, sub_exec.variables["task"]))

                    if sub_state.status == "paused" and sub_state.variables.get("pending_approval"):
                        pending_child = dict(sub_state.variables["pending_approval"])
                        pending_child["child_execution_id"] = sub_id
                        pending_child["child_role_name"] = sub_state.variables.get("role_name")
                        self.state_engine.transition_execution(
                            parent_state, "paused", reason="child approval required", actor="runtime"
                        )
                        parent_state.variables["pending_approval"] = pending_child
                        step["status"] = "pending"
                        self.state_engine.save_to_disk()
                        await self.event_bus.publish(Event(
                            type="ApprovalRequested",
                            payload={
                                "execution_id": execution_id,
                                "child_execution_id": sub_id,
                                "tool_call_id": pending_child.get("tool_call_id"),
                                "capability": pending_child.get("capability"),
                                "action": pending_child.get("action"),
                                "arguments": pending_child.get("arguments", {}),
                                "reason": "A delegated specialist is waiting for this authorization.",
                            },
                        ))
                        await self.event_bus.publish(Event(
                            type="ExecutionPaused",
                            payload={
                                "execution_id": execution_id,
                                "reason": "child_approval",
                                "child_execution_id": sub_id,
                            },
                        ))
                        return "suspended"

                    if sub_state.status == "waiting_provider":
                        self.state_engine.transition_execution(
                            parent_state, "waiting_provider", reason="child waiting for provider", actor="runtime"
                        )
                        parent_state.variables["provider_wait"] = {
                            "child_execution_id": sub_id,
                            "suspended_at": time.time(),
                        }
                        step["status"] = "pending"
                        self.state_engine.save_to_disk()
                        self._schedule_provider_resume(execution_id)
                        return "suspended_provider"

                    if sub_state.status in ("completed", "failed", "cancelled"):
                        break

                if sub_state.status == "completed":
                    delivery = sub_exec.variables.get("delivery")
                    if not isinstance(delivery, dict):
                        sub_conversation = self.state_engine.get_conversation(sub_id)
                        last_response = "Sub-agent execution completed."
                        for msg in reversed(sub_conversation.messages):
                            if msg.get("role") == "assistant" and msg.get("content"):
                                last_response = msg["content"]
                                break
                        delivery = self._structured_delivery(last_response)
                    sub_exec.variables["delivery"] = delivery
                    step["delivery"] = delivery
                    result = json.dumps(delivery, ensure_ascii=False)
                else:
                    child_error = str(
                        sub_state.results.get("error")
                        or f"status: {sub_state.status}"
                    )
                    raise RuntimeError(
                        f"Sub-agent {role_name} stopped: {child_error}"
                    )
            else:
                # Execute step loop locally
                if specialization.get("profile"):
                    state.variables["agent_profile_id"] = specialization["profile"]["id"]
                    state.variables["agent_profile_prompt"] = specialization["profile"].get("system_prompt", "")
                    state.variables["requested_skills"] = sorted(
                        {str(item).lower() for item in state.variables.get("requested_skills", [])
                         if isinstance(item, str)} |
                        {str(item).lower() for item in specialization.get("skill_names", [])
                         if isinstance(item, str)}
                    )
                result = await self._execute_step_loop(execution_id, step)
                if state.variables.get("parent_execution_id") and role_key != "coordinator":
                    delivery = self._structured_delivery(result)
                    state.variables["delivery"] = delivery
                    step["delivery"] = delivery

            # If the step execution suspended (e.g. paused for approval), reset to pending
            parent_state = self.state_engine.get_execution(execution_id)
            if parent_state.status in ("paused", "waiting_provider"):
                step["status"] = "pending"
                return (
                    "suspended_provider" if parent_state.status == "waiting_provider"
                    else "suspended"
                )

            step["status"] = "completed"
            step["result"] = result
            self._sync_document_checkpoint(execution_id, state)
            if not sub_id:
                self._record_specialization_outcome(execution_id, step, True, str(result))
            step_record = {
                "step_id": step.get("id"),
                "description": step.get("description"),
                "role": step.get("role") or "coordinator",
                "execution_id": step.get("assigned_execution_id") or execution_id,
                "result": step.get("delivery") or result,
            }
            state.results.setdefault("steps", {})[str(step.get("id"))] = step_record
            await self.event_bus.publish(Event(
                type="StepCompleted",
                payload={"execution_id": execution_id, "step_index": step_index, "result": result}
            ))
            return "completed"

        except asyncio.CancelledError:
            if sub_id:
                child = self.state_engine.get_execution(sub_id)
                if child.status in ("pending", "running", "paused"):
                    self.state_engine.transition_execution(
                        child, "cancelled", reason="step cancelled", actor="runtime"
                    )
            step["status"] = "pending" if state.status == "paused" else "cancelled"
            raise
        except ProviderUnavailableError:
            step["status"] = "pending"
            raise
        except Exception as e:
            if not sub_id:
                self._record_specialization_outcome(execution_id, step, False, str(e))
            if self._is_permanent_llm_error(e):
                step["status"] = "failed"
                step["error"] = str(e)
                await self.event_bus.publish(Event(
                    type="StepFailed",
                    payload={
                        "execution_id": execution_id,
                        "step_index": step_index,
                        "error": str(e),
                    },
                ))
                raise
            repair_step = next((
                candidate for candidate in steps
                if candidate.get("status") == "pending"
                and canonical_step_role(candidate.get("role")) == "debugger"
                and step.get("id") in candidate.get("dependencies", [])
            ), None)
            if sub_id and role_key == "qa" and repair_step is not None:
                failed_child = self.state_engine.get_execution(sub_id)
                machine_evidence = []
                for item in failed_child.variables.get("tool_call_history", [])[-20:]:
                    result_text = str(item.get("result") or "")
                    if "EXIT_CODE: 0" not in result_text or "Error" in result_text:
                        machine_evidence.append({
                            "capability": item.get("capability"),
                            "action": item.get("action"),
                            "arguments": item.get("arguments"),
                            "result": result_text[-4_000:],
                        })
                failed_commands = list(step.get("verification_commands", []))
                repair_step["verification_commands"] = list(dict.fromkeys([
                    *repair_step.get("verification_commands", []),
                    *failed_commands,
                ]))
                repair_step["retry_context"] = (
                    "Independent validation completed and found defects. Repair the source or tests from the "
                    "user requirements, never weaken valid assertions, then rerun every inherited validation "
                    "command as well as your own.\n"
                    + json.dumps({
                        "validation_step_id": step.get("id"),
                        "failed_execution_id": sub_id,
                        "error": str(e),
                        "machine_evidence": machine_evidence,
                    }, ensure_ascii=False)[:12_000]
                )
                validation_result = {
                    "summary": "Independent validation completed with defects requiring debugger repair.",
                    "passed": False,
                    "failed_execution_id": sub_id,
                    "error": str(e),
                    "verification_commands": failed_commands,
                }
                step["status"] = "completed"
                step["validation_passed"] = False
                step["result"] = json.dumps(validation_result, ensure_ascii=False)
                state.results.setdefault("steps", {})[str(step.get("id"))] = {
                    "step_id": step.get("id"),
                    "description": step.get("description"),
                    "role": step.get("role"),
                    "execution_id": sub_id,
                    "result": validation_result,
                }
                await self.event_bus.publish(Event(
                    type="ValidationRepairHandoff",
                    payload={
                        "execution_id": execution_id,
                        "validation_step_id": step.get("id"),
                        "repair_step_id": repair_step.get("id"),
                        "failed_execution_id": sub_id,
                    },
                ))
                return "completed"
            retry_count = int(step.get("retry_count", 0))
            retry_budget = self._step_retry_budget(task, step)
            if sub_id and retry_count < retry_budget and state.status not in ("cancelled", "paused"):
                step["retry_count"] = retry_count + 1
                step.setdefault("failed_attempts", []).append({
                    "execution_id": sub_id, "attempt": retry_count + 1, "error": str(e),
                })
                failed_child = self.state_engine.get_execution(sub_id)
                recent_failures = []
                for item in failed_child.variables.get("tool_call_history", [])[-8:]:
                    result_text = str(item.get("result") or "")
                    if ("EXIT_CODE: 0" not in result_text or "Error" in result_text
                            or item.get("capability") == "shell"):
                        recent_failures.append({
                            "capability": item.get("capability"),
                            "action": item.get("action"),
                            "arguments": item.get("arguments"),
                            "result": result_text[-3_000:],
                        })
                failed_history = failed_child.variables.get("tool_call_history", [])
                durable_mutations = sum(
                    1 for item in failed_history
                    if (
                        item.get("capability") == "filesystem"
                        and item.get("action") in {"write", "delete"}
                        and "Error" not in str(item.get("result") or "")
                    )
                )
                gate_failures = self._step_completion_issues(
                    sub_id,
                    step,
                    json.dumps({
                        "summary": "retry audit",
                        "artifacts": [],
                        "evidence": [],
                        "risks": [],
                        "next_action": "",
                    }),
                )
                step["retry_context"] = (
                    f"Previous attempt {retry_count + 1} failed: {e}\n"
                    f"Durable filesystem mutations completed: {durable_mutations}. "
                    + (
                        "The attempt made no durable edit; stop inspecting and apply the smallest concrete "
                        "requirement-preserving correction before rerunning validation.\n"
                        if durable_mutations == 0 else
                        "Reuse the valid edits already present; do not overwrite them with the old state.\n"
                    )
                    + "Current machine gate failures: "
                    + "; ".join(gate_failures)
                    + "\n"
                    "Recent machine evidence from that attempt (do not repeat the same failed action):\n"
                    + json.dumps(recent_failures, ensure_ascii=False)[:8_000]
                )
                step.pop("assigned_execution_id", None)
                step["status"] = "pending"
                await self.event_bus.publish(Event(
                    type="StepRetryScheduled",
                    payload={"execution_id": execution_id, "step_index": step_index,
                             "attempt": retry_count + 2, "error": str(e)},
                ))
                return "retry"
            step["status"] = "failed"
            step["error"] = str(e)
            await self.event_bus.publish(Event(
                type="StepFailed",
                payload={"execution_id": execution_id, "step_index": step_index, "error": str(e)}
            ))
            raise e


    async def _process_step_tool_calls(
        self, execution_id: str, state, convo, tool_calls: List[Dict[str, Any]],
        iteration: int, context: Dict[str, Any],
    ) -> Optional[str]:
        """Apply policy and execute or suspend one batch of model tool calls."""
        # Process tool calls
        for tool_call in tool_calls:
            if state.status == "cancelled":
                raise asyncio.CancelledError()
            tool_id = str(tool_call.get("id") or "").strip()
            func_info = tool_call.get("function", {})
            full_name = func_info.get("name", "")
            args = func_info.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            while len(args) == 1 and isinstance(args.get("arguments"), dict):
                args = args["arguments"]
            if not tool_id:
                tool_id = f"anonymous-{iteration}-{len(state.variables.setdefault('completed_tool_calls', {}))}"

            completed_tool_calls = state.variables.setdefault("completed_tool_calls", {})
            if tool_id in completed_tool_calls:
                result_str = completed_tool_calls[tool_id]
                convo.messages.append({
                    "role": "tool", "tool_call_id": tool_id, "name": full_name,
                    "content": result_str, "timestamp": time.time(),
                })
                await self.event_bus.publish(Event(
                    type="ToolReused",
                    payload={"execution_id": execution_id, "tool_call_id": tool_id, "result": result_str},
                ))
                continue

            # Parse capability and action
            if "__" in full_name:
                cap_name, act_name = full_name.split("__", 1)
            else:
                cap_name = full_name
                act_name = ""
            args = self._normalize_tool_arguments(cap_name, act_name, args)

            # Evaluate policy
            fingerprint, policy_desc = self._cached_approval_decision(
                state, cap_name, act_name, args
            )
            if policy_desc is None:
                if state.status == "cancelled":
                    raise asyncio.CancelledError()
                policy_desc = await self.policy_provider.check_action(
                    execution_id=execution_id,
                    capability=cap_name,
                    action=act_name,
                    arguments=args,
                    context=context
                )

            await self.event_bus.publish(Event(
                type="ToolCalled",
                payload={
                    "execution_id": execution_id,
                    "tool_call_id": tool_id,
                    "capability": cap_name,
                    "action": act_name,
                    "arguments": args
                }
            ))

            if policy_desc.decision == "deny":
                result_str = f"Execution blocked: Policy Denied. Reason: {policy_desc.reason}"
                completed_tool_calls[tool_id] = result_str
                convo.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": full_name,
                    "content": result_str,
                    "timestamp": time.time()
                })
                await self.event_bus.publish(Event(
                    type="ToolCompleted",
                    payload={"execution_id": execution_id, "tool_call_id": tool_id, "result": result_str}
                ))
            elif policy_desc.decision == "approval":
                # Human-in-the-loop: Pause execution and await approval
                self.state_engine.transition_execution(
                    state, "paused", reason="capability approval required", actor="runtime"
                )
                state.variables["pending_approval"] = {
                    "tool_call_id": tool_id,
                    "capability": cap_name,
                    "action": act_name,
                    "arguments": args,
                    "fingerprint": fingerprint,
                }
                await self.event_bus.publish(Event(
                    type="ApprovalRequested",
                    payload={
                        "execution_id": execution_id,
                        "tool_call_id": tool_id,
                        "capability": cap_name,
                        "action": act_name,
                        "arguments": args,
                        "reason": policy_desc.reason
                    }
                ))
                await self.event_bus.publish(Event(
                    type="ExecutionPaused",
                    payload={"execution_id": execution_id}
                ))
                # Stop the loop here, we will resume when decision arrives
                return "Paused waiting for human approval."
            else:
                # 'allow' -> Execute the capability
                if state.status == "cancelled":
                    raise asyncio.CancelledError()
                result_str = await self._call_tool(execution_id, cap_name, act_name, args)
                completed_tool_calls[tool_id] = result_str
                self._record_tool_result(execution_id, cap_name, act_name, args, result_str)
                convo.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": full_name,
                    "content": result_str,
                    "timestamp": time.time()
                })
                await self.event_bus.publish(Event(
                    type="ToolCompleted",
                    payload={"execution_id": execution_id, "tool_call_id": tool_id, "result": result_str}
                ))

        return None

    async def _execute_step_loop(self, execution_id: str, step: Dict[str, Any]) -> str:
        """
        Executes a step by running a ReAct-style dialog loop with the LLM.
        """
        state = self.state_engine.get_execution(execution_id)
        convo = self.state_engine.get_conversation(execution_id)
        role_for_step = canonical_step_role(step.get("role")) or infer_step_role(
            step.get("description", "")
        )
        persisted_runtime = (state.variables.get("step_runtime") or {}).get(
            str(step.get("id")), {}
        )
        is_resumed_step = int(persisted_runtime.get("iterations", 0)) > 0
        if (
            role_for_step == "coordinator"
            and is_resumed_step
            and not state.variables.get("pending_approval")
            and self._can_engine_finalize(execution_id, step)
        ):
            # A resumed terminal audit may already have complete delegated QA
            # evidence.  Finalize before asking the model for another action,
            # which could only duplicate tests or mutate an assured workspace.
            return self._engine_delivery(execution_id, step)
        expertise_query = " ".join([
            str(state.variables.get("specialist") or step.get("specialist") or ""),
            step.get("description", ""),
            " ".join(state.variables.get("expertise") or step.get("expertise", [])),
        ])
        skills = self._active_skills(state, expertise_query)
        allowed_capabilities = self._allowed_capabilities(skills)
        
        step_desc = step.get("description", "")
        prerequisite_outputs = state.variables.get("dependency_results") or []
        if not prerequisite_outputs and state.current_plan:
            if role_for_step == "coordinator":
                dependency_ids = [
                    item.get("id") for item in state.current_plan.get("steps", [])
                    if item is not step and item.get("status") == "completed"
                ]
            else:
                dependency_ids = step.get("dependencies", [])
            for dependency_id in dependency_ids:
                dependency_step = next(
                    (item for item in state.current_plan.get("steps", []) if item.get("id") == dependency_id),
                    None,
                )
                if dependency_step:
                    prerequisite_outputs.append({
                        "step_id": dependency_id,
                        "role": dependency_step.get("role"),
                        "description": dependency_step.get("description"),
                        "delivery": dependency_step.get("delivery") or dependency_step.get("result"),
                    })
        # Sub-prompt for the step: only append if not resuming from a pending approval to preserve tool call message ordering
        if not state.variables.get("pending_approval"):
            reuse_instruction = ""
            if prerequisite_outputs:
                reuse_instruction = " Reuse the validated prerequisite outputs supplied in the task; do not repeat their work."
            convo.messages.append({"role": "system", "content": f"Current Step objectives: {step_desc}.{reuse_instruction} Generate thought and select tools if needed.", "timestamp": time.time()})

        runtime_key = str(step.get("id"))
        runtime = state.variables.setdefault("step_runtime", {}).setdefault(
            runtime_key, {"iterations": 0, "stagnant_iterations": 0}
        )
        iteration = int(runtime.get("iterations", 0))
        stagnant_iterations = int(runtime.get("stagnant_iterations", 0))
        stagnation_budget = self._step_stagnation_budget(
            str(state.variables.get("parent_task") or state.variables.get("task") or ""),
            step,
        )
        malformed_retry_budget = max(
            2,
            self._step_retry_budget(
                str(state.variables.get("parent_task") or state.variables.get("task") or ""),
                step,
            ) + 1,
        )
        previous_progress = self._progress_signature(execution_id, step)
        observed_iteration = False

        while True:
            current_progress = self._progress_signature(execution_id, step)
            if observed_iteration:
                improved, improvement_kind = self._quality_improved(
                    execution_id, previous_progress, current_progress
                )
                if improved:
                    stagnant_iterations = 0
                    runtime["stagnant_iterations"] = 0
                    self.telemetry.record(
                        "step_quality_improved", execution_id,
                        improvement=improvement_kind, iteration=iteration,
                    )
                    await self.event_bus.publish(Event(
                        type="StepQualityImproved",
                        payload={
                            "execution_id": execution_id,
                            "iteration": iteration,
                            "improvement": improvement_kind,
                            "remaining_stagnation_budget": stagnation_budget,
                        },
                    ))
                else:
                    stagnant_iterations += 1
                    runtime["stagnant_iterations"] = stagnant_iterations
                    first_threshold = max(3, stagnation_budget // 3)
                    second_threshold = max(
                        first_threshold + 2, (2 * stagnation_budget) // 3
                    )
                    requested_nudge_level = (
                        2 if stagnant_iterations >= second_threshold else
                        1 if stagnant_iterations >= first_threshold else 0
                    )
                    delivered_nudge_level = int(
                        runtime.get("stagnation_nudge_level", 0)
                    )
                    if requested_nudge_level > delivered_nudge_level:
                        runtime["stagnation_nudge_level"] = requested_nudge_level
                        if requested_nudge_level == 1:
                            guidance = (
                                "STAGNATION WARNING: repeated inspection has produced no durable quality improvement. "
                                "Stop rereading or rerunning the same failing command. Use the latest concrete failure to "
                                "perform the smallest requirement-preserving source correction now, preferably with "
                                "filesystem.write, then run one targeted verification. Do not modify or weaken QA tests."
                            )
                        else:
                            guidance = (
                                "STAGNATION CRITICAL: no durable correction followed the previous warning. Either make "
                                "the concrete minimal source edit and verify it now, or identify an exact policy, ownership, "
                                "or missing-input blocker in the delivery contract. More exploratory reads and unchanged "
                                "retries are not useful."
                            )
                        convo.messages.append({
                            "role": "system",
                            "content": guidance,
                            "timestamp": time.time(),
                        })
                        self.telemetry.record(
                            "step_stagnation_nudge", execution_id,
                            level=requested_nudge_level,
                            stagnant_iterations=stagnant_iterations,
                            stagnation_budget=stagnation_budget,
                        )
            previous_progress = current_progress
            if self.continue_while_progress:
                if stagnant_iterations >= stagnation_budget:
                    break
            elif iteration >= stagnation_budget:
                break

            if state.status == "cancelled":
                raise asyncio.CancelledError()
            if state.status == "failed":
                raise RuntimeError("Execution state was marked failed.")
            if state.status == "paused" and not state.variables.get("pending_approval", {}).get("decision"):
                return f"Execution suspended with status: {state.status}."
            iteration += 1
            runtime["iterations"] = iteration
            observed_iteration = True

            # Check if there is a pending approval we just resumed
            pending_app = state.variables.get("pending_approval")
            if pending_app:
                # We have a pending tool call that is now approved or rejected!
                # Remove from pending list
                state.variables.pop("pending_approval")
                tool_call_id = pending_app["tool_call_id"]
                
                # Check if user decision is approved
                decision = pending_app.get("decision", "reject")
                fingerprint = pending_app.get("fingerprint") or tool_call_fingerprint(
                    pending_app["capability"], pending_app["action"], pending_app["arguments"]
                )
                state.variables.setdefault("approval_decisions", {})[fingerprint] = decision
                completed_tool_calls = state.variables.setdefault("completed_tool_calls", {})
                if tool_call_id in completed_tool_calls:
                    result_str = completed_tool_calls[tool_call_id]
                elif decision == "allow":
                    if state.status == "cancelled":
                        raise asyncio.CancelledError()
                    # Execute tool call
                    result_str = await self._call_tool(
                        execution_id,
                        pending_app["capability"],
                        pending_app["action"],
                        pending_app["arguments"]
                    )
                else:
                    result_str = f"Execution blocked: human rejection. Reason: {pending_app.get('reason', 'None')}"
                completed_tool_calls[tool_call_id] = result_str
                self._record_tool_result(
                    execution_id, pending_app["capability"], pending_app["action"],
                    pending_app["arguments"], result_str,
                )

                convo.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": f"{pending_app['capability']}__{pending_app['action']}",
                    "content": result_str,
                    "timestamp": time.time()
                })
                # Re-emit that the tool completed
                await self.event_bus.publish(Event(
                    type="ToolCompleted",
                    payload={
                        "execution_id": execution_id,
                        "tool_call_id": tool_call_id,
                        "result": result_str
                    }
                ))
                # Continue loop to ask LLM for next thought
                continue

            # Build tools list
            is_sub_agent = state.variables.get("parent_execution_id") is not None
            delegated_plan = (not is_sub_agent) and any(
                canonical_step_role(item.get("role")) not in (None, "coordinator")
                for item in (state.current_plan or {}).get("steps", [])
            )
            schema_role = (
                canonical_step_role(step.get("role"))
                or canonical_step_role(state.variables.get("role_key"))
                or infer_step_role(step_desc)
            )
            schemas = self.get_capabilities_schemas(
                is_sub_agent=is_sub_agent,
                allowed_capabilities=allowed_capabilities,
                delegation_depth=int(state.variables.get("delegation_depth", 0)),
                suppress_delegation=(
                    delegated_plan
                    or schema_role in {"qa", "debugger", "coordinator"}
                    or (
                        is_sub_agent
                        and not bool(step.get("allow_nested_delegation", False))
                    )
                ),
            )

            # Compile context
            context = await self.context_engine.compile_context(
                execution_id=execution_id,
                conversation_id=execution_id,
                agent_id="default_agent",
                capabilities_schemas=schemas
            )
            if self.artifact_store and state.variables.get("attachment_ids"):
                attachment_text_budget = self.artifact_store.max_text_chars
                if not attachment_text_budget and self.adaptive_resource_management:
                    attachment_text_budget = int(context.get("context_budget_chars") or 0)
                attachment_query = "\n".join(
                    str(value)
                    for value in (
                        state.variables.get("task"),
                        step.get("description"),
                        step.get("specialist"),
                        " ".join(str(item) for item in step.get("expertise", [])),
                        " ".join(
                            str(item)
                            for item in step.get("acceptance_criteria", [])
                        ),
                    )
                    if value
                )[:8_000]
                context["attachments"] = self.artifact_store.context_items(
                    state.variables["attachment_ids"],
                    getattr(self.llm_provider, "supports_vision", False),
                    max_text_chars=attachment_text_budget,
                    query=attachment_query,
                )

            # Request LLM completion
            llm_messages = []
            role_name = state.variables.get("role_name", "Coordinateur")
            role_key = state.variables.get("role_key") or canonical_step_role(role_name) or "coordinator"
            specialist_name = state.variables.get("specialist") or step.get("specialist") or role_name
            expertise = state.variables.get("expertise") or step.get("expertise", [])
            base_prompt = context.get("system_instructions", "")
            profile_prompt = str(state.variables.get("agent_profile_prompt") or "").strip()
            if profile_prompt:
                base_prompt += "\n\nPersistent autonomous agent profile:\n" + profile_prompt
            environment = context.get("environment", {})
            project_workspace = self._delivery_workspace(execution_id) or ""
            base_prompt += (
                f"\n\nRuntime environment: operating_system={environment.get('operating_system')}, "
                f"shell={environment.get('shell')}, path_separator={environment.get('path_separator')}, "
                f"active_python={sys.executable}, project_workspace={project_workspace}. "
                "Use commands compatible with this exact environment; do not use Unix utilities on Windows. "
                "Filesystem and shell actions already start in project_workspace. Treat it as the workspace "
                "boundary: do not cd to a parent, the GPTMOSS repository, or another project. "
                "Invoke the active interpreter with the literal command python: the runtime maps it to active_python, "
                "so never spend tool calls discovering, installing, or interpolating another Python executable. "
                "On Windows the shell capability executes through cmd.exe; do not invoke PowerShell cmdlets directly "
                "unless the command explicitly starts powershell -NoProfile -Command."
            )
            if skills:
                base_prompt += "\n\nActive skills:\n" + "\n\n".join(
                    f"[{skill.name}]\n{skill.instructions}" for skill in skills
                )
            if state.variables.get("attachment_ids"):
                base_prompt += (
                    "\n\nLocal document workflow: attached documents are parsed and indexed "
                    "without Internet access. Initial excerpts are selected from the whole corpus "
                    "for this assignment and include source, section, block range, and chunk ID. "
                    "Use documents.inventory, documents.search, documents.read, and "
                    "documents.read_chunk to verify coverage or retrieve omitted sections. "
                    "The documents.read start_block parameter is a zero-based normalized-block "
                    "offset, while local citation locators are one-based. A PPTX commonly has "
                    "multiple normalized blocks per slide: cite its provenance slide_number "
                    "within inventory citation_bounds, never its block order or block count. "
                    "Never claim complete corpus coverage from excerpts alone."
                )
            
            if role_key == "architect":
                specialized_prompt = (
                    "You are the Specialized Architect Agent.\n"
                    "Your role is to analyze software requirements, design specifications, and write technical specifications files (e.g. specs.md).\n"
                    "Focus on clear system design, modular structures, and detailed implementation plans for other sub-agents. "
                    "Inventory dependencies against the offline runtime and design standard-library deterministic geometry when ML weights are unavailable."
                )
            elif role_key == "security":
                specialized_prompt = (
                    "You are the Specialized Security & Compliance Reviewer.\n"
                    "Your role is to check specifications or code for logical flaws, cryptographic vulnerabilities, or input validation risks.\n"
                    "Write detailed security reviews (e.g. security_review.md) highlighting potential issues and proposing mitigations."
                )
            elif role_key == "developer":
                specialized_prompt = (
                    "You are the Specialized Developer/Coder Agent.\n"
                    "Your role is to write clean, high-quality, and fully functional source code.\n"
                    "Avoid placeholders; write actual implementation. Do not create local packages that impersonate missing third-party "
                    "dependencies such as numpy, torch, cv2, or trimesh. Use standard-library data structures or explicit optional adapters."
                )
            elif role_key == "qa":
                specialized_prompt = (
                    "You are the Specialized QA Testing Engineer.\n"
                    "Your role is to design and write robust unit tests (e.g. pytest tests) to verify the code correctness.\n"
                    "Import and exercise the actual project modules. Do not replace them with mocks, replicas, or random data. "
                    "Cover edge cases, invariants, input validation, deterministic repeatability, and boundary conditions. "
                    "The user task and validated specification outrank the current implementation: never weaken, delete, skip, "
                    "or rewrite a valid acceptance assertion merely to make defective code pass."
                )
            elif role_key == "debugger":
                specialized_prompt = (
                    "You are the Specialized Debugger & Bug Fixer.\n"
                    "Your role is to analyze test failure logs, run commands to inspect state, and modify source files to fix "
                    "code syntax or logical errors. Never delete, skip, narrow collection of, or weaken an independent QA or "
                    "acceptance test to obtain a passing result; repair the implementation or its integration contract."
                )
            elif role_key == "writer":
                specialized_prompt = (
                    "You are the Specialized Technical Writer.\n"
                    "Your role is to write detailed project documentation, README.md files, and help guides for users. "
                    "For long documents, work section-by-section from the declared section contracts: meet each target "
                    "word count, cite bounded local evidence near factual claims, preserve terminology and requirement IDs, "
                    "and avoid repeating prerequisite sections. Treat the checkpoint and previous-section memory as the "
                    "canonical source of continuity. Never invent a source, citation locator, diagram, metric, or validation result."
                )
            else:
                specialized_prompt = "Coordinate the current step and synthesize prerequisite results without repeating completed work."

            role_prompt = (base_prompt + "\n\n" + specialized_prompt).strip()

            role_prompt += (
                f"\n\nExact specialist assignment: {specialist_name}."
                f"\nRequired expertise: {json.dumps(expertise, ensure_ascii=False)}."
                f"\nRequired artifacts: {json.dumps(step.get('required_artifacts', []), ensure_ascii=False)}."
                f"\nAcceptance criteria: {json.dumps(step.get('acceptance_criteria', []), ensure_ascii=False)}."
                f"\nVerification commands: {json.dumps(step.get('verification_commands', []), ensure_ascii=False)}."
                f"\nRequirement IDs: {json.dumps(step.get('requirement_ids', []), ensure_ascii=False)}."
                f"\nInherited mandatory requirements with full text: "
                f"{json.dumps(state.variables.get('inherited_requirements', []), ensure_ascii=False)}."
                f"\nOwned paths: {json.dumps(step.get('owned_paths', []), ensure_ascii=False)}."
                f"\nExternal tool declarations: {json.dumps((state.current_plan or {}).get('external_tools', []), ensure_ascii=False)}."
                f"\nExecution routines: {json.dumps((state.current_plan or {}).get('execution_routines', []), ensure_ascii=False)}."
                f"\nArtifact validation specifications: {json.dumps((state.current_plan or {}).get('artifact_validations', []), ensure_ascii=False)}."
                f"\nLong-document checkpoint: {json.dumps(state.variables.get('document_model_checkpoint', ''), ensure_ascii=False)}."
                f"\nSection progress: {json.dumps(state.variables.get('document_sections', []), ensure_ascii=False)}."
                f"\nDocument continuity memory: {state.variables.get('document_memory', 'none')}."
                "\nAct autonomously inside the project workspace: inspect existing prerequisite artifacts, implement the assignment, "
                "run relevant checks, diagnose failures, fix root causes, and rerun checks before finishing. Do not merely describe "
                "what should be done. Do not redo validated dependency work. Never claim an artifact or successful test that you did not create or execute."
                " Inherited requirements are context for this assignment; they never expand your ownership. Create or edit only the exact Required artifacts and Owned paths above. Do not create sibling-step deliverables, alternate root-level copies, quality reports owned by later reviewers, or extra subprojects."
                " Do not install dependencies online or create fake dependency packages inside the project."
            )

            if role_key != "coordinator":
                role_prompt += ("\nOnly after all declared gates pass, return one raw JSON object with keys: summary, artifacts, "
                                "evidence, risks, next_action. artifacts and evidence must be arrays. Use empty arrays or strings "
                                "when a field does not apply. Until then, keep using tools and correcting the workspace.")
            if role_key in {"qa", "debugger"}:
                source_contracts = self._source_contract_summary(execution_id)
                if source_contracts:
                    role_prompt += (
                        "\n\nActual source contracts discovered from the workspace are listed below. Read the source files "
                        "before writing assertions. Tests must call these real names and signatures; never invent a more "
                        "convenient API. If source and tests disagree, resolve the disagreement from the user task and validated "
                        "specification. Never relax a valid requirement to match current behavior. Then run the complete declared command.\n"
                        + source_contracts
                    )
            llm_messages.append({"role": "system", "content": role_prompt})
            for attachment in context.get("attachments", []):
                if attachment.get("text") is not None:
                    llm_messages.append({
                        "role": "user",
                        "content": (
                            f"Retrieved local content from attached file "
                            f"{attachment['filename']}:\n"
                            f"Selection metadata: "
                            f"{json.dumps(attachment.get('retrieval', {}), ensure_ascii=False)}\n"
                            f"{attachment['text']}"
                        ),
                    })
                elif attachment.get("image_url"):
                    llm_messages.append({"role": "user", "content": [
                        {"type": "text", "text": f"Attached image: {attachment['filename']}"},
                        {"type": "image_url", "image_url": {"url": attachment["image_url"]}},
                    ]})
                else:
                    llm_messages.append({"role": "system", "content": f"Attachment {attachment['filename']}: {attachment['note']}"})
            if context.get("context_summary"):
                llm_messages.append({"role": "system", "content": context["context_summary"]})
            if prerequisite_outputs:
                llm_messages.append({
                    "role": "system",
                    "content": "Validated prerequisite deliveries to synthesize; do not redo them:\n" + json.dumps(
                        prerequisite_outputs, ensure_ascii=False
                    )[:8_000],
                })
            llm_messages.extend(context["conversation_history"])

            await self.event_bus.publish(Event(
                type="LLMRequest",
                payload={"execution_id": execution_id, "messages": llm_messages}
            ))

            llm_started = time.perf_counter()
            llm_response = await self._completion_with_recovery(
                execution_id,
                messages=llm_messages,
                tools=schemas if schemas else None
            )
            self.telemetry.record("llm_completed", execution_id, duration_ms=round((time.perf_counter() - llm_started) * 1000, 2), message_count=len(llm_messages), tool_calls=len(llm_response.get("tool_calls") or []))

            await self.event_bus.publish(Event(
                type="LLMResponse",
                payload={"execution_id": execution_id, "response": llm_response}
            ))

            # Store LLM assistant message
            assistant_msg = {
                "role": "assistant",
                "content": llm_response.get("content"),
                "timestamp": time.time()
            }
            if llm_response.get("tool_calls"):
                assistant_msg["tool_calls"] = llm_response["tool_calls"]
            convo.messages.append(assistant_msg)

            # Check for tool calls
            tool_calls = llm_response.get("tool_calls")
            if tool_calls:
                runtime["malformed_text_call_streak"] = 0
            if not tool_calls:
                response_text = llm_response.get("content") or ""
                if re.search(
                    r"(?:\"?tool_call\"?\s*:|\"?name\"?\s*:\s*\"?(?:filesystem|shell|agent|devteam)__)",
                    response_text,
                    flags=re.IGNORECASE,
                ):
                    self.telemetry.record(
                        "malformed_text_tool_call_rejected", execution_id,
                        iteration=iteration,
                    )
                    malformed_streak = int(runtime.get("malformed_text_call_streak", 0)) + 1
                    runtime["malformed_text_call_streak"] = malformed_streak
                    if malformed_streak >= malformed_retry_budget:
                        raise RuntimeError(
                            "The model repeatedly emitted malformed or truncated textual tool calls; "
                            "no unvalidated fragment was executed. Retry this step with smaller, valid calls."
                        )
                    convo.messages.append({
                        "role": "system",
                        "content": (
                            "Your previous textual tool call was not executed because its JSON was malformed or truncated. "
                            "Do not repeat the same oversized payload. Emit only one complete valid tool-call JSON object with "
                            "all closing quotes/braces. Keep source modules compact; split a large implementation into smaller "
                            "cohesive files and write one complete file per call."
                        ),
                        "timestamp": time.time(),
                    })
                    continue
                runtime["malformed_text_call_streak"] = 0
                completion_issues = self._step_completion_issues(execution_id, step, response_text)
                if completion_issues:
                    if self._can_engine_finalize(execution_id, step):
                        return self._engine_delivery(execution_id, step)
                    convo.messages.append({
                        "role": "system",
                        "content": (
                            "Delivery rejected by automatic quality gates. Continue working autonomously with tools. "
                            "Before finishing you must: " + "; ".join(completion_issues) + "."
                        ),
                        "timestamp": time.time(),
                    })
                    continue
                # If this is the first iteration and no tools have been called yet,
                # let's prompt the agent to perform actions if needed rather than early-exiting.
                has_called_tools_in_step = any(msg.get("role") == "tool" for msg in convo.messages)
                if iteration == 1 and not has_called_tools_in_step:
                    convo.messages.append({
                        "role": "system",
                        "content": "System: You did not call any tools. If you need to perform actions (read/write files, run commands), please call the appropriate tools. If you are fully finished, please summarize your final output.",
                        "timestamp": time.time()
                    })
                    continue
                else:
                    # No tools called. Step is completed. Return content as result
                    return response_text or "Step completed without response text."

            pause_result = await self._process_step_tool_calls(
                execution_id, state, convo, tool_calls, iteration, context,
            )
            if pause_result is not None:
                return pause_result

            tool_history = state.variables.get("tool_call_history", [])
            if (self._missing_artifacts(execution_id, step) and len(tool_history) >= 8
                    and not state.variables.get("artifact_rescue_attempted")):
                state.variables["artifact_rescue_attempted"] = True
                rescued = await self._rescue_missing_artifacts(
                    execution_id, step, prerequisite_outputs,
                )
                if rescued:
                    convo.messages.append({
                        "role": "system",
                        "content": (
                            "Stall recovery created missing artifact(s): " + ", ".join(rescued) + ". "
                            "Inspect them, correct integration or syntax defects, run relevant checks, then deliver."
                        ),
                        "timestamp": time.time(),
                    })

            if self._can_engine_finalize(execution_id, step):
                nudges = int(state.variables.get("delivery_nudges", 0)) + 1
                state.variables["delivery_nudges"] = nudges
                if nudges >= 2:
                    return self._engine_delivery(execution_id, step)
                convo.messages.append({
                    "role": "system",
                    "content": (
                        "Machine-checkable delivery gates now pass. Stop calling tools and return only the compact raw JSON "
                        "delivery contract immediately. Dependent QA/integration agents will perform broader validation."
                    ),
                    "timestamp": time.time(),
                })
            else:
                state.variables["delivery_nudges"] = 0

        if self._can_engine_finalize(execution_id, step):
            return self._engine_delivery(execution_id, step)
        raise RuntimeError(
            (
                f"Step '{step_desc}' did not satisfy its delivery gates after "
                f"{stagnation_budget} consecutive stagnant iterations."
                if self.continue_while_progress else
                f"Step '{step_desc}' did not satisfy its delivery gates within "
                f"{stagnation_budget} iterations."
            )
        )

    @staticmethod
    def _shell_redirection_paths(command: str) -> List[str]:
        """Extract output redirections while ignoring operators inside quotes."""
        text = str(command or "")
        paths: List[str] = []
        quote = None
        escaped = False
        index = 0
        while index < len(text):
            character = text[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if character == "\\" and quote:
                escaped = True
                index += 1
                continue
            if character in {'"', "'"}:
                if quote == character:
                    quote = None
                elif quote is None:
                    quote = character
                index += 1
                continue
            if quote is not None or character != ">":
                index += 1
                continue

            cursor = index + 1
            if cursor < len(text) and text[cursor] == ">":
                cursor += 1
            while cursor < len(text) and text[cursor].isspace():
                cursor += 1
            # File-descriptor duplication (for example ``2>&1``) is not a
            # filesystem mutation and has no path to authorize.
            if cursor >= len(text) or text[cursor] == "&":
                index = cursor + 1
                continue

            if text[cursor] in {'"', "'"}:
                target_quote = text[cursor]
                cursor += 1
                start = cursor
                while cursor < len(text) and text[cursor] != target_quote:
                    cursor += 1
                target = text[start:cursor]
            else:
                start = cursor
                while cursor < len(text) and not text[cursor].isspace() and text[cursor] not in ";&|":
                    cursor += 1
                target = text[start:cursor]
            if target:
                paths.append(target)
            index = cursor + 1
        return paths

    @staticmethod
    def _shell_mutation_paths(command: str) -> List[str]:
        """Extract explicit file targets from common shell-based mutations."""
        text = str(command or "")
        patterns = [
            r"Path\(\s*[rRuUbBfF]*['\"]([^'\"]+)['\"]\s*\)\.(?:write_text|write_bytes|unlink|rename|replace)\b",
            r"open\(\s*[rRuUbBfF]*['\"]([^'\"]+)['\"]\s*,\s*['\"][wax+]",
            r"(?:Set-Content|Add-Content|Out-File|Remove-Item|Move-Item|Copy-Item)\b[^\r\n]*?(?:-LiteralPath|-Path|-FilePath)\s+['\"]?([^'\"\s;&|]+)",
        ]
        paths = []
        for pattern in patterns:
            paths.extend(re.findall(pattern, text, flags=re.IGNORECASE))
        paths.extend(ExecutionEngine._shell_redirection_paths(text))
        ignored = {"nul", "/dev/null", "&1", "&2"}
        return list(dict.fromkeys(
            path for path in paths if path.strip().lower() not in ignored
        ))

    async def _call_tool(self, execution_id: str, capability: str, action: str, arguments: Dict[str, Any]) -> str:
        """Invoke a tool while enforcing specialist ownership and path serialization."""
        if self.state_engine.get_execution(execution_id).status == "cancelled":
            raise asyncio.CancelledError()
        is_mutation = capability.lower() == "filesystem" and action.lower() in {"write", "delete"}
        path = str(arguments.get("path") or "")
        shell_paths = (
            self._shell_mutation_paths(str(arguments.get("command") or ""))
            if capability.lower() == "shell" and action.lower() == "execute"
            else []
        )
        if shell_paths:
            state = self.state_engine.get_execution(execution_id)
            parent_id = state.variables.get("parent_execution_id")
            contract_state = self.state_engine.get_execution(parent_id) if parent_id else state
            contract = contract_state.variables.get("delivery_contract")
            role = str(state.variables.get("role_key") or "coordinator")
            step_id = state.variables.get("plan_step_id")
            denied = [
                target for target in shell_paths
                if isinstance(contract, dict)
                and not path_is_owned(contract, step_id, role, target)
            ]
            if denied:
                self.telemetry.record(
                    "shell_ownership_violation", execution_id,
                    step_id=step_id, paths=denied,
                )
                return (
                    "Error: File ownership denied for shell mutation target(s): "
                    + ", ".join(denied)
                    + ". Use the declared owned_paths or request a debugger repair handoff."
                )
        if is_mutation:
            state = self.state_engine.get_execution(execution_id)
            parent_id = state.variables.get("parent_execution_id")
            contract_state = (
                self.state_engine.get_execution(parent_id) if parent_id else state
            )
            contract = contract_state.variables.get("delivery_contract")
            role = str(state.variables.get("role_key") or "coordinator")
            step_id = state.variables.get("plan_step_id")
            if isinstance(contract, dict) and not path_is_owned(
                contract, step_id, role, path
            ):
                self.telemetry.record(
                    "file_ownership_violation", execution_id,
                    step_id=step_id, path=path,
                )
                await self.event_bus.publish(Event(
                    type="FileOwnershipViolation",
                    payload={
                        "execution_id": execution_id,
                        "parent_execution_id": parent_id,
                        "step_id": step_id,
                        "path": path,
                    },
                ))
                return (
                    f"Error: File ownership denied for '{path}'. This specialist must only "
                    "modify its declared owned_paths; request a debugger repair handoff for shared files."
                )
            workspace = self._delivery_workspace(execution_id) or ""
            lock_key = os.path.normcase(os.path.abspath(os.path.join(workspace, path)))
            lock = self._path_locks.setdefault(lock_key, asyncio.Lock())
            async with lock:
                return await self._call_tool_impl(
                    execution_id, capability, action, arguments
                )
        return await self._call_tool_impl(execution_id, capability, action, arguments)

    async def _call_tool_impl(self, execution_id: str, capability: str, action: str, arguments: Dict[str, Any]) -> str:
        """Helper to invoke the registered capability class method."""
        if self.state_engine.get_execution(execution_id).status == "cancelled":
            raise asyncio.CancelledError()
        cap_inst = self._capabilities.get(capability.lower())
        if not cap_inst:
            return f"Error: Capability '{capability}' not registered."

        method = cap_inst.actions.get(action)
        if not method:
            return f"Error: Capability '{capability}' has no action '{action}'."

        try:
            # Check signatures and pass self/context if required.
            # In python, calling instance method pass `self` automatically if retrieved from instance,
            # but since getmembers retrieves functions, method may be unbound or bound.
            # Let's retrieve bound method from the instance itself to be safe.
            bound_method = getattr(cap_inst, method.__name__)
            
            sig = inspect.signature(bound_method)
            kwargs = dict(arguments)
            missing_arguments = [
                name for name, parameter in sig.parameters.items()
                if name != "context" and parameter.default is inspect.Parameter.empty
                and name not in kwargs
            ]
            if missing_arguments:
                return (
                    f"Error: Invalid arguments for {capability}.{action}; missing required "
                    f"argument(s): {', '.join(missing_arguments)}. Correct the tool call and retry."
                )
            if "context" in sig.parameters:
                # Compile context to pass along
                context = await self.context_engine.compile_context(
                    execution_id=execution_id,
                    conversation_id=execution_id,
                    agent_id="default_agent",
                    capabilities_schemas=[]
                )
                kwargs["context"] = context

            started = time.perf_counter()
            if inspect.iscoroutinefunction(bound_method):
                res = await bound_method(**kwargs)
            else:
                res = await asyncio.to_thread(bound_method, **kwargs)
            if inspect.isawaitable(res):
                res = await res
            result = str(res)
            self.telemetry.record("tool_completed", execution_id, capability=capability, action=action, duration_ms=round((time.perf_counter() - started) * 1000, 2), result=result)
            return result
        except Exception as e:
            self.telemetry.record("tool_failed", execution_id, capability=capability, action=action, error=str(e))
            logger.error(f"Error executing action {capability}.{action}: {e}", exc_info=True)
            return f"Error executing tool: {e}"

    @staticmethod
    def _structured_delivery(response: str) -> Dict[str, Any]:
        """Normalize a sub-agent response into a stable parent-agent contract."""
        parsed = None
        try:
            parsed = json.loads(response)
        except (TypeError, ValueError):
            text = str(response or "")
            first, last = text.find("{"), text.rfind("}")
            if first >= 0 and last > first:
                try:
                    parsed = json.loads(text[first:last + 1])
                except ValueError:
                    parsed = None
        if isinstance(parsed, dict):
            return {
                "summary": str(parsed.get("summary", "")),
                "artifacts": parsed.get("artifacts", []) if isinstance(parsed.get("artifacts", []), list) else [],
                "evidence": parsed.get("evidence", []) if isinstance(parsed.get("evidence", []), list) else [],
                "risks": parsed.get("risks", []) if isinstance(parsed.get("risks", []), list) else [],
                "next_action": str(parsed.get("next_action", "")),
            }
        return {"summary": response, "artifacts": [], "evidence": [], "risks": [], "next_action": ""}

    async def resolve_scope_approval(
        self, execution_id: str, decision: str, reason: Optional[str] = None
    ) -> None:
        """Approve or reject a frozen scope reduction before workspace execution."""
        await self.approval_coordinator.resolve_scope(execution_id, decision, reason)

    async def resume_with_decision(self, execution_id: str, decision: str, reason: Optional[str] = None):
        """
        Resumes a paused execution with the user decision ('allow' or 'reject').
        """
        await self.approval_coordinator.resolve_capability(
            execution_id, decision, reason
        )
