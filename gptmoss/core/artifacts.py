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
from pathlib import Path, PurePosixPath
from threading import RLock
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
    MAX_CORPUS_FILES = 10_000
    MAX_RELATIVE_PATH_CHARS = 1_000

    def __init__(self, workspace_root: str, max_bytes: int = 0, max_text_chars: int = 0):
        self.root = Path(workspace_root).resolve() / "uploads"
        self.max_bytes = max(0, int(max_bytes))
        self.max_text_chars = max(0, int(max_text_chars))
        self.root.mkdir(parents=True, exist_ok=True)
        self.corpora_root = self.root / "corpora"
        self._lock = RLock()
        self._digest_artifacts: dict[tuple[str, str], str] = {}
        self.document_index = LocalDocumentIndex(
            self.root / "document-index.json"
        )
        self._synchronize_document_index()
        self._refresh_digest_index()

    def update_limits(self, max_bytes: int = 0, max_text_chars: int = 0) -> None:
        """Use zero to remove the upload ceiling or select text budgets per task."""
        self.max_bytes = max(0, int(max_bytes))
        self.max_text_chars = max(0, int(max_text_chars))

    @staticmethod
    def _safe_name(filename: str) -> str:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)
        return name or "upload.bin"

    @classmethod
    def _safe_relative_path(cls, value: str) -> str:
        raw = str(value or "").strip()
        if raw.startswith(("/", "\\")):
            raise ValueError("Corpus relative path must not be absolute.")
        normalized = raw.replace("\\", "/")
        if not normalized or len(normalized) > cls.MAX_RELATIVE_PATH_CHARS:
            raise ValueError("Corpus relative path is empty or too long.")
        path = PurePosixPath(normalized)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("Corpus relative path is invalid.")
        if any("\x00" in part or ":" in part for part in path.parts):
            raise ValueError("Corpus relative path contains a forbidden character.")
        return path.as_posix()

    def _artifact_metadata_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for metadata_path in self.root.glob("*.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(metadata, dict) and metadata.get("id") and metadata.get("sha256"):
                records.append(metadata)
        return records

    def _refresh_digest_index(self) -> None:
        self._digest_artifacts = {
            (
                str(metadata["sha256"]),
                str(metadata.get("source_name") or metadata.get("filename") or ""),
            ): str(metadata["id"])
            for metadata in self._artifact_metadata_records()
        }

    def _corpus_path(self, corpus_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f-]{36}", str(corpus_id or "")):
            raise ValueError("Invalid corpus id.")
        return self.corpora_root / f"{corpus_id}.json"

    def get_corpus(self, corpus_id: str) -> Dict[str, Any]:
        path = self._corpus_path(corpus_id)
        if not path.exists():
            raise FileNotFoundError("Corpus not found.")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("id") != corpus_id:
            raise ValueError("Corpus manifest is invalid.")
        return payload

    def list_corpora(self) -> list[Dict[str, Any]]:
        items: list[Dict[str, Any]] = []
        for path in self.corpora_root.glob("*.json"):
            try:
                item = self.get_corpus(path.stem)
            except (OSError, ValueError, FileNotFoundError, json.JSONDecodeError):
                continue
            items.append(item)
        return sorted(items, key=lambda item: float(item.get("updated_at") or 0), reverse=True)

    def create_corpus(
        self,
        name: str,
        *,
        root_label: str = "",
        source_kind: str = "browser_folder",
        resume: bool = True,
    ) -> tuple[Dict[str, Any], bool]:
        clean_name = str(name or "").strip()[:200]
        clean_root = self._safe_relative_path(root_label or clean_name).split("/", 1)[0]
        if not clean_name:
            raise ValueError("Corpus name is required.")
        with self._lock:
            if resume:
                for item in self.list_corpora():
                    if (
                        item.get("source_kind") == source_kind
                        and str(item.get("root_label") or "").casefold() == clean_root.casefold()
                        and str(item.get("name") or "").casefold() == clean_name.casefold()
                    ):
                        item["state"] = "importing"
                        item["updated_at"] = time.time()
                        write_text_atomic(self._corpus_path(str(item["id"])), json.dumps(item, ensure_ascii=False))
                        return item, True
            now = time.time()
            corpus = {
                "id": str(uuid.uuid4()),
                "name": clean_name,
                "root_label": clean_root,
                "source_kind": source_kind,
                "state": "importing",
                "created_at": now,
                "updated_at": now,
                "entries": {},
                "skipped": [],
                "errors": [],
                "skipped_count": 0,
                "error_count": 0,
            }
            write_text_atomic(self._corpus_path(corpus["id"]), json.dumps(corpus, ensure_ascii=False))
            return corpus, False

    def _record_corpus_entry(
        self,
        corpus_id: str,
        relative_path: str,
        metadata: Dict[str, Any],
        *,
        last_modified: int = 0,
    ) -> Dict[str, Any]:
        corpus = self.get_corpus(corpus_id)
        entries = dict(corpus.get("entries") or {})
        if relative_path not in entries and len(entries) >= self.MAX_CORPUS_FILES:
            raise ValueError(f"Corpus cannot contain more than {self.MAX_CORPUS_FILES} files.")
        previous = entries.get(relative_path)
        entries[relative_path] = {
            "artifact_id": metadata["id"],
            "sha256": metadata["sha256"],
            "size_bytes": metadata["size_bytes"],
            "content_type": metadata["content_type"],
            "last_modified": max(0, int(last_modified or 0)),
        }
        corpus.update({"entries": entries, "state": "importing", "updated_at": time.time()})
        write_text_atomic(self._corpus_path(corpus_id), json.dumps(corpus, ensure_ascii=False))
        if previous and previous.get("artifact_id") != metadata.get("id"):
            self._remove_corpus_membership(
                str(previous.get("artifact_id") or ""), corpus_id, relative_path
            )
        return corpus

    def _remove_corpus_membership(
        self,
        artifact_id: str,
        corpus_id: str,
        relative_path: str = "",
    ) -> None:
        if not artifact_id:
            return
        try:
            metadata = self.get(artifact_id)
        except (OSError, ValueError, FileNotFoundError, KeyError, json.JSONDecodeError):
            return
        memberships = []
        for membership in list(metadata.get("corpus_memberships") or []):
            matches_corpus = str(membership.get("corpus_id") or "") == corpus_id
            matches_path = not relative_path or str(membership.get("relative_path") or "") == relative_path
            if matches_corpus and matches_path:
                continue
            memberships.append(membership)
        if memberships:
            metadata["corpus_memberships"] = memberships
        else:
            metadata.pop("corpus_memberships", None)
        write_text_atomic(
            self.root / f"{artifact_id}.json",
            json.dumps(metadata, ensure_ascii=False),
        )

    def finalize_corpus(
        self,
        corpus_id: str,
        *,
        present_paths: List[str],
        skipped: List[Dict[str, Any]] | None = None,
        errors: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        with self._lock:
            corpus = self.get_corpus(corpus_id)
            present = {self._safe_relative_path(path) for path in present_paths}
            previous_entries = dict(corpus.get("entries") or {})
            entries = {
                path: value
                for path, value in previous_entries.items()
                if path in present
            }
            corpus.update({
                "entries": entries,
                # Preserve the complete totals while bounding persisted samples.
                "skipped": list(skipped or [])[:1_000],
                "errors": list(errors or [])[:1_000],
                "skipped_count": len(skipped or []),
                "error_count": len(errors or []),
                "state": "partial" if errors else "ready",
                "updated_at": time.time(),
            })
            write_text_atomic(self._corpus_path(corpus_id), json.dumps(corpus, ensure_ascii=False))
            for relative_path, entry in previous_entries.items():
                if relative_path not in entries:
                    self._remove_corpus_membership(
                        str(entry.get("artifact_id") or ""), corpus_id, relative_path
                    )
            return corpus

    def delete_corpus(self, corpus_id: str) -> Dict[str, Any]:
        """Remove a corpus manifest while retaining immutable uploaded evidence."""
        with self._lock:
            corpus = self.get_corpus(corpus_id)
            for entry in dict(corpus.get("entries") or {}).values():
                artifact_id = str(entry.get("artifact_id") or "")
                if not artifact_id:
                    continue
                try:
                    metadata = self.get(artifact_id)
                except (OSError, ValueError, FileNotFoundError, KeyError, json.JSONDecodeError):
                    continue
                self._remove_corpus_membership(artifact_id, corpus_id)
            unlink_resilient(self._corpus_path(corpus_id))
            return corpus

    def save_base64(self, filename: str, content_base64: str, content_type: str) -> Dict[str, Any]:
        declared_type = (content_type or "").split(";", 1)[0].strip().lower()
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid base64 upload payload.") from exc
        return self.save_bytes(filename, data, content_type)

    def save_bytes(
        self,
        filename: str,
        data: bytes,
        content_type: str,
        *,
        corpus_id: str = "",
        relative_path: str = "",
        last_modified: int = 0,
        expected_sha256: str = "",
    ) -> Dict[str, Any]:
        declared_type = (content_type or "").split(";", 1)[0].strip().lower()
        if not data or (self.max_bytes and len(data) > self.max_bytes):
            maximum = self.max_bytes if self.max_bytes else "the available infrastructure capacity"
            raise ValueError(f"Upload must contain between 1 byte and {maximum}.")
        if declared_type == "image/png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Invalid PNG data.")
        if declared_type == "image/jpeg" and not data.startswith(b"\xff\xd8"):
            raise ValueError("Invalid JPEG data.")
        if declared_type == "image/webp" and not (data.startswith(b"RIFF") and data[8:12] == b"WEBP"):
            raise ValueError("Invalid WebP data.")
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256.lower()):
            raise ValueError("Invalid expected SHA-256 digest.")
        if expected_sha256 and digest != expected_sha256.lower():
            raise ValueError("Uploaded content does not match its expected SHA-256 digest.")
        safe_relative = self._safe_relative_path(relative_path) if corpus_id else ""
        source_name = safe_relative or self._safe_name(filename)
        with self._lock:
            existing_id = self._digest_artifacts.get((digest, source_name))
            if existing_id:
                try:
                    metadata = self.get(existing_id)
                except (OSError, ValueError, FileNotFoundError, KeyError, json.JSONDecodeError):
                    self._digest_artifacts.pop((digest, source_name), None)
                else:
                    if corpus_id:
                        memberships = list(metadata.get("corpus_memberships") or [])
                        membership = {"corpus_id": corpus_id, "relative_path": safe_relative}
                        if membership not in memberships:
                            memberships.append(membership)
                            metadata["corpus_memberships"] = memberships
                            write_text_atomic(self.root / f"{existing_id}.json", json.dumps(metadata, ensure_ascii=False))
                        self._record_corpus_entry(corpus_id, safe_relative, metadata, last_modified=last_modified)
                    return {**metadata, "deduplicated": True}
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
                "sha256": digest,
                "source_name": source_name,
                "created_at": time.time(),
            }
            if corpus_id:
                metadata["corpus_memberships"] = [
                    {"corpus_id": corpus_id, "relative_path": safe_relative}
                ]
            if declared_type in self.IMAGE_TYPES:
                pass
            else:
                document = parse_document(
                    path,
                    supplied_content_type=declared_type or None,
                ).with_filename(source_name)
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
            with self._lock:
                self._digest_artifacts[(digest, source_name)] = artifact_id
                if corpus_id:
                    self._record_corpus_entry(
                        corpus_id, safe_relative, metadata, last_modified=last_modified
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
        self._digest_artifacts.pop(
            (
                str(metadata.get("sha256") or ""),
                str(metadata.get("source_name") or metadata.get("filename") or ""),
            ),
            None,
        )
        for corpus in self.list_corpora():
            entries = {
                path: value
                for path, value in dict(corpus.get("entries") or {}).items()
                if value.get("artifact_id") != artifact_id
            }
            if len(entries) != len(corpus.get("entries") or {}):
                corpus["entries"] = entries
                corpus["state"] = "partial"
                corpus["updated_at"] = time.time()
                write_text_atomic(
                    self._corpus_path(str(corpus["id"])),
                    json.dumps(corpus, ensure_ascii=False),
                )
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
