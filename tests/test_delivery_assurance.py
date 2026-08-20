import json

from gptmoss.core.delivery import (
    build_delivery_contract,
    commands_equivalent,
    declared_interface_issues,
    evaluate_delivery,
    extract_requirements,
    normalize_scope_changes,
    path_is_owned,
    static_workspace_issues,
)
from gptmoss.core.execution import (
    merge_inherited_requirements,
    normalize_plan,
    requirement_validation_commands,
    requirements_for_delegation,
)


def _plan():
    return normalize_plan({
        "requirements": [{
            "id": "REQ-001",
            "statement": "Expose a runnable local command",
            "priority": "must",
            "mandatory": True,
            "source": "user",
            "acceptance": ["The command exits successfully"],
        }],
        "launch_commands": ["python -m sample.cli --help"],
        "steps": [
            {
                "id": 0,
                "role": "developer",
                "specialist": "CLI Engineer",
                "description": "Implement the runnable local command",
                "dependencies": [],
                "expertise": ["CLI"],
                "required_artifacts": ["src/sample/cli.py"],
                "acceptance_criteria": ["CLI runs"],
                "verification_commands": [],
                "requirement_ids": ["REQ-001"],
                "owned_paths": ["src/sample/**"],
            },
            {
                "id": 1,
                "role": "qa",
                "specialist": "Independent Acceptance Engineer",
                "description": "Exercise the real CLI from a clean process",
                "dependencies": [0],
                "expertise": ["acceptance"],
                "required_artifacts": ["tests/test_cli.py"],
                "acceptance_criteria": ["CLI smoke passes"],
                "verification_commands": ["python -m pytest -q"],
                "requirement_ids": ["REQ-001"],
                "owned_paths": ["tests/**"],
            },
        ],
    })


def test_contract_freezes_traceability_scope_and_ownership():
    plan = _plan()
    plan["scope_changes"] = [{
        "kind": "deferred",
        "statement": "Graphical interface is deferred",
        "requirement_ids": ["REQ-001"],
        "reason": "offline dependency unavailable",
    }]
    contract = build_delivery_contract(plan, "Expose a runnable local command")

    row = contract["traceability"][0]
    assert row["implementation_steps"] == [0]
    assert row["validation_steps"] == [1]
    assert contract["scope_changes"][0]["statement"] == "Graphical interface is deferred"
    assert len(contract["scope_changes_sha256"]) == 64
    assert len(contract["contract_sha256"]) == 64
    assert path_is_owned(contract, 0, "developer", "src/sample/cli.py")
    assert not path_is_owned(contract, 0, "developer", "tests/test_cli.py")
    assert path_is_owned(contract, 1, "debugger", "src/sample/cli.py")
    assert not path_is_owned(contract, 1, "debugger", "tests/test_cli.py")
    assert not path_is_owned(contract, 1, "debugger", ".gptmoss/contract.json")
    assert not path_is_owned(contract, 1, "debugger", "./.gptmoss/contract.json")


def test_scope_approval_hash_is_stable_when_only_delivery_gates_change():
    first_plan = _plan()
    first_plan["scope_changes"] = [{"statement": "Vision review remains deferred"}]
    first_plan["artifact_validations"] = [{
        "path": "report.md", "validator": "document",
        "constraints": {"minimums": {"words": 600}},
    }]
    second_plan = _plan()
    second_plan["scope_changes"] = [{"statement": "Vision review remains deferred"}]
    second_plan["artifact_validations"] = [{
        "path": "report.md", "validator": "document",
        "constraints": {"minimums": {"words": 8750, "valid_diagrams": 3}},
    }]

    first = build_delivery_contract(first_plan, "Write the report")
    second = build_delivery_contract(second_plan, "Write the report")

    assert first["contract_sha256"] != second["contract_sha256"]
    assert first["scope_changes_sha256"] == second["scope_changes_sha256"]


def test_automatic_software_traceability_prefers_developer_and_relevant_qa():
    plan = normalize_plan({
        "requirements": [{
            "id": "REQ-GARMENT",
            "statement": "Fit a reconstructed garment onto multiple avatars",
            "mandatory": True,
        }],
        "steps": [
            {
                "id": 0, "role": "architect", "specialist": "Requirements Analyst",
                "description": "Define all project requirements", "dependencies": [],
                "requirement_ids": ["REQ-GARMENT"],
            },
            {
                "id": 1, "role": "developer", "specialist": "Garment Fitting Engineer",
                "description": "Fit a reconstructed garment onto multiple body avatars",
                "dependencies": [0],
            },
            {
                "id": 2, "role": "qa", "specialist": "Garment Acceptance Engineer",
                "description": "Validate garment fitting across multiple avatars",
                "dependencies": [1],
                "requirement_ids": ["REQ-GARMENT"],
            },
            {
                "id": 3, "role": "coordinator", "specialist": "Final Auditor",
                "description": "Audit the final delivery", "dependencies": [2],
            },
        ],
    })

    row = build_delivery_contract(plan, "Fit garments")["traceability"][0]

    assert row["implementation_steps"] == [1]
    assert row["validation_steps"] == [2]


