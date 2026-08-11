import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import pytest

from gptmoss.api.server import app, init_app
from gptmoss.capabilities.filesystem import FilesystemCapability
from gptmoss.capabilities.shell import ShellCapability
from gptmoss.core import ArtifactStore, ContextEngine, EventBus, ExecutionEngine, RuntimeKernel, StateEngine
from gptmoss.interfaces.llm import LLMProvider
from gptmoss.memory import RAMMemoryProvider
from gptmoss.planners import SimplePlanner
from gptmoss.policies import SimplePolicyProvider


PLAN = {
    "steps": [
        {"id": 0, "role": "architect", "description": "Architect: write calculator specifications.", "dependencies": []},
        {"id": 1, "role": "security", "description": "Security Reviewer: review calculator specifications.", "dependencies": [0]},
        {"id": 2, "role": "developer", "description": "Developer: implement the calculator.", "dependencies": [0, 1]},
        {"id": 3, "role": "qa", "description": "QA: create and execute calculator tests.", "dependencies": [2]},
        {"id": 4, "role": "debugger", "description": "Debugger: inspect QA evidence and fix failures only if needed.", "dependencies": [3]},
        {"id": 5, "role": "writer", "description": "Technical Writer: document the calculator.", "dependencies": [2]},
        {"id": 6, "role": "coordinator", "description": "Final Summary: synthesize every validated delivery.", "dependencies": [4, 5]},
    ],
    "rationale": "QA and documentation run independently after implementation.",
}


class WorkflowLLMProvider(LLMProvider):
    """Scripted provider exercising planning, tools, handoffs and synthesis."""

    ROLE_MARKERS = {
        "architect": "Specialized Architect Agent",
        "security": "Specialized Security & Compliance Reviewer",
        "developer": "Specialized Developer/Coder Agent",
        "qa": "Specialized QA Testing Engineer",
        "debugger": "Specialized Debugger & Bug Fixer",
        "writer": "Specialized Technical Writer",
    }
    FILES = {
        "architect": ("specs.md", "# Calculator\n\nImplement `add(left, right)` with the Python standard library.\n"),
        "security": ("security_review.md", "# Security review\n\nNo untrusted evaluation and no external dependency.\n"),
        "developer": ("calculator.py", "def add(left, right):\n    return left + right\n"),
        "writer": ("README.md", "# Calculator\n\nRun `python -m pytest -q` to verify the project.\n"),
    }

    def __init__(self):
        self.api_key = "test-key"
        self.base_url = "http://127.0.0.1:9999/v1"
        self.default_model = "deterministic-workflow"
        self.supports_vision = False
        self.stages = defaultdict(int)
        self.role_calls = Counter()
        self.tool_calls = Counter()
        self.prompts = defaultdict(list)

    @staticmethod
    def _text(messages: List[Dict[str, Any]]) -> str:
        return "\n".join(
            str(message.get("content") or "")
            for message in messages
            if not isinstance(message.get("content"), list)
        )

    def _role(self, messages: List[Dict[str, Any]]) -> str:
        text = self._text(messages)
        for role, marker in self.ROLE_MARKERS.items():
            if marker in text:
                return role
        return "coordinator"

    @staticmethod
    def _response(content=None, tool_calls=None):
        return {
            "content": content,
            "tool_calls": tool_calls,
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    async def completion(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        text = self._text(messages)
        if "precise JSON planning coordinator" in text:
            return self._response(json.dumps(PLAN))

        role = self._role(messages)
        stage = self.stages[role]
        self.stages[role] += 1
        self.role_calls[role] += 1
        self.prompts[role].append(text)

        if role in self.FILES and stage == 0:
            path, content = self.FILES[role]
            return self._tool(role, stage, "filesystem__write", {"path": path, "content": content})
        if role == "qa" and stage == 0:
            content = (
                "from calculator import add\n\n"
                "def test_addition():\n"
                "    assert add(2, 3) == 5\n"
                "    assert add(-2, 2) == 0\n"
            )
            return self._tool(role, stage, "filesystem__write", {"path": "tests/test_calculator.py", "content": content})
        if role == "qa" and stage == 1:
            return self._tool(role, stage, "shell__execute", {"command": "python -B -m pytest -q"})
        if role == "debugger" and stage == 0:
            # QA already ran the suite. Reading its test is useful; rerunning the
            # same passing command would be duplicate work.
            return self._tool(role, stage, "filesystem__read", {"path": "tests/test_calculator.py"})
        if role == "coordinator":
            return self._response(
                "Project completed once by Architect, Security, Developer, QA, Debugger and Writer; all deliveries merged."
            )

        artifacts = [self.FILES[role][0]] if role in self.FILES else []
        if role == "qa":
            assert "EXIT_CODE: 0" in text
            artifacts = ["tests/test_calculator.py"]
        evidence = ["pytest EXIT_CODE: 0"] if role in ("qa", "debugger") else [f"{role} delivery validated"]
        delivery = {
            "summary": f"{role} completed its assigned work exactly once.",
            "artifacts": artifacts,
            "evidence": evidence,
            "risks": [],
            "next_action": "Use this output in dependent steps.",
        }
        return self._response(json.dumps(delivery))

    def _tool(self, role: str, stage: int, name: str, arguments: Dict[str, Any]):
        call_id = f"call-{role}-{stage}"
        self.tool_calls[call_id] += 1
        return self._response(tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }])

    async def embeddings(self, texts: List[str], **kwargs) -> List[List[float]]:
        return [[0.1] for _ in texts]

    async def tokenize(self, text: str, **kwargs) -> List[int]:
        return list(text.encode())

    async def models(self) -> List[str]:
        return [self.default_model]


