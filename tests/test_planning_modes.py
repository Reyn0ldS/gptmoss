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
