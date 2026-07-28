import asyncio
import ast
import hashlib
import json
import time
import logging
import inspect
import os
import re
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
from gptmoss.core.evolution import AgentProfileRegistry, AutonomousSkillLifecycle
from gptmoss.core.delivery import (
    build_delivery_contract,
    evaluate_delivery,
    path_is_owned,
)

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


logger = logging.getLogger("gptmoss.execution")


class ProviderUnavailableError(RuntimeError):
    """A transient provider outage that must suspend, not destroy, an execution."""

    def __init__(self, message: str, original_error: Exception):
        super().__init__(message)
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
        self.max_step_iterations = max(1, min(int(max_step_iterations), 100))
        self.max_step_retries = max(0, min(int(max_step_retries), 5))
        self.continue_while_progress = bool(continue_while_progress)
        self.agent_profile_registry = agent_profile_registry
        self.skill_lifecycle = skill_lifecycle
        self.autonomous_specialization = bool(autonomous_specialization)
        self._capabilities: Dict[str, Any] = {}  # capability_name -> instance
        self._execution_locks: Dict[str, asyncio.Lock] = {}
        self._provider_resume_tasks: Dict[str, asyncio.Task] = {}
        self._path_locks: Dict[str, asyncio.Lock] = {}

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
    def _is_transient_llm_error(error: Exception) -> bool:
        text = (error.__class__.__name__ + " " + str(error)).lower()
        permanent_markers = ("authentication", "permissiondenied", "invalid api key", "401", "403")
        transient_markers = (
            "connection", "timeout", "timed out", "ratelimit", "rate limit", "429",
            "internalserver", "server error", "502", "503", "504", "temporar", "unavailable",
        )
        return not any(marker in text for marker in permanent_markers) and any(
            marker in text for marker in transient_markers
        )

    async def _completion_with_recovery(self, execution_id: str, **kwargs) -> Dict[str, Any]:
        """Keep durable task state through temporary local/provider outages."""
        consecutive_errors = 0
        while True:
            try:
                return await self.llm_provider.completion(**kwargs)
            except Exception as error:
                if not self._is_transient_llm_error(error):
                    raise
                if consecutive_errors >= min(4, self.max_step_iterations):
                    raise ProviderUnavailableError(
                        "LLM provider is temporarily unavailable; execution state was preserved.",
                        error,
                    ) from error
                consecutive_errors += 1
                delay_seconds = min(30, 2 ** min(consecutive_errors - 1, 5))
                await self.event_bus.publish(Event(
                    type="LLMRetryScheduled",
                    payload={
                        "execution_id": execution_id,
                        "attempt": consecutive_errors,
                        "delay_seconds": delay_seconds,
                        "error_type": error.__class__.__name__,
                    },
                ))
                await asyncio.sleep(delay_seconds)

    def _schedule_provider_resume(self, execution_id: str, delay_seconds: int = 30) -> None:
        existing = self._provider_resume_tasks.get(execution_id)
        if existing and not existing.done():
            return

        async def resume_later():
            cancelled = False
            try:
                await asyncio.sleep(max(1, min(int(delay_seconds), 300)))
                state = self.state_engine.get_execution(execution_id)
                if state.status != "waiting_provider":
                    return
                state.status = "running"
                state.variables["provider_resume_attempts"] = (
                    int(state.variables.get("provider_resume_attempts", 0)) + 1
                )
                await self.event_bus.publish(Event(
                    type="ExecutionProviderRetry",
                    payload={
                        "execution_id": execution_id,
                        "attempt": state.variables["provider_resume_attempts"],
                    },
                ))
                await self.execute_task(
                    execution_id, str(state.variables.get("task") or "")
                )
            except asyncio.CancelledError:
                cancelled = True
                raise
            finally:
                self._provider_resume_tasks.pop(execution_id, None)
                state = self.state_engine.get_execution(execution_id)
                if not cancelled and state.status == "waiting_provider":
                    attempts = int(state.variables.get("provider_resume_attempts", 0))
                    self._schedule_provider_resume(
                        execution_id, delay_seconds=min(300, 30 * max(1, attempts))
                    )

        self._provider_resume_tasks[execution_id] = asyncio.create_task(resume_later())

    def resume_waiting_provider_executions(self) -> None:
        """Restore automatic retries after a process restart."""
        for execution_id, state in self.state_engine.executions.items():
            if state.status == "waiting_provider":
                self._schedule_provider_resume(execution_id, delay_seconds=1)

    async def stop_provider_resume_tasks(self) -> None:
        tasks = list(self._provider_resume_tasks.values())
        self._provider_resume_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def get_capabilities_schemas(self, is_sub_agent: bool = False, allowed_capabilities: Optional[set[str]] = None) -> List[Dict[str, Any]]:
        """Generate JSON schemas for all registered capabilities."""
        schemas = []
        for name, inst in self._capabilities.items():
            if allowed_capabilities is not None and name.lower() not in allowed_capabilities:
                continue
            if is_sub_agent and name.lower() in ("agent", "devteam"):
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

    @staticmethod
    def _allowed_capabilities(skills) -> Optional[set[str]]:
        if not skills:
            return None
        return set().union(*(set(skill.allowed_capabilities) for skill in skills))

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
                        digest = hashlib.sha256()
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

        history = self.state_engine.get_execution(execution_id).variables.get("tool_call_history", [])
        successful_commands = sorted({
            str(item.get("arguments", {}).get("command") or "").strip()
            for item in history
            if item.get("capability") == "shell" and item.get("action") == "execute"
            and "EXIT_CODE: 0" in str(item.get("result") or "")
        })
        latest_failure_count = None
        for item in reversed(history):
            if item.get("capability") != "shell" or item.get("action") != "execute":
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
        suspicious = ("numpy", "torch", "cv2", "trimesh", "pillow", "scipy")
        return [name for name in suspicious
                if os.path.isfile(os.path.join(root, name, "__init__.py"))]

    def _integration_contract_issues(self, execution_id: str) -> List[str]:
        """Detect package-layout defects that can create duplicate Python class identities."""
        filesystem = self.get_capability("filesystem")
        if not filesystem or not hasattr(filesystem, "_get_workspace_for_execution"):
            return []
        try:
            root = filesystem._get_workspace_for_execution(execution_id)
        except (OSError, PermissionError, ValueError):
            return []
        package_root = os.path.join(root, "src", "avatar3d")
        if not os.path.isdir(package_root):
            return []

        issues = []
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
                if re.search(r"(?:from|import)\s+src\.avatar3d\b", content):
                    invalid_imports.append(os.path.relpath(full_path, root).replace(os.sep, "/"))
        if invalid_imports:
            issues.append(
                "replace src.avatar3d imports with the single canonical avatar3d package identity in: "
                + ", ".join(sorted(invalid_imports)[:20])
            )

        pytest_path = os.path.join(root, "pytest.ini")
        if os.path.isfile(pytest_path):
            try:
                with open(pytest_path, "r", encoding="utf-8") as config_file:
                    pytest_config = config_file.read()
                if re.search(r"(?m)^\s*python_paths\s*=", pytest_config):
                    issues.append("replace unsupported pytest.ini option python_paths with pythonpath")
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
            if not re.search(r"(?:from|import)\s+avatar3d", lower):
                issues.append("tests do not import the actual avatar3d implementation")
            if re.search(r"(?:from|import)\s+src\.avatar3d", lower):
                issues.append("tests import src.avatar3d instead of the canonical avatar3d package")
            if any(marker in lower for marker in ("mockmesh", "magicmock", "unittest.mock", "# mocking", "np.random")):
                issues.append("tests contain mocks, replicated implementation, or random data")
            if "def test_" not in lower:
                issues.append("test file contains no pytest test function")
        if any(name in normalized_path for name in ("face.py", "body.py", "garment.py", "geometry.py", "fitting.py")):
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
        quality_contract = bool(
            step.get("specialist") or step.get("required_artifacts")
            or step.get("acceptance_criteria") or step.get("verification_commands")
        )
        role_key = canonical_step_role(step.get("role")) or infer_step_role(step.get("description", ""))
        if quality_contract and role_key != "coordinator" and not self._is_structured_delivery(response):
            issues.append("return the required structured JSON delivery contract")

        missing = [path for path in step.get("required_artifacts", [])
                   if not self._artifact_exists(execution_id, path)]
        if missing:
            issues.append("create non-empty required artifacts: " + ", ".join(missing))

        if role_key in {"qa", "debugger", "coordinator"}:
            fake_packages = self._fake_dependency_packages(execution_id)
            if fake_packages:
                issues.append(
                    "remove local packages impersonating third-party dependencies and use real code contracts: "
                    + ", ".join(fake_packages)
                )
            issues.extend(self._integration_contract_issues(execution_id))

        if step.get("verification_commands"):
            history = self.state_engine.get_execution(execution_id).variables.get("tool_call_history", [])
            missing_commands = []
            for command in step["verification_commands"]:
                matched = any(
                    item.get("capability") == "shell" and item.get("action") == "execute"
                    and str(item.get("arguments", {}).get("command") or "").strip() == command.strip()
                    and "EXIT_CODE: 0" in str(item.get("result") or "")
                    for item in history
                )
                if not matched:
                    missing_commands.append(command)
            if missing_commands:
                issues.append("run declared verification command(s) successfully: " + ", ".join(missing_commands))
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
        return json.dumps({
            "summary": "GPTMOSS verified the specialist's converged workspace delivery after tool execution.",
            "artifacts": artifacts, "evidence": evidence[-8:],
            "risks": ["The specialist did not return a clean final contract; GPTMOSS synthesized it from machine evidence."],
            "next_action": "Validate this delivery in its dependent integration and acceptance steps.",
        }, ensure_ascii=False)

    def _delivery_histories(self, execution_id: str) -> List[Dict[str, Any]]:
        histories: List[Dict[str, Any]] = []
        queue = [execution_id]
        visited = set()
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            current_state = self.state_engine.get_execution(current)
            histories.extend(current_state.variables.get("tool_call_history", []))
            queue.extend(
                child_id for child_id, child in self.state_engine.executions.items()
                if child.variables.get("parent_execution_id") == current
            )
        return histories

    def _delivery_workspace(self, execution_id: str) -> Optional[str]:
        filesystem = self.get_capability("filesystem")
        if not filesystem or not hasattr(filesystem, "_get_workspace_for_execution"):
            return None
        try:
            return filesystem._get_workspace_for_execution(execution_id)
        except (OSError, PermissionError, ValueError):
            return None

    def _independent_delivery_report(self, execution_id: str, steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        state = self.state_engine.get_execution(execution_id)
        contract = state.variables.get("delivery_contract")
        workspace = self._delivery_workspace(execution_id)
        if isinstance(contract, dict) and not contract.get("software_delivery") and not workspace:
            return {
                "schema_version": 1,
                "contract_sha256": contract.get("contract_sha256"),
                "passed": True,
                "checks": [{"name": "direct_task_contract", "passed": True}],
                "failures": [],
            }
        if isinstance(contract, dict) and not workspace:
            has_artifacts = any(step.get("required_artifacts") for step in steps)
            has_commands = bool(contract.get("verification_commands") or contract.get("launch_commands"))
            if not has_artifacts and not has_commands:
                return {
                    "schema_version": 1,
                    "contract_sha256": contract.get("contract_sha256"),
                    "passed": True,
                    "checks": [{"name": "scheduler_only_contract", "passed": True}],
                    "failures": [],
                }
        if not isinstance(contract, dict) or not workspace:
            return {
                "schema_version": 1,
                "passed": False,
                "checks": [],
                "failures": ["delivery contract or workspace is unavailable"],
            }
        return evaluate_delivery(
            workspace, contract, steps, self._delivery_histories(execution_id)
        )

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
                state.status = "waiting_provider"
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
                state.status = "failed"
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

        # 1. Initialize states if new
        if state.status == "pending":
            state.status = "running"
            
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
            
            # Initial convo message containing parent context if sub-agent
            if parent_task and parent_task != task:
                convo.messages.append({
                    "role": "user",
                    "content": f"Main Project Task: {parent_task}\nYour Specific Subtask: {task}",
                    "timestamp": time.time()
                })
            else:
                convo.messages.append({"role": "user", "content": f"Task: {task}", "timestamp": time.time()})

        # 2. Plan generation (if not already planned)
        if not state.current_plan:
            is_sub_agent = state.variables.get("parent_execution_id") is not None
            schemas = self.get_capabilities_schemas(is_sub_agent=is_sub_agent, allowed_capabilities=allowed_capabilities)
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
            plan_result = await self.planner.plan(
                task, context, schemas,
                parent_execution_id=state.variables.get("parent_execution_id"),
                delegated_step=state.variables.get("delegated_step"),
            )
            plan_result = normalize_plan(plan_result)
            self.telemetry.record("plan_generated", execution_id, duration_ms=round((time.perf_counter() - planning_started) * 1000, 2), steps=len(plan_result.get("steps", [])))
            state.current_plan = plan_result
            state.variables["delivery_contract"] = build_delivery_contract(
                state.current_plan, task
            )
            state.current_step = 0
            await self.event_bus.publish(Event(
                type="PlanGenerated",
                payload={"execution_id": execution_id, "plan": plan_result}
            ))

        state.current_plan = normalize_plan(state.current_plan)
        if not isinstance(state.variables.get("delivery_contract"), dict):
            state.variables["delivery_contract"] = build_delivery_contract(
                state.current_plan, task
            )
        delivery_contract = state.variables["delivery_contract"]
        scope_changes = delivery_contract.get("scope_changes", [])
        approved_contract = state.variables.get("approved_scope_contract_sha256")
        if (not state.variables.get("parent_execution_id") and scope_changes
                and approved_contract != delivery_contract.get("contract_sha256")):
            state.status = "paused"
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
                        sub_exec.status = "pending"
                    sub_exec.variables["role_name"] = role_name
                    sub_exec.variables["role_key"] = role_key
                    sub_exec.variables["generic_role_name"] = generic_role_name
                    sub_exec.variables["parent_execution_id"] = execution_id
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
                                sub_state.status = "cancelled"
                                await self.event_bus.publish(Event(
                                    type="ExecutionCancelled",
                                    payload={"execution_id": sub_id}
                                ))
                            break
                        elif parent_state.status == "paused":
                            if sub_state.status == "running":
                                sub_state.status = "paused"
                                await self.event_bus.publish(Event(
                                    type="ExecutionPaused",
                                    payload={"execution_id": sub_id}
                                ))
                            continue
                        
                        # Resume child if parent is resumed
                        if parent_state.status == "running" and sub_state.status == "paused" and not sub_state.variables.get("pending_approval"):
                            sub_state.status = "running"
                            asyncio.create_task(self.execute_task(sub_id, sub_exec.variables["task"]))

                        if sub_state.status == "waiting_provider":
                            parent_state.status = "waiting_provider"
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
                        raise RuntimeError(f"Sub-agent {role_name} stopped with status: {sub_state.status}")
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
                        child.status = "cancelled"
                step["status"] = "pending" if state.status == "paused" else "cancelled"
                raise
            except ProviderUnavailableError:
                step["status"] = "pending"
                raise
            except Exception as e:
                if not sub_id:
                    self._record_specialization_outcome(execution_id, step, False, str(e))
                retry_count = int(step.get("retry_count", 0))
                if sub_id and retry_count < self.max_step_retries and state.status not in ("cancelled", "paused"):
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
                    step["retry_context"] = (
                        f"Previous attempt {retry_count + 1} failed: {e}\n"
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

        # Loop until all steps are completed or execution finishes/pauses/cancels
        while state.status in ("running", "pending"):
            if state.status == "pending":
                state.status = "running"
                
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
                        if repair_step is not None and repair_round < self.max_step_retries:
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
                        state.status = "failed"
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
                    state.status = "completed"
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
                    state.status = "paused"
                    for t in running_tasks.values():
                        t.cancel()
                    return

                if provider_suspended:
                    state.status = "waiting_provider"
                    for t in running_tasks.values():
                        t.cancel()
                    await asyncio.gather(*running_tasks.values(), return_exceptions=True)
                    self.state_engine.save_to_disk()
                    self._schedule_provider_resume(execution_id)
                    return
                    
                if step_failure:
                    state.status = "failed"
                    state.results["error"] = str(step_failure)
                    self.telemetry.record("execution_failed", execution_id, error=str(step_failure))
                    for t in running_tasks.values():
                        t.cancel()
                    await asyncio.gather(*running_tasks.values(), return_exceptions=True)
                    for child in self.state_engine.executions.values():
                        if (child.variables.get("parent_execution_id") == execution_id
                                and child.status in ("pending", "running", "paused")):
                            child.status = "cancelled"
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
                state.status = "failed"
                await self.event_bus.publish(Event(
                    type="ExecutionFailed",
                    payload={"execution_id": execution_id, "error": "Cyclical step dependencies detected in plan."}
                ))
                break

    async def _execute_step_loop(self, execution_id: str, step: Dict[str, Any]) -> str:
        """
        Executes a step by running a ReAct-style dialog loop with the LLM.
        """
        state = self.state_engine.get_execution(execution_id)
        convo = self.state_engine.get_conversation(execution_id)
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
            role_for_step = canonical_step_role(step.get("role")) or infer_step_role(step_desc)
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

        iteration = 0
        stagnant_iterations = 0
        previous_progress = self._progress_signature(execution_id, step)

        while True:
            current_progress = self._progress_signature(execution_id, step)
            if iteration:
                improved, improvement_kind = self._quality_improved(
                    execution_id, previous_progress, current_progress
                )
                if improved:
                    stagnant_iterations = 0
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
                            "remaining_stagnation_budget": self.max_step_iterations,
                        },
                    ))
                else:
                    stagnant_iterations += 1
            previous_progress = current_progress
            if self.continue_while_progress:
                if stagnant_iterations >= self.max_step_iterations:
                    break
            elif iteration >= self.max_step_iterations:
                break

            if state.status == "cancelled":
                raise asyncio.CancelledError()
            if state.status == "failed":
                raise RuntimeError("Execution state was marked failed.")
            if state.status == "paused" and not state.variables.get("pending_approval", {}).get("decision"):
                return f"Execution suspended with status: {state.status}."
            iteration += 1

            # Check if there is a pending approval we just resumed
            pending_app = state.variables.get("pending_approval")
            if pending_app:
                # We have a pending tool call that is now approved or rejected!
                # Remove from pending list
                state.variables.pop("pending_approval")
                tool_call_id = pending_app["tool_call_id"]
                
                # Check if user decision is approved
                decision = pending_app.get("decision", "reject")
                completed_tool_calls = state.variables.setdefault("completed_tool_calls", {})
                if tool_call_id in completed_tool_calls:
                    result_str = completed_tool_calls[tool_call_id]
                elif decision == "allow":
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
            schemas = self.get_capabilities_schemas(
                is_sub_agent=is_sub_agent or delegated_plan,
                allowed_capabilities=allowed_capabilities,
            )

            # Compile context
            context = await self.context_engine.compile_context(
                execution_id=execution_id,
                conversation_id=execution_id,
                agent_id="default_agent",
                capabilities_schemas=schemas
            )
            if self.artifact_store and state.variables.get("attachment_ids"):
                context["attachments"] = self.artifact_store.context_items(
                    state.variables["attachment_ids"], getattr(self.llm_provider, "supports_vision", False)
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
            base_prompt += (
                f"\n\nRuntime environment: operating_system={environment.get('operating_system')}, "
                f"shell={environment.get('shell')}, path_separator={environment.get('path_separator')}. "
                "Use commands compatible with this exact environment; do not use Unix utilities on Windows."
            )
            if skills:
                base_prompt += "\n\nActive skills:\n" + "\n\n".join(
                    f"[{skill.name}]\n{skill.instructions}" for skill in skills
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
                    "Cover edge cases, invariants, input validation, deterministic repeatability, and boundary conditions."
                )
            elif role_key == "debugger":
                specialized_prompt = (
                    "You are the Specialized Debugger & Bug Fixer.\n"
                    "Your role is to analyze test failure logs, run commands to inspect state, and modify files to fix code syntax or logical errors."
                )
            elif role_key == "writer":
                specialized_prompt = (
                    "You are the Specialized Technical Writer.\n"
                    "Your role is to write detailed project documentation, README.md files, and help guides for users."
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
                f"\nOwned paths: {json.dumps(step.get('owned_paths', []), ensure_ascii=False)}."
                "\nAct autonomously inside the project workspace: inspect existing prerequisite artifacts, implement the assignment, "
                "run relevant checks, diagnose failures, fix root causes, and rerun checks before finishing. Do not merely describe "
                "what should be done. Do not redo validated dependency work. Never claim an artifact or successful test that you did not create or execute."
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
                        "convenient API. If existing tests contradict the source contract, correct the tests or the source "
                        "according to the validated specification, then run the complete declared command.\n" + source_contracts
                    )
            llm_messages.append({"role": "system", "content": role_prompt})
            for attachment in context.get("attachments", []):
                if attachment.get("text") is not None:
                    llm_messages.append({"role": "user", "content": f"Attached file {attachment['filename']}:\n{attachment['text']}"})
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
            if not tool_calls:
                response_text = llm_response.get("content") or ""
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

            # Process tool calls
            for tool_call in tool_calls:
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
                    state.status = "paused"
                    state.variables["pending_approval"] = {
                        "tool_call_id": tool_id,
                        "capability": cap_name,
                        "action": act_name,
                        "arguments": args,
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
                f"{self.max_step_iterations} consecutive stagnant iterations."
                if self.continue_while_progress else
                f"Step '{step_desc}' did not satisfy its delivery gates within "
                f"{self.max_step_iterations} iterations."
            )
        )

    async def _call_tool(self, execution_id: str, capability: str, action: str, arguments: Dict[str, Any]) -> str:
        """Invoke a tool while enforcing specialist ownership and path serialization."""
        is_mutation = capability.lower() == "filesystem" and action.lower() in {"write", "delete"}
        path = str(arguments.get("path") or "")
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
            res = bound_method(**kwargs)
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
        state = self.state_engine.get_execution(execution_id)
        pending = state.variables.get("pending_scope_approval")
        if state.status != "paused" or not isinstance(pending, dict):
            raise ValueError(f"Execution {execution_id} has no pending scope approval.")
        state.variables.setdefault("scope_decisions", []).append({
            "contract_sha256": pending.get("contract_sha256"),
            "decision": decision,
            "reason": reason or "",
            "decided_at": time.time(),
        })
        state.variables.pop("pending_scope_approval", None)
        if decision != "allow":
            state.status = "failed"
            state.results["error"] = "Proposed scope reduction was rejected by the user."
            self.state_engine.save_to_disk()
            await self.event_bus.publish(Event(
                type="ExecutionFailed",
                payload={"execution_id": execution_id, "error": state.results["error"]},
            ))
            return
        state.variables["approved_scope_contract_sha256"] = pending.get("contract_sha256")
        state.status = "running"
        self.state_engine.save_to_disk()
        await self.event_bus.publish(Event(
            type="ScopeApproved",
            payload={"execution_id": execution_id, "reason": reason or ""},
        ))
        asyncio.create_task(self.execute_task(
            execution_id, str(state.variables.get("task") or "")
        ))

    async def resume_with_decision(self, execution_id: str, decision: str, reason: Optional[str] = None):
        """
        Resumes a paused execution with the user decision ('allow' or 'reject').
        """
        state = self.state_engine.get_execution(execution_id)
        if state.status != "paused":
            raise ValueError(f"Execution {execution_id} is not paused.")

        pending_app = state.variables.get("pending_approval")
        if not pending_app:
            raise ValueError(f"No pending approval found for execution {execution_id}.")

        pending_app["decision"] = decision
        pending_app["reason"] = reason or ""

        # Set status back to running and resume execution
        state.status = "running"
        await self.event_bus.publish(Event(
            type="ExecutionResumed",
            payload={"execution_id": execution_id, "decision": decision}
        ))

        # Re-start execution process (it will load the step again, find the pending approval, and process it)
        task = state.variables.get("task") or self.state_engine.get_conversation(execution_id).messages[0]["content"]
        if task.startswith("Task: "):
            task = task[6:]
            
        # Run execution loop asynchronously in the background so it doesn't block the caller
        asyncio.create_task(self.execute_task(execution_id, task))
