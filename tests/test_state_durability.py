import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gptmoss.core import (
    Event, EventBus, ExecutionStatus, InvalidExecutionTransition, StateEngine,
)


def test_state_atomic_failure_preserves_previous_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state = StateEngine(str(path))
    state.get_execution("stable").results["value"] = "before"
    assert state.save_to_disk()
    previous = path.read_text(encoding="utf-8")

    def fail_write(*args, **kwargs):
        raise PermissionError("simulated locked UNC destination")

    monkeypatch.setattr("gptmoss.core.state.write_text_atomic", fail_write)
    state.get_execution("stable").results["value"] = "after"
    assert not state.save_to_disk()
    assert path.read_text(encoding="utf-8") == previous


def test_state_snapshot_is_versioned_and_legacy_state_still_loads(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "executions": {"legacy": {"execution_id": "legacy", "status": "completed"}}
    }), encoding="utf-8")

    state = StateEngine(str(path))
    assert state.get_execution("legacy").status == "completed"
    assert state.save_to_disk()
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 3
    restored = StateEngine(str(path))
    assert restored.get_execution("legacy").status == "completed"


def test_corrupt_state_fails_closed_and_can_be_replaced(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")

    state = StateEngine(str(path))
    assert state.executions == {}
    assert "Failed to load state from disk" in caplog.text
    assert state.corrupt_backup_path
    assert Path(state.corrupt_backup_path).read_text(encoding="utf-8") == "{not valid json"
    state.get_execution("recovered").results["ok"] = True
    assert state.save_to_disk()
    assert StateEngine(str(path)).get_execution("recovered").results["ok"] is True


def test_future_state_schema_is_quarantined_instead_of_silently_downgraded(tmp_path):
    path = tmp_path / "future.json"
    path.write_text(json.dumps({"schema_version": 999, "executions": {}}), encoding="utf-8")

    state = StateEngine(str(path))

    assert state.executions == {}
    assert state.corrupt_backup_path
    assert not path.exists()


def test_transition_history_is_bounded():
    engine = StateEngine(max_transitions_per_execution=100)
    state = engine.get_execution("bounded")
    for index in range(150):
        engine.transition_execution(state, "running" if index % 2 == 0 else "pending")

    assert len(state.transitions) == 100
    assert state.transitions[0].timestamp <= state.transitions[-1].timestamp


def test_concurrent_saves_are_serialized(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state = StateEngine(str(path))
    state.get_execution("shared").results["stable"] = True
    active = 0
    maximum = 0
    guard = threading.Lock()
    real_writer = __import__("gptmoss.core.state", fromlist=["write_text_atomic"]).write_text_atomic

    def observed_writer(*args, **kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.01)
            return real_writer(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr("gptmoss.core.state.write_text_atomic", observed_writer)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: state.save_to_disk(), range(16)))

    assert all(results)
    assert maximum == 1
    assert StateEngine(str(path)).get_execution("shared").results["stable"] is True


def test_partitioned_state_writes_only_changed_generations(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state = StateEngine(str(path))
    state.get_execution("first").results["revision"] = 1
    state.get_execution("second").results["revision"] = 1
    assert state.save_to_disk()
    before = json.loads(path.read_text(encoding="utf-8"))["execution_records"]

    writes = []
    real_writer = __import__("gptmoss.core.state", fromlist=["write_text_atomic"]).write_text_atomic

    def observed_writer(target, content, **kwargs):
        writes.append(Path(target))
        return real_writer(target, content, **kwargs)

    monkeypatch.setattr("gptmoss.core.state.write_text_atomic", observed_writer)
    state.get_execution("first").results["revision"] = 2
    assert state.save_to_disk()
    after = json.loads(path.read_text(encoding="utf-8"))["execution_records"]

    generation_writes = [item for item in writes if item.parent.name == "state_executions"]
    assert len(generation_writes) == 1
    assert before["second"] == after["second"]
    assert before["first"] != after["first"]


def test_unreferenced_generation_files_are_removed_after_index_commit(tmp_path):
    path = tmp_path / "state.json"
    state = StateEngine(str(path))
    state.get_execution("keep").results["revision"] = 1
    assert state.save_to_disk()
    executions_dir = tmp_path / "state_executions"
    leftover = executions_dir / "orphan-generation.json"
    leftover.write_text("{}", encoding="utf-8")
    assert leftover.is_file()

    state.get_execution("keep").results["revision"] = 2
    assert state.save_to_disk()

    retained = {
        item["file"]
        for item in json.loads(path.read_text(encoding="utf-8"))["execution_records"].values()
    }
    remaining = {item.name for item in executions_dir.glob("*.json")}
    assert leftover.name not in remaining
    assert remaining == retained
    assert StateEngine(str(path)).get_execution("keep").results["revision"] == 2


def test_index_failure_keeps_previous_consistent_generation(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    state = StateEngine(str(path))
    state.get_execution("stable").results["value"] = "before"
    assert state.save_to_disk()
    previous_index = path.read_text(encoding="utf-8")
    real_writer = __import__("gptmoss.core.state", fromlist=["write_text_atomic"]).write_text_atomic

    def fail_index(target, content, **kwargs):
        if Path(target).resolve() == path.resolve():
            raise PermissionError("simulated interruption before index commit")
        return real_writer(target, content, **kwargs)

    monkeypatch.setattr("gptmoss.core.state.write_text_atomic", fail_index)
    state.get_execution("stable").results["value"] = "after"
    assert not state.save_to_disk()
    assert path.read_text(encoding="utf-8") == previous_index
    restored = StateEngine(str(path))
    assert restored.executions["stable"].results["value"] == "before"


def test_generation_identity_prevents_sanitized_name_collisions(tmp_path):
    path = tmp_path / "state.json"
    state = StateEngine(str(path))
    state.get_execution("unsafe/id").results["value"] = "slash"
    state.get_execution("unsafe?id").results["value"] = "question"
    assert state.save_to_disk()

    index = json.loads(path.read_text(encoding="utf-8"))
    references = index["execution_records"]
    assert references["unsafe/id"]["file"] != references["unsafe?id"]["file"]
    restored = StateEngine(str(path))
    assert restored.executions["unsafe/id"].results["value"] == "slash"
    assert restored.executions["unsafe?id"].results["value"] == "question"


@pytest.mark.asyncio
async def test_flush_loop_is_idempotent_detached_and_flushes_on_stop(tmp_path):
    path = tmp_path / "state.json"
    state = StateEngine(str(path))
    bus = EventBus()

    first = state.start_db_flush_loop(bus)
    second = state.start_db_flush_loop(bus)
    assert first is second
    assert bus.subscriber_count() == 1

    state.get_execution("late").results["saved"] = True
    await bus.publish(Event(type="ExecutionChanged"))
    await state.stop_db_flush_loop()

    assert first.done()
    assert bus.subscriber_count() == 0
    restored = StateEngine(str(path))
    assert restored.get_execution("late").results["saved"] is True


@pytest.mark.asyncio
async def test_flush_loop_can_restart_without_leaking_callbacks(tmp_path):
    state = StateEngine(str(tmp_path / "state.json"))
    bus = EventBus()

    for _ in range(5):
        task = state.start_db_flush_loop(bus)
        assert bus.subscriber_count() == 1
        await state.stop_db_flush_loop()
        assert task.done()
        assert bus.subscriber_count() == 0


def test_execution_transitions_are_typed_audited_and_persisted(tmp_path):
    path = tmp_path / "state.json"
    engine = StateEngine(str(path))
    state = engine.get_execution("typed")

    engine.transition_execution(
        state, ExecutionStatus.RUNNING,
        reason="accepted", actor="test", correlation_id="request-1",
    )
    engine.transition_execution(state, "paused", reason="review")
    assert state.status is ExecutionStatus.PAUSED
    assert [(item.previous_status, item.status) for item in state.transitions] == [
        (ExecutionStatus.PENDING, ExecutionStatus.RUNNING),
        (ExecutionStatus.RUNNING, ExecutionStatus.PAUSED),
    ]
    assert state.transitions[0].correlation_id == "request-1"
    assert engine.save_to_disk()

    restored = StateEngine(str(path)).get_execution("typed")
    assert restored.status is ExecutionStatus.PAUSED
    assert restored.transitions[-1].reason == "review"


def test_execution_transition_rejects_invalid_terminal_resume():
    engine = StateEngine()
    state = engine.get_execution("terminal")
    engine.transition_execution(state, "running")
    engine.transition_execution(state, "completed")

    with pytest.raises(InvalidExecutionTransition, match="completed to running"):
        engine.transition_execution(state, "running")

    with pytest.raises(ValueError):
        engine.transition_execution(engine.get_execution("unknown"), "invented")
