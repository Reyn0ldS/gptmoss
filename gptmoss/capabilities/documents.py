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

    def _resolve_attached(
        self,
        reference: str,
        context: Optional[Dict[str, Any]],
    ) -> str:
        """Resolve an ID, document digest, or filename within the attached scope only."""
        attached = self._attached_ids(context)
        candidate = str(reference or "").strip()
        if candidate in attached:
            return candidate
        matches = []
        for item in self.artifact_store.document_index.inventory():
            if item.get("artifact_id") not in attached:
                continue
            aliases = {
                str(item.get("document_id") or "").casefold(),
                str(item.get("filename") or "").casefold(),
            }
            if candidate.casefold() in aliases:
                matches.append(str(item["artifact_id"]))
        if len(matches) == 1:
            return matches[0]
        available = [
            f"{item.get('filename')} ({item.get('artifact_id')})"
            for item in self.artifact_store.document_index.inventory()
            if item.get("artifact_id") in attached
        ]
        detail = "; ".join(available) or "none"
        raise PermissionError(
            "Document reference is not attached or is not unambiguous. "
            f"Use documents.inventory and retry with a filename or artifact_id. Attached: {detail}."
        )

    def _inventory_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Describe tool offsets and human-facing citation bounds separately."""
        document = self.artifact_store.document(str(item["artifact_id"]))
        block_count = len(document.blocks)
        slide_numbers = {
            block.provenance.slide_number
            for block in document.blocks
            if block.provenance.slide_number is not None
        }
        declared_slide_count = document.metadata.get("slide_count")
        if isinstance(declared_slide_count, int) and not isinstance(declared_slide_count, bool):
            slide_count = max(0, declared_slide_count)
        elif slide_numbers:
            slide_count = max(slide_numbers)
        else:
            slide_count = None
        if slide_count is not None:
            citation_bounds: Dict[str, Any] = {
                "unit": "slides",
                "first": 1 if slide_count else None,
                "last": slide_count if slide_count else None,
            }
        else:
            citation_bounds = {
                "unit": "blocks",
                "first": 1 if block_count else None,
                "last": block_count if block_count else None,
            }
        result = {
            **item,
            "block_count": block_count,
            "normalized_block_offsets": {
                "unit": "blocks",
                "base": 0,
                "first": 0 if block_count else None,
                "last": block_count - 1 if block_count else None,
                "used_by": "documents.read start_block",
            },
            "citation_bounds": citation_bounds,
            "read_reference": item["artifact_id"],
            "read_hint": (
                "Pass artifact_id, filename, or document_id to documents.read; "
                "artifact_id is preferred. start_block is a zero-based normalized-block "
                "offset; citations are one-based and PPTX citations use slide_number."
            ),
        }
        if slide_count is not None:
            result["slide_count"] = slide_count
        return result

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
            self._inventory_item(item)
            for item in self.artifact_store.document_index.inventory()
            if item["artifact_id"] in attached
        ]
        return self._json(
            {
                "documents": items,
                "count": len(items),
                "scope": "explicitly attached local files",
                "addressing_convention": (
                    "documents.read start_block uses zero-based normalized-block offsets. "
                    "Local citations use one-based bounds from citation_bounds. PPTX "
                    "citations use slide_number, never normalized block count."
                ),
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
            selected = [self._resolve_attached(requested, context)]
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
        resolved_id = self._resolve_attached(artifact_id, context)
        document = self.artifact_store.document(resolved_id)
        start = max(0, int(start_block))
        count = max(1, min(int(block_count), 200))
        selected = document.blocks[start : start + count]
        next_start = start + len(selected)
        return self._json(
            {
                "artifact_id": resolved_id,
                "requested_reference": artifact_id,
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
        self._resolve_attached(chunk.artifact_id, context)
        return self._json(chunk.to_dict())
