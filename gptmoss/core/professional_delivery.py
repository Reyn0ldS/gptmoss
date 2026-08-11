"""Deterministic policy for source-grounded professional document deliveries."""

from __future__ import annotations

from typing import Any, Dict, Iterable


PROFILE = "professional-local"


def _source_inventory(artifact_store: Any, attachment_ids: Iterable[str]) -> Dict[str, Dict[str, int]]:
    inventory: Dict[str, Dict[str, int]] = {}
    if artifact_store is None:
        return inventory
    for artifact_id in attachment_ids:
        try:
            document = artifact_store.document(str(artifact_id))
        except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
            continue
        slides = {
            block.provenance.slide_number
            for block in document.blocks
            if block.provenance.slide_number is not None
        }
        inventory[document.filename] = (
            {"slides": max(slides)} if slides else {"blocks": len(document.blocks)}
        )
    return inventory


def apply_professional_profile(
    plan: Dict[str, Any], artifact_store: Any = None,
    attachment_ids: Iterable[str] = (),
) -> Dict[str, Any]:
    """Enforce a useful quality floor without trusting planner-generated policy."""
    if plan.get("delivery_profile") != PROFILE:
        return plan
    inventory = _source_inventory(artifact_store, attachment_ids)
    source_files = list(inventory)
    validations = plan.setdefault("artifact_validations", [])
    if not isinstance(validations, list):
        validations = []
        plan["artifact_validations"] = validations
    text_paths = []
    for step in plan.get("steps", []):
        for path in step.get("required_artifacts", []):
            normalized = str(path).replace("\\", "/")
            if normalized.lower().endswith((".md", ".txt", ".html")):
                text_paths.append(normalized)
    primary = str(plan.get("primary_artifact") or "")
    if not primary:
        primary = next(
            (path for path in text_paths if not path.startswith("analysis/")),
            text_paths[0] if text_paths else "",
        )
    existing = {
        str(item.get("path") or "").replace("\\", "/"): item
        for item in validations if isinstance(item, dict) and item.get("path")
    }
    for path in dict.fromkeys(text_paths):
        item = existing.get(path)
        if item is None:
            item = {"path": path, "validator": "document", "required": True, "constraints": {}}
            validations.append(item)
        item["validator"] = "document"
        item["required"] = True
        constraints = item.setdefault("constraints", {})
        if not isinstance(constraints, dict):
            constraints = {}
            item["constraints"] = constraints
        constraints["forbid_placeholders"] = True
        constraints["max_duplicate_paragraphs"] = 0
        constraints.setdefault("duplicate_min_words", 12)
        minimums = constraints.setdefault("minimums", {})
        if not isinstance(minimums, dict):
            minimums = {}
            constraints["minimums"] = minimums
        minimums["words"] = max(int(minimums.get("words") or 0), 600 if path == primary else 120)
        if inventory and path == primary:
            constraints["source_inventory"] = inventory
            constraints["required_source_files"] = source_files
            constraints["require_local_references"] = True
            constraints["require_bounded_references"] = True
            constraints["require_claim_references"] = True
            constraints.setdefault("claim_min_words", 24)
            minimums["cited_sources"] = max(int(minimums.get("cited_sources") or 0), len(source_files))
            minimums["local_references"] = max(int(minimums.get("local_references") or 0), len(source_files))
    plan["professional_profile"] = {
        "name": PROFILE,
        "primary_artifact": primary,
        "source_count": len(source_files),
        "quality_gate": "deterministic",
    }
    return plan
