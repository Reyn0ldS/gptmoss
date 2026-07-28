import asyncio
import json
from unittest.mock import AsyncMock, Mock
from pathlib import Path

import pytest

from gptmoss.capabilities.filesystem import FilesystemCapability
from gptmoss.core.context import ContextEngine
from gptmoss.core.event_bus import EventBus
from gptmoss.core.execution import ExecutionEngine, ProviderUnavailableError, normalize_plan
from gptmoss.core.skills import SkillRegistry
from gptmoss.core.state import StateEngine
from gptmoss.memory.ram import RAMMemoryProvider
from gptmoss.planners.simple import SimplePlanner, analyze_task_complexity
from gptmoss.policies.simple import SimplePolicyProvider
from tests.mock_llm import MockLLMProvider


AVATAR_PROMPT = (
    "j'aimerais un logiciel ou un programme, qui a partir d'une image de visage, peut créer un avatar en 3D. "
    "Ce programme doit ingérer l'image, puis en extrapoler un modèle 3D cohérent nue. De la même manière avec "
    "les vêtements, on doit pouvoir importer une image, le programme doit faire une extrapolation 3D, et on doit "
    "ensuite pouvoir faire porter ces vêtements aux différentes modèles 3D nue."
)


def _registry():
    return SkillRegistry([str(Path(__file__).resolve().parents[1] / "gptmoss" / "skills")])


def _engine(tmp_path, llm, max_iterations=8):
    state = StateEngine()
    engine = ExecutionEngine(
        EventBus(), state, ContextEngine(state, RAMMemoryProvider()), llm,
        SimplePlanner(llm), SimplePolicyProvider(approval_required_capabilities=[]),
        skill_registry=_registry(), max_step_iterations=max_iterations,
    )
    engine.register_capability("filesystem", FilesystemCapability(str(tmp_path), state))
    return engine, state


