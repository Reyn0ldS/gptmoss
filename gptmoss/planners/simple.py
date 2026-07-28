"""Adaptive LLM planner with deterministic complexity safeguards."""

import json
import logging
import os
import re
from typing import Any, Dict, List

from gptmoss.interfaces.llm import LLMProvider
from gptmoss.interfaces.planner import PlannerProvider

logger = logging.getLogger("gptmoss.planners.simple")

DOMAIN_MARKERS = {
    "software-engineering": ("application", "logiciel", "programme", "software", "code", "api", "site web", "project", "projet"),
    "computer-vision": ("image", "photo", "visage", "face", "segmentation", "vision"),
    "machine-learning": ("ia", "ai", "modèle", "model", "apprentissage", "inférence", "inference"),
    "3d-graphics": ("3d", "maillage", "mesh", "texture", "rendu", "render"),
    "human-avatar": ("avatar", "corps", "body", "humain", "human", "visage", "face"),
    "digital-garments": ("vêtement", "vetement", "garment", "cloth", "habiller", "porter"),
    "user-interface": ("interface", "ui", "web", "desktop", "utilisateur"),
    "data-privacy": ("visage", "face", "biométr", "biometr", "personnel", "privacy", "rgpd", "gdpr"),
    "offline-delivery": ("hors-ligne", "hors ligne", "offline", "portable", "autonome"),
}


def analyze_task_complexity(task: str) -> Dict[str, Any]:
    """Return deterministic hints so the LLM cannot silently trivialize a task."""
    text = str(task or "").lower()
    domains = [domain for domain, markers in DOMAIN_MARKERS.items() if any(marker in text for marker in markers)]
    requested_outcomes = len(re.findall(
        r"\b(?:doit|devra|pouvoir|créer|creer|faire|importer|extrapoler|intégrer|integrer|"
        r"must|should|create|build|implement|support|import)\b", text,
    ))
    score = len(domains) * 2 + min(requested_outcomes, 5)
    score += 2 if len(text) > 300 else 1 if len(text) > 140 else 0
    if score >= 14:
        level, minimum = "very_high", 12
    elif score >= 9:
        level, minimum = "high", 9
    elif score >= 5:
        level, minimum = "moderate", 5
    else:
        level, minimum = "low", 1
    return {"level": level, "score": score, "domains": domains,
            "requested_outcomes": requested_outcomes, "suggested_min_steps": minimum}


def _step(step_id: int, role: str, specialist: str, description: str,
          dependencies: List[int], expertise: List[str], required_artifacts: List[str],
          acceptance_criteria: List[str], verification_commands: List[str] | None = None) -> Dict[str, Any]:
    return {"id": step_id, "role": role, "specialist": specialist, "description": description,
            "dependencies": dependencies, "expertise": expertise,
            "required_artifacts": required_artifacts, "acceptance_criteria": acceptance_criteria,
            "verification_commands": verification_commands or [], "status": "pending"}


