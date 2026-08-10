"""Read-only tools for locally attached normalized documents."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from gptmoss.core.artifacts import ArtifactStore
from gptmoss.interfaces.capability import action, capability


@capability(
    name="documents",
    description="Inventory, search, and read locally attached documents with provenance.",
)
class DocumentCapability:
    """Read-only access to the document corpus selected for an execution."""

    def __init__(self, artifact_store: ArtifactStore):
        self.artifact_store = artifact_store

    def update_store(self, artifact_store: ArtifactStore) -> None:
        self.artifact_store = artifact_store

    @staticmethod
    def _attached_ids(context: Optional[Dict[str, Any]]) -> set[str]:
        variables = (context or {}).get("variables")
        if not isinstance(variables, dict):
            return set()
        values = variables.get("attachment_ids")
        if not isinstance(values, list):
            return set()
        return {str(value) for value in values if value}

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _require_attached(
        self,
        artifact_id: str,
        context: Optional[Dict[str, Any]],
    ) -> None:
        if artifact_id not in self._attached_ids(context):
            raise PermissionError(
                "Document is not attached to this execution. Attach it explicitly first."
            )

    @action(
        name="inventory",
        description=(
            "List the local documents explicitly attached to this execution, including "
            "their identifiers, formats, block counts, chunk counts, and parser versions."
        ),
    )
    def inventory(self, context: Optional[Dict[str, Any]] = None) -> str:
        attached = self._attached_ids(context)
        items = [
            item
            for item in self.artifact_store.document_index.inventory()
            if item["artifact_id"] in attached
        ]
        return self._json(
            {
                "documents": items,
                "count": len(items),
                "scope": "explicitly attached local files",
            }
        )

    @action(
        name="search",
        description=(
            "Search every part of the explicitly attached local documents. The result "
            "contains ranked chunks, headings, block ranges, source provenance, and text. "
            "Use read or read_chunk when more surrounding content is required."
        ),
    )
    def search(
        self,
        query: str,
        limit: int = 8,
        artifact_id: str = "",
        heading: str = "",
        kind: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        attached = self._attached_ids(context)
        if not attached:
            return self._json(
                {
                    "query": query,
                    "results": [],
                    "error": "No local documents are attached to this execution.",
                }
            )
        requested = artifact_id.strip()
        if requested:
            self._require_attached(requested, context)
            selected = [requested]
        else:
            selected = sorted(attached)
        effective_limit = max(1, min(int(limit), 40))
        results = self.artifact_store.search_documents(
            query,
            limit=effective_limit,
            artifact_ids=selected,
            heading=heading.strip() or None,
            kinds=[kind.strip()] if kind.strip() else None,
        )
        budget = max(2_000, int((context or {}).get("context_budget_chars") or 12_000))
        compacted: list[dict[str, Any]] = []
        used = 0
        for result in results:
            item = dict(result)
            text = str(item.get("text") or "")
            allowance = max(600, min(2_400, budget - used))
            if len(text) > allowance:
                item["text"] = text[:allowance].rstrip()
                item["text_truncated"] = True
                item["read_chunk_id"] = item["id"]
            encoded_size = len(self._json(item))
            if compacted and used + encoded_size > budget:
                break
            compacted.append(item)
            used += encoded_size
        return self._json(
            {
                "query": query,
                "results": compacted,
                "result_count": len(compacted),
                "available_result_count": len(results),
                "scope_artifact_ids": selected,
            }
        )

    @action(
        name="read",
        description=(
            "Read an ordered page of normalized blocks from one explicitly attached local "
            "document. Use next_start until has_more is false; no omitted blocks are hidden."
        ),
    )
    def read(
        self,
        artifact_id: str,
        start_block: int = 0,
        block_count: int = 20,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        self._require_attached(artifact_id, context)
        document = self.artifact_store.document(artifact_id)
        start = max(0, int(start_block))
        count = max(1, min(int(block_count), 200))
        selected = document.blocks[start : start + count]
        next_start = start + len(selected)
        return self._json(
            {
                "artifact_id": artifact_id,
                "document_id": document.id,
                "filename": document.filename,
                "title": document.title,
                "content_type": document.content_type,
                "start_block": start,
                "returned_blocks": len(selected),
                "total_blocks": len(document.blocks),
                "has_more": next_start < len(document.blocks),
                "next_start": next_start if next_start < len(document.blocks) else None,
                "blocks": [block.to_dict() for block in selected],
            }
        )

    @action(
        name="read_chunk",
        description=(
            "Read one complete search chunk by its chunk identifier, with headings, block "
            "range, and local source provenance. The chunk must belong to an attached file."
        ),
    )
    def read_chunk(
        self,
        chunk_id: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> str:
        chunk = self.artifact_store.document_index.get_chunk(chunk_id)
        self._require_attached(chunk.artifact_id, context)
        return self._json(chunk.to_dict())
