"""Route delivery failures to the obligation owner, not a fixed role."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from gptmoss.core.plan_obligations import (
    AUTONOMOUS_REPAIR,
    DOCUMENT_RENDER,
    FINAL_AUDIT,
    IMPLEMENTATION,
    INDEPENDENT_VALIDATION,
    SOURCE_INVENTORY,
    matching_steps,
)


INVENTORY_MARKERS = (
    "read every normalized block",
    "analyze every attached image",
    "incomplete source coverage",
    "corpus policy lacks machine evidence",
    "no documents.read",
    "no documents.read_image",
    "corpus_policy_evidence",
    "corpus_evidence_graph",
)
RENDER_TARGETED_MARKERS = (
    "duplicate paragraph",
    "duplicate heading",
    "lack a local reference",
)
RENDER_SECTION_MARKERS = (
    "record section",
    "invalid diagram",
)
RENDER_REWRITE_MARKERS = (
    "invalid local reference",
    "citation-like pattern",
    "external link",
    "placeholder marker",
    "reasoning tag",
    "heading numbering restart",
)
RENDER_APPEND_MARKERS = (
    "words=",
    "empty required section",
    "uncited required source",
    "cited_sources=",
    "local_references=",
)
SOFTWARE_MARKERS = (
    "static integration",
    "syntax_imports",
    "independent commands",
    "launch smoke",
    "cli smoke",
    "durable filesystem mutation",
    "independent_machine_evidence",
    "real_launch_smoke",
    "syntax_imports_signatures",
)


@dataclass(frozen=True)
class FeedbackTarget:
    obligation: Optional[str]
    role: Optional[str]
    required_tool: Optional[str]
    reason: str
    also_reopen: tuple[str, ...] = ()


def _blob(items: Iterable[Any]) -> str:
    return "\n".join(str(item or "") for item in items).casefold()


def classify_issue_texts(texts: Iterable[Any]) -> FeedbackTarget:
    """Map existing gate fragments to one delivery obligation."""
    blob = _blob(texts)
    if any(marker in blob for marker in INVENTORY_MARKERS):
        return FeedbackTarget(
            SOURCE_INVENTORY, "architect", None, "source coverage",
            ("writer", "coordinator"),
        )
    if any(marker in blob for marker in RENDER_TARGETED_MARKERS):
        return FeedbackTarget(
            DOCUMENT_RENDER, "writer", "filesystem__replace_paragraph",
            "paragraph repair", ("coordinator",),
        )
    if any(marker in blob for marker in RENDER_SECTION_MARKERS):
        return FeedbackTarget(
            DOCUMENT_RENDER, "writer", "filesystem__replace_section",
            "record section repair", ("coordinator",),
        )
    if any(marker in blob for marker in RENDER_APPEND_MARKERS):
        return FeedbackTarget(
            DOCUMENT_RENDER, "writer", "filesystem__append",
            "document append", ("coordinator",),
        )
    if any(marker in blob for marker in RENDER_REWRITE_MARKERS):
        return FeedbackTarget(
            DOCUMENT_RENDER, "writer", "filesystem__write",
            "document rewrite", ("coordinator",),
        )
    if any(marker in blob for marker in SOFTWARE_MARKERS):
        return FeedbackTarget(
            AUTONOMOUS_REPAIR, "debugger", None, "software assurance",
            ("coordinator",),
        )
    return FeedbackTarget(None, None, None, "unclassified")


def classify_assurance_report(report: Any) -> FeedbackTarget:
    """Prefer named delivery checks, then fall back to failure text."""
    if not isinstance(report, dict):
        return FeedbackTarget(None, None, None, "unclassified")
    failed_checks = [
        str(item.get("name") or "")
        for item in (report.get("checks") or [])
        if isinstance(item, dict) and not item.get("passed", True)
    ]
    texts: List[Any] = list(report.get("failures") or [])
    texts.extend(failed_checks)
    if "corpus_policy_evidence" in failed_checks or "corpus_evidence_graph" in failed_checks:
        return FeedbackTarget(
            SOURCE_INVENTORY, "architect", None, "corpus evidence",
            ("writer", "coordinator"),
        )
    if "plan_obligations" in failed_checks:
        missing: List[str] = []
        for item in report.get("checks") or []:
            if isinstance(item, dict) and item.get("name") == "plan_obligations":
                missing = [str(value) for value in (item.get("missing") or [])]
        if SOURCE_INVENTORY in missing:
            return FeedbackTarget(
                SOURCE_INVENTORY, "architect", None, "missing inventory",
                ("writer", "coordinator"),
            )
        if DOCUMENT_RENDER in missing:
            return FeedbackTarget(
                DOCUMENT_RENDER, "writer", None, "missing render",
                ("coordinator",),
            )
        if IMPLEMENTATION in missing:
            return FeedbackTarget(
                IMPLEMENTATION, "developer", None, "missing implementation",
                ("debugger", "coordinator"),
            )
        if INDEPENDENT_VALIDATION in missing or AUTONOMOUS_REPAIR in missing:
            return FeedbackTarget(
                AUTONOMOUS_REPAIR, "debugger", None, "missing validation or repair",
                ("coordinator",),
            )
        if FINAL_AUDIT in missing:
            return FeedbackTarget(
                FINAL_AUDIT, "coordinator", None, "missing audit", (),
            )
    if "artifact_structure_and_constraints" in failed_checks:
        targeted = classify_issue_texts(texts)
        if targeted.obligation:
            return targeted
    if any(name in failed_checks for name in (
        "syntax_imports_signatures", "independent_machine_evidence", "real_launch_smoke",
    )):
        return FeedbackTarget(
            AUTONOMOUS_REPAIR, "debugger", None, "software assurance",
            ("coordinator",),
        )
    return classify_issue_texts(texts)


def select_reopen_step(plan: Any, target: FeedbackTarget) -> Optional[Dict[str, Any]]:
    """Return the last plan step that owns the classified obligation."""
    steps = [
        step for step in (plan or {}).get("steps", [])
        if isinstance(step, dict)
    ]
    if target.role == "debugger":
        matched = [
            step for step in steps
            if str(step.get("role") or "").strip().lower() == "debugger"
        ]
        if matched:
            return matched[-1]
    if target.obligation:
        matched = matching_steps(steps, target.obligation)
        if matched:
            return matched[-1]
    if target.role:
        matched = [
            step for step in steps
            if str(step.get("role") or "").strip().lower() == target.role
        ]
        if matched:
            return matched[-1]
    for step in reversed(steps):
        if str(step.get("role") or "").strip().lower() == "debugger":
            return step
    return None


def steps_to_reopen(
    plan: Any, target: FeedbackTarget, primary: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Primary owner plus the roles that must re-check after the repair."""
    steps = [
        step for step in (plan or {}).get("steps", [])
        if isinstance(step, dict)
    ]
    selected: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add(step: Optional[Dict[str, Any]]) -> None:
        if not isinstance(step, dict):
            return
        key = str(step.get("id"))
        if key in seen:
            return
        seen.add(key)
        selected.append(step)

    add(primary)
    roles = {str(role).strip().lower() for role in target.also_reopen}
    for step in steps:
        if str(step.get("role") or "").strip().lower() in roles:
            add(step)
    return selected


def disjoint_owned_paths(left: Sequence[Any], right: Sequence[Any]) -> bool:
    """Return whether two ownership claims can run in the same wave."""
    first = {str(item).replace("\\", "/").strip() for item in left if str(item).strip()}
    second = {str(item).replace("\\", "/").strip() for item in right if str(item).strip()}
    if not first or not second:
        return False
    return first.isdisjoint(second)
