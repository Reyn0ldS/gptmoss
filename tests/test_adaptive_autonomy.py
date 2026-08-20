import asyncio
import base64
import json
from unittest.mock import AsyncMock, Mock
from pathlib import Path

import pytest

from gptmoss.capabilities.filesystem import FilesystemCapability
from gptmoss.capabilities.documents import DocumentCapability
from gptmoss.capabilities.shell import ShellCapability
from gptmoss.core.context import ContextEngine
from gptmoss.core.adaptive import tool_call_fingerprint
from gptmoss.core.delivery import build_delivery_contract
from gptmoss.core.artifacts import ArtifactStore
from gptmoss.core.event_bus import EventBus
from gptmoss.core.execution import (
    ExecutionEngine,
    ProviderConfigurationError,
    ProviderUnavailableError,
    normalize_plan,
)
from gptmoss.core.skills import SkillRegistry
from gptmoss.core.domains import ProjectDomainRegistry
from gptmoss.core.state import StateEngine
from gptmoss.memory.ram import RAMMemoryProvider
from gptmoss.planners.simple import SimplePlanner, analyze_task_complexity
from gptmoss.policies.simple import SimplePolicyProvider
from tests.mock_llm import MockLLMProvider


COMPLEX_PROJECT_PROMPT = (
    "Construire une application locale et portable qui ingère des documents, images et données, "
    "automatise leur validation, applique des règles de sécurité et de confidentialité, expose une API "
    "et une interface accessible, produit des rapports auditables, fonctionne hors ligne, sauvegarde son "
    "état et reprend les traitements interrompus avec des tests complets."
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


def test_project_domain_registry_is_generic_and_project_extensible():
    registry = ProjectDomainRegistry()
    registry.register("legal-case-management", ["dossier contentieux", "jurisprudence"])

    generic = analyze_task_complexity("Construire une application offline avec API", registry)
    specialized = analyze_task_complexity(
        "Organiser un dossier contentieux et sa jurisprudence", registry
    )

    assert "software-engineering" in generic["domains"]
    assert "offline-and-operations" in generic["domains"]
    assert "legal-case-management" in specialized["domains"]
    assert not any("avatar" in domain or "garment" in domain for domain in generic["domains"])


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
    resume_job = engine.provider_recovery.jobs.pop("durable-wait")
    assert engine.scheduler.cancel(resume_job)
    await engine.stop_runtime_services()


@pytest.mark.asyncio
async def test_scope_reduction_requires_a_hashed_decision_before_resume(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("scope-decision")
    execution.status = "paused"
    execution.variables["task"] = "Build the complete application"
    execution.variables["pending_scope_approval"] = {
        "contract_sha256": "abc123",
        "scope_changes_sha256": "scope123",
        "changes": [{"id": "SCOPE-001", "statement": "Defer the UI"}],
    }
    engine.execute_task = AsyncMock()

    await engine.resolve_scope_approval("scope-decision", "allow", "accepted prototype")
    await asyncio.sleep(0)

    assert execution.status == "running"
    assert execution.variables["approved_scope_contract_sha256"] == "abc123"
    assert execution.variables["approved_scope_changes_sha256"] == "scope123"
    assert execution.variables["scope_decisions"][0]["reason"] == "accepted prototype"
    engine.execute_task.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_child_approval_decision_clears_parent_mirror(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    parent = state.get_execution("approval-parent")
    parent.status = "paused"
    parent.variables.update({
        "task": "Parent task",
        "pending_approval": {"child_execution_id": "approval-child"},
    })
    child = state.get_execution("approval-child")
    child.status = "paused"
    child.variables.update({
        "task": "Child task",
        "parent_execution_id": "approval-parent",
        "pending_approval": {
            "tool_call_id": "gate",
            "capability": "devteam",
            "action": "approve_quality_gate",
            "arguments": {"test_output": "incomplete"},
        },
    })
    engine.execute_task = AsyncMock()

    await engine.resume_with_decision("approval-child", "reject", "insufficient")
    await asyncio.sleep(0)

    assert child.status == "running"
    assert child.variables["pending_approval"]["decision"] == "reject"
    assert parent.status == "running"
    assert "pending_approval" not in parent.variables
    assert engine.execute_task.await_count == 2


@pytest.mark.asyncio
async def test_stale_parent_approval_mirror_is_cleared_without_500(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    parent = state.get_execution("stale-parent")
    parent.status = "paused"
    parent.variables.update({
        "task": "Resume parent",
        "pending_approval": {"child_execution_id": "finished-child"},
    })
    child = state.get_execution("finished-child")
    child.status = "completed"
    child.variables["parent_execution_id"] = "stale-parent"
    engine.execute_task = AsyncMock()

    await engine.resume_with_decision("stale-parent", "reject", "already handled")
    await asyncio.sleep(0)

    assert parent.status == "running"
    assert "pending_approval" not in parent.variables
    engine.execute_task.assert_awaited_once_with("stale-parent", "Resume parent")


def test_terminal_coordinator_auto_finalizes_only_after_independent_assurance(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("terminal-audit")
    implementation = {
        "id": 0, "role": "developer", "description": "Implement",
        "dependencies": [], "status": "completed",
    }
    auditor = {
        "id": 1, "role": "coordinator", "specialist": "Final Auditor",
        "description": "Audit the delivery", "dependencies": [0],
        "acceptance_criteria": ["Independent assurance passes"], "status": "running",
    }
    execution.current_plan = {"steps": [implementation, auditor], "requirements": []}
    engine._independent_delivery_report = Mock(return_value={
        "passed": True, "checks": [{"name": "required_artifacts", "passed": True}],
        "failures": [],
    })

    assert engine._can_engine_finalize("terminal-audit", auditor)

    engine._independent_delivery_report.return_value = {
        "passed": False, "checks": [], "failures": ["verification failed"],
    }
    assert not engine._can_engine_finalize("terminal-audit", auditor)

    engine._independent_delivery_report.return_value = {
        "passed": True, "checks": [], "failures": [],
    }
    implementation["status"] = "running"
    assert not engine._can_engine_finalize("terminal-audit", auditor)


def test_coordinator_reuses_exact_verification_evidence_from_delegated_children(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    parent = state.get_execution("evidence-parent")
    auditor = {
        "id": 1, "role": "coordinator", "specialist": "Final Auditor",
        "description": "Audit the delivery", "dependencies": [0],
        "acceptance_criteria": ["All exact verification commands passed"],
    }
    parent.current_plan = {
        "requirements": [{
            "id": "REQ-TEST", "statement": "Validate with `python -m pytest -q`",
            "acceptance": [],
        }],
        "steps": [auditor],
    }

    initial_issues = engine._step_completion_issues("evidence-parent", auditor, "done")
    assert any("python -m pytest -q" in issue for issue in initial_issues)

    child = state.get_execution("evidence-child")
    child.variables.update({
        "parent_execution_id": "evidence-parent",
        "tool_call_history": [{
            "capability": "shell", "action": "execute",
            "arguments": {"command": "python -m pytest -q"},
            "result": "224 passed\\nEXIT_CODE: 0",
        }],
    })

    assert engine._step_completion_issues("evidence-parent", auditor, "done") == []


@pytest.mark.asyncio
async def test_resumed_converged_coordinator_finalizes_before_another_llm_call(tmp_path):
    llm = MockLLMProvider()
    engine, state = _engine(tmp_path, llm)
    execution = state.get_execution("preflight-audit")
    implementation = {
        "id": 0, "role": "developer", "description": "Implement",
        "dependencies": [], "status": "completed",
    }
    auditor = {
        "id": 1, "role": "coordinator", "specialist": "Final Auditor",
        "description": "Audit the delivery", "dependencies": [0],
        "acceptance_criteria": ["Independent assurance passes"], "status": "running",
    }
    execution.current_plan = {"steps": [implementation, auditor], "requirements": []}
    execution.variables["step_runtime"] = {
        "1": {"iterations": 4, "stagnant_iterations": 1},
    }
    engine._independent_delivery_report = Mock(return_value={
        "passed": True,
        "checks": [{"name": "independent_machine_evidence", "passed": True}],
        "failures": [],
    })

    result = json.loads(await engine._execute_step_loop("preflight-audit", auditor))

    assert llm.call_count == 0
    assert result["next_action"] == "Deliver the independently assured result."
    assert "independent assurance passed" in result["evidence"][0]


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


@pytest.mark.asyncio
async def test_failed_document_assurance_reopens_writer_not_debugger(tmp_path):
    llm = MockLLMProvider()
    delivery = json.dumps({
        "summary": "repaired", "artifacts": ["dossier.md"], "evidence": [],
        "risks": [], "next_action": "audit",
    })
    llm.add_response(content=delivery)
    llm.add_response(content="final audit")
    engine, state = _engine(tmp_path, llm)
    parent = state.get_execution("doc-assurance")
    parent.current_plan = normalize_plan({"steps": [
        {
            "id": 0, "role": "writer", "specialist": "Writer",
            "description": "Write the dossier", "dependencies": [],
            "required_artifacts": ["dossier.md"], "acceptance_criteria": ["done"],
            "verification_commands": [], "status": "completed",
        },
        {
            "id": 1, "role": "debugger", "specialist": "Repair",
            "description": "Repair defects", "dependencies": [0],
            "required_artifacts": [], "acceptance_criteria": [],
            "verification_commands": [], "status": "completed",
        },
        {
            "id": 2, "role": "coordinator", "specialist": "Auditor",
            "description": "Audit the final delivery", "dependencies": [1],
            "required_artifacts": [], "acceptance_criteria": [],
            "verification_commands": [], "status": "completed",
        },
    ]})
    failed_report = {
        "passed": False,
        "checks": [{"name": "artifact_structure_and_constraints", "passed": False}],
        "failures": ["duplicate paragraph occurrence(s)"],
    }
    passed_report = {"passed": True, "checks": [], "failures": []}
    engine._independent_delivery_report = Mock(side_effect=[failed_report, passed_report])

    await engine.execute_task("doc-assurance", "Rédige un dossier professionnel")

    children = [item for item in state.executions.values()
                if item.variables.get("parent_execution_id") == "doc-assurance"]
    assert children
    assert children[0].variables["plan_step_id"] == 0
    runtime = parent.variables.get("step_runtime") or {}
    assert runtime.get("0", {}).get("required_next_tool") == "filesystem__replace_paragraph"


def test_cross_domain_request_has_rich_engine_agnostic_fallback():
    analysis = analyze_task_complexity(COMPLEX_PROJECT_PROMPT)
    plan = SimplePlanner._fallback_plan(COMPLEX_PROJECT_PROMPT, analysis)

    assert analysis["level"] in {"high", "very_high"}
    assert len(plan["steps"]) >= 12
    specialists = [step["specialist"] for step in plan["steps"]]
    assert len(set(specialists)) == len(specialists)
    assert sum(step["role"] == "developer" for step in plan["steps"]) >= 4
    assert "External Tool Contract Engineer" in specialists
    assert "Independent Contract Test Engineer" in specialists
    assert "Clean-Process Acceptance Engineer" in specialists
    external_step = next(step for step in plan["steps"] if step["specialist"] == "External Tool Contract Engineer")
    assert "execution_routines" in external_step["description"]
    assert "Do not claim GUI operation" in external_step["description"]
    qa_step = next(step for step in plan["steps"] if step["specialist"] == "Independent Contract Test Engineer")
    assert qa_step["verification_commands"] == ["python -m pytest --collect-only -q"]
    first_repair = next(step for step in plan["steps"] if step["specialist"] == "Autonomous Integration Repair Engineer")
    assert qa_step["id"] in first_repair["dependencies"]
    assert first_repair["verification_commands"] == ["python -m pytest -q"]
    assert plan["steps"][-2]["role"] == "writer"
    assert plan["steps"][-1]["role"] == "coordinator"
    assert {item["id"] for item in plan["requirements"]} == {"REQ-DELIVERY"}
    assert all(step["requirement_ids"] == ["REQ-DELIVERY"] for step in plan["steps"])
    assert not any(
        str(path).startswith("src/fixed-domain-package")
        for step in plan["steps"] for path in step["required_artifacts"]
    )
    assert plan["scope_changes"] == []


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
    explicit_media = {
        skill.name for skill in registry.select(
            "Project-specific media adapter", requested=["computer-vision-ml"]
        )
    }
    integration = {skill.name for skill in registry.select("Autonomous Integration Repair Engineer acceptance tests root cause")}

    assert "computer-vision-ml" not in vision
    assert "computer-vision-ml" in explicit_media
    assert "integration-delivery" in integration
    assert all(skill.auto_select for skill in registry.select("generic software integration"))


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
    execution.variables["tool_call_history"].append({
        "capability": "shell", "action": "execute", "arguments": {"command": "dir /b"},
        "result": "EXIT_CODE: 0\n",
    })
    assert engine._progress_signature("progress", step) == before
    (root / "work.md").write_bytes(b"same\r\n")
    assert engine._progress_signature("progress", step) == before
    (root / "tmp_fix_module.py").write_text("repair helper\n", encoding="utf-8")
    (root / "test_results.txt").write_text("volatile output\n", encoding="utf-8")
    (root / "test_output_full.txt").write_text("volatile full output\n", encoding="utf-8")
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


def test_progress_failure_count_ignores_unrelated_shell_probes(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("failure-progress")
    root = tmp_path / "projects" / "proj-default"
    root.mkdir(parents=True)
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    step = {
        "required_artifacts": [],
        "verification_commands": ["python -m pytest -q"],
    }
    execution.variables["tool_call_history"] = [{
        "capability": "shell", "action": "execute",
        "arguments": {"command": "python -m pytest -q"},
        "result": "EXIT_CODE: 1\n5 failed\n",
    }]
    failed = engine._progress_signature("failure-progress", step)

    execution.variables["tool_call_history"].extend([{
        "capability": "shell", "action": "execute",
        "arguments": {"command": "where python"},
        "result": "EXIT_CODE: 1\n",
    }, {
        "capability": "shell", "action": "execute",
        "arguments": {"command": "findstr Config agentbench\\models.py"},
        "result": "EXIT_CODE: 0\n",
    }])

    assert engine._progress_signature("failure-progress", step) == failed
    execution.variables["tool_call_history"].append({
        "capability": "shell", "action": "execute",
        "arguments": {"command": "python -m pytest -q"},
        "result": "EXIT_CODE: 1\n2 failed\n",
    })
    improved = engine._progress_signature("failure-progress", step)
    assert improved[3] == 2
    assert engine._quality_improved("failure-progress", failed, improved) == (
        True, "fewer_machine_failures",
    )


def test_progress_rewards_new_document_coverage_but_not_repeated_reads(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("source-progress")
    step = {"required_artifacts": []}
    before = engine._progress_signature("source-progress", step)
    read = json.dumps({
        "artifact_id": "doc-1", "blocks": [{"order": 0}, {"order": 1}],
    })
    engine._record_tool_result(
        "source-progress", "documents", "read", {}, read
    )
    after = engine._progress_signature("source-progress", step)
    assert engine._quality_improved("source-progress", before, after) == (
        True, "new_source_coverage",
    )

    engine._record_tool_result(
        "source-progress", "documents", "read", {}, read
    )
    repeated = engine._progress_signature("source-progress", step)
    assert repeated == after
    execution.variables["visualized_artifact_ids"] = ["image-1"]
    visual = engine._progress_signature("source-progress", step)
    assert engine._quality_improved("source-progress", repeated, visual) == (
        True, "new_source_coverage",
    )


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

    shell = ExecutionEngine._normalize_tool_arguments(
        "shell", "execute",
        {"cmd": "python -m pytest -q", "path": "C:/ignored", "cwd": "C:/ignored"},
    )
    assert shell == {"command": "python -m pytest -q"}


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
    monkeypatch.setattr(engine.scheduler, "wait", no_wait)

    response = await engine._completion_with_recovery("recover", messages=[])

    assert response["content"] == "recovered"
    assert no_wait.await_count == 2


@pytest.mark.asyncio
async def test_provider_authentication_error_is_actionable_and_never_retried(tmp_path):
    class UnauthorizedLLM(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def completion(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("Error code: 401 - {'error': 'Unauthorized'}")

    llm = UnauthorizedLLM()
    engine, _ = _engine(tmp_path, llm)

    with pytest.raises(ProviderConfigurationError, match="Paramètres"):
        await engine._completion_with_recovery("unauthorized", messages=[])

    assert llm.calls == 1


@pytest.mark.asyncio
async def test_unauthorized_specialist_is_not_replaced_by_an_identical_retry(tmp_path):
    class UnauthorizedLLM(MockLLMProvider):
        async def completion(self, *args, **kwargs):
            raise RuntimeError("Error code: 401 - {'error': 'Unauthorized'}")

    engine, state = _engine(tmp_path, UnauthorizedLLM())
    engine.max_step_retries = 3
    parent = state.get_execution("unauthorized-parent")
    parent.current_plan = {"steps": [{
        "id": 0,
        "role": "analyst",
        "specialist": "Local Corpus Evidence Analyst",
        "description": "Inventory every explicit attachment",
        "dependencies": [],
        "expertise": ["document evidence"],
        "required_artifacts": [],
        "acceptance_criteria": ["Record source coverage"],
        "verification_commands": [],
    }]}

    await engine.execute_task(
        "unauthorized-parent",
        "Inventory every explicit attachment without Internet evidence",
    )

    children = [
        execution for execution in state.executions.values()
        if execution.variables.get("parent_execution_id") == "unauthorized-parent"
    ]
    assert parent.status == "failed"
    assert len(children) == 1
    assert parent.current_plan["steps"][0].get("retry_count", 0) == 0
    assert "401/403" in parent.results["error"]


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
    engine.adaptive_resource_management = False
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


@pytest.mark.asyncio
async def test_failed_qa_hands_machine_evidence_and_commands_to_debugger(tmp_path):
    llm = MockLLMProvider()
    for _ in range(8):
        llm.add_response(content="QA done without running the declared command")
    llm.add_response(tool_calls=[{
        "id": "debug-full", "type": "function",
        "function": {"name": "shell__execute", "arguments": {"command": "python -m pytest -q"}},
    }])
    llm.add_response(tool_calls=[{
        "id": "debug-collect", "type": "function",
        "function": {"name": "shell__execute", "arguments": {"command": "python -m pytest --collect-only -q"}},
    }])
    llm.add_response(content=json.dumps({
        "summary": "repaired", "artifacts": [],
        "evidence": ["full and collect-only suites passed"],
        "risks": [], "next_action": "",
    }))
    engine, state = _engine(tmp_path, llm, max_iterations=2)
    engine.register_capability(
        "shell", ShellCapability(str(tmp_path), state, timeout_seconds=20)
    )
    project = tmp_path / "projects" / "proj-default"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    parent = state.get_execution("validation-handoff")
    parent.current_plan = normalize_plan({"steps": [{
        "id": -1, "role": "developer", "specialist": "Implementation Engineer",
        "description": "Implement the validated feature", "dependencies": [],
        "status": "completed", "acceptance_criteria": ["Feature is implemented"],
        "verification_commands": [],
    }, {
        "id": 0, "role": "qa", "specialist": "Independent QA",
        "description": "Run independent collection", "dependencies": [-1],
        "acceptance_criteria": ["Collection passes"],
        "verification_commands": ["python -m pytest --collect-only -q"],
    }, {
        "id": 1, "role": "debugger", "specialist": "Repair Engineer",
        "description": "Repair validation defects", "dependencies": [0],
        "acceptance_criteria": ["All tests pass"],
        "verification_commands": ["python -m pytest -q"],
    }]})
    parent.variables["delivery_contract"] = build_delivery_contract(
        parent.current_plan, "Validate then repair"
    )

    await engine.execute_task("validation-handoff", "Validate then repair")

    assert parent.status == "completed", json.dumps({
        "error": parent.results.get("error"),
        "call_count": llm.call_count,
        "steps": parent.current_plan.get("steps"),
        "children": {
            key: {"status": value.status, "error": value.results.get("error")}
            for key, value in state.executions.items()
            if value.variables.get("parent_execution_id") == "validation-handoff"
        },
    }, default=str, indent=2)
    assert parent.current_plan["steps"][1]["validation_passed"] is False
    assert parent.current_plan["steps"][2]["verification_commands"] == [
        "python -m pytest -q", "python -m pytest --collect-only -q",
    ]
    children = [item for item in state.executions.values()
                if item.variables.get("parent_execution_id") == "validation-handoff"]
    assert sorted(item.status for item in children) == ["completed", "failed"]


def test_verification_gate_requires_the_exact_declared_successful_command(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("verify")
    execution.variables["tool_call_history"] = [{
        "capability": "shell", "action": "execute", "arguments": {"command": "dir"},
        "result": "EXIT_CODE: 0\n",
    }]
    step = {"description": "Verify", "verification_commands": ["python -m pytest -q"]}
    assert "python -m pytest -q" in " ".join(engine._step_completion_issues("verify", step, "done"))


def test_verification_gate_requires_exact_commands_in_inherited_requirements(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("inherited-verify")
    delimiter = chr(96)
    execution.variables["inherited_requirements"] = [{
        "id": "REQ-TEST",
        "statement": (
            "Run exact " + delimiter + "python -m pytest --collect-only -q"
            + delimiter + " and " + delimiter + "python -m pytest -q" + delimiter + "."
        ),
        "mandatory": True,
    }]
    execution.variables["tool_call_history"] = [{
        "capability": "shell",
        "action": "execute",
        "arguments": {"command": "python -m pytest --collect-only -q"},
        "result": "EXIT_CODE: 0\n",
    }]
    step = {
        "role": "developer",
        "description": "Repair the implementation",
        "acceptance_criteria": ["Complete validation is green"],
        "verification_commands": [],
    }
    response = '{"summary":"checked","artifacts":[],"evidence":[],"risks":[],"next_action":""}'

    issues = " ".join(
        engine._step_completion_issues("inherited-verify", step, response)
    )

    assert "python -m pytest -q" in issues
    assert "collect-only" not in issues


def test_inherited_software_validation_commands_do_not_block_architecture(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("architect-verify")
    delimiter = chr(96)
    execution.variables["inherited_requirements"] = [{
        "id": "REQ-TEST",
        "statement": (
            "Final software must pass " + delimiter + "python -m pytest -q" + delimiter + "."
        ),
        "mandatory": True,
    }]
    step = {
        "role": "architect",
        "description": "Design the integration",
        "acceptance_criteria": ["Architecture covers requirements"],
        "verification_commands": [],
    }
    response = '{"summary":"designed","artifacts":[],"evidence":[],"risks":[],"next_action":""}'

    issues = engine._step_completion_issues("architect-verify", step, response)

    assert not any("pytest" in issue for issue in issues)


def test_current_task_validation_commands_do_not_block_architecture(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("current-architect-verify")
    delimiter = chr(96)
    execution.current_plan = {
        "requirements": [{
            "id": "REQ-TEST",
            "statement": (
                "Final software must pass " + delimiter
                + "python -m pytest -q tests/test_api.py" + delimiter + "."
            ),
            "mandatory": True,
        }],
        "steps": [],
    }
    step = {
        "role": "architect",
        "description": "Analyze requirements before implementation",
        "acceptance_criteria": ["Requirements are testable"],
        "verification_commands": [],
    }
    response = '{"summary":"designed","artifacts":[],"evidence":[],"risks":[],"next_action":""}'

    issues = engine._step_completion_issues(
        "current-architect-verify", step, response
    )

    assert not any("pytest" in issue for issue in issues)


def test_custom_delegated_role_requires_own_validation_and_durable_edit(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("custom-fixer")
    execution.variables["parent_execution_id"] = "root"
    delimiter = chr(96)
    execution.current_plan = {
        "requirements": [
            {"id": "REQ-EDIT", "statement": "Edit ONLY src/fix.py", "mandatory": True},
            {
                "id": "REQ-TEST",
                "statement": (
                    "Then run exactly " + delimiter
                    + "python -m pytest -q tests/test_fix.py" + delimiter
                ),
                "mandatory": True,
            },
        ],
        "steps": [],
    }
    step = {"description": "Apply the smallest concrete correction"}

    initial = " ".join(engine._step_completion_issues("custom-fixer", step, "done"))
    assert "durable filesystem mutation" in initial
    assert "python -m pytest -q tests/test_fix.py" in initial

    engine._record_tool_result(
        "custom-fixer", "filesystem", "write",
        {"path": "src/fix.py", "content": "fixed = True\n"},
        "Wrote 13 bytes to src/fix.py",
    )
    after_write = " ".join(
        engine._step_completion_issues("custom-fixer", step, "done")
    )
    assert "durable filesystem mutation" not in after_write
    assert "python -m pytest -q tests/test_fix.py" in after_write

    engine._record_tool_result(
        "custom-fixer", "shell", "execute",
        {"command": "python -m pytest -q tests/test_fix.py"},
        "EXIT_CODE: 0\n",
    )
    assert engine._step_completion_issues("custom-fixer", step, "done") == []


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


@pytest.mark.asyncio
async def test_stall_rescue_does_not_invent_grounded_document_from_attachments(tmp_path):
    llm = MockLLMProvider()
    engine, state = _engine(tmp_path, llm)
    execution = state.get_execution("grounded-rescue")
    execution.variables.update({
        "project_id": "proj-grounded",
        "parent_task": "Write a report from local sources",
        "attachment_ids": ["attached-source"],
    })
    step = {
        "role": "architect",
        "specialist": "Evidence Analyst",
        "description": "Create a source-grounded inventory",
        "required_artifacts": ["analysis/corpus-inventory.md"],
        "acceptance_criteria": ["Every statement is sourced"],
        "verification_commands": [],
    }

    rescued = await engine._rescue_missing_artifacts(
        "grounded-rescue", step, []
    )

    assert rescued == []
    assert llm.call_count == 0


def test_step_gate_rejects_invalid_intermediate_document_before_handoff(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    execution = state.get_execution("document-gate")
    execution.variables["project_id"] = "proj-document-gate"
    execution.current_plan = {
        "artifact_validations": [{
            "path": "analysis/corpus-inventory.md",
            "validator": "document",
            "required": True,
            "constraints": {"forbid_placeholders": True},
        }],
    }
    target = tmp_path / "projects" / "proj-document-gate" / "analysis" / "corpus-inventory.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Inventaire\n\nSource : ...\n\n</think>\n", encoding="utf-8")
    step = {
        "role": "architect",
        "specialist": "Evidence Analyst",
        "description": "Inventory sources",
        "required_artifacts": ["analysis/corpus-inventory.md"],
        "acceptance_criteria": ["No placeholders"],
        "verification_commands": [],
    }
    response = json.dumps({
        "summary": "done", "artifacts": ["analysis/corpus-inventory.md"],
        "evidence": [], "risks": [], "next_action": "handoff",
    })

    issues = engine._step_completion_issues("document-gate", step, response)

    assert any("placeholder" in issue for issue in issues)
    assert not engine._can_engine_finalize("document-gate", step)


def test_exhaustive_inventory_gate_requires_every_normalized_block_read(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    store = ArtifactStore(str(tmp_path / "artifacts"))
    uploaded = store.save_base64(
        "vision.txt",
        base64.b64encode(
            b"# Slide 1\n\nfirst\n\n# Slide 2\n\nsecond\n\n# Slide 3\n\nthird"
        ).decode("ascii"),
        "text/plain",
    )
    engine.artifact_store = store
    execution = state.get_execution("coverage-gate")
    execution.variables["attachment_ids"] = [uploaded["id"]]
    document = store.document(uploaded["id"])
    step = {
        "description": "Inventory every explicit attachment and record complete coverage.",
        "acceptance_criteria": ["All normalized blocks were read."],
    }

    partial = {
        "artifact_id": uploaded["id"],
        "total_blocks": len(document.blocks),
        "blocks": [block.to_dict() for block in document.blocks[:2]],
    }
    engine._record_tool_result(
        "coverage-gate", "documents", "read", {}, json.dumps(partial)
    )

    issues = engine._document_coverage_issues("coverage-gate", step)
    assert issues
    assert "vision.txt" in issues[0]

    remainder = {
        "artifact_id": uploaded["id"],
        "total_blocks": len(document.blocks),
        "blocks": [block.to_dict() for block in document.blocks[2:]],
    }
    engine._record_tool_result(
        "coverage-gate", "documents", "read", {}, json.dumps(remainder)
    )

    assert engine._document_coverage_issues("coverage-gate", step) == []


def test_resumed_specialist_reuses_only_same_assignment_document_reads(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    store = ArtifactStore(str(tmp_path / "resume-artifacts"))
    uploaded = store.save_base64(
        "evidence.txt",
        base64.b64encode(b"first\n\nsecond\n\nthird").decode("ascii"),
        "text/plain",
    )
    engine.artifact_store = store
    document = store.document(uploaded["id"])
    step = {
        "description": "Inventory every explicit attachment and record complete coverage.",
        "acceptance_criteria": ["All normalized blocks were read."],
    }
    previous = state.get_execution("previous-specialist")
    previous.variables.update({
        "parent_execution_id": "parent",
        "plan_step_id": 0,
        "project_id": "project-a",
        "attachment_ids": [uploaded["id"]],
    })
    payload = {
        "artifact_id": uploaded["id"],
        "total_blocks": len(document.blocks),
        "blocks": [block.to_dict() for block in document.blocks],
    }
    engine._record_tool_result(
        "previous-specialist", "documents", "read", {}, json.dumps(payload)
    )

    resumed = state.get_execution("resumed-specialist")
    resumed.variables.update({
        "parent_execution_id": "parent",
        "plan_step_id": 0,
        "project_id": "project-a",
        "attachment_ids": [uploaded["id"]],
    })
    unrelated = state.get_execution("unrelated-specialist")
    unrelated.variables.update({
        "parent_execution_id": "parent",
        "plan_step_id": 1,
        "project_id": "project-a",
        "attachment_ids": [uploaded["id"]],
    })

    assert engine._document_coverage_issues("resumed-specialist", step) == []
    assert engine._document_coverage_issues("unrelated-specialist", step)
    assert engine._inherits_complete_document_coverage("resumed-specialist", step)
    assert not engine._inherits_complete_document_coverage("unrelated-specialist", step)

    schemas = [
        {"function": {"name": "documents__inventory"}},
        {"function": {"name": "documents__read"}},
        {"function": {"name": "documents__read_chunk"}},
        {"function": {"name": "documents__search"}},
        {"function": {"name": "filesystem__read"}},
    ]
    filtered = engine._schemas_for_inherited_document_coverage(schemas)
    assert [item["function"]["name"] for item in filtered] == [
        "documents__search", "filesystem__read",
    ]


def test_exhaustive_inventory_gate_requires_each_attached_image_presented(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    engine.llm_provider.supports_vision = True
    store = ArtifactStore(str(tmp_path / "image-artifacts"))
    image = store.save_bytes(
        "evidence.png", b"\x89PNG\r\n\x1a\nevidence", "image/png"
    )
    engine.artifact_store = store
    execution = state.get_execution("image-coverage-gate")
    execution.variables["attachment_ids"] = [image["id"]]
    step = {
        "description": "Inventory every explicit attachment and record complete coverage.",
        "acceptance_criteria": ["All images were analyzed."],
    }

    result = json.dumps({
        "artifact_id": image["id"],
        "status": "scheduled_for_multimodal_context",
    })
    engine._record_tool_result(
        "image-coverage-gate", "documents", "read_image", {}, result
    )

    assert execution.variables["pending_visual_artifact_ids"] == [image["id"]]
    assert "evidence.png" in engine._document_coverage_issues(
        "image-coverage-gate", step
    )[0]
    execution.variables["visualized_artifact_ids"] = [image["id"]]
    assert engine._document_coverage_issues("image-coverage-gate", step) == []


def test_exhaustive_inventory_gate_defers_images_to_declared_vision_gap(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    engine.llm_provider.supports_vision = False
    store = ArtifactStore(str(tmp_path / "no-vision-artifacts"))
    image = store.save_bytes(
        "evidence.png", b"\x89PNG\r\n\x1a\nevidence", "image/png"
    )
    engine.artifact_store = store
    execution = state.get_execution("no-vision-coverage-gate")
    execution.variables["attachment_ids"] = [image["id"]]
    step = {
        "description": "Inventory every explicit attachment and record complete coverage.",
        "acceptance_criteria": ["All supported contents were analyzed."],
    }

    assert engine._capability_gaps(execution)[0]["capability"] == "vision"
    assert engine._document_coverage_issues(
        "no-vision-coverage-gate", step,
    ) == []


@pytest.mark.asyncio
async def test_document_coverage_gate_forces_real_read_after_prose_promise(tmp_path):
    class CapturingProvider(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def completion(self, messages, **kwargs):
            self.requests.append({
                "messages": [dict(message) for message in messages],
                "tools": list(kwargs.get("tools") or []),
                "tool_choice": kwargs.get("tool_choice"),
            })
            return await super().completion(messages, **kwargs)

    delivery = json.dumps({
        "summary": "complete", "artifacts": [], "evidence": ["local corpus"],
        "risks": [], "next_action": "handoff",
    })
    llm = CapturingProvider()
    llm.add_response(content=(
        "I will now read evidence.txt from start block 0. " + delivery
    ))
    llm.add_response(tool_calls=[{
        "id": "read-1", "type": "function",
        "function": {
            "name": "documents__read",
            "arguments": {
                "artifact_id": "evidence.txt", "start_block": 0,
                "block_count": 200,
            },
        },
    }])
    llm.add_response(content=delivery)
    engine, state = _engine(tmp_path, llm, max_iterations=8)
    store = ArtifactStore(str(tmp_path / "forced-read-artifacts"))
    uploaded = store.save_base64(
        "evidence.txt", base64.b64encode(b"local evidence").decode("ascii"),
        "text/plain",
    )
    engine.artifact_store = store
    engine.register_capability("documents", DocumentCapability(store))
    execution = state.get_execution("forced-document-read")
    execution.variables.update({
        "parent_execution_id": "parent", "role_key": "architect",
        "role_name": "Local Corpus Evidence Analyst",
        "specialist": "Local Corpus Evidence Analyst",
        "attachment_ids": [uploaded["id"]],
    })
    execution.current_plan = {"steps": [{
        "id": 0, "role": "architect",
        "specialist": "Local Corpus Evidence Analyst",
        "description": "Inventory every explicit attachment and record complete coverage.",
        "operation": "inventory", "dependencies": [], "expertise": [],
        "required_artifacts": [],
        "acceptance_criteria": ["All normalized blocks were read."],
        "verification_commands": [],
    }]}

    await engine.execute_task(
        "forced-document-read", "Inventory every explicit attachment",
    )

    assert execution.status == "completed", execution.results
    assert llm.requests[1]["tool_choice"] == "required"
    assert [
        item["function"]["name"] for item in llm.requests[1]["tools"]
    ] == ["documents__read"]
    history = execution.variables["tool_call_history"]
    assert [(item["capability"], item["action"]) for item in history] == [
        ("documents", "read")
    ]


def test_arithmetic_quality_failure_forces_targeted_repair_for_any_specialist(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    state.get_execution("arithmetic-repair")
    project = tmp_path / "projects" / "proj-default" / "analysis"
    project.mkdir(parents=True)
    (project / "inventory.md").write_text(
        "# Coverage\n\n- **Blocks read**: 4 + 10 + 97 = **101 blocks**.\n",
        encoding="utf-8",
    )
    step = {"required_artifacts": ["analysis/inventory.md"]}
    issues = [
        "analysis/inventory.md: arithmetic sum mismatch(es): 4 + 10 + 97 "
        "equals 111, not 101; paragraph prefix: - **Blocks read**: 4 + 10 + 97"
    ]

    assert engine._writer_incremental_repair_tool(
        "arithmetic-repair", "architect", step, issues,
    ) == "filesystem__replace_paragraph"
    assert "calculated value reported by the gate" in engine._writer_incremental_repair_nudge(
        "arithmetic-repair", "architect", step, issues,
    )


def test_missing_document_coverage_precedes_artifact_mutation(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    state.get_execution("repair-order")
    step = {"required_artifacts": ["analysis/inventory.md"]}
    issues = [
        "read every normalized block of requirements.txt; missing 1-based block(s): 3",
        "analysis/inventory.md: invalid local reference(s): undeclared local source 'legacy.txt'",
    ]

    tool, nudge = engine._quality_repair_directive(
        "repair-order", "architect", step, issues,
    )

    assert tool == "documents__read"
    assert "start_block=2" in nudge
    assert "filesystem" not in nudge


@pytest.mark.asyncio
async def test_missing_specialist_artifact_forces_bounded_write_after_malformed_json(tmp_path):
    class CapturingProvider(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def completion(self, messages, **kwargs):
            self.requests.append({
                "messages": [dict(message) for message in messages],
                "tools": list(kwargs.get("tools") or []),
                "tool_choice": kwargs.get("tool_choice"),
            })
            return await super().completion(messages, **kwargs)

    delivery = json.dumps({
        "summary": "created", "artifacts": ["analysis/inventory.md"],
        "evidence": ["local sources"], "risks": [], "next_action": "handoff",
    })
    llm = CapturingProvider()
    llm.add_response(content=(
        '```json\n{"tool_call":{"name":"filesystem__write","arguments":'
        '{"path":"analysis/inventory.md","content":"oversized and truncated\n```'
    ))
    llm.add_response(tool_calls=[{
        "id": "write-1", "type": "function",
        "function": {
            "name": "filesystem__write",
            "arguments": {
                "path": "analysis/inventory.md",
                "content": "# Inventory\n\nA bounded, durable local inventory section.",
            },
        },
    }])
    llm.add_response(content=delivery)
    engine, state = _engine(tmp_path, llm, max_iterations=8)
    execution = state.get_execution("forced-specialist-write")
    execution.variables.update({
        "parent_execution_id": "parent", "role_key": "architect",
        "role_name": "Evidence Analyst", "specialist": "Evidence Analyst",
    })
    execution.current_plan = {"steps": [{
        "id": 0, "role": "architect", "specialist": "Evidence Analyst",
        "description": "Create the required local evidence inventory.",
        "dependencies": [], "expertise": [],
        "required_artifacts": ["analysis/inventory.md"],
        "acceptance_criteria": ["Inventory exists"],
        "verification_commands": [],
    }]}

    await engine.execute_task(
        "forced-specialist-write", "Create the local evidence inventory",
    )

    assert execution.status == "completed", execution.results
    assert llm.requests[1]["tool_choice"] == "required"
    assert [
        item["function"]["name"] for item in llm.requests[1]["tools"]
    ] == ["filesystem__write"]
    repair_prompt = next(
        item["content"] for item in llm.requests[1]["messages"]
        if item.get("role") == "system"
        and "exactly one valid filesystem__write" in str(item.get("content"))
    )
    assert "Later turns can append" in repair_prompt


def test_rescue_strips_prefixed_fence_and_rejects_mock_random_tests():
    raw = "tests/test_real.py\n```python\nfrom avatar3d.body import Body\n\ndef test_body():\n    assert Body\n```"
    cleaned = ExecutionEngine._strip_code_fence(raw, "tests/test_real.py")
    assert "```" not in cleaned
    assert cleaned.startswith("from avatar3d.body")
    assert ExecutionEngine._rescue_content_issues("tests/test_real.py", cleaned) == []

    bad = "import numpy as np\nclass MockMesh: pass\ndef test_fake(): np.random.rand(2)\n"
    issues = ExecutionEngine._rescue_content_issues("tests/test_fake.py", bad)
    assert any("mocks" in issue for issue in issues)

    empty_test = ExecutionEngine._rescue_content_issues("tests/test_empty.py", "VALUE = 1\n")
    assert any("pytest test function" in issue for issue in empty_test)

    wrong_identity = "from src.avatar3d.body import Body\ndef test_body(): assert Body\n"
    identity_issues = ExecutionEngine._rescue_content_issues("tests/test_identity.py", wrong_identity)
    assert any("canonical package identity" in issue for issue in identity_issues)


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

    assert any("canonical installed package identity" in issue for issue in issues)
    assert any("pythonpath" in issue for issue in issues)


def test_integration_gate_rejects_module_package_collision_without_src_layout(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    state.get_execution("collision")
    package = tmp_path / "projects" / "proj-default" / "agentbench"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "fixtures.py").write_text("VALUE = 1\n", encoding="utf-8")
    fixture_package = package / "fixtures"
    fixture_package.mkdir()
    (fixture_package / "__init__.py").write_text("", encoding="utf-8")
    tests = tmp_path / "projects" / "proj-default" / "tests"
    tests.mkdir()
    (tests / "test_acceptance.py").write_text(
        "def test_acceptance(): assert True\n", encoding="utf-8"
    )
    (tmp_path / "projects" / "proj-default" / "pytest.ini").write_text(
        "[pytest]\ntestpaths = agentbench/tests\n", encoding="utf-8"
    )

    issues = engine._integration_contract_issues("collision")

    assert any(
        "module/package name collisions" in issue
        and "agentbench/fixtures" in issue
        for issue in issues
    )
    assert any(
        "no discovered validation suite is hidden" in issue
        and "tests" in issue
        for issue in issues
    )


def test_quality_gate_fingerprint_ignores_volatile_test_duration():
    first = tool_call_fingerprint("devteam", "approve_quality_gate", {
        "test_output": "67 passed in 2.41 seconds",
        "status": "passed",
    })
    second = tool_call_fingerprint("devteam", "approve_quality_gate", {
        "test_output": "67 passed in 9.88 seconds",
        "status": "passed",
    })

    assert first == second


def test_tests_package_is_not_treated_as_a_fake_third_party_dependency(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    state.get_execution("package-check")
    tests_package = tmp_path / "projects" / "proj-default" / "tests"
    tests_package.mkdir(parents=True)
    (tests_package / "__init__.py").write_text("", encoding="utf-8")

    assert "tests" not in engine._fake_dependency_packages("package-check")


def test_shell_mutation_detection_covers_python_powershell_and_redirection():
    detected = ExecutionEngine._shell_mutation_paths(
        "python -c \"from pathlib import Path; Path('src/app.py').write_text('x')\""
    )
    assert detected == ["src/app.py"]
    assert ExecutionEngine._shell_mutation_paths(
        "Set-Content -LiteralPath tests/test_app.py -Value ok"
    ) == ["tests/test_app.py"]
    assert ExecutionEngine._shell_mutation_paths("echo ok > docs/result.txt") == ["docs/result.txt"]


def test_shell_mutation_detection_ignores_comparisons_inside_python_code():
    command = (
        'python -c "import subprocess; result = subprocess.run([\'python\'], '
        'capture_output=True, text=True); '
        'print(result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout)"'
    )

    assert ExecutionEngine._shell_mutation_paths(command) == []
    assert ExecutionEngine._shell_mutation_paths(
        'python -c "print(\'ok\')" > pytest_output.txt 2>&1'
    ) == ["pytest_output.txt"]


@pytest.mark.asyncio
async def test_restart_recovery_schedules_only_top_level_interrupted_work(tmp_path):
    engine, state = _engine(tmp_path, MockLLMProvider())
    root = state.get_execution("interrupted-root")
    root.status = "running"
    root.variables["task"] = "Resume me"
    child = state.get_execution("interrupted-child")
    child.status = "running"
    child.variables.update({"task": "Do not schedule directly", "parent_execution_id": "interrupted-root"})
    engine.execute_task = AsyncMock()

    engine.resume_interrupted_executions()
    await asyncio.sleep(0)

    engine.execute_task.assert_awaited_once_with("interrupted-root", "Resume me")
    assert child.status == "pending"
    assert child.variables["interrupted_resume_count"] == 1


@pytest.mark.asyncio
async def test_truncated_text_tool_call_gets_recovery_feedback_instead_of_completion_gate(tmp_path):
    llm = MockLLMProvider()
    llm.add_response(content=(
        '```json\n{"tool_call":{"name":"filesystem__write","arguments":'
        '{"path":"too_large.py","content":"unterminated'
    ))
    llm.add_response(content=json.dumps({
        "summary": "recovered", "artifacts": [], "evidence": ["compact response"],
        "risks": [], "next_action": "",
    }))
    engine, state = _engine(tmp_path, llm, max_iterations=4)
    execution = state.get_execution("truncated-call")
    execution.variables.update({
        "parent_execution_id": "parent", "role_key": "writer",
        "role_name": "Writer", "specialist": "Writer",
    })
    execution.current_plan = {"steps": [{
        "id": 0, "role": "writer", "specialist": "Writer",
        "description": "Document a compact module", "dependencies": [],
        "expertise": ["documentation"], "required_artifacts": [],
        "acceptance_criteria": ["Recovery is explicit"], "verification_commands": [],
    }]}

    await engine.execute_task("truncated-call", "Document a compact module")

    assert execution.status == "completed"
    feedback = [message.get("content", "") for message in state.get_conversation("truncated-call").messages]
    assert any("malformed or truncated" in message for message in feedback)
    assert not any("return the required structured JSON" in message for message in feedback)


@pytest.mark.asyncio
async def test_truncated_tool_code_marker_gets_protocol_recovery_feedback(tmp_path):
    llm = MockLLMProvider()
    llm.add_response(content="I will write the file now.\n<tool_code>")
    llm.add_response(content=json.dumps({
        "summary": "recovered", "artifacts": [], "evidence": [],
        "risks": [], "next_action": "",
    }))
    engine, state = _engine(tmp_path, llm, max_iterations=4)
    execution = state.get_execution("tool-code-marker")
    execution.variables.update({
        "parent_execution_id": "parent", "role_key": "writer",
        "role_name": "Writer", "specialist": "Writer",
    })
    execution.current_plan = {"steps": [{
        "id": 0, "role": "writer", "specialist": "Writer",
        "description": "Return a compact delivery", "dependencies": [],
        "expertise": [], "required_artifacts": [],
        "acceptance_criteria": [], "verification_commands": [],
    }]}

    await engine.execute_task("tool-code-marker", "Return a compact delivery")

    assert execution.status == "completed"
    feedback = [message.get("content", "") for message in state.get_conversation("tool-code-marker").messages]
    assert any("malformed or truncated" in message for message in feedback)


@pytest.mark.asyncio
async def test_specialist_prompt_delegates_artifact_validation_to_runtime(tmp_path):
    class CapturingProvider(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def completion(self, messages, **kwargs):
            self.requests.append(messages)
            return await super().completion(messages, **kwargs)

    llm = CapturingProvider()
    llm.add_response(tool_calls=[{
        "id": "write", "type": "function",
        "function": {
            "name": "filesystem__write",
            "arguments": {"path": "report.md", "content": "# Verified report\n\nConcrete evidence.\n"},
        },
    }])
    llm.add_response(content=json.dumps({
        "summary": "written", "artifacts": ["report.md"],
        "evidence": ["read-back contract"], "risks": [], "next_action": "",
    }))
    engine, state = _engine(tmp_path, llm)
    execution = state.get_execution("runtime-validation-prompt")
    execution.variables.update({
        "parent_execution_id": "parent", "role_key": "writer",
        "role_name": "Writer", "specialist": "Writer",
    })
    execution.current_plan = {
        "steps": [{
            "id": 0, "role": "writer", "specialist": "Writer",
            "description": "Write a verified report", "dependencies": [],
            "expertise": ["documentation"], "required_artifacts": ["report.md"],
            "acceptance_criteria": ["Report exists"], "verification_commands": [],
        }],
        "artifact_validations": [{
            "path": "report.md", "validator": "document", "required": True,
            "constraints": {"minimums": {"words": 2}},
        }],
    }

    await engine.execute_task("runtime-validation-prompt", "Write a verified report")

    system_prompt = next(
        message["content"] for message in llm.requests[0] if message.get("role") == "system"
    )
    assert "enforced automatically by the execution engine" in system_prompt
    assert "Do not invoke repository-only validator scripts" in system_prompt
    assert execution.status == "completed"


@pytest.mark.parametrize("tool_name", ["shell__execute", "documents__read"])
@pytest.mark.asyncio
async def test_repeated_malformed_text_tool_calls_trip_safe_retry_circuit(
    tmp_path, tool_name,
):
    llm = MockLLMProvider()
    malformed = (
        f'```json\n{{"tool_call":{{"name":"{tool_name}","arguments":'
        '{"command": python -m pytest -q\n```'
    )
    for _ in range(5):
        llm.add_response(content=malformed)
    engine, state = _engine(tmp_path, llm, max_iterations=30)
    execution = state.get_execution("malformed-circuit")
    execution.variables.update({
        "parent_execution_id": "parent", "role_key": "developer",
        "role_name": "Developer", "specialist": "Developer",
    })
    execution.current_plan = {"steps": [{
        "id": 0, "role": "developer", "specialist": "Developer",
        "description": "Implement and validate a module", "dependencies": [],
        "expertise": [], "required_artifacts": [],
        "acceptance_criteria": ["Implementation is validated"],
        "verification_commands": [],
    }]}

    await engine.execute_task("malformed-circuit", "Implement and validate")

    assert execution.status == "failed"
    assert "repeatedly emitted malformed or truncated" in execution.results["error"]
    assert execution.variables.get("tool_call_history", []) == []


@pytest.mark.asyncio
async def test_malformed_writer_call_preserves_targeted_repair_constraint(tmp_path):
    class CapturingProvider(MockLLMProvider):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def completion(self, messages, **kwargs):
            self.requests.append({
                "messages": [dict(message) for message in messages],
                "tools": kwargs.get("tools"),
                "tool_choice": kwargs.get("tool_choice"),
            })
            return await super().completion(messages, **kwargs)

    repeated = (
        "This duplicated professional decision paragraph contains enough words "
        "for a deterministic targeted repair without rebuilding the document."
    )
    llm = CapturingProvider()
    delivery = json.dumps({
        "summary": "repaired", "artifacts": ["report.md"],
        "evidence": ["deterministic gate"], "risks": [], "next_action": "",
    })
    llm.add_response(content=delivery)
    llm.add_response(content=(
        '```json\n{"tool_call":{"name":"filesystem__replace_paragraph",'
        '"arguments":{"path":"report.md","paragraph_prefix":"truncated\n```'
    ))
    llm.add_response(tool_calls=[{
        "id": "repair", "type": "function",
        "function": {
            "name": "filesystem__replace_paragraph",
            "arguments": {
                "path": "report.md",
                "paragraph_prefix": repeated,
                "content": "",
                "occurrence": 2,
            },
        },
    }])
    llm.add_response(content=delivery)
    engine, state = _engine(tmp_path, llm, max_iterations=10)
    execution = state.get_execution("malformed-writer-repair")
    execution.variables.update({
        "role_key": "writer", "role_name": "Writer", "specialist": "Writer",
    })
    execution.current_plan = {
        "steps": [{
            "id": 0, "role": "writer", "specialist": "Writer",
            "description": "Repair a professional report", "dependencies": [],
            "expertise": [], "required_artifacts": ["report.md"],
            "acceptance_criteria": ["No duplicate paragraphs"],
            "verification_commands": [], "owned_paths": ["report.md"],
        }],
        "artifact_validations": [{
            "path": "report.md", "validator": "document", "required": True,
            "constraints": {
                "max_duplicate_paragraphs": 0,
                "duplicate_min_words": 8,
            },
        }],
    }
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "report.md"
    target.write_text(
        f"# Review\n\n{repeated}\n\n{repeated}\n", encoding="utf-8",
    )

    await engine.execute_task("malformed-writer-repair", "Repair the report")

    assert execution.status == "completed"
    assert target.read_text(encoding="utf-8").count(repeated) == 1
    constrained = llm.requests[2]
    tool_names = [
        tool["function"]["name"] for tool in (constrained["tools"] or [])
    ]
    assert tool_names == ["filesystem__replace_paragraph"]
    assert constrained["tool_choice"] == "required"
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in constrained["messages"]
        if message.get("role") == "system"
    )
    assert "Never overwrite an existing long document" in system_text
    assert "exactly one valid filesystem__replace_paragraph" in system_text
