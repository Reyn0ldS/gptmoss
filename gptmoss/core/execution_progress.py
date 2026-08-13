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
        missing_images = sorted(image_ids - visualized)
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