class SimplePlanner(PlannerProvider):
    """Generate an adaptive specialist DAG and reject undersized plans."""

    def __init__(self, llm_provider: LLMProvider):
        self.llm_provider = llm_provider

    @staticmethod
    def _avatar_3d_fallback(task: str, analysis: Dict[str, Any]) -> Dict[str, Any]:
        steps = [
            _step(0, "architect", "Product & Feasibility Analyst",
                  "Define user journeys, honest MVP boundaries, input assumptions, measurable acceptance criteria, and unavailable model/checkpoint constraints in specs/requirements.md.",
                  [], ["requirements engineering", "ML feasibility", "product scope"],
                  ["specs/requirements.md"], ["MVP and production-grade capabilities are explicitly distinguished."]),
            _step(1, "architect", "3D/ML Systems Architect",
                  "Design the end-to-end modular architecture, data contracts, coordinate systems, model adapters, and offline deployment strategy in specs/architecture.md.",
                  [0], ["3D reconstruction", "ML systems", "geometry pipelines"],
                  ["specs/architecture.md"], ["Every requested workflow maps to concrete modules and interfaces."]),
            _step(2, "security", "Biometric Privacy & Safety Specialist",
                  "Specify consent, biometric data retention, access controls, path/input validation, and safe handling of body representations.",
                  [0], ["biometric privacy", "GDPR", "secure media ingestion"],
                  ["specs/security.md"], ["Face and body data risks have actionable mitigations."]),
            _step(3, "developer", "Face Reconstruction Engineer",
                  "Implement a runnable face-image ingestion and avatar reconstruction adapter with deterministic local fallback, validation, and mesh export.",
                  [1, 2], ["computer vision", "face reconstruction", "mesh export"],
                  ["src/avatar3d/face.py"], ["Module runs without silently returning random or fake geometry."]),
            _step(4, "developer", "Parametric Body & Geometry Engineer",
                  "Implement canonical body/avatar geometry, coordinate conventions, mesh validation, and the face-to-body attachment contract.",
                  [1, 2], ["SMPL-X adapters", "mesh topology", "3D transforms"],
                  ["src/avatar3d/body.py", "src/avatar3d/geometry.py"], ["A coherent deterministic body mesh can be exported locally."]),
            _step(5, "developer", "Garment Reconstruction Engineer",
                  "Implement garment-image ingestion, segmentation/reconstruction adapter interfaces, deterministic garment geometry, metadata, and export.",
                  [1, 2], ["garment segmentation", "single-view reconstruction", "mesh processing"],
                  ["src/avatar3d/garment.py"], ["Garment output has validated non-random topology and sizing metadata."]),
            _step(6, "developer", "Virtual Try-On & Rigging Engineer",
                  "Implement garment fitting to canonical avatars, deformation/rigging contracts, collision checks, and composed scene export.",
                  [4, 5], ["skinning", "cloth fitting", "collision detection"],
                  ["src/avatar3d/fitting.py"], ["The same garment fits deterministically to multiple avatar parameters."]),
            _step(7, "developer", "Backend/API Engineer",
                  "Implement the service and local CLI workflows for face import, garment import, avatar generation, fitting, status, and export.",
                  [3, 4, 5, 6], ["Python API design", "job orchestration", "file validation"],
                  ["src/avatar3d/service.py", "src/avatar3d/cli.py"], ["All core workflows are reachable through a runnable local interface."]),
            _step(8, "developer", "3D User Experience Engineer",
                  "Implement a dependency-light local viewer/demo exposing avatar and garment workflows and previewing OBJ scenes.",
                  [7], ["3D UX", "web UI", "OBJ visualization"],
                  ["demo/index.html"], ["The demo works without CDN dependencies."]),
            _step(9, "qa", "Geometry & ML Contract Test Engineer",
                  "Create deterministic root tests for media validation, mesh validity, transforms, body variation, garment fitting, and adapter failures. For a src/avatar3d layout, import avatar3d (never src.avatar3d), set pytest.ini testpaths=tests and pythonpath=src, and do not place tests inside the source package.",
                  [3, 4, 5, 6], ["property testing", "mesh invariants", "ML adapter contracts"],
                  ["pytest.ini", "tests/test_geometry.py", "tests/test_pipeline.py"], ["Tests import the real package and reject random, empty, NaN, and invalid-index meshes."],
                  ["python -m pytest --collect-only -q"]),
            _step(10, "debugger", "Autonomous Unit & Integration Repair Engineer",
                  "Run the unit and integration suite, inspect concrete failures, fix root causes across source modules and test contract mistakes, and rerun until the complete suite passes.",
                  [7, 8, 9], ["root-cause analysis", "cross-module integration", "dependency minimization"],
                  [], ["The unit and integration suite exits with code 0."], ["python -m pytest -q"]),
            _step(11, "qa", "End-to-End Acceptance Engineer",
                  "Create and run an end-to-end smoke workflow with generated local fixtures, verify exports, and record repeatable evidence.",
                  [10], ["E2E testing", "CLI testing", "artifact validation"],
                  ["tests/test_end_to_end.py"], ["A clean offline-compatible smoke run exits successfully."],
                  ["python -m pytest -q"]),
            _step(12, "debugger", "Final Autonomous Acceptance Repair Engineer",
                  "Repair only failures introduced or exposed by end-to-end acceptance, then rerun the complete suite and leave no known failure.",
                  [11], ["acceptance debugging", "regression repair", "evidence validation"],
                  [], ["The complete test suite exits with code 0."], ["python -m pytest -q"]),
            _step(13, "writer", "Technical Documentation & Model Operations Writer",
                  "Document installation, offline operation, demo/CLI usage, architecture, model adapter/checkpoint integration, limitations, privacy, and production next steps.",
                  [7, 8, 11], ["technical writing", "ML model operations", "offline deployment"],
                  ["README.md"], ["A new user can install, run, test, and understand MVP limitations."]),
            _step(14, "coordinator", "Final Delivery Auditor",
                  "Audit every requested outcome against actual artifacts and executed evidence; report completed scope, limitations, risks, and exact next actions without overstating capability.",
                  [12, 13], ["delivery audit", "acceptance management", "evidence synthesis"],
                  [], ["No capability is claimed without an artifact or successful execution evidence."]),
        ]
        return {"analysis": {**analysis, "workstreams": [step["specialist"] for step in steps],
                             "mvp_boundary": "Runnable deterministic local prototype plus adapters for external pretrained models; no claim of photorealistic single-view reconstruction without checkpoints."},
                "steps": steps, "rationale": "Deterministic cross-domain fallback preserves the real size and dependencies of the request."}

    @staticmethod
    def _fallback_plan(task: str, analysis: Dict[str, Any] | None = None) -> Dict[str, Any]:
        analysis = analysis or analyze_task_complexity(task)
        domains = set(analysis["domains"])
        if {"computer-vision", "3d-graphics", "human-avatar", "digital-garments"} <= domains:
            return SimplePlanner._avatar_3d_fallback(task, analysis)
        if "software-engineering" in domains:
            steps = [
                _step(0, "architect", "Requirements & Feasibility Analyst", "Analyze requirements, constraints, assumptions, risks, and acceptance criteria.", [], ["requirements engineering"], ["specs/requirements.md"], ["Requested outcomes are testable."]),
                _step(1, "architect", "Solution Architect", "Design modules, interfaces, data flow, dependencies, and delivery strategy.", [0], ["software architecture", *sorted(domains)], ["specs/architecture.md"], ["Architecture covers every requirement."]),
                _step(2, "security", "Security & Privacy Reviewer", "Review the design and specify concrete security and privacy mitigations.", [0, 1], ["threat modeling", "privacy"], ["specs/security.md"], ["Risks have actionable controls."]),
                _step(3, "developer", "Core Implementation Engineer", "Implement the complete runnable core from validated specifications.", [1, 2], ["implementation", *sorted(domains)], [], ["Core behavior has no placeholders."]),
                _step(4, "developer", "Integration Engineer", "Integrate components and expose the requested user-facing workflows.", [3], ["systems integration"], [], ["Requested workflows run end to end."]),
                _step(5, "qa", "Test & Acceptance Engineer", "Create and run unit, edge-case, integration, and acceptance tests.", [4], ["test engineering"], ["tests/test_acceptance.py"], ["Complete tests exit successfully."], ["python -m pytest -q"]),
                _step(6, "debugger", "Autonomous Repair Engineer", "Fix root causes and rerun the complete validation suite.", [5], ["debugging", "root-cause analysis"], [], ["Complete tests exit with code 0."], ["python -m pytest -q"]),
                _step(7, "writer", "Technical Documentation Writer", "Document installation, use, architecture, tests, limitations, and maintenance.", [4, 5], ["technical writing"], ["README.md"], ["Documentation matches actual behavior."]),
                _step(8, "coordinator", "Final Delivery Auditor", "Audit requirements against artifacts and evidence and report honestly.", [6, 7], ["delivery audit"], [], ["No unsupported completion claim."]),
            ]
            return {"analysis": analysis, "steps": steps, "rationale": "Adaptive deterministic software fallback."}
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
    def _validate_generated_plan(plan: Dict[str, Any], analysis: Dict[str, Any]) -> None:
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
            for field in ("expertise", "required_artifacts", "acceptance_criteria", "verification_commands"):
                value = step.get(field, [])
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError(f"Planner step {index} has invalid {field}.")
            if any(str(dependency) not in identifiers for dependency in step.get("dependencies", [])):
                raise ValueError(f"Planner step {index} references an unknown dependency.")

        if analysis["level"] in {"high", "very_high"}:
            if len(steps) < analysis["suggested_min_steps"]:
                raise ValueError(
                    f"Planner undersized a {analysis['level']} task: {len(steps)} < {analysis['suggested_min_steps']} steps."
                )
            if len(specialists) < max(6, len(steps) * 3 // 4):
                raise ValueError("Planner reused too many generic specialist profiles.")
            if "debugger" not in roles or roles[-1] != "coordinator":
                raise ValueError("Complex plan lacks autonomous repair or final delivery audit.")

        all_text = " ".join(
            str(step.get(field) or "")
            for step in steps for field in ("specialist", "description", "expertise", "acceptance_criteria")
        ).lower()
        domains = set(analysis.get("domains", []))
        avatar_garment = {"computer-vision", "3d-graphics", "human-avatar", "digital-garments"} <= domains
        if avatar_garment:
            if not any(marker in all_text for marker in ("body", "corps", "smpl", "parametric human", "human mesh")):
                raise ValueError("Avatar/garment plan omitted coherent full-body reconstruction.")
            fitting_text = " ".join(
                str(step.get("description") or "") for step in steps
                if any(marker in str(step.get("specialist") or "").lower()
                       for marker in ("garment", "cloth", "drap", "try-on", "rig"))
            ).lower()
            if fitting_text and not any(marker in fitting_text for marker in ("body", "avatar", "corps", "human")):
                raise ValueError("Garment workflow is not fitted to a full avatar/body.")

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

        analysis = analyze_task_complexity(task)
        capabilities_list = [schema["function"]["name"] for schema in capabilities_schemas]
        prompt = (
            "You are GPTMOSS's adaptive planning engine. Produce a realistic specialist DAG, not a generic seven-role checklist.\n"
            "First assess the final deliverable's true size, domains, workstreams, dependencies, unavailable assets, risks, and MVP boundary.\n"
            f"Deterministic complexity hints (minimum safeguards, not a ceiling): {json.dumps(analysis, ensure_ascii=False)}\n"
            f"User task: {task}\nAvailable capabilities: {json.dumps(capabilities_list)}\n\n"
            f"Delivery environment: platform={os.name}; dependencies and model weights may not be downloaded during offline execution. "
            "Use portable Python/pytest verification commands and dependency-light implementations.\n"
            "Use canonical roles only from architect, security, developer, qa, debugger, writer, coordinator, but create as many distinct domain specialists as required. "
            "Multiple differently specialized developers and testers are expected.\n"
            f"For {analysis['level']} complexity, provide at least {analysis['suggested_min_steps']} substantive steps unless a smaller complete DAG is explicitly justified. "
            "Parallelize independent work and depend on existing outputs so work is not repeated.\n"
            "Every step must contain id, role, specialist, description, dependencies, expertise, required_artifacts, acceptance_criteria, verification_commands, and status='pending'. "
            "Array fields must be arrays; artifacts are concrete relative file paths.\n"
            "Implementation must be runnable and must not silently substitute random/mock behavior. If weights, hardware, datasets, or services are unavailable, build a truthful deterministic prototype and explicit adapter contract, document the limitation, and test both paths.\n"
            "Do not list pretrained checkpoint files as artifacts an agent can create. For a human avatar plus clothing task, reconstruct a coherent canonical full body and fit garments to that body; a face/head mesh alone is not an avatar that can wear clothing.\n"
            "End with autonomous repair after acceptance testing and a final delivery auditor that cannot claim success without evidence.\n"
            "Return raw JSON with keys analysis, steps, rationale. analysis includes level, domains, workstreams, assumptions, risks, mvp_boundary, out_of_scope. No prose."
        )
        try:
            response = await self.llm_provider.completion(
                messages=[{"role": "system", "content": "You are a precise JSON planning coordinator and systems planner. Return only valid raw JSON."},
                          {"role": "user", "content": prompt}], temperature=0.1)
            plan_data = self._extract_json(response.get("content", ""))
            if not plan_data:
                raise ValueError("Planner response is not a JSON object.")
            plan_data.setdefault("analysis", analysis)
            for step in plan_data["steps"]:
                step.setdefault("dependencies", [])
                step["expertise"] = self._coerce_string_array(step.get("expertise", []), ("name", "area", "skill"))
                step["required_artifacts"] = self._coerce_string_array(step.get("required_artifacts", []), ("path", "file", "artifact"))
                step["acceptance_criteria"] = self._coerce_string_array(step.get("acceptance_criteria", []), ("criterion", "description", "name"))
                step["verification_commands"] = self._coerce_string_array(step.get("verification_commands", []), ("command", "cmd", "description"))
                if analysis["level"] in {"low", "moderate"} and not step.get("specialist"):
                    role_title = str(step.get("role") or "Task").replace("_", " ").title()
                    step["specialist"] = f"{role_title} Specialist"
                step["status"] = "pending"
            self._validate_generated_plan(plan_data, analysis)
            return plan_data
        except Exception as exc:
            logger.warning("Error or undersized LLM plan; using adaptive fallback: %s", exc)
            return self._fallback_plan(task, analysis)
