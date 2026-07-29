import asyncio
import base64
import json
import os
import struct

import pytest

from gptmoss.capabilities.shell import ShellCapability
from gptmoss.core.adaptive import AdaptiveRuntimePolicy, tool_call_fingerprint
from gptmoss.core.artifact_validation import validate_artifact
from gptmoss.core.artifacts import ArtifactStore
from gptmoss.core.context import ContextEngine
from gptmoss.core.delivery import build_delivery_contract, evaluate_delivery
from gptmoss.core.event_bus import EventBus
from gptmoss.core.execution import ExecutionEngine, normalize_plan
from gptmoss.core.state import StateEngine
from gptmoss.memory.ram import RAMMemoryProvider
from gptmoss.planners.simple import SimplePlanner
from gptmoss.policies.simple import SimplePolicyProvider
from gptmoss.providers.qwen import QwenProvider
from gptmoss.core.kernel import RuntimeKernel
from tests.mock_llm import MockLLMProvider


def _engine(tmp_path, *, artifacts=None):
    events = EventBus()
    state = StateEngine()
    llm = MockLLMProvider()
    context = ContextEngine(state, RAMMemoryProvider())
    engine = ExecutionEngine(
        events,
        state,
        context,
        llm,
        SimplePlanner(llm),
        SimplePolicyProvider(),
        artifact_store=artifacts,
        autonomous_specialization=False,
    )
    return engine, state


def test_adaptive_budgets_scale_with_the_real_contract():
    policy = AdaptiveRuntimePolicy(
        baseline_stagnation_iterations=10,
        baseline_retries=1,
        adaptive=True,
    )
    simple = {"description": "Write one file"}
    complex_step = {
        "description": "Integrate a large service",
        "required_artifacts": [f"src/file_{index}.py" for index in range(12)],
        "acceptance_criteria": [f"criterion {index}" for index in range(12)],
        "verification_commands": ["python -m pytest", "python -m sample.cli"],
        "expertise": ["API", "storage", "security"],
    }

    assert policy.stagnation_budget("small", complex_step) > policy.stagnation_budget("small", simple)
    assert policy.retry_budget("small", complex_step) > policy.retry_budget("small", simple)


def test_provider_context_compaction_preserves_instructions_and_recent_order():
    messages = [{"role": "system", "content": "authoritative instructions"}]
    messages.extend(
        {"role": "user", "content": f"old-{index}-" + "x" * 500}
        for index in range(12)
    )
    messages.extend([
        {"role": "assistant", "content": None, "tool_calls": [{"id": "latest"}]},
        {"role": "tool", "tool_call_id": "latest", "content": "latest result"},
        {"role": "user", "content": "latest request"},
    ])

    compacted = QwenProvider._compact_messages(messages, 3_000)

    assert compacted[0]["content"] == "authoritative instructions"
    assert compacted[-3:] == messages[-3:]
    assert any("were compacted" in str(item.get("content")) for item in compacted)


def test_provider_compacts_a_single_oversized_user_message():
    messages = [
        {"role": "system", "content": "authoritative"},
        {"role": "user", "content": "A" * 20_000},
    ]

    compacted = QwenProvider._compact_messages(messages, 2_000)

    assert compacted[0]["content"] == "authoritative"
    assert len(compacted[1]["content"]) < 20_000
    assert "context boundary" in compacted[1]["content"]
    assert QwenProvider._message_chars(compacted) <= 2_000


def test_vision_capability_can_be_explicit_when_model_names_are_ambiguous():
    provider = object.__new__(QwenProvider)
    provider.default_model = "custom-model-without-modality-metadata"
    provider.vision_mode = "auto"
    provider.set_vision_mode("enabled")

    assert provider.supports_vision is True
    provider.set_vision_mode("disabled")
    assert provider.supports_vision is False


def test_skills_add_procedures_without_removing_capabilities_by_default(tmp_path):
    engine, _ = _engine(tmp_path)
    skill = type("Skill", (), {"allowed_capabilities": {"filesystem"}})()

    assert engine._allowed_capabilities([skill]) is None
    engine.strict_skill_capabilities = True
    assert engine._allowed_capabilities([skill]) == {"filesystem"}


def test_missing_vision_is_a_declared_capability_gap(tmp_path):
    store = ArtifactStore(str(tmp_path))
    image = store.save_base64(
        "reference.png",
        base64.b64encode(b"\x89PNG\r\n\x1a\npayload").decode("ascii"),
        "image/png",
    )
    engine, state = _engine(tmp_path, artifacts=store)
    execution = state.get_execution("vision-gap")
    execution.variables["attachment_ids"] = [image["id"]]

    gaps = engine._capability_gaps(execution)

    assert gaps[0]["capability"] == "vision"
    assert gaps[0]["available"] is False
    assert "configuration" in gaps[0]["resolution"]