def test_delegated_plan_inherits_parent_requirement_identifiers():
    plan = normalize_plan({
        "steps": [{
            "id": 0,
            "role": "developer",
            "description": "Implement validated face geometry",
            "dependencies": [],
            "requirement_ids": ["REQ-FACE-IMAGE"],
        }],
    })
    inherited = [{
        "id": "REQ-FACE-IMAGE",
        "statement": "Derive validated face geometry from an image when vision exists",
        "priority": "must",
        "mandatory": True,
        "source": "user",
        "acceptance": ["Exported geometry is non-empty"],
    }]

    contract = build_delivery_contract(
        merge_inherited_requirements(plan, inherited),
        "Implement the delegated face geometry adapter",
    )

    assert contract["requirements"][0]["id"] == "REQ-FACE-IMAGE"
    assert contract["traceability"][0]["implementation_steps"] == [0]


def test_delegation_transmits_full_requirement_text_and_defaults_to_all_mandatory():
    requirements = [
        {"id": "REQ-1", "statement": "Restore the public model", "mandatory": True},
        {"id": "REQ-2", "statement": "Run complete acceptance", "mandatory": True},
        {"id": "REQ-3", "statement": "Optional polish", "mandatory": False},
    ]

    selected = requirements_for_delegation(requirements, ["REQ-2"])
    fallback = requirements_for_delegation(requirements, [])

    assert selected == [requirements[1]]
    assert fallback == requirements[:2]


def test_explicit_validation_commands_are_extracted_from_requirement_text():
    delimiter = chr(96)
    requirements = [{
        "id": "REQ-TEST",
        "statement": (
            "Require exact " + delimiter + "python -m pytest --collect-only -q"
            + delimiter + " and " + delimiter + "python -m pytest -q"
            + delimiter + "; preserve " + delimiter + "HealthStatus" + delimiter + "."
        ),
        "mandatory": True,
    }]

    assert requirement_validation_commands(requirements) == [
        "python -m pytest --collect-only -q",
        "python -m pytest -q",
    ]


def test_static_assurance_detects_package_identity_and_signature_mismatch(tmp_path):
    package = tmp_path / "src" / "sample"
    package.mkdir(parents=True)
    (package / "service.py").write_text(
        "class Fitter:\n"
        "    def fit(self, avatar_mesh, garment_mesh):\n"
        "        return avatar_mesh\n",
        encoding="utf-8",
    )
    (package / "consumer.py").write_text(
        "from src.sample.service import Fitter\n"
        "class Client:\n"
        "    def __init__(self):\n"
        "        self.fitter = Fitter()\n"
        "    def run(self):\n"
        "        return self.fitter.fit(avatar='a', garment='g')\n",
        encoding="utf-8",
    )

    issues = static_workspace_issues(tmp_path)

    assert any(issue["kind"] == "package_identity" for issue in issues)
    assert any(
        issue["kind"] == "signature" and "avatar" in issue["message"]
        for issue in issues
    )


def test_independent_evidence_is_required_for_delivery(tmp_path):
    plan = _plan()
    contract = build_delivery_contract(plan, "Expose a runnable local command")
    for artifact in ("src/sample/cli.py", "tests/test_cli.py"):
        path = tmp_path / artifact
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# valid\n", encoding="utf-8")

    failed = evaluate_delivery(tmp_path, contract, plan["steps"], [])
    assert not failed["passed"]
    evidence_check = next(
        check for check in failed["checks"]
        if check["name"] == "independent_machine_evidence"
    )
    assert "python -m pytest -q" in evidence_check["missing_commands"]

    history = [{
        "capability": "shell",
        "action": "execute",
        "arguments": {"command": "python -m pytest -q"},
        "result": "1 passed\nEXIT_CODE: 0",
    }, {
        "capability": "shell",
        "action": "execute",
        "arguments": {"command": "python -m sample.cli --help"},
        "result": "usage: sample\nEXIT_CODE: 0",
    }]
    passed = evaluate_delivery(tmp_path, contract, plan["steps"], history)
    assert passed["passed"], json.dumps(passed, indent=2)


