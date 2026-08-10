import asyncio
import json
import sys
import time

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
    lines = (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["payload"]["api_key"] == "[redacted]"


def test_trace_recorder_repairs_legacy_literal_newline_separators(tmp_path):
    path = tmp_path / "telemetry.jsonl"
    first = {"timestamp": 1, "event_type": "one", "execution_id": "x", "payload": {}}
    second = {"timestamp": 2, "event_type": "two", "execution_id": "x", "payload": {}}
    path.write_text(
        json.dumps(first) + "\\n" + json.dumps(second) + "\\n",
        encoding="utf-8",
    )

    TraceRecorder(path).record("three", "x")

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["event_type"] for record in records] == ["one", "two", "three"]


def test_shell_safe_mode_blocks_destructive_command(tmp_path):
    shell = ShellCapability(str(tmp_path))
    assert "blocked by shell safe mode" in shell.execute("shutdown /s")


def test_shell_safe_mode_allows_shutdown_word_inside_python_output(tmp_path):
    shell = ShellCapability(str(tmp_path))

    result = shell.execute('python -c "print(\'Shutdown called\')"')

    assert "EXIT_CODE: 0" in result
    assert "Shutdown called" in result


@pytest.mark.parametrize(
    "command",
    [
        "taskkill /f /im python.exe",
        "Stop-Process -Name python -Force",
        "pkill python",
        "killall python",
    ],
)
def test_shell_safe_mode_blocks_process_wide_termination(command, tmp_path):
    shell = ShellCapability(str(tmp_path))

    result = shell.execute(command)

    assert "process-wide termination by name" in result


def test_shell_removes_terminal_pagers_that_would_block_hidden_execution():
    assert ShellCapability._without_interactive_pager("git log | more") == "git log"
    assert ShellCapability._without_interactive_pager("type report.txt | less -R") == "type report.txt"


def test_shell_strips_only_redundant_workspace_cd_and_keeps_portable_imports(tmp_path):
    package = tmp_path / "sample_package"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    shell = ShellCapability(str(tmp_path), timeout_seconds=10)
    command = f'cd /d "{tmp_path}" && python -c "import sample_package; print(sample_package.VALUE)"'

    result = shell.execute(command)

    assert "EXIT_CODE: 0" in result
    assert "42" in result
    assert shell._strip_redundant_workspace_cd(
        "cd subdir && python -V", str(tmp_path)
    ).startswith("cd subdir")


def test_shell_keeps_portable_project_imports_with_redirection(tmp_path):
    package = tmp_path / "redirected_package"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 73\n", encoding="utf-8")
    shell = ShellCapability(str(tmp_path), timeout_seconds=10)

    result = shell.execute(
        'python -c "import redirected_package; print(redirected_package.VALUE)" 2>&1'
    )

    assert "EXIT_CODE: 0" in result
    assert "73" in result


def test_shell_executes_multiline_python_with_stderr_merge_and_real_exit_code(tmp_path):
    shell = ShellCapability(str(tmp_path), timeout_seconds=10)
    script = """
from pathlib import Path
Path('multiline-proof.txt').write_text('executed', encoding='utf-8')
print('MULTILINE_EXECUTED')
"""

    result = shell.execute(f'python -c "{script}" 2>&1')
    failure = shell.execute('python -c "raise SystemExit(7)" 2>&1')

    assert "EXIT_CODE: 0" in result
    assert "MULTILINE_EXECUTED" in result
    assert (tmp_path / "multiline-proof.txt").read_text(encoding="utf-8") == "executed"
    assert "EXIT_CODE: 7" in failure


def test_shell_timeout_terminates_a_hung_process_tree(tmp_path):
    shell = ShellCapability(str(tmp_path), timeout_seconds=1)
    started = time.monotonic()

    result = shell.execute('python -c "import time; time.sleep(20)"')

    assert "timed out (1s)" in result
    assert time.monotonic() - started < 8


def test_shell_rejects_leading_cd_outside_assigned_workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    shell = ShellCapability(str(project), timeout_seconds=10)

    result = shell.execute(
        f'cd /d "{tmp_path}" && python -c "print(123)"'
        if sys.platform == "win32"
        else f'cd "{tmp_path}" && python -c "print(123)"'
    )

    assert "escapes the assigned project workspace" in result


def test_shell_allows_leading_cd_to_project_subdirectory(tmp_path):
    project = tmp_path / "project"
    child = project / "child"
    child.mkdir(parents=True)
    shell = ShellCapability(str(project), timeout_seconds=10)

    result = shell.execute(
        f'cd /d "{child}" && python -c "import os;print(os.path.basename(os.getcwd()))"'
        if sys.platform == "win32"
        else f'cd "{child}" && python -c "import os;print(os.path.basename(os.getcwd()))"'
    )

    assert "EXIT_CODE: 0" in result
    assert "child" in result


def test_shell_blocks_copy_destination_outside_assigned_workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "runtime.pth"
    external.write_text("original", encoding="utf-8")
    shell = ShellCapability(str(project), timeout_seconds=10)

    command = (
        f'copy "{external}" "{external}.bak"'
        if sys.platform == "win32"
        else f'cp "{external}" "{external}.bak"'
    )
    result = shell.execute(command)

    assert "mutation targets outside the assigned project workspace" in result
    assert not (tmp_path / "runtime.pth.bak").exists()


def test_shell_allows_copy_from_external_source_into_assigned_workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "fixture.txt"
    external.write_text("fixture", encoding="utf-8")
    destination = project / "fixture.txt"
    shell = ShellCapability(str(project), timeout_seconds=10)

    command = (
        f'copy "{external}" "{destination}"'
        if sys.platform == "win32"
        else f'cp "{external}" "{destination}"'
    )
    result = shell.execute(command)

    assert "EXIT_CODE: 0" in result
    assert destination.read_text(encoding="utf-8") == "fixture"


@pytest.mark.asyncio
async def test_shell_cancellation_stops_the_active_process(tmp_path):
    state = StateEngine()
    execution = state.get_execution("cancel-shell")
    execution.status = "running"
    execution.variables["project_path"] = str(tmp_path)
    shell = ShellCapability(str(tmp_path), state_engine=state, timeout_seconds=30)
    task = asyncio.create_task(asyncio.to_thread(
        shell.execute,
        'python -c "import time; time.sleep(20)"',
        {"execution_id": "cancel-shell"},
    ))
    await asyncio.sleep(0.3)

    execution.status = "cancelled"
    shell.cancel_execution("cancel-shell")
    result = await asyncio.wait_for(task, timeout=8)

    assert "cancelled" in result


def test_subagent_delivery_contract_normalizes_plain_and_json_responses():
    structured = ExecutionEngine._structured_delivery('{"summary": "done", "artifacts": ["report.md"]}')
    assert structured["summary"] == "done"
    assert structured["artifacts"] == ["report.md"]
    assert ExecutionEngine._structured_delivery("plain answer")["summary"] == "plain answer"
