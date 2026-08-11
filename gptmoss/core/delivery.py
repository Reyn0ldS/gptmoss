"""Deterministic delivery contracts and independent workspace assurance."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from gptmoss.core.artifact_validation import validate_artifact


SCHEMA_VERSION = 1
INDEPENDENT_ROLES = {"qa", "debugger", "coordinator"}
SCOPE_MARKERS = (
    "out of scope", "hors périmètre", "hors perimetre", "not supported",
    "future work", "plus tard", "not implemented", "mock only", "simulation only",
)
IGNORED_DIRECTORIES = {
    ".git", ".pytest_cache", "__pycache__", "node_modules", ".mypy_cache",
    ".ruff_cache", ".venv", "venv",
}


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-zA-ZÀ-ÿ0-9]{4,}", str(text).lower())
    stop = {
        "avec", "dans", "pour", "from", "that", "this", "then", "doit",
        "devra", "pouvoir", "faire", "create", "build", "implement", "support",
        "programme", "logiciel", "project", "projet",
    }
    return {word for word in words if word not in stop}


def extract_requirements(task: str, limit: int = 0) -> List[Dict[str, Any]]:
    """Extract stable, user-owned requirement clauses without calling an LLM."""
    text = str(task or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    clauses: List[str] = []
    for line in lines:
        line = re.sub(r"^(?:[-*+]|\d+[.)]|[A-Za-z][.)])\s+", "", line).strip()
        clauses.extend(re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", line))
    clauses = [clause.strip(" .;,") for clause in clauses
               if len(clause.strip(" .;,")) >= 6]
    if not clauses:
        clauses = [re.sub(r"\s+", " ", text)]
    selected = clauses[:limit] if limit and limit > 0 else clauses
    requirements = []
    seen = set()
    for clause in selected:
        clause = re.sub(r"\s+", " ", clause).strip()
        digest = hashlib.sha256(clause.lower().encode("utf-8")).hexdigest()[:12]
        if digest in seen:
            continue
        seen.add(digest)
        requirements.append({
            "id": f"REQ-{len(requirements) + 1:03d}",
            "statement": clause,
            "priority": "must",
            "mandatory": True,
            "source": "user",
            "acceptance": [],
        })
    return requirements


def normalize_requirements(plan: Dict[str, Any], task: str) -> List[Dict[str, Any]]:
    raw = plan.get("requirements")
    if not isinstance(raw, list) or not raw:
        raw = extract_requirements(task)
    normalized: List[Dict[str, Any]] = []
    identifiers = set()
    for index, item in enumerate(raw):
        if isinstance(item, str):
            item = {"statement": item}
        if not isinstance(item, dict):
            raise ValueError(f"Requirement {index} must be an object or string.")
        identifier = str(item.get("id") or f"REQ-{index + 1:03d}").strip()
        statement = str(item.get("statement") or item.get("description") or "").strip()
        if not identifier or identifier in identifiers or not statement:
            raise ValueError(f"Requirement {index} has an invalid id or statement.")
        identifiers.add(identifier)
        priority = str(item.get("priority") or "must").strip().lower()
        mandatory = bool(item.get("mandatory", priority in {"must", "required", "critical"}))
        normalized.append({
            "id": identifier,
            "statement": statement,
            "priority": priority,
            "mandatory": mandatory,
            "source": str(item.get("source") or "user"),
            "acceptance": _strings(item.get("acceptance")),
        })
    plan["requirements"] = normalized
    return normalized


def map_requirements(plan: Dict[str, Any], requirements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map every requirement to implementation and independent validation steps."""
    steps = plan.get("steps", [])
    has_developer_steps = any(
        str(step.get("role") or "").lower() == "developer" for step in steps
    )
    known = {requirement["id"] for requirement in requirements}
    for step in steps:
        explicit = _strings(step.get("requirement_ids"))
        unknown = set(explicit) - known
        if unknown:
            raise ValueError(
                f"Plan step {step.get('id')} references unknown requirements: {sorted(unknown)}"
            )
        step["requirement_ids"] = explicit

    for requirement in requirements:
        requirement_words = _keywords(requirement["statement"])
        scored = []
        for step in steps:
            step_text = " ".join([
                str(step.get("description") or ""),
                " ".join(_strings(step.get("acceptance_criteria"))),
                " ".join(_strings(step.get("expertise"))),
            ])
            overlap = len(requirement_words & _keywords(step_text))
            scored.append((overlap, step))
        explicitly_mapped = [step for _, step in scored if requirement["id"] in step["requirement_ids"]]
        if not explicitly_mapped:
            implementation_candidates = [
                pair for pair in scored
                if (
                    str(pair[1].get("role") or "").lower() == "developer"
                    if has_developer_steps
                    else str(pair[1].get("role") or "") not in INDEPENDENT_ROLES
                )
            ]
            best = max(implementation_candidates or scored, key=lambda pair: pair[0],
                       default=(0, None))[1]
            if best is not None:
                best["requirement_ids"].append(requirement["id"])
        elif not any(
            (
                str(step.get("role") or "").lower() == "developer"
                if has_developer_steps
                else str(step.get("role") or "") not in INDEPENDENT_ROLES
            )
            for step in explicitly_mapped
        ):
            implementation_candidates = [
                pair for pair in scored
                if (
                    str(pair[1].get("role") or "").lower() == "developer"
                    if has_developer_steps
                    else str(pair[1].get("role") or "") not in INDEPENDENT_ROLES
                )
            ]
            best = max(implementation_candidates, key=lambda pair: pair[0],
                       default=(0, None))[1]
            if best is not None:
                best["requirement_ids"].append(requirement["id"])
        if not any(
            requirement["id"] in step["requirement_ids"]
            and str(step.get("role") or "") in INDEPENDENT_ROLES
            for step in steps
        ):
            validation_candidates = [
                pair for pair in scored
                if str(pair[1].get("role") or "").lower() == "qa"
            ]
            positive_qa = [pair for pair in validation_candidates if pair[0] > 0]
            if positive_qa:
                verifier = max(positive_qa, key=lambda pair: pair[0])[1]
            else:
                verifier = next(
                    (step for step in reversed(steps)
                     if str(step.get("role") or "") in INDEPENDENT_ROLES),
                    None,
                )
            if verifier is not None:
                verifier["requirement_ids"].append(requirement["id"])

    matrix = []
    for requirement in requirements:
        mapped = [
            step for step in steps
            if requirement["id"] in step.get("requirement_ids", [])
        ]
        mapped_has_developer = any(
            str(step.get("role") or "").lower() == "developer" for step in mapped
        )
        matrix.append({
            "requirement_id": requirement["id"],
            "statement": requirement["statement"],
            "mandatory": requirement["mandatory"],
            "implementation_steps": [
                step["id"] for step in mapped
                if (
                    str(step.get("role") or "").lower() == "developer"
                    if mapped_has_developer
                    else str(step.get("role") or "") not in INDEPENDENT_ROLES
                )
            ],
            "validation_steps": [
                step["id"] for step in mapped
                if str(step.get("role") or "") in INDEPENDENT_ROLES
            ],
            "acceptance": requirement["acceptance"],
        })
    plan["traceability"] = matrix
    return matrix


