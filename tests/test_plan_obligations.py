import pytest

from gptmoss.core.delivery import build_delivery_contract, evaluate_delivery
from gptmoss.core.plan_obligations import (
    attach_plan_obligations,
    collect_plan_obligations,
    obligation_issues,
    unsatisfied_obligations,
    validate_plan_obligations,
)
from gptmoss.core.workload import MAX_SOURCE_PARTITIONS, compile_work_graph
from gptmoss.planners.complexity import analyze_task_complexity
from gptmoss.planners.fallbacks import _step
from gptmoss.planners.simple import SimplePlanner
from tests.mock_llm import MockLLMProvider


def _software_analysis(level="very_high"):
    return {
        "level": level,
        "score": 20,
        "domains": ["software-engineering"],
        "requested_outcomes": 4,
        "suggested_min_steps": 12,
    }


def test_direct_mode_coalesces_required_work_without_expanding_the_plan():
    obligations = collect_plan_obligations(
        task="Build a software application with tests.",
        planning_mode="direct",
        analysis=_software_analysis(),
    )
    assert [item["id"] for item in obligations] == ["implementation"]
    assert obligations[0]["coalesced"] is True


def test_direct_professional_corpus_keeps_inventory_and_final_artifact_in_one_step():
    task = "Produce a professional report from the selected local folder."
    plan = {
        "planning_mode": "direct",
        "primary_artifact": "deliverables/final-report.docx",
        "steps": [
            _step(
                0, "coordinator", "Direct Task Specialist", task, [],
                ["local evidence", "professional writing"], [],
                ["The report and its source coverage are delivered."],
            )
        ],
    }
    attach_plan_obligations(
        plan,
        task=task,
        planning_mode="direct",
        workload_profile={"attachment_count": 20, "document_count": 20},
        corpus_policy={"enabled": True, "professional_delivery": True},
        repair=True,
        validate=True,
    )

    assert len(plan["steps"]) == 1
    assert {
        "source_inventory", "document_render",
    } <= set(plan["steps"][0]["satisfies_obligations"])
    assert plan["steps"][0]["required_artifacts"] == [
        "analysis/corpus-inventory.md",
        "deliverables/final-report.docx",
    ]


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


def test_software_architecture_dossier_does_not_inject_implementation_work():
    task = (
        "Produce a professional dossier from the local corpus. Analyze the API, GUI, "
        "runtime, source code architecture, tests, scheduling and recovery. Include "
        "Mermaid diagrams and a DOCX package, but do not change the application."
    )
    analysis = analyze_task_complexity(task)
    assert "software-engineering" in analysis["domains"]
    assert analysis["software_implementation_requested"] is False

    plan = SimplePlanner._fallback_plan(
        task, analysis, "auto",
        corpus_policy={"enabled": True, "professional_delivery": True},
    )
    attach_plan_obligations(
        plan,
        task=task,
        planning_mode="auto",
        analysis=analysis,
        workload_profile={"attachment_count": 4, "document_count": 4},
        corpus_policy={"enabled": True, "professional_delivery": True},
        repair=True,
        validate=True,
    )

    assert "implementation" not in {
        item["id"] for item in plan["plan_obligations"]
    }
    assert not any(step.get("role") == "developer" for step in plan["steps"])


def test_corpus_folder_does_not_turn_software_implementation_into_a_dossier():
    task = (
        "Construire une application locale et portable qui expose une API "
        "et une interface, avec des tests complets."
    )
    analysis = analyze_task_complexity(task)
    plan = SimplePlanner._fallback_plan(
        task, analysis, "auto",
        corpus_policy={"enabled": True, "professional_delivery": True},
        workload_profile={"attachment_count": 4, "document_count": 4},
    )
    assert any(step.get("role") == "developer" for step in plan["steps"])
    assert plan.get("delivery_profile") != "professional-local"
    assert not any(
        "corpus" in str(step.get("specialist") or "").casefold()
        for step in plan["steps"]
    )


def test_acceptance_test_in_a_dossier_is_not_software_implementation():
    task = "Créer un test d'acceptation dans le dossier à partir des pièces jointes."
    analysis = analyze_task_complexity(task)
    assert analysis["software_implementation_requested"] is False
    plan = SimplePlanner._fallback_plan(
        task, analysis, "auto",
        workload_profile={"attachment_count": 3, "document_count": 3},
    )
    assert not any(step.get("role") == "developer" for step in plan["steps"])


def test_french_create_application_is_software_implementation():
    task = (
        "Construire une application locale et portable qui expose une API "
        "et une interface, avec des tests complets."
    )
    analysis = analyze_task_complexity(task)
    assert analysis["software_implementation_requested"] is True
    plan = SimplePlanner._fallback_plan(task, analysis, "auto")
    assert any(step.get("role") == "developer" for step in plan["steps"])


def test_french_project_dossier_with_attachments_is_not_a_software_dag():
    task = "Rédige un dossier du projet à partir des pièces jointes."
    analysis = analyze_task_complexity(task)
    assert "software-engineering" in analysis["domains"]
    assert analysis["software_implementation_requested"] is False

    plan = SimplePlanner._fallback_plan(
        task, analysis, "auto",
        workload_profile={"attachment_count": 4, "document_count": 4},
    )
    assert not any(step.get("role") == "developer" for step in plan["steps"])
    assert plan.get("delivery_profile") == "professional-local" or any(
        "corpus" in str(step.get("specialist") or "").casefold()
        for step in plan["steps"]
    )