@pytest.mark.asyncio
async def test_parent_surfaces_and_resolves_a_child_approval(tmp_path):
    engine, state = _engine(tmp_path)
    parent = state.get_execution("parent")
    parent.status = "running"
    parent.variables["task"] = "Build a project"
    parent.current_plan = normalize_plan({
        "steps": [{
            "id": 0,
            "role": "developer",
            "specialist": "Implementation specialist",
            "description": "Implement the project",
            "dependencies": [],
            "assigned_execution_id": "child",
        }],
    })
    parent.variables["delivery_contract"] = build_delivery_contract(
        parent.current_plan, parent.variables["task"]
    )
    child = state.get_execution("child")
    child.status = "paused"
    child.variables.update({
        "parent_execution_id": "parent",
        "task": "Implement the project",
        "role_name": "Implementation specialist",
        "pending_approval": {
            "tool_call_id": "shell-1",
            "capability": "shell",
            "action": "execute",
            "arguments": {"command": "python -m pytest"},
            "fingerprint": tool_call_fingerprint(
                "shell", "execute", {"command": "python -m pytest"}
            ),
        },
    })

    await engine.execute_task("parent", parent.variables["task"])

    assert parent.status == "paused"
    assert parent.variables["pending_approval"]["child_execution_id"] == "child"

    scheduled = []

    async def record_schedule(execution_id, task):
        scheduled.append(execution_id)

    engine.execute_task = record_schedule
    await engine.resume_with_decision("parent", "reject", "Not needed")
    await asyncio.sleep(0)

    assert parent.status == "running"
    assert child.status == "running"
    assert "pending_approval" not in parent.variables
    assert child.variables["pending_approval"]["decision"] == "reject"
    assert set(scheduled) == {"parent", "child"}


@pytest.mark.asyncio
async def test_kernel_rejects_recursive_delegation_but_not_new_subtasks(tmp_path):
    engine, state = _engine(tmp_path)
    scheduled = []

    async def record_schedule(execution_id, task):
        scheduled.append((execution_id, task))

    engine.execute_task = record_schedule
    kernel = RuntimeKernel(EventBus(), state, engine)
    parent = state.get_execution("ancestor")
    parent.variables.update({
        "task": "Inspect API",
        "delegation_depth": 0,
        "delegation_lineage": ["inspect api"],
    })

    with pytest.raises(ValueError, match="cycle"):
        await kernel.submit_task(
            "  INSPECT   API ",
            {"parent_execution_id": "ancestor"},
        )

    child_id = await kernel.submit_task(
        "Inspect storage",
        {"parent_execution_id": "ancestor"},
    )
    await asyncio.sleep(0)
    assert state.get_execution(child_id).variables["delegation_depth"] == 1
    assert scheduled[0][1] == "Inspect storage"


def test_obj_validation_checks_numbers_indices_topology_and_scale(tmp_path):
    valid = tmp_path / "valid.obj"
    valid.write_text(
        "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
        "vn 0 0 1\nvt 0 0\nvt 1 0\nvt 0 1\n"
        "f 1/1/1 2/2/1 3/3/1\n",
        encoding="utf-8",
    )
    report = validate_artifact(
        valid,
        constraints={
            "max_degenerate_faces": 0,
            "extents": {"x": [0.9, 1.1], "y": [0.9, 1.1]},
        },
    )
    assert report["valid"], json.dumps(report, indent=2)
    assert report["metrics"]["triangles"] == 1

    invalid = tmp_path / "invalid.obj"
    invalid.write_text(
        "v nan 0 0\nv 0 0 0\nv 0 0 0\nf 1 2 99\n",
        encoding="utf-8",
    )
    report = validate_artifact(invalid)
    assert not report["valid"]
    assert report["metrics"]["invalid_numbers"] == 1
    assert report["metrics"]["invalid_indices"] == 1


def test_artifact_validator_fails_safely_on_malformed_constraints(tmp_path):
    path = tmp_path / "output.obj"
    path.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n", encoding="utf-8")

    report = validate_artifact(
        path,
        validator="obj",
        constraints={"minimums": {"vertices": "not-a-number"}},
    )

    assert not report["valid"]
    assert any("failed safely" in failure for failure in report["failures"])


def test_glb_validation_parses_header_chunks_and_document(tmp_path):
    positions = struct.pack("<9f", 0, 0, 0, 1, 0, 0, 0, 1, 0)
    document = json.dumps({
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(positions)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(positions)}],
        "accessors": [{
            "bufferView": 0,
            "componentType": 5126,
            "count": 3,
            "type": "VEC3",
            "min": [0, 0, 0],
            "max": [1, 1, 0],
        }],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "nodes": [{"mesh": 0}],
        "scenes": [{"nodes": [0]}],
        "scene": 0,
    }).encode("utf-8")
    document += b" " * ((4 - len(document) % 4) % 4)
    total_length = 12 + 8 + len(document) + 8 + len(positions)
    data = (
        struct.pack("<4sII", b"glTF", 2, total_length)
        + struct.pack("<II", len(document), 0x4E4F534A)
        + document
        + struct.pack("<II", len(positions), 0x004E4942)
        + positions
    )
    path = tmp_path / "model.glb"
    path.write_bytes(data)

    report = validate_artifact(path, constraints={"require_mesh": True})

    assert report["valid"], json.dumps(report, indent=2)
    assert report["metrics"]["meshes"] == 1


