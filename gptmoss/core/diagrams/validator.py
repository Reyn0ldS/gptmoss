"""Semantic and visual-safety checks for canonical diagrams."""

from __future__ import annotations

from typing import Any

from gptmoss.core.diagrams.model import DiagramSpec


ALLOWED_MERMAID_TYPES = "flowchart, sequenceDiagram, stateDiagram-v2, pie"


def validate_diagram(spec: DiagramSpec, *, max_nodes: int = 80, max_edges: int = 160) -> dict[str, Any]:
    issues: list[str] = []
    node_ids = [node.node_id for node in spec.nodes]
    node_set = set(node_ids)
    if spec.kind == "unsupported":
        named = spec.unsupported_type or "unknown"
        issues.append(
            f"unsupported mermaid diagram type {named!r}; allowed: {ALLOWED_MERMAID_TYPES}"
        )
        return {
            "valid": False,
            "issues": issues,
            "node_count": len(spec.nodes),
            "edge_count": len(spec.edges),
            "density": 0,
        }
    if not spec.diagram_id.strip():
        issues.append("diagram_id is required")
    if not spec.title.strip() or not spec.caption.strip() or not spec.alt_text.strip():
        issues.append("title, caption and alt_text are required")
    if spec.kind == "pie":
        if len(spec.nodes) < 2:
            issues.append("pie diagram must contain at least two slices")
        if len(spec.nodes) > max_nodes:
            issues.append(f"diagram has {len(spec.nodes)} slices; maximum is {max_nodes}")
        if any(not node.label.strip() for node in spec.nodes):
            issues.append("pie slice labels are required")
        if any(node.value is None or float(node.value) < 0 for node in spec.nodes):
            issues.append("pie slice values must be non-negative numbers")
        elif sum(float(node.value or 0) for node in spec.nodes) <= 0:
            issues.append("pie diagram values must sum to more than zero")
        if spec.width <= 0 or spec.height <= 0:
            issues.append("diagram dimensions must be positive")
        return {
            "valid": not issues,
            "issues": issues,
            "node_count": len(spec.nodes),
            "edge_count": 0,
            "density": 0,
        }
    if not spec.nodes:
        issues.append("diagram must contain at least one node")
    elif len(spec.nodes) < 2:
        issues.append("diagram must contain at least two nodes to communicate a relationship")
    if not spec.edges:
        issues.append("diagram must contain at least one relationship")
    if any(not node.node_id.strip() or not node.label.strip() for node in spec.nodes):
        issues.append("node identifiers and labels are required")
    if len(node_ids) != len(node_set):
        issues.append("node identifiers must be unique")
    if len(spec.nodes) > max_nodes:
        issues.append(f"diagram has {len(spec.nodes)} nodes; maximum is {max_nodes}")
    if len(spec.edges) > max_edges:
        issues.append(f"diagram has {len(spec.edges)} edges; maximum is {max_edges}")
    if spec.width <= 0 or spec.height <= 0:
        issues.append("diagram dimensions must be positive")
    for edge in spec.edges:
        if edge.source not in node_set or edge.target not in node_set:
            issues.append(f"edge references missing node: {edge.source}->{edge.target}")
        if edge.source == edge.target:
            issues.append(f"self-loop is not allowed: {edge.source}")
    zones = {node.node_id: node.zone for node in spec.nodes}
    for edge in spec.edges:
        if zones.get(edge.source) and zones.get(edge.target) and zones[edge.source] != zones[edge.target] and not edge.trust_boundary:
            issues.append(f"cross-zone edge must declare trust_boundary: {edge.source}->{edge.target}")
    density = len(spec.edges) / max(1, len(spec.nodes))
    if density > 8:
        issues.append("edge density is too high for a readable document figure")
    return {
        "valid": not issues,
        "issues": issues,
        "node_count": len(spec.nodes),
        "edge_count": len(spec.edges),
        "density": round(density, 3),
    }
