import asyncio
import ast
import hashlib
import json
import time
import logging
import inspect
import math
import os
import re
import shlex
import sys
import unicodedata
from contextlib import AsyncExitStack
from pathlib import Path
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
from gptmoss.core.delivery_feedback import (
    classify_assurance_report,
    classify_issue_texts,
    select_reopen_step,
    steps_to_reopen,
)
from gptmoss.core.plan_obligations import attach_plan_obligations
from gptmoss.core.corpus_policy import normalize_corpus_policy
from gptmoss.core.professional_delivery import apply_professional_profile
from gptmoss.core.document_planning import optimize_professional_document_dag
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
from gptmoss.core.document_model import DocumentSection, SectionContract
from gptmoss.core.execution_plan import (
    ROLE_ALIASES,
    ROLE_DISPLAY_NAMES,
    canonical_step_role,
    infer_step_role,
    merge_inherited_requirements,
    normalize_plan,
    parse_step_role,
    requirement_validation_commands,
    requirements_for_delegation,
    requirements_request_mutation,
)
from gptmoss.core.execution_progress import ExecutionProgressMixin
from gptmoss.core.execution_rescue import ExecutionRescueMixin
from gptmoss.core.workload import (
    build_workload_profile,
    compile_work_graph,
    partition_attachment_ids,
)



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


