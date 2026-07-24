import pytest

from gptmoss.capabilities.shell import ShellCapability
from gptmoss.core.context import ContextEngine
from gptmoss.core.execution import ExecutionEngine
from gptmoss.core.event_bus import EventBus
from gptmoss.core.observability import TraceRecorder
from gptmoss.core.state import StateEngine
from gptmoss.memory.ram import RAMMemoryProvider


@pytest.mark.asyncio
async def test_context_budget_compacts_old_messages_and_large_tool_output():
    state = StateEngine()
    conversation = state.get_conversation("context-test")
    conversation.messages = [
        {"role": "user", "content": "old-" + "x" * 200},
        {"role": "tool", "content": "tool-" + "y" * 200},
        {"role": "assistant", "content": "recent"},
    ]
    context = await ContextEngine(state, RAMMemoryProvider(), max_history_chars=100, max_tool_output_chars=40).compile_context(
        "context-test", "context-test", "default", []
    )

    assert context["context_summary"]
    assert context["conversation_history"][-1]["content"] == "recent"


def test_trace_recorder_redacts_secrets_and_counts_events(tmp_path):
    recorder = TraceRecorder(tmp_path / "telemetry.jsonl")
    recorder.record("tool_completed", "exec-1", api_key="secret", result="ok")

    assert recorder.events[0]["payload"]["api_key"] == "[redacted]"
    assert recorder.metrics("exec-1")["counts"] == {"tool_completed": 1}
    assert "[redacted]" in (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8")


def test_shell_safe_mode_blocks_destructive_command(tmp_path):
    shell = ShellCapability(str(tmp_path))
    assert "blocked by shell safe mode" in shell.execute("shutdown /s")


def test_subagent_delivery_contract_normalizes_plain_and_json_responses():
    structured = ExecutionEngine._structured_delivery('{"summary": "done", "artifacts": ["report.md"]}')
    assert structured["summary"] == "done"
    assert structured["artifacts"] == ["report.md"]
    assert ExecutionEngine._structured_delivery("plain answer")["summary"] == "plain answer"
