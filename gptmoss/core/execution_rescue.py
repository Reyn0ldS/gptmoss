"""Artifact rescue when a specialist stalls before creating files."""

from __future__ import annotations

import ast
import json
import os
import re
from typing import Any, Dict, List

from gptmoss.core.event_bus import Event


class ExecutionRescueMixin:
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
                async def on_text_delta(delta: str) -> None:
                    await self.event_bus.publish(Event(
                        type="LLMDelta",
                        payload={"execution_id": execution_id, "delta": delta, "path": path},
                    ))

                response = await self._completion_with_recovery(
                    execution_id,
                    messages=rescue_messages,
                    temperature=0.1,
                    on_text_delta=on_text_delta,
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
