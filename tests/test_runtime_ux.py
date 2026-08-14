import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest

from gptmoss.capabilities.filesystem import FilesystemCapability
from gptmoss.capabilities.shell import ShellCapability
from gptmoss.core.context import ContextEngine
from gptmoss.core.event_bus import Event, EventBus
from gptmoss.core.execution import ExecutionEngine
from gptmoss.core.kernel import RuntimeKernel
from gptmoss.core.state import STATE_SCHEMA_VERSION, StateEngine
from gptmoss.memory.ram import RAMMemoryProvider
from gptmoss.planners.complexity import normalize_planning_mode, task_title_from_text
from gptmoss.planners.simple import SimplePlanner
from gptmoss.policies.simple import SimplePolicyProvider
from gptmoss.providers.qwen import QwenProvider
from tests.mock_llm import MockLLMProvider


def _engine(tmp_path, llm=None):
    state = StateEngine()
    engine = ExecutionEngine(
        EventBus(),
        state,
        ContextEngine(state, RAMMemoryProvider()),
        llm or MockLLMProvider(),
        SimplePlanner(llm or MockLLMProvider()),
        SimplePolicyProvider(approval_required_capabilities=[]),
    )
    engine.register_capability("filesystem", FilesystemCapability(str(tmp_path), state))
    engine.register_capability("shell", ShellCapability(str(tmp_path), timeout_seconds=10))
    return engine, state


def test_planning_mode_aliases_and_task_titles():
    assert normalize_planning_mode("equipe-courte") == "short_team"
    assert normalize_planning_mode("unknown") == "auto"
    assert task_title_from_text("  Hello   world  ") == "Hello world"
    assert task_title_from_text("x" * 80).endswith("…")
    assert len(task_title_from_text("x" * 80)) == 72


def test_progress_signature_reuses_digest_when_mtime_and_size_match(tmp_path, monkeypatch):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("cache")
    step = {"required_artifacts": []}
    root = tmp_path / "projects" / "proj-default"
    root.mkdir(parents=True)
    target = root / "work.md"
    target.write_text("same\n", encoding="utf-8")

    hashes = {"count": 0}
    original = engine._hash_workspace_file

    def counted(full_path, filename):
        hashes["count"] += 1
        return original(full_path, filename)

    monkeypatch.setattr(engine, "_hash_workspace_file", counted)
    first = engine._progress_signature("cache", step)
    second = engine._progress_signature("cache", step)
    assert first == second
    assert hashes["count"] == 1
    execution.variables["tool_call_history"] = []
    target.write_text("changed\n", encoding="utf-8")
    third = engine._progress_signature("cache", step)
    assert third != first
    assert hashes["count"] == 2


def test_leading_subdirectory_cd_rewrites_python_to_portable_interpreter(tmp_path):
    project = tmp_path / "project"
    child = project / "child"
    child.mkdir(parents=True)
    shell = ShellCapability(str(project), timeout_seconds=10)
    command = (
        f'cd /d "{child}" && python -c "import os,sys;print(os.path.basename(os.getcwd()));print(sys.executable)"'
        if sys.platform == "win32"
        else f'cd "{child}" && python -c "import os,sys;print(os.path.basename(os.getcwd()));print(sys.executable)"'
    )
    result = shell.execute(command)
    assert "EXIT_CODE: 0" in result
    assert "child" in result
    assert sys.executable in result


@pytest.mark.asyncio
async def test_mock_llm_and_execution_publish_llm_delta(tmp_path):
    llm = MockLLMProvider()
    llm.add_response(content="Hello streamed world")
    engine, state = _engine(tmp_path, llm)
    deltas = []

    async def capture(event: Event):
        if event.type == "LLMDelta":
            deltas.append(event.payload["delta"])

    engine.event_bus.subscribe("LLMDelta", capture)
    execution = state.get_execution("stream")
    execution.status = "pending"
    execution.current_plan = {
        "steps": [{
            "id": 0,
            "role": "coordinator",
            "specialist": "Task Specialist",
            "description": "Reply briefly",
            "dependencies": [],
            "status": "pending",
            "acceptance_criteria": ["A short answer is returned."],
        }],
        "rationale": "direct",
    }
    execution.current_step = 0
    await engine.execute_task("stream", "Reply briefly")
    assert deltas
    assert "Hello streamed world" in "".join(deltas)