def normalize_scope_changes(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Collect explicit or planner-analysis scope reductions for human approval."""
    raw = plan.get("scope_changes")
    if raw is None:
        raw = []
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("scope_changes must be a list.")
    analysis = plan.get("analysis") if isinstance(plan.get("analysis"), dict) else {}
    out_of_scope = analysis.get("out_of_scope")
    for statement in _strings(out_of_scope):
        raw.append({"kind": "out_of_scope", "statement": statement})
    boundary = str(analysis.get("mvp_boundary") or "").strip()
    if boundary and any(marker in boundary.lower() for marker in (
        "no claim", "prototype only", "mvp only", "ne couvre pas",
        "does not cover", "will not implement", "not implemented",
    )):
        raw.append({"kind": "mvp_boundary", "statement": boundary})
    for step in plan.get("steps", []):
        step_text = " ".join([
            str(step.get("description") or ""),
            " ".join(_strings(step.get("acceptance_criteria"))),
        ])
        if any(marker in step_text.lower() for marker in SCOPE_MARKERS):
            raw.append({
                "kind": "scope_reduction",
                "statement": step_text,
                "requirement_ids": _strings(step.get("requirement_ids")),
                "reason": "Detected in the proposed execution plan.",
            })

    normalized = []
    seen = set()
    for index, item in enumerate(raw):
        if isinstance(item, str):
            item = {"statement": item}
        if not isinstance(item, dict):
            raise ValueError(f"Scope change {index} must be an object or string.")
        statement = str(item.get("statement") or item.get("description") or "").strip()
        if not statement:
            continue
        digest = hashlib.sha256(statement.lower().encode("utf-8")).hexdigest()[:12]
        if digest in seen:
            continue
        seen.add(digest)
        normalized.append({
            "id": str(item.get("id") or f"SCOPE-{len(normalized) + 1:03d}"),
            "kind": str(item.get("kind") or "scope_reduction"),
            "statement": statement,
            "requirement_ids": _strings(item.get("requirement_ids")),
            "reason": str(item.get("reason") or ""),
        })
    plan["scope_changes"] = normalized
    return normalized


def normalize_command(command: str) -> str:
    """Normalize harmless launch wrappers while preserving the tested target."""
    text = str(command or "").strip()
    text = re.sub(r"^(?:cmd(?:\.exe)?\s+/[cd]\s+)", "", text, flags=re.IGNORECASE)
    segments = [part.strip() for part in re.split(r"\s*&&\s*", text) if part.strip()]
    while len(segments) > 1 and re.match(
        r"^(?:chcp\b|cd(?:\s+/d)?\b|set\s+[A-Za-z_][A-Za-z0-9_]*=)",
        segments[0],
        flags=re.IGNORECASE,
    ):
        segments.pop(0)
    text = " && ".join(segments)
    text = re.sub(
        r"\s+(?:\d?>|>>|2>&1)\s*(?:nul|/dev/null)?\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r'^(?:"[^"]*[\\/]python(?:\.exe)?"|[^\s"]*[\\/]python(?:\.exe)?|py(?:\.exe)?(?:\s+-3)?)(?=\s|$)',
        "python",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace(chr(92), "/").lower()


def _command_tokens(command: str) -> List[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def commands_equivalent(expected: str, observed: str) -> bool:
    """Compare command evidence without confusing targeted and full test runs."""
    expected_normalized = normalize_command(expected)
    observed_normalized = normalize_command(observed)
    if expected_normalized == observed_normalized:
        return True
    expected_tokens = _command_tokens(expected_normalized)
    observed_tokens = _command_tokens(observed_normalized)
    pytest_prefix = ["python", "-m", "pytest"]
    if expected_tokens[:3] != pytest_prefix or observed_tokens[:3] != pytest_prefix:
        return False
    return sorted(expected_tokens[3:]) == sorted(observed_tokens[3:])


def normalize_ownership(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalize file ownership while allowing legacy plans to derive it."""
    claims = []
    exact_owners: Dict[str, Any] = {}
    for step in plan.get("steps", []):
        owned = _strings(step.get("owned_paths"))
        if not owned:
            owned = _strings(step.get("required_artifacts"))
        step["owned_paths"] = list(dict.fromkeys(path.replace("\\", "/") for path in owned))
        for pattern in step["owned_paths"]:
            if pattern.startswith("/") or ".." in Path(pattern).parts:
                raise ValueError(f"Step {step.get('id')} has unsafe owned path '{pattern}'.")
            if not any(char in pattern for char in "*?["):
                previous = exact_owners.get(pattern)
                if previous is not None and previous != step.get("id"):
                    raise ValueError(
                        f"File ownership conflict for '{pattern}' between steps "
                        f"{previous} and {step.get('id')}."
                    )
                exact_owners[pattern] = step.get("id")
            claims.append({
                "step_id": step.get("id"),
                "role": step.get("role"),
                "pattern": pattern,
            })
    plan["ownership"] = claims
    return claims


def _normalization_warning(plan: Dict[str, Any], message: str) -> None:
    warnings = plan.setdefault("normalization_warnings", [])
    if isinstance(warnings, list):
        warnings.append(message)


def normalize_artifact_validations(
    plan: Dict[str, Any], *, strict: bool = True
) -> List[Dict[str, Any]]:
    """Normalize extensible validation specifications declared by the planner."""
    raw = plan.get("artifact_validations") or []
    if not isinstance(raw, list):
        if strict:
            raise ValueError("artifact_validations must be a list.")
        _normalization_warning(plan, "Ignored malformed artifact_validations metadata.")
        raw = []
    normalized = []
    seen = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            if strict:
                raise ValueError(f"Artifact validation {index} must be an object.")
            _normalization_warning(plan, f"Ignored malformed artifact validation {index}.")
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/")
        validator = str(item.get("validator") or Path(path).suffix).strip().lower().lstrip(".")
        constraints = item.get("constraints") or {}
        if (
            not path or path.startswith("/") or ".." in Path(path).parts
            or path in seen or not isinstance(constraints, dict)
        ):
            if strict:
                raise ValueError(f"Artifact validation {index} is invalid.")
            _normalization_warning(plan, f"Ignored invalid artifact validation {index}.")
            continue
        seen.add(path)
        normalized.append({
            "path": path,
            "validator": validator,
            "constraints": constraints,
            "required": bool(item.get("required", True)),
        })
    plan["artifact_validations"] = normalized
    return normalized


def _routine_steps(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = str(
                item.get("command") or item.get("action")
                or item.get("description") or json.dumps(item, ensure_ascii=False)
            )
        else:
            text = str(item)
        if text.strip():
            normalized.append(text.strip())
    return normalized


def normalize_external_tools(
    plan: Dict[str, Any], *, strict: bool = True
) -> List[Dict[str, Any]]:
    """Freeze truthful setup contracts for project-specific external tools."""
    raw = plan.get("external_tools") or []
    if not isinstance(raw, list):
        if strict:
            raise ValueError("external_tools must be a list.")
        _normalization_warning(plan, "Ignored malformed external_tools metadata.")
        raw = []
    normalized = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            if strict:
                raise ValueError(f"External tool {index} must be an object.")
            _normalization_warning(plan, f"Ignored malformed external tool {index}.")
            continue
        name = str(item.get("name") or item.get("tool") or "").strip()
        purpose = str(item.get("purpose") or item.get("description") or "").strip()
        configuration = item.get("configuration") or item.get("parameters") or {}
        if not name or not purpose or not isinstance(configuration, dict):
            if strict:
                raise ValueError(
                    f"External tool {index} requires name, purpose, and configuration."
                )
            _normalization_warning(plan, f"Ignored invalid external tool {index}.")
            continue
        normalized.append({
            "name": name,
            "purpose": purpose,
            "required": bool(item.get("required", False)),
            "availability_probe": str(item.get("availability_probe") or "").strip(),
            "configuration": configuration,
            "setup": _routine_steps(item.get("setup") or item.get("installation") or []),
            "commands": _routine_steps(item.get("commands") or []),
            "expected_outputs": _strings(item.get("expected_outputs") or item.get("outputs")),
            "validation": _routine_steps(item.get("validation") or []),
            "rollback": _routine_steps(item.get("rollback") or []),
        })
    plan["external_tools"] = normalized
    return normalized


def normalize_execution_routines(
    plan: Dict[str, Any], *, strict: bool = True
) -> List[Dict[str, Any]]:
    """Normalize reusable operator-run routines without executing desktop tools."""
    raw = plan.get("execution_routines") or []
    if not isinstance(raw, list):
        if strict:
            raise ValueError("execution_routines must be a list.")
        _normalization_warning(plan, "Ignored malformed execution_routines metadata.")
        raw = []
    normalized = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            if strict:
                raise ValueError(f"Execution routine {index} must be an object.")
            _normalization_warning(plan, f"Ignored malformed execution routine {index}.")
            continue
        name = str(item.get("name") or "").strip()
        purpose = str(item.get("purpose") or item.get("description") or "").strip()
        configuration = item.get("configuration") or item.get("parameters") or {}
        steps = _routine_steps(item.get("steps") or item.get("commands") or [])
        if not name or not purpose or not isinstance(configuration, dict) or not steps:
            if strict:
                raise ValueError(
                    f"Execution routine {index} requires name, purpose, configuration, and steps."
                )
            _normalization_warning(plan, f"Ignored invalid execution routine {index}.")
            continue
        normalized.append({
            "name": name,
            "purpose": purpose,
            "prerequisites": _strings(item.get("prerequisites")),
            "configuration": configuration,
            "steps": steps,
            "expected_outputs": _strings(item.get("expected_outputs") or item.get("outputs")),
            "validation": _routine_steps(item.get("validation") or []),
            "failure_handling": _routine_steps(
                item.get("failure_handling") or item.get("rollback") or []
            ),
        })
    plan["execution_routines"] = normalized
    return normalized


def build_delivery_contract(plan: Dict[str, Any], task: str) -> Dict[str, Any]:
    """Enrich a normalized plan and freeze the user-owned delivery contract."""
    requirements = normalize_requirements(plan, task)
    traceability = map_requirements(plan, requirements)
    scope_changes = normalize_scope_changes(plan)
    ownership = normalize_ownership(plan)
    artifact_validations = normalize_artifact_validations(plan, strict=False)
    external_tools = normalize_external_tools(plan, strict=False)
    execution_routines = normalize_execution_routines(plan, strict=False)
    commands = []
    for step in plan.get("steps", []):
        for command in _strings(step.get("verification_commands")):
            commands.append({
                "step_id": step.get("id"),
                "role": step.get("role"),
                "command": command,
                "independent": str(step.get("role") or "") in INDEPENDENT_ROLES,
            })
    software_suffixes = {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt",
        ".cs", ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp",
        ".rb", ".php", ".swift", ".scala", ".sh", ".ps1", ".bat",
    }
    software_delivery = any(
        str(step.get("role") or "") == "developer"
        or any(Path(path).suffix.lower() in software_suffixes
               for path in _strings(step.get("required_artifacts")))
        or any(re.search(
            r"(?i)\b(?:pytest|unittest|npm\s+(?:run\s+)?test|cargo\s+test|"
            r"go\s+test|dotnet\s+test|mvn\s+test|gradle\w*\s+test)\b",
            command,
        ) for command in _strings(step.get("verification_commands")))
        for step in plan.get("steps", [])
    )
    contract = {
        "schema_version": SCHEMA_VERSION,
        "task_sha256": hashlib.sha256(str(task).encode("utf-8")).hexdigest(),
        "task": str(task),
        "requirements": requirements,
        "traceability": traceability,
        "scope_changes": scope_changes,
        "ownership": ownership,
        "verification_commands": commands,
        "launch_commands": _strings(plan.get("launch_commands")),
        "interfaces": plan.get("interfaces") if isinstance(plan.get("interfaces"), list) else [],
        "artifact_validations": artifact_validations,
        "external_tools": external_tools,
        "execution_routines": execution_routines,
        "normalization_warnings": _strings(plan.get("normalization_warnings")),
        "software_delivery": software_delivery,
    }
    frozen = json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    contract["contract_sha256"] = hashlib.sha256(frozen.encode("utf-8")).hexdigest()
    return contract


def path_is_owned(contract: Dict[str, Any], step_id: Any, role: str, path: str) -> bool:
    """Return whether a specialist may mutate a relative workspace path."""
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    original_path = str(path or "").replace(chr(92), "/")
    if (not normalized or original_path.startswith("/")
            or original_path.removeprefix("./").startswith(".gptmoss/")):
        return False
    claims = contract.get("ownership") if isinstance(contract, dict) else []
    if role == "debugger":
        return not any(
            str(claim.get("role") or "").lower() == "qa"
            and fnmatch.fnmatchcase(normalized, str(claim.get("pattern") or ""))
            for claim in claims
        )
    if not claims:
        return True
    own_claims = [
        claim for claim in claims
        if str(claim.get("step_id")) == str(step_id)
    ]
    if not own_claims:
        return True  # legacy steps without claims remain compatible
    return any(fnmatch.fnmatchcase(normalized, str(claim.get("pattern") or ""))
               for claim in own_claims)


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        if not any(part in IGNORED_DIRECTORIES for part in path.parts):
            yield path


def _signature_accepts(function: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call) -> Optional[str]:
    positional = list(function.args.posonlyargs) + list(function.args.args)
    if positional and positional[0].arg in {"self", "cls"}:
        positional = positional[1:]
    required_count = max(0, len(positional) - len(function.args.defaults))
    maximum = None if function.args.vararg else len(positional)
    supplied = len(call.args)
    keywords = {keyword.arg for keyword in call.keywords if keyword.arg}
    accepted_keywords = {argument.arg for argument in positional}
    accepted_keywords.update(argument.arg for argument in function.args.kwonlyargs)
    if supplied < required_count and not keywords:
        return f"expects at least {required_count} positional arguments, received {supplied}"
    if maximum is not None and supplied > maximum:
        return f"expects at most {maximum} positional arguments, received {supplied}"
    if function.args.kwarg is None:
        unknown = keywords - accepted_keywords
        if unknown:
            return f"does not accept keyword(s): {', '.join(sorted(unknown))}"
    return None


def _expression_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _expression_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def static_workspace_issues(root: str | Path) -> List[Dict[str, Any]]:
    """Run independent syntax, package-identity, import, and call-signature checks."""
    workspace = Path(root).resolve()
    issues: List[Dict[str, Any]] = []
    parsed: Dict[Path, ast.Module] = {}
    definitions: Dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    methods: Dict[tuple[str, str], ast.FunctionDef | ast.AsyncFunctionDef] = {}
    classes: Dict[str, ast.ClassDef] = {}

    for path in _iter_python_files(workspace):
        relative = path.relative_to(workspace).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
            parsed[path] = tree
        except (OSError, UnicodeError, SyntaxError) as error:
            issues.append({"kind": "syntax", "path": relative, "message": str(error)})
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definitions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                classes[node.name] = node
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods[(node.name, child.name)] = child

    for path, tree in parsed.items():
        relative = path.relative_to(workspace).as_posix()
        aliases: Dict[str, str] = {}
        instance_types: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and (node.module.startswith("src.") or node.module == "src"):
                    issues.append({
                        "kind": "package_identity", "path": relative,
                        "message": f"non-canonical import from {node.module}",
                    })
                for alias in node.names:
                    aliases[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("src."):
                        issues.append({
                            "kind": "package_identity", "path": relative,
                            "message": f"non-canonical import {alias.name}",
                        })
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                if isinstance(node.value.func, ast.Name):
                    class_name = aliases.get(node.value.func.id, node.value.func.id)
                    if class_name in classes:
                        for target in node.targets:
                            target_name = _expression_name(target)
                            if target_name:
                                instance_types[target_name] = class_name

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = None
            label = None
            if isinstance(node.func, ast.Name):
                name = aliases.get(node.func.id, node.func.id)
                target = definitions.get(name)
                label = name
            elif isinstance(node.func, ast.Attribute):
                owner_name = _expression_name(node.func.value)
                class_name = instance_types.get(owner_name or "")
                if class_name:
                    target = methods.get((class_name, node.func.attr))
                    label = f"{class_name}.{node.func.attr}"
            if target is not None:
                mismatch = _signature_accepts(target, node)
                if mismatch:
                    issues.append({
                        "kind": "signature", "path": relative,
                        "line": getattr(node, "lineno", None),
                        "message": f"{label} {mismatch}",
                    })
    return issues


def declared_interface_issues(
    root: str | Path, interfaces: Iterable[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Compare planner-frozen public interface contracts with actual source AST."""
    workspace = Path(root).resolve()
    issues = []
    for index, interface in enumerate(interfaces):
        if not isinstance(interface, dict):
            issues.append({"kind": "interface_contract", "message": f"interface {index} is not an object"})
            continue
        module = str(interface.get("module") or interface.get("producer") or "").strip()
        symbol = str(interface.get("symbol") or "").strip()
        expected = _strings(interface.get("parameters"))
        if not module or not symbol:
            issues.append({"kind": "interface_contract", "message": f"interface {index} must declare module and symbol"})
            continue
        module_relative = module.replace(".", "/")
        candidates = [
            workspace / f"{module_relative}.py", workspace / "src" / f"{module_relative}.py",
            workspace / module_relative / "__init__.py", workspace / "src" / module_relative / "__init__.py",
        ]
        source_path = next((path for path in candidates if path.is_file()), None)
        if source_path is None:
            issues.append({"kind": "interface_contract", "module": module, "symbol": symbol,
                           "message": f"declared interface module '{module}' does not exist"})
            continue
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, SyntaxError) as error:
            issues.append({"kind": "interface_contract", "module": module, "symbol": symbol,
                           "message": f"cannot parse interface module: {error}"})
            continue
        parts = symbol.split(".")
        body = tree.body
        target = None
        for part in parts:
            target = next((node for node in body
                           if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                           and node.name == part), None)
            if target is None:
                break
            body = target.body if isinstance(target, ast.ClassDef) else []
        if not isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
            issues.append({"kind": "interface_contract", "module": module, "symbol": symbol,
                           "message": f"declared symbol '{symbol}' does not exist"})
            continue
        actual = [argument.arg for argument in list(target.args.posonlyargs) + list(target.args.args)
                  if argument.arg not in {"self", "cls"}]
        if expected and actual != expected:
            issues.append({"kind": "interface_contract", "module": module, "symbol": symbol,
                           "message": f"{module}.{symbol} parameters {actual} do not match frozen contract {expected}"})
        for consumer in _strings(interface.get("consumers")):
            consumer_path = (workspace / consumer).resolve()
            try:
                consumer_path.relative_to(workspace)
            except ValueError:
                consumer_path = Path("")
            if not consumer_path.is_file():
                issues.append({"kind": "interface_contract", "module": module, "symbol": symbol,
                               "message": f"declared consumer '{consumer}' does not exist"})
    return issues


def evaluate_delivery(
    root: str | Path,
    contract: Dict[str, Any],
    steps: List[Dict[str, Any]],
    tool_histories: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Evaluate the frozen contract from workspace and machine evidence."""
    workspace = Path(root).resolve()
    failures = []
    checks = []

    has_independent_step = any(
        str(step.get("role") or "") in INDEPENDENT_ROLES for step in steps
    )
    for row in contract.get("traceability", []):
        if (contract.get("software_delivery") and row.get("mandatory")
                and not row.get("implementation_steps")):
            failures.append(f"{row.get('requirement_id')} has no implementation owner")
        if (contract.get("software_delivery") and has_independent_step
                and row.get("mandatory") and not row.get("validation_steps")):
            failures.append(f"{row.get('requirement_id')} has no independent validation")
    checks.append({"name": "requirements_traceability", "passed": not failures})

    missing_artifacts = []
    for step in steps:
        for artifact in _strings(step.get("required_artifacts")):
            candidate = (workspace / artifact).resolve()
            try:
                candidate.relative_to(workspace)
            except ValueError:
                missing_artifacts.append(artifact)
                continue
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                missing_artifacts.append(artifact)
    if missing_artifacts:
        failures.append("missing artifacts: " + ", ".join(sorted(set(missing_artifacts))))
    checks.append({"name": "required_artifacts", "passed": not missing_artifacts})

    validation_specs = {
        str(item.get("path") or "").replace("\\", "/"): item
        for item in contract.get("artifact_validations", [])
        if isinstance(item, dict)
    }
    artifact_reports = []
    validation_failures = []
    validation_targets = {
        artifact.replace("\\", "/")
        for step in steps
        for artifact in _strings(step.get("required_artifacts"))
    }
    validation_targets.update(validation_specs)
    for normalized in sorted(validation_targets):
        candidate = (workspace / normalized).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError:
            validation_failures.append(f"{normalized}: path is outside the workspace")
            continue
        specification = validation_specs.get(normalized, {})
        if not candidate.is_file():
            if specification.get("required", False):
                validation_failures.append(f"{normalized}: required validation target is missing")
            continue
        report = validate_artifact(
            candidate,
            validator=specification.get("validator"),
            constraints=specification.get("constraints"),
        )
        report["artifact"] = normalized
        artifact_reports.append(report)
        if not report.get("valid", False):
            validation_failures.extend(
                f"{normalized}: {message}" for message in report.get("failures", [])
            )
    if validation_failures:
        failures.append(
            "artifact validation failed: " + "; ".join(validation_failures[:20])
        )
    checks.append({
        "name": "artifact_structure_and_constraints",
        "passed": not validation_failures,
        "reports": artifact_reports,
    })

    static_issues = (
        static_workspace_issues(workspace) if contract.get("software_delivery") else []
    )
    if static_issues:
        failures.append(
            f"{len(static_issues)} static integration issue(s): "
            + "; ".join(issue["message"] for issue in static_issues[:8])
        )
    checks.append({
        "name": "syntax_imports_signatures",
        "passed": not static_issues,
        "issues": static_issues[:50],
    })

    histories = list(tool_histories)
    interface_issues = declared_interface_issues(workspace, contract.get("interfaces", []))
    if interface_issues:
        failures.append(f"{len(interface_issues)} declared interface issue(s): "
                        + "; ".join(issue["message"] for issue in interface_issues[:8]))
    checks.append({"name": "declared_interfaces", "passed": not interface_issues,
                   "issues": interface_issues[:50]})
    missing_commands = []
    for item in contract.get("verification_commands", []):
        if not item.get("independent"):
            continue
        command = str(item.get("command") or "").strip()
        matched = any(
            commands_equivalent(
                command,
                str(entry.get("arguments", {}).get("command") or ""),
            )
            and "EXIT_CODE: 0" in str(entry.get("result") or "")
            for entry in histories
        )
        if not matched:
            missing_commands.append(command)
    if missing_commands:
        failures.append(
            "independent commands lack successful evidence: "
            + ", ".join(sorted(set(missing_commands)))
        )
    checks.append({
        "name": "independent_machine_evidence",
        "passed": not missing_commands,
        "missing_commands": sorted(set(missing_commands)),
    })

    missing_launches = []
    for command in contract.get("launch_commands", []):
        matched = any(
            commands_equivalent(
                command,
                str(entry.get("arguments", {}).get("command") or ""),
            )
            and "EXIT_CODE: 0" in str(entry.get("result") or "")
            for entry in histories
        )
        if not matched:
            missing_launches.append(command)
    if missing_launches:
        failures.append("launch smoke commands lack successful evidence: "
                        + ", ".join(sorted(set(missing_launches))))
    checks.append({"name": "real_launch_smoke", "passed": not missing_launches,
                   "missing_commands": sorted(set(missing_launches))})

    return {
        "schema_version": SCHEMA_VERSION,
        "contract_sha256": contract.get("contract_sha256"),
        "passed": not failures,
        "checks": checks,
        "failures": failures,
    }