@pytest.mark.parametrize("task", [
    "Build a runnable software application and its tests.",
    "Fix the API authentication bug in the existing source code.",
    "Ajoute une fonctionnalite a la GUI et mets a jour les tests.",
])
def test_explicit_software_mutation_still_requires_implementation(task):
    analysis = analyze_task_complexity(task)
    obligations = collect_plan_obligations(
        task=task, planning_mode="auto", analysis=analysis,
    )

    assert analysis["software_implementation_requested"] is True
    assert "implementation" in {item["id"] for item in obligations}


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
    assert len(plan["steps"]) == 51
    inserted = [step for step in plan["steps"] if step.get("runtime_inserted")]
    assert [step["role"] for step in inserted] == ["qa"]
    assert plan["steps"][-1]["role"] == "coordinator"


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
    assert any(step["role"] == "qa" for step in plan["steps"])

    stripped = [step for step in plan["steps"] if step["role"] != "developer"]
    assert "implementation" in unsatisfied_obligations(
        stripped, contract["plan_obligations"]
    )


def test_keywords_without_structured_work_or_evidence_do_not_satisfy_gates():
    steps = [
        _step(0, "architect", "Generic", "Inventory attachments and write deliverable.",
              [], ["general"], [], ["Done"]),
        _step(1, "coordinator", "Generic", "Audit result.",
              [], ["general"], [], ["Done"]),
    ]
    obligations = collect_plan_obligations(
        task="Produce a professional report from attached files.",
        planning_mode="auto",
        analysis={"level": "high", "domains": []},
        workload_profile={"attachment_count": 400, "document_count": 400},
        corpus_auto_workflow=True,
    )

    issues = obligation_issues(steps, obligations)

    assert "missing:source_inventory" in issues
    assert "missing:document_render" in issues
    assert "missing:independent_validation" in issues


def test_invalid_llm_fallback_is_repaired_for_professional_corpus_delivery():
    task = (
        "Analyse intégralement le dossier source sélectionné et produis un dossier "
        "professionnel avec rapport de couverture du corpus."
    )
    analysis = {"level": "low", "domains": [], "suggested_min_steps": 1}
    plan = SimplePlanner._fallback_plan(
        task, analysis, "auto",
        corpus_policy={"enabled": True, "professional_delivery": True},
    )
    profile = {"attachment_count": 400, "document_count": 400, "suggested_partitions": 5}
    attach_plan_obligations(
        plan,
        task=task,
        planning_mode="auto",
        analysis=analysis,
        workload_profile=profile,
        corpus_policy={"enabled": True, "professional_delivery": True},
        repair=True,
        validate=True,
    )
    compiled = compile_work_graph(plan, profile)
    validate_plan_obligations(compiled["steps"], plan["plan_obligations"])

    assert any(step["role"] == "writer" and step["required_artifacts"]
               for step in compiled["steps"])
    assert any(step["role"] == "qa" for step in compiled["steps"])
    assert compiled["steps"][-1]["role"] == "coordinator"


@pytest.mark.asyncio
async def test_planner_provider_failure_returns_a_valid_repaired_corpus_plan():
    llm = MockLLMProvider()
    llm.add_response("not-json")
    task = (
        "Analyse intégralement le dossier source sélectionné et produis un dossier "
        "professionnel avec rapport de couverture du corpus."
    )
    profile = {"attachment_count": 400, "document_count": 400, "suggested_partitions": 5}
    policy = {"enabled": True, "professional_delivery": True, "source_kind": "corpus"}

    plan = await SimplePlanner(llm).plan(
        task,
        {"variables": {"corpus_auto_workflow": True, "corpus_policy": policy}},
        [],
        workload_profile=profile,
        corpus_auto_workflow=True,
        corpus_policy=policy,
    )

    validate_plan_obligations(plan["steps"], plan["plan_obligations"])
    assert any(step["role"] == "writer" for step in plan["steps"])
    assert any(step["role"] == "qa" for step in plan["steps"])
    assert plan["steps"][-1]["role"] == "coordinator"


def test_delivery_requires_actual_document_tool_evidence_for_corpus_policy(tmp_path):
    task = "Produce a professional report from the attached local corpus."
    plan = {
        "analysis": {"level": "high", "domains": []},
        "planning_mode": "auto",
        "workload_profile": {"attachment_count": 1, "document_count": 1},
        "corpus_policy": {
            "enabled": True,
            "professional_delivery": True,
            "source_kind": "corpus",
            "document_count": 1,
        },
        "steps": [
            _step(0, "coordinator", "Initial Coordinator", "Coordinate delivery.",
                  [], ["general"], [], ["Delivery is checked."]),
        ],
    }
    contract = build_delivery_contract(plan, task)
    for step in plan["steps"]:
        for relative in step.get("required_artifacts", []):
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# Verified evidence\n\nConcrete local result and traceability.\n", encoding="utf-8")

    missing = evaluate_delivery(tmp_path, contract, plan["steps"], [])
    evidenced = evaluate_delivery(
        tmp_path,
        contract,
        plan["steps"],
        [{"capability": "documents", "action": "read", "arguments": {}, "result": "{}"}],
    )

    assert missing["passed"] is False
    assert any("documents.read evidence" in item for item in missing["failures"])
    assert evidenced["passed"] is True
