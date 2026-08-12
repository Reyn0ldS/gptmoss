import asyncio
from types import SimpleNamespace

import pytest

from gptmoss.capabilities.shell_runtime import ProcessRegistry, ProcessRunner, ShellSafetyPolicy
from gptmoss.providers.qwen import QwenProvider
from gptmoss.providers.qwen_support import ContextWindowPolicy, ToolCallParser


def test_shell_policy_is_pure_configurable_and_adaptive():
    policy = ShellSafetyPolicy(safe_mode=True, timeout_seconds=0)
    assert policy.blocked_reason("taskkill /F /IM python.exe")
    assert policy.blocked_reason("shutdown /s /t 0")
    assert policy.blocked_reason("python -c \"print('shutdown /s is text')\"") is None
    assert policy.effective_timeout("python -m pytest -q") == 900
    assert policy.effective_timeout("pip install package") == 1_800
    policy.configure(False, 17)
    assert policy.blocked_reason("taskkill /F /IM python.exe") is None
    assert policy.effective_timeout("anything") == 17


def test_process_registry_tracks_and_cancels_only_owned_processes(monkeypatch):
    registry = ProcessRegistry()
    process_a, process_b = object(), object()
    terminated = []
    monkeypatch.setattr(registry, "terminate", terminated.append)
    registry.register("a", process_a)
    registry.register("b", process_b)
    registry.register("a", process_a)
    assert registry.count("a") == 1
    registry.cancel("a")
    assert terminated == [process_a]
    registry.unregister("a", process_a)
    assert registry.count() == 1


def test_process_runner_formats_success_and_releases_registry(monkeypatch, tmp_path):
    class Process:
        returncode = 0
        pid = 42

        def __init__(self, stdout, **kwargs):
            self.stdout = stdout

        def wait(self, timeout=None):
            self.stdout.write("ok")
            self.stdout.flush()
            return self.returncode

        def poll(self):
            return self.returncode

    registry = ProcessRegistry()
    runner = ProcessRunner(registry)
    monkeypatch.setattr(
        "gptmoss.capabilities.shell_runtime.subprocess.Popen",
        lambda *a, **kwargs: Process(**kwargs),
    )
    output = runner.run(
        ["ignored"], use_shell=False, cwd=str(tmp_path), execution_id="exec",
        timeout=1, max_output_chars=0, cancelled=lambda _: False,
    )
    assert output == "EXIT_CODE: 0\nSTDOUT:\nok\n"
    assert registry.count() == 0


def test_process_runner_bounds_large_output_before_returning(monkeypatch, tmp_path):
    class Process:
        returncode = 0
        pid = 43

        def __init__(self, stdout, stderr, **kwargs):
            self.stdout = stdout
            self.stderr = stderr

        def wait(self, timeout=None):
            self.stdout.write("x" * 50_000)
            self.stderr.write("y" * 50_000)
            self.stdout.flush()
            self.stderr.flush()
            return self.returncode

    monkeypatch.setattr(
        "gptmoss.capabilities.shell_runtime.subprocess.Popen",
        lambda *a, **kwargs: Process(**kwargs),
    )
    runner = ProcessRunner(ProcessRegistry())

    output = runner.run(
        ["ignored"], use_shell=False, cwd=str(tmp_path), execution_id="bounded",
        timeout=1, max_output_chars=128, cancelled=lambda _: False,
    )

    assert len(output) < 300
    assert "x" * 128 in output
    assert "STDERR:" not in output
    assert "output truncated by shell safety limit" in output


def test_process_runner_reports_cancellation_that_races_with_process_exit(
    monkeypatch, tmp_path
):
    class Process:
        returncode = -15
        pid = 44

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(
        "gptmoss.capabilities.shell_runtime.subprocess.Popen",
        lambda *args, **kwargs: Process(),
    )
    cancellation_checks = iter((False, True))
    runner = ProcessRunner(ProcessRegistry())

    output = runner.run(
        ["ignored"],
        use_shell=False,
        cwd=str(tmp_path),
        execution_id="cancel-race",
        timeout=1,
        max_output_chars=128,
        cancelled=lambda _: next(cancellation_checks),
    )

    assert output == "Error: Command execution cancelled."


@pytest.mark.asyncio
async def test_qwen_reconfiguration_closes_superseded_and_active_clients(monkeypatch):
    created = []

    class FakeHttpClient:
        pass

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.closed = 0
            self.kwargs = kwargs
            created.append(self)

        async def close(self):
            self.closed += 1

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: FakeHttpClient())
    monkeypatch.setattr("gptmoss.providers.qwen.AsyncOpenAI", FakeOpenAI)
    provider = QwenProvider(api_key="one", base_url="https://one.invalid")
    first = provider.client

    provider.update_config("two", "https://two.invalid", model_name="qwen-next")
    await asyncio.sleep(0)

    assert first.closed == 1
    assert first not in provider._retired_clients
    active = provider.client
    assert active is created[-1]
    await provider.close()
    assert active.closed == 1


def test_context_policy_preserves_system_and_recent_tool_order():
    messages = [{"role": "system", "content": "rules"}]
    messages.extend({"role": "user", "content": str(index) * 500} for index in range(8))
    messages.extend([
        {"role": "assistant", "content": "call"},
        {"role": "tool", "content": "result"},
    ])
    compacted = ContextWindowPolicy.compact(messages, 1_500)
    assert compacted[0] == messages[0]
    assert compacted[-2:] == messages[-2:]
    assert ContextWindowPolicy.message_chars(compacted) <= 1_500
    assert ContextWindowPolicy.is_limit_error(Exception("maximum context length exceeded"))


def test_tool_call_parser_normalizes_native_and_text_calls():
    text = '<tool_call>{"name":"shell.execute","arguments":{"command":"pwd"}}</tool_call>'
    calls = ToolCallParser.parse_text(text)
    assert calls[0]["function"] == {
        "name": "shell.execute", "arguments": {"command": "pwd"},
    }
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))],
        usage=None,
    )
    parsed = ToolCallParser.parse_response(response)
    assert parsed["content"] is None
    assert parsed["tool_calls"] == calls
