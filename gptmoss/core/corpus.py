"""Persistent local corpus segmentation and lexical search for normalized documents."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from threading import RLock
from typing import Any, Iterable
import unicodedata

from gptmoss.core.documents import DocumentBlock, NormalizedDocument


_INDEX_VERSION = 1
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))


def _tokens(value: str) -> list[str]:
    return [
        token
        for token in _TOKEN_RE.findall(_fold(value))
        if len(token) > 1 or token.isdigit()
    ]


@dataclass(slots=True, frozen=True)
class DocumentChunk:
    id: str
    artifact_id: str
    document_id: str
    filename: str
    content_type: str
    title: str
    text: str
    block_ids: tuple[str, ...]
    block_kinds: tuple[str, ...]
    heading_path: tuple[str, ...]
    start_order: int
    end_order: int
    provenance: tuple[dict[str, Any], ...]
    part: int = 1
    part_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "artifact_id": self.artifact_id,
            "document_id": self.document_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "title": self.title,
            "text": self.text,
            "block_ids": list(self.block_ids),
            "block_kinds": list(self.block_kinds),
            "heading_path": list(self.heading_path),
            "start_order": self.start_order,
            "end_order": self.end_order,
            "provenance": [dict(item) for item in self.provenance],
            "part": self.part,
            "part_count": self.part_count,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DocumentChunk":
        return cls(
            id=str(value["id"]),
            artifact_id=str(value["artifact_id"]),
            document_id=str(value["document_id"]),
            filename=str(value["filename"]),
            content_type=str(value["content_type"]),
            title=str(value.get("title") or ""),
            text=str(value["text"]),
            block_ids=tuple(str(item) for item in value.get("block_ids", [])),
            block_kinds=tuple(str(item) for item in value.get("block_kinds", [])),
            heading_path=tuple(str(item) for item in value.get("heading_path", [])),
            start_order=int(value["start_order"]),
            end_order=int(value["end_order"]),
            provenance=tuple(dict(item) for item in value.get("provenance", [])),
            part=max(1, int(value.get("part", 1))),
            part_count=max(1, int(value.get("part_count", 1))),
        )


def _adaptive_target(total_characters: int, requested: int) -> int:
    if requested > 0:
        return max(512, requested)
    if total_characters <= 8_000:
        return 2_000
    estimate = int(math.sqrt(total_characters) * 42)
    return max(2_000, min(8_000, estimate))


def _split_oversized(text: str, target: int) -> list[str]:
    if len(text) <= target:
        return [text]
    parts: list[str] = []
    cursor = 0
    overlap = min(240, max(40, target // 12))
    while cursor < len(text):
        end = min(len(text), cursor + target)
        if end < len(text):
            boundary = max(
                text.rfind("\n", cursor + target // 2, end),
                text.rfind(" ", cursor + target // 2, end),
            )
            if boundary > cursor:
                end = boundary
        part = text[cursor:end].strip()
        if part:
            parts.append(part)
        if end >= len(text):
            break
        cursor = max(cursor + 1, end - overlap)
    return parts


def _block_text(block: DocumentBlock) -> str:
    if block.kind == "heading":
        level = max(1, min(6, int(block.metadata.get("level", 2))))
        return f"{'#' * level} {block.text}"
    if block.kind == "list_item":
        return f"- {block.text}"
    return block.text


def chunk_document(
    artifact_id: str,
    document: NormalizedDocument,
    *,
    target_chars: int = 0,
) -> tuple[DocumentChunk, ...]:
    """Split a document on structural boundaries without dropping any block."""

    if not document.blocks:
        return ()
    total = sum(len(block.text) for block in document.blocks)
    target = _adaptive_target(total, target_chars)
    pending: list[DocumentBlock] = []
    pending_text: list[str] = []
    chunks: list[DocumentChunk] = []

    def emit() -> None:
        if not pending:
            return
        text = "\n\n".join(pending_text).strip()
        if not text:
            pending.clear()
            pending_text.clear()
            return
        parts = _split_oversized(text, target)
        first = pending[0]
        last = pending[-1]
        provenances: list[dict[str, Any]] = []
        seen_provenance: set[str] = set()
        for block in pending:
            value = block.provenance.to_dict()
            key = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if key not in seen_provenance:
                seen_provenance.add(key)
                provenances.append(value)
        for part_index, part_text in enumerate(parts, start=1):
            digest = sha256(
                (
                    f"{artifact_id}:{document.id}:{first.order}:{last.order}:"
                    f"{part_index}:{part_text}"
                ).encode("utf-8")
            ).hexdigest()[:24]
            chunks.append(
                DocumentChunk(
                    id=digest,
                    artifact_id=artifact_id,
                    document_id=document.id,
                    filename=document.filename,
                    content_type=document.content_type,
                    title=document.title,
                    text=part_text,
                    block_ids=tuple(block.id for block in pending),
                    block_kinds=tuple(block.kind for block in pending),
                    heading_path=last.heading_path or first.heading_path,
                    start_order=first.order,
                    end_order=last.order,
                    provenance=tuple(provenances),
                    part=part_index,
                    part_count=len(parts),
                )
            )
        pending.clear()
        pending_text.clear()

    current_structure: tuple[tuple[str, ...], int | None] | None = None
    for block in document.blocks:
        structure = (block.heading_path, block.provenance.slide_number)
        rendered = _block_text(block)
        projected = sum(len(value) for value in pending_text) + len(rendered)
        begins_section = block.kind == "heading" and bool(pending)
        structure_changed = current_structure is not None and structure != current_structure
        if pending and (begins_section or structure_changed or projected > target):
            emit()
        pending.append(block)
        pending_text.append(rendered)
        current_structure = structure
        if len(rendered) > target:
            emit()
    emit()
    return tuple(chunks)


class LocalDocumentIndex:
    """Small persistent BM25-like index with source and structure filters."""

    def __init__(self, path: str | Path, *, target_chunk_chars: int = 0) -> None:
        self.path = Path(path).resolve()
        self.target_chunk_chars = max(0, int(target_chunk_chars))
        self._lock = RLock()
        self._documents: dict[str, dict[str, Any]] = {}
        self._chunks: dict[str, DocumentChunk] = {}
        self.load_error = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if int(payload.get("version", 0)) != _INDEX_VERSION:
                raise ValueError("unsupported document index version")
            documents = payload.get("documents")
            chunks = payload.get("chunks")
            if not isinstance(documents, dict) or not isinstance(chunks, list):
                raise ValueError("invalid document index shape")
            loaded_chunks = {
                chunk.id: chunk
                for chunk in (DocumentChunk.from_dict(item) for item in chunks)
            }
            self._documents = {
                str(artifact_id): dict(metadata)
                for artifact_id, metadata in documents.items()
            }
            self._chunks = loaded_chunks
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.load_error = str(exc)
            self._documents = {}
            self._chunks = {}

    def _payload(self) -> dict[str, Any]:
        return {
            "version": _INDEX_VERSION,
            "target_chunk_chars": self.target_chunk_chars,
            "documents": self._documents,
            "chunks": [
                chunk.to_dict()
                for chunk in sorted(
                    self._chunks.values(),
                    key=lambda item: (
                        item.filename.casefold(),
                        item.start_order,
                        item.part,
                        item.id,
                    ),
                )
            ],
        }

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(self._payload(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def fingerprints(self) -> dict[str, str]:
        with self._lock:
            return {
                artifact_id: str(metadata.get("document_id") or "")
                for artifact_id, metadata in self._documents.items()
            }

    def add_document(
        self,
        artifact_id: str,
        document: NormalizedDocument,
        *,
        persist: bool = True,
    ) -> int:
        chunks = chunk_document(
            artifact_id,
            document,
            target_chars=self.target_chunk_chars,
        )
        with self._lock:
            self._remove_unlocked(artifact_id)
            self._documents[artifact_id] = {
                "document_id": document.id,
                "filename": document.filename,
                "content_type": document.content_type,
                "title": document.title,
                "parser": document.parser,
                "parser_version": document.parser_version,
                "block_count": len(document.blocks),
                "chunk_count": len(chunks),
            }
            for chunk in chunks:
                self._chunks[chunk.id] = chunk
            if persist:
                self._persist()
        return len(chunks)

    def _remove_unlocked(self, artifact_id: str) -> None:
        self._documents.pop(artifact_id, None)
        self._chunks = {
            chunk_id: chunk
            for chunk_id, chunk in self._chunks.items()
            if chunk.artifact_id != artifact_id
        }

    def remove_document(self, artifact_id: str, *, persist: bool = True) -> bool:
        with self._lock:
            existed = artifact_id in self._documents
            self._remove_unlocked(artifact_id)
            if persist and (existed or self.path.exists()):
                self._persist()
            return existed

    def rebuild(
        self,
        documents: Iterable[tuple[str, NormalizedDocument]],
    ) -> dict[str, int]:
        with self._lock:
            self._documents = {}
            self._chunks = {}
            counts: dict[str, int] = {}
            for artifact_id, document in documents:
                counts[artifact_id] = self.add_document(
                    artifact_id,
                    document,
                    persist=False,
                )
            self._persist()
            self.load_error = ""
            return counts

    def inventory(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"artifact_id": artifact_id, **dict(metadata)}
                for artifact_id, metadata in sorted(
                    self._documents.items(),
                    key=lambda item: str(item[1].get("filename") or "").casefold(),
                )
            ]

    def _matching_chunks(
        self,
        *,
        artifact_ids: set[str] | None,
        content_types: set[str] | None,
        heading: str | None,
        kinds: set[str] | None,
    ) -> list[DocumentChunk]:
        heading_folded = _fold(heading or "")
        return [
            chunk
            for chunk in self._chunks.values()
            if (not artifact_ids or chunk.artifact_id in artifact_ids)
            and (not content_types or chunk.content_type in content_types)
            and (not kinds or bool(kinds.intersection(chunk.block_kinds)))
            and (
                not heading_folded
                or heading_folded in _fold(" / ".join(chunk.heading_path))
            )
        ]

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        artifact_ids: Iterable[str] | None = None,
        content_types: Iterable[str] | None = None,
        heading: str | None = None,
        kinds: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        with self._lock:
            candidates = self._matching_chunks(
                artifact_ids=set(artifact_ids or ()) or None,
                content_types=set(content_types or ()) or None,
                heading=heading,
                kinds=set(kinds or ()) or None,
            )
            if not candidates:
                return []
            token_counts = {
                chunk.id: Counter(_tokens(chunk.text))
                for chunk in candidates
            }
            lengths = {
                chunk_id: sum(counts.values())
                for chunk_id, counts in token_counts.items()
            }
            average_length = max(
                1.0,
                sum(lengths.values()) / max(1, len(lengths)),
            )
            document_frequency = {
                token: sum(
                    1 for counts in token_counts.values() if token in counts
                )
                for token in set(query_tokens)
            }
            query_folded = _fold(query).strip()
            scored: list[tuple[float, DocumentChunk, list[str]]] = []
            for chunk in candidates:
                counts = token_counts[chunk.id]
                score = 0.0
                matched: list[str] = []
                for token in query_tokens:
                    frequency = counts.get(token, 0)
                    if not frequency:
                        continue
                    matched.append(token)
                    frequency_documents = document_frequency.get(token, 0)
                    inverse_frequency = math.log(
                        1 + (len(candidates) - frequency_documents + 0.5)
                        / (frequency_documents + 0.5)
                    )
                    normalized_frequency = (
                        frequency * 2.5
                        / (
                            frequency
                            + 1.5
                            * (
                                0.25
                                + 0.75
                                * lengths[chunk.id]
                                / average_length
                            )
                        )
                    )
                    score += inverse_frequency * normalized_frequency
                    if token in _tokens(" ".join(chunk.heading_path)):
                        score += inverse_frequency * 1.4
                    if token in _tokens(chunk.title):
                        score += inverse_frequency * 0.8
                folded_text = _fold(chunk.text)
                if query_folded and query_folded in folded_text:
                    score += 2.5 + min(2.0, len(query_tokens) * 0.25)
                coverage = len(set(matched)) / len(set(query_tokens))
                score += coverage
                if score > 0:
                    scored.append((score, chunk, sorted(set(matched))))
            scored.sort(
                key=lambda item: (
                    -item[0],
                    item[1].filename.casefold(),
                    item[1].start_order,
                    item[1].part,
                )
            )
            return [
                {
                    "score": round(score, 6),
                    "matched_terms": matched,
                    **chunk.to_dict(),
                }
                for score, chunk, matched in scored[: max(1, min(int(limit), 100))]
            ]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "version": _INDEX_VERSION,
                "documents": len(self._documents),
                "chunks": len(self._chunks),
                "load_error": self.load_error,
                "path": str(self.path),
            }