async def wait_for_terminal_state(state_engine: StateEngine, execution_id: str, timeout: float = 15.0):
    async with asyncio.timeout(timeout):
        while state_engine.get_execution(execution_id).status not in ("completed", "failed", "cancelled"):
            await asyncio.sleep(0.05)
    return state_engine.get_execution(execution_id)


@pytest.mark.asyncio
async def test_complete_project_workflow_assigns_once_and_aggregates(tmp_path):
    event_bus = EventBus()
    events = []
    event_bus.subscribe_all(events.append)
    state_engine = StateEngine()
    provider = WorkflowLLMProvider()
    context = ContextEngine(state_engine, RAMMemoryProvider())
    filesystem = FilesystemCapability(str(tmp_path), state_engine)
    shell = ShellCapability(str(tmp_path), state_engine, safe_mode=True, timeout_seconds=30)
    engine = ExecutionEngine(
        event_bus=event_bus,
        state_engine=state_engine,
        context_engine=context,
        llm_provider=provider,
        planner=SimplePlanner(provider),
        policy_provider=SimplePolicyProvider(approval_required_capabilities=["never"]),
        artifact_store=ArtifactStore(str(tmp_path)),
    )
    engine.register_capability("filesystem", filesystem)
    engine.register_capability("shell", shell)
    kernel = RuntimeKernel(event_bus, state_engine, engine)
    init_app(kernel, engine, state_engine, event_bus)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        created = await client.post("/projects", json={"id": "proj-e2e", "name": "Calculator E2E"})
        assert created.status_code == 201
        assert (tmp_path / "projects" / "proj-e2e").is_dir()

        submitted = await client.post("/executions", json={
            "task": "Create a tested calculator project",
            "project_id": "proj-e2e",
        })
        assert submitted.status_code == 201
        execution_id = submitted.json()["execution_id"]

        # Simulate duplicate scheduling caused by rapid UI/reconnect/resume events.
        await asyncio.gather(
            engine.execute_task(execution_id, "Create a tested calculator project"),
            engine.execute_task(execution_id, "Create a tested calculator project"),
        )
        state = await wait_for_terminal_state(state_engine, execution_id)
        details = (await client.get(f"/executions/{execution_id}")).json()

    assert state.status == "completed", state.results
    project = tmp_path / "projects" / "proj-e2e"
    assert (project / "calculator.py").read_text(encoding="utf-8").count("def add") == 1
    assert (project / "tests" / "test_calculator.py").is_file()
    assert (project / "README.md").is_file()

    children = [
        child for child in state_engine.executions.values()
        if child.variables.get("parent_execution_id") == execution_id
    ]
    assert len(children) == 6
    assert Counter(child.variables["role_key"] for child in children) == Counter({
        "architect": 1, "security": 1, "developer": 1,
        "qa": 1, "debugger": 1, "writer": 1,
    })
    assert len({child.variables["plan_step_id"] for child in children}) == 6
    assert all(count == 1 for count in provider.tool_calls.values())

    steps = details["plan"]["steps"]
    assert all(step["status"] == "completed" for step in steps)
    assert all(step.get("assigned_execution_id") for step in steps[:6])
    assert "assigned_execution_id" not in steps[6]
    assert len(details["results"]["steps"]) == 7
    assert len(details["results"]["deliveries"]) == 7
    assert "all deliveries merged" in details["results"]["final_output"]
    assert details["results"]["telemetry"]["counts"]["duplicate_execution_skipped"] >= 1

    assert "specs.md" in provider.prompts["security"][0]
    assert "security_review.md" in provider.prompts["developer"][0]
    assert "pytest EXIT_CODE: 0" in provider.prompts["debugger"][0]
    coordinator_prompt = provider.prompts["coordinator"][0]
    for role in ("architect", "security", "developer", "qa", "debugger", "writer"):
        assert f"{role} completed its assigned work exactly once" in coordinator_prompt

    # The observable event stream is a causal audit trail, not merely a list of
    # successful outputs. Dependencies must complete before their consumers
    # start, and every delegated result must return before its parent step closes.
    assert [event.timestamp for event in events] == sorted(event.timestamp for event in events)
    root_events = [event for event in events if event.payload.get("execution_id") == execution_id]
    root_types = [event.type for event in root_events]
    assert root_types.index("TaskCreated") < root_types.index("ExecutionStarted")
    assert root_types.index("ExecutionStarted") < root_types.index("ContextBuilt")
    assert root_types.index("ContextBuilt") < root_types.index("PlanGenerated")

    root_started = {
        event.payload["step_index"]: index for index, event in enumerate(events)
        if event.type == "StepStarted" and event.payload.get("execution_id") == execution_id
    }
    root_completed = {
        event.payload["step_index"]: index for index, event in enumerate(events)
        if event.type == "StepCompleted" and event.payload.get("execution_id") == execution_id
    }
    assert set(root_started) == set(root_completed) == set(range(len(PLAN["steps"])))
    for step_index, step in enumerate(PLAN["steps"]):
        assert root_started[step_index] < root_completed[step_index]
        for dependency in step["dependencies"]:
            assert root_completed[dependency] < root_started[step_index]

    assurance_index = next(index for index, event in enumerate(events)
                           if event.type == "DeliveryAssuranceCompleted"
                           and event.payload.get("execution_id") == execution_id)
    completed_index = next(index for index, event in enumerate(events)
                           if event.type == "ExecutionCompleted"
                           and event.payload.get("execution_id") == execution_id)
    assert max(root_completed.values()) < assurance_index < completed_index

    for child in children:
        step_index = next(
            index for index, step in enumerate(PLAN["steps"])
            if step["id"] == child.variables["plan_step_id"]
        )
        child_created = next(index for index, event in enumerate(events)
                             if event.type == "TaskCreated"
                             and event.payload.get("execution_id") == child.execution_id)
        child_completed = next(index for index, event in enumerate(events)
                               if event.type == "ExecutionCompleted"
                               and event.payload.get("execution_id") == child.execution_id)
        assert root_started[step_index] < child_created < child_completed < root_completed[step_index]


def test_gui_has_no_online_boot_dependency_and_initializes_attachments_early():
    gui = (Path(__file__).parents[1] / "gptmoss" / "api" / "gui.html").read_text(encoding="utf-8")
    assert '<script src="http' not in gui
    assert "fonts.googleapis.com" not in gui
    assert "fonts.gstatic.com" not in gui
    assert "const markdownParser = window.marked ||" in gui
    assert gui.index("const selectedLibraryArtifacts") < gui.index('document.addEventListener("DOMContentLoaded"')
    assert 'id="task-submit-btn"' in gui
    assert 'requestApi("/executions"' in gui
