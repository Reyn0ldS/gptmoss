import json

from gptmoss.core.delivery import (
    build_delivery_contract,
    declared_interface_issues,
    evaluate_delivery,
    path_is_owned,
    static_workspace_issues,
)
from gptmoss.core.execution import normalize_plan


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
    assert len(contract["contract_sha256"]) == 64
    assert path_is_owned(contract, 0, "developer", "src/sample/cli.py")
    assert not path_is_owned(contract, 0, "developer", "tests/test_cli.py")
    assert path_is_owned(contract, 1, "debugger", "src/sample/cli.py")
    assert not path_is_owned(contract, 1, "debugger", ".gptmoss/contract.json")
    assert not path_is_owned(contract, 1, "debugger", "./.gptmoss/contract.json")


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
