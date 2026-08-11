"""Agent-facing, approval-oriented access to durable project memory."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from gptmoss.interfaces.capability import action, capability
from gptmoss.interfaces.memory import MemoryProvider


@capability(
    name="memory",
    description="Search validated project memory and propose new memories for human review.",
)
class MemoryCapability:
    """Never lets an agent silently validate or globalize its own memories."""

    def __init__(self, provider: MemoryProvider):
        self.provider = provider

    @staticmethod
    def _variables(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        variables = (context or {}).get("variables")
        return variables if isinstance(variables, dict) else {}

    @action(
        name="search",
        description="Search validated memories scoped to the current project.",
    )
    async def search(
        self,
        query: str,
        limit: int = 5,
        kind: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        variables = self._variables(context)
        project_id = variables.get("project_id")
        results = await self.provider.search(
            query,
            limit=max(1, min(int(limit), 20)),
            project_id=project_id,
            kind=kind.strip().lower() or None,
            include_global=False,
        )
        safe_results = [
            {
                key: item.get(key)
                for key in (
                    "id", "value", "kind", "scope", "project_id", "provenance",
                    "source_execution_id", "source_artifacts", "validated_at",
                )
            }
            for item in results
        ]
        return json.dumps({"query": query, "memories": safe_results}, ensure_ascii=False)

    @action(
        name="propose",
        description=(
            "Propose a project-scoped fact, decision, preference, constraint, or lesson. "
            "The proposal remains pending until a human validates it in the GUI."
        ),
    )
    async def propose(
        self,
        value: str,
        kind: str = "fact",
        source_artifacts: Optional[List[str]] = None,
        supersedes_id: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        text = " ".join(str(value or "").split())
        if len(text) < 8:
            return "Error: A memory proposal must contain at least 8 characters."
        normalized_kind = kind.strip().lower()
        if normalized_kind not in {"fact", "decision", "preference", "constraint", "lesson"}:
            return "Error: Unsupported memory kind."
        variables = self._variables(context)
        project_id = variables.get("project_id")
        execution_id = (context or {}).get("execution_id")
        if not project_id:
            return "Error: Project-scoped memory requires a project_id."
        memory_id = await self.provider.store(
            text,
            provenance={"source": "agent_proposal", "execution_id": execution_id},
            validated=False,
            kind=normalized_kind,
            scope="project",
            project_id=str(project_id),
            source_execution_id=str(execution_id) if execution_id else None,
            source_artifacts=[str(item) for item in (source_artifacts or []) if item],
            supersedes_id=supersedes_id.strip() or None,
        )
        return json.dumps(
            {"id": memory_id, "status": "pending_human_validation", "project_id": project_id},
            ensure_ascii=False,
        )
