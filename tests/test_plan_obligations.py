from gptmoss.core.delivery import build_delivery_contract
from gptmoss.core.plan_obligations import (
    collect_plan_obligations,
    unsatisfied_obligations,
    validate_plan_obligations,
)
from gptmoss.core.workload import MAX_SOURCE_PARTITIONS, compile_work_graph
from gptmoss.planners.fallbacks import _step
from gptmoss.planners.simple import SimplePlanner


def _software_analysis(level="very_high"):
    return {
        "level": level,
        "score": 20,
        "domains": ["software-engineering"],
        "requested_outcomes": 4,
        "suggested_min_steps": 12,
    }


def test_direct_mode_has_no_structural_obligations():
    obligations = collect_plan_obligations(
        task="Build a software application with tests.",
        planning_mode="direct",
        analysis=_software_analysis(),
    )
    assert obligations == []


def test_corpus_auto_workflow_adds_source_gates_without_rewriting_the_task():
    task = "Fix the attached source bug only."
    obligations = collect_plan_obligations(
        task=task,
        planning_mode="auto",
        analysis={"level": "moderate", "domains": ["software-engineering"]},
        workload_profile={"document_count": 40, "attachment_count": 40},
        corpus_auto_workflow=True,
    )
    ids = {item["id"] for item in obligations}
    assert "source_inventory" in ids
    assert "document_render" in ids
    assert "implementation" in ids
    assert task == "Fix the attached source bug only."


def test_unchecked_corpus_workflow_does_not_force_document_gates_on_software():
    obligations = collect_plan_obligations(
        task="Fix the attached source bug only.",
        planning_mode="short_team",
        analysis={"level": "moderate", "domains": ["software-engineering"]},
        workload_profile={"document_count": 40, "attachment_count": 40},
        corpus_auto_workflow=False,
    )
    ids = {item["id"] for item in obligations}
    assert "source_inventory" not in ids
    assert "implementation" in ids
    assert "independent_validation" in ids


def test_fifty_step_software_plan_is_accepted():
    analysis = _software_analysis()
    steps = [
        _step(
            index, "developer", f"Feature Engineer {index + 1}",
            f"Implement workstream {index + 1}.",
            [] if index == 0 else [index - 1],
            ["implementation"], [],
            ["The workstream is runnable."],
        )
        for index in range(48)
    ]
    steps.append(_step(
        48, "debugger", "Autonomous Repair Engineer",
        "Repair root causes and rerun.", [47], ["debugging"], [],
        ["The suite exits with code 0."], ["python -m pytest -q"],
    ))
    steps.append(_step(
        49, "coordinator", "Final Requirement Traceability Auditor",
        "Audit every mandatory requirement.", [48], ["delivery audit"], [],
        ["No unsupported completion claim remains."],
    ))
    plan = {"analysis": analysis, "steps": steps}
    SimplePlanner._validate_generated_plan(
        plan, analysis, "auto", task="Build a software application with tests.",
    )
    assert len(plan["steps"]) == 50


def test_compiled_source_shards_still_satisfy_inventory_obligation():
    task = (
        "Rédige un dossier professionnel uniquement à partir du corpus et des "
        "fichiers locaux attachés, sans Internet.\n1. dossier.md"
    )
    analysis = {"level": "high", "domains": ["general"], "suggested_min_steps": 9}
    plan = SimplePlanner._fallback_plan(task, analysis, "auto")
    compiled = compile_work_graph(
        plan, {"suggested_partitions": 50, "document_count": 4_000}
    )
    obligations = collect_plan_obligations(
        task=task,
        planning_mode="auto",
        analysis=analysis,
        workload_profile={"document_count": 4_000, "attachment_count": 4_000},
        corpus_auto_workflow=True,
    )
    validate_plan_obligations(compiled["steps"], obligations)
    shards = [
        step for step in compiled["steps"]
        if step.get("source_partition", {}).get("count") == 50
    ]
    assert len(shards) == 50
    assert 50 < MAX_SOURCE_PARTITIONS


def test_delivery_contract_records_obligations_and_evaluate_detects_loss():
    analysis = _software_analysis("high")
    plan = {
        "analysis": analysis,
        "planning_mode": "auto",
        "requirements": [{"id": "REQ-001", "statement": "Expose a runnable local command"}],
        "steps": [
            _step(0, "developer", "CLI Engineer", "Implement the command.",
                  [], ["CLI"], ["src/sample/cli.py"], ["CLI runs"]),
            _step(1, "debugger", "Repair Engineer", "Repair failures.",
                  [0], ["debug"], [], ["Suite is green"], ["python -m pytest -q"]),
            _step(2, "coordinator", "Auditor", "Audit the outcome.",
                  [1], ["audit"], [], ["Evidence exists"]),
        ],
    }
    contract = build_delivery_contract(plan, "Build a software application with tests.")
    ids = {item["id"] for item in contract["plan_obligations"]}
    assert "implementation" in ids
    assert unsatisfied_obligations(plan["steps"], contract["plan_obligations"]) == []

    stripped = [step for step in plan["steps"] if step["role"] != "developer"]
    assert "implementation" in unsatisfied_obligations(
        stripped, contract["plan_obligations"]
    )
