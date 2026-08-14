"""Generic workload profiling and source-aware DAG compilation.

This module intentionally reuses GPTMOSS requirements, plan normalization,
delivery contracts and execution scheduler.  It only bridges the missing gap
between real input volume and the plan that those existing components execute.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable


SOURCE_MARKERS = (
    "corpus", "source", "attachment", "document", "evidence", "inventory",
    "fichier", "pièce jointe", "piece jointe", "preuve", "inventor",
)
SINGLE_PARTITION_USABLE = 48
PARTITION_LOAD_UNIT = 96
MAX_SOURCE_PARTITIONS = 128


def build_workload_profile(
    artifact_store: Any,
    artifact_ids: Iterable[str],
    *,
    corpus_summaries: Iterable[dict[str, Any]] = (),
    supports_vision: bool = False,
) -> dict[str, Any]:
    """Profile actual attached evidence without loading it into an LLM prompt."""
    identifiers = list(dict.fromkeys(str(item) for item in artifact_ids if item))
    formats: Counter[str] = Counter()
    documents = images = total_bytes = total_blocks = total_chunks = unreadable = 0
    for artifact_id in identifiers:
        try:
            metadata = artifact_store.get(artifact_id)
        except (OSError, ValueError, FileNotFoundError, KeyError):
            unreadable += 1
            continue
        content_type = str(metadata.get("content_type") or "application/octet-stream")
        formats[content_type] += 1
        total_bytes += max(0, int(metadata.get("size_bytes") or 0))
        if content_type in artifact_store.DOCUMENT_TYPES:
            documents += 1
            total_blocks += max(0, int(metadata.get("document_blocks") or 0))
            total_chunks += max(0, int(metadata.get("document_chunks") or 0))
        elif content_type in artifact_store.IMAGE_TYPES:
            images += 1

    summaries = [dict(item) for item in corpus_summaries if isinstance(item, dict)]
    ignored = sum(max(0, int(item.get("skipped_count") or len(item.get("skipped") or []))) for item in summaries)
    failed = sum(max(0, int(item.get("error_count") or len(item.get("errors") or []))) for item in summaries)
    usable = documents + images
    # A partition is a bounded retrieval unit, not a prompt dump. Documents
    # are costlier than image metadata and indexed chunks increase retrieval work.
    load_units = documents + math.ceil(images / 4) + math.ceil(total_chunks / 200)
    # Scale with measured load. There is no small quality cap: a day-long
    # corpus job may legitimately need dozens of shards. The ceiling is only
    # an operational bound so the compiled DAG stays persistable.
    if usable <= SINGLE_PARTITION_USABLE:
        partition_count = 1
    else:
        partition_count = max(1, min(
            MAX_SOURCE_PARTITIONS,
            math.ceil(max(1, load_units) / PARTITION_LOAD_UNIT),
        ))
    return {
        "schema_version": 1,
        "attachment_count": len(identifiers),
        "document_count": documents,
        "image_count": images,
        "total_bytes": total_bytes,
        "total_blocks": total_blocks,
        "total_chunks": total_chunks,
        "formats": dict(sorted(formats.items())),
        "unreadable_count": unreadable,
        "ignored_count": ignored,
        "failed_count": failed,
        "supports_vision": bool(supports_vision),
        "suggested_partitions": partition_count,
        "partition_strategy": "stable_round_robin",
        "requires_retrieval": documents > 0,
        "requires_image_inventory": images > 0,
    }


def planning_profile(profile: Any) -> dict[str, Any]:
    """Return the bounded, identifier-free subset safe for planning prompts."""
    if not isinstance(profile, dict):
        return {}
    keys = (
        "schema_version", "attachment_count", "document_count", "image_count",
        "total_bytes", "total_blocks", "total_chunks", "formats",
        "unreadable_count", "ignored_count", "failed_count", "supports_vision",
        "suggested_partitions", "partition_strategy", "requires_retrieval",
        "requires_image_inventory",
    )
    return {key: profile.get(key) for key in keys}


def _operation(step: dict[str, Any]) -> str:
    declared = str(step.get("operation") or "").strip().lower()
    if declared:
        return declared
    role = str(step.get("role") or "").strip().lower()
    structural_roles = {
        "developer": "implement",
        "writer": "document_render",
        "qa": "validate",
        "debugger": "repair",
        "coordinator": "audit",
    }
    if role in structural_roles:
        return structural_roles[role]
    text = " ".join((
        str(step.get("description") or ""),
        str(step.get("specialist") or ""),
        str(step.get("role") or ""),
    )).casefold()
    rules = (
        ("inventory", ("inventory", "inventor", "local corpus", "source evidence")),
        ("validate", ("validate", "validation", "review", "qa", "audit")),
        ("repair", ("repair", "debug", "corriger", "correct")),
        ("render", ("render", "docx", "diagram", "rendu")),
        ("write", ("write", "writer", "rédact", "redact", "editor")),
        ("implement", ("implement", "developer", "dévelop", "develop")),
        ("design", ("architect", "design", "conception")),
        ("consolidate", ("consolid", "synth")),
    )
    return next((name for name, markers in rules if any(marker in text for marker in markers)), "execute")


def _renumber(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping = {str(step["id"]): index for index, step in enumerate(steps)}
    result: list[dict[str, Any]] = []
    for index, original in enumerate(steps):
        step = dict(original)
        step["id"] = index
        step["dependencies"] = list(dict.fromkeys(
            mapping[str(value)] for value in original.get("dependencies", [])
            if str(value) in mapping and mapping[str(value)] != index
        ))
        step["status"] = "pending"
        result.append(step)
    return result


def compile_work_graph(
    plan: dict[str, Any],
    profile: Any,
    *,
    planning_mode: str = "auto",
) -> dict[str, Any]:
    """Annotate and, when justified by real source load, partition the DAG."""
    result = dict(plan)
    steps = [dict(step) for step in result.get("steps", []) if isinstance(step, dict)]
    safe_profile = planning_profile(profile)
    result["workload_profile"] = safe_profile
    result["planning_strategy"] = "generic-work-graph-v1"
    for step in steps:
        step["operation"] = _operation(step)

    partitions = int(safe_profile.get("suggested_partitions") or 1)
    if planning_mode == "direct" or partitions <= 1 or not steps:
        result["steps"] = _renumber(steps)
        return result

    source_step = next((
        step for step in steps
        if step.get("operation") == "inventory"
        or any(marker in str(step.get("description") or "").casefold() for marker in SOURCE_MARKERS)
    ), None)
    if source_step is None:
        result["steps"] = _renumber(steps)
        return result

    original_id = str(source_step["id"])
    shard_ids = [f"source-shard-{index + 1}" for index in range(partitions)]
    shards: list[dict[str, Any]] = []
    for index, shard_id in enumerate(shard_ids):
        shards.append({
            **source_step,
            "id": shard_id,
            "specialist": f"Source Evidence Analyst {index + 1}/{partitions}",
            "description": (
                f"Process source partition {index + 1}/{partitions}: inventory every assigned "
                "attachment, extract relevant evidence for the mapped requirements, record unreadable "
                "items and coverage, and write the bounded shard result. Do not claim whole-corpus coverage."
            ),
            "dependencies": list(source_step.get("dependencies", [])),
            "required_artifacts": [f"analysis/corpus-shards/shard-{index + 1:03d}.md"],
            "owned_paths": [f"analysis/corpus-shards/shard-{index + 1:03d}.md"],
            "acceptance_criteria": [
                "Every attachment assigned to this partition has a coverage or error state.",
                "Evidence retains local source identifiers and bounded locations.",
            ],
            "operation": "extract",
            "source_partition": {
                "index": index,
                "count": partitions,
                "strategy": "stable_round_robin",
            },
            "status": "pending",
        })

    consolidation_id = "source-consolidation"
    consolidation = {
        **source_step,
        "id": consolidation_id,
        "specialist": "Corpus Evidence Consolidation Analyst",
        "description": (
            "Consolidate all completed source partitions, deduplicate evidence, reconcile contradictions, "
            "verify aggregate source coverage and produce the original corpus-analysis artifacts."
        ),
        "dependencies": shard_ids,
        "operation": "consolidate",
        "source_partition": {"consolidates": partitions},
        "status": "pending",
    }
    rewritten: list[dict[str, Any]] = []
    for step in steps:
        if str(step["id"]) == original_id:
            rewritten.extend([*shards, consolidation])
            continue
        dependencies = [
            consolidation_id if str(value) == original_id else value
            for value in step.get("dependencies", [])
        ]
        rewritten.append({**step, "dependencies": dependencies})
    result["steps"] = _renumber(rewritten)
    if not isinstance(result.get("analysis"), dict):
        result["analysis"] = {}
    result["analysis"]["compiled_source_partitions"] = partitions
    return result


def partition_attachment_ids(
    artifact_ids: Iterable[str], partition: Any
) -> list[str]:
    """Select one stable round-robin partition without duplicating source state."""
    identifiers = list(dict.fromkeys(str(item) for item in artifact_ids if item))
    if not isinstance(partition, dict) or "index" not in partition:
        if isinstance(partition, dict) and partition.get("consolidates"):
            return []
        return identifiers
    count = max(1, int(partition.get("count") or 1))
    index = max(0, min(count - 1, int(partition.get("index") or 0)))
    return [value for position, value in enumerate(identifiers) if position % count == index]
