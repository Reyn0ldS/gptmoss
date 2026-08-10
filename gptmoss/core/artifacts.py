"""Safe storage and context preparation for user-provided files and images."""

import base64
import hashlib
import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

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
        path.write_bytes(data)
        document_path: Path | None = None
        metadata_path = self.root / f"{artifact_id}.json"
        try:
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
                document_path.write_text(document.to_json(), encoding="utf-8")
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
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False),
                encoding="utf-8",
            )
            return metadata
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            if document_path is not None:
                document_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            raise

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

    @staticmethod
    def _context_text(text: str, limit: int) -> tuple[str, bool]:
        if not limit or len(text) <= limit:
            return text, False
        notice = "\n… [middle of attachment compacted adaptively] …\n"
        available = max(0, limit - len(notice))
        head = (available * 2) // 3
        tail = available - head
        return text[:head] + notice + (text[-tail:] if tail else ""), True

    def context_items(
        self,
        artifact_ids: List[str],
        supports_vision: bool = False,
        max_text_chars: int | None = None,
    ) -> List[Dict[str, Any]]:
        items = []
        text_limit = self.max_text_chars if max_text_chars is None else max(0, int(max_text_chars))
        for artifact_id in artifact_ids:
            metadata = self.get(artifact_id)
            path = Path(metadata["path"])
            item = {key: metadata[key] for key in ("id", "filename", "content_type", "size_bytes", "sha256")}
            if metadata["content_type"] in self.DOCUMENT_TYPES:
                document = self.document(artifact_id)
                full_text = document.to_markdown()
                item["text"], item["text_compacted"] = self._context_text(full_text, text_limit)
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
