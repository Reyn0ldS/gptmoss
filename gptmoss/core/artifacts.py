"""Safe storage and context preparation for user-provided files and images."""

import base64
import hashlib
import json
import mimetypes
import os
import re
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List

from gptmoss.core.corpus import LocalDocumentIndex
from gptmoss.core.durable_io import (
    unlink_resilient,
    write_bytes_atomic,
    write_text_atomic,
)
from gptmoss.core.documents import (
    NormalizedDocument,
    SUPPORTED_DOCUMENT_TYPES,
    parse_document,
)


class ArtifactStore:
    MAX_BYTES = 0
    TEXT_TYPES = {"text/plain", "text/markdown", "application/json", "text/csv"}
    DOCUMENT_TYPES = set(SUPPORTED_DOCUMENT_TYPES)
    IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}

    def __init__(self, workspace_root: str, max_bytes: int = 0, max_text_chars: int = 0):
        self.root = Path(workspace_root).resolve() / "uploads"
        self.max_bytes = max(0, int(max_bytes))
        self.max_text_chars = max(0, int(max_text_chars))
        self.root.mkdir(parents=True, exist_ok=True)
        self.document_index = LocalDocumentIndex(
            self.root / "document-index.json"
        )
        self._synchronize_document_index()

    def update_limits(self, max_bytes: int = 0, max_text_chars: int = 0) -> None:
        """Use zero to remove the upload ceiling or select text budgets per task."""
        self.max_bytes = max(0, int(max_bytes))
        self.max_text_chars = max(0, int(max_text_chars))

    @staticmethod
    def _safe_name(filename: str) -> str:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)
        return name or "upload.bin"

    def save_base64(self, filename: str, content_base64: str, content_type: str) -> Dict[str, Any]:
        declared_type = (content_type or "").split(";", 1)[0].strip().lower()
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid base64 upload payload.") from exc
        if not data or (self.max_bytes and len(data) > self.max_bytes):
            maximum = self.max_bytes if self.max_bytes else "the available infrastructure capacity"
            raise ValueError(f"Upload must contain between 1 byte and {maximum}.")
        if declared_type == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid PNG data.")
        if declared_type == "image/jpeg" and not data.startswith(b"\xff\xd8"):
            raise ValueError("Invalid JPEG data.")
        if declared_type == "image/webp" and not (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
            raise ValueError("Invalid WebP data.")
        artifact_id = str(uuid.uuid4())
        safe_name = self._safe_name(filename)
        path = self.root / f"{artifact_id}_{safe_name}"
        document_path: Path | None = None
        metadata_path = self.root / f"{artifact_id}.json"
        indexed = False
        try:
            write_bytes_atomic(path, data)
            metadata = {
                "id": artifact_id,
                "filename": safe_name,
                "content_type": declared_type,
                "path": str(path),
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "created_at": time.time(),
            }
            if declared_type in self.IMAGE_TYPES:
                pass
            else:
                document = parse_document(
                    path,
                    supplied_content_type=declared_type or None,
                ).with_filename(safe_name)
                document_path = self.root / f"{artifact_id}.document.json"
                write_text_atomic(document_path, document.to_json())
                metadata.update(
                    {
                        "content_type": document.content_type,
                        "document_path": str(document_path),
                        "document_id": document.id,
                        "document_title": document.title,
                        "document_blocks": len(document.blocks),
                        "document_parser": document.parser,
                        "document_parser_version": document.parser_version,
                    }
                )
                metadata["document_chunks"] = self.document_index.add_document(
                    artifact_id,
                    document,
                )
                indexed = True
            write_text_atomic(
                metadata_path,
                json.dumps(metadata, ensure_ascii=False),
            )
            return metadata
        except (OSError, ValueError):
            try:
                self.document_index.remove_document(artifact_id, persist=indexed)
            except OSError:
                self.document_index.remove_document(artifact_id, persist=False)
            with suppress(OSError):
                unlink_resilient(path)
            if document_path is not None:
                with suppress(OSError):
                    unlink_resilient(document_path)
            with suppress(OSError):
                unlink_resilient(metadata_path)
            raise

    def _document_metadata(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for metadata_path in self.root.glob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (
                isinstance(metadata, dict)
                and metadata.get("id")
                and metadata.get("document_id")
                and metadata.get("path")
            ):
                records.append(metadata)
        return records

    def _synchronize_document_index(self) -> None:
        records = self._document_metadata()
        expected = {
            str(metadata["id"]): str(metadata["document_id"])
            for metadata in records
        }
        if (
            not self.document_index.load_error
            and self.document_index.fingerprints() == expected
        ):
            return
        documents: list[tuple[str, NormalizedDocument]] = []
        for metadata in records:
            try:
                documents.append(
                    (str(metadata["id"]), self.document(str(metadata["id"])))
                )
            except (FileNotFoundError, KeyError, OSError, ValueError):
                continue
        self.document_index.rebuild(documents)

    def rebuild_document_index(self) -> dict[str, int]:
        documents: list[tuple[str, NormalizedDocument]] = []
        for metadata in self._document_metadata():
            try:
                documents.append(
                    (str(metadata["id"]), self.document(str(metadata["id"])))
                )
            except (FileNotFoundError, KeyError, OSError, ValueError):
                continue
        return self.document_index.rebuild(documents)

    def search_documents(
        self,
        query: str,
        *,
        limit: int = 8,
        artifact_ids: List[str] | None = None,
        content_types: List[str] | None = None,
        heading: str | None = None,
        kinds: List[str] | None = None,
    ) -> List[Dict[str, Any]]:
        return self.document_index.search(
            query,
            limit=limit,
            artifact_ids=artifact_ids,
            content_types=content_types,
            heading=heading,
            kinds=kinds,
        )

    def get(self, artifact_id: str) -> Dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f-]{36}", artifact_id):
            raise ValueError("Invalid artifact id.")
        path = self.root / f"{artifact_id}.json"
        if not path.exists():
            raise FileNotFoundError("Artifact not found.")
        metadata = json.loads(path.read_text(encoding="utf-8"))
        data_path = Path(metadata.get("path", "")).resolve()
        if self.root != data_path.parent or not data_path.exists():
            raise FileNotFoundError("Artifact data not found.")
        return metadata

    def document(self, artifact_id: str) -> NormalizedDocument:
        metadata = self.get(artifact_id)
        document_path_value = metadata.get("document_path")
        if document_path_value:
            document_path = Path(document_path_value).resolve()
            if self.root != document_path.parent or not document_path.exists():
                raise FileNotFoundError("Normalized document data not found.")
            try:
                payload = json.loads(document_path.read_text(encoding="utf-8"))
                document = NormalizedDocument.from_dict(payload)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("Normalized document data is invalid.") from exc
            if document.id != metadata.get("sha256"):
                raise ValueError("Normalized document does not match its source artifact.")
            return document

        if metadata["content_type"] in self.DOCUMENT_TYPES:
            return parse_document(
                metadata["path"],
                supplied_content_type=metadata["content_type"],
            ).with_filename(metadata["filename"])
        raise ValueError("Artifact is not a document.")

    def preview_text(self, artifact_id: str) -> str:
        return self.document(artifact_id).to_markdown()

    def delete(self, artifact_id: str) -> Dict[str, Any]:
        metadata = self.get(artifact_id)
        self.document_index.remove_document(artifact_id)
        Path(metadata["path"]).unlink(missing_ok=True)
        document_path_value = metadata.get("document_path")
        if document_path_value:
            document_path = Path(document_path_value).resolve()
            if document_path.parent == self.root:
                document_path.unlink(missing_ok=True)
        (self.root / f"{artifact_id}.json").unlink(missing_ok=True)
        return metadata

    @staticmethod
    def _context_text(text: str, limit: int) -> tuple[str, bool]:
        if not limit or len(text) <= limit:
            return text, False
        notice = "\n… [middle of attachment compacted adaptively] …\n"
        available = max(0, limit - len(notice))
        head = (available * 2) // 3
        tail = available - head
        return text[:head] + notice + (text[-tail:] if tail else ""), True

    @staticmethod
    def _evenly_spaced_chunks(chunks: list[Any], count: int) -> list[Any]:
        if count >= len(chunks):
            return list(chunks)
        if count <= 1:
            return [chunks[len(chunks) // 2]]
        indices = {
            round(index * (len(chunks) - 1) / (count - 1))
            for index in range(count)
        }
        return [chunks[index] for index in sorted(indices)]

    @staticmethod
    def _render_context_chunk(chunk: Any) -> str:
        section = " / ".join(chunk.heading_path) or "(root)"
        return (
            f"[Local source: {chunk.filename} | section: {section} | "
            f"blocks: {chunk.start_order}-{chunk.end_order} | chunk: {chunk.id}]\n"
            f"{chunk.text}"
        )

    def context_items(
        self,
        artifact_ids: List[str],
        supports_vision: bool = False,
        max_text_chars: int | None = None,
        query: str = "",
    ) -> List[Dict[str, Any]]:
        items = []
        text_limit = self.max_text_chars if max_text_chars is None else max(0, int(max_text_chars))
        metadata_items = [self.get(artifact_id) for artifact_id in artifact_ids]
        document_ids = [
            metadata["id"]
            for metadata in metadata_items
            if metadata["content_type"] in self.DOCUMENT_TYPES
        ]
        per_document_limit = 0
        if text_limit and document_ids:
            per_document_limit = max(128, text_limit // len(document_ids))

        ranked_by_artifact: Dict[str, List[Dict[str, Any]]] = {}
        if query.strip() and document_ids:
            ranked = self.search_documents(
                query,
                limit=max(8, min(100, len(document_ids) * 8)),
                artifact_ids=document_ids,
            )
            for result in ranked:
                ranked_by_artifact.setdefault(result["artifact_id"], []).append(result)

        for metadata in metadata_items:
            artifact_id = metadata["id"]
            path = Path(metadata["path"])
            item = {key: metadata[key] for key in ("id", "filename", "content_type", "size_bytes", "sha256")}
            if metadata["content_type"] in self.DOCUMENT_TYPES:
                document = self.document(artifact_id)
                full_text = document.to_markdown()
                chunks = self.document_index.chunks_for_artifact(artifact_id)
                selected_chunks: list[Any] = []
                ranked_results = ranked_by_artifact.get(artifact_id, [])
                if ranked_results:
                    selected_chunks = [
                        self.document_index.get_chunk(result["id"])
                        for result in ranked_results
                    ]
                elif per_document_limit and len(full_text) > per_document_limit:
                    estimated_count = max(1, per_document_limit // 1_800)
                    selected_chunks = self._evenly_spaced_chunks(
                        chunks,
                        min(len(chunks), estimated_count),
                    )

                if selected_chunks and (query.strip() or per_document_limit):
                    if per_document_limit:
                        maximum_selected = max(1, per_document_limit // 700)
                        selected_chunks = selected_chunks[:maximum_selected]
                        chunk_allowance = max(
                            128,
                            per_document_limit // len(selected_chunks),
                        )
                    else:
                        chunk_allowance = 0
                    rendered: list[str] = []
                    used = 0
                    selected_ids: list[str] = []
                    for chunk in selected_chunks:
                        value = self._render_context_chunk(chunk)
                        if per_document_limit:
                            remaining = per_document_limit - used
                            if remaining <= 0:
                                break
                            allowance = min(remaining, chunk_allowance)
                            if len(value) > allowance:
                                value, _ = self._context_text(value, allowance)
                        if value:
                            rendered.append(value)
                            selected_ids.append(chunk.id)
                            used += len(value) + 2
                    item["text"] = "\n\n".join(rendered)
                    item["text_compacted"] = len(selected_ids) < len(chunks)
                    item["retrieval"] = {
                        "query": query.strip(),
                        "selected_chunk_ids": selected_ids,
                        "selected_chunk_count": len(selected_ids),
                        "total_chunk_count": len(chunks),
                        "strategy": (
                            "ranked_local_search"
                            if ranked_results
                            else "even_structural_sampling"
                        ),
                    }
                else:
                    item["text"], item["text_compacted"] = self._context_text(
                        full_text,
                        per_document_limit,
                    )
                    item["retrieval"] = {
                        "query": query.strip(),
                        "selected_chunk_ids": [chunk.id for chunk in chunks],
                        "selected_chunk_count": len(chunks),
                        "total_chunk_count": len(chunks),
                        "strategy": "complete_document",
                    }
                item["text_total_chars"] = len(full_text)
                item["document"] = {
                    "id": document.id,
                    "title": document.title,
                    "parser": document.parser,
                    "parser_version": document.parser_version,
                    "block_count": len(document.blocks),
                }
            elif supports_vision:
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                item["image_url"] = f"data:{metadata['content_type']};base64,{encoded}"
            else:
                item["note"] = "Image attached; the configured model does not advertise vision support."
            items.append(item)
        return items
