"""Adaptive LLM planner with deterministic complexity safeguards."""

import json
import logging
import os
import re
from typing import Any, Dict, List

from gptmoss.interfaces.llm import LLMProvider
from gptmoss.interfaces.planner import PlannerProvider
from gptmoss.core.document_planning import adapt_document_steps
from gptmoss.core.domains import ProjectDomainRegistry
from gptmoss.planners.complexity import analyze_task_complexity, normalize_planning_mode
from gptmoss.planners.fallbacks import (
    _assign_document_requirements,
    _document_deliverable_task,
    _document_validation_policy,
    _requested_output_artifacts,
    _step,
    _supporting_document_validation_policies,
)

logger = logging.getLogger("gptmoss.planners.simple")

class SimplePlanner(PlannerProvider):
    """Generate an adaptive specialist DAG and reject undersized plans."""

    def __init__(self, llm_provider: LLMProvider,
                 domain_registry: ProjectDomainRegistry | None = None):
        self.llm_provider = llm_provider
        self.domain_registry = domain_registry or ProjectDomainRegistry()

    @staticmethod
    def _cross_domain_fallback(task: str, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Build a sizeable fallback without assuming a package, engine, or desktop tool."""
        domains = sorted(set(analysis.get("domains", [])))
        domain_text = ", ".join(domains) or "general"
        requirements = [{
            "id": "REQ-DELIVERY",
            "statement": str(task).strip(),
            "priority": "must",
            "mandatory": True,
            "source": "user",
            "acceptance": ["Every requested outcome is traced to implementation or an explicitly approved scope change."],
        }]
        steps = [
            _step(0, "architect", "Requirements & Feasibility Analyst",
                  "Extract exact outcomes, constraints, unavailable assets, risks, and measurable acceptance criteria.",
                  [], ["requirements engineering", *domains], ["specs/requirements.md"],
                  ["Requirements preserve the user's requested scope and identify external dependencies."]),
            _step(1, "architect", "Cross-Domain Systems Architect",
                  "Design project modules, interfaces, data flow, formats, coordinate or unit conventions, and recovery boundaries.",
                  [0], ["systems architecture", *domains], ["specs/architecture.md"],
                  ["Every requirement maps to a concrete producer, consumer, and independently verifiable output."]),
            _step(2, "architect", "External Tool Contract Engineer",
                  "Describe project-specific engines, runtimes, models, and desktop tools as configuration-driven external_tools and execution_routines: availability probes, parameters, exact non-interactive commands or APIs, outputs, rollback, and validation. Do not claim GUI operation or execution without evidence.",
                  [0, 1], ["tool integration", "configuration management", "operational runbooks"],
                  ["specs/external-tools.md"], ["A human can configure and run each external dependency without GPTMOSS assuming direct control."]),
            _step(3, "security", "Data Safety & Privacy Reviewer",
                  "Review inputs, generated artifacts, credentials, personal data, filesystem boundaries, and dependency risks.",
                  [0, 1], ["privacy", "threat modeling", "safe file handling"], ["specs/safety.md"],
                  ["Risks have actionable controls appropriate to the detected domains."]),
            _step(4, "developer", "Core Domain Implementation Engineer",
                  "Implement deterministic validated domain models and transformations behind explicit public interfaces; never substitute random, mocked, or fabricated capability.",
                  [1, 3], ["domain implementation", *domains], [],
                  ["Core behavior is runnable and rejects invalid or non-finite data."]),
            _step(5, "developer", "Input & Output Pipeline Engineer",
                  "Implement validated import/export paths, provenance, units, schema checks, and transactional failure handling for requested formats.",
                  [1, 3, 4], ["data pipelines", "artifact validation", *domains], [],
                  ["Requested outputs are structurally valid and independently inspectable."]),
            _step(6, "developer", "Adapter & Configuration Engineer",
                  "Implement adapter boundaries and configuration templates for optional models, engines, hardware, or services while keeping the local core truthful when they are absent.",
                  [2, 4, 5], ["adapter design", "runtime configuration"], [],
                  ["Unavailable external capability yields an actionable routine, not a false success."]),
            _step(7, "developer", "Interface & Workflow Engineer",
                  "Expose the requested API, CLI, UI, or automation workflows through the same validated implementation.",
                  [4, 5, 6], ["workflow integration", "public interfaces"], [],
                  ["All requested entry points use coherent shared contracts."]),
            _step(8, "qa", "Independent Contract Test Engineer",
                  "Create boundary, format, interface, determinism, failure, and regression tests against public modules without replicated implementation or mocks of the subject.",
                  [4, 5, 6], ["contract testing", "property testing", *domains],
                  ["tests/test_acceptance.py"], ["Tests reject empty, malformed, non-finite, inconsistent, and unsupported outputs."],
                  ["python -m pytest --collect-only -q"]),
            _step(9, "debugger", "Autonomous Integration Repair Engineer",
                  "Run the complete unit and integration suite, inspect concrete failures, repair root causes, and rerun until green.",
                  [7, 8], ["root-cause analysis", "cross-component integration"], [],
                  ["The complete unit and integration suite exits with code 0."], ["python -m pytest -q"]),
            _step(10, "qa", "Clean-Process Acceptance Engineer",
                  "Run complete public user journeys in a fresh process with representative fixtures and independently validate all generated artifacts that can be checked locally.",
                  [9], ["end-to-end testing", "artifact inspection", *domains],
                  ["tests/test_end_to_end.py"], ["Local acceptance produces repeatable evidence without pretending to operate unavailable external tools."],
                  ["python -m pytest -q"]),
            _step(11, "writer", "Configuration & Operations Writer",
                  "Document setup, adaptive parameters, external tool routines, exact commands, expected outputs, troubleshooting, rollback, limitations, and manual validation steps.",
                  [2, 7, 10], ["technical writing", "operations", *domains], ["README.md"],
                  ["A new user can reproduce local checks and perform deferred external-tool checks."]),
            _step(12, "coordinator", "Final Requirement Traceability Auditor",
                  "Audit requirements against files, structural validators, executed commands, and approved scope changes; report uncertainty without overstating quality.",
                  [10, 11], ["delivery audit", "traceability"], [],
                  ["No completion or quality claim lacks concrete evidence."]),
        ]
        for step in steps:
            step["requirement_ids"] = ["REQ-DELIVERY"]
        return {
            "analysis": {**analysis, "workstreams": [step["specialist"] for step in steps]},
            "requirements": requirements,
            "scope_changes": [],
            "interfaces": [],
            "external_tools": [],
            "execution_routines": [],
            "artifact_validations": [],
            "launch_commands": [],
            "steps": steps,
            "rationale": f"Generic adaptive fallback for {domain_text}; no project package or external engine is assumed.",
        }

    @staticmethod
    def _document_fallback(task: str, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Preserve a source-grounded document delivery when LLM planning is unusable."""
        outputs = _requested_output_artifacts(task)
        primary = next(
            (
                item for item in outputs
                if item.casefold() in {"architecture.md", "dossier.md", "deliverable.md"}
            ),
            None,
        )
        if primary is None:
            primary = next(
                (
                    item for item in outputs
                    if item.casefold().endswith(".md")
                    and not any(
                        marker in item.casefold()
                        for marker in ("matrix", "matrice", "report", "rapport", "review", "audit")
                    )
                ),
                "deliverable.md",
            )
        if primary not in outputs:
            outputs.insert(0, primary)

        def select(*markers: str) -> List[str]:
            return [
                item for item in outputs
                if item != primary and any(marker in item.casefold() for marker in markers)
            ]

        matrices = select("requirement", "exigence", "evidence", "preuve", "matrix", "matrice")
        decisions = select("decision", "adr")
        quality = select("quality", "qualite", "qualité")
        review = select("review", "audit", "revue")
        assigned = {primary, *matrices, *decisions, *quality, *review}
        remaining = [item for item in outputs if item not in assigned]

        steps = [
            _step(
                0, "architect", "Local Corpus Evidence Analyst",
                "Inventory every explicit attachment with the documents capability, inspect all formats and boundaries, search each decision topic, and record source coverage without using Internet evidence.",
                [], ["document analysis", "local provenance", "coverage auditing"],
                ["analysis/corpus-inventory.md"],
                ["Every attached source, block range or slide range, contradiction, and unread area is explicit."],
            ),
            _step(
                1, "architect", "Requirements & Traceability Architect",
                "Extract all requirements, constraints, acceptance gates, decisions, risks, and open questions from the inventoried corpus; build complete source-to-section and requirement coverage matrices.",
                [0], ["requirements engineering", "traceability", "evidence matrices"],
                matrices or ["analysis/requirements-and-evidence.md"],
                ["Every mandatory source identifier is mapped to evidence and a planned deliverable section."],
            ),
            _step(
                2, "architect", "Architecture Decision Analyst",
                "Resolve or explicitly escalate source contradictions, compare alternatives against stated drivers, and record proposed decisions, consequences, risks, owners, and validation evidence.",
                [0, 1], ["architecture decisions", "trade-off analysis", "governance"],
                decisions or ["analysis/decision-register.md"],
                ["Contradictions remain visible and every proposed decision has authority and validation status."],
            ),
            _step(
                3, "architect", "Application, Integration & Data Architect",
                "Design consistent context, logical component, interface, data ownership, lifecycle, ingestion, indexing, retrieval, and generation views grounded in local evidence.",
                [1, 2], ["application architecture", "integration", "data architecture"],
                ["analysis/application-data-architecture.md"],
                ["Components, flows, contracts, failure behavior, provenance, and data lifecycle are implementable and mutually consistent."],
            ),
            _step(
                4, "security", "Identity, Security & Privacy Architect",
                "Threat-model the proposed architecture; specify identity, authorization-before-content, trust zones, secrets, audit, prompt-injection controls, privacy, retention, and residual risk using source evidence.",
                [1, 2, 3], ["zero trust", "privacy", "threat modeling", "audit"],
                ["analysis/security-privacy-architecture.md"],
                ["Every material security requirement has a control, verification method, owner, and residual risk."],
            ),
            _step(
                5, "architect", "Platform, Capacity & SRE Architect",
                "Design deployment, capacity, scaling, observability, backup, recovery, failover, support, and continuity; reconcile conflicting service targets with measurable tiers.",
                [1, 2, 3, 4], ["platform engineering", "capacity", "SRE", "continuity"],
                ["analysis/platform-sre-architecture.md"],
                ["Capacity and resilience decisions use sourced volumes, concurrency, RPO/RTO, tests, and operational ownership."],
            ),
            _step(
                6, "architect", "Migration & Operating Model Architect",
                "Define phased coexistence, reconciliation, checkpoints, rollback, decommissioning, responsibilities, decision forums, readiness gates, roadmap, costs, and unresolved prerequisites.",
                [1, 2, 3, 4, 5], ["migration", "operating model", "roadmaps", "rollback"],
                ["analysis/migration-operating-model.md"],
                ["Each phase has entry/exit evidence, rollback, reconciliation, ownership, and measurable acceptance."],
            ),
            _step(
                7, "writer", "Professional Architecture Dossier Editor",
                "Synthesize the approved analyses into the requested long-form primary document and any remaining outputs. Preserve exact required headings, stable identifiers, nearby bounded local references, terminology, distinctions between fact and recommendation, and non-repetitive professional prose.",
                [1, 2, 3, 4, 5, 6],
                ["long-form technical writing", "architecture communication", "source grounding"],
                [primary, *remaining],
                ["The primary document is complete, coherent, readable, locally sourced, and satisfies all declared structural and minimum-content constraints."],
            ),
            _step(
                8, "qa", "Independent Document Quality Analyst",
                "Read the actual requested outputs, run a clean independent coverage and provenance review, identify exact missing headings, identifiers, traceability rows, source bounds, unsupported claims, repetitions, placeholders, terminology conflicts, and inconsistencies.",
                [7], ["document QA", "provenance audit", "quality gates"],
                ["analysis/document-quality-findings.md"],
                ["Findings cite exact files and locations and include actionable repair instructions; no self-authored quality claim is accepted as evidence."],
            ),
            _step(
                9, "debugger", "Autonomous Document Repair Editor",
                "Repair the primary document and non-QA supporting artifacts from concrete independent findings, then reread the affected sections and remove all critical validation failures without weakening the requested policy.",
                [8], ["document repair", "root-cause correction", "cross-section consistency"],
                [], ["All critical findings are corrected or explicitly escalated with a truthful reason."],
            ),
            _step(
                10, "writer", "Quality Evidence & Review Editor",
                "Produce the requested frozen policy, machine-readable quality report, readable synthesis, and review report from the corrected files and actual independent findings. Never claim a passing result that the evidence does not support.",
                [9], ["quality evidence", "professional reporting", "residual risk"],
                [*quality, *review] or ["quality-policy.json", "quality-report.json", "quality-report.md", "review-report.md"],
                ["The reports are truthful, internally consistent, and derived from the actual final files and independent findings."],
            ),
            _step(
                11, "qa", "Final Deterministic Delivery Reviewer",
                "Independently inspect every final requested file, rerun the declared document policy conceptually against actual content, compare the JSON and Markdown quality reports, and record exact pass/fail evidence without editing author-owned outputs.",
                [10], ["deterministic validation", "acceptance audit", "residual risk"],
                ["analysis/final-delivery-audit.md"],
                ["Every mandatory file, policy result, traceability claim, contradiction, and residual risk is independently checked."],
            ),
            _step(
                12, "coordinator", "Final Requirement Traceability Auditor",
                "Audit every user requirement against the final files, local evidence, declared artifact validator, repair history, and residual risks; do not claim completion while a mandatory gap or critical validation failure remains.",
                [8, 9, 10, 11], ["delivery assurance", "traceability", "evidence-based completion"],
                [], ["Every mandatory requirement has final implementation and independent validation evidence."],
            ),
        ]
        steps, estimate = adapt_document_steps(task, analysis, steps)
        requirements = _assign_document_requirements(task, steps)
        return {
            "delivery_profile": "professional-local",
            "primary_artifact": primary,
            "analysis": {
                **analysis,
                "workstreams": [step["specialist"] for step in steps],
                "document_work_estimate": {
                    "complexity": estimate.complexity,
                    "stage_budget": estimate.stage_budget,
                    "reasons": list(estimate.reasons),
                },
                "mvp_boundary": "Complete source-grounded document delivery with adaptive section planning; rich Office/PDF rendering is validated by the renderer when requested.",
            },
            "scope_changes": [],
            "interfaces": [],
            "external_tools": [],
            "execution_routines": [],
            "requirements": requirements,
            "artifact_validations": [
                _document_validation_policy(task, outputs, primary),
                *_supporting_document_validation_policies(task, steps, primary),
            ],
            "launch_commands": [],
            "steps": steps,
            "rationale": "Deterministic local-document fallback preserving explicit outputs, provenance, repair, and final quality gates.",
        }

    @staticmethod
    def _fallback_plan(
        task: str,
        analysis: Dict[str, Any] | None = None,
        planning_mode: str | None = None,
    ) -> Dict[str, Any]:
        analysis = analysis or analyze_task_complexity(task)
        mode = normalize_planning_mode(planning_mode)
        domains = set(analysis["domains"])
        if mode == "direct":
            return {"analysis": analysis,
                    "steps": [_step(0, "coordinator", "Task Specialist", f"Perform the user task: {task}", [], list(domains) or ["general"], [], ["The requested outcome is delivered."])],
                    "rationale": "Explicit direct planning mode."}
        if _document_deliverable_task(task):
            return SimplePlanner._document_fallback(task, analysis)
        if mode == "full_team" and "software-engineering" in domains:
            analysis = {**analysis, "level": "high" if analysis["level"] in {"low", "moderate"} else analysis["level"]}
        if mode != "short_team" and analysis["level"] in {"high", "very_high"} and len(domains) >= 3:
            return SimplePlanner._cross_domain_fallback(task, analysis)
        if "software-engineering" in domains:
            if mode != "short_team" and analysis["level"] in {"high", "very_high"}:
                steps = [
                    _step(0, "architect", "Requirements & Feasibility Analyst", "Analyze requirements, constraints, assumptions, risks, and acceptance criteria.", [], ["requirements engineering"], ["specs/requirements.md"], ["Requested outcomes are testable."]),
                    _step(1, "architect", "Solution Architect", "Design modules, interfaces, data flow, dependencies, and delivery strategy.", [0], ["software architecture", *sorted(domains)], ["specs/architecture.md"], ["Architecture covers every requirement."]),
                    _step(2, "security", "Security & Privacy Reviewer", "Review the design and specify concrete security and privacy mitigations.", [0, 1], ["threat modeling", "privacy"], ["specs/security.md"], ["Risks have actionable controls."]),
                    _step(3, "developer", "Domain Model & Persistence Engineer", "Implement complete validated domain models, persistence boundaries, recovery behavior, and core invariants.", [1, 2], ["domain modeling", "persistence", *sorted(domains)], [], ["State and domain behavior satisfy validated invariants."]),
                    _step(4, "developer", "Core Workflow Engineer", "Implement the complete runnable business workflows from specifications, reusing the domain contracts.", [1, 2, 3], ["implementation", "workflow engineering", *sorted(domains)], [], ["Core workflows execute real behavior without mock substitution."]),
                    _step(5, "developer", "Interface & Automation Engineer", "Expose the requested CLI, API, UI, import/export, and automation entry points that apply to the request.", [1, 4], ["API design", "CLI design", "user journeys"], [], ["Every requested user-facing workflow has a runnable entry point."]),
                    _step(6, "developer", "Cross-Component Integration Engineer", "Integrate components, verify producer/consumer signatures and data shapes, and remove duplicate or disconnected implementations.", [3, 4, 5], ["systems integration", "interface contracts"], [], ["Requested workflows use one coherent implementation end to end."]),
                    _step(7, "qa", "Independent Contract Test Engineer", "Create independent unit, boundary, import, and interface contract tests against actual public modules, then collect them without changing implementation.", [3, 4, 5], ["test engineering", "contract testing"], ["tests/test_acceptance.py"], ["Tests import and exercise real public modules."], ["python -m pytest --collect-only -q"]),
                    _step(8, "debugger", "Autonomous Unit & Integration Repair Engineer", "Run the complete suite, inspect concrete failures, repair root causes across integrated components, and rerun until green.", [6, 7], ["debugging", "root-cause analysis"], [], ["Complete unit and integration suite exits with code 0."], ["python -m pytest -q"]),
                    _step(9, "qa", "Clean-Process End-to-End Acceptance Engineer", "Create and run complete representative CLI/API/UI acceptance journeys from a fresh process with local fixtures; verify outputs instead of mocked replicas.", [8], ["end-to-end testing", "process isolation", "artifact validation"], ["tests/test_end_to_end.py"], ["Requested user journeys run through public entry points."], ["python -m pytest -q"]),
                    _step(10, "debugger", "Final Autonomous Acceptance Repair Engineer", "Repair only defects exposed by end-to-end acceptance and rerun all validations without repeating completed feature work.", [9], ["acceptance debugging", "regression repair"], [], ["Complete suite exits with code 0 after final repair."], ["python -m pytest -q"]),
                    _step(11, "writer", "Technical Documentation & Operations Writer", "Document installation, offline operation, launch commands, use, architecture, tests, recovery, limitations, and maintenance from actual evidence.", [5, 9], ["technical writing", "operations"], ["README.md"], ["Documentation matches runnable behavior and exact limitations."]),
                    _step(12, "coordinator", "Final Requirement Traceability Auditor", "Audit every mandatory requirement against implementation artifacts, independent validation, launch evidence, limitations, and approved scope changes.", [10, 11], ["delivery audit", "traceability"], [], ["No unsupported completion claim and no unmapped mandatory requirement."]),
                ]
                return {"analysis": analysis, "steps": steps, "rationale": "Adaptive deterministic software fallback."}
            steps = [
                _step(0, "architect", "Requirements & Feasibility Analyst", "Analyze the requested change, constraints, and acceptance criteria.", [], ["requirements engineering"], ["specs/requirements.md"], ["The requested outcome is testable."]),
                _step(1, "developer", "Implementation Engineer", "Implement the requested software change against the existing project contracts.", [0], ["implementation", *sorted(domains)], [], ["The change is runnable and does not substitute mocks for required behavior."]),
                _step(2, "qa", "Independent Test Engineer", "Add or update focused tests that exercise the public behavior requested.", [1], ["test engineering"], ["tests/test_acceptance.py"], ["Tests import real modules and cover the change."], ["python -m pytest --collect-only -q"]),
                _step(3, "debugger", "Autonomous Repair Engineer", "Run the relevant suite, repair root causes, and rerun until green.", [2], ["debugging"], [], ["The verification command exits with code 0."], ["python -m pytest -q"]),
                _step(4, "coordinator", "Final Requirement Traceability Auditor", "Audit the requested outcome against files, tests, and residual risk.", [3], ["delivery audit"], [], ["No unsupported completion claim remains."]),
            ]
            return {"analysis": analysis, "steps": steps, "rationale": "Compact software fallback sized to the task."}
        return {"analysis": analysis,
                "steps": [_step(0, "coordinator", "Task Specialist", f"Perform the user task: {task}", [], list(domains) or ["general"], [], ["The requested outcome is delivered."])],
                "rationale": "Deterministic direct-task fallback."}

    @staticmethod
    def _extract_json(content: str) -> Dict[str, Any] | None:
        candidates = [content.strip()]
        if "```json" in content:
            candidates.append(content.split("```json", 1)[1].split("```", 1)[0].strip())
        elif "```" in content:
            candidates.append(content.split("```", 1)[1].split("```", 1)[0].strip())
        first, last = content.find("{"), content.rfind("}")
        if first >= 0 and last > first:
            candidates.append(content[first:last + 1])
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _validate_generated_plan(
        plan: Dict[str, Any],
        analysis: Dict[str, Any],
        planning_mode: str = "auto",
    ) -> None:
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("Planner response has no valid steps array.")
        allowed_roles = {"architect", "security", "developer", "qa", "debugger", "writer", "coordinator"}
        identifiers = {str(step.get("id", index)) for index, step in enumerate(steps) if isinstance(step, dict)}
        if len(identifiers) != len(steps):
            raise ValueError("Planner returned invalid or duplicate step identifiers.")
        specialists = set()
        roles = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or not str(step.get("description") or "").strip():
                raise ValueError(f"Planner step {index} has no actionable description.")
            role = str(step.get("role") or "").lower()
            if role not in allowed_roles:
                raise ValueError(f"Planner step {index} uses unsupported role '{role}'.")
            roles.append(role)
            specialist = str(step.get("specialist") or "").strip()
            if not specialist:
                raise ValueError(f"Planner step {index} has no specialist profile.")
            specialists.add(specialist.lower())
            dependencies = step.get("dependencies", [])
            if not isinstance(dependencies, list) or any(not isinstance(item, (str, int)) for item in dependencies):
                raise ValueError(f"Planner step {index} has invalid dependencies.")
            for field in (
                "expertise", "required_artifacts", "acceptance_criteria",
                "verification_commands", "requirement_ids", "owned_paths",
            ):
                value = step.get(field, [])
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError(f"Planner step {index} has invalid {field}.")
            if any(str(dependency) not in identifiers for dependency in step.get("dependencies", [])):
                raise ValueError(f"Planner step {index} references an unknown dependency.")

        mode = normalize_planning_mode(planning_mode)
        apply_complex_gates = mode not in {"direct", "short_team"} and (
            analysis["level"] in {"high", "very_high"} or mode == "full_team"
        )
        if apply_complex_gates:
            minimum = int(analysis.get("suggested_min_steps") or 1)
            if mode == "full_team":
                minimum = max(minimum, 9)
            if len(steps) < minimum:
                raise ValueError(
                    f"Planner undersized a {analysis['level']} task: {len(steps)} < {minimum} steps."
                )
            if len(specialists) < max(6, len(steps) * 3 // 4):
                raise ValueError("Planner reused too many generic specialist profiles.")
            if "debugger" not in roles or roles[-1] != "coordinator":
                raise ValueError("Complex plan lacks autonomous repair or final delivery audit.")

        binary_model_suffixes = (".pth", ".pt", ".ckpt", ".safetensors", ".onnx")
        generated_model_assets = [artifact for step in steps for artifact in step.get("required_artifacts", [])
                                  if artifact.lower().endswith(binary_model_suffixes)]
        if generated_model_assets:
            raise ValueError("Plan falsely treats unavailable pretrained model weights as generatable artifacts.")

        if os.name == "nt":
            incompatible = [command for step in steps for command in step.get("verification_commands", [])
                            if re.search(r"(^|[|;&])\s*(grep|cat|head|tail|ls|file|unzip)\b", command.lower())]
            if incompatible:
                raise ValueError("Plan contains platform-incompatible verification commands.")

    @staticmethod
    def _coerce_string_array(value: Any, preferred_keys: tuple[str, ...]) -> List[str]:
        """Recover common structured-array variations emitted by local LLMs."""
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        normalized = []
        for item in value:
            if isinstance(item, str):
                text = item
            elif isinstance(item, dict):
                candidate = next((item.get(key) for key in preferred_keys if item.get(key)), None)
                text = str(candidate) if candidate is not None else json.dumps(item, ensure_ascii=False)
            else:
                text = str(item) if item is not None else ""
            if text.strip():
                normalized.append(text.strip())
        return normalized

    async def plan(self, task: str, context: Dict[str, Any],
                   capabilities_schemas: List[Dict[str, Any]], **kwargs) -> Dict[str, Any]:
        parent_id = kwargs.get("parent_execution_id")
        if not parent_id and context and "variables" in context:
            parent_id = context["variables"].get("parent_execution_id")
        if parent_id:
            delegated = kwargs.get("delegated_step")
            if isinstance(delegated, dict):
                direct_step = dict(delegated)
                direct_step.update({"id": 0, "dependencies": [], "status": "pending"})
            else:
                direct_step = {"id": 0, "description": task, "dependencies": [], "status": "pending"}
            return {"steps": [direct_step], "rationale": "Direct execution of a delegated specialist step."}

        domain_registry = self.domain_registry
        project_domains = kwargs.get("project_domains")
        if isinstance(project_domains, dict) and project_domains:
            domain_registry = ProjectDomainRegistry(self.domain_registry.definitions)
            for name, markers in project_domains.items():
                if not isinstance(markers, list):
                    raise ValueError(f"Project domain markers must be an array: {name}")
                domain_registry.register(str(name), markers)
        analysis = analyze_task_complexity(task, domain_registry)
        planning_mode = normalize_planning_mode(
            kwargs.get("planning_mode")
            or (context or {}).get("variables", {}).get("planning_mode")
        )
        if planning_mode == "full_team" and analysis["level"] in {"low", "moderate"}:
            analysis = {
                **analysis,
                "level": "high",
                "suggested_min_steps": max(int(analysis.get("suggested_min_steps") or 1), 9),
            }
        mode_instructions = {
            "direct": (
                "Planning mode is direct: return exactly one coordinator step that performs "
                "the whole user task. Do not expand into a specialist team."
            ),
            "short_team": (
                "Planning mode is short_team: produce a compact specialist DAG, typically "
                "3-6 steps, with implementation, independent verification, and a final "
                "coordinator. Do not inflate into a full cross-domain programme."
            ),
            "full_team": (
                "Planning mode is full_team: produce a complete specialist programme with "
                "distinct architects, implementation, independent tests, autonomous repair, "
                "and a final coordinator even if the wording looks small."
            ),
            "auto": (
                "Planning mode is auto: size the DAG to the deterministic complexity hints."
            ),
        }[planning_mode]
        capabilities_list = [schema["function"]["name"] for schema in capabilities_schemas]
        prompt = (
            "You are GPTMOSS's adaptive planning engine. Produce a realistic specialist DAG, not a generic seven-role checklist.\n"
            "First assess the final deliverable's true size, domains, workstreams, dependencies, unavailable assets, risks, and MVP boundary.\n"
            f"{mode_instructions}\n"
            f"Deterministic complexity hints (minimum safeguards, not a ceiling): {json.dumps(analysis, ensure_ascii=False)}\n"
            f"User task: {task}\nAvailable capabilities: {json.dumps(capabilities_list)}\n"
            f"Detected capability gaps: {json.dumps((context or {}).get('variables', {}).get('capability_gaps', []), ensure_ascii=False)}\n\n"
            f"Delivery environment: platform={os.name}; dependencies and model weights may not be downloaded during offline execution. "
            "Use portable Python/pytest verification commands and dependency-light implementations.\n"
            "Use canonical roles only from architect, security, developer, qa, debugger, writer, coordinator, but create as many distinct domain specialists as required. "
            "A delegated specialist is bounded to its own assignment by default. Set allow_nested_delegation=true on a step only when that specialist genuinely requires a subordinate team and explain why in the rationale; never use it to duplicate sibling steps. "
            "Multiple differently specialized developers and testers are expected.\n"
            f"For {analysis['level']} complexity, provide at least {analysis['suggested_min_steps']} substantive steps unless a smaller complete DAG is explicitly justified. "
            "Parallelize independent work and depend on existing outputs so work is not repeated.\n"
            "Extract a top-level requirements array from the user's exact request before planning. Preserve every distinct requested outcome; never merge or truncate requirements merely to reduce plan size. Each requirement has id, statement, priority, mandatory, source='user', and acceptance. "
            "Every step must contain id, role, specialist, description, dependencies, expertise, required_artifacts, acceptance_criteria, verification_commands, requirement_ids, owned_paths, and status='pending'. "
            "requirement_ids trace exact requirements. owned_paths are non-overlapping relative file paths or globs that the specialist may modify. "
            "Array fields must be arrays; artifacts are concrete relative file paths.\n"
            "Implementation must be runnable and must not silently substitute random/mock behavior. If weights, hardware, datasets, or services are unavailable, build a truthful deterministic prototype and explicit adapter contract, document the limitation, and test both paths.\n"
            "Do not list pretrained checkpoint files as artifacts an agent can create. Preserve each requested end-to-end outcome: never replace a complete object, workflow, or integration with a narrower proxy merely because it is easier to generate.\n"
            "For unavailable or project-specific engines and desktop tools, do not pretend to run them. Declare top-level external_tools and execution_routines containing availability probes, installation/configuration parameters, exact commands or API calls, expected outputs, rollback guidance, and independent validation. "
            "Declare top-level artifact_validations with path, validator, required, and machine-readable constraints. Built-in validators include json, document/markdown/txt, obj, and glb; projects may register more. For professional documents, declare required_headings, required_requirement_ids, required_traceability_ids, required_source_files, source_inventory, minimums, and the local-reference/content gates appropriate to the assignment.\n"
            "End with autonomous repair after acceptance testing and a final delivery auditor that cannot claim success without evidence.\n"
            "Acceptance must be independent of implementation: include clean-room launch/CLI/API user journeys and interface/signature checks, not only tests written by implementation agents. "
            "For browser or responsive interfaces, include computed-layout acceptance in Microsoft Edge/Chromium at representative widths 320, 480, 768, and 1366 pixels. Assert that content is readable, not clipped, and either fits or has an intentional reachable scrollbar. "
            "Any MVP boundary, unsupported feature, mock, deferred work, or reduction of a mandatory user requirement must be listed in top-level scope_changes with statement, kind, reason, and requirement_ids. It requires user approval. "
            "Each interfaces item has module, symbol (function or Class.method), parameters, returns, and consumers (relative source paths). "
            "Return raw JSON with keys analysis, requirements, scope_changes, interfaces, external_tools, execution_routines, artifact_validations, launch_commands, steps, rationale. analysis includes level, domains, workstreams, assumptions, risks, mvp_boundary, out_of_scope. No prose."
        )
        try:
            response = await self.llm_provider.completion(
                messages=[{"role": "system", "content": "You are a precise JSON planning coordinator and systems planner. Return only valid raw JSON."},
                          {"role": "user", "content": prompt}], temperature=0.1)
            plan_data = self._extract_json(response.get("content", ""))
            if not plan_data:
                raise ValueError("Planner response is not a JSON object.")
            plan_data.setdefault("analysis", analysis)
            if not isinstance(plan_data.get("requirements"), list):
                plan_data["requirements"] = []
            if not isinstance(plan_data.get("scope_changes"), list):
                plan_data["scope_changes"] = []
            if not isinstance(plan_data.get("interfaces"), list):
                plan_data["interfaces"] = []
            for field in ("external_tools", "execution_routines", "artifact_validations"):
                if not isinstance(plan_data.get(field), list):
                    plan_data[field] = []
            plan_data["launch_commands"] = self._coerce_string_array(
                plan_data.get("launch_commands", []), ("command", "cmd", "description")
            )
            for step in plan_data["steps"]:
                step.setdefault("dependencies", [])
                step["expertise"] = self._coerce_string_array(step.get("expertise", []), ("name", "area", "skill"))
                step["required_artifacts"] = self._coerce_string_array(step.get("required_artifacts", []), ("path", "file", "artifact"))
                step["acceptance_criteria"] = self._coerce_string_array(step.get("acceptance_criteria", []), ("criterion", "description", "name"))
                step["verification_commands"] = self._coerce_string_array(step.get("verification_commands", []), ("command", "cmd", "description"))
                step["requirement_ids"] = self._coerce_string_array(step.get("requirement_ids", []), ("id", "requirement_id", "name"))
                step["owned_paths"] = self._coerce_string_array(step.get("owned_paths", []), ("path", "glob", "file"))
                if analysis["level"] in {"low", "moderate"} and not step.get("specialist"):
                    role_title = str(step.get("role") or "Task").replace("_", " ").title()
                    step["specialist"] = f"{role_title} Specialist"
                step["status"] = "pending"
            self._validate_generated_plan(plan_data, analysis, planning_mode)
            if _document_deliverable_task(task):
                plan_data["delivery_profile"] = "professional-local"
            plan_data["planning_mode"] = planning_mode
            return plan_data
        except Exception as exc:
            logger.warning("Error or undersized LLM plan; using adaptive fallback: %s", exc)
            return self._fallback_plan(task, analysis, planning_mode)
