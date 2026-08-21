"""Artifact existence, validation and progress fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List

from gptmoss.core.artifact_validation import validate_artifact
from gptmoss.core.delivery import commands_equivalent
from gptmoss.core.execution_plan import (
    canonical_step_role,
    infer_step_role,
    requirement_validation_commands,
)


class ExecutionProgressMixin:
    def _vision_is_available(self, state) -> bool:
        """Vision is usable only while the current model still accepts image parts."""
        if not getattr(self.llm_provider, "supports_vision", False):
            return False
        rejected_for = getattr(self.llm_provider, "vision_rejected_for_model", None)
        current = getattr(self.llm_provider, "default_model", None)
        # A per-execution note is stale after the user changes model/vision settings.
        if rejected_for is not None and rejected_for == current:
            return False
        return True

    def _vision_was_rejected(self, state) -> bool:
        rejected_for = getattr(self.llm_provider, "vision_rejected_for_model", None)
        current = getattr(self.llm_provider, "default_model", None)
        if rejected_for is not None and rejected_for == current:
            return True
        return bool(state.variables.get("vision_rejection") and rejected_for is not None)

    @staticmethod
    def _freeze_existing_record_ids(path: str, constraints: Dict[str, Any]) -> None:
        """Persist record IDs already present before a semantic repair begins."""
        policy = constraints.get("record_section_policy")
        if not isinstance(policy, dict) or not policy.get("preserve_existing_record_ids"):
            return
        if policy.get("required_record_ids"):
            return
        heading_pattern = str(policy.get("heading_pattern") or "").strip()
        if not heading_pattern:
            return
        try:
            record_pattern = re.compile(heading_pattern, flags=re.IGNORECASE)
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeError, re.error):
            return
        identifiers: List[str] = []
        seen = set()
        for line in text.splitlines():
            heading = re.match(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$", line)
            if not heading:
                continue
            for match in record_pattern.finditer(heading.group(1)):
                identifier = match.group(0).strip()
                folded = identifier.casefold()
                if identifier and folded not in seen:
                    seen.add(folded)
                    identifiers.append(identifier)
        if identifiers:
            policy["required_record_ids"] = identifiers
            policy["minimum_records"] = max(
                int(policy.get("minimum_records") or 0), len(identifiers),
            )

    def _reopen_invalid_completed_steps(
        self, execution_id: str, state, steps: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Reopen persisted work invalidated by stronger deterministic gates.

        Profile upgrades are intentionally applied on resume. Completed steps
        must not bypass those new gates: reopen the invalid producer and every
        transitive consumer while preserving their durable files for repair.
        """
        profile_retry_prefix = (
            "A deterministic profile upgrade invalidated this persisted artifact. "
        )
        upstream_retry_prefix = (
            "A completed upstream artifact was reopened by stronger deterministic "
            "quality gates. "
        )
        # A pause can leave a reopened step pending. Recompute its repair brief
        # after every profile upgrade so removed or narrowed gates cannot keep
        # sending a replacement child after obsolete defects.
        for step in steps:
            retry_context = str(step.get("retry_context") or "")
            failed_attempt_retry = "Current machine gate failures:" in retry_context
            refreshable_retry = retry_context.startswith(
                profile_retry_prefix
            ) or retry_context.startswith(upstream_retry_prefix) or failed_attempt_retry
            if step.get("status") != "pending" or not refreshable_retry:
                continue
            current_issues = self._step_artifact_validation_issues(execution_id, step)
            if current_issues:
                if failed_attempt_retry:
                    step["retry_context"] = (
                        "A resumed specialist retry was refreshed from the current durable "
                        "artifact. Ignore obsolete defects and repair only these current "
                        "machine gate failures:\n" + "; ".join(current_issues)
                    )[:12_000]
                else:
                    step["retry_context"] = (
                        profile_retry_prefix
                        + "Preserve valid content and repair these machine-observed defects:\n"
                        + "; ".join(current_issues)
                    )[:12_000]
                runtime = state.variables.get("step_runtime")
                runtime = runtime.get(str(step.get("id"))) if isinstance(runtime, dict) else None
                if isinstance(runtime, dict):
                    runtime.pop("required_next_tool", None)
                    runtime.pop("required_repair_issues", None)
            elif retry_context.startswith(profile_retry_prefix) or failed_attempt_retry:
                step.pop("retry_context", None)

        invalid: Dict[str, List[str]] = {}
        for step in steps:
            if step.get("status") != "completed":
                continue
            issues = self._step_artifact_validation_issues(execution_id, step)
            if issues:
                invalid[str(step.get("id"))] = issues
        if not invalid:
            return []

        affected = set(invalid)
        changed = True
        while changed:
            changed = False
            for step in steps:
                identifier = str(step.get("id"))
                dependencies = {str(item) for item in step.get("dependencies", [])}
                if identifier not in affected and dependencies & affected:
                    affected.add(identifier)
                    changed = True

        reopened: List[Dict[str, Any]] = []
        stored_steps = state.results.get("steps")
        for step in steps:
            identifier = str(step.get("id"))
            if identifier not in affected:
                continue
            own_issues = invalid.get(identifier)
            step["status"] = "pending"
            step.pop("assigned_execution_id", None)
            step.pop("delivery", None)
            step.pop("result", None)
            step.pop("error", None)
            step.pop("validation_passed", None)
            if own_issues:
                step["retry_context"] = (
                    profile_retry_prefix
                    + "Preserve valid content and repair these machine-observed defects:\n"
                    + "; ".join(own_issues)
                )[:12_000]
            else:
                step["retry_context"] = (
                    "A completed upstream artifact was reopened by stronger deterministic "
                    "quality gates. Reuse its corrected result and refresh this dependent "
                    "artifact without trusting stale conclusions."
                )
            if isinstance(stored_steps, dict):
                stored_steps.pop(identifier, None)
            reopened.append(step)
        state.current_step = sum(
            1 for step in steps if step.get("status") == "completed"
        )
        return reopened

    def _artifact_quality_defects(
        self, execution_id: str, step: Dict[str, Any]
    ) -> tuple:
        """Return machine defects whose monotonic reduction proves real progress."""
        state = self.state_engine.get_execution(execution_id)
        specifications = {
            str(item.get("path") or "").replace("\\", "/"): item
            for item in (state.current_plan or {}).get("artifact_validations", [])
            if isinstance(item, dict) and item.get("path")
        }
        filesystem = self.get_capability("filesystem")
        if not filesystem or not hasattr(filesystem, "_resolve_path"):
            return ()
        defects = []
        for raw_path in step.get("required_artifacts", []):
            path = str(raw_path).replace("\\", "/")
            specification = specifications.get(path)
            if not specification or not self._artifact_exists(execution_id, path):
                continue
            constraints = dict(specification.get("constraints") or {})
            try:
                report = validate_artifact(
                    filesystem._resolve_path(path, execution_id),
                    validator=specification.get("validator"),
                    constraints=constraints,
                )
            except (OSError, PermissionError, TypeError, ValueError):
                continue
            metrics = report.get("metrics") or {}
            minimums = constraints.get("minimums") or {}
            if not isinstance(minimums, dict):
                minimums = {}

            def deficit(name: str, actual_name: str | None = None) -> int:
                required = max(0, int(minimums.get(name) or 0))
                actual = max(0, int(metrics.get(actual_name or name) or 0))
                return max(0, required - actual)

            defect_vector = (
                max(0, int(metrics.get("empty_required_sections") or 0)),
                max(0, int(metrics.get("invalid_local_references") or 0)),
                max(0, int(metrics.get("uncited_required_sources") or 0)),
                max(
                    0,
                    int(metrics.get("duplicate_paragraphs") or 0)
                    - int(constraints.get("max_duplicate_paragraphs") or 0),
                ),
                max(
                    0,
                    int(metrics.get("duplicate_list_items") or 0)
                    - int(constraints.get("max_duplicate_list_items") or 0),
                ),
                max(
                    0,
                    int(metrics.get("duplicate_headings") or 0)
                    - int(constraints.get("max_duplicate_headings") or 0),
                ),
                max(0, int(metrics.get("heading_number_restarts") or 0)),
                max(0, int(metrics.get("invalid_record_sections") or 0)),
                max(0, int(metrics.get("missing_record_section_ids") or 0)),
                max(0, int(metrics.get("unsupported_claim_paragraphs") or 0)),
                max(0, int(metrics.get("placeholder_markers") or 0)),
                (
                    max(0, int(metrics.get("external_links") or 0))
                    if constraints.get("forbid_external_links") else 0
                ),
                max(
                    0,
                    int(metrics.get("required_headings_total") or 0)
                    - int(metrics.get("required_headings_covered") or 0),
                ),
                max(
                    0,
                    int(metrics.get("requirement_ids_total") or 0)
                    - int(metrics.get("requirement_ids_covered") or 0),
                ),
                max(
                    0,
                    int(metrics.get("traceability_ids_total") or 0)
                    - int(metrics.get("traceability_ids_covered") or 0),
                ),
                max(
                    0,
                    int(metrics.get("required_sources_total") or 0)
                    - int(metrics.get("required_sources_cited") or 0),
                ),
                deficit("words"),
                deficit("local_references"),
                deficit("cited_sources"),
                deficit("headings"),
                deficit("valid_diagrams"),
                (
                    max(0, int(metrics.get("invalid_diagrams") or 0))
                    if constraints.get("reject_invalid_diagrams") else 0
                ),
            )
            defects.append((path, defect_vector))
        return tuple(sorted(defects))

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
                self._freeze_existing_record_ids(resolved, constraints)
                if specification:
                    specification["constraints"] = constraints
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
        explicit_source_gate = (
            str(step.get("operation") or "").lower() in {"inventory", "extract"}
            or "source_inventory" in step.get("satisfies_obligations", [])
            or "source_coverage" in step.get("required_evidence", [])
        )
        if not explicit_source_gate and not (
            any(marker in assignment for marker in inventory_markers)
            and exhaustive_assignment
        ):
            return []
        document_ids: set[str] = set()
        image_ids: set[str] = set()
        for artifact_id in attached:
            try:
                metadata = self.artifact_store.get(artifact_id)
            except (FileNotFoundError, KeyError, OSError, ValueError):
                continue
            if metadata.get("content_type") in self.artifact_store.IMAGE_TYPES:
                image_ids.add(artifact_id)
            else:
                document_ids.add(artifact_id)
        covered: Dict[str, set[int]] = {
            artifact_id: set() for artifact_id in document_ids
        }
        history = list(state.variables.get("tool_call_history", []))
        parent_id = state.variables.get("parent_execution_id")
        plan_step_id = state.variables.get("plan_step_id")
        project_id = state.variables.get("project_id")
        # A manual pause or process restart creates a fresh specialist while
        # retaining durable workspace edits. Reuse only successful local read
        # evidence from an earlier specialist assigned to the exact same
        # parent step, project and attachment set. Artifact IDs bind the proof
        # to the same immutable uploaded content; unrelated siblings cannot
        # satisfy this execution's coverage gate.
        if parent_id is not None and plan_step_id is not None:
            for sibling in self.state_engine.executions.values():
                if sibling.execution_id == execution_id:
                    continue
                sibling_variables = sibling.variables
                if (
                    sibling_variables.get("parent_execution_id") != parent_id
                    or sibling_variables.get("plan_step_id") != plan_step_id
                    or sibling_variables.get("project_id") != project_id
                    or {
                        str(item) for item in sibling_variables.get("attachment_ids", [])
                        if item
                    } != attached
                ):
                    continue
                history.extend(sibling_variables.get("tool_call_history", []))
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
        for artifact_id in sorted(document_ids):
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
        visualized = {
            str(item) for item in state.variables.get(
                "visualized_artifact_ids", []
            ) if item
        }
        # When vision is unavailable, the declared capability-gap workflow owns
        # the limitation and requires human scope approval at the parent level.
        # Requiring a visual tool here would create an impossible child loop and
        # could falsely imply that image content had been interpreted.
        vision_available = self._vision_is_available(state)
        missing_images = sorted(image_ids - visualized) if vision_available else []
        if missing_images:
            names = []
            for artifact_id in missing_images[:20]:
                try:
                    names.append(str(self.artifact_store.get(artifact_id)["filename"]))
                except (FileNotFoundError, KeyError, OSError, ValueError):
                    names.append(artifact_id)
            suffix = (
                f", and {len(missing_images) - 20} more"
                if len(missing_images) > 20 else ""
            )
            issues.append(
                "analyze every attached image through documents.read_image/read_images; "
                f"missing: {', '.join(names)}{suffix}"
            )
        return issues

    def _inherits_complete_document_coverage(
        self, execution_id: str, step: Dict[str, Any]
    ) -> bool:
        """Identify a fresh retry whose exact prior assignment proved full coverage."""
        state = self.state_engine.get_execution(execution_id)
        variables = state.variables
        parent_id = variables.get("parent_execution_id")
        plan_step_id = variables.get("plan_step_id")
        project_id = variables.get("project_id")
        attachments = {
            str(item) for item in variables.get("attachment_ids", []) if item
        }
        if parent_id is None or plan_step_id is None or not attachments:
            return False
        if any(
            item.get("capability") == "documents" and item.get("action") == "read"
            for item in variables.get("tool_call_history", [])
        ):
            return False
        prior_assignment = any(
            sibling.execution_id != execution_id
            and sibling.variables.get("parent_execution_id") == parent_id
            and sibling.variables.get("plan_step_id") == plan_step_id
            and sibling.variables.get("project_id") == project_id
            and {
                str(item) for item in sibling.variables.get("attachment_ids", []) if item
            } == attachments
            and any(
                item.get("capability") == "documents" and item.get("action") == "read"
                for item in sibling.variables.get("tool_call_history", [])
            )
            for sibling in self.state_engine.executions.values()
        )
        return prior_assignment and not self._document_coverage_issues(execution_id, step)

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
        vision_available = self._vision_is_available(state)
        if image_attachments and not vision_available:
            gaps.append({
                "capability": "vision",
                "required_for": "Interpret attached image content",
                "inputs": image_attachments,
                "available": False,
                "resolution": (
                    "The provider rejected image parts. Set vision_mode to disabled "
                    "or switch to a vision model before retrying image analysis."
                    if self._vision_was_rejected(state) else
                    "Configure a vision-capable provider, or restrict execution to "
                    "documented adapters, configuration, routines, and validators."
                ),
            })
        return gaps

    def _hash_workspace_file(self, full_path: str, filename: str) -> str:
        """Hash file contents, normalizing text newlines the same way as before."""
        digest = hashlib.sha256()
        text_extensions = {
            ".py", ".pyi", ".md", ".txt", ".json", ".jsonl",
            ".yaml", ".yml", ".toml", ".ini", ".cfg", ".html",
            ".css", ".js", ".ts", ".tsx", ".jsx", ".xml",
            ".csv", ".sh", ".ps1", ".bat", ".cmd",
        }
        if os.path.splitext(filename)[1].lower() in text_extensions:
            try:
                with open(full_path, "r", encoding="utf-8", newline=None) as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk.encode("utf-8"))
                return digest.hexdigest()
            except UnicodeDecodeError:
                digest = hashlib.sha256()
        with open(full_path, "rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _cached_workspace_digest(self, full_path: str, filename: str) -> str | None:
        """Reuse a content digest when mtime and size have not changed."""
        try:
            stat = os.stat(full_path)
        except OSError:
            return None
        cache = getattr(self, "_progress_file_digest_cache", None)
        if cache is None:
            cache = {}
            self._progress_file_digest_cache = cache
        key = os.path.normcase(os.path.abspath(full_path))
        cached = cache.get(key)
        if (
            cached
            and cached[0] == stat.st_mtime_ns
            and cached[1] == stat.st_size
        ):
            return cached[2]
        digest = self._hash_workspace_file(full_path, filename)
        cache[key] = (stat.st_mtime_ns, stat.st_size, digest)
        return digest

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
                        digest = self._cached_workspace_digest(full_path, filename)
                        if digest is None:
                            continue
                        files.append((relative, digest))
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
        source_coverage = set()
        for item in history:
            if item.get("capability") != "documents":
                continue
            action = str(item.get("action") or "")
            try:
                payload = json.loads(str(item.get("result") or ""))
            except (TypeError, ValueError):
                continue
            if action == "read":
                artifact_id = str(payload.get("artifact_id") or "")
                for block in payload.get("blocks") or []:
                    if artifact_id and isinstance(block, dict):
                        try:
                            source_coverage.add(
                                ("block", artifact_id, int(block["order"]))
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
            elif action == "read_chunk" and payload.get("id"):
                source_coverage.add(("chunk", str(payload["id"])))
            elif action == "inventory":
                source_coverage.add(("inventory-page", int(payload.get("offset") or 0)))
        source_coverage.update(
            ("image", str(item))
            for item in execution_state.variables.get(
                "visualized_artifact_ids", []
            ) if item
        )
        return (
            tuple(files),
            tuple(successful_commands),
            tuple(sorted(self._missing_artifacts(execution_id, step))),
            latest_failure_count,
            tuple(sorted(source_coverage)),
            self._artifact_quality_defects(execution_id, step),
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
        previous_sources = set(previous[4]) if len(previous) > 4 else set()
        current_sources = set(current[4]) if len(current) > 4 else set()
        if current_sources - previous_sources:
            return True, "new_source_coverage"
        previous_quality = dict(previous[5]) if len(previous) > 5 else {}
        current_quality = dict(current[5]) if len(current) > 5 else {}
        if previous_quality and previous_quality.keys() == current_quality.keys():
            before = tuple(
                value
                for path in sorted(previous_quality)
                for value in previous_quality[path]
            )
            after = tuple(
                value
                for path in sorted(current_quality)
                for value in current_quality[path]
            )
            if (
                len(before) == len(after)
                and all(
                    current_value <= previous_value
                    for previous_value, current_value in zip(before, after)
                )
                and any(
                    current_value < previous_value
                    for previous_value, current_value in zip(before, after)
                )
            ):
                return True, "document_quality_improved"

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
