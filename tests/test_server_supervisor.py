import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from server_supervisor import (  # noqa: E402
    ControlHTTPServer,
    RuntimeController,
    is_one_shot,
    make_handler,
    option_value,
    origin_is_local,
    replace_option,
    valid_port,
)


class FakeProcess:
    _next_pid = 4100

    def __init__(self, command, **kwargs):
        type(self)._next_pid += 1
        self.pid = type(self)._next_pid
        self.command = command
        self.kwargs = kwargs
        self.returncode = None
        self.signals = []

    def poll(self):
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)
        self.returncode = 0

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def make_controller(tmp_path):
    python = tmp_path / "python.exe"
    main = tmp_path / "main.py"
    python.touch()
    main.write_text("", encoding="utf-8")
    processes = []

    def spawn(command, **kwargs):
        process = FakeProcess(command, **kwargs)
        processes.append(process)
        return process

    controller = RuntimeController(
        python,
        main,
        tmp_path,
        ["--workspace", "workspace", "--port", "8011"],
        popen_factory=spawn,
        health_probe=lambda _host, _port: True,
    )
    controller._port_is_available = lambda: True
    controller.configure_control("http://127.0.0.1:8765", "test-token")
    return controller, processes


def test_controller_listens_on_moss_host_port_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSS_HOST", "127.0.0.1")
    monkeypatch.setenv("MOSS_PORT", "9055")
    python = tmp_path / "python.exe"
    main = tmp_path / "main.py"
    python.touch()
    main.write_text("", encoding="utf-8")
    controller = RuntimeController(
        python, main, tmp_path, ["--workspace", "workspace"],
        popen_factory=lambda *args, **kwargs: FakeProcess(args, **kwargs),
        health_probe=lambda _host, _port: True,
    )
    assert controller.host == "127.0.0.1"
    assert controller.port == 9055


def test_argument_helpers_preserve_unrelated_application_options():
    arguments = ["--workspace", "space", "--port=8000", "--host", "127.0.0.1"]

    updated = replace_option(arguments, "--port", "8123")

    assert option_value(updated, "--port", "0") == "8123"
    assert option_value(updated, "--workspace", "") == "space"
    assert valid_port("65535") == 65535
    with pytest.raises(ValueError):
        valid_port(0)
    assert is_one_shot(["--help"])
    assert is_one_shot(["--task", "inspect"])
    assert not is_one_shot(["--workspace", "space"])


def test_control_origin_is_restricted_to_the_local_machine():
    assert origin_is_local("")
    assert origin_is_local("http://127.0.0.1:8000")
    assert origin_is_local("http://localhost:9000")
    assert not origin_is_local("https://example.test")
    assert not origin_is_local("null")


def test_controller_starts_stops_and_rebinds_the_child(tmp_path):
    controller, processes = make_controller(tmp_path)

    started = controller.start()
    assert started["state"] == "running"
    assert started["port"] == 8011
    assert option_value(processes[-1].command[3:], "--port", "") == "8011"
    assert processes[-1].kwargs["env"]["GPTMOSS_SUPERVISOR_TOKEN"] == "test-token"

    rebound = controller.restart(port=8123)
    assert rebound["state"] == "running"
    assert rebound["port"] == 8123
    assert len(processes) == 2
    assert option_value(processes[-1].command[3:], "--port", "") == "8123"

    stopped = controller.stop()
    assert stopped["state"] == "stopped"
    assert stopped["pid"] is None


def test_control_http_api_requires_token_for_mutations(tmp_path):
    controller, _ = make_controller(tmp_path)
    token = "secret-control-token"
    controller.configure_control("http://127.0.0.1:0", token)
    server = ControlHTTPServer(("127.0.0.1", 0), make_handler(controller, token))
    controller.configure_control(f"http://127.0.0.1:{server.server_port}", token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=2) as response:
            status = json.loads(response.read())
        assert status["service"] == "gptmoss-supervisor"

        unauthorized = urllib.request.Request(
            base + "/api/start", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(unauthorized, timeout=2)
        assert error.value.code == 403

        authorized = urllib.request.Request(
            base + "/api/rebind", data=b'{"port": 8124}', method="POST",
            headers={"Content-Type": "application/json", "X-GPTMOSS-Control-Token": token},
        )
        with urllib.request.urlopen(authorized, timeout=2) as response:
            rebound = json.loads(response.read())
        assert rebound["state"] == "running"
        assert rebound["port"] == 8124
    finally:
        controller.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
