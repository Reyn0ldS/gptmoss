"""Canonical, restartable state for source-grounded document deliveries.

The model deliberately contains no provider-specific objects.  It is a small
serialisable contract shared by planning, writing, validation and rendering.
Checkpoints are written atomically so an interrupted execution can resume
without losing the last coherent document state.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EvidenceReference:
    source: str
    locator: str
    summary: str = ""
    requirement_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["requirement_ids"] = list(self.requirement_ids)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EvidenceReference":
        return cls(
            source=str(value.get("source", "")),
            locator=str(value.get("locator", "")),
            summary=str(value.get("summary", "")),
            requirement_ids=tuple(str(item) for item in value.get("requirement_ids", [])),
        )


@dataclass
class SectionContract:
    section_id: str
    heading: str
    purpose: str
    target_words: int = 400
    required_topics: list[str] = field(default_factory=list)
    requirement_ids: list[str] = field(default_factory=list)
    evidence_refs: list[EvidenceReference] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    status: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_refs"] = [item.to_dict() for item in self.evidence_refs]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SectionContract":
        return cls(
            section_id=str(value.get("section_id", "")),
            heading=str(value.get("heading", "")),
            purpose=str(value.get("purpose", "")),
            target_words=max(1, int(value.get("target_words", 400))),
            required_topics=[str(item) for item in value.get("required_topics", [])],
            requirement_ids=[str(item) for item in value.get("requirement_ids", [])],
            evidence_refs=[EvidenceReference.from_dict(item) for item in value.get("evidence_refs", [])],
            dependencies=[str(item) for item in value.get("dependencies", [])],
            status=str(value.get("status", "pending")),
        )


@dataclass
class DocumentSection:
    contract: SectionContract
    content: str = ""
    revision: int = 0
    word_count: int = 0
    quality_flags: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=_now)

    def record(self, content: str, quality_flags: Iterable[str] = ()) -> None:
        self.content = str(content or "").strip()
        self.word_count = len(self.content.split())
        self.quality_flags = [str(item) for item in quality_flags]
        self.revision += 1
        self.contract.status = "complete" if self.content else "pending"
        self.updated_at = _now()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["contract"] = self.contract.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DocumentSection":
        contract = SectionContract.from_dict(value.get("contract", {}))
        section = cls(
            contract=contract,
            content=str(value.get("content", "")),
            revision=max(0, int(value.get("revision", 0))),
            word_count=max(0, int(value.get("word_count", 0))),
            quality_flags=[str(item) for item in value.get("quality_flags", [])],
            updated_at=str(value.get("updated_at", _now())),
        )
        if section.content and not section.word_count:
            section.word_count = len(section.content.split())
        return section


@dataclass
class DocumentModel:
    execution_id: str
    title: str
    output_path: str
    sections: list[DocumentSection] = field(default_factory=list)
    evidence_inventory: list[dict[str, Any]] = field(default_factory=list)
    diagrams: list[dict[str, Any]] = field(default_factory=list)
    requirements: list[dict[str, Any]] = field(default_factory=list)
    revision: int = 0
    status: str = "planned"
    last_error: str = ""
    updated_at: str = field(default_factory=_now)

    def section(self, section_id: str) -> DocumentSection | None:
        return next((item for item in self.sections if item.contract.section_id == section_id), None)

    def upsert_section(self, section: DocumentSection) -> None:
        existing = self.section(section.contract.section_id)
        if existing is None:
            self.sections.append(section)
        else:
            index = self.sections.index(existing)
            self.sections[index] = section
        self.revision += 1
        self.status = "writing"
        self.updated_at = _now()

    def assemble_markdown(self) -> str:
        parts = [f"# {self.title}".strip()]
        for section in self.sections:
            if section.content:
                parts.extend([f"\n## {section.contract.heading}", section.content.strip()])
        return "\n\n".join(parts).strip() + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "execution_id": self.execution_id,
            "title": self.title,
            "output_path": self.output_path,
            "sections": [item.to_dict() for item in self.sections],
            "evidence_inventory": self.evidence_inventory,
            "diagrams": self.diagrams,
            "requirements": self.requirements,
            "revision": self.revision,
            "status": self.status,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DocumentModel":
        version = int(value.get("schema_version", 0))
        if version not in {0, SCHEMA_VERSION}:
            raise ValueError(f"Unsupported document model schema version: {version}")
        return cls(
            execution_id=str(value.get("execution_id", "")),
            title=str(value.get("title", "Document")),
            output_path=str(value.get("output_path", "deliverable.md")),
            sections=[DocumentSection.from_dict(item) for item in value.get("sections", [])],
            evidence_inventory=list(value.get("evidence_inventory", [])),
            diagrams=list(value.get("diagrams", [])),
            requirements=list(value.get("requirements", [])),
            revision=max(0, int(value.get("revision", 0))),
            status=str(value.get("status", "planned")),
            last_error=str(value.get("last_error", "")),
            updated_at=str(value.get("updated_at", _now())),
        )


class DocumentModelStore:
    """Atomic JSON checkpoints with conservative corruption handling."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, execution_id: str) -> Path:
        safe_id = "".join(char for char in str(execution_id) if char.isalnum() or char in "-_")
        if not safe_id:
            raise ValueError("execution_id must contain at least one safe character")
        return self.root / f"{safe_id}.document.json"

    def save(self, model: DocumentModel) -> Path:
        target = self.path_for(model.execution_id)
        payload = json.dumps(model.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target

    def load(self, execution_id: str) -> DocumentModel | None:
        target = self.path_for(execution_id)
        if not target.is_file():
            return None
        try:
            with target.open("r", encoding="utf-8") as stream:
                return DocumentModel.from_dict(json.load(stream))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            quarantine = target.with_suffix(target.suffix + f".corrupt-{int(datetime.now().timestamp())}")
            try:
                os.replace(target, quarantine)
            except OSError:
                pass
            raise ValueError(f"Document checkpoint is corrupt: {target}: {exc}") from exc
