"""Plan normalization, roles and requirement helpers for execution."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

ROLE_DISPLAY_NAMES = {
    "architect": "Architecte",
    "security": "Analyste Sécurité",
    "developer": "Développeur",
    "qa": "Testeur QA",
    "debugger": "Débugueur",
    "writer": "Rédacteur Technique",
    "coordinator": "Coordinateur",
}

ROLE_ALIASES = {
    "architect": "architect", "architecte": "architect", "analyst": "architect", "analyste": "architect",
    "security": "security", "sécurité": "security", "reviewer": "security", "analyste sécurité": "security",
    "developer": "developer", "développeur": "developer", "coder": "developer", "codeur": "developer",
    "qa": "qa", "tester": "qa", "testeur": "qa", "testeur qa": "qa",
    "debugger": "debugger", "debug": "debugger", "débugueur": "debugger", "bug fixer": "debugger",
    "writer": "writer", "rédacteur": "writer", "rédacteur technique": "writer", "documentation": "writer",
    "coordinator": "coordinator", "coordinateur": "coordinator", "summary": "coordinator",
}

def canonical_step_role(value: Any) -> Optional[str]:
    if value is None:
        return None
    return ROLE_ALIASES.get(str(value).strip().lower())

def infer_step_role(description: str) -> Optional[str]:
    desc_lower = str(description or "").lower()
    # Debugger descriptions commonly contain "tests"; match them before QA.
    if any(marker in desc_lower for marker in ("debug", "bug fixer", "débug", "corriger les erreurs")):
        return "debugger"
    if any(marker in desc_lower for marker in ("architect", "architecte", "technical specification", "spécification technique")):
        return "architect"
    if any(marker in desc_lower for marker in ("security", "sécurité", "compliance reviewer", "revue de conformité")):
        return "security"
    if any(marker in desc_lower for marker in ("qa", "tester", "testeur", "testing engineer", "unit tests")):
        return "qa"
    if any(marker in desc_lower for marker in ("developer", "coder", "développeur", "codeur")):
        return "developer"
    if any(marker in desc_lower for marker in ("technical writer", "writer", "rédacteur", "documentation")):
        return "writer"
    return None

def parse_step_role(description: str) -> Optional[str]:
    role = infer_step_role(description)
    return ROLE_DISPLAY_NAMES.get(role) if role else None

def normalize_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a planner response and normalize its stable execution contract."""
    if not isinstance(plan, dict) or not isinstance(plan.get("steps"), list):
        raise ValueError("A plan must contain a list of steps.")
    steps = plan["steps"]
    identifiers = []
    identifier_keys = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"Plan step {index} must be an object.")
        step_id = step.get("id", index)
        identifier_key = str(step_id)
        if (
            isinstance(step_id, bool) or not isinstance(step_id, (int, str))
            or identifier_key in identifier_keys
        ):
            raise ValueError(f"Plan step {index} has an invalid or duplicate id.")
        identifiers.append(step_id)
        identifier_keys.add(identifier_key)
        step["id"] = step_id
        step["description"] = str(step.get("description") or "").strip()
        if not step["description"]:
            raise ValueError(f"Plan step {step_id} has no description.")
        dependencies = step.get("dependencies") or []
        if (
            not isinstance(dependencies, list)
            or any(isinstance(dep, bool) or not isinstance(dep, (int, str)) for dep in dependencies)
            or len(set(map(str, dependencies))) != len(dependencies)
        ):
            raise ValueError(f"Plan step {step_id} has invalid dependencies.")
        step["dependencies"] = dependencies
        requested_role = step.get("role")
        role = canonical_step_role(requested_role) if requested_role is not None else infer_step_role(step["description"])
        if requested_role is not None and not role:
            raise ValueError(f"Plan step {step_id} has unsupported role '{requested_role}'.")
        if role:
            step["role"] = role
        specialist = str(step.get("specialist") or "").strip()
        if specialist:
            if len(specialist) > 160:
                raise ValueError(f"Plan step {step_id} has an excessively long specialist title.")
            step["specialist"] = specialist
        for field in (
            "expertise", "required_artifacts", "acceptance_criteria",
            "verification_commands", "requirement_ids", "owned_paths",
        ):
            values = step.get(field) or []
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"Plan step {step_id} has an invalid {field} list.")
            step[field] = [value.strip() for value in values if value.strip()]
        step["status"] = step.get("status", "pending")

    identifier_set = set(identifiers)
    for step in steps:
        if step["id"] in step["dependencies"] or any(dep not in identifier_set for dep in step["dependencies"]):
            raise ValueError(f"Plan step {step['id']} references an invalid dependency.")

    completed = set()
    while len(completed) < len(steps):
        ready = [step["id"] for step in steps if step["id"] not in completed and set(step["dependencies"]) <= completed]
        if not ready:
            raise ValueError("Plan contains cyclical dependencies.")
        completed.update(ready)
    return plan


