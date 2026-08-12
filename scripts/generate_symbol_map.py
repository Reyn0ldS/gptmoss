"""Generate a deterministic relational map of GPTMOSS Python symbols.

The graph intentionally tracks architectural data rather than every local variable:
public symbols, inheritance, internal calls, imported dependencies, API/event surfaces,
configuration keys, execution state keys, model fields and persistent stores.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "symbol-map.json"
APPLICATION_MAP = ROOT / "docs" / "application-map.json"
ROUTE_METHODS = {"get", "post", "put", "patch", "delete", "websocket"}
STATE_MAPPINGS = {"variables": "execution-variable", "results": "execution-result"}
MAPPING_MUTATORS = {"setdefault", "update", "pop", "popitem", "clear"}


def dotted(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def literal_string(node: ast.AST | None) -> str | None:
    return str(node.value) if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or "__root__"


def source_paths(root: Path) -> list[Path]:
    paths = [root / "main.py"]
    for folder in ("gptmoss", "scripts", "tests"):
        paths.extend(sorted((root / folder).rglob("*.py")))
    return sorted({path.resolve() for path in paths if path.is_file()})


def operational_paths(root: Path) -> list[Path]:
    """Return non-Python sources that participate in GUI and distribution flows."""
    paths = [root / "gptmoss" / "api" / "gui.html"]
    for pattern in ("*.bat", "*.sh", "*.ps1"):
        paths.extend(root.glob(pattern))
        paths.extend((root / "scripts").rglob(pattern))
    paths.extend((root / "scripts").rglob("*.py"))
    return sorted({path.resolve() for path in paths if path.is_file()})


def normalize_web_path(value: str) -> str:
    path = value.split("?", 1)[0]
    path = re.sub(r"\$\{([^}]+)\}", lambda match: "{" + match.group(1) + "}", path)
    return path


def route_shape(path: str) -> tuple[str, ...]:
    return tuple("{}" if part.startswith("{") and part.endswith("}") else part
                 for part in path.strip("/").split("/"))


def symbol_id(module: str, qualname: str) -> str:
    return f"{module}:{qualname}"


def module_id(module: str) -> str:
    return f"module:{module}"


def data_id(kind: str, name: str) -> str:
    return f"data:{kind}:{name}"


def walk_scope(root: ast.AST) -> Iterable[ast.AST]:
    """Walk one callable without attributing nested callable bodies to its owner."""
    stack = list(reversed(list(ast.iter_child_nodes(root))))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _annotation_name(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Subscript):
        base = dotted(node.value)
        if base in {"Optional", "typing.Optional", "list", "List", "dict", "Dict", "set", "Set"}:
            return _annotation_name(node.slice)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _annotation_name(node.left)
        return left if left not in {"None", "NoneType"} else _annotation_name(node.right)
    return dotted(node)


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    try:
        arguments = ast.unparse(node.args)
        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    except (AttributeError, ValueError):
        arguments, returns = "...", ""
    return f"{prefix} {node.name}({arguments}){returns}"


def _visibility(name: str) -> str:
    if name.startswith("__") and name.endswith("__"):
        return "dunder"
    return "private" if name.startswith("_") else "public"


@dataclass
class ModuleInfo:
    path: Path
    relative: str
    module: str
    tree: ast.Module
    imports: dict[str, str] = field(default_factory=dict)
    definitions: dict[str, str] = field(default_factory=dict)
    class_methods: dict[str, dict[str, str]] = field(default_factory=dict)
    class_types: dict[str, dict[str, str]] = field(default_factory=dict)


class SymbolGraphBuilder:
    def __init__(self, root: Path = ROOT) -> None:
        self.root = root.resolve()
        self.application_map = json.loads((self.root / "docs" / "application-map.json").read_text(encoding="utf-8"))
        self.modules: dict[str, ModuleInfo] = {}
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: set[tuple[str, str, str, int, str]] = set()
        self.aliases: dict[str, str] = {}
        self.persistence_names = self._persistence_names()

    def _persistence_names(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for location, description in self.application_map.get("persistence", []):
            normalized = str(location).replace("<project>/", "").replace("workspace/", "")
            candidates = {normalized}
            basename = Path(normalized).name
            if "." in basename:
                candidates.add(basename)
            for candidate in candidates:
                if candidate and "<" not in candidate:
                    result[candidate] = str(location)
        return result

    def add_node(self, identifier: str, kind: str, **attributes: Any) -> None:
        compact = {"id": identifier, "kind": kind}
        compact.update({key: value for key, value in attributes.items() if value not in (None, "", [], {})})
        existing = self.nodes.get(identifier)
        if existing:
            existing.update(compact)
        else:
            self.nodes[identifier] = compact

    def add_edge(self, source: str, target: str, kind: str, line: int = 0, confidence: str = "exact") -> None:
        if source != target:
            self.edges.add((source, target, kind, int(line or 0), confidence))

    def load(self) -> None:
        domains = self.application_map.get("modules", {})
        features_by_test: dict[str, list[str]] = {}
        for feature in self.application_map.get("features", []):
            for test in feature.get("tests", []):
                features_by_test.setdefault(test, []).append(feature["id"])
        for path in source_paths(self.root):
            relative = path.relative_to(self.root).as_posix()
            module = module_name(path, self.root)
            info = ModuleInfo(path, relative, module, ast.parse(path.read_text(encoding="utf-8"), filename=relative))
            self.modules[module] = info
            self.add_node(
                module_id(module), "module", module=module, path=relative,
                domain=domains.get(relative, "test" if relative.startswith("tests/") else "operations"),
                features=sorted(features_by_test.get(relative, [])),
            )
        self._index_definitions()
        self._resolve_aliases()
        self._extract_relations()
        self._extract_gui_relations()
        self._extract_script_relations()

    def _extract_gui_relations(self) -> None:
        path = self.root / "gptmoss" / "api" / "gui.html"
        if not path.is_file():
            return
        relative = path.relative_to(self.root).as_posix()
        source = path.read_text(encoding="utf-8")
        functions = list(re.finditer(
            r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", source
        ))
        for index, match in enumerate(functions):
            name = match.group(1)
            identifier = f"gui:{name}"
            line = source.count("\n", 0, match.start()) + 1
            end = functions[index + 1].start() if index + 1 < len(functions) else len(source)
            self.add_node(identifier, "function", name=name, qualname=name, path=relative,
                          module="gptmoss.api.gui", line=line, end_line=source.count("\n", 0, end) + 1,
                          visibility="public")
            self.add_edge("surface:gui", identifier, "contains", line)

        def owner(position: int) -> str:
            preceding = [match for match in functions if match.start() <= position]
            return f"gui:{preceding[-1].group(1)}" if preceding else "surface:gui"

        self.add_node("surface:gui", "surface", data_kind="gui", name="GPTMOSS GUI", path=relative)
        routes = [
            node for node in self.nodes.values()
            if node.get("data_kind") == "api-route"
        ]
        unresolved = []
        for match in re.finditer(r"(?:fetch|requestApi)\(\s*([`\"'])(.*?)\1", source, flags=re.DOTALL):
            raw = match.group(2).strip()
            if not raw.startswith("/") or "${runtimeControl.supervisor_url}" in raw:
                continue
            snippet = source[match.end():match.end() + 500].split(";", 1)[0]
            method_match = re.search(r"\bmethod\s*:\s*[\"']([A-Za-z]+)[\"']", snippet)
            method = (method_match.group(1) if method_match else "GET").upper()
            normalized = normalize_web_path(raw)
            if "{endpoint}" in normalized or "{action}" in normalized:
                target = data_id("api-call", f"{method} {normalized}")
                self.add_node(target, "surface", data_kind="dynamic-api-call",
                              name=f"{method} {normalized}", path=relative)
                self.add_edge(owner(match.start()), target, "calls_dynamic_api",
                              source.count("\n", 0, match.start()) + 1, "dynamic")
                continue
            shape = route_shape(normalized)
            candidates = [route for route in routes
                          if route.get("name", "").startswith(method + " ")
                          and route_shape(route["name"].split(" ", 1)[1]) == shape]
            if candidates:
                self.add_edge(owner(match.start()), candidates[0]["id"], "calls_api",
                              source.count("\n", 0, match.start()) + 1)
            else:
                unresolved.append({"method": method, "path": normalized,
                                   "line": source.count("\n", 0, match.start()) + 1})
                target = data_id("unresolved-api-call", f"{method} {normalized}")
                self.add_node(target, "surface", data_kind="unresolved-api-call",
                              name=f"{method} {normalized}", path=relative)
                self.add_edge(owner(match.start()), target, "calls_missing_api",
                              source.count("\n", 0, match.start()) + 1)
        self.gui_unresolved = unresolved

        for match in re.finditer(r"new\s+WebSocket\(\s*`[^`]*?(/ws/[^`]*)`", source):
            normalized = normalize_web_path(match.group(1))
            shape = route_shape(normalized)
            candidate = next((route for route in routes
                              if route.get("name", "").startswith("WEBSOCKET ")
                              and route_shape(route["name"].split(" ", 1)[1]) == shape), None)
            if candidate:
                self.add_edge(owner(match.start()), candidate["id"], "opens_websocket",
                              source.count("\n", 0, match.start()) + 1)

        known_functions = {match.group(1) for match in functions}
        for match in re.finditer(r"\bonclick\s*=\s*[\"']([A-Za-z_$][\w$]*)\s*\(", source):
            element_id = f"gui-element:onclick:{source.count(chr(10), 0, match.start()) + 1}"
            self.add_node(element_id, "surface", data_kind="gui-control",
                          name=f"onclick {match.group(1)}", path=relative,
                          line=source.count("\n", 0, match.start()) + 1)
            if match.group(1) in known_functions:
                self.add_edge(element_id, f"gui:{match.group(1)}", "triggers",
                              source.count("\n", 0, match.start()) + 1)

    def _extract_script_relations(self) -> None:
        paths = operational_paths(self.root)
        by_name = {path.name.lower(): path for path in paths}
        for path in paths:
            if path.suffix.lower() == ".html":
                continue
            relative = path.relative_to(self.root).as_posix()
            identifier = f"script:{relative}"
            self.add_node(identifier, "surface", data_kind="operational-script",
                          name=path.name, path=relative)
        for path in paths:
            if path.suffix.lower() == ".html":
                continue
            relative = path.relative_to(self.root).as_posix()
            content = path.read_text(encoding="utf-8", errors="replace").lower()
            for name, target_path in by_name.items():
                if target_path == path or name not in content:
                    continue
                target_relative = target_path.relative_to(self.root).as_posix()
                if target_path.suffix.lower() != ".html":
                    self.add_edge(f"script:{relative}", f"script:{target_relative}",
                                  "invokes_script", 0, "literal")

    def _index_definitions(self) -> None:
        for info in self.modules.values():
            owner = module_id(info.module)
            for node in info.tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    self._record_imports(info, node)
                elif isinstance(node, ast.ClassDef):
                    identifier = symbol_id(info.module, node.name)
                    info.definitions[node.name] = identifier
                    info.class_methods[node.name] = {}
                    self._add_symbol_node(info, node, identifier, "class", node.name)
                    self.add_edge(owner, identifier, "contains", node.lineno)
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            method_identifier = symbol_id(info.module, f"{node.name}.{child.name}")
                            info.class_methods[node.name][child.name] = method_identifier
                            self._add_symbol_node(info, child, method_identifier, "method", f"{node.name}.{child.name}", owner=identifier)
                            self.add_edge(identifier, method_identifier, "contains", child.lineno)
                        elif isinstance(child, (ast.AnnAssign, ast.Assign)):
                            self._record_class_field(info, node, child, identifier)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    identifier = symbol_id(info.module, node.name)
                    info.definitions[node.name] = identifier
                    self._add_symbol_node(info, node, identifier, "function", node.name)
                    self.add_edge(owner, identifier, "contains", node.lineno)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    for name in self._assignment_names(node):
                        if name.isupper():
                            identifier = data_id("constant", f"{info.module}.{name}")
                            self.add_node(identifier, "data", data_kind="constant", name=name, module=info.module, path=info.relative, line=node.lineno)
                            self.add_edge(owner, identifier, "defines", node.lineno)

    def _add_symbol_node(
        self, info: ModuleInfo, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        identifier: str, kind: str, qualname: str, owner: str | None = None,
    ) -> None:
        attributes: dict[str, Any] = {
            "module": info.module, "path": info.relative, "qualname": qualname,
            "name": node.name, "line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno),
            "visibility": _visibility(node.name), "owner": owner,
        }
        doc = ast.get_docstring(node, clean=True)
        if doc:
            attributes["summary"] = doc.splitlines()[0][:240]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            attributes["signature"] = _signature(node)
            attributes["async"] = isinstance(node, ast.AsyncFunctionDef)
            routes = self._routes(node)
            if routes:
                attributes["routes"] = routes
            action = self._decorator_keyword(node.decorator_list, "action", "name")
            if action:
                attributes["capability_action"] = action
        else:
            bases = [ast.unparse(base) for base in node.bases]
            if bases:
                attributes["bases"] = bases
            capability = self._decorator_keyword(node.decorator_list, "capability", "name")
            if capability:
                attributes["capability"] = capability
        self.add_node(identifier, kind, **attributes)

    @staticmethod
    def _assignment_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
        targets: list[ast.AST] = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
        return [target.id for target in targets if isinstance(target, ast.Name)]

    def _record_class_field(self, info: ModuleInfo, class_node: ast.ClassDef, node: ast.Assign | ast.AnnAssign, owner: str) -> None:
        for name in self._assignment_names(node):
            identifier = data_id("model-field", f"{info.module}.{class_node.name}.{name}")
            annotation = ast.unparse(node.annotation) if isinstance(node, ast.AnnAssign) and node.annotation else ""
            self.add_node(identifier, "data", data_kind="model-field", name=name, module=info.module, path=info.relative, line=node.lineno, annotation=annotation)
            self.add_edge(owner, identifier, "defines", node.lineno)

    def _record_imports(self, info: ModuleInfo, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                info.imports[alias.asname or alias.name.split(".")[0]] = alias.name
            return
        base = node.module or ""
        if node.level:
            package_parts = info.module.split(".") if info.path.name == "__init__.py" else info.module.split(".")[:-1]
            ascend = max(0, node.level - 1)
            keep = max(0, len(package_parts) - ascend)
            base = ".".join(package_parts[:keep] + ([base] if base else []))
        for alias in node.names:
            if alias.name == "*":
                continue
            info.imports[alias.asname or alias.name] = f"{base}:{alias.name}" if base else alias.name

    def _resolve_aliases(self) -> None:
        for info in self.modules.values():
            for local, target in info.imports.items():
                resolved = self._canonical_import(target)
                self.aliases[f"{info.module}:{local}"] = resolved
                target_id = resolved if ":" in resolved else module_id(resolved)
                if target_id in self.nodes:
                    self.add_edge(module_id(info.module), target_id, "imports", 0)

    def _canonical_import(self, target: str, seen: set[str] | None = None) -> str:
        seen = seen or set()
        if target in seen or ":" not in target:
            return target
        seen.add(target)
        module, name = target.split(":", 1)
        direct = symbol_id(module, name)
        if direct in self.nodes:
            return direct
        info = self.modules.get(module)
        if info and name in info.imports:
            return self._canonical_import(info.imports[name], seen)
        possible_module = f"{module}.{name}"
        return possible_module if possible_module in self.modules else target

    def _extract_relations(self) -> None:
        for info in self.modules.values():
            for node in info.tree.body:
                if isinstance(node, ast.ClassDef):
                    class_identifier = info.definitions[node.name]
                    for base in node.bases:
                        target = self._resolve_reference(info, dotted(base), node.name, {})
                        if target in self.nodes:
                            self.add_edge(class_identifier, target, "inherits", node.lineno)
                    class_types = self._constructor_types(info, node)
                    info.class_types[node.name] = class_types
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            self._walk_callable(info, child, info.class_methods[node.name][child.name], node.name, class_types)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self._walk_callable(info, node, info.definitions[node.name], None, {})

    def _constructor_types(self, info: ModuleInfo, class_node: ast.ClassDef) -> dict[str, str]:
        types: dict[str, str] = {}
        constructor = next((node for node in class_node.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "__init__"), None)
        if not constructor:
            return types
        parameters = self._parameter_types(info, constructor)
        for node in ast.walk(constructor):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name) or target.value.id != "self":
                    continue
                inferred = ""
                if isinstance(value, ast.Name):
                    inferred = parameters.get(value.id, "")
                elif isinstance(value, ast.Call):
                    inferred = self._resolve_reference(info, dotted(value.func), class_node.name, parameters)
                annotation = _annotation_name(node.annotation) if isinstance(node, ast.AnnAssign) else ""
                resolved = self._resolve_reference(info, annotation, class_node.name, parameters) or inferred
                types[target.attr] = resolved
                if resolved in self.nodes:
                    self.add_edge(symbol_id(info.module, class_node.name), resolved, "composes", target.lineno, "inferred")
        return types

    def _parameter_types(self, info: ModuleInfo, node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
        parameters: dict[str, str] = {}
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            name = _annotation_name(argument.annotation)
            if name:
                parameters[argument.arg] = self._resolve_reference(info, name, None, {})
        return parameters

    def _walk_callable(
        self, info: ModuleInfo, node: ast.FunctionDef | ast.AsyncFunctionDef,
        source: str, class_name: str | None, class_types: dict[str, str],
    ) -> None:
        local_types = self._parameter_types(info, node)
        for referenced_type in sorted(set(local_types.values())):
            if referenced_type in self.nodes:
                self.add_edge(source, referenced_type, "uses_type", node.lineno)
        return_type = self._resolve_reference(info, _annotation_name(node.returns), class_name, local_types)
        if return_type in self.nodes:
            self.add_edge(source, return_type, "uses_type", node.lineno)
        for child in walk_scope(node):
            if isinstance(child, ast.Call):
                self._call_relation(info, source, child, class_name, class_types, local_types)
                self._mapping_call_relation(source, child)
                self._environment_call_relation(source, child)
            elif isinstance(child, ast.Subscript):
                self._subscript_relation(source, child)
            elif isinstance(child, ast.Assign):
                self._assignment_relation(info, source, child, class_name, class_types, local_types)
            elif isinstance(child, ast.AnnAssign):
                self._assignment_relation(info, source, child, class_name, class_types, local_types)
            elif isinstance(child, ast.Attribute):
                self._state_field_relation(source, child)
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                self._persistence_relation(source, child.value, child.lineno)
        for route in self._routes(node):
            identifier = data_id("api-route", f"{route['method']} {route['path']}")
            self.add_node(identifier, "surface", data_kind="api-route", name=f"{route['method']} {route['path']}")
            self.add_edge(source, identifier, "exposes", node.lineno)
        action = self._decorator_keyword(node.decorator_list, "action", "name")
        if action:
            identifier = data_id("capability-action", action)
            self.add_node(identifier, "surface", data_kind="capability-action", name=action)
            self.add_edge(source, identifier, "exposes", node.lineno)

    def _call_relation(
        self, info: ModuleInfo, source: str, node: ast.Call, class_name: str | None,
        class_types: dict[str, str], local_types: dict[str, str],
    ) -> None:
        name = dotted(node.func)
        target = ""
        if name.startswith("self.") and class_name:
            parts = name.split(".")
            if len(parts) == 2:
                target = info.class_methods.get(class_name, {}).get(parts[1], "")
            elif len(parts) >= 3:
                owner_type = class_types.get(parts[1], "")
                target = self._method_on_type(owner_type, parts[-1])
        elif "." in name:
            root, method = name.split(".", 1)
            owner_type = local_types.get(root, "")
            if owner_type:
                target = self._method_on_type(owner_type, method.rsplit(".", 1)[-1])
            else:
                resolved_root = self._resolve_reference(info, root, class_name, local_types)
                target = self._method_on_type(resolved_root, method.rsplit(".", 1)[-1])
                if not target:
                    target = self._resolve_reference(info, name, class_name, local_types)
        else:
            target = self._resolve_reference(info, name, class_name, local_types)
        if target in self.nodes:
            self.add_edge(source, target, "calls", node.lineno, "inferred" if name.startswith("self.") and name.count(".") > 1 else "exact")
        event_type = self._event_type(node)
        if event_type:
            identifier = data_id("event", event_type)
            self.add_node(identifier, "surface", data_kind="event", name=event_type)
            self.add_edge(source, identifier, "publishes", node.lineno)

    def _assignment_relation(
        self, info: ModuleInfo, source: str, node: ast.Assign | ast.AnnAssign,
        class_name: str | None, class_types: dict[str, str], local_types: dict[str, str],
    ) -> None:
        targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        inferred = ""
        if isinstance(value, ast.Call):
            inferred = self._resolve_reference(info, dotted(value.func), class_name, local_types)
        for target in targets:
            if isinstance(target, ast.Name) and inferred:
                local_types[target.id] = inferred
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self" and class_name:
                inferred = inferred or class_types.get(target.attr, "")
                identifier = data_id("instance-attribute", f"{info.module}.{class_name}.{target.attr}")
                self.add_node(identifier, "data", data_kind="instance-attribute", name=target.attr, module=info.module, path=info.relative, line=target.lineno, value_type=inferred)
                self.add_edge(symbol_id(info.module, class_name), identifier, "owns", target.lineno)
                self.add_edge(source, identifier, "writes", target.lineno)
                if inferred in self.nodes:
                    self.add_edge(identifier, inferred, "typed_as", target.lineno, "inferred")

    def _resolve_reference(
        self, info: ModuleInfo, name: str, class_name: str | None,
        local_types: dict[str, str],
    ) -> str:
        if not name:
            return ""
        name = name.strip("'\"")
        if name in local_types:
            return local_types[name]
        if class_name and name in info.class_methods.get(class_name, {}):
            return info.class_methods[class_name][name]
        if name in info.definitions:
            return info.definitions[name]
        root, _, remainder = name.partition(".")
        alias = self.aliases.get(f"{info.module}:{root}")
        if alias:
            if remainder:
                method = self._method_on_type(alias, remainder.rsplit(".", 1)[-1])
                if method:
                    return method
                if alias.startswith("module:"):
                    return symbol_id(alias.removeprefix("module:"), remainder)
                if ":" not in alias:
                    return symbol_id(alias, remainder)
            return alias
        if name.startswith("gptmoss.") or name.startswith("scripts.") or name.startswith("tests."):
            if ":" in name:
                return name
            module, _, qualname = name.rpartition(".")
            candidate = symbol_id(module, qualname)
            return candidate if candidate in self.nodes else name
        return ""

    def _method_on_type(self, owner_type: str, method: str) -> str:
        if not owner_type:
            return ""
        if owner_type.startswith("module:"):
            candidate = symbol_id(owner_type.removeprefix("module:"), method)
            return candidate if candidate in self.nodes else ""
        if ":" not in owner_type:
            return ""
        module, qualname = owner_type.split(":", 1)
        candidate = symbol_id(module, f"{qualname}.{method}")
        return candidate if candidate in self.nodes else ""

    def _mapping_call_relation(self, source: str, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or not node.args:
            return
        operation = node.func.attr
        if operation not in {"get", *MAPPING_MUTATORS}:
            return
        key = literal_string(node.args[0])
        mapping = dotted(node.func.value)
        if not key:
            return
        kind = self._mapping_kind(mapping)
        if not kind:
            return
        identifier = data_id(kind, key)
        self.add_node(identifier, "data", data_kind=kind, name=key)
        relation = "reads" if operation == "get" else "writes"
        self.add_edge(source, identifier, relation, node.lineno)

    def _subscript_relation(self, source: str, node: ast.Subscript) -> None:
        key = literal_string(node.slice)
        if not key:
            return
        kind = self._mapping_kind(dotted(node.value))
        if not kind:
            return
        identifier = data_id(kind, key)
        self.add_node(identifier, "data", data_kind=kind, name=key)
        relation = "writes" if isinstance(node.ctx, (ast.Store, ast.Del)) else "reads"
        self.add_edge(source, identifier, relation, node.lineno)

    @staticmethod
    def _mapping_kind(mapping: str) -> str:
        tail = mapping.rsplit(".", 1)[-1]
        if tail in STATE_MAPPINGS:
            return STATE_MAPPINGS[tail]
        if mapping in {"config", "config_data"}:
            return "configuration"
        return ""

    def _state_field_relation(self, source: str, node: ast.Attribute) -> None:
        if node.attr not in {"status", "current_plan", "current_step", "execution_id"}:
            return
        owner = dotted(node.value).rsplit(".", 1)[-1]
        if owner not in {"state", "exec_state", "parent_state", "child_state", "current", "execution"} and not owner.endswith("_state"):
            return
        identifier = data_id("execution-field", node.attr)
        self.add_node(identifier, "data", data_kind="execution-field", name=node.attr)
        relation = "writes" if isinstance(node.ctx, ast.Store) else "reads"
        self.add_edge(source, identifier, relation, node.lineno)

    def _environment_call_relation(self, source: str, node: ast.Call) -> None:
        name = dotted(node.func)
        if name not in {"os.getenv", "os.environ.get"} or not node.args:
            return
        key = literal_string(node.args[0])
        if key:
            identifier = data_id("environment", key)
            self.add_node(identifier, "data", data_kind="environment", name=key)
            self.add_edge(source, identifier, "reads", node.lineno)

    def _persistence_relation(self, source: str, value: str, line: int) -> None:
        normalized = value.replace("\\", "/")
        for candidate, location in self.persistence_names.items():
            if candidate in normalized:
                identifier = data_id("persistence", location)
                self.add_node(identifier, "data", data_kind="persistence", name=location)
                self.add_edge(source, identifier, "accesses", line, "literal")

    @staticmethod
    def _event_type(node: ast.Call) -> str | None:
        if dotted(node.func).rsplit(".", 1)[-1] != "Event":
            return None
        for keyword in node.keywords:
            if keyword.arg == "type":
                return literal_string(keyword.value)
        return None

    @staticmethod
    def _decorator_keyword(decorators: list[ast.expr], decorator_name: str, keyword_name: str) -> str | None:
        for decorator in decorators:
            if not isinstance(decorator, ast.Call) or dotted(decorator.func).rsplit(".", 1)[-1] != decorator_name:
                continue
            for keyword in decorator.keywords:
                if keyword.arg == keyword_name:
                    return literal_string(keyword.value)
            if decorator.args:
                return literal_string(decorator.args[0])
        return None

    @staticmethod
    def _routes(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[dict[str, str]]:
        routes: list[dict[str, str]] = []
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            name = dotted(decorator.func)
            method = name.rsplit(".", 1)[-1]
            path = literal_string(decorator.args[0])
            if name.startswith("app.") and method in ROUTE_METHODS and path is not None:
                routes.append({"method": method.upper(), "path": path})
        return routes

    def build(self) -> dict[str, Any]:
        self.load()
        paths = sorted(set([*source_paths(self.root), *operational_paths(self.root)]))
        digest = hashlib.sha256()
        for path in paths:
            relative = path.relative_to(self.root).as_posix()
            content = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
            digest.update(relative.encode("utf-8") + b"\0" + content.encode("utf-8") + b"\0")
        nodes = sorted(self.nodes.values(), key=lambda item: item["id"])
        edges = [
            {"source": source, "target": target, "kind": kind, **({"line": line} if line else {}), "confidence": confidence}
            for source, target, kind, line, confidence in sorted(self.edges)
        ]
        kind_counts: dict[str, int] = {}
        for node in nodes:
            kind_counts[node["kind"]] = kind_counts.get(node["kind"], 0) + 1
        edge_counts: dict[str, int] = {}
        for edge in edges:
            edge_counts[edge["kind"]] = edge_counts.get(edge["kind"], 0) + 1
        return {
            "schema_version": 1,
            "application": "GPTMOSS",
            "source_sha256": digest.hexdigest(),
            "scope": {
                "production": ["main.py", "gptmoss/**/*.py"],
                "operations": ["scripts/**/*.py"],
                "tests": ["tests/**/*.py"],
                "frontend": ["gptmoss/api/gui.html"],
                "distribution": ["*.bat", "*.sh", "scripts/**/*.ps1"],
                "structured_data_only": False,
            },
            "diagnostics": {"unresolved_gui_api_calls": getattr(self, "gui_unresolved", [])},
            "stats": {"files": len(paths), "nodes": len(nodes), "edges": len(edges), "nodes_by_kind": kind_counts, "edges_by_kind": edge_counts},
            "nodes": nodes,
            "edges": edges,
        }


def generate(root: Path = ROOT) -> dict[str, Any]:
    return SymbolGraphBuilder(root).build()


def serialized(graph: dict[str, Any]) -> str:
    """Keep generated diffs reviewable: one stable JSON record per node or edge."""
    lines = ["{"]
    metadata = [(key, value) for key, value in graph.items() if key not in {"nodes", "edges"}]
    for key, value in metadata:
        lines.append(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))},")
    for collection_name in ("nodes", "edges"):
        lines.append(f"  {json.dumps(collection_name)}: [")
        values = graph[collection_name]
        for index, value in enumerate(values):
            comma = "," if index + 1 < len(values) else ""
            lines.append(f"    {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}{comma}")
        suffix = "," if collection_name == "nodes" else ""
        lines.append(f"  ]{suffix}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def check(output: Path = DEFAULT_OUTPUT, root: Path = ROOT) -> list[str]:
    if not output.exists():
        return [f"symbol map is absent: {output}"]
    expected = serialized(generate(root))
    actual = output.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return [] if actual == expected else ["docs/symbol-map.json is stale; run python scripts/generate_symbol_map.py"]


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or verify the GPTMOSS relational symbol map.")
    parser.add_argument("--check", action="store_true", help="Fail when the committed graph differs from the code.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    if args.check:
        errors = check(output)
        if errors:
            for error in errors:
                print(f"[FAIL] {error}")
            return 1
        print("[PASS] Relational symbol map matches the Python sources.")
        return 0
    graph = generate()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized(graph), encoding="utf-8", newline="\n")
    print(f"[PASS] Wrote {graph['stats']['nodes']} nodes and {graph['stats']['edges']} relations to {output.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