class ExecutionEngine(ExecutionProgressMixin, ExecutionRescueMixin):
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
        max_parallel_plan_steps: int = 0,
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
        self.max_parallel_plan_steps = max(0, int(max_parallel_plan_steps))
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
        self._active_execution_tasks: Dict[str, asyncio.Task] = {}
        self._path_locks: Dict[str, asyncio.Lock] = {}
        self._progress_file_digest_cache: Dict[str, tuple] = {}
        self.scheduler = scheduler or Scheduler()
        self.provider_recovery = ProviderRecoveryCoordinator(
            event_bus, state_engine, llm_provider,
            self.start_execution,
            self.max_step_iterations, self.scheduler,
        )
        self.delivery_coordinator = DeliveryCoordinator(state_engine, self.get_capability)
        self.approval_coordinator = ApprovalCoordinator(
            state_engine, event_bus,
            self.start_execution,
        )

    def register_capability(self, capability_name: str, instance: Any):
        """Register instantiated capability."""
        self._capabilities[capability_name.lower()] = instance
        # Ensure standard action methods are populated on instance
        instance.actions = get_actions(instance.__class__)
        logger.info(f"Registered capability: {capability_name}")

    @staticmethod
    def _bounded_dependency_results(
        results: List[Dict[str, Any]], budget: int = 8_000
    ) -> List[Dict[str, Any]]:
        """Keep every prerequisite visible while bounding its delivery payload."""
        if not results:
            return []
        allowance = max(80, max(1, int(budget)) // len(results) - 400)
        bounded: List[Dict[str, Any]] = []
        for item in results:
            copy = dict(item)
            delivery = json.dumps(
                copy.get("delivery"), ensure_ascii=False, default=str
            )
            if len(delivery) > allowance:
                notice = "… [prerequisite delivery compacted] …"
                payload = max(0, allowance - len(notice))
                head = (payload * 2) // 3
                tail = payload - head
                delivery = (
                    delivery[:head] + notice + (delivery[-tail:] if tail else "")
                )
                copy["delivery_compacted"] = True
            copy["delivery"] = delivery
            bounded.append(copy)
        return bounded

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

    def start_execution(self, execution_id: str, task: str) -> asyncio.Task:
        """Start or reuse the one owned asyncio task for an execution."""
        existing = self._active_execution_tasks.get(execution_id)
        if existing is not None and not existing.done():
            return existing
        running = asyncio.create_task(
            self.execute_task(execution_id, task),
            name=f"gptmoss-execution:{execution_id}",
        )
        self._active_execution_tasks[execution_id] = running

        def discard(completed: asyncio.Task) -> None:
            if self._active_execution_tasks.get(execution_id) is completed:
                self._active_execution_tasks.pop(execution_id, None)

        running.add_done_callback(discard)
        return running

    async def cancel_active_execution(self, execution_id: str) -> bool:
        """Cancel scheduled retries and interrupt an in-flight execution task."""
        cancelled = self.scheduler.cancel(f"execution:{execution_id}")
        cancelled = self.provider_recovery.cancel(execution_id) or cancelled
        running = self._active_execution_tasks.get(execution_id)
        if running is None or running.done():
            return cancelled
        if running is asyncio.current_task():
            return cancelled
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        if self._active_execution_tasks.get(execution_id) is running:
            self._active_execution_tasks.pop(execution_id, None)
        return True

    async def stop_active_executions(self) -> None:
        """Interrupt all owned execution work before transports are closed."""
        running = [task for task in self._active_execution_tasks.values() if not task.done()]
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._active_execution_tasks.clear()

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
        self.scheduler.start()
        due = float(run_at) if run_at is not None else time.time() + max(0.0, float(delay))
        if due <= time.time():
            self.start_execution(execution_id, task)
            return job_id
        if self.scheduler.has(job_id):
            return job_id

        def launch() -> None:
            # The timing service must remain free to deliver provider backoffs
            # while the owned execution task is running.
            self.start_execution(execution_id, task)

        self.scheduler.schedule(
            launch,
            delay=delay,
            run_at=due,
            job_id=job_id,
            metadata={"kind": "execution", "execution_id": execution_id},
        )
        return job_id

    async def stop_provider_resume_tasks(self) -> None:
        await self.provider_recovery.stop()

    async def stop_runtime_services(self) -> None:
        await self.provider_recovery.stop()
        await self.scheduler.stop(cancel_pending=True)
        await self.stop_active_executions()
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
            if action.lower() in {"write", "append"} and "content" not in normalized:
                for alias in ("text", "source", "body"):
                    if alias in normalized:
                        normalized["content"] = normalized.pop(alias)
                        break
            if action.lower() in {"write", "append"} and not normalized.get("path"):
                content = normalized.get("content")
                if isinstance(content, str):
                    first_line = content.strip().splitlines()[0] if content.strip() else ""
                    candidate = first_line.removeprefix("File:").strip().strip(chr(34) + chr(39) + "`")
                    if re.fullmatch(r"[\w .()/-]+\.[A-Za-z0-9]{1,10}", candidate.replace(chr(92), "/")):
                        normalized["path"] = candidate
                        normalized["content"] = ExecutionRescueMixin._strip_code_fence(
                            "\n".join(content.strip().splitlines()[1:]), candidate,
                        )
            if action.lower() == "replace_paragraph":
                if "paragraph_prefix" not in normalized:
                    for alias in ("prefix", "old_text", "match_text"):
                        if normalized.get(alias):
                            normalized["paragraph_prefix"] = normalized.pop(alias)
                            break
                if "content" not in normalized:
                    for alias in ("new_text", "replacement", "text", "body"):
                        if alias in normalized:
                            normalized["content"] = normalized.pop(alias)
                            break
            if action.lower() == "replace_section":
                if "heading_selector" not in normalized:
                    for alias in ("heading", "section_heading", "selector"):
                        if normalized.get(alias):
                            normalized["heading_selector"] = normalized.pop(alias)
                            break
                if "content" not in normalized:
                    for alias in ("new_text", "replacement", "text", "body"):
                        if alias in normalized:
                            normalized["content"] = normalized.pop(alias)
                            break
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

    def _writer_incremental_repair_tool(
        self,
        execution_id: str,
        role_key: str,
        step: Dict[str, Any],
        issues: List[str],
    ) -> str:
        """Return the exact mutation required to repair a long writer delivery."""
        targeted_markers = (
            "arithmetic sum mismatch", "source inventory total mismatch",
            "duplicate paragraph", "duplicate heading", "invalid local reference",
            "lack a local reference",
        )
        replacement_markers = (
            "citation-like pattern",
            "external link", "placeholder marker", "reasoning tag",
            "heading numbering restart",
        )
        section_markers = ("record section", "invalid diagram")
        append_markers = (
            "uncited required source", "cited_sources=", "local_references=",
        )
        if not any(
            marker in issue
            for issue in issues
            for marker in (
                "words=", "empty required section", "lack a local reference",
                *targeted_markers,
                *section_markers,
                *replacement_markers,
                *append_markers,
            )
        ):
            return ""
        artifacts = [str(path).strip() for path in step.get("required_artifacts", []) if str(path).strip()]
        if not artifacts:
            return ""
        target = artifacts[0]
        if any(marker in issue for issue in issues for marker in targeted_markers):
            return "filesystem__replace_paragraph"
        if any(marker in issue for issue in issues for marker in section_markers):
            return "filesystem__replace_section"
        # Missing real evidence takes precedence over citation examples that
        # happen to be wrapped in Markdown code.  Appending the missing plain
        # bounded citation can satisfy both observations without destroying a
        # valid document merely to remove harmless syntax examples.
        if any(marker in issue for issue in issues for marker in append_markers):
            return "filesystem__append"
        if any(marker in issue for issue in issues for marker in replacement_markers):
            return "filesystem__write"
        exists = self._artifact_exists(execution_id, target)
        return "filesystem__append" if exists else "filesystem__write"

    def _writer_incremental_repair_nudge(
        self,
        execution_id: str,
        role_key: str,
        step: Dict[str, Any],
        issues: List[str],
    ) -> str:
        """Turn long-document gate failures into one bounded executable action."""
        action = self._writer_incremental_repair_tool(
            execution_id, role_key, step, issues,
        )
        if not action:
            return ""
        artifacts = [str(path).strip() for path in step.get("required_artifacts", []) if str(path).strip()]
        target = artifacts[0]
        if action == "filesystem__replace_paragraph":
            if any("duplicate heading" in str(issue).casefold() for issue in issues):
                return (
                    f" Do not answer with a plan. Your next response must be exactly one valid "
                    f"{action} tool call targeting '{target}'. Copy one exact Markdown heading "
                    "selector (including its # markers) reported by the gate, set occurrence=2, "
                    "and set content to an empty string. This removes only the repeated heading "
                    "line and preserves all section body content. Change exactly one heading "
                    "occurrence per iteration; never rewrite or delete the whole document."
                )
            return (
                f" Do not answer with a plan. Your next response must be exactly one valid "
                f"{action} tool call targeting '{target}'. Use a paragraph prefix reported by "
                "the gate. For a duplicate, remove occurrence=2 with empty content. For an "
                "unsupported claim, replace occurrence=1 with one corrected, evidence-grounded "
                "paragraph containing a valid nearby bounded local citation. For an arithmetic "
                "or inventory-total mismatch, preserve the paragraph and replace every incorrect "
                "total in it with the calculated value reported by the gate. For an invalid local "
                "reference, preserve the surrounding Markdown and correct its source plus one-based "
                "block or slide bounds from the gate. Change exactly one "
                "paragraph per iteration; never rewrite or delete the whole document."
            )
        if action == "filesystem__replace_section":
            if any("invalid diagram" in str(issue).casefold() for issue in issues):
                return (
                    f" Do not answer with a plan. Your next response must be exactly one valid "
                    f"{action} tool call targeting '{target}'. Copy the exact Markdown section "
                    "selector reported for the invalid diagram and replace only that section body "
                    "with one complete, syntactically valid Mermaid diagram plus concise explanatory "
                    "prose and nearby bounded local citations. Eliminate every reported semantic "
                    "diagram defect, including self-loops, while preserving all other sections."
                )
            return (
                f" Do not answer with a plan. Your next response must be exactly one valid "
                f"{action} tool call targeting '{target}'. Copy one exact Markdown heading "
                "selector (including its # markers) reported by the gate and replace only that "
                "section body with complete required fields, evidence and nearby valid bounded "
                "local citations. Do not include the selected heading in content. Repair exactly "
                "one record per iteration; every other record and section must remain untouched."
            )
        chunk_size = "400-800 word"
        if action == "filesystem__append":
            if any(
                marker in str(issue).casefold()
                for issue in issues
                for marker in ("uncited required source", "cited_sources=", "local_references=")
            ):
                continuity = (
                    "Preserve the existing valid content and append exactly one short prose "
                    "paragraph citing every currently missing source exactly once with a plain, "
                    "one-based bounded locator from source_inventory. Do not add a heading, list, "
                    "table, requirement matrix, or repeat existing content"
                )
                chunk_size = "40-120 word"
            else:
                continuity = (
                    "Preserve the existing valid content and append the next missing or underdeveloped section"
                )
                chunk_size = "400-800 word"
        elif self._artifact_exists(execution_id, target):
            continuity = (
                "Replace the defective document with only the first clean, complete section; "
                "later turns will append the remaining non-duplicated sections"
            )
        else:
            continuity = "Create only the first complete section"
        return (
            f" Do not answer with a plan and do not send the whole document in one call. "
            f"Your next response must be exactly one valid {action} tool call targeting '{target}'. "
            f"{continuity} in a bounded {chunk_size} chunk with nearby valid local citations. "
            "Repeat with another bounded append call on later iterations until every gate passes; "
            "never create undeclared part files."
        )

    @staticmethod
    def _document_coverage_repair_tool(issues: List[str]) -> str:
        """Select the read-only tool required by a failed corpus coverage gate."""
        for issue in issues:
            normalized = str(issue or "").casefold()
            if (
                "read every normalized block" in normalized
                or "prove complete document coverage" in normalized
            ):
                return "documents__read"
        for issue in issues:
            if "analyze every attached image" in str(issue or "").casefold():
                return "documents__read_image"
        return ""

    @classmethod
    def _document_coverage_repair_nudge(cls, issues: List[str]) -> str:
        """Turn corpus gate failures into one bounded, executable read action."""
        action = cls._document_coverage_repair_tool(issues)
        if not action:
            return ""
        if action == "documents__read":
            issue = next(
                (
                    str(item) for item in issues
                    if "read every normalized block" in str(item).casefold()
                ),
                "",
            )
            filename_match = re.search(
                r"read every normalized block of (.+?);", issue,
                flags=re.IGNORECASE,
            )
            block_match = re.search(
                r"missing 1-based block\(s\):\s*(\d+)", issue,
                flags=re.IGNORECASE,
            )
            target = filename_match.group(1).strip() if filename_match else "the first incomplete attachment"
            start_block = max(0, int(block_match.group(1)) - 1) if block_match else 0
            return (
                " Do not describe or simulate the read. Your next response must be exactly one valid "
                f"{action} tool call for '{target}', with start_block={start_block} and a bounded "
                "block_count no greater than 200. Use the real tool result as evidence; prose that merely "
                "claims blocks were read does not satisfy the gate."
            )
        issue = next(
            (
                str(item) for item in issues
                if "analyze every attached image" in str(item).casefold()
            ),
            "",
        )
        missing_match = re.search(r"missing:\s*([^,;]+)", issue, flags=re.IGNORECASE)
        target = missing_match.group(1).strip() if missing_match else "the first missing image"
        return (
            " Do not describe or simulate visual inspection. Your next response must be exactly one valid "
            f"{action} tool call for '{target}'. Use the injected image on the following model turn; prose "
            "that merely claims the image was analyzed does not satisfy the gate."
        )

    def _required_artifact_initialization_tool(
        self,
        execution_id: str,
        step: Dict[str, Any],
        issues: List[str],
    ) -> str:
        """Require a bounded first write when an owned text artifact is absent."""
        if not any(
            "create non-empty required artifacts" in str(issue or "").casefold()
            for issue in issues
        ):
            return ""
        textual_suffixes = {
            ".css", ".csv", ".html", ".ini", ".js", ".json", ".jsonl",
            ".jsx", ".md", ".py", ".pyi", ".sh", ".toml", ".ts", ".tsx",
            ".txt", ".xml", ".yaml", ".yml",
        }
        return "filesystem__write" if any(
            not self._artifact_exists(execution_id, path)
            and Path(str(path)).suffix.casefold() in textual_suffixes
            for path in step.get("required_artifacts", [])
        ) else ""

    def _required_artifact_initialization_nudge(
        self,
        execution_id: str,
        step: Dict[str, Any],
        issues: List[str],
    ) -> str:
        """Constrain first artifact creation so prompt-fallback JSON stays complete."""
        action = self._required_artifact_initialization_tool(
            execution_id, step, issues,
        )
        if not action:
            return ""
        target = next(
            str(path) for path in step.get("required_artifacts", [])
            if not self._artifact_exists(execution_id, str(path))
            and Path(str(path)).suffix.casefold() in {
                ".css", ".csv", ".html", ".ini", ".js", ".json", ".jsonl",
                ".jsx", ".md", ".py", ".pyi", ".sh", ".toml", ".ts", ".tsx",
                ".txt", ".xml", ".yaml", ".yml",
            }
        )
        prose_suffixes = {".html", ".md", ".txt"}
        bounded_content = (
            "Write one self-contained 300-500 word first section with the required nearby evidence references"
            if Path(target).suffix.casefold() in prose_suffixes
            else "Write one small, syntactically complete initial unit no larger than 4,000 characters"
        )
        return (
            " Do not answer with a plan and do not serialize the entire artifact in one response. "
            f"Your next response must be exactly one valid {action} tool call targeting '{target}'. "
            f"{bounded_content}. Later turns can append further bounded sections after the first call succeeds."
        )

    def _quality_repair_directive(
        self,
        execution_id: str,
        role_key: str,
        step: Dict[str, Any],
        issues: List[str],
    ) -> tuple[str, str]:
        """Choose one safe repair, always gathering missing evidence before mutation."""
        coverage_tool = self._document_coverage_repair_tool(issues)
        if coverage_tool:
            return coverage_tool, self._document_coverage_repair_nudge(issues)
        artifact_tool = self._writer_incremental_repair_tool(
            execution_id, role_key, step, issues,
        )
        if artifact_tool:
            return artifact_tool, self._writer_incremental_repair_nudge(
                execution_id, role_key, step, issues,
            )
        initialization_tool = self._required_artifact_initialization_tool(
            execution_id, step, issues,
        )
        return initialization_tool, self._required_artifact_initialization_nudge(
            execution_id, step, issues,
        )

    @staticmethod
    def _schemas_for_required_tool(
        schemas: List[Dict[str, Any]], required_tool: str,
    ) -> List[Dict[str, Any]]:
        """Expose only the mutation mandated by the preceding quality gate."""
        if not required_tool:
            return schemas
        return [
            schema for schema in schemas
            if schema.get("function", {}).get("name") == required_tool
        ]

    @staticmethod
    def _schemas_for_inherited_document_coverage(
        schemas: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Hide redundant bulk reads while retaining targeted document search."""
        redundant = {
            "documents__inventory", "documents__read", "documents__read_chunk",
        }
        return [
            schema for schema in schemas
            if schema.get("function", {}).get("name") not in redundant
        ]

    @staticmethod
    def _required_tool_succeeded(
        messages: List[Dict[str, Any]],
        tool_calls: List[Dict[str, Any]],
        required_tool: str,
    ) -> bool:
        expected_ids = {
            str(call.get("id") or "")
            for call in tool_calls
            if call.get("function", {}).get("name") == required_tool
        }
        return bool(expected_ids) and any(
            message.get("role") == "tool"
            and str(message.get("tool_call_id") or "") in expected_ids
            and not str(message.get("content") or "").lstrip().startswith("Error:")
            for message in messages
        )

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
                    and item.get("action") in {
                        "write", "append", "replace_paragraph", "replace_section", "delete",
                    }
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
        if capability.lower() == "documents" and action.lower() in {
            "read_image", "read_images",
        }:
            try:
                payload = json.loads(str(result))
            except (TypeError, ValueError):
                return
            requested = []
            if action.lower() == "read_image" and payload.get("artifact_id"):
                requested = [payload["artifact_id"]]
            elif isinstance(payload.get("images"), list):
                requested = [
                    item.get("artifact_id") for item in payload["images"]
                    if isinstance(item, dict) and item.get("artifact_id")
                ]
            pending = state.variables.setdefault("pending_visual_artifact_ids", [])
            for artifact_id in requested:
                if artifact_id not in pending:
                    pending.append(artifact_id)

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
        policy = state.variables.get("corpus_policy") if hasattr(state, "variables") else {}
        professional = isinstance(policy, dict) and policy.get("professional_delivery")
        if not professional and not any(marker in str(task).casefold() for marker in (
            "dossier", "rapport", "livrable", "long-form", "document-analysis",
            "rédige", "redige", "write a document", "professional document",
            "professional report",
        )):
            return
        workspace = self._delivery_workspace(execution_id)
        if not workspace:
            return
        workspace_path = Path(workspace).resolve()
        checkpoint_root = workspace_path / ".gptmoss" / "document-state"
        engine = LongDocumentEngine(checkpoint_root)
        try:
            model = engine.resume(execution_id)
        except ValueError as exc:
            logger.warning("Recreating corrupt document checkpoint for %s: %s", execution_id, exc)
            model = None
        primary = str(plan.get("primary_artifact") or "deliverable.md")
        headings: list[str] = []
        primary_minimum_words = 0
        for artifact_policy in plan.get("artifact_validations", []):
            if artifact_policy.get("path") != primary:
                continue
            constraints = artifact_policy.get("constraints", {})
            headings = [str(item) for item in constraints.get("required_headings", [])]
            minimums = constraints.get("minimums") or {}
            if isinstance(minimums, dict):
                try:
                    primary_minimum_words = max(0, int(minimums.get("words") or 0))
                except (TypeError, ValueError):
                    primary_minimum_words = 0
            break
        if model is None:
            output_path = (workspace_path / primary).resolve()
            if output_path == workspace_path or workspace_path not in output_path.parents:
                logger.warning("Unsafe primary document path %r; using deliverable.md", primary)
                primary = "deliverable.md"
                output_path = workspace_path / primary
            model = engine.create_model(execution_id, task, str(output_path), plan.get("requirements", []))
            if not headings:
                headings = [
                    str(step.get("specialist") or f"Section {index}")
                    for index, step in enumerate(plan.get("steps", []), 1)
                    if step.get("role") in {"architect", "security", "writer"}
                ]
            selected_headings = headings or ["Executive Summary", "Architecture", "Conclusion"]
            target_words = max(
                self.document_target_section_words,
                math.ceil(primary_minimum_words / max(1, len(selected_headings))),
            )
            engine.plan_sections(
                model,
                selected_headings,
                requirements=plan.get("requirements", []),
                target_words=target_words,
            )
        else:
            # A resume must inherit stricter policies introduced after the
            # checkpoint was first created. Preserve written content while
            # increasing section contracts and refreshing requirements.
            model.requirements = [dict(item) for item in plan.get("requirements", [])]
            if headings and not any(section.content for section in model.sections):
                target_words = max(
                    self.document_target_section_words,
                    math.ceil(primary_minimum_words / max(1, len(headings))),
                )
                engine.plan_sections(
                    model,
                    headings,
                    requirements=model.requirements,
                    target_words=target_words,
                )
            elif headings:
                folded_existing = {
                    section.contract.heading.casefold() for section in model.sections
                }
                for heading in headings:
                    if heading.casefold() in folded_existing:
                        continue
                    section_number = len(model.sections) + 1
                    model.sections.append(DocumentSection(contract=SectionContract(
                        section_id=f"SEC-{section_number:03d}",
                        heading=heading,
                        purpose=f"Explain {heading} with source-grounded facts, decisions and consequences.",
                        target_words=self.document_target_section_words,
                        required_topics=[heading],
                        dependencies=[f"SEC-{section_number - 1:03d}"] if section_number > 1 else [],
                    )))
                    folded_existing.add(heading.casefold())
            section_count = max(1, len(model.sections))
            target_words = max(
                self.document_target_section_words,
                math.ceil(primary_minimum_words / section_count),
            )
            for section in model.sections:
                section.contract.target_words = max(
                    int(section.contract.target_words or 0), target_words
                )
            assigned_ids = {
                identifier
                for section in model.sections
                for identifier in section.contract.requirement_ids
            }
            missing_ids = [
                str(item.get("id")) for item in model.requirements
                if item.get("id") and str(item.get("id")) not in assigned_ids
            ]
            for index, identifier in enumerate(missing_ids):
                if model.sections:
                    model.sections[index % len(model.sections)].contract.requirement_ids.append(identifier)
            model.revision += 1
            engine.store.save(model)
        state.variables["document_model_checkpoint"] = str(
            Path(".gptmoss") / "document-state" / engine.store.path_for(execution_id).name
        ).replace("\\", "/")
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
        if not state.variables.get("document_model_checkpoint"):
            return
        try:
            workspace = self._delivery_workspace(execution_id)
            if not workspace:
                return
            workspace_path = Path(workspace).resolve()
            engine = LongDocumentEngine(workspace_path / ".gptmoss" / "document-state")
            model = engine.resume(execution_id)
            if model is None:
                return
            output = Path(model.output_path).resolve()
            if output == workspace_path or workspace_path not in output.parents:
                logger.warning("Ignoring unsafe document output path for %s: %s", execution_id, output)
                return
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
            if not is_sub_agent and self.artifact_store:
                state.variables["workload_profile"] = build_workload_profile(
                    self.artifact_store,
                    state.variables.get("attachment_ids", []),
                    corpus_summaries=state.variables.get("corpus_summaries", []),
                    supports_vision=bool(
                        getattr(self.llm_provider, "supports_vision", False)
                    ),
                )
                state.variables["corpus_policy"] = normalize_corpus_policy(
                    state.variables.get("corpus_policy"),
                    enabled=bool(state.variables.get("corpus_auto_workflow")),
                    workload_profile=state.variables["workload_profile"],
                )
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
                    planning_mode=state.variables.get("planning_mode"),
                    workload_profile=state.variables.get("workload_profile"),
                    corpus_auto_workflow=bool(state.variables.get("corpus_auto_workflow")),
                    corpus_policy=state.variables.get("corpus_policy"),
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
            plan_result = optimize_professional_document_dag(plan_result)
            plan_result["corpus_policy"] = dict(
                state.variables.get("corpus_policy") or {}
            )
            if not is_sub_agent:
                plan_result = compile_work_graph(
                    plan_result,
                    state.variables.get("workload_profile"),
                    planning_mode=str(state.variables.get("planning_mode") or "auto"),
                )
                attach_plan_obligations(
                    plan_result,
                    task=task,
                    planning_mode=str(state.variables.get("planning_mode") or "auto"),
                    analysis=plan_result.get("analysis"),
                    workload_profile=state.variables.get("workload_profile"),
                    corpus_auto_workflow=bool(state.variables.get("corpus_auto_workflow")),
                    corpus_policy=state.variables.get("corpus_policy"),
                    repair=True,
                    validate=True,
                )
            plan_result = normalize_plan(plan_result)
            self.telemetry.record("plan_generated", execution_id, duration_ms=round((time.perf_counter() - planning_started) * 1000, 2), steps=len(plan_result.get("steps", [])))
            state.current_plan = plan_result
            self._initialize_document_state(execution_id, task, plan_result, state)
            state.variables["delivery_contract"] = build_delivery_contract(
                state.current_plan, task,
                repair_obligations=not bool(state.variables.get("parent_execution_id")),
            )
            state.current_step = 0
            await self.event_bus.publish(Event(
                type="PlanGenerated",
                payload={"execution_id": execution_id, "plan": plan_result}
            ))

        state.current_plan = normalize_plan(state.current_plan)
        # Reapply the deterministic profile on resume so persisted plans gain
        # newly introduced quality gates without requiring replanning or losing
        # completed work. The operation is idempotent and preserves stricter
        # planner/user constraints.
        state.current_plan = apply_professional_profile(
            state.current_plan,
            self.artifact_store,
            state.variables.get("attachment_ids", []),
        )
        state.current_plan = optimize_professional_document_dag(state.current_plan)
        self._initialize_document_state(execution_id, task, state.current_plan, state)
        # Rebuild after deterministic profile upgrades. A persisted contract
        # must never keep weaker validations or stale requirement ownership.
        state.variables["delivery_contract"] = build_delivery_contract(
            state.current_plan, task,
            repair_obligations=not bool(state.variables.get("parent_execution_id")),
        )
        if not state.variables.get("parent_execution_id"):
            reopened = self._reopen_invalid_completed_steps(
                execution_id,
                state,
                state.current_plan.get("steps", []),
            )
            if reopened:
                self.telemetry.record(
                    "persisted_steps_reopened", execution_id,
                    step_ids=[step.get("id") for step in reopened],
                )
        delivery_contract = state.variables["delivery_contract"]
        scope_changes = delivery_contract.get("scope_changes", [])
        approved_contract = state.variables.get("approved_scope_contract_sha256")
        scope_changes_sha256 = delivery_contract.get("scope_changes_sha256")
        approved_scope = state.variables.get("approved_scope_changes_sha256")
        scope_is_approved = bool(
            (scope_changes_sha256 and approved_scope == scope_changes_sha256)
            or approved_contract == delivery_contract.get("contract_sha256")
        )
        if (not state.variables.get("parent_execution_id") and scope_changes
                and not scope_is_approved):
            self.state_engine.transition_execution(
                state, "paused", reason="scope approval required", actor="runtime"
            )
            state.variables["pending_scope_approval"] = {
                "contract_sha256": delivery_contract.get("contract_sha256"),
                "scope_changes_sha256": scope_changes_sha256,
                "changes": scope_changes,
            }
            self.state_engine.save_to_disk()
            await self.event_bus.publish(Event(
                type="ScopeApprovalRequested",
                payload={
                    "execution_id": execution_id,
                    "contract_sha256": delivery_contract.get("contract_sha256"),
                    "scope_changes_sha256": scope_changes_sha256,
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

        try:
            await self._coordinate_plan_execution(
                execution_id, state, steps, task, run_step, running_tasks
            )
        finally:
            unfinished = [item for item in running_tasks.values() if not item.done()]
            for item in unfinished:
                item.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

    def _parallel_plan_step_limit(self) -> int:
        """Return an in-flight limit without constraining total plan size."""
        if self.max_parallel_plan_steps > 0:
            return self.max_parallel_plan_steps
        cpu_limit = max(1, min(4, ((os.cpu_count() or 2) + 1) // 2))
        try:
            provider_limit = int(
                getattr(self.llm_provider, "recommended_parallel_requests", 0) or 0
            )
        except (TypeError, ValueError):
            provider_limit = 0
        return max(1, min(cpu_limit, provider_limit)) if provider_limit else cpu_limit

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
                        target = classify_assurance_report(assurance_report)
                        repair_step = select_reopen_step(
                            state.current_plan or {"steps": steps}, target
                        )
                        repair_budget = (
                            self._step_retry_budget(task, repair_step)
                            if repair_step is not None else self.max_step_retries
                        )
                        if repair_step is not None and repair_round < repair_budget:
                            state.variables["assurance_repair_round"] = repair_round + 1
                            reopened = steps_to_reopen(
                                state.current_plan or {"steps": steps}, target, repair_step
                            )
                            context = (
                                "Independent delivery assurance rejected the assembled project. "
                                "Fix these machine-observed defects without redoing validated work:\n"
                                + json.dumps(assurance_report, ensure_ascii=False)[:10_000]
                            )
                            for item in reopened:
                                item["status"] = "pending"
                                item.pop("assigned_execution_id", None)
                                item["retry_context"] = context
                            if target.required_tool:
                                runtime = state.variables.setdefault("step_runtime", {}).setdefault(
                                    str(repair_step.get("id")), {}
                                )
                                runtime["required_next_tool"] = target.required_tool
                            await self.event_bus.publish(Event(
                                type="DeliveryRepairScheduled",
                                payload={
                                    "execution_id": execution_id,
                                    "round": repair_round + 1,
                                    "step_id": repair_step.get("id"),
                                    "obligation": target.obligation,
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
                    workspace = self._delivery_workspace(execution_id)
                    package = None
                    delivery_plan = state.current_plan or {}
                    try:
                        if workspace:
                            package = build_delivery_package(
                                workspace, execution_id, delivery_plan,
                                assurance_report,
                                diagram_rendering=self.diagram_rendering,
                                docx_embed_diagrams=self.docx_embed_diagrams,
                            )
                    except (OSError, TypeError, ValueError) as error:
                        self.state_engine.transition_execution(
                            state, "failed", reason="delivery packaging failed", actor="runtime"
                        )
                        state.results["error"] = f"Delivery packaging failed: {error}"
                        await self.event_bus.publish(Event(
                            type="ExecutionFailed",
                            payload={"execution_id": execution_id, "error": state.results["error"]},
                        ))
                        break
                    if delivery_plan.get("delivery_profile") == "professional-local" and not package:
                        self.state_engine.transition_execution(
                            state, "failed", reason="professional package missing", actor="runtime"
                        )
                        state.results["error"] = (
                            "Professional delivery passed assurance but its DOCX/manifest/ZIP package "
                            "could not be created."
                        )
                        await self.event_bus.publish(Event(
                            type="ExecutionFailed",
                            payload={"execution_id": execution_id, "error": state.results["error"]},
                        ))
                        break
                    if package:
                        state.results["delivery_package"] = package
                    self.state_engine.transition_execution(
                        state, "completed", reason="delivery completed", actor="runtime"
                    )
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
                
            # Preserve an arbitrarily large DAG while bounding only the active
            # provider wave. Pending steps remain durable and are scheduled as
            # capacity becomes available.
            parallel_limit = self._parallel_plan_step_limit()
            parent_state.variables["plan_parallelism_limit"] = parallel_limit
            capacity = max(0, parallel_limit - len(running_tasks))
            for step in ready_steps[:capacity]:
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
                dependency_results = self._bounded_dependency_results(
                    dependency_results
                )
                handoff = json.dumps(dependency_results, ensure_ascii=False)
                if len(handoff) > 8_000:
                    handoff = handoff[:8_000] + "\n… [dependency handoff truncated]"
                sub_task = step["description"]
                if step.get("retry_context"):
                    sub_task += (
                        "\n\nAUTONOMOUS RETRY: A previous specialist attempt did not satisfy its delivery gates. "
                        "Reuse every valid partial artifact and correct the root cause. When the machine defects "
                        "already provide an exact selector, act on that selector before any broad inspection; "
                        "never reread the complete artifact merely to rediscover a reported defect. "
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
                sub_exec.variables["attachment_ids"] = partition_attachment_ids(
                    state.variables.get("attachment_ids", []),
                    step.get("source_partition"),
                )
                sub_exec.variables["workload_profile"] = state.variables.get(
                    "workload_profile", {}
                )
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
                    self.start_execution(sub_id, sub_exec.variables["task"])

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
                        self.start_execution(sub_id, sub_exec.variables["task"])

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
                        and item.get("action") in {
                            "write", "append", "replace_paragraph", "replace_section", "delete",
                        }
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
        inherited_document_coverage = self._inherits_complete_document_coverage(
            execution_id, step,
        )
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
            if inherited_document_coverage:
                reuse_instruction += (
                    " Machine-verified complete document coverage was inherited from the exact prior "
                    "assignment. Do not inventory or reread the corpus; preserve the existing artifact, "
                    "and apply only machine-reported repairs. If a defect includes an exact paragraph or "
                    "Markdown heading selector, mutate it directly without reading the whole artifact; "
                    "otherwise use one bounded filesystem.read window only around the necessary location."
                )
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
                            coverage_issues = self._document_coverage_issues(
                                execution_id, step,
                            )
                            coverage_nudge = self._document_coverage_repair_nudge(
                                coverage_issues,
                            )
                            if coverage_nudge:
                                guidance = (
                                    "STAGNATION WARNING: repeated inspection has produced no new durable corpus "
                                    "coverage. Stop promising or narrating reads and perform the next missing bounded "
                                    "document action now." + coverage_nudge
                                )
                            else:
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
            if inherited_document_coverage:
                schemas = self._schemas_for_inherited_document_coverage(schemas)
            required_next_tool = str(runtime.get("required_next_tool") or "")
            if required_next_tool:
                constrained_schemas = self._schemas_for_required_tool(
                    schemas, required_next_tool,
                )
                if constrained_schemas:
                    schemas = constrained_schemas

            # Compile context
            context = await self.context_engine.compile_context(
                execution_id=execution_id,
                conversation_id=execution_id,
                agent_id="default_agent",
                capabilities_schemas=schemas
            )
            if self.artifact_store and state.variables.get("attachment_ids"):
                provider_budget = int(
                    getattr(self.llm_provider, "context_input_budget_chars", 0) or 0
                )
                history_used = sum(
                    len(str(item.get("content") or ""))
                    for item in context.get("conversation_history", [])
                )
                reserved_prompt = max(12_000, provider_budget // 3) if provider_budget else 12_000
                available_for_sources = max(
                    4_000,
                    provider_budget - history_used - reserved_prompt,
                ) if provider_budget else int(context.get("context_budget_chars") or 12_000)
                configured_attachment_budget = int(self.artifact_store.max_text_chars or 0)
                attachment_text_budget = min(
                    available_for_sources,
                    configured_attachment_budget or available_for_sources,
                )
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
                pending_images = list(dict.fromkeys(
                    str(item) for item in state.variables.get(
                        "pending_visual_artifact_ids", []
                    ) if item
                ))
                provider_input_tokens = int(
                    getattr(self.llm_provider, "context_input_budget_tokens", 0) or 0
                )
                visual_slots = (
                    max(0, min(4, (provider_input_tokens - 4_096) // 4_096))
                    if provider_input_tokens else 4
                )
                automatic_images = (
                    visual_slots if iteration == 1 and not pending_images else 0
                )
                context["attachments"] = self.artifact_store.context_items(
                    state.variables["attachment_ids"],
                    getattr(self.llm_provider, "supports_vision", False),
                    max_text_chars=attachment_text_budget,
                    query=attachment_query,
                    max_items=max(8, min(96, attachment_text_budget // 2_000)),
                    max_images=(
                        min(visual_slots, max(automatic_images, len(pending_images)))
                        if getattr(self.llm_provider, "supports_vision", False) else 0
                    ),
                    max_image_bytes=min(
                        8 * 1024 * 1024,
                        max(256 * 1024, available_for_sources * 8),
                    ),
                    preferred_artifact_ids=pending_images,
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
                    "documents.read_chunk to retrieve text; use documents.read_image or "
                    "documents.read_images to load specific images in bounded batches. "
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
                    "canonical source of continuity. Build an oversized owned artifact with one filesystem.write call for "
                    "its first bounded section followed by filesystem.append calls for later sections; do not create undeclared "
                    "temporary part files. When a quality gate reports the normalized prefix of one duplicate or unsupported "
                    "paragraph, repair only that occurrence with filesystem.replace_paragraph instead of rebuilding the artifact. "
                    "When a gate reports a defective named record section, repair only its body with "
                    "filesystem.replace_section using the exact reported Markdown heading selector. "
                    "Never invent a source, citation locator, diagram, metric, or validation result."
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
                "\nArtifact validation specifications are enforced automatically by the execution engine after your structured "
                "delivery response. Do not invoke repository-only validator scripts or fabricate constraints/report inputs. "
                "Create and read back only your owned artifacts, then return the structured response to trigger validation; "
                "if the engine rejects it, repair the reported violations and retry."
                " Write bounded local evidence citations as plain Markdown text such as "
                "[source.ext > Section > blocks 1-3], never inside inline-code backticks or code fences. "
                "Citation bounds are strictly one-based and inclusive even though documents.read "
                "start_block and returned block.order values are zero-based. Use only `blocks N-M` "
                "for documents or `slide N`/`slides N-M` for presentations; never use `sections`, "
                "zero bounds, raw tool offsets, or a range outside source_inventory."
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
            visual_attachments = []
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
                    visual_attachments.append(attachment)
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
            # Requested images are deliberately the most recent messages so
            # provider compaction preserves them ahead of older conversation.
            for attachment in visual_attachments:
                llm_messages.append({"role": "user", "content": [
                    {"type": "text", "text": (
                        f"Attached image: {attachment['filename']} "
                        f"[artifact_id:{attachment['id']}]"
                    )},
                    {"type": "image_url", "image_url": {"url": attachment["image_url"]}},
                ]})

            await self.event_bus.publish(Event(
                type="LLMRequest",
                payload={"execution_id": execution_id, "messages": llm_messages}
            ))

            async def on_text_delta(delta: str) -> None:
                await self.event_bus.publish(Event(
                    type="LLMDelta",
                    payload={
                        "execution_id": execution_id,
                        "delta": delta,
                        "step_index": state.current_step,
                    },
                ))

            llm_started = time.perf_counter()
            submitted_visual_ids: set[str] = set()

            def record_fitted_context(messages: List[Dict[str, Any]]) -> None:
                submitted_visual_ids.clear()
                for message in messages:
                    content = message.get("content")
                    if not isinstance(content, list):
                        continue
                    has_image = any(
                        isinstance(part, dict) and part.get("type") == "image_url"
                        for part in content
                    )
                    if not has_image:
                        continue
                    text = " ".join(
                        str(part.get("text") or "") for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                    submitted_visual_ids.update(
                        re.findall(r"\[artifact_id:([^\]]+)\]", text)
                    )

            llm_response = await self._completion_with_recovery(
                execution_id,
                messages=llm_messages,
                tools=schemas if schemas else None,
                tool_choice="required" if required_next_tool else None,
                on_text_delta=on_text_delta,
                on_context_fitted=record_fitted_context,
            )
            if visual_attachments:
                visualized = state.variables.setdefault(
                    "visualized_artifact_ids", []
                )
                delivered_ids = set(submitted_visual_ids)
                for artifact_id in delivered_ids:
                    if artifact_id not in visualized:
                        visualized.append(artifact_id)
                state.variables["pending_visual_artifact_ids"] = [
                    item for item in state.variables.get(
                        "pending_visual_artifact_ids", []
                    ) if item not in delivered_ids
                ]
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
                    r"(?:<tool_code\b|\"?tool_call\"?\s*:|\"?name\"?\s*:\s*\"?(?:filesystem|documents|shell|agent|devteam)__)",
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
                    malformed_issues = self._step_completion_issues(
                        execution_id, step, response_text,
                    )
                    required_repair_tool, repair_nudge = self._quality_repair_directive(
                        execution_id, role_for_step, step, malformed_issues,
                    )
                    if required_repair_tool:
                        runtime["required_next_tool"] = required_repair_tool
                        runtime["required_repair_issues"] = [
                            str(item) for item in malformed_issues if str(item).strip()
                        ]
                    convo.messages.append({
                        "role": "system",
                        "content": (
                            "Your previous textual tool call was not executed because its JSON was malformed or truncated. "
                            "Do not repeat the same oversized payload. Emit only one complete valid tool-call JSON object with "
                            "all closing quotes/braces. Keep source modules compact and split a large implementation into smaller "
                            "cohesive files. If a text artifact is absent, write only its first bounded section; if it already "
                            "exists, preserve it and use append or a targeted paragraph replacement. Never overwrite an existing "
                            "long document merely to recover from malformed JSON; do not create undeclared temporary part files."
                            + repair_nudge
                        ),
                        "timestamp": time.time(),
                    })
                    continue
                runtime["malformed_text_call_streak"] = 0
                completion_issues = self._step_completion_issues(execution_id, step, response_text)
                if completion_issues:
                    if self._can_engine_finalize(execution_id, step):
                        return self._engine_delivery(execution_id, step)
                    required_repair_tool, repair_nudge = self._quality_repair_directive(
                        execution_id, role_for_step, step, completion_issues,
                    )
                    if required_repair_tool:
                        runtime["required_next_tool"] = required_repair_tool
                        runtime["required_repair_issues"] = [
                            str(item) for item in completion_issues if str(item).strip()
                        ]
                    convo.messages.append({
                        "role": "system",
                        "content": (
                            "Delivery rejected by automatic quality gates. Continue working autonomously with tools. "
                            "Before finishing you must: " + "; ".join(completion_issues) + "."
                            + repair_nudge
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
            effective_required_tool = (
                required_next_tool or self._active_required_repair_tool(state)
            )
            if effective_required_tool and self._required_tool_succeeded(
                convo.messages, tool_calls, effective_required_tool,
            ):
                validation_probe = json.dumps({
                    "summary": "automatic post-mutation validation",
                    "artifacts": list(step.get("required_artifacts", [])),
                    "evidence": ["deterministic artifact validation"],
                    "risks": [],
                    "next_action": "",
                })
                post_mutation_issues = self._step_completion_issues(
                    execution_id, step, validation_probe,
                )
                if post_mutation_issues:
                    next_repair_tool, next_repair_nudge = self._quality_repair_directive(
                        execution_id, role_for_step, step, post_mutation_issues,
                    )
                    if next_repair_tool:
                        runtime["required_next_tool"] = next_repair_tool
                        runtime["required_repair_issues"] = [
                            str(item) for item in post_mutation_issues if str(item).strip()
                        ]
                    else:
                        runtime.pop("required_next_tool", None)
                        runtime.pop("required_repair_issues", None)
                    convo.messages.append({
                        "role": "system",
                        "content": (
                            "The required mutation succeeded and deterministic gates were "
                            "re-run immediately. Do not inspect or modify any unrelated content. "
                            "Remaining defects: " + "; ".join(post_mutation_issues) + "."
                            + next_repair_nudge
                        ),
                        "timestamp": time.time(),
                    })
                    continue
                runtime.pop("required_next_tool", None)
                runtime.pop("required_repair_issues", None)
                convo.messages.append({
                    "role": "system",
                    "content": (
                        "The required mutation succeeded and all machine-checkable delivery "
                        "gates now pass. Stop calling tools and return only the compact raw JSON "
                        "delivery contract immediately."
                    ),
                    "timestamp": time.time(),
                })
                continue

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

    @staticmethod
    def _normalized_workspace_path(path: str) -> str:
        normalized = os.path.normcase(os.path.normpath(str(path or "").strip()))
        while normalized.startswith(f".{os.sep}"):
            normalized = normalized[2:]
        return normalized

    @classmethod
    def _active_required_artifacts(cls, state) -> set[str]:
        """Return artifacts whose deletion would invalidate the active step."""
        plan = state.current_plan if isinstance(state.current_plan, dict) else {}
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        index = int(state.current_step or 0)
        if index < 0 or index >= len(steps) or not isinstance(steps[index], dict):
            return set()
        return {
            cls._normalized_workspace_path(path)
            for path in steps[index].get("required_artifacts", [])
            if str(path or "").strip()
        }

    @classmethod
    def _active_document_artifacts(cls, state) -> set[str]:
        """Return active required paths governed by the document validator."""
        plan = state.current_plan if isinstance(state.current_plan, dict) else {}
        document_paths = {
            cls._normalized_workspace_path(item.get("path"))
            for item in plan.get("artifact_validations", [])
            if (
                isinstance(item, dict)
                and str(item.get("validator") or "").lower() == "document"
                and str(item.get("path") or "").strip()
            )
        }
        return cls._active_required_artifacts(state) & document_paths

    @staticmethod
    def _active_required_repair_tool(state) -> str:
        """Read the transient tool constraint for the active plan step."""
        plan = state.current_plan if isinstance(state.current_plan, dict) else {}
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        index = int(state.current_step or 0)
        if index < 0 or index >= len(steps) or not isinstance(steps[index], dict):
            return ""
        step_id = str(steps[index].get("id", index))
        runtimes = state.variables.get("step_runtime")
        runtime = runtimes.get(step_id) if isinstance(runtimes, dict) else None
        required = (
            str(runtime.get("required_next_tool") or "")
            if isinstance(runtime, dict) else ""
        )
        if required:
            return required
        delegated = state.variables.get("delegated_step")
        retry_context = (
            str(delegated.get("retry_context") or "")
            if isinstance(delegated, dict) else ""
        )
        if not retry_context:
            return ""
        # The parent builds retry_context exclusively from the preceding
        # machine gate. A fresh child may therefore inspect first, then use
        # the exact repair that gate authorized without weakening normal
        # overwrite protection.
        return str(classify_issue_texts([retry_context]).required_tool or "")

    @staticmethod
    def _active_required_repair_issues(state) -> List[str]:
        """Return machine gate issues authorizing the active targeted repair."""
        plan = state.current_plan if isinstance(state.current_plan, dict) else {}
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        index = int(state.current_step or 0)
        if index < 0 or index >= len(steps) or not isinstance(steps[index], dict):
            return []
        step_id = str(steps[index].get("id", index))
        runtimes = state.variables.get("step_runtime")
        runtime = runtimes.get(step_id) if isinstance(runtimes, dict) else None
        issues = (
            [
                str(item) for item in runtime.get("required_repair_issues", [])
                if str(item).strip()
            ]
            if isinstance(runtime, dict) else []
        )
        if issues:
            return issues
        delegated = state.variables.get("delegated_step")
        retry_context = (
            str(delegated.get("retry_context") or "")
            if isinstance(delegated, dict) else ""
        )
        target = classify_issue_texts([retry_context]) if retry_context else None
        return [retry_context] if target and target.required_tool else []

    @staticmethod
    def _shell_requests_deletion(command: str) -> bool:
        return bool(re.search(
            r"(?:\b(?:del|erase|rm|rmdir|rd|remove-item)\b|\.unlink\s*\()",
            str(command or ""),
            flags=re.IGNORECASE,
        ))

    async def _call_tool(self, execution_id: str, capability: str, action: str, arguments: Dict[str, Any]) -> str:
        """Invoke a tool while enforcing specialist ownership and path serialization."""
        state = self.state_engine.get_execution(execution_id)
        if state.status == "cancelled":
            raise asyncio.CancelledError()
        is_mutation = capability.lower() == "filesystem" and action.lower() in {
            "write", "append", "replace_paragraph", "replace_section", "delete",
        }
        path = str(arguments.get("path") or "")
        if is_mutation and not path.strip():
            return (
                f"Error: Invalid arguments for {capability}.{action}; missing required "
                "argument(s): path. Correct the tool call and retry."
            )
        command = str(arguments.get("command") or "")
        shell_paths = (
            self._shell_mutation_paths(command)
            if capability.lower() == "shell" and action.lower() == "execute"
            else []
        )
        lock_paths = list(shell_paths)
        if capability.lower() == "shell" and action.lower() == "execute":
            from gptmoss.capabilities.shell import ShellCapability
            for target in ShellCapability._shell_mutation_targets(command):
                if target and target not in lock_paths:
                    lock_paths.append(target)
        if is_mutation and path:
            lock_paths.append(path)
        protected_artifacts = self._active_required_artifacts(state)
        protected_document_artifacts = self._active_document_artifacts(state)
        protected_targets = {
            target for target in lock_paths
            if self._normalized_workspace_path(target) in protected_artifacts
        }
        deletes_required_artifact = (
            capability.lower() == "filesystem"
            and action.lower() == "delete"
            and self._normalized_workspace_path(path) in protected_artifacts
        ) or (
            capability.lower() == "shell"
            and action.lower() == "execute"
            and self._shell_requests_deletion(command)
            and bool(protected_targets)
        )
        if deletes_required_artifact:
            denied = sorted(protected_targets or {path})
            self.telemetry.record(
                "required_artifact_deletion_blocked", execution_id, paths=denied,
            )
            return (
                "Error: Deletion blocked for active required artifact(s): "
                + ", ".join(denied)
                + ". Repair the declared artifact in place with filesystem.write or "
                "filesystem.append; a required delivery may not be removed."
            )
        role = str(state.variables.get("role_key") or "coordinator").lower()
        required_repair_tool = self._active_required_repair_tool(state)
        required_repair_issues = self._active_required_repair_issues(state)
        if (
            required_repair_tool == "filesystem__append"
            and capability.lower() == "filesystem"
            and action.lower() == "append"
            and any(
                "uncited required source" in str(issue).casefold()
                for issue in required_repair_issues
            )
        ):
            append_content = str(arguments.get("content") or "").strip()
            append_words = re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", append_content)
            contains_structure = any(
                re.match(r"^\s*(?:#{1,6}\s|\||[-*+]\s+)", line)
                for line in append_content.splitlines()
                if line.strip()
            )
            if not append_content or len(append_words) > 180 or contains_structure:
                self.telemetry.record(
                    "unsafe_source_coverage_append_blocked", execution_id,
                    words=len(append_words), contains_structure=contains_structure,
                )
                return (
                    "Error: Source-coverage append must be one concise prose paragraph of at "
                    "most 180 words, without headings, lists, or tables. Cite only the missing "
                    "sources with valid bounded locators and do not repeat existing sections."
                )
        if (
            required_repair_tool == "filesystem__replace_paragraph"
            and capability.lower() == "filesystem"
            and action.lower() == "replace_paragraph"
            and required_repair_issues
        ):
            duplicate_heading_repair = any(
                "duplicate heading" in str(issue).casefold()
                for issue in required_repair_issues
            )
            if duplicate_heading_repair:
                selector = str(arguments.get("paragraph_prefix") or "").strip()
                try:
                    occurrence = int(arguments.get("occurrence", 1))
                except (TypeError, ValueError):
                    occurrence = 0
                if (
                    not re.match(r"^#{1,6}\s+\S", selector)
                    or occurrence != 2
                    or str(arguments.get("content") or "").strip()
                ):
                    self.telemetry.record(
                        "unsafe_duplicate_heading_repair_blocked", execution_id,
                        paragraph_prefix=selector[:180], occurrence=occurrence,
                    )
                    return (
                        "Error: Duplicate-heading repair must copy one reported Markdown "
                        "heading selector, set occurrence=2, and use empty content. This "
                        "preserves the duplicate section body under the first heading."
                    )
            def repair_prefix_key(value: Any) -> str:
                decomposed = unicodedata.normalize("NFKD", str(value or ""))
                folded = "".join(
                    character for character in decomposed
                    if not unicodedata.combining(character)
                ).casefold()
                return " ".join(re.findall(r"[^\W_]+", folded, flags=re.UNICODE))

            supplied_prefix = repair_prefix_key(arguments.get("paragraph_prefix"))
            normalized_issues = repair_prefix_key(" ".join(required_repair_issues))
            reported_prefix = supplied_prefix[:100]
            prefix_is_reported = bool(
                supplied_prefix
                and (
                    supplied_prefix in normalized_issues
                    or (
                        len(supplied_prefix) > len(reported_prefix)
                        and reported_prefix in normalized_issues
                    )
                )
            )
            if not prefix_is_reported:
                self.telemetry.record(
                    "unreported_document_repair_blocked", execution_id,
                    paragraph_prefix=str(arguments.get("paragraph_prefix") or "")[:180],
                )
                return (
                    "Error: Targeted repair blocked because paragraph_prefix was not reported "
                    "by the active machine quality gate. Use one exact paragraph prefix from "
                    "the latest gate failure and change only that passage."
                )
        if (
            required_repair_tool == "filesystem__replace_section"
            and capability.lower() == "filesystem"
            and action.lower() == "replace_section"
            and required_repair_issues
        ):
            supplied_heading = " ".join(
                str(arguments.get("heading_selector") or "").casefold().split()
            )
            normalized_issues = " ".join(
                " ".join(issue.casefold().split()) for issue in required_repair_issues
            )
            if not supplied_heading or supplied_heading not in normalized_issues:
                self.telemetry.record(
                    "unreported_document_section_repair_blocked", execution_id,
                    heading_selector=str(arguments.get("heading_selector") or "")[:180],
                )
                return (
                    "Error: Section repair blocked because heading_selector was not reported "
                    "by the active machine quality gate. Copy one exact Markdown heading "
                    "selector from the latest gate failure."
                )
        normalized_path = self._normalized_workspace_path(path)
        overwrites_existing_document = (
            capability.lower() == "filesystem"
            and action.lower() == "write"
            and normalized_path in protected_document_artifacts
            and self._artifact_exists(execution_id, path)
            and required_repair_tool != "filesystem__write"
        )
        shell_mutates_document = (
            capability.lower() == "shell"
            and action.lower() == "execute"
            and any(
                self._normalized_workspace_path(target) in protected_document_artifacts
                and self._artifact_exists(execution_id, target)
                for target in shell_paths
            )
        )
        if overwrites_existing_document or shell_mutates_document:
            denied = sorted(protected_targets or ({path} if path else set(shell_paths)))
            self.telemetry.record(
                "required_document_overwrite_blocked", execution_id, paths=denied,
            )
            return (
                "Error: Global overwrite blocked for existing required document artifact(s): "
                + ", ".join(denied)
                + ". Preserve valid content with filesystem.append, filesystem.replace_paragraph, "
                "or filesystem.replace_section. "
                "A full filesystem.write is allowed only when the automatic quality gate explicitly requires it."
            )
        empties_required_artifact = (
            capability.lower() == "filesystem"
            and action.lower() == "write"
            and self._normalized_workspace_path(path) in protected_artifacts
            and not str(arguments.get("content") or "").strip()
        )
        if empties_required_artifact:
            return (
                f"Error: Empty overwrite blocked for active required artifact '{path}'. "
                "Write one complete bounded section instead."
            )
        if shell_paths:
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
        if lock_paths:
            workspace = self._delivery_workspace(execution_id) or ""
            lock_keys = sorted({
                os.path.normcase(os.path.abspath(os.path.join(workspace, target)))
                for target in lock_paths if str(target).strip()
            })
            locks = [self._path_locks.setdefault(key, asyncio.Lock()) for key in lock_keys]
            async with AsyncExitStack() as stack:
                for lock in locks:
                    await stack.enter_async_context(lock)
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
            accepts_extra_arguments = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in sig.parameters.values()
            )
            unexpected_arguments = sorted(
                name for name in kwargs
                if name not in sig.parameters and not accepts_extra_arguments
            )
            if unexpected_arguments:
                accepted_arguments = [
                    name for name in sig.parameters
                    if name != "context"
                ]
                accepted = ", ".join(accepted_arguments) or "none"
                return (
                    f"Error: Invalid arguments for {capability}.{action}; unexpected "
                    f"argument(s): {', '.join(unexpected_arguments)}. Accepted arguments: "
                    f"{accepted}. Correct the tool call and retry."
                )
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
