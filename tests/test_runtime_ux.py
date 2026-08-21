import asyncio
import json
import os
import sys
from pathlib import Path
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


def test_verified_document_quality_progress_is_not_limited_by_edit_credits(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("quality-progress")
    repeated = (
        "This repeated professional paragraph contains enough material words to "
        "be detected reliably by the document quality validator."
    )
    execution.current_plan = {
        "artifact_validations": [{
            "path": "dossier.md",
            "validator": "document",
            "constraints": {
                "max_duplicate_paragraphs": 0,
                "duplicate_min_words": 8,
            },
        }],
        "steps": [],
    }
    step = {"required_artifacts": ["dossier.md"]}
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    target.write_text(f"# Review\n\n{repeated}\n\n{repeated}\n", encoding="utf-8")
    before = engine._progress_signature("quality-progress", step)
    execution.variables["quality_edit_credits"] = {"dossier.md": 2}

    target.write_text(f"# Review\n\n{repeated}\n", encoding="utf-8")
    after = engine._progress_signature("quality-progress", step)

    improved, reason = engine._quality_improved(
        "quality-progress", before, after,
    )
    assert improved is True
    assert reason == "document_quality_improved"


def test_quality_progress_cannot_hide_regression_in_another_artifact(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("quality-regression")
    repeated = (
        "This repeated professional paragraph contains enough material words to "
        "be detected reliably by the document quality validator."
    )
    constraints = {
        "max_duplicate_paragraphs": 0,
        "duplicate_min_words": 8,
    }
    execution.current_plan = {
        "artifact_validations": [
            {"path": "one.md", "validator": "document", "constraints": constraints},
            {"path": "two.md", "validator": "document", "constraints": constraints},
        ],
        "steps": [],
    }
    step = {"required_artifacts": ["one.md", "two.md"]}
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    one = project / "one.md"
    two = project / "two.md"
    one.write_text(f"# One\n\n{repeated}\n\n{repeated}\n", encoding="utf-8")
    two.write_text(f"# Two\n\n{repeated}\n", encoding="utf-8")
    before = engine._progress_signature("quality-regression", step)
    execution.variables["quality_edit_credits"] = {"one.md": 2, "two.md": 2}

    one.write_text(f"# One\n\n{repeated}\n", encoding="utf-8")
    two.write_text(
        f"# Two\n\n{repeated}\n\n{repeated}\n\n{repeated}\n",
        encoding="utf-8",
    )
    after = engine._progress_signature("quality-regression", step)

    assert engine._quality_improved("quality-regression", before, after) == (
        False,
        "no_quality_delta",
    )


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


@pytest.mark.asyncio
async def test_qwen_stream_options_rejection_walks_openai_cause_chain():
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
            wrapped = Exception("Error code: 400")
            wrapped.__cause__ = Exception("unknown field: stream_options")
            raise wrapped
        return _Stream()

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    result = await provider.completion(
        messages=[{"role": "user", "content": "hi"}], on_text_delta=lambda _: None
    )

    assert len(calls) == 2
    assert result["content"] == "ok"


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
async def test_owned_document_paragraph_can_be_repaired_without_global_rewrite(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("paragraph-repair")
    execution.variables["delivery_contract"] = {
        "steps": [{"step_id": 0, "role": "writer", "owned_paths": ["dossier.md"]}]
    }
    execution.variables["plan_step_id"] = 0
    execution.variables["role_key"] = "writer"
    repeated = (
        "This material architectural paragraph is deliberately repeated so the "
        "targeted repair can remove only its second occurrence safely."
    )
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    target.write_text(
        f"# Decision\n\n{repeated}\n\n{repeated}\n\nFinal paragraph.\n",
        encoding="utf-8",
    )

    removed = await engine._call_tool(
        "paragraph-repair", "filesystem", "replace_paragraph",
        {
            "path": "dossier.md",
            "paragraph_prefix": "this material architectural paragraph is deliberately repeated so the targeted repair",
            "content": "",
            "occurrence": 2,
        },
    )
    corrected = (
        "This material architectural paragraph now has a bounded local source "
        "reference. [architecture.docx > blocks 2-3]"
    )
    replaced = await engine._call_tool(
        "paragraph-repair", "filesystem", "replace_paragraph",
        {
            "path": "dossier.md",
            "paragraph_prefix": "THIS material, architectural paragraph is deliberately repeated so the targeted repair!",
            "content": corrected,
        },
    )

    content = target.read_text(encoding="utf-8")
    assert "occurrence 2 replaced successfully" in removed
    assert "occurrence 1 replaced successfully" in replaced
    assert repeated not in content
    assert corrected in content
    assert content.startswith("# Decision")
    assert content.endswith("Final paragraph.\n")


@pytest.mark.asyncio
async def test_owned_markdown_list_item_can_be_repaired_by_reported_prefix(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("list-item-repair")
    execution.variables["delivery_contract"] = {
        "steps": [{"step_id": 0, "role": "architect", "owned_paths": ["inventory.md"]}]
    }
    execution.variables["plan_step_id"] = 0
    execution.variables["role_key"] = "architect"
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "inventory.md"
    target.write_text(
        "# Coverage\n\n- First stable inventory item.\n"
        "- Total of 751 normalized blocks across all documents.\n"
        "- Final stable inventory item.\n",
        encoding="utf-8",
    )

    result = await engine._call_tool(
        "list-item-repair", "filesystem", "replace_paragraph",
        {
            "path": "inventory.md",
            "paragraph_prefix": "- Total of 751 normalized blocks across all documents.",
            "content": "- Total of 811 normalized blocks across all documents.",
        },
    )

    content = target.read_text(encoding="utf-8")
    assert "Markdown line occurrence 1 replaced successfully" in result
    assert "751" not in content
    assert "- Total of 811 normalized blocks" in content
    assert "- First stable inventory item." in content
    assert "- Final stable inventory item." in content


@pytest.mark.asyncio
async def test_owned_markdown_heading_reference_can_be_repaired_by_reported_prefix(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("heading-repair")
    execution.variables["delivery_contract"] = {
        "steps": [{"step_id": 0, "role": "architect", "owned_paths": ["inventory.md"]}]
    }
    execution.variables["plan_step_id"] = 0
    execution.variables["role_key"] = "architect"
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "inventory.md"
    target.write_text(
        "# Inventory\n\n### 2.1 `qualification/requirements-specification.txt` "
        "[> (root) > blocks 0-3]\n\n"
        "Material evidence remains below this heading.\n",
        encoding="utf-8",
    )

    result = await engine._call_tool(
        "heading-repair", "filesystem", "replace_paragraph",
        {
            "path": "inventory.md",
            "paragraph_prefix": (
                "### 2.1 `qualification/requirements-specification.txt` "
                "[> (root) > blocks 0-3]"
            ),
            "content": (
                "### 2.1 `qualification/requirements-specification.txt` "
                "[qualification/requirements-specification.txt > (root) > blocks 1-4]"
            ),
        },
    )

    content = target.read_text(encoding="utf-8")
    assert "Markdown line occurrence 1 replaced successfully" in result
    assert "[> (root)" not in content
    assert "[qualification/requirements-specification.txt > (root) > blocks 1-4]" in content
    assert "Material evidence remains below this heading." in content


@pytest.mark.asyncio
async def test_short_duplicate_markdown_heading_occurrence_can_be_removed(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("duplicate-heading-repair")
    execution.variables["delivery_contract"] = {
        "steps": [{"step_id": 0, "role": "architect", "owned_paths": ["inventory.md"]}]
    }
    execution.variables["plan_step_id"] = 0
    execution.variables["role_key"] = "architect"
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "inventory.md"
    target.write_text(
        "# Inventory\n\n## Conclusion\n\nFirst body.\n\n"
        "Conclusion\n\n## Conclusion\n\nSecond body remains.\n",
        encoding="utf-8",
    )

    result = await engine._call_tool(
        "duplicate-heading-repair", "filesystem", "replace_paragraph",
        {
            "path": "inventory.md", "paragraph_prefix": "## Conclusion",
            "content": "", "occurrence": 2,
        },
    )

    content = target.read_text(encoding="utf-8")
    assert "Markdown line occurrence 2 replaced successfully" in result
    assert content.count("## Conclusion") == 1
    assert "\nConclusion\n" in content
    assert "First body." in content
    assert "Second body remains." in content


@pytest.mark.asyncio
async def test_owned_plain_markdown_line_can_be_repaired_by_reported_prefix(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("plain-line-repair")
    execution.variables["delivery_contract"] = {
        "steps": [{"step_id": 0, "role": "architect", "owned_paths": ["inventory.md"]}]
    }
    execution.variables["plan_step_id"] = 0
    execution.variables["role_key"] = "architect"
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "inventory.md"
    target.write_text(
        "# Coverage\n\n**Evidence Trace:**\n"
        "The first source remains valid [first.txt > blocks 1-2].\n"
        "The second source has invalid bounds [second.txt > blocks 0-3].\n"
        "The final source remains valid [third.txt > block 1].\n",
        encoding="utf-8",
    )

    result = await engine._call_tool(
        "plain-line-repair", "filesystem", "replace_paragraph",
        {
            "path": "inventory.md",
            "paragraph_prefix": (
                "The second source has invalid bounds [second.txt > blocks 0-3]."
            ),
            "content": (
                "The second source has valid bounds [second.txt > blocks 1-4]."
            ),
        },
    )

    content = target.read_text(encoding="utf-8")
    assert "Markdown line occurrence 1 replaced successfully" in result
    assert "second.txt > blocks 0-3" not in content
    assert "second.txt > blocks 1-4" in content
    assert "The first source remains valid" in content
    assert "The final source remains valid" in content


@pytest.mark.asyncio
async def test_reference_led_markdown_line_uses_literal_selector_fallback(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("reference-line-repair")
    execution.variables["delivery_contract"] = {
        "steps": [{"step_id": 0, "role": "architect", "owned_paths": ["inventory.md"]}]
    }
    execution.variables["plan_step_id"] = 0
    execution.variables["role_key"] = "architect"
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "inventory.md"
    target.write_text(
        "# Coverage\n\n"
        "- [qualification/acceptance-criteria.txt > blocks 0-3]: Fully covered.\n"
        "- [qualification/architecture-reference.docx > blocks 1-97]: Fully covered.\n",
        encoding="utf-8",
    )

    result = await engine._call_tool(
        "reference-line-repair", "filesystem", "replace_paragraph",
        {
            "path": "inventory.md",
            "paragraph_prefix": (
                "- [qualification/acceptance-criteria.txt > blocks 0-3]: Fully covered."
            ),
            "content": (
                "- [qualification/acceptance-criteria.txt > blocks 1-4]: Fully covered."
            ),
        },
    )

    content = target.read_text(encoding="utf-8")
    assert "Markdown line occurrence 1 replaced successfully" in result
    assert "acceptance-criteria.txt > blocks 0-3" not in content
    assert "acceptance-criteria.txt > blocks 1-4" in content
    assert "architecture-reference.docx > blocks 1-97" in content
    assert content.count("Fully covered.") == 2


@pytest.mark.asyncio
async def test_citation_selector_disambiguates_repeated_table_label(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("citation-disambiguation")
    execution.variables["delivery_contract"] = {
        "steps": [{"step_id": 0, "role": "architect", "owned_paths": ["inventory.md"]}]
    }
    execution.variables["plan_step_id"] = 0
    execution.variables["role_key"] = "architect"
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "inventory.md"
    target.write_text(
        "# First matrix\n\n"
        "| DEC-003 | Deterministic validators | [quality.py > blocks 1-44] | Complete |\n\n"
        "# Second matrix\n\n"
        "| DEC-003 | Deterministic validators | [quality.py > blocks 0-18] | Complete |\n",
        encoding="utf-8",
    )

    result = await engine._call_tool(
        "citation-disambiguation", "filesystem", "replace_paragraph",
        {
            "path": "inventory.md",
            "paragraph_prefix": (
                "| DEC-003 | Deterministic validators | "
                "[quality.py > blocks 0-18] | Complete |"
            ),
            "content": (
                "| DEC-003 | Deterministic validators | "
                "[quality.py > blocks 1-44] | Complete |"
            ),
        },
    )

    content = target.read_text(encoding="utf-8")
    assert "Markdown line occurrence 1 replaced successfully" in result
    assert "blocks 0-18" not in content
    assert content.count("[quality.py > blocks 1-44]") == 2


@pytest.mark.asyncio
async def test_paragraph_repair_rejects_ambiguous_short_prefix(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("short-prefix")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    target.write_text("A paragraph that must remain intact.\n", encoding="utf-8")

    result = await engine._call_tool(
        "short-prefix", "filesystem", "replace_paragraph",
        {
            "path": "dossier.md",
            "paragraph_prefix": "too short",
            "content": "replacement",
        },
    )

    assert "at least 24 normalized characters" in result
    assert target.read_text(encoding="utf-8") == "A paragraph that must remain intact.\n"


@pytest.mark.asyncio
async def test_active_required_artifact_cannot_be_deleted_by_its_writer(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("protected-delivery")
    execution.current_plan = {
        "steps": [{
            "id": 0,
            "role": "writer",
            "required_artifacts": ["dossier.md"],
            "owned_paths": ["dossier.md"],
        }]
    }
    execution.current_step = 0
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    target.write_text("validated draft", encoding="utf-8")

    result = await engine._call_tool(
        "protected-delivery", "filesystem", "delete", {"path": "dossier.md"},
    )

    assert "Deletion blocked for active required artifact" in result
    assert target.read_text(encoding="utf-8") == "validated draft"

    empty_result = await engine._call_tool(
        "protected-delivery", "filesystem", "write",
        {"path": "dossier.md", "content": ""},
    )
    assert "Empty overwrite blocked for active required artifact" in empty_result
    assert target.read_text(encoding="utf-8") == "validated draft"


@pytest.mark.parametrize("role", ["writer", "architect"])
@pytest.mark.asyncio
async def test_specialist_cannot_globally_overwrite_existing_document_without_gate(
    tmp_path, role,
):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("guarded-document")
    execution.variables["role_key"] = role
    execution.current_plan = {
        "steps": [{
            "id": 0,
            "role": role,
            "required_artifacts": ["dossier.md"],
            "owned_paths": ["dossier.md"],
        }],
        "artifact_validations": [{
            "path": "dossier.md",
            "validator": "document",
            "constraints": {"minimums": {"words": 2}},
        }],
    }
    execution.current_step = 0
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    target.write_text("validated durable draft", encoding="utf-8")

    blocked = await engine._call_tool(
        "guarded-document", "filesystem", "write",
        {"path": "dossier.md", "content": "short replacement"},
    )
    assert "Global overwrite blocked" in blocked
    assert target.read_text(encoding="utf-8") == "validated durable draft"

    execution.variables["step_runtime"] = {
        "0": {"required_next_tool": "filesystem__write"},
    }
    allowed = await engine._call_tool(
        "guarded-document", "filesystem", "write",
        {"path": "dossier.md", "content": "gate-authorized replacement"},
    )
    assert "written successfully" in allowed
    assert target.read_text(encoding="utf-8") == "gate-authorized replacement"


@pytest.mark.asyncio
async def test_retry_child_appends_code_wrapped_citations_reported_by_gate(
    tmp_path,
):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("citation-retry")
    execution.variables["role_key"] = "architect"
    execution.variables["delegated_step"] = {
        "retry_context": (
            "Current machine gate failures: dossier.md: 48 citation-like pattern(s) "
            "inside Markdown code do not count as evidence; write actual citations "
            "without backticks or code fences"
        ),
    }
    execution.current_plan = {
        "steps": [{
            "id": 0,
            "role": "architect",
            "required_artifacts": ["dossier.md"],
            "owned_paths": ["dossier.md"],
        }],
        "artifact_validations": [{
            "path": "dossier.md",
            "validator": "document",
            "constraints": {"require_local_references": True},
        }],
    }
    execution.current_step = 0
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    original = "Evidence: `[source.md > Scope > blocks 1-2]`"
    target.write_text(original, encoding="utf-8")

    blocked = await engine._call_tool(
        "citation-retry", "filesystem", "write",
        {
            "path": "dossier.md",
            "content": "Evidence: [source.md > Scope > blocks 1-2]",
        },
    )
    assert "Global overwrite blocked" in blocked
    assert target.read_text(encoding="utf-8") == original

    allowed = await engine._call_tool(
        "citation-retry", "filesystem", "append",
        {
            "path": "dossier.md",
            "content": " The source is cited. [source.md > Scope > blocks 1-2]",
        },
    )
    assert "appended successfully" in allowed
    assert "[source.md > Scope > blocks 1-2]" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_gate_authorized_rewrite_cannot_discard_most_of_a_long_document(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("lossy-document-retry")
    execution.variables["role_key"] = "writer"
    execution.current_plan = {
        "steps": [{
            "id": 0, "role": "writer",
            "required_artifacts": ["dossier.md"], "owned_paths": ["dossier.md"],
        }],
        "artifact_validations": [{
            "path": "dossier.md", "validator": "document", "constraints": {},
        }],
    }
    execution.current_step = 0
    execution.variables["step_runtime"] = {
        "0": {"required_next_tool": "filesystem__write"},
    }
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    original = "# Dossier\n\n" + ("Contenu validé et sourcé. " * 400)
    target.write_text(original, encoding="utf-8")

    blocked = await engine._call_tool(
        "lossy-document-retry", "filesystem", "write",
        {"path": "dossier.md", "content": "# Dossier\n\nRésumé tronqué."},
    )

    assert "Lossy global rewrite blocked" in blocked
    assert target.read_text(encoding="utf-8") == original


def test_profile_upgrade_reopens_invalid_completed_producer_and_consumers(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("resume-revalidation")
    execution.current_plan = {
        "steps": [
            {
                "id": 0, "status": "completed", "dependencies": [],
                "required_artifacts": ["analysis/decisions.md"],
                "assigned_execution_id": "old-producer",
            },
            {
                "id": 1, "status": "completed", "dependencies": [0],
                "required_artifacts": ["dossier.md"],
                "assigned_execution_id": "old-consumer",
            },
        ],
        "artifact_validations": [{
            "path": "analysis/decisions.md", "validator": "document",
            "constraints": {"required_headings": ["Validated decisions"]},
        }],
    }
    execution.results["steps"] = {"0": {"old": True}, "1": {"old": True}}
    project = tmp_path / "projects" / "proj-default"
    (project / "analysis").mkdir(parents=True)
    (project / "analysis" / "decisions.md").write_text(
        "# Incomplete decisions\n\nNo validated section.\n", encoding="utf-8",
    )
    (project / "dossier.md").write_text("# Stale dossier\n", encoding="utf-8")

    reopened = engine._reopen_invalid_completed_steps(
        "resume-revalidation", execution, execution.current_plan["steps"],
    )

    assert [step["id"] for step in reopened] == [0, 1]
    assert all(step["status"] == "pending" for step in reopened)
    assert all("assigned_execution_id" not in step for step in reopened)
    assert "deterministic profile upgrade" in reopened[0]["retry_context"]
    assert "upstream artifact was reopened" in reopened[1]["retry_context"]
    assert execution.results["steps"] == {}


def test_profile_upgrade_refreshes_obsolete_pending_retry_context(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("resume-pending-revalidation")
    execution.current_plan = {
        "steps": [{
            "id": 0, "status": "pending", "dependencies": [],
            "required_artifacts": ["analysis/inventory.md"],
            "retry_context": (
                "A deterministic profile upgrade invalidated this persisted artifact. "
                "Preserve valid content and repair these machine-observed defects:\n"
                "obsolete semantic record defect"
            ),
        }],
        "artifact_validations": [{
            "path": "analysis/inventory.md", "validator": "document",
            "constraints": {"required_headings": ["Coverage"]},
        }],
    }
    project = tmp_path / "projects" / "proj-default" / "analysis"
    project.mkdir(parents=True)
    (project / "inventory.md").write_text("# Inventory\n", encoding="utf-8")

    reopened = engine._reopen_invalid_completed_steps(
        "resume-pending-revalidation", execution, execution.current_plan["steps"],
    )

    assert reopened == []
    retry_context = execution.current_plan["steps"][0]["retry_context"]
    assert "missing required heading(s): Coverage" in retry_context
    assert "obsolete semantic record defect" not in retry_context


def test_profile_upgrade_replaces_vague_upstream_retry_with_current_defects(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("resume-upstream-revalidation")
    execution.current_plan = {
        "steps": [{
            "id": 0, "status": "pending", "dependencies": [],
            "required_artifacts": ["analysis/architecture.md"],
            "retry_context": (
                "A completed upstream artifact was reopened by stronger deterministic "
                "quality gates. Reuse its corrected result and refresh this dependent "
                "artifact without trusting stale conclusions."
            ),
        }],
        "artifact_validations": [{
            "path": "analysis/architecture.md", "validator": "document",
            "constraints": {"required_headings": ["Validated architecture"]},
        }],
    }
    project = tmp_path / "projects" / "proj-default" / "analysis"
    project.mkdir(parents=True)
    (project / "architecture.md").write_text("# Draft\n", encoding="utf-8")

    reopened = engine._reopen_invalid_completed_steps(
        "resume-upstream-revalidation", execution, execution.current_plan["steps"],
    )

    assert reopened == []
    retry_context = execution.current_plan["steps"][0]["retry_context"]
    assert retry_context.startswith("A deterministic profile upgrade invalidated")
    assert "missing required heading(s): Validated architecture" in retry_context
    assert "without trusting stale conclusions" not in retry_context


def test_resume_refreshes_stale_failed_attempt_gates_and_tool_constraint(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("resume-failed-writer")
    execution.current_plan = {
        "steps": [{
            "id": 0, "status": "pending", "dependencies": [],
            "required_artifacts": ["dossier.md"],
            "retry_context": (
                "Previous attempt failed. Current machine gate failures: "
                "obsolete duplicate heading defect"
            ),
        }],
        "artifact_validations": [{
            "path": "dossier.md", "validator": "document",
            "constraints": {
                "required_headings": ["Registre des risques"],
                "min_section_words": 20,
            },
        }],
    }
    execution.variables["step_runtime"] = {"0": {
        "required_next_tool": "filesystem__replace_paragraph",
        "required_repair_issues": ["obsolete duplicate heading defect"],
    }}
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text(
        "# Dossier\n\n## Registre des risques\n\nCourt.\n",
        encoding="utf-8",
    )

    engine._reopen_invalid_completed_steps(
        "resume-failed-writer", execution, execution.current_plan["steps"],
    )

    refreshed = execution.current_plan["steps"][0]["retry_context"]
    assert refreshed.startswith("A resumed specialist retry was refreshed")
    assert "exact Markdown heading selector(s): ## Registre des risques" in refreshed
    assert "obsolete duplicate heading defect" not in refreshed
    assert "required_next_tool" not in execution.variables["step_runtime"]["0"]
    assert "required_repair_issues" not in execution.variables["step_runtime"]["0"]


@pytest.mark.asyncio
async def test_empty_section_repair_rejects_heading_reported_for_another_defect(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("guarded-empty-section")
    execution.variables["role_key"] = "writer"
    execution.current_plan = {
        "steps": [{
            "id": 0, "role": "writer", "required_artifacts": ["dossier.md"],
            "owned_paths": ["dossier.md"],
        }],
        "artifact_validations": [{
            "path": "dossier.md", "validator": "document", "constraints": {},
        }],
    }
    execution.current_step = 0
    execution.variables["step_runtime"] = {"0": {
        "required_next_tool": "filesystem__replace_section",
        "required_repair_issues": [
            "empty required section(s): Registre des risques; exact Markdown "
            "heading selector(s): # Registre des risques",
            "duplicate heading selector(s): ### 7.1. Analyse détaillée des risques",
        ],
    }}
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    original = (
        "# Registre des risques\n\nCourt.\n\n"
        "### 7.1. Analyse détaillée des risques\n\nStable.\n"
    )
    target.write_text(original, encoding="utf-8")

    blocked = await engine._call_tool(
        "guarded-empty-section", "filesystem", "replace_section",
        {
            "path": "dossier.md",
            "heading_selector": "### 7.1. Analyse détaillée des risques",
            "content": "Mauvaise cible.",
        },
    )

    assert "use only a selector listed after" in blocked
    assert target.read_text(encoding="utf-8") == original


def test_profile_upgrade_freezes_all_existing_record_ids_before_repair(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("resume-record-preservation")
    policy = {
        "heading_pattern": r"\bDEC-\d{3}\b",
        "minimum_records": 1,
        "preserve_existing_record_ids": True,
        "required_fields": {"context": ["context"], "decision": ["decision"]},
    }
    execution.current_plan = {
        "steps": [{
            "id": 0, "status": "completed", "dependencies": [],
            "required_artifacts": ["analysis/decisions.md"],
        }],
        "artifact_validations": [{
            "path": "analysis/decisions.md", "validator": "document",
            "constraints": {"record_section_policy": policy},
        }],
    }
    project = tmp_path / "projects" / "proj-default" / "analysis"
    project.mkdir(parents=True)
    (project / "decisions.md").write_text(
        "# Decisions\n\n### DEC-001: Storage\n\n- **Context:** A\n\n"
        "### DEC-002: Runtime\n\n- **Context:** B\n",
        encoding="utf-8",
    )

    reopened = engine._reopen_invalid_completed_steps(
        "resume-record-preservation", execution, execution.current_plan["steps"],
    )

    frozen = execution.current_plan["artifact_validations"][0]["constraints"][
        "record_section_policy"
    ]
    assert [step["id"] for step in reopened] == [0]
    assert frozen["required_record_ids"] == ["DEC-001", "DEC-002"]
    assert frozen["minimum_records"] == 2
    assert "### DEC-001: Storage" in reopened[0]["retry_context"]
    assert "### DEC-002: Runtime" in reopened[0]["retry_context"]


@pytest.mark.asyncio
async def test_section_repair_requires_a_machine_reported_heading(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("guarded-section-repair")
    execution.variables["role_key"] = "architect"
    execution.current_plan = {
        "steps": [{
            "id": 0, "role": "architect",
            "required_artifacts": ["decisions.md"], "owned_paths": ["decisions.md"],
        }],
        "artifact_validations": [{
            "path": "decisions.md", "validator": "document", "constraints": {},
        }],
    }
    execution.variables["step_runtime"] = {"0": {
        "required_next_tool": "filesystem__replace_section",
        "required_repair_issues": [
            "decisions.md: '### DEC-001: Storage' missing alternatives, risks"
        ],
    }}
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "decisions.md"
    target.write_text(
        "### DEC-001: Storage\n\nOld.\n\n### DEC-002: Runtime\n\nStable.\n",
        encoding="utf-8",
    )

    blocked = await engine._call_tool(
        "guarded-section-repair", "filesystem", "replace_section",
        {"path": "decisions.md", "heading_selector": "### DEC-002: Runtime", "content": "Wrong."},
    )
    allowed = await engine._call_tool(
        "guarded-section-repair", "filesystem", "replace_section",
        {
            "path": "decisions.md", "heading_selector": "### DEC-001: Storage",
            "content": "- **Alternatives:** A\n- **Risks:** B",
        },
    )

    assert "Section repair blocked" in blocked
    assert "Markdown section replaced successfully" in allowed
    assert "### DEC-002: Runtime\n\nStable." in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_targeted_repair_must_use_a_machine_reported_prefix(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("guarded-targeted-repair")
    execution.variables["role_key"] = "architect"
    execution.current_plan = {
        "steps": [{
            "id": 0,
            "role": "architect",
            "required_artifacts": ["inventory.md"],
            "owned_paths": ["inventory.md"],
        }],
        "artifact_validations": [{
            "path": "inventory.md", "validator": "document", "constraints": {},
        }],
    }
    execution.variables["step_runtime"] = {"0": {
        "required_next_tool": "filesystem__replace_paragraph",
        "required_repair_issues": [
            "inventory.md: invalid local reference; paragraph prefix: "
            "The machine-reported evidence line has invalid blocks 0-3."
        ],
    }}
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "inventory.md"
    target.write_text(
        "The unrelated introduction must remain unchanged.\n\n"
        "The machine-reported evidence line has invalid blocks 0-3.\n",
        encoding="utf-8",
    )

    blocked = await engine._call_tool(
        "guarded-targeted-repair", "filesystem", "replace_paragraph",
        {
            "path": "inventory.md",
            "paragraph_prefix": "The unrelated introduction must remain unchanged.",
            "content": "An unrequested rewrite.",
        },
    )
    allowed = await engine._call_tool(
        "guarded-targeted-repair", "filesystem", "replace_paragraph",
        {
            "path": "inventory.md",
            "paragraph_prefix": (
                "The machine-reported evidence line has invalid blocks 0-3."
            ),
            "content": "The machine-reported evidence line has valid blocks 1-4.",
        },
    )

    content = target.read_text(encoding="utf-8")
    assert "Targeted repair blocked" in blocked
    assert "replaced successfully" in allowed
    assert "unrelated introduction must remain unchanged" in content
    assert "valid blocks 1-4" in content


@pytest.mark.asyncio
async def test_targeted_repair_accepts_gate_normalized_prefix_with_terminal_punctuation(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("normalized-prefix-repair")
    execution.variables["role_key"] = "architect"
    execution.current_plan = {
        "steps": [{
            "id": 0, "role": "architect", "required_artifacts": ["inventory.md"],
            "owned_paths": ["inventory.md"],
        }],
        "artifact_validations": [{
            "path": "inventory.md", "validator": "document", "constraints": {},
        }],
    }
    execution.variables["step_runtime"] = {"0": {
        "required_next_tool": "filesystem__replace_paragraph",
        "required_repair_issues": [
            "inventory.md: repeated paragraph prefix(es): cette matrice trace "
            "chaque exigence vers les preuves locales correspondantes"
        ],
    }}
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "inventory.md"
    paragraph = "Cette matrice trace chaque exigence vers les preuves locales correspondantes."
    target.write_text(f"{paragraph}\n\n{paragraph}\n", encoding="utf-8")

    result = await engine._call_tool(
        "normalized-prefix-repair", "filesystem", "replace_paragraph",
        {
            "path": "inventory.md", "paragraph_prefix": paragraph,
            "content": "", "occurrence": 2,
        },
    )

    assert "occurrence 2 replaced successfully" in result


@pytest.mark.asyncio
async def test_source_coverage_append_rejects_repeated_document_structure(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("guarded-source-append")
    execution.variables["role_key"] = "architect"
    execution.current_plan = {
        "steps": [{
            "id": 0, "role": "architect", "required_artifacts": ["inventory.md"],
            "owned_paths": ["inventory.md"],
        }],
        "artifact_validations": [{
            "path": "inventory.md", "validator": "document", "constraints": {},
        }],
    }
    execution.variables["step_runtime"] = {"0": {
        "required_next_tool": "filesystem__append",
        "required_repair_issues": [
            "inventory.md: uncited required source file(s): architecture.md"
        ],
    }}
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "inventory.md"
    target.write_text("# Inventory\n\nExisting evidence.\n", encoding="utf-8")

    blocked = await engine._call_tool(
        "guarded-source-append", "filesystem", "append",
        {"path": "inventory.md", "content": "## Repeated section\n\n| A | B |"},
    )
    allowed = await engine._call_tool(
        "guarded-source-append", "filesystem", "append",
        {
            "path": "inventory.md",
            "content": (
                "La source complète l'analyse locale. "
                "[architecture.md > Architecture > blocks 1-2]"
            ),
        },
    )

    assert "Source-coverage append must be one concise prose paragraph" in blocked
    assert "Content appended successfully" in allowed
    assert "Repeated section" not in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_duplicate_heading_repair_cannot_insert_a_truncated_replacement(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("guarded-duplicate-heading")
    execution.variables["role_key"] = "architect"
    execution.current_plan = {
        "steps": [{
            "id": 0, "role": "architect",
            "required_artifacts": ["architecture.md"], "owned_paths": ["architecture.md"],
        }],
        "artifact_validations": [{
            "path": "architecture.md", "validator": "document", "constraints": {},
        }],
    }
    execution.variables["step_runtime"] = {"0": {
        "required_next_tool": "filesystem__replace_paragraph",
        "required_repair_issues": [
            "architecture.md: duplicate heading occurrence(s); repeated Markdown "
            "heading selector(s): ### Diagram 2"
        ],
    }}
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "architecture.md"
    target.write_text(
        "### Diagram 2\n\nFirst body.\n\n### Diagram 2\n\nSecond body.\n",
        encoding="utf-8",
    )

    blocked = await engine._call_tool(
        "guarded-duplicate-heading", "filesystem", "replace_paragraph",
        {
            "path": "architecture.md", "paragraph_prefix": "### Diagram 2",
            "content": "## 4. Truncated", "occurrence": 2,
        },
    )
    allowed = await engine._call_tool(
        "guarded-duplicate-heading", "filesystem", "replace_paragraph",
        {
            "path": "architecture.md", "paragraph_prefix": "### Diagram 2",
            "content": "", "occurrence": 2,
        },
    )

    content = target.read_text(encoding="utf-8")
    assert "Duplicate-heading repair must" in blocked
    assert "Markdown line occurrence 2 replaced successfully" in allowed
    assert "Truncated" not in content
    assert content.count("### Diagram 2") == 1
    assert "First body." in content and "Second body." in content


@pytest.mark.asyncio
async def test_writer_cannot_bypass_document_overwrite_guard_with_shell(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("guarded-shell-document")
    execution.variables["role_key"] = "writer"
    execution.current_plan = {
        "steps": [{
            "id": 0,
            "role": "writer",
            "required_artifacts": ["dossier.md"],
            "owned_paths": ["dossier.md"],
        }],
        "artifact_validations": [{
            "path": "dossier.md", "validator": "document", "constraints": {},
        }],
    }
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    target.write_text("validated durable draft", encoding="utf-8")
    command = (
        "Set-Content -LiteralPath dossier.md -Value replaced"
        if sys.platform == "win32" else "printf replaced > dossier.md"
    )

    result = await engine._call_tool(
        "guarded-shell-document", "shell", "execute", {"command": command},
    )

    assert "Global overwrite blocked" in result
    assert target.read_text(encoding="utf-8") == "validated durable draft"


@pytest.mark.asyncio
async def test_shell_cannot_bypass_required_artifact_deletion_guard(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("protected-shell-delivery")
    execution.current_plan = {
        "steps": [{
            "id": 0,
            "role": "writer",
            "required_artifacts": ["dossier.md"],
            "owned_paths": ["dossier.md"],
        }]
    }
    execution.current_step = 0
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    target.write_text("validated draft", encoding="utf-8")
    command = "del dossier.md" if sys.platform == "win32" else "rm dossier.md"

    result = await engine._call_tool(
        "protected-shell-delivery", "shell", "execute", {"command": command},
    )

    assert "Deletion blocked for active required artifact" in result
    assert target.read_text(encoding="utf-8") == "validated draft"


def test_writer_gate_nudge_requires_one_bounded_append_on_existing_artifact(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-nudge")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text("# Existing\n\nValid content.\n", encoding="utf-8")
    step = {"role": "writer", "required_artifacts": ["dossier.md"]}

    nudge = engine._writer_incremental_repair_nudge(
        "writer-nudge",
        "writer",
        step,
        ["dossier.md: words=4288 is below required minimum 9000"],
    )

    assert "exactly one valid filesystem__append tool call" in nudge
    assert "400-800 word chunk" in nudge
    assert "do not send the whole document" in nudge
    assert "undeclared part files" in nudge


def test_writer_gate_can_constrain_the_next_turn_to_the_required_mutation(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-tool-constraint")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text("existing", encoding="utf-8")
    step = {"role": "writer", "required_artifacts": ["dossier.md"]}
    issues = ["dossier.md: words=10 is below required minimum 9000"]
    schemas = [
        {"function": {"name": "filesystem__read"}},
        {"function": {"name": "filesystem__append"}},
        {"function": {"name": "shell__execute"}},
    ]

    required = engine._writer_incremental_repair_tool(
        "writer-tool-constraint", "writer", step, issues,
    )
    constrained = engine._schemas_for_required_tool(schemas, required)

    assert required == "filesystem__append"
    assert [item["function"]["name"] for item in constrained] == [
        "filesystem__append"
    ]

    calls = [{
        "id": "append-1",
        "function": {"name": "filesystem__append", "arguments": {}},
    }]
    failed = [{
        "role": "tool", "tool_call_id": "append-1", "content": "Error: denied",
    }]
    succeeded = [{
        "role": "tool", "tool_call_id": "append-1",
        "content": "Content appended successfully to dossier.md",
    }]
    assert not engine._required_tool_succeeded(
        failed, calls, "filesystem__append",
    )
    assert engine._required_tool_succeeded(
        succeeded, calls, "filesystem__append",
    )


def test_writer_duplicate_gate_requires_one_targeted_paragraph_repair(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-rewrite")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text("duplicated", encoding="utf-8")
    step = {"role": "writer", "required_artifacts": ["dossier.md"]}
    issues = ["dossier.md: document contains 23 duplicate paragraph occurrence(s)"]

    required = engine._writer_incremental_repair_tool(
        "writer-rewrite", "writer", step, issues,
    )
    nudge = engine._writer_incremental_repair_nudge(
        "writer-rewrite", "writer", step, issues,
    )

    assert required == "filesystem__replace_paragraph"
    assert "exactly one valid filesystem__replace_paragraph" in nudge
    assert "remove occurrence=2" in nudge
    assert "never rewrite or delete the whole document" in nudge


def test_writer_duplicate_heading_gate_requires_exact_second_heading(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-heading-repair")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text("# Repeated\n", encoding="utf-8")
    step = {"role": "writer", "required_artifacts": ["dossier.md"]}
    issues = [
        "dossier.md: document contains 2 duplicate heading occurrence(s); "
        "repeated Markdown heading selector(s): ## Conclusion"
    ]

    nudge = engine._writer_incremental_repair_nudge(
        "writer-heading-repair", "writer", step, issues,
    )

    assert "including its # markers" in nudge
    assert "occurrence=2" in nudge
    assert "preserves all section body content" in nudge


def test_writer_record_gate_requires_one_reported_section_repair(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-record-repair")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "decisions.md").write_text("### DEC-001: Storage\n", encoding="utf-8")
    step = {"role": "architect", "required_artifacts": ["decisions.md"]}
    issues = [
        "decisions.md: 1 record section(s) violate the declared semantic schema: "
        "'### DEC-001: Storage' missing alternatives, risks"
    ]

    required = engine._writer_incremental_repair_tool(
        "writer-record-repair", "architect", step, issues,
    )
    nudge = engine._writer_incremental_repair_nudge(
        "writer-record-repair", "architect", step, issues,
    )

    assert required == "filesystem__replace_section"
    assert "including its # markers" in nudge
    assert "one record per iteration" in nudge
    assert "every other record and section must remain untouched" in nudge


def test_writer_empty_required_section_gate_requires_in_place_section_fill(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-empty-section")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text(
        "# Dossier\n\n## Registre des risques\n\n## Conclusion\n\nStable.\n",
        encoding="utf-8",
    )
    step = {"role": "writer", "required_artifacts": ["dossier.md"]}
    issues = [
        "dossier.md: empty required section(s): Registre des risques; "
        "exact Markdown heading selector(s): ## Registre des risques",
    ]

    required = engine._writer_incremental_repair_tool(
        "writer-empty-section", "writer", step, issues,
    )
    nudge = engine._writer_incremental_repair_nudge(
        "writer-empty-section", "writer", step, issues,
    )

    assert required == "filesystem__replace_section"
    assert "empty or underdeveloped body" in nudge
    assert "do not modify any other section" in nudge


@pytest.mark.asyncio
async def test_word_count_append_cannot_recreate_existing_section_headings(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("writer-word-count-append")
    execution.variables["role_key"] = "writer"
    execution.current_plan = {
        "steps": [{
            "id": 0, "role": "writer", "required_artifacts": ["dossier.md"],
            "owned_paths": ["dossier.md"],
        }],
        "artifact_validations": [{
            "path": "dossier.md", "validator": "document", "constraints": {},
        }],
    }
    execution.current_step = 0
    execution.variables["step_runtime"] = {"0": {
        "required_next_tool": "filesystem__append",
        "required_repair_issues": ["dossier.md: words=3000 is below required minimum 8750"],
    }}
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    target.write_text("# Dossier\n\nContenu stable.\n", encoding="utf-8")

    blocked = await engine._call_tool(
        "writer-word-count-append", "filesystem", "append",
        {"path": "dossier.md", "content": "\n## Registre des risques\n\nAjout."},
    )

    assert "without adding Markdown headings" in blocked
    assert target.read_text(encoding="utf-8") == "# Dossier\n\nContenu stable.\n"


def test_writer_invalid_diagram_gate_requires_its_reported_section(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-diagram-repair")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "architecture.md").write_text("### Runtime flow\n", encoding="utf-8")
    step = {"role": "architect", "required_artifacts": ["architecture.md"]}
    issues = [
        "architecture.md: document contains 1 invalid diagram(s): line 20 under "
        "section selector '### Runtime flow': self-loop is not allowed: A"
    ]

    required = engine._writer_incremental_repair_tool(
        "writer-diagram-repair", "architect", step, issues,
    )
    nudge = engine._writer_incremental_repair_nudge(
        "writer-diagram-repair", "architect", step, issues,
    )

    assert required == "filesystem__replace_section"
    assert "allowed subset" in nudge
    assert "including self-loops" in nudge
    assert "preserving all other sections" in nudge


def test_writer_unsupported_claim_gate_requires_cited_paragraph_repair(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-citation-repair")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text("unsupported", encoding="utf-8")
    step = {"role": "writer", "required_artifacts": ["dossier.md"]}
    issues = ["dossier.md: 4 material paragraph(s) lack a local reference: claim"]

    required = engine._writer_incremental_repair_tool(
        "writer-citation-repair", "writer", step, issues,
    )
    nudge = engine._writer_incremental_repair_nudge(
        "writer-citation-repair", "writer", step, issues,
    )

    assert required == "filesystem__replace_paragraph"
    assert "corrected, evidence-grounded paragraph" in nudge
    assert "valid nearby bounded local citation" in nudge


def test_missing_source_gate_requires_one_bounded_append(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-source-append")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text("existing evidence", encoding="utf-8")
    step = {"role": "architect", "required_artifacts": ["dossier.md"]}
    issues = [
        "dossier.md: uncited required source file(s): source-a.docx, source-b.pptx; "
        "cited_sources=3 is below required minimum 5",
    ]

    required = engine._writer_incremental_repair_tool(
        "writer-source-append", "architect", step, issues,
    )
    nudge = engine._writer_incremental_repair_nudge(
        "writer-source-append", "architect", step, issues,
    )

    assert required == "filesystem__append"
    assert "currently missing source exactly once" in nudge
    assert "one-based bounded locator" in nudge


def test_writer_mixed_duplicate_and_placeholder_keeps_duplicate_nudge(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-mixed-repair")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text("TODO complete this\n\nDuplicated.\n\nDuplicated.\n", encoding="utf-8")
    step = {"role": "writer", "required_artifacts": ["dossier.md"]}
    issues = [
        "dossier.md: document contains 23 duplicate paragraph occurrence(s)",
        "dossier.md: document contains 1 placeholder marker(s); paragraph prefix: TODO complete this",
    ]

    required = engine._writer_incremental_repair_tool(
        "writer-mixed-repair", "writer", step, issues,
    )
    nudge = engine._writer_incremental_repair_nudge(
        "writer-mixed-repair", "writer", step, issues,
    )

    assert required == "filesystem__replace_paragraph"
    assert "occurrence=2" in nudge
    assert "placeholder" not in nudge.casefold()


def test_writer_placeholder_gate_requires_one_targeted_paragraph_repair(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-placeholder-repair")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text("TODO complete this section\n", encoding="utf-8")
    step = {"role": "writer", "required_artifacts": ["dossier.md"]}
    issues = [
        "dossier.md: document contains 1 placeholder marker(s); "
        "paragraph prefix: TODO complete this section",
    ]

    required = engine._writer_incremental_repair_tool(
        "writer-placeholder-repair", "writer", step, issues,
    )
    nudge = engine._writer_incremental_repair_nudge(
        "writer-placeholder-repair", "writer", step, issues,
    )

    assert required == "filesystem__replace_paragraph"
    assert "exactly one valid filesystem__replace_paragraph" in nudge
    assert "never rewrite or delete the whole document" in nudge
    assert "first clean, complete section" not in nudge


def test_writer_code_wrapped_citations_require_append_not_global_write(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-code-citations")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text("Evidence: `[source.md > blocks 1-2]`\n", encoding="utf-8")
    step = {"role": "writer", "required_artifacts": ["dossier.md"]}
    issues = [
        "dossier.md: 2 citation-like pattern(s) inside Markdown code do not count as evidence; "
        "write actual citations without backticks or code fences",
    ]

    required = engine._writer_incremental_repair_tool(
        "writer-code-citations", "writer", step, issues,
    )
    nudge = engine._writer_incremental_repair_nudge(
        "writer-code-citations", "writer", step, issues,
    )

    assert required == "filesystem__append"
    assert "exactly one valid filesystem__append" in nudge
    assert "first clean, complete section" not in nudge


def test_missing_source_gate_precedes_code_example_rewrite(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-source-code-example")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text("existing evidence", encoding="utf-8")
    step = {"role": "architect", "required_artifacts": ["dossier.md"]}
    issues = [
        "dossier.md: uncited required source file(s): source-a.docx; "
        "2 citation-like pattern(s) inside Markdown code do not count as evidence; "
        "write actual citations without backticks or code fences",
        "dossier.md: cited_sources=3 is below required minimum 4",
    ]

    required = engine._writer_incremental_repair_tool(
        "writer-source-code-example", "architect", step, issues,
    )

    assert required == "filesystem__append"


def test_heading_number_restart_requires_bounded_heading_repair(tmp_path):
    engine, state = _engine(tmp_path)
    state.get_execution("writer-heading-restart")
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    (project / "dossier.md").write_text(
        "# Dossier\n\n## 1. Base\n\nTexte.\n\n## 4. Appended\n\nSuite.\n",
        encoding="utf-8",
    )
    step = {"role": "writer", "required_artifacts": ["dossier.md"]}
    issues = [
        "dossier.md: document contains 1 heading numbering restart(s), suggesting "
        "an appended duplicate section series: ## 4. Appended (number 4 after 9)",
    ]

    required = engine._writer_incremental_repair_tool(
        "writer-heading-restart", "writer", step, issues,
    )
    nudge = engine._writer_incremental_repair_nudge(
        "writer-heading-restart", "writer", step, issues,
    )

    assert required == "filesystem__replace_paragraph"
    assert "## 4. Appended" not in nudge  # The gate supplies the exact selector.
    assert "restarted Markdown heading selector" in nudge
    assert "occurrence=1" in nudge
    assert "empty string" in nudge


def test_upstream_reopen_requires_current_artifact_reads_before_completion(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("refresh-dependent-artifacts")
    execution.variables["role_key"] = "architect"
    execution.variables["delegated_step"] = {
        "retry_context": (
            "A completed upstream artifact was reopened by stronger deterministic "
            "quality gates. Reuse its corrected result."
        ),
        "refresh_required_artifacts": ["analysis/decisions.md", "analysis/inventory.md"],
    }
    execution.current_plan = {"steps": [{
        "id": 0, "role": "architect", "specialist": "Decision analyst",
        "required_artifacts": ["analysis/decisions.md"],
    }]}
    project = tmp_path / "projects" / "proj-default" / "analysis"
    project.mkdir(parents=True)
    (project / "decisions.md").write_text("# Decisions\n", encoding="utf-8")
    (project / "inventory.md").write_text("# Inventory\n", encoding="utf-8")
    response = json.dumps({
        "summary": "refreshed", "artifacts": ["analysis/decisions.md"],
        "evidence": [], "risks": [], "next_action": "",
    })

    issues = engine._step_completion_issues(
        "refresh-dependent-artifacts", execution.current_plan["steps"][0], response,
    )
    assert issues[-2:] == [
        "reread refreshed artifact before accepting dependent conclusions: analysis/decisions.md",
        "reread refreshed artifact before accepting dependent conclusions: analysis/inventory.md",
    ]
    tool, nudge = engine._quality_repair_directive(
        "refresh-dependent-artifacts", "architect",
        execution.current_plan["steps"][0], issues,
    )
    assert tool == "filesystem__read"
    assert "analysis/decisions.md" in nudge

    conversation = state.get_conversation("refresh-dependent-artifacts")
    for index, path in enumerate(("analysis/decisions.md", "analysis/inventory.md"), 1):
        call_id = f"read-{index}"
        conversation.messages.extend([
            {
                "role": "assistant", "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {
                        "name": "filesystem__read",
                        "arguments": {"path": path, "offset": 0, "limit": 12000},
                    },
                }],
            },
            {"role": "tool", "tool_call_id": call_id, "content": "current content"},
        ])

    refreshed_issues = engine._step_completion_issues(
        "refresh-dependent-artifacts", execution.current_plan["steps"][0], response,
    )
    assert not any(item.startswith("reread refreshed artifact") for item in refreshed_issues)


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


def test_bootstrap_runtime_honors_first_run_tls_environment(tmp_path, monkeypatch):
    """The documented .env TLS controls must configure a new workspace."""
    import json

    from main import bootstrap_runtime

    monkeypatch.setenv("SSL_VERIFY", "False")
    monkeypatch.setenv("SSL_CERT_PATH", "internal-ca.pem")

    _, engine, _, _ = bootstrap_runtime(str(tmp_path))

    assert engine.llm_provider.ssl_verify is False
    assert engine.llm_provider.ssl_cert_path == "internal-ca.pem"
    persisted = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert persisted["ssl_verify"] is False
    assert persisted["ssl_cert_path"] == "internal-ca.pem"


def test_bootstrap_runtime_honors_first_run_openai_endpoint(tmp_path, monkeypatch):
    """Documented OPENAI_* endpoint env vars must configure a new workspace."""
    import json

    from main import bootstrap_runtime

    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "site-qwen")

    _, engine, _, _ = bootstrap_runtime(str(tmp_path))

    assert engine.llm_provider.base_url == "http://127.0.0.1:9/v1"
    assert engine.llm_provider.default_model == "site-qwen"
    persisted = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert persisted["base_url"] == "http://127.0.0.1:9/v1"
    assert persisted["model_name"] == "site-qwen"
    bootstrap = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert 'os.getenv("OPENAI_BASE_URL")' in bootstrap
    assert 'os.getenv("SSL_VERIFY")' in bootstrap


def test_bootstrap_runtime_keeps_persisted_endpoint_over_environment(tmp_path, monkeypatch):
    """A saved GUI endpoint remains authoritative after a restart."""
    import json

    from main import bootstrap_runtime

    (tmp_path / "config.json").write_text(
        json.dumps({
            "base_url": "https://gpu01.quartz.moss/general/v1",
            "model_name": "Qwen/Qwen3.6-35B",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_MODEL_NAME", "env-model")

    _, engine, _, _ = bootstrap_runtime(str(tmp_path))

    assert engine.llm_provider.base_url == "https://gpu01.quartz.moss/general/v1"
    assert engine.llm_provider.default_model == "Qwen/Qwen3.6-35B"


def test_bootstrap_runtime_quarantines_unreadable_config(tmp_path, monkeypatch):
    import json

    from main import bootstrap_runtime

    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    (tmp_path / "config.json").write_text("{not-json", encoding="utf-8")

    _, engine, _, _ = bootstrap_runtime(str(tmp_path))

    sidecars = list(tmp_path.glob("config.json.corrupt-*"))
    assert sidecars
    assert sidecars[0].read_text(encoding="utf-8") == "{not-json"
    persisted = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert persisted["base_url"] == "http://127.0.0.1:9/v1"
    assert engine.llm_provider.base_url == "http://127.0.0.1:9/v1"


def test_bootstrap_runtime_keeps_persisted_tls_settings_over_environment(
    tmp_path, monkeypatch,
):
    """A saved GUI choice remains authoritative after a restart."""
    import json

    from main import bootstrap_runtime

    (tmp_path / "config.json").write_text(
        json.dumps({"ssl_verify": True, "ssl_cert_path": ""}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SSL_VERIFY", "False")
    monkeypatch.setenv("SSL_CERT_PATH", "environment-ca.pem")

    _, engine, _, _ = bootstrap_runtime(str(tmp_path))

    assert engine.llm_provider.ssl_verify is True
    assert engine.llm_provider.ssl_cert_path == ""


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
