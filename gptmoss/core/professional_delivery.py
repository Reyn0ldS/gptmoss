"""Deterministic policy for source-grounded professional document deliveries."""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Dict, Iterable


PROFILE = "professional-local"
WORDS_PER_REQUESTED_PAGE = 250


def _requirement_text(plan: Dict[str, Any]) -> str:
    return "\n".join(
        str(item.get("statement") or "")
        for item in plan.get("requirements", [])
        if isinstance(item, dict)
    )


def _requested_page_range(text: str) -> tuple[int, int] | None:
    """Extract an explicit page range without inventing one for ordinary reports."""
    match = re.search(
        r"(?i)\b(\d{1,4})\s*(?:a|à|to|[-–—])\s*(\d{1,4})\s+pages?\b",
        text,
    )
    if match:
        lower, upper = int(match.group(1)), int(match.group(2))
        if 1 <= lower <= upper:
            return lower, upper
    match = re.search(r"(?i)\b(?:au\s+moins|minimum(?:\s+de)?|at\s+least)\s+(\d{1,4})\s+pages?\b", text)
    if match:
        lower = int(match.group(1))
        if lower >= 1:
            return lower, lower
    match = re.search(r"(?i)\b(?:environ\s+|about\s+|approximately\s+)?(\d{1,4})\s+pages?\b", text)
    if match:
        target = int(match.group(1))
        if target >= 1:
            return target, target
    if re.search(r"(?i)\b(?:une\s+quarantaine\s+de|about\s+forty)\s+pages?\b", text):
        return 40, 40
    return None


def _requested_diagram_count(text: str) -> int:
    number_words = {
        "one": 1, "un": 1, "une": 1, "two": 2, "deux": 2,
        "three": 3, "trois": 3, "four": 4, "quatre": 4,
        "five": 5, "cinq": 5,
    }
    match = re.search(
        r"(?i)\b(?:au\s+moins|minimum(?:\s+de)?|at\s+least)\s+"
        r"(\d{1,3}|one|un|une|two|deux|three|trois|four|quatre|five|cinq)\s+"
        r"(?:diagrammes?|diagrams?|sch[ée]mas?)\b",
        text,
    )
    if not match:
        return 0
    raw = match.group(1).casefold()
    return int(raw) if raw.isdigit() else number_words.get(raw, 0)


def _primary_writer(plan: Dict[str, Any], primary: str) -> Dict[str, Any] | None:
    for step in plan.get("steps", []):
        artifacts = {
            str(path).replace("\\", "/") for path in step.get("required_artifacts", [])
        }
        if primary and primary in artifacts:
            return step
    return None


def _fold(value: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", value).casefold()
        if not unicodedata.combining(character)
    )


