"""Provider-neutral orchestration for large professional documents.

This is an AgentWrite-style *workflow* rather than another model: the active
GPTMOSS provider writes one bounded section at a time, while this engine keeps
contracts, evidence memory, checkpoints and deterministic consolidation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from gptmoss.core.document_model import (
    DocumentModel,
    DocumentModelStore,
    DocumentSection,
    EvidenceReference,
    SectionContract,
)


@dataclass(frozen=True)
class SectionMemory:
    terminology: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    previous_headings: tuple[str, ...] = ()

    def as_prompt(self) -> str:
        return (
            "Terminology: " + ", ".join(self.terminology or ("use the source terminology",)) +
            "\nDecisions: " + "; ".join(self.decisions or ("none recorded",)) +
            "\nUnresolved: " + "; ".join(self.unresolved or ("none recorded",)) +
            "\nExisting headings: " + ", ".join(self.previous_headings or ("none",))
        )


def _word_target(text: str, default: int = 450) -> int:
    match = re.search(r"(?i)\b(?:min_section_words|section_words)\s*=\s*(\d[\d _]*)", text)
    if match:
        return max(80, int(match.group(1).replace(" ", "").replace("_", "")))
    match = re.search(r"(?i)\b(?:minimums?\s+)?words\s*=\s*(\d[\d _]*)", text)
    if match:
        total = int(match.group(1).replace(" ", "").replace("_", ""))
        return max(default, min(1800, total // 8))
    return default


class LongDocumentEngine:
    """Stateful section planner and consolidator backed by JSON checkpoints."""

    def __init__(self, checkpoint_root: str | Path):
        self.store = DocumentModelStore(checkpoint_root)

    def create_model(
        self,
        execution_id: str,
        task: str,
        output_path: str = "deliverable.md",
        requirements: Iterable[dict[str, Any]] = (),
    ) -> DocumentModel:
        title = "Document"
        match = re.search(r"(?i)(?:titled?|intitul[ée]|titre)\s*[:=]\s*[\"']?([^\"'\n]+)", task)
        if match:
            title = match.group(1).strip().rstrip(".") or title
        model = DocumentModel(
            execution_id=str(execution_id),
            title=title,
            output_path=str(output_path),
            writing_brief=str(task),
            requirements=[dict(item) for item in requirements],
        )
        self.store.save(model)
        return model

    def plan_sections(
        self,
        model: DocumentModel,
        headings: Iterable[str],
        evidence: Iterable[EvidenceReference] = (),
        requirements: Iterable[dict[str, Any]] = (),
        target_words: int | None = None,
    ) -> DocumentModel:
        refs = list(evidence)
        requirement_rows = list(requirements) or model.requirements
        contracts = []
        for index, heading in enumerate(headings, 1):
            clean_heading = str(heading).strip()
            if not clean_heading:
                continue
            section_id = f"SEC-{index:03d}"
            owned_ids = [
                str(item.get("id")) for item in requirement_rows
                if str(item.get("section", "")).casefold() == clean_heading.casefold()
            ]
            contracts.append(SectionContract(
                section_id=section_id,
                heading=clean_heading,
                purpose=f"Explain {clean_heading} with source-grounded facts, decisions and consequences.",
                target_words=target_words or _word_target(model.writing_brief or model.title),
                required_topics=[clean_heading],
                requirement_ids=owned_ids,
                evidence_refs=refs,
                dependencies=[f"SEC-{index - 1:03d}"] if index > 1 else [],
            ))
        assigned = {requirement_id for contract in contracts for requirement_id in contract.requirement_ids}
        unassigned = [
            str(item.get("id")) for item in requirement_rows
            if item.get("id") and str(item.get("id")) not in assigned
        ]
        for index, requirement_id in enumerate(unassigned):
            if contracts:
                contracts[index % len(contracts)].requirement_ids.append(requirement_id)
        model.sections = [DocumentSection(contract=item) for item in contracts]
        model.status = "planned"
        model.revision += 1
        self.store.save(model)
        return model

    def memory(self, model: DocumentModel) -> SectionMemory:
        headings = tuple(section.contract.heading for section in model.sections if section.content)
        decisions = tuple(
            str(item.get("decision")) for item in model.requirements
            if item.get("decision")
        )
        unresolved = tuple(
            str(item.get("statement")) for item in model.requirements
            if item.get("status") in {"open", "unresolved"}
        )
        terminology = tuple(dict.fromkeys(
            word for section in model.sections for word in re.findall(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", section.content)
        ))[:40]
        return SectionMemory(terminology=terminology, decisions=decisions, unresolved=unresolved, previous_headings=headings)

    def record_section(
        self,
        model: DocumentModel,
        section_id: str,
        content: str,
        quality_flags: Iterable[str] = (),
    ) -> DocumentModel:
        section = model.section(section_id)
        if section is None:
            raise KeyError(f"Unknown document section: {section_id}")
        section.record(content, quality_flags)
        model.upsert_section(section)
        self.store.save(model)
        return model

    def apply_patch(self, model: DocumentModel, section_id: str, replacement: str, reason: str) -> DocumentModel:
        return self.record_section(model, section_id, replacement, [f"patched: {reason}"])

    def consolidate(self, model: DocumentModel) -> str:
        content = model.assemble_markdown()
        complete = bool(model.sections) and all(section.content for section in model.sections)
        model.mark_status("complete" if complete else "writing")
        self.store.save(model)
        output = Path(model.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(content, encoding="utf-8", newline="\n")
        return content

    def checkpoint(self, model: DocumentModel, error: str | None = None) -> Path:
        if error:
            model.last_error = str(error)
            model.status = "paused"
        return self.store.save(model)

    def resume(self, execution_id: str) -> DocumentModel | None:
        return self.store.load(execution_id)
