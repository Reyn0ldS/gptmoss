"""Deterministic planner fallbacks and document requirement assignment."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Mapping

from gptmoss.core.delivery import extract_requirements
from gptmoss.core.document_planning import adapt_document_steps, estimate_document_work
from gptmoss.planners.complexity import (
    analyze_task_complexity,
    requires_software_implementation,
)

_ARTIFACT_NAME = re.compile(
    r"(?<![\w./-])([A-Za-z0-9][A-Za-z0-9_.-]*\.(?:md|json|txt|html|docx|pptx))"
    r"(?![\w-]|\.[A-Za-z0-9])",
    flags=re.IGNORECASE,
)


def _document_deliverable_task(
    task: str,
    *,
    workload: Mapping[str, Any] | None = None,
    corpus_policy: Mapping[str, Any] | None = None,
) -> bool:
    """Distinguish source-grounded writing from generic software documentation."""
    policy = corpus_policy if isinstance(corpus_policy, Mapping) else {}
    if policy.get("professional_delivery"):
        return True
    if requires_software_implementation(task):
        return False
    text = str(task or "")
    lowered = text.casefold()
    formats = {
        suffix for suffix in ("docx", "pptx", "txt", "html", "markdown")
        if re.search(rf"(?<!\w){suffix}(?!\w)", lowered)
    }
    source_signals = sum(
        marker in lowered
        for marker in (
            "corpus", "pièces jointes", "pieces jointes", "pièce jointe", "piece jointe",
            "attached files", "fichiers locaux", "fichiers joints", "fichier joint",
            "local files", "documents.inventory", "documents.search",
        )
    )
    writing_signal = any(
        marker in lowered
        for marker in (
            "rédige", "redige", "rédaction", "redaction", "dossier", "rapport",
            "synthèse", "synthese", "livrable", "long-form", "write a", "produce a",
        )
    )
    explicit_validator = bool(re.search(
        r"(?i)(?:validator\s*=\s*document|validator[^\n]{0,20}document|"
        r"document-analysis|document quality)",
        text,
    ))
    workload_sources = 0
    if isinstance(workload, Mapping):
        workload_sources = max(
            int(workload.get("attachment_count") or 0),
            int(workload.get("document_count") or 0),
        )
    return explicit_validator or (
        writing_signal
        and (len(formats) >= 1 or source_signals >= 1 or workload_sources > 0)
    )


def _requested_output_artifacts(task: str) -> List[str]:
    """Extract output filenames without mistaking inline source citations for outputs."""
    outputs: List[str] = []
    for line in str(task or "").splitlines():
        if not re.match(r"\s*(?:\d+[.)]|[-*])\s+", line):
            continue
        match = _ARTIFACT_NAME.search(line)
        if match and match.group(1) not in outputs:
            outputs.append(match.group(1))
    verb_pattern = re.compile(
        r"(?i)\b(?:crée|cree|produis|génère|genere|write|create|produce|generate)"
        r"[^\r\n]{0,100}?" + _ARTIFACT_NAME.pattern
    )
    for match in verb_pattern.finditer(str(task or "")):
        filename = match.group(1)
        if filename and filename not in outputs:
            outputs.append(filename)
    naming_pattern = re.compile(
        r"(?i)\b(?:s['\N{RIGHT SINGLE QUOTATION MARK}]appelle|"
        r"s['\N{RIGHT SINGLE QUOTATION MARK}]appeler|named|called|nomm[\N{LATIN SMALL LETTER E WITH ACUTE}e])"
        r"[^\r\n]{0,40}?" + _ARTIFACT_NAME.pattern
    )
    for match in naming_pattern.finditer(str(task or "")):
        filename = match.group(1)
        if filename and filename not in outputs:
            outputs.append(filename)
    return outputs


def _expanded_identifier_ranges(task: str) -> List[str]:
    identifiers: List[str] = []
    pattern = re.compile(
        r"\b([A-Z][A-Z0-9]{1,12})-(\d{2,4})\s*"
        r"(?:à|a|to|through|–|-)\s*(?:\1-)?(\d{2,4})\b",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(str(task or "")):
        prefix, start_text, end_text = match.groups()
        start, end = int(start_text), int(end_text)
        if end < start or end - start > 200:
            continue
        width = max(len(start_text), len(end_text))
        for value in range(start, end + 1):
            identifier = f"{prefix.upper()}-{value:0{width}d}"
            if identifier not in identifiers:
                identifiers.append(identifier)
    return identifiers


def _required_document_headings(task: str) -> List[str]:
    match = re.search(
        r"(?is)sections?[^\n:]{0,160}(?:exactement|exactly)\s*:\s*"
        r"(.+?)(?:\r?\n\s*\r?\n|\bChaque\b|\bEvery\b)",
        str(task or ""),
    )
    if not match:
        return []
    headings = []
    for item in match.group(1).split(";"):
        heading = item.strip().strip(". ")
        if heading and len(heading) <= 160 and heading not in headings:
            headings.append(heading)
    return headings


def _source_inventory(task: str, outputs: List[str]) -> Dict[str, Dict[str, int]]:
    inventory: Dict[str, Dict[str, int]] = {}
    pattern = re.compile(
        r"\b([A-Za-z0-9][A-Za-z0-9_.-]*\.(?:docx|pptx|txt|html))\s+"
        r"(blocks|slides)\s*=\s*(\d+)",
        flags=re.IGNORECASE,
    )
    output_names = {item.casefold() for item in outputs}
    for filename, unit, count in pattern.findall(str(task or "")):
        if filename.casefold() in output_names:
            continue
        key = "slides" if unit.casefold() == "slides" else "blocks"
        inventory[filename] = {key: int(count)}
    return inventory


def _named_integer(task: str, name: str) -> int | None:
    match = re.search(
        rf"(?i)\b{re.escape(name)}\s*=\s*(\d[\d _]*)",
        str(task or ""),
    )
    if not match:
        return None
    return int(match.group(1).replace(" ", "").replace("_", ""))


def _document_validation_policy(
    task: str, outputs: List[str], primary: str
) -> Dict[str, Any]:
    inventory = _source_inventory(task, outputs)
    constraints: Dict[str, Any] = {}
    headings = _required_document_headings(task)
    identifiers = _expanded_identifier_ranges(task)
    if headings:
        constraints["required_headings"] = headings
    section_words = _named_integer(task, "min_section_words")
    if section_words is not None:
        constraints["min_section_words"] = section_words
    if identifiers:
        constraints["required_requirement_ids"] = identifiers
        if "required_traceability_ids" in str(task):
            constraints["required_traceability_ids"] = identifiers
    if inventory:
        constraints["required_source_files"] = list(inventory)
        constraints["source_inventory"] = inventory
    for name in (
        "require_local_references", "require_bounded_references",
        "require_claim_references", "forbid_external_links", "forbid_placeholders",
    ):
        if re.search(rf"(?i)\b{re.escape(name)}\s*=\s*true\b", str(task or "")):
            constraints[name] = True
    for name in (
        "claim_min_words", "duplicate_min_words", "max_duplicate_paragraphs",
    ):
        value = _named_integer(task, name)
        if value is not None:
            constraints[name] = value
    minimums: Dict[str, int] = {}
    minimum_match = re.search(r"(?is)\bminimums\b(.{0,160})", str(task or ""))
    if minimum_match:
        for metric in ("words", "local_references", "cited_sources", "headings"):
            match = re.search(
                rf"(?i)\b{metric}\s*=\s*(\d[\d _]*)", minimum_match.group(1)
            )
            if match:
                minimums[metric] = int(
                    match.group(1).replace(" ", "").replace("_", "")
                )
    if minimums:
        constraints["minimums"] = minimums
    return {
        "path": primary,
        "validator": "document",
        "required": True,
        "constraints": constraints,
    }


def _supporting_document_validation_policies(
    task: str, steps: List[Dict[str, Any]], primary: str
) -> List[Dict[str, Any]]:
    """Give every reusable document artifact a deterministic acceptance floor."""
    inventory = _source_inventory(task, [
        str(path)
        for step in steps
        for path in step.get("required_artifacts", [])
    ])
    identifiers = _expanded_identifier_ranges(task)
    forbid_external = bool(
        re.search(r"(?i)\bforbid_external_links\s*=\s*true\b", task)
        or re.search(r"(?i)\b(?:aucun|sans)\s+(?:lien|url).*internet", task)
    )
    policies: List[Dict[str, Any]] = []
    seen = {primary.replace("\\", "/")}
    for step in steps:
        for raw_path in step.get("required_artifacts", []):
            path = str(raw_path).replace("\\", "/")
            if path in seen:
                continue
            seen.add(path)
            suffix = os.path.splitext(path)[1].lower()
            if suffix == ".json":
                policies.append({
                    "path": path,
                    "validator": "json",
                    "required": True,
                    "constraints": {"min_size_bytes": 20},
                })
                continue
            if suffix not in {".md", ".txt", ".html"}:
                continue
            lower = path.casefold()
            source_grounded = (
                lower.startswith("analysis/")
                and not any(marker in lower for marker in ("quality", "audit"))
            ) or any(marker in lower for marker in (
                "requirement", "exigence", "evidence", "preuve", "decision", "adr",
            ))
            complete_source_coverage = any(marker in lower for marker in (
                "corpus-inventory", "requirement", "exigence", "evidence", "preuve",
            ))
            minimum_words = 300 if source_grounded else 120
            constraints: Dict[str, Any] = {
                "forbid_placeholders": True,
                "minimums": {"words": minimum_words},
            }
            if forbid_external:
                constraints["forbid_external_links"] = True
            if source_grounded:
                constraints["require_local_references"] = True
                constraints["minimums"]["local_references"] = 3
                if inventory:
                    constraints["source_inventory"] = inventory
                    constraints["require_bounded_references"] = True
            if complete_source_coverage and inventory:
                constraints["required_source_files"] = list(inventory)
                constraints["minimums"]["local_references"] = max(
                    4, len(inventory)
                )
                if "corpus-inventory" in lower:
                    constraints["require_source_coverage"] = True
            if identifiers and any(marker in lower for marker in (
                "requirement", "exigence", "evidence", "preuve",
            )):
                constraints["required_requirement_ids"] = identifiers
                constraints["required_traceability_ids"] = identifiers
            policies.append({
                "path": path,
                "validator": "document",
                "required": True,
                "constraints": constraints,
            })
    return policies


def _step_search_blob(step: Dict[str, Any]) -> str:
    artifacts = " ".join(str(item) for item in (step.get("required_artifacts") or []))
    owned = " ".join(str(item) for item in (step.get("owned_paths") or []))
    return " ".join([
        str(step.get("specialist") or ""),
        str(step.get("description") or ""),
        str(step.get("role") or ""),
        artifacts,
        owned,
    ]).casefold()


def _steps_matching(steps: List[Dict[str, Any]], *markers: str) -> List[Dict[str, Any]]:
    return [step for step in steps if any(marker in _step_search_blob(step) for marker in markers)]


def _assign_requirement(step: Dict[str, Any], requirement_id: str) -> None:
    identifiers = step.setdefault("requirement_ids", [])
    if requirement_id not in identifiers:
        identifiers.append(requirement_id)


def _assign_document_requirements(
    task: str, steps: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Map document requirements to surviving specialists, never to frozen step ids."""
    requirements = extract_requirements(task)
    producers = [step for step in steps if step.get("role") in {"architect", "security", "writer"}]
    writers = [step for step in producers if step.get("role") == "writer"]
    primary_writers = [
        step for step in writers
        if (
            "professional" in _step_search_blob(step)
            or any(
                str(path).casefold().endswith((".md", ".txt", ".html"))
                and not str(path).replace("\\", "/").casefold().startswith("analysis/")
                and not any(
                    marker in str(path).casefold()
                    for marker in ("quality", "review", "audit")
                )
                for path in (step.get("required_artifacts") or [])
            )
        )
        and "quality evidence" not in _step_search_blob(step)
    ]
    primary_writer = primary_writers[0] if primary_writers else (writers[0] if writers else None)
    coordinators = [step for step in steps if step.get("role") == "coordinator"]
    final_coordinator = coordinators[-1] if coordinators else steps[-1]
    final_reviewers = [
        step for step in steps
        if any(marker in _step_search_blob(step) for marker in (
            "deterministic", "traceability auditor", "delivery reviewer",
            "final-delivery-audit", "delivery audit",
        ))
    ]
    if not final_reviewers:
        final_reviewers = [final_coordinator]
    for step in steps:
        step["requirement_ids"] = []

    for requirement in requirements:
        statement = str(requirement["statement"]).casefold()
        requirement_id = requirement["id"]
        named_owners = [
            step for step in steps
            if any(
                os.path.basename(str(path)).casefold() in statement
                for path in (step.get("required_artifacts") or [])
                if path
            )
        ]
        if any(marker in statement for marker in (
            "inventorier", "documents.inventory", "capability documents",
            "recherche puis lis", "pièces jointes locales", "pieces jointes locales",
        )):
            targets = _steps_matching(steps, "corpus", "inventory", "documents.inventory")
        elif any(marker in statement for marker in (
            "requirements-matrix", "evidence-matrix", "matrice", "toutes les exigences",
        )):
            targets = _steps_matching(
                steps, "traceability", "requirements &", "requirements-and-evidence",
                "requirements-matrix", "evidence-matrix",
            )
            targets = [
                step for step in targets
                if "quality" not in str(step.get("specialist") or "").casefold()
            ]
        elif any(marker in statement for marker in (
            "decisions-register", "contradiction", "paliers chiffr", "autorité compétente",
        )):
            targets = _steps_matching(steps, "decision", "contradiction")
        elif any(marker in statement for marker in (
            "quality-policy", "quality-report", "review-report", "validateur document",
            "delivery_assurance", "auditeur final",
        )):
            targets = _steps_matching(
                steps, "quality evidence", "review editor", "quality-policy",
                "quality-report", "review-report",
            )
        elif any(marker in statement for marker in (
            "sécurité", "securite", "sec-001", "identité", "identite",
        )):
            targets = _steps_matching(steps, "security", "privacy")
        elif any(marker in statement for marker in (
            "rto", "rpo", "capacité", "capacite", "déploiement", "deploiement",
            "résilience", "resilience", "observabilité", "observabilite",
        )):
            targets = _steps_matching(steps, "platform", "sre", "capacity")
        elif any(marker in statement for marker in (
            "migration", "coexistence", "feuille de route",
        )):
            targets = _steps_matching(steps, "migration", "operating model", "roadmap")
        elif "chaque auteur doit relire" in statement:
            targets = list(producers)
        elif any(marker in statement for marker in (
            "fichiers locaux comme sources", "aucune recherche web",
            "numéros doivent être réels", "numeros doivent etre reels",
            "aucun placeholder", "aucune url externe",
        )):
            targets = [
                step for step in steps
                if step.get("role") in {"architect", "security", "writer"}
            ]
        elif named_owners:
            targets = named_owners
        else:
            targets = [primary_writer] if primary_writer else (
                producers[-1:] if producers else [final_coordinator]
            )
        if not targets:
            if any(marker in statement for marker in ("inventor", "documents.inventory", "pièces jointes", "pieces jointes")):
                targets = _steps_matching(steps, "corpus")[:1] or producers[:1]
            elif any(marker in statement for marker in ("quality", "review", "audit", "rapport")):
                targets = _steps_matching(steps, "quality evidence", "deterministic")[:1] or producers[-1:]
            else:
                targets = [primary_writer] if primary_writer else (
                    producers[-1:] if producers else [final_coordinator]
                )
        for target in targets:
            _assign_requirement(target, requirement_id)
        _assign_requirement(final_coordinator, requirement_id)
        for reviewer in final_reviewers:
            _assign_requirement(reviewer, requirement_id)
    return requirements


