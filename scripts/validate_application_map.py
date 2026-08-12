"""Validate the living GPTMOSS application map against the repository."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "docs" / "application-map.json"
ROUTE_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "WEBSOCKET"}


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        prefix = _decorator_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _constant_keyword(call: ast.Call, name: str) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    return None


def _python_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def discover_routes() -> set[tuple[str, str, str]]:
    routes: set[tuple[str, str, str]] = set()
    tree = _python_tree(ROOT / "gptmoss" / "api" / "server.py")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            name = _decorator_name(decorator.func)
            if not name.startswith("app.") or not isinstance(decorator.args[0], ast.Constant):
                continue
            method = name.rsplit(".", 1)[-1].upper()
            if method in ROUTE_METHODS:
                routes.add((method, str(decorator.args[0].value), node.name))
    return routes


def discover_capabilities() -> dict[str, dict[str, Any]]:
    capabilities: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "gptmoss" / "capabilities").glob("*.py")):
        tree = _python_tree(path)
        for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
            capability_name: str | None = None
            for decorator in class_node.decorator_list:
                if isinstance(decorator, ast.Call) and _decorator_name(decorator.func) == "capability":
                    capability_name = _constant_keyword(decorator, "name")
            if not capability_name:
                continue
            actions: list[str] = []
            for function_node in class_node.body:
                if not isinstance(function_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in function_node.decorator_list:
                    if isinstance(decorator, ast.Call) and _decorator_name(decorator.func) == "action":
                        actions.append(_constant_keyword(decorator, "name") or function_node.name)
            capabilities[capability_name] = {
                "module": path.relative_to(ROOT).as_posix(),
                "actions": actions,
            }
    return capabilities


def discover_events() -> set[str]:
    events: set[str] = set()
    for path in (ROOT / "gptmoss").rglob("*.py"):
        tree = _python_tree(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _decorator_name(node.func).rsplit(".", 1)[-1] != "Event":
                continue
            event_type = _constant_keyword(node, "type")
            if event_type:
                events.add(event_type)
    return events


def _normalized_sha256(path: Path) -> str:
    content = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _pinned_requirements(path: Path) -> dict[str, str]:
    packages: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
        if not match:
            raise ValueError(f"Unpinned or unsupported requirement: {line}")
        packages[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(2)
    return packages


def _difference(label: str, mapped: Iterable[Any], actual: Iterable[Any]) -> list[str]:
    mapped_set, actual_set = set(mapped), set(actual)
    errors: list[str] = []
    missing = sorted(actual_set - mapped_set)
    stale = sorted(mapped_set - actual_set)
    if missing:
        errors.append(f"{label}: missing from map: {missing}")
    if stale:
        errors.append(f"{label}: stale map entries: {stale}")
    return errors


def validate(root: Path = ROOT) -> list[str]:
    global ROOT, MAP_PATH
    previous_root, previous_map = ROOT, MAP_PATH
    ROOT = root.resolve()
    MAP_PATH = ROOT / "docs" / "application-map.json"
    try:
        mapping = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        errors: list[str] = []
        if mapping.get("schema_version") != 1:
            errors.append("schema_version must be 1")

        references = (
            list(mapping.get("entrypoints", []))
            + list(mapping.get("modules", {}))
            + list(mapping.get("operational_files", []))
            + list(mapping.get("documents", []))
            + list(mapping.get("offline", {}).get("required_repository_files", []))
        )
        for relative in sorted(set(references)):
            if not (ROOT / relative).exists():
                errors.append(f"referenced path does not exist: {relative}")

        actual_modules = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "gptmoss").rglob("*.py")
            if path.name != "__init__.py"
        }
        errors.extend(_difference("Python modules", mapping.get("modules", {}), actual_modules))

        actual_operations = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "scripts").iterdir()
            if path.is_file()
        }
        errors.extend(_difference("Operational files", mapping.get("operational_files", []), actual_operations))

        actual_tests = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "tests").glob("test_*.py")
        }
        mapped_tests = {
            test
            for feature in mapping.get("features", [])
            for test in feature.get("tests", [])
        }
        errors.extend(_difference("Tests", mapped_tests, actual_tests))

        mapped_routes = {tuple(route) for route in mapping.get("api_routes", [])}
        errors.extend(_difference("API routes", mapped_routes, discover_routes()))
        if mapping.get("capabilities") != discover_capabilities():
            errors.append("Capability/action inventory differs from decorated capability code")
        errors.extend(_difference("Events", mapping.get("events", []), discover_events()))

        config_fields = set(json.loads((ROOT / "config.json.template").read_text(encoding="utf-8")))
        errors.extend(_difference("Configuration fields", mapping.get("configuration_fields", []), config_fields))

        gui = (ROOT / "gptmoss" / "api" / "gui.html").read_text(encoding="utf-8")
        for surface in mapping.get("gui_surfaces", []):
            for evidence in surface.get("evidence", []):
                if evidence not in gui:
                    errors.append(f"GUI surface {surface.get('id')}: evidence not found: {evidence}")

        offline = mapping.get("offline", {})
        manifest = json.loads((ROOT / offline["manifest"]).read_text(encoding="utf-8"))
        requirements_path = ROOT / offline["requirements"]
        constraints_path = ROOT / offline["constraints"]
        if manifest.get("requirements_sha256") != _normalized_sha256(requirements_path):
            errors.append("Offline manifest requirements hash is stale")
        if manifest.get("constraints_sha256") != _normalized_sha256(constraints_path):
            errors.append("Offline manifest constraints hash is stale")
        requirements = _pinned_requirements(requirements_path)
        constraints = _pinned_requirements(constraints_path)
        manifest_packages = {
            re.sub(r"[-_.]+", "-", name).lower(): str(version)
            for name, version in manifest.get("packages", {}).items()
        }
        for package, version in requirements.items():
            if constraints.get(package) != version:
                errors.append(f"Offline constraint mismatch for {package}=={version}")
        if constraints != manifest_packages:
            errors.append("Offline manifest package inventory differs from constraints-runtime.txt")
        if manifest.get("runtime_directory") != offline.get("runtime_directory"):
            errors.append("Offline runtime directory differs between map and manifest")
        if not (ROOT / offline["runtime_directory"]).is_dir():
            errors.append("Offline runtime directory is absent")

        return errors
    finally:
        ROOT, MAP_PATH = previous_root, previous_map


def main() -> int:
    try:
        errors = validate()
    except (OSError, ValueError, json.JSONDecodeError, SyntaxError) as exc:
        print(f"[FAIL] Application map validation could not run: {exc}")
        return 1
    if errors:
        print("[FAIL] GPTMOSS application map is inconsistent:")
        for error in errors:
            print(f" - {error}")
        return 1
    print("[PASS] GPTMOSS application map matches modules, API, GUI, tests, config and offline metadata.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
