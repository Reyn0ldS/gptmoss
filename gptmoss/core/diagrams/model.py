"""Small provider-independent diagram model with a Mermaid-compatible parser."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class DiagramNode:
    node_id: str
    label: str
    kind: str = "component"
    zone: str = ""
    x: int | None = None
    y: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DiagramEdge:
    source: str
    target: str
    label: str = ""
    relation: str = "data"
    trust_boundary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiagramSpec:
    diagram_id: str
    title: str
    caption: str
    alt_text: str
    direction: str = "TB"
    nodes: list[DiagramNode] = field(default_factory=list)
    edges: list[DiagramEdge] = field(default_factory=list)
    width: int = 1200
    height: int = 720

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["nodes"] = [node.to_dict() for node in self.nodes]
        value["edges"] = [edge.to_dict() for edge in self.edges]
        return value


_NODE_RE = re.compile(r"^(?P<id>[A-Za-z0-9_-]+)\s*(?:\[\"?(?P<label>[^\]\"]+)\"?\]|\((?P<round>[^\)]+)\))?$")
_EDGE_RE = re.compile(
    r"^(?P<source>[A-Za-z0-9_-]+)(?:\[\"?(?P<source_label>[^\]\"]+)\"?\])?"
    r"\s*[-.=]+>\s*(?:\|(?P<label>[^|]+)\|\s*)?"
    r"(?P<target>[A-Za-z0-9_-]+)(?:\[\"?(?P<target_label>[^\]\"]+)\"?\])?"
)


def parse_mermaid(source: str, diagram_id: str = "diagram-1", title: str = "Architecture diagram") -> DiagramSpec:
    """Parse the intentionally small flowchart subset emitted by GPTMOSS."""
    direction = "TB"
    nodes: dict[str, DiagramNode] = {}
    edges: list[DiagramEdge] = []
    for raw in str(source or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("%%"):
            continue
        header = re.match(r"(?i)^(?:graph|flowchart)\s+(TB|TD|LR|RL|BT)\b", line)
        if header:
            direction = "TB" if header.group(1).upper() == "TD" else header.group(1).upper()
            continue
        edge = _EDGE_RE.match(line)
        if edge:
            data = edge.groupdict()
            nodes.setdefault(data["source"], DiagramNode(data["source"], (data.get("source_label") or data["source"]).strip()))
            nodes.setdefault(data["target"], DiagramNode(data["target"], (data.get("target_label") or data["target"]).strip()))
            edges.append(DiagramEdge(data["source"], data["target"], (data.get("label") or "").strip()))
            continue
        node = _NODE_RE.match(line)
        if node:
            data = node.groupdict()
            label = (data.get("label") or data.get("round") or data["id"]).strip()
            nodes[data["id"]] = DiagramNode(data["id"], label)
    return DiagramSpec(
        diagram_id=diagram_id,
        title=title,
        caption=title,
        alt_text=f"{title}: {len(nodes)} nodes and {len(edges)} relationships.",
        direction=direction,
        nodes=list(nodes.values()),
        edges=edges,
    )