def _step(step_id: int, role: str, specialist: str, description: str,
          dependencies: List[int], expertise: List[str], required_artifacts: List[str],
          acceptance_criteria: List[str], verification_commands: List[str] | None = None) -> Dict[str, Any]:
    operation = {
        "developer": "implement", "writer": "document_render", "qa": "validate",
        "debugger": "repair", "coordinator": "audit",
    }.get(role, "execute")
    obligation = {
        "implement": "implementation", "document_render": "document_render",
        "validate": "independent_validation", "repair": "autonomous_repair",
        "audit": "final_audit",
    }.get(operation)
    evidence = {
        "implement": ["implementation_artifacts"],
        "document_render": ["artifact_validation", "source_to_section_coverage"],
        "validate": ["artifact_validation", "independent_tool_evidence"],
        "repair": ["repair_history", "regression_validation"],
        "audit": ["requirements_traceability", "delivery_assurance"],
    }.get(operation, [])
    step = {"id": step_id, "role": role, "specialist": specialist, "description": description,
            "dependencies": dependencies, "expertise": expertise,
            "required_artifacts": required_artifacts, "acceptance_criteria": acceptance_criteria,
            "verification_commands": verification_commands or [], "requirement_ids": [],
            "owned_paths": list(required_artifacts), "satisfies_obligations": [],
            "required_evidence": [], "status": "pending"}
    if obligation:
        step.update({
            "operation": operation,
            "satisfies_obligations": [obligation],
            "required_evidence": evidence,
        })
    return step

