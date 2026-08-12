"""Live GUI smoke: serve the real page, call the GUI contracts, dump Edge DOM."""

from __future__ import annotations

import socket
import subprocess
import tempfile
import threading
import time

import httpx
import pytest
import uvicorn

from gptmoss.api.server import app, init_app
from gptmoss.core import ContextEngine, EventBus, ExecutionEngine, RuntimeKernel, StateEngine
from gptmoss.memory import RAMMemoryProvider
from gptmoss.planners import SimplePlanner
from gptmoss.policies import SimplePolicyProvider
from scripts.browser_layout_audit import find_edge
from tests.mock_llm import MockLLMProvider
from tests.test_api import ASGIClient


def _gui_app():
    event_bus = EventBus()
    state_engine = StateEngine()
    llm = MockLLMProvider()
    llm.add_response(content="GUI smoke reply")
    engine = ExecutionEngine(
        event_bus, state_engine, ContextEngine(state_engine, RAMMemoryProvider()),
        llm, SimplePlanner(llm), SimplePolicyProvider(),
    )
    kernel = RuntimeKernel(event_bus, state_engine, engine)
    init_app(kernel, engine, state_engine, event_bus)
    return app, state_engine


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(port: int):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", use_colors=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.4)
            return server, thread
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("GUI smoke server did not become ready.")


def _dump_dom(edge, url: str, width: int, height: int) -> str:
    with tempfile.TemporaryDirectory(prefix="gptmoss-gui-") as profile:
        result = subprocess.run(
            [
                str(edge),
                "--headless=new",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                "--no-first-run",
                "--disable-extensions",
                f"--user-data-dir={profile}",
                f"--window-size={width},{height}",
                "--virtual-time-budget=4000",
                "--dump-dom",
                url,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    if result.returncode != 0:
        raise AssertionError(
            f"Edge dump-dom failed ({result.returncode}): {result.stderr[-2000:]}"
        )
    return result.stdout


def test_gui_page_and_composer_contract_match_the_new_controls():
    _gui_app()
    client = ASGIClient(app)
    page = client.get("/")
    assert page.status_code == 200
    html = page.text
    assert 'id="task-planning-mode"' in html
    assert 'value="direct"' in html
    assert 'value="short_team"' in html
    assert 'value="full_team"' in html
    assert "function submitNewTask" in html
    assert "planning_mode" in html
    assert "function appendLlmStream" in html
    assert "LLMDelta" in html

    created = client.post("/executions", json={
        "task": "Rédige un résumé court du dossier local",
        "planning_mode": "direct",
        "project_id": "proj-default",
        "attachment_ids": [],
    })
    assert created.status_code == 201
    execution_id = created.json()["execution_id"]
    listed = client.get("/executions").json()
    card = next(item for item in listed if item["execution_id"] == execution_id)
    assert card["planning_mode"] == "direct"
    assert card["task_title"].startswith("Rédige un résumé court")
    details = client.get(f"/executions/{execution_id}").json()
    assert details["variables"]["planning_mode"] == "direct"
    assert details["variables"]["task"] == "Rédige un résumé court du dossier local"


def test_live_gui_renders_planning_controls_and_task_title_in_edge():
    try:
        edge = find_edge()
    except FileNotFoundError:
        pytest.skip("Microsoft Edge is required for the live DOM dump")

    _gui_app()
    port = _free_port()
    server, thread = _serve(port)
    base = f"http://127.0.0.1:{port}"
    try:
        page = httpx.get(f"{base}/", timeout=5)
        assert page.status_code == 200
        assert 'id="task-planning-mode"' in page.text

        created = httpx.post(f"{base}/executions", json={
            "task": "Verifier le titre visible dans la barre laterale",
            "planning_mode": "short_team",
        }, timeout=5)
        assert created.status_code == 201

        desktop = _dump_dom(edge, f"{base}/", 1366, 768)
        mobile = _dump_dom(edge, f"{base}/", 360, 740)
        for dumped in (desktop, mobile):
            assert 'id="task-planning-mode"' in dumped
            assert 'value="direct"' in dumped
            assert 'value="full_team"' in dumped
            assert "Nouvelle Tâche" in dumped or "Nouvelle T&#226;che" in dumped
            assert "Verifier le titre visible" in dumped
            if "data-layout-global-overflow" in dumped:
                assert 'data-layout-global-overflow="false"' in dumped
    finally:
        server.should_exit = True
        thread.join(timeout=5)
