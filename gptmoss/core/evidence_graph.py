"""Bounded corpus evidence graph derived from tool histories."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping

from gptmoss.core.corpus_policy import normalize_corpus_policy


MAX_EVIDENCE_NODES = 500


def _parse_result(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {"text": text[:500]}
    return parsed if isinstance(parsed, Mapping) else {"value": parsed}


def _source_key(artifact_id: str = "", source_name: str = "", digest: str = "") -> str:
    if digest:
        return f"sha256:{digest}"
    if artifact_id:
        return f"artifact:{artifact_id}"
    if source_name:
        return f"source:{source_name.replace(chr(92), '/')}"
    return ""


def build_evidence_graph(
    plan: Mapping[str, Any] | None,
    histories: Iterable[Mapping[str, Any]],
    *,
    corpus_policy: Mapping[str, Any] | None = None,
    max_nodes: int = MAX_EVIDENCE_NODES,
) -> Dict[str, Any]:
    """Project inventories, reads and citations into a compact graph."""
    policy = normalize_corpus_policy(corpus_policy or (plan or {}).get("corpus_policy"))
    nodes: Dict[str, Dict[str, Any]] = {}
    aliases: Dict[str, str] = {}
    edges: List[Dict[str, str]] = []
    truncated = False

    def add_node(node_id: str, kind: str, **fields: Any) -> bool:
        nonlocal truncated
        if not node_id:
            return False
        artifact_id = str(fields.get("artifact_id") or "")
        digest = str(fields.get("sha256") or "")
        if artifact_id and artifact_id in aliases:
            node_id = aliases[artifact_id]
        if digest and f"sha256:{digest}" in nodes:
            node_id = f"sha256:{digest}"
        if node_id in nodes:
            nodes[node_id].update({key: value for key, value in fields.items() if value})
            if artifact_id:
                aliases[artifact_id] = node_id
            return True
        if len(nodes) >= max(1, int(max_nodes)):
            truncated = True
            return False
        nodes[node_id] = {"id": node_id, "kind": kind, **fields}
        if artifact_id:
            aliases[artifact_id] = node_id
        return True

    def add_edge(kind: str, source: str, target: str) -> None:
        if not source or not target or source == target:
            return
        item = {"type": kind, "from": source, "to": target}
        if item not in edges:
            edges.append(item)

    for entry in histories:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("capability") or "") != "documents":
            continue
        action = str(entry.get("action") or "")
        payload = _parse_result(entry.get("result"))
        arguments = entry.get("arguments") if isinstance(entry.get("arguments"), Mapping) else {}
        if action == "inventory":
            for bucket, kind in (("documents", "source"), ("images", "image")):
                for item in payload.get(bucket) or []:
                    if not isinstance(item, Mapping):
                        continue
                    key = _source_key(
                        str(item.get("id") or item.get("artifact_id") or ""),
                        str(item.get("source_name") or item.get("filename") or ""),
                        str(item.get("sha256") or ""),
                    )
                    if add_node(
                        key, kind,
                        artifact_id=item.get("id") or item.get("artifact_id"),
                        source_name=item.get("source_name") or item.get("filename"),
                        sha256=item.get("sha256"),
                    ):
                        add_edge("inventories", "inventory", key)
            add_node("inventory", "inventory")
        elif action in {"read", "read_chunk"}:
            artifact_id = str(
                payload.get("artifact_id") or arguments.get("artifact_id") or ""
            )
            source_name = str(payload.get("source_name") or "")
            key = aliases.get(artifact_id) or _source_key(artifact_id, source_name)
            add_node(key, "source", artifact_id=artifact_id, source_name=source_name)
            key = aliases.get(artifact_id) or key
            for block in payload.get("blocks") or []:
                if not isinstance(block, Mapping):
                    continue
                order = block.get("order")
                block_id = f"{key}:block:{order}"
                if add_node(block_id, "block_range", order=order, artifact_id=artifact_id):
                    add_edge("covers", key, block_id)
            if not payload.get("blocks") and key:
                add_edge("covers", key, key)
        elif action in {"read_image", "read_images"}:
            items = payload.get("images") or payload.get("documents") or [payload]
            if isinstance(payload.get("artifact_id"), str):
                items = [payload]
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                artifact_id = str(item.get("artifact_id") or item.get("id") or arguments.get("artifact_id") or "")
                key = _source_key(artifact_id, str(item.get("source_name") or ""), str(item.get("sha256") or ""))
                if add_node(key, "image", artifact_id=artifact_id):
                    add_edge("covers", key, key)

    return {
        "schema_version": 1,
        "enabled": bool(policy.get("enabled")),
        "truncated": truncated,
        "nodes": list(nodes.values()),
        "edges": edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "covered_sources": len({
                edge["from"] for edge in edges if edge["type"] == "covers"
            }),
        },
    }
