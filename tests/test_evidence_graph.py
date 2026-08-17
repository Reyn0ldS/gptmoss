import pytest

from gptmoss.api.server import app, init_app
from gptmoss.core import ContextEngine, EventBus, ExecutionEngine, RuntimeKernel, StateEngine
from gptmoss.core.evidence_graph import build_evidence_graph
from gptmoss.memory import RAMMemoryProvider
from gptmoss.planners import SimplePlanner
from gptmoss.policies import SimplePolicyProvider
from tests.mock_llm import MockLLMProvider
from tests.test_api import ASGIClient


def test_evidence_graph_covers_read_blocks_and_unifies_sources():
    histories = [
        {
            "capability": "documents",
            "action": "inventory",
            "result": {
                "documents": [
                    {"id": "a1", "source_name": "one.md", "sha256": "abc"},
                    {"id": "a2", "source_name": "copy.md", "sha256": "abc"},
                ]
            },
        },
        {
            "capability": "documents",
            "action": "read",
            "arguments": {"artifact_id": "a1"},
            "result": {
                "artifact_id": "a1",
                "source_name": "one.md",
                "blocks": [{"order": 1}, {"order": 2}],
            },
        },
    ]
    graph = build_evidence_graph(
        {}, histories, corpus_policy={"enabled": True, "document_count": 2},
    )
    sources = [node for node in graph["nodes"] if node["kind"] == "source"]
    assert len(sources) == 1
    assert graph["stats"]["covered_sources"] == 1
    assert any(edge["type"] == "covers" for edge in graph["edges"])


def test_evidence_graph_route_is_scoped_to_execution():
    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        event_bus, state_engine, ContextEngine(state_engine, RAMMemoryProvider()),
        llm, SimplePlanner(llm), SimplePolicyProvider(),
    )
    kernel = RuntimeKernel(event_bus, state_engine, engine)
    init_app(kernel, engine, state_engine, event_bus)
    client = ASGIClient(app)
    created = client.post("/executions", json={"task": "Translate hello.", "planning_mode": "direct"})
    assert created.status_code == 201
    exec_id = created.json()["execution_id"]
    found = client.get(f"/executions/{exec_id}/evidence-graph")
    missing = client.get("/executions/unknown/evidence-graph")
    assert found.status_code == 200
    assert found.json()["nodes"] == []
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_disjoint_validators_can_share_a_wave():
    from gptmoss.core.delivery_feedback import disjoint_owned_paths
    from gptmoss.core.execution import ExecutionEngine
    import asyncio

    event_bus = EventBus()
    state_engine = StateEngine()
    engine = ExecutionEngine(
        event_bus, state_engine, ContextEngine(state_engine, RAMMemoryProvider()),
        MockLLMProvider(), SimplePlanner(MockLLMProvider()), SimplePolicyProvider(),
        max_parallel_plan_steps=2,
    )
    state = state_engine.get_execution("parallel-qa")
    state.status = "running"
    state.variables["parent_execution_id"] = "parent"
    steps = [
        {"id": 0, "role": "writer", "status": "completed", "dependencies": [], "owned_paths": ["dossier.md"]},
        {"id": 1, "role": "qa", "status": "pending", "dependencies": [0], "owned_paths": ["analysis/coverage.md"]},
        {"id": 2, "role": "qa", "status": "pending", "dependencies": [0], "owned_paths": ["analysis/quality.md"]},
        {"id": 3, "role": "coordinator", "status": "pending", "dependencies": [1, 2], "owned_paths": []},
    ]
    assert disjoint_owned_paths(steps[1]["owned_paths"], steps[2]["owned_paths"])
    active = 0
    maximum = 0

    async def run_step(step):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        step["status"] = "completed"
        active -= 1

    await engine._coordinate_plan_execution(
        "parallel-qa", state, steps, "Document", run_step, {}
    )
    assert maximum == 2
    assert all(step["status"] == "completed" for step in steps)
