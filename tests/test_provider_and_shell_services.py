from types import SimpleNamespace

from gptmoss.capabilities.shell_runtime import ProcessRegistry, ProcessRunner, ShellSafetyPolicy
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

        def communicate(self, timeout=None):
            return "ok", ""

        def poll(self):
            return self.returncode

    registry = ProcessRegistry()
    runner = ProcessRunner(registry)
    monkeypatch.setattr("gptmoss.capabilities.shell_runtime.subprocess.Popen", lambda *a, **k: Process())
    output = runner.run(
        ["ignored"], use_shell=False, cwd=str(tmp_path), execution_id="exec",
        timeout=1, max_output_chars=0, cancelled=lambda _: False,
    )
    assert output == "EXIT_CODE: 0\nSTDOUT:\nok\n"
    assert registry.count() == 0


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
