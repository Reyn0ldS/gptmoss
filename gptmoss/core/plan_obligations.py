"""Deterministic semantic obligations for adaptive delivery plans.

Plans remain free to contain one step or hundreds. The runtime controls the
meaning, causal order and evidence of required work instead of trusting an LLM
step count or a few words in a description.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Mapping

from gptmoss.core.corpus_policy import normalize_corpus_policy


INDEPENDENT_ROLES = {"qa", "debugger"}

SOURCE_INVENTORY = "source_inventory"
DOCUMENT_RENDER = "document_render"
IMPLEMENTATION = "implementation"
INDEPENDENT_VALIDATION = "independent_validation"
AUTONOMOUS_REPAIR = "autonomous_repair"
FINAL_AUDIT = "final_audit"

OBLIGATION_OPERATIONS = {
    SOURCE_INVENTORY: {"inventory", "extract"},
    DOCUMENT_RENDER: {"write", "render", "document_render"},
    IMPLEMENTATION: {"implement"},
    INDEPENDENT_VALIDATION: {"validate"},
    AUTONOMOUS_REPAIR: {"repair"},
    FINAL_AUDIT: {"audit"},
}
OPERATION_OBLIGATION = {
    operation: obligation
    for obligation, operations in OBLIGATION_OPERATIONS.items()
    for operation in operations
}

REASONS = {
    SOURCE_INVENTORY: "Attached local sources must be inventoried before conclusions or drafting.",
    DOCUMENT_RENDER: "A source-grounded writing assignment needs a concrete professional artifact.",
    IMPLEMENTATION: "Software delivery needs a concrete implementation owner.",
    INDEPENDENT_VALIDATION: "The deliverable needs evidence from an owner distinct from its producer.",
    AUTONOMOUS_REPAIR: "High-risk software work needs repair after independent checks.",
    FINAL_AUDIT: "Completion needs a final evidence auditor downstream of required work.",
}


def _strings(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _role(step: Mapping[str, Any]) -> str:
    return str(step.get("role") or "").strip().lower()


def _operation(step: Mapping[str, Any]) -> str:
    return str(step.get("operation") or "").strip().lower()


def _declared_obligations(step: Mapping[str, Any]) -> set[str]:
    return set(_strings(step.get("satisfies_obligations")))


def _structural_operation(step: Mapping[str, Any]) -> str:
    """Infer only from trusted structural fields, never arbitrary prose."""
    declared = _operation(step)
    if declared:
        return declared
    role = _role(step)
    if role == "developer":
        return "implement"
    if role == "writer":
        return "document_render"
    if role == "qa":
        return "validate"
    if role == "debugger":
        return "repair"
    if role == "coordinator":
        return "audit"
    specialist = str(step.get("specialist") or "").casefold()
    artifacts = " ".join(_strings(step.get("required_artifacts"))).casefold()
    if (
        any(marker in specialist for marker in (
            "corpus evidence", "source evidence", "corpus analyst",
        ))
        or any(marker in artifacts for marker in (
            "corpus-inventory", "source-inventory",
        ))
    ):
        return "inventory"
    return "execute"


def _matches(step: Mapping[str, Any], obligation_id: str) -> bool:
    if obligation_id in _declared_obligations(step):
        return True
    return _structural_operation(step) in OBLIGATION_OPERATIONS.get(obligation_id, set())


def matches_source_inventory(step: Dict[str, Any]) -> bool:
    return _matches(step, SOURCE_INVENTORY)


def matches_implementation(step: Dict[str, Any]) -> bool:
    return _matches(step, IMPLEMENTATION)


def matches_document_render(step: Dict[str, Any]) -> bool:
    return _matches(step, DOCUMENT_RENDER)


def matches_independent(step: Dict[str, Any]) -> bool:
    return _matches(step, INDEPENDENT_VALIDATION) and _role(step) == "qa"


def matches_repair(step: Dict[str, Any]) -> bool:
    return _matches(step, AUTONOMOUS_REPAIR) and _role(step) == "debugger"


def matches_audit(step: Dict[str, Any]) -> bool:
    return _matches(step, FINAL_AUDIT) and _role(step) == "coordinator"


def _producers(steps: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        step for step in steps
        if matches_implementation(step) or matches_document_render(step)
    ]


def _validators(steps: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [step for step in steps if matches_independent(step)]


MATCHERS: Dict[str, Callable[[List[Dict[str, Any]]], bool]] = {
    SOURCE_INVENTORY: lambda steps: any(matches_source_inventory(step) for step in steps),
    DOCUMENT_RENDER: lambda steps: any(matches_document_render(step) for step in steps),
    IMPLEMENTATION: lambda steps: any(matches_implementation(step) for step in steps),
    INDEPENDENT_VALIDATION: lambda steps: bool(_producers(steps)) and bool(_validators(steps)),
    AUTONOMOUS_REPAIR: lambda steps: any(matches_repair(step) for step in steps),
    FINAL_AUDIT: lambda steps: any(matches_audit(step) for step in steps),
}


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "full"}
    return bool(value)


def _count(profile: Any, *keys: str) -> int:
    if not isinstance(profile, Mapping):
        return 0
    return sum(max(0, int(profile.get(key) or 0)) for key in keys)


def _domains(analysis: Any) -> set[str]:
    if not isinstance(analysis, Mapping):
        return set()
    return {str(item) for item in (analysis.get("domains") or []) if item}


def collect_plan_obligations(
    *,
    task: str = "",
    planning_mode: str = "auto",
    analysis: Dict[str, Any] | None = None,
    workload_profile: Dict[str, Any] | None = None,
    corpus_auto_workflow: bool = False,
    corpus_policy: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """Select semantic gates; complexity never imposes a step quota."""
    from gptmoss.planners.complexity import (
        normalize_planning_mode,
        requires_software_implementation,
    )
    from gptmoss.planners.fallbacks import _document_deliverable_task

    mode = normalize_planning_mode(planning_mode)
    analysis = analysis if isinstance(analysis, dict) else {}
    domains = _domains(analysis)
    level = str(analysis.get("level") or "low")
    high = level in {"high", "very_high"}
    software = (
        "software-engineering" in domains
        and requires_software_implementation(task, analysis)
    )
    document_task = _document_deliverable_task(
        task, workload=workload_profile, corpus_policy=corpus_policy,
    )
    has_sources = _count(
        workload_profile, "attachment_count", "document_count", "image_count"
    ) > 0
    policy = normalize_corpus_policy(
        corpus_policy,
        enabled=(
            corpus_policy.get("enabled")
            if isinstance(corpus_policy, Mapping) and "enabled" in corpus_policy
            else corpus_auto_workflow
        ),
        professional_delivery=(
            corpus_policy.get("professional_delivery")
            if isinstance(corpus_policy, Mapping) and "professional_delivery" in corpus_policy
            else (corpus_auto_workflow or document_task)
        ),
        workload_profile=workload_profile,
    )
    source_workflow = has_sources and (policy["enabled"] or document_task)
    professional_delivery = document_task or bool(policy["professional_delivery"])

    selected: List[str] = []
    if source_workflow:
        selected.append(SOURCE_INVENTORY)
    if source_workflow and professional_delivery:
        selected.append(DOCUMENT_RENDER)
    if software:
        selected.append(IMPLEMENTATION)
    if mode != "direct":
        if software and (mode == "full_team" or (mode == "auto" and high)):
            selected.append(AUTONOMOUS_REPAIR)
        if software or professional_delivery or high or mode in {"short_team", "full_team"}:
            selected.extend((INDEPENDENT_VALIDATION, FINAL_AUDIT))

    seen: set[str] = set()
    obligations: List[Dict[str, Any]] = []
    for identifier in selected:
        if identifier in seen:
            continue
        seen.add(identifier)
        obligations.append({
            "id": identifier,
            "required": True,
            "reason": REASONS[identifier],
            "coalesced": mode == "direct",
        })
    return obligations


def matching_steps(
    steps: Iterable[Dict[str, Any]], obligation_id: str
) -> List[Dict[str, Any]]:
    materialized = [step for step in steps if isinstance(step, dict)]
    return [step for step in materialized if _matches(step, obligation_id)]


def _step_map(steps: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(step.get("id")): step for step in steps if isinstance(step, dict)}


def _ancestors(step: Mapping[str, Any], by_id: Mapping[str, Dict[str, Any]]) -> set[str]:
    pending = [str(item) for item in step.get("dependencies", [])]
    found: set[str] = set()
    while pending:
        identifier = pending.pop()
        if identifier in found:
            continue
        found.add(identifier)
        dependency = by_id.get(identifier)
        if dependency:
            pending.extend(str(item) for item in dependency.get("dependencies", []))
    return found


def obligation_issues(
    steps: Iterable[Dict[str, Any]], obligations: Iterable[Dict[str, Any]]
) -> List[str]:
    """Return missing, causal and evidence defects in a materialized DAG."""
    materialized = [step for step in steps if isinstance(step, dict)]
    required = [
        dict(item) for item in obligations
        if isinstance(item, dict) and item.get("required", True) and item.get("id")
    ]
    coalesced = bool(required) and all(item.get("coalesced") for item in required)
    by_obligation = {
        str(item["id"]): matching_steps(materialized, str(item["id"]))
        for item in required
    }
    issues = [
        f"missing:{identifier}"
        for identifier, matches in by_obligation.items() if not matches
    ]
    if issues or coalesced:
        return issues

    by_id = _step_map(materialized)
    source_ids = {str(step["id"]) for step in by_obligation.get(SOURCE_INVENTORY, [])}
    for step in by_obligation.get(DOCUMENT_RENDER, []):
        if source_ids and not (_ancestors(step, by_id) & source_ids):
            issues.append(f"causal:{DOCUMENT_RENDER}:requires:{SOURCE_INVENTORY}")
            break

    producers = _producers(materialized)
    validators = by_obligation.get(INDEPENDENT_VALIDATION, [])
    if validators:
        producer_ids = {str(step["id"]) for step in producers}
        causally_independent = False
        for validator in validators:
            ancestors = _ancestors(validator, by_id)
            upstream = [step for step in producers if str(step["id"]) in ancestors]
            if upstream and all(
                str(step.get("specialist") or "").casefold()
                != str(validator.get("specialist") or "").casefold()
                for step in upstream
            ):
                causally_independent = True
                break
        if producer_ids and not causally_independent:
            issues.append(
                f"causal:{INDEPENDENT_VALIDATION}:requires_distinct_upstream_producer"
            )

    validation_ids = {str(step["id"]) for step in validators}
    for repair in by_obligation.get(AUTONOMOUS_REPAIR, []):
        if validation_ids and not (_ancestors(repair, by_id) & validation_ids):
            issues.append(f"causal:{AUTONOMOUS_REPAIR}:requires:{INDEPENDENT_VALIDATION}")
            break

    audits = by_obligation.get(FINAL_AUDIT, [])
    if audits:
        required_upstream = {
            str(step["id"])
            for identifier, matched in by_obligation.items()
            if identifier != FINAL_AUDIT
            for step in matched
        }
        if required_upstream and not any(
            required_upstream <= _ancestors(audit, by_id) for audit in audits
        ):
            issues.append(f"causal:{FINAL_AUDIT}:not_downstream_of_required_work")

    evidence_rules = {
        SOURCE_INVENTORY: lambda step: bool(
            step.get("required_artifacts") and step.get("acceptance_criteria")
        ),
        DOCUMENT_RENDER: lambda step: bool(
            step.get("required_artifacts") and step.get("acceptance_criteria")
        ),
        INDEPENDENT_VALIDATION: lambda step: bool(
            step.get("acceptance_criteria") and step.get("required_evidence")
        ),
        FINAL_AUDIT: lambda step: bool(
            step.get("acceptance_criteria") and step.get("required_evidence")
        ),
    }
    for identifier, rule in evidence_rules.items():
        matched = by_obligation.get(identifier, [])
        if matched and not any(rule(step) for step in matched):
            issues.append(f"evidence:{identifier}:missing_machine_contract")
    return list(dict.fromkeys(issues))


def unsatisfied_obligations(
    steps: Iterable[Dict[str, Any]], obligations: Iterable[Dict[str, Any]]
) -> List[str]:
    """Return obligation identifiers affected by any semantic defect."""
    identifiers: List[str] = []
    for issue in obligation_issues(steps, obligations):
        parts = issue.split(":")
        identifier = parts[1] if parts[0] in {"causal", "evidence"} else parts[-1]
        if identifier in REASONS and identifier not in identifiers:
            identifiers.append(identifier)
    return identifiers


def validate_plan_obligations(
    steps: Iterable[Dict[str, Any]], obligations: Iterable[Dict[str, Any]]
) -> None:
    issues = obligation_issues(steps, obligations)
    if issues:
        missing = [item.split(":", 1)[1] for item in issues if item.startswith("missing:")]
        prefix = (
            "Plan is missing required delivery obligations: " + ", ".join(missing)
            if missing else "Plan has invalid delivery obligation structure"
        )
        raise ValueError(prefix + " (" + "; ".join(issues) + ")")


def _next_identifier(steps: Iterable[Dict[str, Any]], stem: str) -> str:
    used = {str(step.get("id")) for step in steps}
    candidate = stem
    index = 2
    while candidate in used:
        candidate = f"{stem}-{index}"
        index += 1
    return candidate


def _delivery_artifact(plan: Mapping[str, Any]) -> str:
    primary = str(plan.get("primary_artifact") or "").strip()
    if primary:
        return primary.replace("\\", "/")
    for item in plan.get("artifact_validations") or []:
        if isinstance(item, Mapping) and item.get("required", True) and item.get("path"):
            path = str(item["path"]).replace("\\", "/")
            if PurePosixPath(path).suffix.lower() in {
                ".md", ".txt", ".html", ".docx", ".pdf",
            }:
                return path
    return "deliverables/professional-report.md"


def _repair_step(
    steps: List[Dict[str, Any]],
    obligation_id: str,
    dependencies: Iterable[Any],
    *,
    artifact: str | None = None,
) -> Dict[str, Any]:
    templates = {
        SOURCE_INVENTORY: (
            "architect", "Local Corpus Evidence Analyst", "inventory",
            "Inventory every assigned local source with the documents capability; preserve paths, cover all blocks and images, search each decision topic, and record contradictions, gaps and unreadable items without Internet evidence.",
            "Every assigned source has a coverage, evidence, unsupported or unreadable state.",
            ["source_coverage", "local_provenance"],
        ),
        DOCUMENT_RENDER: (
            "writer", "Professional Evidence-grounded Writer", "document_render",
            "Produce the requested professional deliverable from consolidated local evidence, with bounded citations, diagrams where useful, traceability and explicit unsupported claims.",
            "The concrete deliverable is coherent, source-grounded and passes its artifact validator.",
            ["artifact_validation", "source_to_section_coverage"],
        ),
        IMPLEMENTATION: (
            "developer", "Implementation Engineer", "implement",
            "Implement the requested behavior through the project public contracts without mock substitution.",
            "The requested behavior is runnable through its real public entry point.",
            ["implementation_artifacts"],
        ),
        INDEPENDENT_VALIDATION: (
            "qa", "Independent Acceptance Validator", "validate",
            "Independently validate produced artifacts and public behavior against every mandatory requirement without editing producer-owned outputs.",
            "Every mandatory outcome has concrete independent pass/fail evidence.",
            ["artifact_validation", "independent_tool_evidence"],
        ),
        AUTONOMOUS_REPAIR: (
            "debugger", "Autonomous Repair Engineer", "repair",
            "Repair root causes reported by independent validation and rerun affected acceptance checks without weakening requirements.",
            "All critical failures are corrected or truthfully escalated with evidence.",
            ["repair_history", "regression_validation"],
        ),
        FINAL_AUDIT: (
            "coordinator", "Final Evidence Auditor", "audit",
            "Audit requirements, artifacts, source coverage, validation evidence, repairs, approved scope changes and residual risks before completion.",
            "No unsupported completion claim or unmapped mandatory requirement remains.",
            ["requirements_traceability", "delivery_assurance"],
        ),
    }
    role, specialist, operation, description, acceptance, evidence = templates[obligation_id]
    required_artifacts: List[str] = []
    if obligation_id == SOURCE_INVENTORY:
        required_artifacts = ["analysis/corpus-inventory.md"]
    elif obligation_id == DOCUMENT_RENDER and artifact:
        required_artifacts = [artifact]
    elif obligation_id == INDEPENDENT_VALIDATION:
        required_artifacts = ["analysis/independent-validation.md"]
    step = {
        "id": _next_identifier(steps, f"required-{obligation_id.replace('_', '-')}"),
        "role": role,
        "specialist": specialist,
        "description": description,
        "dependencies": list(dict.fromkeys(dependencies)),
        "expertise": [operation, "evidence-based delivery"],
        "required_artifacts": required_artifacts,
        "acceptance_criteria": [acceptance],
        "verification_commands": [],
        "requirement_ids": [],
        "owned_paths": list(required_artifacts),
        "operation": operation,
        "satisfies_obligations": [obligation_id],
        "required_evidence": evidence,
        "status": "pending",
        "runtime_inserted": True,
    }
    steps.append(step)
    return step


def _decorate_structural_fields(step: Dict[str, Any]) -> None:
    operation = _structural_operation(step)
    step["operation"] = operation
    obligation = OPERATION_OBLIGATION.get(operation)
    declared = _strings(step.get("satisfies_obligations"))
    if obligation and obligation not in declared:
        declared.append(obligation)
    step["satisfies_obligations"] = declared
    evidence = _strings(step.get("required_evidence"))
    defaults = {
        SOURCE_INVENTORY: ["source_coverage", "local_provenance"],
        DOCUMENT_RENDER: ["artifact_validation", "source_to_section_coverage"],
        IMPLEMENTATION: ["implementation_artifacts"],
        INDEPENDENT_VALIDATION: ["artifact_validation", "independent_tool_evidence"],
        AUTONOMOUS_REPAIR: ["repair_history", "regression_validation"],
        FINAL_AUDIT: ["requirements_traceability", "delivery_assurance"],
    }
    acceptance_defaults = {
        SOURCE_INVENTORY: "Every assigned source has an explicit coverage or error state.",
        DOCUMENT_RENDER: "The concrete deliverable passes artifact and source-grounding checks.",
        IMPLEMENTATION: "The requested behavior is runnable through a real public entry point.",
        INDEPENDENT_VALIDATION: "Independent evidence covers every mandatory produced outcome.",
        AUTONOMOUS_REPAIR: "Critical validation failures are repaired or truthfully escalated.",
        FINAL_AUDIT: "No unsupported completion claim or unmapped mandatory requirement remains.",
    }
    for identifier in declared:
        for item in defaults.get(identifier, []):
            if item not in evidence:
                evidence.append(item)
    step["required_evidence"] = evidence
    if declared and not _strings(step.get("acceptance_criteria")):
        step["acceptance_criteria"] = [
            acceptance_defaults[identifier]
            for identifier in declared if identifier in acceptance_defaults
        ]


def _add_dependencies(
    step: Dict[str, Any], dependencies: Iterable[Any],
    *, steps: Iterable[Dict[str, Any]] | None = None,
) -> None:
    own = str(step.get("id"))
    current = list(step.get("dependencies") or [])
    known = {str(item) for item in current}
    by_id = _step_map(steps or [])
    for dependency in dependencies:
        dependency_id = str(dependency)
        dependency_step = by_id.get(dependency_id)
        would_cycle = bool(
            dependency_step and own in _ancestors(dependency_step, by_id)
        )
        if dependency_id != own and dependency_id not in known and not would_cycle:
            current.append(dependency)
            known.add(dependency_id)
    step["dependencies"] = current


def repair_plan_obligations(
    plan: Dict[str, Any], obligations: Iterable[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Complete only missing semantic gates and repair their causal ordering."""
    # Preserve object identity: runtime specialization and tests retain direct
    # references to plan steps while the repair pass enriches them in place.
    steps = [step for step in plan.get("steps") or [] if isinstance(step, dict)]
    required = [dict(item) for item in obligations if isinstance(item, dict)]
    if not steps:
        plan["steps"] = steps
        return steps
    for step in steps:
        _decorate_structural_fields(step)

    if required and all(item.get("coalesced") for item in required):
        step = steps[0]
        identifiers = [str(item["id"]) for item in required]
        step["satisfies_obligations"] = list(dict.fromkeys([
            *_strings(step.get("satisfies_obligations")), *identifiers,
        ]))
        step["required_evidence"] = list(dict.fromkeys([
            *_strings(step.get("required_evidence")), "structured_delivery",
            *(["source_coverage"] if SOURCE_INVENTORY in identifiers else []),
            *(["artifact_validation"] if DOCUMENT_RENDER in identifiers else []),
        ]))
        artifacts = _strings(step.get("required_artifacts"))
        if SOURCE_INVENTORY in identifiers:
            artifacts.append("analysis/corpus-inventory.md")
        if DOCUMENT_RENDER in identifiers:
            artifacts.append(_delivery_artifact(plan))
        step["required_artifacts"] = list(dict.fromkeys(artifacts))
        step["owned_paths"] = list(dict.fromkeys([
            *_strings(step.get("owned_paths")), *step["required_artifacts"],
        ]))
        plan["steps"] = steps
        return steps

    required_ids = {
        str(item.get("id")) for item in required if item.get("required", True)
    }
    if SOURCE_INVENTORY in required_ids and not matching_steps(steps, SOURCE_INVENTORY):
        _repair_step(steps, SOURCE_INVENTORY, [])
    source_steps = matching_steps(steps, SOURCE_INVENTORY)
    for source_step in source_steps:
        if not source_step.get("required_artifacts"):
            source_step["required_artifacts"] = ["analysis/corpus-inventory.md"]
            source_step["owned_paths"] = list(source_step["required_artifacts"])
    source_ids = [step["id"] for step in source_steps]

    if IMPLEMENTATION in required_ids and not matching_steps(steps, IMPLEMENTATION):
        _repair_step(steps, IMPLEMENTATION, source_ids)

    if DOCUMENT_RENDER in required_ids and not matching_steps(steps, DOCUMENT_RENDER):
        upstream = [
            step["id"] for step in steps
            if matches_source_inventory(step) or matches_implementation(step)
        ]
        _repair_step(
            steps, DOCUMENT_RENDER, upstream, artifact=_delivery_artifact(plan)
        )
    if DOCUMENT_RENDER in required_ids:
        for render_step in matching_steps(steps, DOCUMENT_RENDER):
            if not render_step.get("required_artifacts"):
                render_step["required_artifacts"] = [_delivery_artifact(plan)]
                render_step["owned_paths"] = list(render_step["required_artifacts"])

    if source_ids:
        for step in steps:
            if matches_implementation(step) or matches_document_render(step):
                _add_dependencies(step, source_ids)

    producers: List[Dict[str, Any]] = []
    if IMPLEMENTATION in required_ids:
        producers.extend(matching_steps(steps, IMPLEMENTATION))
    if DOCUMENT_RENDER in required_ids:
        producers.extend(matching_steps(steps, DOCUMENT_RENDER))
    if not producers:
        producers = _producers(steps)
    producers = list({str(step["id"]): step for step in producers}.values())
    producer_ids = [step["id"] for step in producers]
    if (
        INDEPENDENT_VALIDATION in required_ids
        and not matching_steps(steps, INDEPENDENT_VALIDATION)
    ):
        _repair_step(steps, INDEPENDENT_VALIDATION, producer_ids)
    validators = matching_steps(steps, INDEPENDENT_VALIDATION)
    for validator in validators:
        _add_dependencies(validator, producer_ids, steps=steps)

    validator_ids = [step["id"] for step in validators]
    if AUTONOMOUS_REPAIR in required_ids and not matching_steps(steps, AUTONOMOUS_REPAIR):
        _repair_step(steps, AUTONOMOUS_REPAIR, validator_ids)
    repairs = matching_steps(steps, AUTONOMOUS_REPAIR)
    for repair in repairs:
        _add_dependencies(repair, validator_ids, steps=steps)

    if FINAL_AUDIT in required_ids:
        audits = matching_steps(steps, FINAL_AUDIT)
        dependents = {
            str(dependency)
            for step in steps for dependency in step.get("dependencies", [])
        }
        sink_audits = [step for step in audits if str(step["id"]) not in dependents]
        if not sink_audits:
            sink_audits = [_repair_step(steps, FINAL_AUDIT, [])]
        upstream_ids = [step["id"] for step in steps if not matches_audit(step)]
        final_audit = sink_audits[-1]
        _add_dependencies(final_audit, upstream_ids, steps=steps)
        # The executor exposes the last delivery as final_output; keep the
        # causally final auditor last without changing any step count.
        if steps[-1] is not final_audit:
            steps.remove(final_audit)
            steps.append(final_audit)

    plan["steps"] = steps
    return steps


def attach_plan_obligations(
    plan: Dict[str, Any],
    *,
    task: str = "",
    planning_mode: str | None = None,
    analysis: Dict[str, Any] | None = None,
    workload_profile: Dict[str, Any] | None = None,
    corpus_auto_workflow: bool = False,
    corpus_policy: Dict[str, Any] | None = None,
    repair: bool = False,
    validate: bool = True,
) -> List[Dict[str, Any]]:
    """Store, optionally repair, and enforce the obligation snapshot."""
    obligations = collect_plan_obligations(
        task=task,
        planning_mode=planning_mode or str(plan.get("planning_mode") or "auto"),
        analysis=analysis if analysis is not None else plan.get("analysis"),
        workload_profile=(
            workload_profile if workload_profile is not None
            else plan.get("workload_profile")
        ),
        corpus_auto_workflow=corpus_auto_workflow,
        corpus_policy=(
            corpus_policy if corpus_policy is not None else plan.get("corpus_policy")
        ),
    )
    plan["plan_obligations"] = obligations
    if repair:
        repair_plan_obligations(plan, obligations)
    if validate:
        validate_plan_obligations(plan.get("steps") or [], obligations)
    return obligations