def test_glb_validation_rejects_non_finite_geometry_and_bad_references(tmp_path):
    positions = struct.pack("<3f", float("nan"), 0, 0)
    document = json.dumps({
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(positions)}],
        "bufferViews": [{"buffer": 0, "byteLength": len(positions)}],
        "accessors": [{
            "bufferView": 0,
            "componentType": 5126,
            "count": 1,
            "type": "VEC3",
        }],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "nodes": [{"mesh": 99}],
    }).encode("utf-8")
    document += b" " * ((4 - len(document) % 4) % 4)
    total_length = 12 + 8 + len(document) + 8 + len(positions)
    path = tmp_path / "invalid.glb"
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total_length)
        + struct.pack("<II", len(document), 0x4E4F534A)
        + document
        + struct.pack("<II", len(positions), 0x004E4942)
        + positions
    )

    report = validate_artifact(path)

    assert not report["valid"]
    assert report["metrics"]["non_finite_values"] == 1
    assert report["metrics"]["invalid_references"] >= 1


def test_delivery_assurance_rejects_a_structurally_invalid_output(tmp_path):
    plan = normalize_plan({
        "artifact_validations": [{
            "path": "output.obj",
            "validator": "obj",
            "constraints": {"max_degenerate_faces": 0},
        }],
        "steps": [{
            "id": 0,
            "role": "developer",
            "specialist": "Exporter",
            "description": "Export a valid model",
            "dependencies": [],
            "required_artifacts": ["output.obj"],
        }],
    })
    (tmp_path / "output.obj").write_text(
        "v 0 0 0\nv 0 0 0\nv 0 0 0\nf 1 2 3\n",
        encoding="utf-8",
    )
    contract = build_delivery_contract(plan, "Export a valid model")

    report = evaluate_delivery(tmp_path, contract, plan["steps"], [])

    assert not report["passed"]
    check = next(
        item for item in report["checks"]
        if item["name"] == "artifact_structure_and_constraints"
    )
    assert not check["passed"]


def test_delivery_validates_declared_targets_even_when_no_step_owns_them(tmp_path):
    plan = normalize_plan({
        "artifact_validations": [{
            "path": "operator-output.glb",
            "validator": "glb",
            "required": True,
        }],
        "steps": [{
            "id": 0,
            "role": "writer",
            "specialist": "External tool writer",
            "description": "Document an operator-run external routine",
            "dependencies": [],
        }],
    })
    contract = build_delivery_contract(plan, "Document and inspect external output")

    report = evaluate_delivery(tmp_path, contract, plan["steps"], [])

    assert not report["passed"]
    assert any(
        "required validation target is missing" in failure
        for failure in report["failures"]
    )


def test_external_tool_and_operator_routines_are_frozen_in_contract():
    plan = normalize_plan({
        "external_tools": [{
            "name": "Project engine",
            "purpose": "Produce the domain-specific output",
            "availability_probe": "engine --version",
            "configuration": {"quality": "<operator choice>"},
            "commands": ["engine --config project.json"],
            "validation": ["Validate the exported artifact independently"],
        }],
        "execution_routines": [{
            "name": "run-project-engine",
            "purpose": "Let the operator execute the external engine",
            "configuration": {"config_path": "project.json"},
            "steps": ["Review configuration", "Run the engine"],
            "expected_outputs": ["output.bin"],
            "validation": ["Run the registered output validator"],
        }],
        "steps": [{
            "id": 0,
            "role": "writer",
            "specialist": "Integration writer",
            "description": "Document the external engine integration",
            "dependencies": [],
        }],
    })

    contract = build_delivery_contract(plan, "Configure a project-specific engine")

    assert contract["external_tools"][0]["availability_probe"] == "engine --version"
    assert contract["execution_routines"][0]["steps"] == [
        "Review configuration", "Run the engine"
    ]


def test_python_pipeline_is_not_misparsed_as_python_argv():
    shell = ShellCapability(".", timeout_seconds=30)
    assert shell._portable_python_command(
        'python -m pytest -q 2>&1 | findstr failed'
    ) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd pipeline")
def test_windows_python_pipeline_runs_with_redirection(tmp_path):
    shell = ShellCapability(str(tmp_path), timeout_seconds=30)

    result = shell.execute('python -c "print(123)" 2>&1 | findstr 123')

    assert "EXIT_CODE: 0" in result
    assert "123" in result
