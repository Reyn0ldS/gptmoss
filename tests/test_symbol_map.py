import json

import pytest

from scripts.analyze_impact import AmbiguousQuery, SymbolGraph
from scripts.generate_symbol_map import DEFAULT_OUTPUT, check, generate


def _graph_payload():
    return json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))


def _edges(payload):
    return {(edge["source"], edge["target"], edge["kind"]) for edge in payload["edges"]}


def test_committed_symbol_map_is_deterministic_and_current():
    assert check() == []
    generated = generate()
    committed = _graph_payload()

    assert generated == committed
    assert committed["scope"]["structured_data_only"] is False
    assert committed["stats"]["nodes"] > 1_000
    assert committed["stats"]["edges"] > 3_000


def test_symbol_graph_links_gui_routes_websockets_controls_and_scripts():
    payload = _graph_payload()
    edges = _edges(payload)
    nodes = {node["id"]: node for node in payload["nodes"]}

    assert payload["diagnostics"]["unresolved_gui_api_calls"] == []
    assert "gui:pauseActiveExecution" in nodes
    assert (
        "gui:pauseActiveExecution",
        "data:api-route:POST /executions/{execution_id}/pause",
        "calls_api",
    ) in edges
    assert (
        "gui:setupWebSocket",
        "data:api-route:WEBSOCKET /ws/events",
        "opens_websocket",
    ) in edges
    assert any(kind == "triggers" and target == "gui:pauseActiveExecution"
               for source, target, kind in edges)
    assert (
        "script:prepare-offline-source.bat",
        "script:scripts/prepare_offline_source_launcher.py",
        "invokes_script",
    ) in edges


def test_symbol_graph_links_classes_methods_calls_and_composition():
    payload = _graph_payload()
    edges = _edges(payload)

    assert (
        "gptmoss.core.kernel:RuntimeKernel",
        "gptmoss.core.execution:ExecutionEngine",
        "composes",
    ) in edges
    assert (
        "gptmoss.core.kernel:RuntimeKernel.submit_task",
        "gptmoss.core.execution:ExecutionEngine.schedule_execution",
        "calls",
    ) in edges
    assert (
        "module:gptmoss.core",
        "gptmoss.core.execution:ExecutionEngine",
        "imports",
    ) in edges
    assert (
        "gptmoss.core.execution:ExecutionEngine.execute_task",
        "gptmoss.core.execution:ExecutionEngine._execute_task_unlocked",
        "calls",
    ) in edges


def test_symbol_graph_links_structured_data_public_surfaces_and_tests():
    payload = _graph_payload()
    edges = _edges(payload)
    nodes = {node["id"]: node for node in payload["nodes"]}

    assert "data:configuration:api_key" in nodes
    assert "data:execution-variable:task" in nodes
    assert "data:execution-field:status" in nodes
    assert "data:api-route:POST /executions" in nodes
    assert "data:event:ExecutionWaitingProvider" in nodes
    assert not any(node.get("data_kind") == "local-variable" for node in nodes.values())
    assert (
        "gptmoss.api.server:submit_task",
        "data:api-route:POST /executions",
        "exposes",
    ) in edges
    assert any(
        source.startswith("tests.")
        and target == "gptmoss.core.execution:ExecutionEngine.execute_task"
        and kind == "calls"
        for source, target, kind in edges
    )


def test_impact_analysis_returns_consumers_data_surfaces_and_tests():
    graph = SymbolGraph(_graph_payload())
    selected = graph.resolve("ExecutionEngine.execute_task")
    report = graph.impact([selected], depth=2)

    dependent_ids = {item["id"] for item in report["dependents"]}
    test_ids = {item["id"] for item in report["tests"]}
    data_ids = {item["id"] for item in report["structured_data"]}
    assert "gptmoss.core.execution:ExecutionEngine._run_plan_step" in dependent_ids
    assert any(identifier.startswith("tests.test_execution:") for identifier in test_ids)
    assert "data:execution-field:status" in data_ids
    assert "gptmoss/core/execution.py" in report["files"]


def test_file_impact_and_ambiguous_queries_are_safe():
    graph = SymbolGraph(_graph_payload())

    symbols = graph.symbols_for_file("gptmoss/core/kernel.py")
    assert "gptmoss.core.kernel:RuntimeKernel.submit_task" in symbols
    assert graph.impact(symbols, depth=1)["summary"]["files"] >= 1
    with pytest.raises(AmbiguousQuery):
        graph.resolve("Capability")
