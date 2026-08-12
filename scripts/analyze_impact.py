"""Query the GPTMOSS relational symbol map before changing code."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MAP = ROOT / "docs" / "symbol-map.json"
DEPENDENCY_KINDS = {"calls", "imports", "inherits", "composes", "uses_type", "typed_as"}
DATA_KINDS = {"reads", "writes", "accesses", "owns", "defines"}
SURFACE_KINDS = {"exposes", "publishes"}
REVERSE_IMPACT_KINDS = DEPENDENCY_KINDS | {"reads", "writes", "accesses"}


class AmbiguousQuery(ValueError):
    def __init__(self, query: str, matches: list[str]) -> None:
        super().__init__(f"Ambiguous symbol query {query!r}")
        self.query = query
        self.matches = matches


class SymbolGraph:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.nodes = {node["id"]: node for node in payload.get("nodes", [])}
        self.outgoing: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for edge in payload.get("edges", []):
            self.outgoing[edge["source"]].append(edge)
            self.incoming[edge["target"]].append(edge)

    @classmethod
    def load(cls, path: Path = DEFAULT_MAP) -> "SymbolGraph":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def resolve(self, query: str) -> str:
        query = query.strip()
        if query in self.nodes:
            return query
        normalized = query.replace("/", ".").removesuffix(".py")
        exact_suffixes = [
            identifier for identifier, node in self.nodes.items()
            if node.get("qualname") == query
            or identifier.endswith(f":{query}")
            or (node.get("path") == query)
            or (node.get("module") == normalized)
        ]
        if len(exact_suffixes) == 1:
            return exact_suffixes[0]
        candidates = sorted(
            identifier for identifier, node in self.nodes.items()
            if query.lower() in identifier.lower()
            or query.lower() in str(node.get("name", "")).lower()
            or query.lower() in str(node.get("path", "")).lower()
        )
        if len(candidates) == 1:
            return candidates[0]
        if not candidates and not exact_suffixes:
            raise KeyError(query)
        raise AmbiguousQuery(query, sorted(set(exact_suffixes or candidates))[:50])

    def symbols_for_file(self, relative_path: str) -> list[str]:
        normalized = relative_path.replace("\\", "/").lstrip("./")
        return sorted(
            identifier for identifier, node in self.nodes.items()
            if node.get("path") == normalized and node.get("kind") in {"module", "class", "method", "function"}
        )

    def impact(self, identifiers: Iterable[str], depth: int = 2) -> dict[str, Any]:
        selected = sorted(set(identifiers))
        impacted: dict[str, int] = {identifier: 0 for identifier in selected}
        queue = deque((identifier, 0) for identifier in selected)
        reasons: dict[str, set[str]] = defaultdict(set)
        while queue:
            current, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            for edge in self.incoming.get(current, []):
                if edge["kind"] not in REVERSE_IMPACT_KINDS:
                    continue
                source = edge["source"]
                next_depth = current_depth + 1
                reasons[source].add(f"{edge['kind']} {current}")
                if source not in impacted or next_depth < impacted[source]:
                    impacted[source] = next_depth
                    queue.append((source, next_depth))

        direct_edges = [
            edge for identifier in selected
            for edge in [*self.outgoing.get(identifier, []), *self.incoming.get(identifier, [])]
        ]
        related_data = sorted({
            endpoint
            for edge in direct_edges if edge["kind"] in DATA_KINDS
            for endpoint in (edge["source"], edge["target"])
            if self.nodes.get(endpoint, {}).get("kind") == "data"
        })
        surfaces = sorted({
            edge["target"] for identifier in impacted
            for edge in self.outgoing.get(identifier, [])
            if edge["kind"] in SURFACE_KINDS
        })
        tests = sorted(
            identifier for identifier in impacted
            if str(self.nodes.get(identifier, {}).get("path", "")).startswith("tests/")
        )
        production_dependents = sorted(
            (identifier for identifier in impacted if identifier not in selected and identifier not in tests),
            key=lambda item: (impacted[item], item),
        )
        files = sorted({
            str(self.nodes[identifier].get("path"))
            for identifier in impacted
            if self.nodes.get(identifier, {}).get("path")
        })
        domains = sorted({
            str(self.nodes[identifier].get("domain"))
            for identifier in impacted
            if self.nodes.get(identifier, {}).get("domain")
        })
        features = sorted({
            feature for identifier in impacted
            for feature in self.nodes.get(identifier, {}).get("features", [])
        })
        dependencies = sorted({
            edge["target"] for identifier in selected
            for edge in self.outgoing.get(identifier, [])
            if edge["kind"] in DEPENDENCY_KINDS
        })
        return {
            "selected": [self._describe(identifier) for identifier in selected],
            "summary": {
                "depth": depth,
                "impacted_symbols": len(impacted),
                "production_dependents": len(production_dependents),
                "tests": len(tests),
                "files": len(files),
            },
            "public_surfaces": [self._describe(identifier) for identifier in surfaces],
            "structured_data": [self._describe(identifier) for identifier in related_data],
            "dependencies": [self._describe(identifier) for identifier in dependencies],
            "dependents": [
                {**self._describe(identifier), "distance": impacted[identifier], "reasons": sorted(reasons[identifier])}
                for identifier in production_dependents
            ],
            "tests": [
                {**self._describe(identifier), "distance": impacted[identifier], "reasons": sorted(reasons[identifier])}
                for identifier in tests
            ],
            "files": files,
            "domains": domains,
            "features": features,
        }

    def _describe(self, identifier: str) -> dict[str, Any]:
        node = self.nodes.get(identifier, {"id": identifier, "kind": "unknown"})
        return {
            key: node[key] for key in ("id", "kind", "path", "line", "signature", "data_kind", "name")
            if key in node
        }


def _print_items(title: str, items: list[dict[str, Any]], limit: int = 30) -> None:
    print(f"\n{title} ({len(items)})")
    if not items:
        print("  - aucun")
        return
    for item in items[:limit]:
        location = f" — {item['path']}:{item.get('line', 1)}" if item.get("path") else ""
        distance = f" [distance {item['distance']}]" if "distance" in item else ""
        print(f"  - {item['id']}{distance}{location}")
    if len(items) > limit:
        print(f"  … {len(items) - limit} autre(s), utiliser --json pour la liste complète")


def print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print("Impact GPTMOSS")
    print(
        f"  {summary['impacted_symbols']} symbole(s), {summary['files']} fichier(s), "
        f"{summary['tests']} test(s), profondeur {summary['depth']}"
    )
    _print_items("Symboles sélectionnés", report["selected"])
    _print_items("Surfaces publiques", report["public_surfaces"])
    _print_items("Données structurantes", report["structured_data"])
    _print_items("Dépendances directes", report["dependencies"])
    _print_items("Appelants et consommateurs", report["dependents"])
    _print_items("Tests concernés", report["tests"])
    print(f"\nFichiers ({len(report['files'])})")
    for path in report["files"]:
        print(f"  - {path}")
    if report["domains"]:
        print("\nDomaines : " + ", ".join(report["domains"]))
    if report["features"]:
        print("Fonctionnalités : " + ", ".join(report["features"]))


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show callers, data, public surfaces and tests affected by a GPTMOSS change.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("symbol", nargs="?", help="Stable ID, qualified name or unambiguous symbol fragment.")
    target.add_argument("--file", dest="file_path", help="Analyze every symbol declared in a repository-relative Python file.")
    parser.add_argument("--depth", type=int, default=2, choices=range(1, 6))
    parser.add_argument("--json", action="store_true", help="Emit the complete machine-readable report.")
    parser.add_argument("--map", dest="map_path", type=Path, default=DEFAULT_MAP)
    args = parser.parse_args(list(argv) if argv is not None else None)
    map_path = args.map_path if args.map_path.is_absolute() else ROOT / args.map_path
    try:
        graph = SymbolGraph.load(map_path)
        if args.file_path:
            identifiers = graph.symbols_for_file(args.file_path)
            if not identifiers:
                raise KeyError(args.file_path)
        else:
            identifiers = [graph.resolve(args.symbol)]
        report = graph.impact(identifiers, depth=args.depth)
    except AmbiguousQuery as exc:
        print(f"[FAIL] Requête ambiguë : {exc.query}")
        for match in exc.matches:
            print(f"  - {match}")
        return 2
    except KeyError as exc:
        print(f"[FAIL] Aucun symbole cartographié pour : {exc.args[0]}")
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[FAIL] Impossible de lire la cartographie : {exc}")
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
