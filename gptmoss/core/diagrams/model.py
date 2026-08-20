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
    """Parse the bounded flowchart, sequence and state subsets emitted by GPTMOSS."""
    direction = "TB"
    nodes: dict[str, DiagramNode] = {}
    edges: list[DiagramEdge] = []
    kind = "flowchart"
    start_counter = 0
    end_counter = 0

    def state_id(raw: str, *, source_side: bool) -> str:
        nonlocal start_counter, end_counter
        value = raw.strip()
        if value != "[*]":
            return value
        if source_side:
            start_counter += 1
            return f"START_{start_counter}"
        end_counter += 1
        return f"END_{end_counter}"

    for raw in str(source or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        title_match = re.match(r"(?i)^%%\s*title\s*:\s*(.+)$", line)
        if title_match:
            title = title_match.group(1).strip() or title
            continue
        if line.startswith("%%"):
            continue
        header = re.match(r"(?i)^(?:graph|flowchart)\s+(TB|TD|LR|RL|BT)\b", line)
        if header:
            kind = "flowchart"
            direction = "TB" if header.group(1).upper() == "TD" else header.group(1).upper()
            continue
        if re.match(r"(?i)^sequenceDiagram\b", line):
            kind = "sequence"
            direction = "LR"
            continue
        if re.match(r"(?i)^stateDiagram(?:-v2)?\b", line):
            kind = "state"
            direction = "TB"
            continue
        if kind == "sequence":
            participant = re.match(
                r"(?i)^(?:participant|actor)\s+([A-Za-z0-9_-]+)(?:\s+as\s+(.+))?$",
                line,
            )
            if participant:
                node_id = participant.group(1)
                nodes[node_id] = DiagramNode(
                    node_id, (participant.group(2) or node_id).strip(), kind="participant"
                )
                continue
            message = re.match(
                r"^([A-Za-z0-9_][A-Za-z0-9_-]*?)\s*"
                r"(?:-->>|->>|-->|->|--x|-x|--\)|-\))\s*"
                r"([A-Za-z0-9_][A-Za-z0-9_-]*)\s*:\s*(.+)$",
                line,
            )
            if message:
                source_id, target_id, label = message.groups()
                nodes.setdefault(source_id, DiagramNode(source_id, source_id, kind="participant"))
                nodes.setdefault(target_id, DiagramNode(target_id, target_id, kind="participant"))
                edges.append(DiagramEdge(source_id, target_id, label.strip(), relation="message"))
                continue
        if kind == "state":
            declaration = re.match(
                r'(?i)^state\s+(?:"([^"]+)"\s+as\s+)?([A-Za-z0-9_-]+)$', line
            )
            if declaration:
                label, node_id = declaration.groups()
                nodes[node_id] = DiagramNode(node_id, (label or node_id).strip(), kind="state")
                continue
            transition = re.match(
                r"^(\[\*\]|[A-Za-z0-9_-]+)\s*--+>\s*"
                r"(\[\*\]|[A-Za-z0-9_-]+)(?:\s*:\s*(.+))?$",
                line,
            )
            if transition:
                raw_source, raw_target, label = transition.groups()
                source_id = state_id(raw_source, source_side=True)
                target_id = state_id(raw_target, source_side=False)
                nodes.setdefault(source_id, DiagramNode(source_id, "Start", kind="state"))
                nodes.setdefault(target_id, DiagramNode(target_id, "End", kind="state"))
                if raw_source != "[*]":
                    nodes[source_id] = DiagramNode(source_id, source_id, kind="state")
                if raw_target != "[*]":
                    nodes[target_id] = DiagramNode(target_id, target_id, kind="state")
                edges.append(DiagramEdge(source_id, target_id, (label or "").strip(), relation="transition"))
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