def _requested_professional_headings(text: str) -> list[str]:
    """Promote explicitly named professional sections into deterministic gates."""
    folded = _fold(text)
    french = any(marker in folded for marker in ("synthese", "feuille de route", "criteres"))
    candidates = (
        (("synthese executive", "executive summary"), "Synthèse exécutive", "Executive Summary"),
        (("analyse detaillee", "detailed analysis"), "Analyse détaillée", "Detailed Analysis"),
        (("matrice de tracabilite", "traceability matrix"), "Matrice de traçabilité", "Traceability Matrix"),
        (("registre des risques", "risk register"), "Registre des risques", "Risk Register"),
        (("feuille de route", "roadmap"), "Feuille de route 30/60/90 jours", "30/60/90 Roadmap"),
        (("plan de tests", "test plan"), "Plan de tests", "Test Plan"),
        (("criteres d'acceptation", "criteres d’acceptation", "acceptance criteria"), "Critères d’acceptation", "Acceptance Criteria"),
    )
    return [
        french_title if french else english_title
        for markers, french_title, english_title in candidates
        if any(marker in folded for marker in markers)
    ]


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
            {
                "slides": max(slides),
                "normalized_blocks": len(document.blocks),
            }
            if slides else {"blocks": len(document.blocks)}
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
    requirements_text = _requirement_text(plan)
    page_range = _requested_page_range(requirements_text)
    requested_diagrams = _requested_diagram_count(requirements_text)
    requested_headings = _requested_professional_headings(requirements_text)
    mandatory_ids = [
        str(item.get("id"))
        for item in plan.get("requirements", [])
        if isinstance(item, dict) and item.get("mandatory", True) and item.get("id")
    ]
    validations = plan.setdefault("artifact_validations", [])
    if not isinstance(validations, list):
        validations = []
        plan["artifact_validations"] = validations
    text_paths = []
    text_owners: Dict[str, Dict[str, Any]] = {}
    for step in plan.get("steps", []):
        for path in step.get("required_artifacts", []):
            normalized = str(path).replace("\\", "/")
            if normalized.lower().endswith((".md", ".txt", ".html")):
                text_paths.append(normalized)
                text_owners[normalized] = step
    primary = str(plan.get("primary_artifact") or "")
    if not primary:
        primary = next(
            (path for path in text_paths if not path.startswith("analysis/")),
            text_paths[0] if text_paths else "",
        )
    primary_owner = _primary_writer(plan, primary)
    if primary_owner is not None:
        # The primary deliverable is the user-facing synthesis.  It must inherit
        # every mandatory requirement even when a planner also assigns some of
        # them to analysis or quality-report specialists.
        owned_ids = primary_owner.setdefault("requirement_ids", [])
        for identifier in mandatory_ids:
            if identifier not in owned_ids:
                owned_ids.append(identifier)

    primary_minimum_words = 600
    if page_range:
        primary_minimum_words = max(
            primary_minimum_words,
            page_range[0] * WORDS_PER_REQUESTED_PAGE,
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
        constraints["validate_arithmetic"] = True
        constraints["max_duplicate_paragraphs"] = 0
        constraints["max_duplicate_list_items"] = 0
        constraints["max_duplicate_headings"] = 0
        constraints["reject_heading_number_restarts"] = True
        constraints["reject_invalid_diagrams"] = True
        constraints.setdefault("duplicate_min_words", 12)
        minimums = constraints.setdefault("minimums", {})
        if not isinstance(minimums, dict):
            minimums = {}
            constraints["minimums"] = minimums
        support_floor = max(120, math.ceil(primary_minimum_words / 20))
        minimums["words"] = max(
            int(minimums.get("words") or 0),
            primary_minimum_words if path == primary else support_floor,
        )
        owner = text_owners.get(path, {})
        owner_blob = " ".join((
            str(owner.get("specialist") or ""),
            str(owner.get("description") or ""),
            path,
        )).casefold()
        # Semantic record schemas must be selected from stable ownership
        # metadata, never incidental words in a free-form task description.
        # For example, a corpus inventory may be asked to search "decision
        # topics" without being a decision register itself.
        owner_identity = " ".join((
            str(owner.get("specialist") or ""),
            path,
        )).casefold()
        decision_record_policy = {
                "heading_pattern": r"\b(?:DEC|ADR)-\d{3}\b",
                "minimum_records": 1,
                "preserve_existing_record_ids": True,
                "required_fields": {
                    "context": ["contexte", "context"],
                    "drivers": ["facteurs", "drivers", "motivations", "critères"],
                    "alternatives": ["alternative", "options"],
                    "decision": ["décision", "decision"],
                    "consequences": ["conséquence", "consequence", "impact"],
                    "risks": ["risque", "risk"],
                    "owner": ["propriétaire", "responsable", "owner"],
                    "validation_status": [
                        "statut de validation", "validation status", "validation",
                    ],
                },
            }
        if "decision" in owner_identity or "adr" in owner_identity:
            existing_record_policy = constraints.get("record_section_policy")
            if isinstance(existing_record_policy, dict):
                existing_ids = existing_record_policy.get("required_record_ids")
                if isinstance(existing_ids, list) and existing_ids:
                    decision_record_policy["required_record_ids"] = list(existing_ids)
                    decision_record_policy["minimum_records"] = max(
                        int(decision_record_policy["minimum_records"]), len(existing_ids),
                    )
            constraints["record_section_policy"] = decision_record_policy
        else:
            stale_record_policy = constraints.get("record_section_policy")
            comparable_stale = (
                dict(stale_record_policy) if isinstance(stale_record_policy, dict) else {}
            )
            comparable_current = dict(decision_record_policy)
            comparable_stale.pop("preserve_existing_record_ids", None)
            comparable_stale.pop("required_record_ids", None)
            comparable_current.pop("preserve_existing_record_ids", None)
            comparable_current.pop("required_record_ids", None)
            if comparable_stale and comparable_stale == comparable_current:
                # Remove profile-generated policies persisted by older,
                # overly broad classifiers while preserving custom schemas.
                constraints.pop("record_section_policy", None)
        if requested_diagrams and any(
            marker in owner_blob
            for marker in ("application", "integration", "data architect")
        ):
            minimums["valid_diagrams"] = max(
                int(minimums.get("valid_diagrams") or 0),
                min(2, requested_diagrams),
            )
        source_grounded = bool(
            path == primary
            or constraints.get("source_inventory")
            or constraints.get("required_source_files")
            or constraints.get("require_local_references")
        )
        if path == primary:
            if inventory:
                constraints["require_claim_references"] = True
            constraints.setdefault("claim_min_words", 24)
            if mandatory_ids:
                constraints["required_requirement_ids"] = mandatory_ids
                if re.search(r"(?i)\b(?:tra[çc]abilit[ée]|traceability)\b", requirements_text):
                    constraints["required_traceability_ids"] = mandatory_ids
            if requested_headings:
                existing_headings = constraints.setdefault("required_headings", [])
                if not isinstance(existing_headings, list):
                    existing_headings = []
                    constraints["required_headings"] = existing_headings
                for heading in requested_headings:
                    if heading not in existing_headings:
                        existing_headings.append(heading)
                constraints["min_section_words"] = max(
                    int(constraints.get("min_section_words") or 0), 120
                )
            if requested_diagrams:
                minimums["valid_diagrams"] = max(
                    int(minimums.get("valid_diagrams") or 0), requested_diagrams
                )
                constraints["reject_invalid_diagrams"] = True
            if re.search(
                r"(?i)\b(?:aucune?\s+(?:preuve\s+)?internet|sans\s+(?:preuve\s+)?internet|"
                r"no\s+internet\s+evidence|without\s+internet\s+evidence)\b",
                requirements_text,
            ):
                constraints["forbid_external_links"] = True
        if inventory and source_grounded:
            # The artifact store owns source identity. Planner policies may
            # contain basenames while a folder corpus preserves relative
            # paths; mixing both makes correct citations fail validation.
            constraints["source_inventory"] = inventory
            constraints["required_source_files"] = source_files
            constraints["require_local_references"] = True
            constraints["require_bounded_references"] = True
            if path == primary:
                constraints["require_claim_references"] = True
            minimums["cited_sources"] = max(int(minimums.get("cited_sources") or 0), len(source_files))
            minimums["local_references"] = max(int(minimums.get("local_references") or 0), len(source_files))
    for item in validations:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").replace("\\", "/").casefold()
        if not path.endswith(".json"):
            continue
        constraints = item.setdefault("constraints", {})
        if not isinstance(constraints, dict):
            constraints = {}
            item["constraints"] = constraints
        constraints["top_level_type"] = "dict"
        if path.endswith("quality-policy.json"):
            constraints["required_keys"] = [
                "minimums", "required_requirement_ids", "required_source_files",
                "source_inventory",
            ]
        elif path.endswith("quality-report.json"):
            constraints["required_keys"] = [
                "validator", "valid", "failures", "warnings", "metrics",
            ]
    plan["professional_profile"] = {
        "name": PROFILE,
        "primary_artifact": primary,
        "source_count": len(source_files),
        "quality_gate": "deterministic",
        "requested_page_range": list(page_range) if page_range else [],
        "minimum_words": primary_minimum_words,
        "minimum_valid_diagrams": requested_diagrams,
        "required_headings": requested_headings,
    }
    return plan
