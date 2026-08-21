"""Deterministic SVG renderer; no browser or ML dependency is required."""

from __future__ import annotations

import html
import math

from gptmoss.core.diagrams.model import DiagramSpec


def render_svg(spec: DiagramSpec) -> str:
    if spec.kind == "pie":
        return _render_pie(spec)
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


def _render_pie(spec: DiagramSpec) -> str:
    slices = [node for node in spec.nodes if (node.value or 0) > 0]
    total = sum(float(node.value or 0) for node in slices) or 1.0
    width = max(spec.width, 720)
    height = max(spec.height, 420)
    cx, cy, radius = 220.0, 230.0, 140.0
    colors = ["#0369a1", "#0f766e", "#b45309", "#7c3aed", "#be123c", "#15803d"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(spec.alt_text)}">',
        f'<rect width="100%" height="100%" fill="#f8fafc"/><text x="40" y="32" font-family="Arial" font-size="22" font-weight="bold" fill="#0f172a">{html.escape(spec.title)}</text>',
    ]
    angle = -math.pi / 2
    for index, node in enumerate(slices):
        sweep = 2 * math.pi * (float(node.value or 0) / total)
        color = colors[index % len(colors)]
        if len(slices) == 1 or sweep >= 2 * math.pi - 1e-9:
            parts.append(
                f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{radius}" fill="{color}" '
                f'stroke="#f8fafc" stroke-width="2"/>'
            )
        else:
            next_angle = angle + sweep
            x1 = cx + radius * math.cos(angle)
            y1 = cy + radius * math.sin(angle)
            x2 = cx + radius * math.cos(next_angle)
            y2 = cy + radius * math.sin(next_angle)
            large = 1 if sweep > math.pi else 0
            parts.append(
                f'<path d="M {cx:.1f} {cy:.1f} L {x1:.1f} {y1:.1f} A {radius} {radius} 0 {large} 1 {x2:.1f} {y2:.1f} Z" '
                f'fill="{color}" stroke="#f8fafc" stroke-width="2"/>'
            )
            angle = next_angle
        parts.append(
            f'<rect x="420" y="{80 + index * 28}" width="14" height="14" fill="{color}"/>'
            f'<text x="442" y="{92 + index * 28}" font-family="Arial" font-size="14" fill="#0f172a">'
            f'{html.escape(node.label[:48])} ({float(node.value or 0):g})</text>'
        )
    parts.append(
        f'<text x="40" y="{height - 24}" font-family="Arial" font-size="12" fill="#475569">{html.escape(spec.caption)}</text></svg>'
    )
    return "".join(parts)
