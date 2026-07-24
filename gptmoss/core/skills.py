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
            skill = Skill(name, str(fields.get("description") or ""), instructions,
                          [str(item).lower() for item in fields.get("allowed_capabilities", [])],
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

    def select(self, task: str, requested: Optional[List[str]] = None, limit: int = 3) -> List[Skill]:
        if requested:
            return [self.skills[name.lower()] for name in requested if name.lower() in self.skills]
        task_tokens = set(re.findall("[A-Za-z0-9_-]+", task.lower()))
        scored = []
        for skill in self.skills.values():
            haystack = f"{skill.name} {skill.description} {skill.instructions[:500]}".lower()
            score = len(task_tokens & set(re.findall("[A-Za-z0-9_-]+", haystack)))
            if score:
                scored.append((score, skill))
        return [skill for _, skill in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]]