@pytest.mark.asyncio
async def test_qwen_stream_assembles_content_and_forwards_deltas():
    provider = QwenProvider(api_key="mock", base_url="http://127.0.0.1:9/v1")

    class _Delta:
        def __init__(self, content=None):
            self.content = content
            self.tool_calls = None

    class _Choice:
        def __init__(self, content):
            self.delta = _Delta(content)

    class _Chunk:
        def __init__(self, content=None, usage=None):
            self.choices = [_Choice(content)] if content is not None else []
            self.usage = usage

    class _Stream:
        def __init__(self):
            self._items = [
                _Chunk("Hel"),
                _Chunk("lo"),
                _Chunk(usage=SimpleNamespace(
                    prompt_tokens=4, completion_tokens=2, total_tokens=6
                )),
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    async def fake_create(**kwargs):
        assert kwargs.get("stream") is True
        return _Stream()

    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    seen = []
    result = await provider.completion(
        messages=[{"role": "user", "content": "hi"}],
        on_text_delta=seen.append,
    )
    assert seen == ["Hel", "lo"]
    assert result["content"] == "Hello"
    assert result["usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "total_tokens": 6,
    }


@pytest.mark.asyncio
async def test_qwen_prompt_tool_fallback_stream_normalizes_dict_usage():
    provider = QwenProvider(api_key="mock", base_url="http://127.0.0.1:9/v1")
    provider._native_tools_supported = False

    class _Delta:
        def __init__(self, content=None):
            self.content = content
            self.tool_calls = None

    class _Chunk:
        def __init__(self, content=None, usage=None):
            self.choices = (
                [SimpleNamespace(delta=_Delta(content))] if content is not None else []
            )
            self.usage = usage

    class _Stream:
        def __init__(self):
            self._items = [
                _Chunk('{"tool_call":{"name":"read_file","arguments":'),
                _Chunk('{"path":"README.md"}}}'),
                _Chunk(usage=SimpleNamespace(
                    prompt_tokens=20, completion_tokens=8, total_tokens=28
                )),
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    async def fake_create(**kwargs):
        assert kwargs.get("stream") is True
        return _Stream()

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    seen = []
    result = await provider.completion(
        messages=[{"role": "user", "content": "read the file"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        on_text_delta=seen.append,
    )

    assert seen
    assert result["tool_calls"][0]["function"] == {
        "name": "read_file", "arguments": {"path": "README.md"}
    }
    assert result["usage"]["total_tokens"] == 28


@pytest.mark.asyncio
async def test_qwen_stream_retries_without_optional_usage_extension():
    provider = QwenProvider(api_key="mock", base_url="http://127.0.0.1:9/v1")
    calls = []

    class _Stream:
        def __init__(self):
            self.done = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.done:
                raise StopAsyncIteration
            self.done = True
            return SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(
                    content="ok", tool_calls=None
                ))],
                usage=None,
            )

    async def fake_create(**kwargs):
        calls.append(kwargs)
        if "stream_options" in kwargs:
            raise RuntimeError("stream_options is not supported")
        return _Stream()

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    result = await provider.completion(
        messages=[{"role": "user", "content": "hi"}], on_text_delta=lambda _: None
    )

    assert len(calls) == 2
    assert result["content"] == "ok"
    assert result["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


def test_state_schema_v2_writes_sidecars_and_reloads(tmp_path):
    path = tmp_path / "state_store.json"
    state = StateEngine(str(path))
    execution = state.get_execution("exec-a")
    execution.results["marker"] = "kept"
    state.get_conversation("exec-a").messages.append({"role": "user", "content": "hello"})
    assert state.save_to_disk()

    index = json.loads(path.read_text(encoding="utf-8"))
    assert index["schema_version"] == STATE_SCHEMA_VERSION
    assert "exec-a" in index["execution_ids"]
    assert "executions" not in index
    sidecar = tmp_path / "state_executions" / index["execution_records"]["exec-a"]["file"]
    conversation = (
        tmp_path / "state_conversations" / index["conversation_records"]["exec-a"]["file"]
    )
    assert sidecar.is_file()
    assert conversation.is_file()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["payload"]["results"]["marker"] == "kept"

    restored = StateEngine(str(path))
    assert restored.get_execution("exec-a").results["marker"] == "kept"
    assert restored.get_conversation("exec-a").messages[0]["content"] == "hello"

    del restored.executions["exec-a"]
    del restored.conversations["exec-a"]
    assert restored.save_to_disk()
    assert not sidecar.exists()
    assert not conversation.exists()


@pytest.mark.asyncio
async def test_flush_loop_ignores_llm_delta_events(tmp_path):
    path = tmp_path / "state.json"
    state = StateEngine(str(path))
    bus = EventBus()
    state.start_db_flush_loop(bus)
    state.get_execution("delta").results["before"] = True
    await bus.publish(Event(type="LLMDelta", payload={"delta": "x"}))
    await asyncio.sleep(0.05)
    index = json.loads(path.read_text(encoding="utf-8"))
    delta_sidecar = tmp_path / "state_executions" / index["execution_records"]["delta"]["file"]
    assert "before" not in json.loads(
        delta_sidecar.read_text(encoding="utf-8")
    )["payload"].get("results", {})
    await bus.publish(Event(type="ExecutionChanged"))
    await state.stop_db_flush_loop()
    assert StateEngine(str(path)).get_execution("delta").results["before"] is True


@pytest.mark.asyncio
async def test_shell_and_filesystem_mutations_share_sorted_path_locks(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("locks")
    overlapping = []
    active = 0

    async def slow_impl(execution_id, capability, action, arguments):
        nonlocal active
        active += 1
        overlapping.append(active)
        await asyncio.sleep(0.05)
        active -= 1
        return "ok"

    engine._call_tool_impl = slow_impl
    await asyncio.gather(
        engine._call_tool("locks", "filesystem", "write", {"path": "shared.txt", "content": "a"}),
        engine._call_tool("locks", "shell", "execute", {"command": "echo hi > shared.txt"}),
    )
    assert max(overlapping) == 1


@pytest.mark.asyncio
async def test_filesystem_mutation_without_path_reports_argument_error_before_ownership(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("missing-path")
    execution.variables["delivery_contract"] = {
        "steps": [{"step_id": 0, "role": "writer", "owned_paths": ["deliverable.md"]}]
    }
    execution.variables["plan_step_id"] = 0
    execution.variables["role_key"] = "writer"

    result = await engine._call_tool(
        "missing-path", "filesystem", "write", {"content": "draft"}
    )

    assert "missing required argument(s): path" in result
    assert "ownership denied" not in result.casefold()


@pytest.mark.asyncio
async def test_tool_dispatch_reports_unexpected_arguments_without_invoking_capability(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("unexpected-argument")
    target = tmp_path / "evidence.txt"
    target.write_text("local evidence", encoding="utf-8")

    result = await engine._call_tool(
        "unexpected-argument",
        "filesystem",
        "read",
        {"path": "evidence.txt", "artifact_id": "not-valid-for-filesystem"},
    )

    assert "unexpected argument(s): artifact_id" in result
    assert "Accepted arguments: path, offset, limit" in result


@pytest.mark.asyncio
async def test_owned_long_artifact_can_be_built_with_bounded_append_calls(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("long-append")
    execution.variables["delivery_contract"] = {
        "steps": [{"step_id": 0, "role": "writer", "owned_paths": ["dossier.md"]}]
    }
    execution.variables["plan_step_id"] = 0
    execution.variables["role_key"] = "writer"

    first = await engine._call_tool(
        "long-append", "filesystem", "write",
        {"path": "dossier.md", "content": "# Section 1\n\nEvidence one.\n"},
    )
    second = await engine._call_tool(
        "long-append", "filesystem", "append",
        {"path": "dossier.md", "content": "\n# Section 2\n\nEvidence two.\n"},
    )

    assert first == "File written successfully to dossier.md"
    assert second == "Content appended successfully to dossier.md"
    assert (tmp_path / "projects" / "proj-default" / "dossier.md").read_text(
        encoding="utf-8"
    ) == (
        "# Section 1\n\nEvidence one.\n\n# Section 2\n\nEvidence two.\n"
    )


@pytest.mark.asyncio
async def test_kernel_stores_planning_mode_and_title():
    state = StateEngine()
    llm = MockLLMProvider()
    engine = ExecutionEngine(
        EventBus(),
        state,
        ContextEngine(state, RAMMemoryProvider()),
        llm,
        SimplePlanner(llm),
        SimplePolicyProvider(),
    )
    kernel = RuntimeKernel(EventBus(), state, engine)
    engine.schedule_execution = lambda *args, **kwargs: None
    exec_id = await kernel.submit_task(
        "Write a concise summary of the attached notes.",
        {"planning_mode": "direct"},
    )
    stored = state.get_execution(exec_id)
    assert stored.variables["planning_mode"] == "direct"
    assert stored.variables["task_title"].startswith("Write a concise summary")


def test_bootstrap_runtime_serves_health_readiness_and_gui(tmp_path):
    """Catch launch regressions that unit tests of isolated engines miss."""
    from gptmoss.api.server import app, init_app
    from main import bootstrap_runtime
    from tests.test_api import ASGIClient

    kernel, engine, state, bus = bootstrap_runtime(str(tmp_path))
    init_app(kernel, engine, state, bus)
    client = ASGIClient(app)

    assert client.get("/health").status_code == 200
    ready = client.get("/readiness")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    page = client.get("/")
    assert page.status_code == 200
    assert 'id="task-planning-mode"' in page.text
    assert "corpus_auto_workflow" in page.text


def test_main_py_process_reaches_readiness(tmp_path):
    """Start the same process as start.bat: python main.py."""
    import socket
    import subprocess
    import time

    import httpx

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    workspace = tmp_path / "launch-workspace"
    proc = subprocess.Popen(
        [
            sys.executable, os.path.join(root, "main.py"),
            "--host", "127.0.0.1", "--port", str(port),
            "--workspace", str(workspace),
        ],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 25
        last_error = None
        while time.time() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                raise AssertionError(
                    f"main.py exited {proc.returncode} during launch:\n{output[-4000:]}"
                )
            try:
                health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
                ready = httpx.get(f"http://127.0.0.1:{port}/readiness", timeout=0.5)
                gui = httpx.get(f"http://127.0.0.1:{port}/", timeout=2.0)
                if (
                    health.status_code == 200
                    and ready.status_code == 200
                    and gui.status_code == 200
                ):
                    assert "task-planning-mode" in gui.text
                    return
            except Exception as exc:
                last_error = exc
                time.sleep(0.15)
        output = ""
        if proc.stdout:
            proc.terminate()
            output = proc.stdout.read()
        raise AssertionError(
            f"main.py did not become ready: {last_error}\n{output[-4000:]}"
        )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
