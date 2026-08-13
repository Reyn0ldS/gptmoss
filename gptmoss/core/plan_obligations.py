"""Semantic delivery obligations: required work kinds, not step quotas.

A large assignment may need fifty specialist steps over a long run. The
runtime never rejects a plan for having too many steps. It only rejects a
plan that omits a work kind required to reach a truthful deliverable.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List


INDEPENDENT_ROLES = {"qa", "debugger", "coordinator"}

SOURCE_INVENTORY = "source_inventory"
DOCUMENT_RENDER = "document_render"
IMPLEMENTATION = "implementation"
INDEPENDENT_VALIDATION = "independent_validation"
AUTONOMOUS_REPAIR = "autonomous_repair"
FINAL_AUDIT = "final_audit"

_SOURCE_MARKERS = (
    "inventory", "inventor", "local corpus", "source evidence", "attachment",
    "corpus", "pièce jointe", "piece jointe", "preuve",
)
_IMPLEMENT_MARKERS = ("implement", "developer", "dévelop", "develop")
_RENDER_MARKERS = (
    "write", "writer", "rédact", "redact", "render", "dossier", "deliverable",
)
_REPAIR_MARKERS = ("repair", "debug", "corriger", "rerun", "root-cause")
_AUDIT_MARKERS = ("audit", "traceability", "auditor", "traceabilit")


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _blob(step: Dict[str, Any]) -> str:
    return " ".join((
        str(step.get("operation") or ""),
        str(step.get("role") or ""),
        str(step.get("specialist") or ""),
        str(step.get("description") or ""),
        " ".join(_strings(step.get("acceptance_criteria"))),
    )).casefold()


def _role(step: Dict[str, Any]) -> str:
    return str(step.get("role") or "").strip().lower()


def _has_marker(step: Dict[str, Any], markers: Iterable[str]) -> bool:
    text = _blob(step)
    return any(marker in text for marker in markers)


def matches_source_inventory(step: Dict[str, Any]) -> bool:
    operation = str(step.get("operation") or "").strip().lower()
    if operation in {"inventory", "extract"}:
        return True
    return _has_marker(step, _SOURCE_MARKERS)


def matches_implementation(step: Dict[str, Any]) -> bool:
    if _role(step) == "developer":
        return True
    return _has_marker(step, _IMPLEMENT_MARKERS)


def matches_document_render(step: Dict[str, Any]) -> bool:
    if _role(step) == "writer":
        return True
    return _has_marker(step, _RENDER_MARKERS)


def matches_independent(step: Dict[str, Any]) -> bool:
    return _role(step) in INDEPENDENT_ROLES


def matches_repair(step: Dict[str, Any]) -> bool:
    if _role(step) == "debugger":
        return True
    return _has_marker(step, _REPAIR_MARKERS)


def matches_audit(step: Dict[str, Any]) -> bool:
    if _role(step) == "coordinator":
        return True
    return _has_marker(step, _AUDIT_MARKERS)


def _producers(steps: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [step for step in steps if not matches_independent(step)]


def _validators(steps: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [step for step in steps if matches_independent(step)]


MATCHERS: Dict[str, Callable[[List[Dict[str, Any]]], bool]] = {
    SOURCE_INVENTORY: lambda steps: any(matches_source_inventory(step) for step in steps),
    DOCUMENT_RENDER: lambda steps: any(matches_document_render(step) for step in steps),
    IMPLEMENTATION: lambda steps: any(matches_implementation(step) for step in steps),
    INDEPENDENT_VALIDATION: lambda steps: bool(_producers(steps)) and bool(_validators(steps)),
    AUTONOMOUS_REPAIR: lambda steps: any(matches_repair(step) for step in steps),
    FINAL_AUDIT: lambda steps: any(matches_audit(step) for step in steps),
}

REASONS = {
    SOURCE_INVENTORY: (
        "Attached local sources must be inventoried before conclusions or drafting."
    ),
    DOCUMENT_RENDER: (
        "A source-grounded writing assignment needs a dedicated professional render step."
    ),
    IMPLEMENTATION: (
        "Software delivery needs a concrete implementation owner."
    ),
    INDEPENDENT_VALIDATION: (
        "The path to the deliverable needs an owner distinct from the producer."
    ),
    AUTONOMOUS_REPAIR: (
        "High-risk software work needs an autonomous repair step after independent checks."
    ),
    FINAL_AUDIT: (
        "A team plan needs a final evidence auditor that cannot claim success without proof."
    ),
}


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _count(profile: Any, *keys: str) -> int:
    if not isinstance(profile, dict):
        return 0
    return sum(max(0, int(profile.get(key) or 0)) for key in keys)


def _domains(analysis: Any) -> set[str]:
    if not isinstance(analysis, dict):
        return set()
    return {str(item) for item in (analysis.get("domains") or []) if item}


def collect_plan_obligations(
    *,
    task: str = "",
    planning_mode: str = "auto",
    analysis: Dict[str, Any] | None = None,
    workload_profile: Dict[str, Any] | None = None,
    corpus_auto_workflow: bool = False,
) -> List[Dict[str, Any]]:
    """Return the semantic gates required for this assignment.

    Step count is intentionally unbounded. A 24-hour programme may add as
    many extra specialists as the real work needs after these gates are met.
    """
    # Imported lazily to avoid planners/__init__ <-> delivery cycles.
    from gptmoss.planners.complexity import normalize_planning_mode
    from gptmoss.planners.fallbacks import _document_deliverable_task

    mode = normalize_planning_mode(planning_mode)
    if mode == "direct":
        return []

    analysis = analysis if isinstance(analysis, dict) else {}
    domains = _domains(analysis)
    level = str(analysis.get("level") or "low")
    high = level in {"high", "very_high"}
    software = "software-engineering" in domains
    document_task = _document_deliverable_task(task)
    has_sources = _count(
        workload_profile,
        "attachment_count", "document_count", "image_count",
    ) > 0
    source_workflow = document_task or (has_sources and _bool(corpus_auto_workflow))

    selected: List[str] = []
    if source_workflow:
        selected.extend((SOURCE_INVENTORY, DOCUMENT_RENDER))
    if software:
        selected.append(IMPLEMENTATION)
        if mode == "full_team" or (mode == "auto" and high):
            selected.append(AUTONOMOUS_REPAIR)
    if software or source_workflow or high or mode in {"short_team", "full_team"}:
        selected.extend((INDEPENDENT_VALIDATION, FINAL_AUDIT))

    seen: set[str] = set()
    obligations: List[Dict[str, Any]] = []
    for identifier in selected:
        if identifier in seen:
            continue
        seen.add(identifier)
        obligations.append({
            "id": identifier,
            "required": True,
            "reason": REASONS[identifier],
        })
    return obligations


def matching_steps(
    steps: Iterable[Dict[str, Any]], obligation_id: str
) -> List[Dict[str, Any]]:
    """Return the steps that can satisfy one obligation id."""
    detectors = {
        SOURCE_INVENTORY: matches_source_inventory,
        DOCUMENT_RENDER: matches_document_render,
        IMPLEMENTATION: matches_implementation,
        INDEPENDENT_VALIDATION: matches_independent,
        AUTONOMOUS_REPAIR: matches_repair,
        FINAL_AUDIT: matches_audit,
    }
    detector = detectors.get(obligation_id)
    if detector is None:
        return []
    matched = [step for step in steps if isinstance(step, dict) and detector(step)]
    if obligation_id == INDEPENDENT_VALIDATION:
        return matched if _producers(steps) else []
    return matched


def unsatisfied_obligations(
    steps: Iterable[Dict[str, Any]],
    obligations: Iterable[Dict[str, Any]],
) -> List[str]:
    materialized = [step for step in steps if isinstance(step, dict)]
    missing: List[str] = []
    for item in obligations:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("id") or "").strip()
        if not identifier or not item.get("required", True):
            continue
        checker = MATCHERS.get(identifier)
        if checker is None or not checker(materialized):
            missing.append(identifier)
    return missing


def validate_plan_obligations(
    steps: Iterable[Dict[str, Any]],
    obligations: Iterable[Dict[str, Any]],
) -> None:
    missing = unsatisfied_obligations(steps, obligations)
    if missing:
        raise ValueError(
            "Plan is missing required delivery obligations: " + ", ".join(missing)
        )


def attach_plan_obligations(
    plan: Dict[str, Any],
    *,
    task: str = "",
    planning_mode: str | None = None,
    analysis: Dict[str, Any] | None = None,
    workload_profile: Dict[str, Any] | None = None,
    corpus_auto_workflow: bool = False,
    validate: bool = True,
) -> List[Dict[str, Any]]:
    """Store the obligation snapshot on the plan and optionally enforce it."""
    obligations = collect_plan_obligations(
        task=task,
        planning_mode=planning_mode or str(plan.get("planning_mode") or "auto"),
        analysis=analysis if analysis is not None else plan.get("analysis"),
        workload_profile=(
            workload_profile if workload_profile is not None
            else plan.get("workload_profile")
        ),
        corpus_auto_workflow=corpus_auto_workflow,
    )
    if validate:
        validate_plan_obligations(plan.get("steps") or [], obligations)
    plan["plan_obligations"] = obligations
    return obligations
