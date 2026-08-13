import pytest

from gptmoss.planners.simple import SimplePlanner, analyze_task_complexity


PLANNING_CASES = [
    ("direct", "Translate this sentence into French."),
    ("software", "Build a software application with a REST API, persistence and tests."),
    (
        "complex-cross-domain",
        "Build a software application with a user interface, machine learning inference, privacy controls and "
        "offline delivery; implement validated imports, support recovery, create tests and document operations.",
    ),
    (
        "local-document",
        "Rédige un dossier professionnel uniquement à partir du corpus et des fichiers locaux attachés, sans Internet.\n"
        "1. dossier.md\n2. evidence-matrix.md\n3. review-report.html",
    ),
]


@pytest.mark.parametrize("mode,task", PLANNING_CASES)
def test_planner_modes_produce_valid_causal_dependency_graphs(mode, task):
    analysis = analyze_task_complexity(task)
    plan = SimplePlanner._fallback_plan(task, analysis)
    SimplePlanner._validate_generated_plan(plan, analysis)

    steps = plan["steps"]
    positions = {str(step["id"]): index for index, step in enumerate(steps)}
    assert len(positions) == len(steps)
    for index, step in enumerate(steps):
        assert step["status"] == "pending"
        assert all(positions[str(dependency)] < index for dependency in step["dependencies"])
        assert step["specialist"] and step["acceptance_criteria"]

    if mode == "direct":
        assert len(steps) == 1 and steps[0]["role"] == "coordinator"
    elif mode == "software":
        assert 3 <= len(steps) <= 5
        assert steps[-1]["role"] == "coordinator"
        assert any(step["role"] == "developer" for step in steps)
        assert any(step["role"] in {"qa", "debugger"} for step in steps)
    else:
        assert len(steps) >= analysis["suggested_min_steps"]
        assert steps[-1]["role"] == "coordinator"
        assert any(step["role"] == "qa" for step in steps)
        assert any(step["role"] == "debugger" for step in steps)

    if mode == "complex-cross-domain":
        assert steps[2]["specialist"] == "External Tool Contract Engineer"
        assert "execution_routines" in steps[2]["description"]
    elif mode == "local-document":
        assert steps[0]["specialist"] == "Local Corpus Evidence Analyst"
        assert "without using Internet evidence" in steps[0]["description"]
        assert plan["artifact_validations"]
        assert any(item["validator"] == "document" for item in plan["artifact_validations"])
        assert plan["scope_changes"] == []


def test_explicit_planning_modes_override_complexity_sizing():
    software = "Build a software application with a REST API, persistence and tests."
    analysis = analyze_task_complexity(software)

    direct = SimplePlanner._fallback_plan(software, analysis, "direct")
    SimplePlanner._validate_generated_plan(direct, analysis, "direct")
    assert len(direct["steps"]) == 1
    assert direct["steps"][0]["role"] == "coordinator"

    short = SimplePlanner._fallback_plan(software, {**analysis, "level": "high"}, "short_team")
    SimplePlanner._validate_generated_plan(short, {**analysis, "level": "high"}, "short_team")
    assert 3 <= len(short["steps"]) <= 5

    full = SimplePlanner._fallback_plan(software, {**analysis, "level": "low"}, "full_team")
    assert len(full["steps"]) >= 9
    assert any(step["role"] == "debugger" for step in full["steps"])


def test_generated_plan_validation_uses_obligations_not_a_step_floor():
    from gptmoss.planners.fallbacks import _step

    analysis = {
        "level": "very_high",
        "score": 20,
        "domains": ["software-engineering"],
        "requested_outcomes": 4,
        "suggested_min_steps": 12,
    }
    task = "Build a software application with tests."
    compact = {
        "analysis": analysis,
        "steps": [
            _step(0, "developer", "Implementation Engineer",
                  "Implement the requested software.", [], ["implementation"], [],
                  ["The change is runnable."]),
            _step(1, "qa", "Independent Test Engineer",
                  "Add independent tests.", [0], ["test engineering"],
                  ["tests/test_acceptance.py"], ["Tests cover the change."],
                  ["python -m pytest --collect-only -q"]),
            _step(2, "debugger", "Autonomous Repair Engineer",
                  "Repair root causes and rerun.", [1], ["debugging"], [],
                  ["The suite exits with code 0."], ["python -m pytest -q"]),
            _step(3, "coordinator", "Final Requirement Traceability Auditor",
                  "Audit the requested outcome.", [2], ["delivery audit"], [],
                  ["No unsupported completion claim remains."]),
        ],
    }
    SimplePlanner._validate_generated_plan(compact, analysis, "auto", task=task)
    assert len(compact["steps"]) < analysis["suggested_min_steps"]
    assert {item["id"] for item in compact["plan_obligations"]} >= {
        "implementation", "independent_validation", "autonomous_repair", "final_audit",
    }

    incomplete = {
        "analysis": analysis,
        "steps": [
            _step(0, "coordinator", "Task Specialist",
                  "Perform the user task.", [], ["general"], [],
                  ["The requested outcome is delivered."]),
        ],
    }
    with pytest.raises(ValueError, match="missing required delivery obligations"):
        SimplePlanner._validate_generated_plan(incomplete, analysis, "auto", task=task)


def test_generic_team_fallback_keeps_a_producer_and_auditor():
    task = "Summarize this meeting for the steering committee in two pages."
    analysis = analyze_task_complexity(task)
    short = SimplePlanner._fallback_plan(task, {**analysis, "level": "high"}, "short_team")
    SimplePlanner._validate_generated_plan(
        short, {**analysis, "level": "high"}, "short_team", task=task,
    )
    assert [step["role"] for step in short["steps"]] == ["architect", "coordinator"]

    auto_high = SimplePlanner._fallback_plan(task, {**analysis, "level": "very_high"}, "auto")
    SimplePlanner._validate_generated_plan(
        auto_high, {**analysis, "level": "very_high"}, "auto", task=task,
    )
    assert len(auto_high["steps"]) == 2


def test_thirteen_steps_are_a_fallback_shape_not_a_global_fixed_count():
    direct_task = "Translate this sentence into French."
    software_task = "Build a software application with a REST API, persistence and tests."
    complex_task = PLANNING_CASES[2][1]

    counts = {
        len(SimplePlanner._fallback_plan(task, analyze_task_complexity(task))["steps"])
        for task in (direct_task, software_task, complex_task)
    }

    assert len(counts) >= 3
    assert 1 in counts
    assert 13 in counts