def merge_inherited_requirements(
    plan: Dict[str, Any], inherited: Any
) -> Dict[str, Any]:
    """Keep parent requirement identifiers valid in delegated child plans."""
    if not isinstance(inherited, list) or not inherited:
        return plan
    requirements = plan.get("requirements")
    if not isinstance(requirements, list):
        requirements = []
    else:
        requirements = list(requirements)
    known = {
        str(item.get("id"))
        for item in requirements
        if isinstance(item, dict) and item.get("id")
    }
    requirements.extend(
        dict(item) for item in inherited
        if isinstance(item, dict) and item.get("id") and str(item["id"]) not in known
    )
    plan["requirements"] = requirements
    return plan


def requirements_for_delegation(
    parent_requirements: Any, requirement_ids: Any
) -> List[Dict[str, Any]]:
    """Select complete requirement records, never bare identifiers, for a specialist."""
    if not isinstance(parent_requirements, list):
        return []
    requirements = [
        dict(requirement) for requirement in parent_requirements
        if isinstance(requirement, dict) and requirement.get("id")
    ]
    selected_ids = {
        str(requirement_id) for requirement_id in (requirement_ids or [])
        if str(requirement_id).strip()
    }
    if selected_ids:
        return [
            requirement for requirement in requirements
            if str(requirement.get("id")) in selected_ids
        ]
    return [
        requirement for requirement in requirements
        if requirement.get("mandatory", True)
    ]


def requirement_validation_commands(requirements: Any) -> List[str]:
    """Extract explicit machine-validation commands quoted in requirement text."""
    if not isinstance(requirements, list):
        return []
    commands = []
    validation_pattern = re.compile(
        r"(?i)(?:\bpytest\b|\bunittest\b|\bcompileall\b|"
        r"\bnpm\s+(?:run\s+)?test\b|\bcargo\s+test\b|\bgo\s+test\b|"
        r"\bdotnet\s+test\b|\bmvn(?:\.cmd)?\s+test\b|"
        r"\bgradle(?:w)?\s+test\b|\bruff\s+check\b|\bmypy\b|\btsc\b)"
    )
    delimiter = chr(96)
    quoted_command = re.compile(
        re.escape(delimiter) + r"([^" + re.escape(delimiter) + r"\r\n]+)"
        + re.escape(delimiter)
    )
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        texts = [str(requirement.get("statement") or "")]
        acceptance = requirement.get("acceptance")
        if isinstance(acceptance, list):
            texts.extend(str(item) for item in acceptance)
        for text in texts:
            for candidate in quoted_command.findall(text):
                command = candidate.strip()
                if validation_pattern.search(command) and command not in commands:
                    commands.append(command)
    return commands


def requirements_request_mutation(requirements: Any) -> bool:
    """Return whether the assignment explicitly asks for a durable edit."""
    if not isinstance(requirements, list):
        return False
    explicit_filesystem_edit = re.compile(
        r"(?i)\buse\s+(?:the\s+)?filesystem\s+(?:write|edit)\b"
    )
    direct_file_edit = re.compile(
        r"(?i)^\s*(?:please\s+)?(?:edit|modify|write|create|fix|repair|update|"
        r"delete|remove)\b[^\r\n]*(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+"
    )
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        texts = [str(requirement.get("statement") or "")]
        acceptance = requirement.get("acceptance")
        if isinstance(acceptance, list):
            texts.extend(str(item) for item in acceptance)
        if any(
            explicit_filesystem_edit.search(text) or direct_file_edit.search(text)
            for text in texts
        ):
            return True
    return False
