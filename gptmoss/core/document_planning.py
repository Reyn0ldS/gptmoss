"""Deterministic complexity estimation and adaptive document-stage selection.

The LLM may still return a richer DAG, but the local fallback must never be a
fixed ceremony.  This module selects only the stages justified by the request
while retaining independent repair and final assurance for every document.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any
from unicodedata import combining, normalize


@dataclass(frozen=True)
class DocumentWorkEstimate:
    complexity: str
    source_count: int
    output_count: int
    requested_words: int
    has_diagrams: bool
    stage_budget: int
    reasons: tuple[str, ...] = ()


def estimate_document_work(task: str, analysis: dict[str, Any] | None = None) -> DocumentWorkEstimate:
    text = str(task or "")
    lowered = text.casefold()
    file_matches = list(re.finditer(
        r"\b[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:docx|pptx|pdf|txt|html|md|json)\b",
        text,
        re.I,
    ))
    output_name = re.compile(
        r"(?i)(?:deliverable|delivery|livrable|output|report|rapport|dossier|manifest|"
        r"decision-log|traceability|requirements|executive-summary)"
    )
    source_context = re.compile(
        r"\b(?:from|using|source|sources|input|inputs|attachment|attachments|"
        r"depuis|corpus|piece jointe|pieces jointes|a partir de|analyse|inspecte)\b"
    )
    output_context = re.compile(
        r"\b(?:create|generate|produce|write|export|save|deliver|output|"
        r"cree|generer|genere|produis|produire|redige|exporte|enregistre|livre|livrable)\b"
    )
    sources: list[str] = []
    outputs: list[str] = []
    for file_match in file_matches:
        filename = file_match.group(0)
        raw_context = text[max(0, file_match.start() - 64):file_match.start()]
        context = "".join(
            character for character in normalize("NFKD", raw_context).casefold()
            if not combining(character)
        )
        last_source = max((item.start() for item in source_context.finditer(context)), default=-1)
        last_output = max((item.start() for item in output_context.finditer(context)), default=-1)
        if output_name.search(filename) or last_output > last_source:
            outputs.append(filename)
        else:
            sources.append(filename)
    requested_words = 0
    match = re.search(r"(?i)\b(?:minimums?\s+)?words\s*=\s*(\d[\d _]*)", text)
    if match:
        requested_words = int(match.group(1).replace(" ", "").replace("_", ""))
    has_diagrams = any(marker in lowered for marker in ("diagram", "mermaid", "schéma", "schema", "uml", "architecture view"))
    score = len(set(item.casefold() for item in sources)) * 2 + len(set(item.casefold() for item in outputs))
    score += 3 if requested_words >= 3000 else 2 if requested_words >= 1200 else 0
    score += 2 if len(text) >= 2500 else 1 if len(text) >= 900 else 0
    if has_diagrams:
        score += 2
    if analysis:
        score += 1 if analysis.get("level") == "high" else 2 if analysis.get("level") == "very_high" else 0
    if score >= 18:
        complexity, budget = "very_high", 13
    elif score >= 11:
        complexity, budget = "high", 10
    elif score >= 6:
        complexity, budget = "moderate", 8
    else:
        complexity, budget = "low", 6
    reasons = []
    if sources:
        reasons.append(f"{len(set(sources))} local source(s)")
    if outputs:
        reasons.append(f"{len(set(outputs))} requested artifact(s)")
    if requested_words:
        reasons.append(f"{requested_words} target words")
    if has_diagrams:
        reasons.append("diagram requirement")
    return DocumentWorkEstimate(
        complexity=complexity,
        source_count=len(set(item.casefold() for item in sources)),
        output_count=len(set(item.casefold() for item in outputs)),
        requested_words=requested_words,
        has_diagrams=has_diagrams,
        stage_budget=budget,
        reasons=tuple(reasons),
    )


def _renumber(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapping = {str(step["id"]): index for index, step in enumerate(steps)}
    result = []
    for index, original in enumerate(steps):
        step = dict(original)
        step["id"] = index
        dependencies = [mapping[str(item)] for item in original.get("dependencies", []) if str(item) in mapping]
        if index and not dependencies:
            dependencies = [index - 1]
        step["dependencies"] = dependencies
        result.append(step)
    return result


def adapt_document_steps(
    task: str,
    analysis: dict[str, Any],
    steps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], DocumentWorkEstimate]:
    """Select a justified subset of the canonical stages and repair the DAG.

    The full 13-stage path remains available for very-high complexity dossiers
    (large source corpus, many outputs, strict word/traceability gates).  Small
    requests receive a compact plan with no architecture ceremonies that do
    not apply.
    """
    estimate = estimate_document_work(task, analysis)
    if estimate.complexity == "very_high":
        return steps, replace(estimate, stage_budget=len(steps))

    by_name = {str(step.get("specialist", "")).casefold(): step for step in steps}
    names = list(by_name)
    wanted: list[str]

    def pick(marker: str) -> str | None:
        return next((name for name in names if marker in name), None)

    first = pick("local corpus") or names[0]
    requirements = pick("requirements")
    decisions = pick("decision")
    application = pick("application")
    security = pick("identity")
    platform = pick("platform")
    migration = pick("migration")
    writer = pick("professional") or pick("editor")
    quality = pick("quality analyst")
    repair = pick("repair")
    report = pick("quality evidence")
    audit = pick("deterministic")
    coordinator = next((name for name in names if "final requirement" in name), None) or names[-1]

    wanted = [first]
    if requirements:
        wanted.append(requirements)
    if estimate.complexity in {"high", "moderate"}:
        for item in (decisions, application):
            if item:
                wanted.append(item)
    if estimate.complexity == "high":
        for item in (security, platform):
            if item:
                wanted.append(item)
    if estimate.has_diagrams and application and application not in wanted:
        wanted.append(application)
    if writer:
        wanted.append(writer)
    if quality:
        wanted.append(quality)
    if repair:
        wanted.append(repair)
    if report and (estimate.complexity != "low" or estimate.output_count > 2):
        wanted.append(report)
    if audit:
        wanted.append(audit)
    if coordinator:
        wanted.append(coordinator)

    selected = []
    seen = set()
    for name in wanted:
        step = by_name.get(name)
        if step is not None and name not in seen:
            selected.append(step)
            seen.add(name)
    if len(selected) < 4:
        selected = [steps[0], steps[1], steps[7], steps[8], steps[9], steps[12]]
    selected = _renumber(selected)
    return selected, replace(estimate, stage_budget=len(selected))
