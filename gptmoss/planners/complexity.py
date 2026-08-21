"""Deterministic task-complexity hints for the adaptive planner."""

from __future__ import annotations

import re
from typing import Any, Dict
from unicodedata import combining, normalize

from gptmoss.core.domains import DEFAULT_DOMAIN_REGISTRY, ProjectDomainRegistry

PLANNING_MODES = ("auto", "direct", "short_team", "full_team")
_PLANNING_MODE_ALIASES = {
    "short": "short_team",
    "compact": "short_team",
    "equipe_courte": "short_team",
    "full": "full_team",
    "complete": "full_team",
    "equipe_complete": "full_team",
}


def normalize_planning_mode(value: Any) -> str:
    """Return a stable planning-mode token; unknown values become auto."""
    text = str(value or "auto").strip().lower().replace("-", "_").replace(" ", "_")
    text = _PLANNING_MODE_ALIASES.get(text, text)
    return text if text in PLANNING_MODES else "auto"


def task_title_from_text(task: str, limit: int = 72) -> str:
    """Build a short sidebar title from the first line of a user task."""
    text = " ".join(str(task or "").split())
    if not text:
        return "Tâche"
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def requires_software_implementation(
    task: str, analysis: Dict[str, Any] | None = None,
) -> bool:
    """Distinguish changing software from merely analysing software.

    Domain classification intentionally stays broad: a professional dossier
    about an API still belongs to software engineering. Implementation gates,
    however, require an explicit mutation verb tied to a software target.
    """
    if isinstance(analysis, dict) and "software_implementation_requested" in analysis:
        return bool(analysis["software_implementation_requested"])
    text = "".join(
        character for character in normalize("NFKD", str(task or "")).casefold()
        if not combining(character)
    )
    mutation = (
        r"(?:implement|develop|fix|repair|debug|modify|update|refactor|"
        r"add|remove|configure|deploy|migrate|integrate|build|create|"
        r"implemente|developpe|developper|coder|corrige|repare|modifie|"
        r"mets?\s+a\s+jour|refactorise|ajoute|supprime|configure|deploie|"
        r"migre|integre|cree|creer|construire)"
    )
    strong_target = (
        r"(?:software|application|api|gui|code|runtime|server|"
        r"service|module|package|endpoint|script|repository|repo|"
        r"pipeline|logiciel|serveur|fonctionnalite|"
        r"plateforme|platform)"
    )
    weak_target = (
        r"(?:test|project|projet|source|import|export|feature|function|"
        r"logic|depot|fonction|interface|workflow|programme|program|console)"
    )
    strong = bool(
        re.search(rf"\b{mutation}\b[^.\n;:]{{0,96}}\b{strong_target}\b", text)
        or re.search(rf"\b{strong_target}\b[^.\n;:]{{0,48}}\b{mutation}\b", text)
    )
    if strong:
        return True
    weak = bool(
        re.search(rf"\b{mutation}\b[^.\n;:]{{0,96}}\b{weak_target}\b", text)
        or re.search(rf"\b{weak_target}\b[^.\n;:]{{0,48}}\b{mutation}\b", text)
    )
    if not weak:
        return False
    writing = any(
        re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", text)
        for marker in (
            "redige", "redaction", "dossier", "rapport", "synthese", "livrable",
            "long-form", "write a", "produce a", "document",
        )
    )
    return not writing


def analyze_task_complexity(
    task: str, domain_registry: ProjectDomainRegistry | None = None
) -> Dict[str, Any]:
    """Return deterministic hints so the LLM cannot silently trivialize a task."""
    text = str(task or "").lower()
    domains = (domain_registry or DEFAULT_DOMAIN_REGISTRY).classify(text)
    software_implementation_requested = requires_software_implementation(
        task, {"domains": domains}
    )
    if (
        software_implementation_requested
        and "software-engineering" not in domains
    ):
        domains = [*domains, "software-engineering"]
    requested_outcomes = len(re.findall(
        r"\b(?:doit|devra|pouvoir|créer|creer|faire|importer|extrapoler|intégrer|integrer|"
        r"must|should|create|build|implement|support|import)\b", text,
    ))
    if "software-engineering" in domains:
        requested_outcomes = max(
            requested_outcomes,
            min(5, len(re.findall(r"[,;]", text)) + 1),
        )
    score = len(domains) * 2 + min(requested_outcomes, 5)
    score += 2 if len(text) > 300 else 1 if len(text) > 140 else 0
    if score >= 14:
        level, minimum = "very_high", 12
    elif score >= 9:
        level, minimum = "high", 9
    elif score >= 5:
        level, minimum = "moderate", 5
    else:
        level, minimum = "low", 1
    return {"level": level, "score": score, "domains": domains,
            "software_implementation_requested": software_implementation_requested,
            "requested_outcomes": requested_outcomes,
            "suggested_min_steps": minimum}  # hint for the LLM, never a hard floor