def test_declared_interface_is_checked_against_actual_ast(tmp_path):
    package = tmp_path / "src" / "sample"
    package.mkdir(parents=True)
    (package / "service.py").write_text(
        "class Service:\n"
        "    def create(self, image_path, output_path):\n"
        "        return output_path\n",
        encoding="utf-8",
    )
    contract = [{
        "module": "sample.service",
        "symbol": "Service.create",
        "parameters": ["image_path", "garment_path", "output_path"],
        "consumers": ["src/sample/cli.py"],
    }]

    issues = declared_interface_issues(tmp_path, contract)

    assert any("parameters" in issue["message"] for issue in issues)
    assert any("consumer" in issue["message"] for issue in issues)


def test_requirement_extraction_preserves_lists_punctuation_and_has_no_hidden_cap():
    task = "\n".join(
        f"- Outcome {index}: keep commas, semicolons; and all details together"
        for index in range(30)
    )

    requirements = extract_requirements(task)

    assert len(requirements) == 30
    assert ", semicolons;" in requirements[0]["statement"]
    assert requirements[-1]["statement"].startswith("Outcome 29")


def test_requirement_extraction_preserves_explicit_stable_identifiers():
    requirements = extract_requirements(
        "REQ-E2E-001 — Inventorier toutes les pièces.\n"
        "REQ-E2E-002: Produire une matrice de traçabilité.\n"
        "- Conserver aussi cette exigence sans identifiant."
    )

    assert [item["id"] for item in requirements] == [
        "REQ-E2E-001", "REQ-E2E-002", "REQ-001",
    ]
    assert requirements[0]["statement"] == "Inventorier toutes les pièces"
    assert requirements[1]["statement"] == "Produire une matrice de traçabilité"


def test_complete_without_placeholders_is_not_misclassified_as_scope_reduction():
    plan = {"steps": [{
        "id": 0,
        "description": "Implement the complete system without placeholders",
        "acceptance_criteria": ["No placeholder remains"],
    }]}

    assert normalize_scope_changes(plan) == []

    plan = {"steps": [{"id": 0, "description": "Dashboard is future work"}]}
    assert normalize_scope_changes(plan)[0]["kind"] == "scope_reduction"


def test_optional_planner_metadata_cannot_abort_an_otherwise_valid_plan():
    plan = _plan()
    plan.update({
        "artifact_validations": ["invalid"],
        "external_tools": [{"name": "Blender"}],
        "execution_routines": "invalid",
    })

    contract = build_delivery_contract(plan, "Expose a runnable local command")

    assert contract["artifact_validations"] == []
    assert contract["external_tools"] == []
    assert contract["execution_routines"] == []
    assert len(contract["normalization_warnings"]) == 3


def test_delegated_delivery_contract_does_not_expand_team_obligations():
    plan = {
        "steps": [{
            "id": 0,
            "role": "architect",
            "specialist": "Requirements Architect",
            "description": "Build the delegated requirements matrix.",
            "dependencies": [],
            "expertise": ["requirements"],
            "required_artifacts": ["requirements-matrix.md"],
            "owned_paths": ["requirements-matrix.md"],
            "acceptance_criteria": ["The matrix is complete."],
            "verification_commands": [],
            "requirement_ids": [],
            "satisfies_obligations": [],
            "required_evidence": [],
        }],
    }

    contract = build_delivery_contract(
        plan,
        "Build a professional requirements matrix from local evidence.",
        repair_obligations=False,
    )

    assert len(plan["steps"]) == 1
    assert plan["steps"][0]["owned_paths"] == ["requirements-matrix.md"]
    assert contract["plan_obligations"] == []
    assert not any(
        item.get("pattern") == "analysis/independent-validation.md"
        for item in contract["ownership"]
    )


def test_command_evidence_accepts_windows_wrappers_but_not_a_targeted_subset():
    assert commands_equivalent(
        "python -m pytest -q",
        'chcp 65001 >nul && cd /d "C:/work" && "C:/runtime/python.exe" -m pytest -q',
    )
    assert commands_equivalent(
        "python -m pytest tests/test_cli.py -q",
        "python -m pytest -q tests/test_cli.py",
    )
    assert not commands_equivalent(
        "python -m pytest -q",
        "python -m pytest -q tests/test_cli.py",
    )
