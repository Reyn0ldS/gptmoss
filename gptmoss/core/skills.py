"""Portable Markdown skills for GPTMOSS agents."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    allowed_capabilities: List[str]
    source_path: str
    digest: str

class SkillRegistry:
    TOOL_MAP = {"shell_command": "shell", "apply_patch": "filesystem"}
    FORBIDDEN_INSTRUCTIONS = (
        "ignore previous instructions", "ignore system instructions", "disable safety",
        "disable safeguards", "bypass approval", "reveal api key", "exfiltrate",
        "modify config.json", "modify the policy",
    )

    def __init__(self, roots: Iterable[str] = ()):
        self.skills: Dict[str, Skill] = {}
        for root in roots:
            self.discover(root)

    @staticmethod
    def _frontmatter(text: str) -> tuple[Dict[str, object], str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text
        try:
            end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        except StopIteration:
            return {}, text
        fields: Dict[str, object] = {}
        for line in lines[1:end]:
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            value = value.strip().strip("\"'")
            if value.startswith("[") and value.endswith("]"):
                fields[key.strip()] = [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]
            else:
                fields[key.strip()] = value
        return fields, chr(10).join(lines[end + 1:]).strip()

    def discover(self, root: str) -> List[Skill]:
        root_path = Path(root)
        if not root_path.exists():
            return []
        discovered = []
        for path in root_path.rglob("SKILL.md"):
            text = path.read_text(encoding="utf-8")
            fields, instructions = self._frontmatter(text)
            name = str(fields.get("name") or path.parent.name).strip().lower()
            if not re.fullmatch("[a-z0-9][a-z0-9_-]*", name):
                continue
            raw_capabilities = fields.get(
                "allowed_capabilities",
                fields.get("allowed-tools", []),
            )
            if isinstance(raw_capabilities, str):
                raw_capabilities = [
                    item
                    for item in re.split(r"[\s,]+", raw_capabilities)
                    if item
                ]
            skill = Skill(name, str(fields.get("description") or ""), instructions,
                          [str(item).lower() for item in raw_capabilities],
                          str(path), hashlib.sha256(text.encode("utf-8")).hexdigest())
            self.skills[name] = skill
            discovered.append(skill)
        return discovered

    def compatibility_report(self, path: str) -> Dict[str, object]:
        text = Path(path).read_text(encoding="utf-8")
        all_tools = [*self.TOOL_MAP, "image_gen"]
        requested = sorted(tool for tool in all_tools if re.search("(?<![A-Za-z0-9_])" + re.escape(tool) + "(?![A-Za-z0-9_])", text))
        return {"mapped": {tool: self.TOOL_MAP[tool] for tool in requested if tool in self.TOOL_MAP},
                "unsupported": [tool for tool in requested if tool not in self.TOOL_MAP]}

    def validate(self, *, name: str, description: str, instructions: str,
                 allowed_capabilities: Iterable[str],
                 registered_capabilities: Iterable[str]) -> Dict[str, object]:
        """Statically validate an untrusted generated skill before registration."""
        errors: List[str] = []
        warnings: List[str] = []
        normalized_name = str(name or "").strip().lower()
        normalized_instructions = str(instructions or "").strip()
        requested = {str(item).strip().lower() for item in allowed_capabilities if str(item).strip()}
        registered = {str(item).strip().lower() for item in registered_capabilities if str(item).strip()}
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", normalized_name):
            errors.append("Skill name must contain 2-80 lowercase safe characters.")
        if not str(description or "").strip():
            errors.append("Skill description is required.")
        if len(normalized_instructions) < 120:
            errors.append("Skill instructions are too short to define a reliable workflow.")
        if len(normalized_instructions) > 20_000:
            errors.append("Skill instructions exceed the safe size limit.")
        unknown = sorted(requested - registered)
        if unknown:
            errors.append("Unknown or unavailable capabilities: " + ", ".join(unknown))
        lowered = normalized_instructions.lower()
        forbidden = [marker for marker in self.FORBIDDEN_INSTRUCTIONS if marker in lowered]
        if forbidden:
            errors.append("Unsafe instruction patterns: " + ", ".join(forbidden))
        if not any(marker in lowered for marker in ("verify", "validat", "test", "check", "contrô")):
            warnings.append("The workflow does not explicitly describe verification.")
        return {"valid": not errors, "errors": errors, "warnings": warnings,
                "normalized_capabilities": sorted(requested)}

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {token for token in re.findall(r"[^\W_]+(?:[-_][^\W_]+)*", text.lower(), flags=re.UNICODE)
                if len(token) > 1}

    def coverage(self, task: str) -> Dict[str, object]:
        """Explain how well the loaded registry covers a specialization."""
        task_lower = str(task or "").lower()
        task_tokens = self._tokens(task_lower)
        ranked = []
        for skill in self.skills.values():
            haystack = f"{skill.name} {skill.description} {skill.instructions[:500]}".lower()
            overlap = task_tokens & self._tokens(haystack)
            score = len(overlap) + len(task_tokens & self._tokens(skill.description)) * 2
            if skill.name.replace("-", " ") in task_lower:
                score += 5
            ranked.append({"name": skill.name, "score": score, "overlap": sorted(overlap)})
        ranked.sort(key=lambda item: (-int(item["score"]), str(item["name"])))
        return {"best_score": int(ranked[0]["score"]) if ranked else 0, "matches": ranked[:5]}

    def select(self, task: str, requested: Optional[List[str]] = None,
               preferred: Optional[List[str]] = None, limit: int = 4) -> List[Skill]:
        """Select explicit baseline skills plus expertise relevant to this exact subtask."""
        selected: List[Skill] = []
        seen = set()
        for name in [*(requested or []), *(preferred or [])]:
            skill = self.skills.get(str(name).lower())
            if skill and skill.name not in seen:
                selected.append(skill)
                seen.add(skill.name)

        # Per-execution requested skills are an explicit selection contract,
        # not seed words for an unrelated fourth skill. Autonomous specialist
        # skills are appended to this same requested list before selection.
        # Keep every explicit item even when it exceeds the automatic ranking limit.
        if requested:
            return selected

        task_lower = task.lower()
        task_tokens = self._tokens(task_lower)
        scored = []
        for skill in self.skills.values():
            if skill.name in seen:
                continue
            haystack = f"{skill.name} {skill.description} {skill.instructions[:500]}".lower()
            overlap = task_tokens & self._tokens(haystack)
            score = len(overlap)
            if skill.name.replace("-", " ") in task_lower:
                score += 5
            description_tokens = self._tokens(skill.description)
            score += len(task_tokens & description_tokens) * 2
            # A single incidental token (for example a local filename such as
            # vision.pptx) is not enough to activate unrelated expertise.
            # Explicitly requested and preferred skills remain unconditional.
            if score >= 2:
                scored.append((score, skill))
        scored.sort(key=lambda item: (-item[0], item[1].name))
        for _, skill in scored:
            if len(selected) >= limit:
                break
            selected.append(skill)
        return selected[:limit]
