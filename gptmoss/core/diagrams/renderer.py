"""Deterministic SVG renderer; no browser or ML dependency is required."""

from __future__ import annotations

import html
import math

from gptmoss.core.diagrams.model import DiagramSpec


def render_svg(spec: DiagramSpec) -> str:
    columns = max(1, math.ceil(math.sqrt(max(1, len(spec.nodes)))))
    cell_w, cell_h = 240, 130
    positions = {}
    for index, node in enumerate(spec.nodes):
        x = node.x if node.x is not None else 40 + (index % columns) * cell_w
        y = node.y if node.y is not None else 70 + (index // columns) * cell_h
        positions[node.node_id] = (x, y)
    width = max(spec.width, 80 + columns * cell_w)
    rows = max(1, math.ceil(len(spec.nodes) / columns))
    height = max(spec.height, 130 + rows * cell_h)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(spec.alt_text)}">',
        '<defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L9,3 z" fill="#334155"/></marker></defs>',
        f'<rect width="100%" height="100%" fill="#f8fafc"/><text x="40" y="32" font-family="Arial" font-size="22" font-weight="bold" fill="#0f172a">{html.escape(spec.title)}</text>',
    ]
    for edge in spec.edges:
        if edge.source not in positions or edge.target not in positions:
            continue
        x1, y1 = positions[edge.source]
        x2, y2 = positions[edge.target]
        out.append(f'<line x1="{x1 + 90}" y1="{y1 + 35}" x2="{x2 + 90}" y2="{y2 + 35}" stroke="#64748b" stroke-width="2" marker-end="url(#arrow)"/>')
        if edge.label:
            out.append(f'<text x="{(x1 + x2) / 2 + 90:.1f}" y="{(y1 + y2) / 2 + 28:.1f}" font-family="Arial" font-size="13" fill="#334155">{html.escape(edge.label)}</text>')
    for node in spec.nodes:
        x, y = positions[node.node_id]
        out.append(f'<rect x="{x}" y="{y}" width="180" height="70" rx="10" fill="#e0f2fe" stroke="#0369a1" stroke-width="2"/>')
        out.append(f'<text x="{x + 90}" y="{y + 31}" text-anchor="middle" font-family="Arial" font-size="15" fill="#0c4a6e">{html.escape(node.label[:48])}</text>')
        if node.zone:
            out.append(f'<text x="{x + 90}" y="{y + 53}" text-anchor="middle" font-family="Arial" font-size="11" fill="#475569">{html.escape(node.zone[:36])}</text>')
    out.append(f'<text x="40" y="{height - 24}" font-family="Arial" font-size="12" fill="#475569">{html.escape(spec.caption)}</text></svg>')
    return "".join(out)
