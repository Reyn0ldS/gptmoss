"""Structured, deterministic policy for local source processing.

The user task remains immutable.  Corpus guarantees travel beside it as a
machine-readable contract that planning, execution and delivery assurance can
all inspect without relying on prompt wording.
"""

from __future__ import annotations

from typing import Any, Mapping


CORPUS_POLICY_REQUIREMENTS = (
    "inventory_all_sources",
    "preserve_relative_source_paths",
    "keep_sources_read_only",
    "use_local_evidence_only",
    "classify_requirements_decisions_risks_and_contradictions",
    "search_each_decision_topic_across_the_corpus",
    "analyze_relevant_images",
    "record_unreadable_or_unsupported_sources",
    "produce_source_to_section_coverage",
    "separate_supported_claims_inferences_and_gaps",
)


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "full"}
    return bool(value)


def build_corpus_policy(
    *,
    enabled: Any = False,
    source_kind: str = "attachments",
    professional_delivery: Any = False,
    workload_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the stable corpus contract without copying source content."""
    active = _bool(enabled)
    profile = workload_profile if isinstance(workload_profile, Mapping) else {}
    documents = max(0, int(profile.get("document_count") or 0))
    images = max(0, int(profile.get("image_count") or 0))
    return {
        "schema_version": 1,
        "enabled": active,
        "mode": "full" if active else "off",
        "source_kind": "corpus" if source_kind == "corpus" else "attachments",
        "professional_delivery": active and _bool(professional_delivery),
        "internet_evidence": "prohibited" if active else "not_constrained",
        "source_mutation": "prohibited" if active else "not_constrained",
        "coverage_scope": "all_attached_sources" if active else "requested_sources",
        "requirements": list(CORPUS_POLICY_REQUIREMENTS) if active else [],
        "expected_evidence": (
            [
                "source_inventory",
                "block_and_image_coverage",
                "bounded_local_citations",
                "contradiction_and_gap_register",
                "source_to_section_matrix",
                "unreadable_source_report",
            ]
            if active else []
        ),
        "document_count": documents,
        "image_count": images,
    }


def normalize_corpus_policy(
    value: Any,
    *,
    enabled: Any = None,
    source_kind: str | None = None,
    professional_delivery: Any = None,
    workload_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize persisted or API policy data and restore mandatory controls."""
    current = dict(value) if isinstance(value, Mapping) else {}
    active = _bool(current.get("enabled") if enabled is None else enabled)
    kind = source_kind or str(current.get("source_kind") or "attachments")
    professional = (
        current.get("professional_delivery", False)
        if professional_delivery is None else professional_delivery
    )
    normalized = build_corpus_policy(
        enabled=active,
        source_kind=kind,
        professional_delivery=professional,
        workload_profile=(
            workload_profile if isinstance(workload_profile, Mapping) else current
        ),
    )
    # Unknown extension fields are preserved for forward-compatible projects,
    # while the security and coverage fields above remain authoritative.
    extensions = {
        key: item for key, item in current.items()
        if key not in normalized and not str(key).startswith("_")
    }
    return {**extensions, **normalized}