@pytest.mark.asyncio
async def test_provider_outage_is_persisted_as_resumable_wait(tmp_path):
    class UnavailablePlanner:
        async def plan(self, *args, **kwargs):
            error = ConnectionError("private provider offline")
            raise ProviderUnavailableError("provider unavailable", error)

    persist_path = tmp_path / "state.json"
    state = StateEngine(str(persist_path))
    event_bus = EventBus()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state, ContextEngine(state, RAMMemoryProvider()), llm,
        UnavailablePlanner(), SimplePolicyProvider(),
    )

    await engine.execute_task("durable-wait", "Build a complex local application")

    execution = state.get_execution("durable-wait")
    assert execution.status == "waiting_provider"
    assert execution.results.get("error") is None
    assert execution.variables["task"] == "Build a complex local application"
    restored = StateEngine(str(persist_path)).get_execution("durable-wait")
    assert restored.status == "waiting_provider"
    resume_task = engine._provider_resume_tasks.pop("durable-wait")
    resume_task.cancel()
    await asyncio.gather(resume_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_scope_reduction_requires_a_hashed_decision_before_resume(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("scope-decision")
    execution.status = "paused"
    execution.variables["task"] = "Build the complete application"
    execution.variables["pending_scope_approval"] = {
        "contract_sha256": "abc123",
        "changes": [{"id": "SCOPE-001", "statement": "Defer the UI"}],
    }
    engine.execute_task = AsyncMock()

    await engine.resolve_scope_approval("scope-decision", "allow", "accepted prototype")
    await asyncio.sleep(0)

    assert execution.status == "running"
    assert execution.variables["approved_scope_contract_sha256"] == "abc123"
    assert execution.variables["scope_decisions"][0]["reason"] == "accepted prototype"
    engine.execute_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_final_assurance_reopens_only_repair_and_auditor(tmp_path):
    llm = MockLLMProvider()
    delivery = json.dumps({
        "summary": "repaired", "artifacts": [], "evidence": [],
        "risks": [], "next_action": "audit",
    })
    llm.add_response(content=delivery)
    llm.add_response(content=delivery)
    llm.add_response(content="final audit")
    llm.add_response(content="final audit")
    engine, state = _engine(tmp_path, llm)
    parent = state.get_execution("assurance-repair")
    parent.current_plan = normalize_plan({"steps": [
        {
            "id": 0, "role": "developer", "specialist": "Completed Engineer",
            "description": "Existing implementation", "dependencies": [],
            "required_artifacts": [], "acceptance_criteria": [],
            "verification_commands": [], "status": "completed",
        },
        {
            "id": 1, "role": "debugger", "specialist": "Final Repair Engineer",
            "description": "Repair final defects", "dependencies": [0],
            "required_artifacts": [], "acceptance_criteria": [],
            "verification_commands": [], "status": "completed",
        },
        {
            "id": 2, "role": "coordinator", "specialist": "Final Auditor",
            "description": "Audit the final delivery", "dependencies": [1],
            "required_artifacts": [], "acceptance_criteria": [],
            "verification_commands": [], "status": "completed",
        },
    ]})
    failed_report = {"passed": False, "checks": [], "failures": ["CLI smoke failed"]}
    passed_report = {"passed": True, "checks": [], "failures": []}
    engine._independent_delivery_report = Mock(side_effect=[failed_report, passed_report])

    await engine.execute_task("assurance-repair", "Build a software application")

    assert parent.status == "completed"
    assert parent.variables["assurance_repair_round"] == 1
    assert engine._independent_delivery_report.call_count == 2
    children = [item for item in state.executions.values()
                if item.variables.get("parent_execution_id") == "assurance-repair"]
    assert len(children) == 1
    assert children[0].variables["plan_step_id"] == 1


def test_avatar_request_is_very_high_complexity_and_has_rich_safe_fallback():
    analysis = analyze_task_complexity(AVATAR_PROMPT)
    plan = SimplePlanner._fallback_plan(AVATAR_PROMPT, analysis)

    assert analysis["level"] == "very_high"
    assert len(plan["steps"]) >= 12
    specialists = [step["specialist"] for step in plan["steps"]]
    assert len(set(specialists)) == len(specialists)
    assert sum(step["role"] == "developer" for step in plan["steps"]) >= 5
    assert any("Face Reconstruction" in name for name in specialists)
    assert any("Garment Reconstruction" in name for name in specialists)
    assert any("End-to-End" in name for name in specialists)
    assert "no claim" in plan["analysis"]["mvp_boundary"]
    qa_step = next(step for step in plan["steps"] if step["specialist"] == "Geometry & ML Contract Test Engineer")
    assert "pytest.ini" in qa_step["required_artifacts"]
    assert "never src.avatar3d" in qa_step["description"]
    assert qa_step["verification_commands"] == ["python -m pytest --collect-only -q"]
    first_repair = next(step for step in plan["steps"] if step["specialist"] == "Autonomous Unit & Integration Repair Engineer")
    assert qa_step["id"] in first_repair["dependencies"]
    assert first_repair["verification_commands"] == ["python -m pytest -q"]
    assert plan["steps"][-2]["role"] == "writer"
    assert plan["steps"][-1]["role"] == "coordinator"


def test_plan_contract_preserves_specialist_quality_fields_and_rejects_bad_lists():
    plan = normalize_plan({"steps": [{
        "id": 0, "role": "developer", "specialist": "Vision Engineer",
        "description": "Implement image ingestion", "dependencies": [],
        "expertise": ["computer vision"], "required_artifacts": ["src/vision.py"],
        "acceptance_criteria": ["Images are validated"],
        "verification_commands": ["python -m pytest -q"],
    }]})
    assert plan["steps"][0]["specialist"] == "Vision Engineer"
    assert plan["steps"][0]["required_artifacts"] == ["src/vision.py"]

    with pytest.raises(ValueError, match="invalid expertise"):
        normalize_plan({"steps": [{"description": "bad", "expertise": "vision"}]})


def test_skills_are_selected_for_each_specialist_not_only_the_parent_task():
    registry = _registry()
    vision = {skill.name for skill in registry.select("Face Reconstruction Engineer computer vision inference")}
    garments = {skill.name for skill in registry.select("Garment Reconstruction Engineer cloth fitting draping")}
    integration = {skill.name for skill in registry.select("Autonomous Integration Repair Engineer acceptance tests root cause")}

    assert "computer-vision-ml" in vision
    assert "digital-garments" in garments
    assert "integration-delivery" in integration
    assert vision != garments


@pytest.mark.asyncio
async def test_workspace_full_autonomy_preapproves_current_and_future_shell_actions():
    policy = SimplePolicyProvider(
        approval_required_capabilities=["shell"], workspace_full_autonomy=True,
    )
    for action in ("execute", "future_action_not_known_at_build_time"):
        decision = await policy.check_action("exec", "shell", action, {}, {})
        assert decision.decision == "allow"

    denied = SimplePolicyProvider(
        approval_required_capabilities=["shell"], denied_capabilities=["shell"],
        workspace_full_autonomy=True,
    )
    assert (await denied.check_action("exec", "shell", "execute", {}, {})).decision == "deny"


@pytest.mark.asyncio
async def test_quality_gate_rejects_prose_then_agent_creates_artifact_and_finishes(tmp_path):
    llm = MockLLMProvider()
    llm.add_response(content="I am done.")
    llm.add_response(tool_calls=[{
        "id": "write-app", "type": "function",
        "function": {"name": "filesystem__write", "arguments": {"path": "src/app.py", "content": "VALUE = 42\n"}},
    }])
    delivery = {"summary": "implemented", "artifacts": ["src/app.py"],
                "evidence": ["artifact gate"], "risks": [], "next_action": ""}
    llm.add_response(content=json.dumps(delivery))
    engine, state = _engine(tmp_path, llm)

    execution = state.get_execution("child")
    execution.variables.update({"parent_execution_id": "parent", "role_key": "developer",
                                "role_name": "Core Engineer", "specialist": "Core Engineer"})
    execution.current_plan = {"steps": [{
        "id": 0, "role": "developer", "specialist": "Core Engineer",
        "description": "Create the application", "dependencies": [],
        "expertise": ["implementation"], "required_artifacts": ["src/app.py"],
        "acceptance_criteria": ["File exists"], "verification_commands": [],
    }]}

    await engine.execute_task("child", "Create the application")

    assert execution.status == "completed"
    artifact = tmp_path / "projects" / "proj-default" / "src" / "app.py"
    assert artifact.read_text(encoding="utf-8") == "VALUE = 42\n"
    assert any("Delivery rejected" in message.get("content", "")
               for message in state.get_conversation("child").messages)


@pytest.mark.asyncio
async def test_unsatisfied_quality_gate_fails_instead_of_reporting_completion(tmp_path):
    llm = MockLLMProvider()
    llm.add_response(content="done")
    llm.add_response(content="still done")
    engine, state = _engine(tmp_path, llm, max_iterations=2)
    execution = state.get_execution("child-fail")
    execution.variables.update({"parent_execution_id": "parent", "role_key": "writer",
                                "role_name": "Writer", "specialist": "Writer"})
    execution.current_plan = {"steps": [{
        "id": 0, "role": "writer", "specialist": "Writer", "description": "Write docs",
        "dependencies": [], "expertise": ["documentation"],
        "required_artifacts": ["README.md"], "acceptance_criteria": ["README exists"],
        "verification_commands": [],
    }]}

    await engine.execute_task("child-fail", "Write docs")

    assert execution.status == "failed"
    assert "did not satisfy its delivery gates" in execution.results["error"]
    assert not (tmp_path / "README.md").exists()


@pytest.mark.asyncio
async def test_productive_step_can_exceed_iteration_budget_until_delivery(tmp_path):
    llm = MockLLMProvider()
    for call_id, path in (("one", "notes/one.md"), ("two", "notes/two.md"), ("readme", "README.md")):
        llm.add_response(tool_calls=[{
            "id": call_id, "type": "function",
            "function": {
                "name": "filesystem__write",
                "arguments": {"path": path, "content": f"progress {call_id}\n"},
            },
        }])
    llm.add_response(content=json.dumps({
        "summary": "documented", "artifacts": ["README.md"],
        "evidence": ["durable files"], "risks": [], "next_action": "",
    }))
    engine, state = _engine(tmp_path, llm, max_iterations=1)
    execution = state.get_execution("long-writer")
    execution.variables.update({
        "parent_execution_id": "parent", "role_key": "writer",
        "role_name": "Writer", "specialist": "Writer",
    })
    execution.current_plan = {"steps": [{
        "id": 0, "role": "writer", "specialist": "Writer",
        "description": "Build documentation incrementally", "dependencies": [],
        "expertise": ["documentation"], "required_artifacts": ["README.md"],
        "acceptance_criteria": ["README exists"], "verification_commands": [],
    }]}

    await engine.execute_task("long-writer", "Build documentation incrementally")

    assert execution.status == "completed"
    assert llm.call_count == 4
    assert (tmp_path / "projects" / "proj-default" / "README.md").is_file()


def test_progress_ignores_repeated_success_and_identical_file_rewrite(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("progress")
    step = {"required_artifacts": []}
    root = tmp_path / "projects" / "proj-default"
    root.mkdir(parents=True)
    (root / "work.md").write_text("same\n", encoding="utf-8")
    execution.variables["tool_call_history"] = [{
        "capability": "shell", "action": "execute", "arguments": {"command": "python -m pytest -q"},
        "result": "EXIT_CODE: 0\n",
    }]
    before = engine._progress_signature("progress", step)
    (root / "work.md").write_text("same\n", encoding="utf-8")
    execution.variables["tool_call_history"].append(dict(execution.variables["tool_call_history"][0]))
    assert engine._progress_signature("progress", step) == before

    (root / "work.md").write_text("changed\n", encoding="utf-8")
    assert engine._progress_signature("progress", step) != before


def test_quality_delta_limits_file_churn_and_rewards_fewer_failures(tmp_path):
    engine, _ = _engine(tmp_path, MockLLMProvider())
    first = ((('app.py', 'hash-1'),), (), (), 5)
    second = ((('app.py', 'hash-2'),), (), (), 5)
    third = ((('app.py', 'hash-3'),), (), (), 5)
    fourth = ((('app.py', 'hash-4'),), (), (), 5)

    assert engine._quality_improved("quality", first, second)[0]
    assert engine._quality_improved("quality", second, third)[0]
    assert not engine._quality_improved("quality", third, fourth)[0]
    improved, kind = engine._quality_improved(
        "quality", fourth, ((('app.py', 'hash-4'),), (), (), 2)
    )
    assert improved
    assert kind == "fewer_machine_failures"


def test_tool_argument_normalization_recovers_wrappers_aliases_and_prefixed_path():
    wrapped = ExecutionEngine._normalize_tool_arguments(
        "filesystem", "write", {"parameters": {"file_path": "src/app.py", "text": "VALUE = 1\n"}},
    )
    assert wrapped == {"path": "src/app.py", "content": "VALUE = 1\n"}

    prefixed = ExecutionEngine._normalize_tool_arguments(
        "filesystem", "write", {"content": "tests/test_app.py\n```python\ndef test_app():\n    assert True\n```"},
    )
    assert prefixed["path"] == "tests/test_app.py"
    assert prefixed["content"].startswith("def test_app")


@pytest.mark.asyncio
async def test_transient_llm_errors_are_retried_without_losing_execution(tmp_path, monkeypatch):
    class TransientLLM(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.failures = 2

        async def completion(self, *args, **kwargs):
            if self.failures:
                self.failures -= 1
                raise TimeoutError("temporary provider timeout")
            return {"content": "recovered", "tool_calls": None, "usage": {}}

    llm = TransientLLM()
    engine, _ = _engine(tmp_path, llm)
    no_wait = AsyncMock()
    monkeypatch.setattr("gptmoss.core.execution.asyncio.sleep", no_wait)

    response = await engine._completion_with_recovery("recover", messages=[])

    assert response["content"] == "recovered"
    assert no_wait.await_count == 2


@pytest.mark.asyncio
async def test_parent_replaces_failed_specialist_and_completes_from_partial_workspace(tmp_path):
    llm = MockLLMProvider()
    llm.add_response(content="done without artifact")
    llm.add_response(content="still done without artifact")
    llm.add_response(tool_calls=[{
        "id": "retry-write", "type": "function",
        "function": {"name": "filesystem__write", "arguments": {"path": "app.py", "content": "READY = True\n"}},
    }])
    llm.add_response(content=json.dumps({
        "summary": "recovered", "artifacts": ["app.py"], "evidence": ["file gate"],
        "risks": [], "next_action": "",
    }))
    engine, state = _engine(tmp_path, llm, max_iterations=2)
    engine.max_step_retries = 1
    parent = state.get_execution("retry-parent")
    parent.current_plan = {"steps": [{
        "id": 0, "role": "developer", "specialist": "Recovery Engineer",
        "description": "Create a runnable app", "dependencies": [],
        "expertise": ["implementation"], "required_artifacts": ["app.py"],
        "acceptance_criteria": ["App exists"], "verification_commands": [],
    }]}

    await engine.execute_task("retry-parent", "Create a runnable app")

    children = [execution for execution in state.executions.values()
                if execution.variables.get("parent_execution_id") == "retry-parent"]
    assert parent.status == "completed"
    assert parent.current_plan["steps"][0]["retry_count"] == 1
    assert sorted(child.status for child in children) == ["completed", "failed"]
    assert (tmp_path / "projects" / "proj-default" / "app.py").is_file()


def test_verification_gate_requires_the_exact_declared_successful_command(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("verify")
    execution.variables["tool_call_history"] = [{
        "capability": "shell", "action": "execute", "arguments": {"command": "dir"},
        "result": "EXIT_CODE: 0\n",
    }]
    step = {"description": "Verify", "verification_commands": ["python -m pytest -q"]}
    assert "python -m pytest -q" in " ".join(engine._step_completion_issues("verify", step, "done"))


@pytest.mark.asyncio
async def test_stall_rescue_generates_missing_text_artifact_in_clean_context(tmp_path):
    llm = MockLLMProvider()
    llm.add_response(content="VALUE = 123\nREADY = True\n")
    engine, state = _engine(tmp_path, llm)
    execution = state.get_execution("rescue")
    execution.variables.update({"project_id": "proj-rescue", "parent_task": "Build an app"})
    step = {
        "role": "developer", "specialist": "Core Engineer", "description": "Implement core",
        "expertise": ["Python"], "required_artifacts": ["src/core.py"],
        "acceptance_criteria": ["Core is runnable"], "verification_commands": [],
    }

    rescued = await engine._rescue_missing_artifacts("rescue", step, [])

    assert rescued == ["src/core.py"]
    assert (tmp_path / "projects" / "proj-rescue" / "src" / "core.py").read_text(encoding="utf-8") == "VALUE = 123\nREADY = True\n"


def test_rescue_strips_prefixed_fence_and_rejects_mock_random_tests():
    raw = "tests/test_real.py\n```python\nfrom avatar3d.body import Body\n\ndef test_body():\n    assert Body\n```"
    cleaned = ExecutionEngine._strip_code_fence(raw, "tests/test_real.py")
    assert "```" not in cleaned
    assert cleaned.startswith("from avatar3d.body")
    assert ExecutionEngine._rescue_content_issues("tests/test_real.py", cleaned) == []

    bad = "import numpy as np\nclass MockMesh: pass\ndef test_fake(): np.random.rand(2)\n"
    issues = ExecutionEngine._rescue_content_issues("tests/test_fake.py", bad)
    assert any("actual avatar3d" in issue for issue in issues)
    assert any("mocks" in issue for issue in issues)

    wrong_identity = "from src.avatar3d.body import Body\ndef test_body(): assert Body\n"
    identity_issues = ExecutionEngine._rescue_content_issues("tests/test_identity.py", wrong_identity)
    assert any("canonical avatar3d" in issue for issue in identity_issues)


def test_integration_gate_rejects_duplicate_src_package_identity_and_bad_pytest_option(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    state.get_execution("identity")
    root = tmp_path / "projects" / "proj-default"
    package = root / "src" / "avatar3d"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "bad.py").write_text("from src.avatar3d.geometry import Mesh\n", encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\npython_paths = src\n", encoding="utf-8")

    issues = engine._integration_contract_issues("identity")

    assert any("single canonical avatar3d" in issue for issue in issues)
    assert any("pythonpath" in issue for issue in issues)
