from gptmoss.core.delivery_feedback import disjoint_owned_paths
from gptmoss.core.execution_plan import normalize_plan, synthesize_plan_edges
from gptmoss.core.workload import compile_work_graph


def test_normalize_plan_synthesizes_typed_edges_from_roles():
    plan = normalize_plan({
        "steps": [
            {"id": 0, "role": "developer", "description": "Implement", "dependencies": []},
            {"id": 1, "role": "qa", "description": "Validate", "dependencies": [0]},
            {"id": 2, "role": "debugger", "description": "Repair", "dependencies": [1]},
            {"id": 3, "role": "coordinator", "description": "Audit", "dependencies": [2]},
        ]
    })
    kinds = {(edge["from"], edge["to"], edge["type"]) for edge in plan["edges"]}
    assert ("0", "1", "validates") in kinds
    assert ("1", "2", "repairs") in kinds
    assert ("2", "3", "validates") in kinds


def test_source_shards_emit_consolidate_edges():
    compiled = compile_work_graph(
        {
            "steps": [
                {
                    "id": "inventory",
                    "role": "architect",
                    "description": "Inventory the local corpus and extract evidence.",
                    "dependencies": [],
                    "operation": "inventory",
                },
                {
                    "id": "write",
                    "role": "writer",
                    "description": "Write the deliverable.",
                    "dependencies": ["inventory"],
                },
            ]
        },
        {"suggested_partitions": 4, "document_count": 2_000},
    )
    consolidate = [edge for edge in compiled["edges"] if edge["type"] == "consolidates"]
    assert len(consolidate) == 4
    writer = next(step for step in compiled["steps"] if step["role"] == "writer")
    consolidation = next(step for step in compiled["steps"] if step["operation"] == "consolidate")
    assert writer["dependencies"] == [consolidation["id"]]
    assert any(
        edge["from"] == str(consolidation["id"]) and edge["to"] == str(writer["id"])
        for edge in compiled["edges"]
    )


def test_synthesize_is_idempotent_and_uses_string_ids():
    plan = {"steps": [
        {"id": 0, "role": "writer", "description": "Write", "dependencies": []},
        {"id": 1, "role": "qa", "description": "Check", "dependencies": [0]},
    ]}
    first = synthesize_plan_edges(plan)
    second = synthesize_plan_edges(plan)
    assert first == second
    assert all(isinstance(edge["from"], str) and isinstance(edge["to"], str) for edge in first)
