"""Keep the GPTMOSS web server controllable while its child process is stopped."""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


SERVICE_NAME = "gptmoss-supervisor"
DEFAULT_CONTROL_HOST = "127.0.0.1"
DEFAULT_CONTROL_PORT = 8765
DEFAULT_APP_HOST = "127.0.0.1"
DEFAULT_APP_PORT = 8000


def option_value(arguments: list[str], name: str, default: str) -> str:
    for index, value in enumerate(arguments):
        if value == name and index + 1 < len(arguments):
            return arguments[index + 1]
        if value.startswith(name + "="):
            return value.split("=", 1)[1]
    return default


def replace_option(arguments: list[str], name: str, value: str) -> list[str]:
    updated: list[str] = []
    replaced = False
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item == name:
            updated.extend((name, value))
            replaced = True
            index += 2 if index + 1 < len(arguments) else 1
            continue
        if item.startswith(name + "="):
            updated.extend((name, value))
            replaced = True
            index += 1
            continue
        updated.append(item)
        index += 1
    if not replaced:
        updated.extend((name, value))
    return updated


def valid_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Port must be an integer.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return port


def is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin_is_local(origin: str) -> bool:
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and is_loopback_host(
        parsed.hostname
    )


class RuntimeController:
    def __init__(
        self,
        python_executable: Path,
        main_script: Path,
        project_root: Path,
        app_arguments: list[str],
        *,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        health_probe: Callable[[str, int], bool] | None = None,
    ) -> None:
        self.python_executable = python_executable.resolve()
        self.main_script = main_script.resolve()
        self.project_root = project_root.resolve()
        self.app_arguments = list(app_arguments)
        self.host = option_value(self.app_arguments, "--host", DEFAULT_APP_HOST)
        self.port = valid_port(option_value(self.app_arguments, "--port", str(DEFAULT_APP_PORT)))
        self.control_url = ""
        self.control_token = ""
        self.process: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.last_exit_code: int | None = None
        self.last_error = ""
        self._popen = popen_factory
        self._health_probe = health_probe or self._default_health_probe
        self._lock = threading.RLock()

    def configure_control(self, url: str, token: str) -> None:
        self.control_url = url.rstrip("/")
        self.control_token = token

    def _default_health_probe(self, host: str, port: int) -> bool:
        probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
        try:
            with urllib.request.urlopen(
                f"http://{probe_host}:{port}/health", timeout=0.35
            ) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def _port_is_available(self) -> bool:
        bind_host = self.host
        if bind_host == "localhost":
            bind_host = "127.0.0.1"
        family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as candidate:
                candidate.bind((bind_host, self.port))
            return True
        except OSError:
            return False

    def _poll_process(self) -> int | None:
        if self.process is None:
            return self.last_exit_code
        return_code = self.process.poll()
        if return_code is not None:
            self.last_exit_code = return_code
            self.process = None
            if return_code and not self.last_error:
                self.last_error = f"Server exited with code {return_code}."
        return return_code

    def _command(self) -> list[str]:
        arguments = replace_option(self.app_arguments, "--host", self.host)
        arguments = replace_option(arguments, "--port", str(self.port))
        return [str(self.python_executable), "-B", str(self.main_script), *arguments]

    def start(self, port: int | None = None) -> dict[str, Any]:
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return self.status()
            if port is not None:
                self.port = valid_port(port)
            self.last_exit_code = None
            self.last_error = ""
            if not self._port_is_available():
                self.last_error = f"Port {self.port} is already in use by another process."
                return self.status()
            environment = os.environ.copy()
            environment.update(
                {
                    "GPTMOSS_SUPERVISOR_URL": self.control_url,
                    "GPTMOSS_SUPERVISOR_TOKEN": self.control_token,
                    "GPTMOSS_SUPERVISOR_MANAGED": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            kwargs: dict[str, Any] = {"cwd": str(self.project_root), "env": environment}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            try:
                self.process = self._popen(self._command(), **kwargs)
            except OSError as exc:
                self.last_error = f"Unable to start server: {exc}"
                self.process = None
                return self.status()
            self.started_at = time.time()
            return self.status()

    def _terminate(self, process: subprocess.Popen) -> None:
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
            process.wait(timeout=10)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        process.kill()
        process.wait(timeout=5)

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self.process
            if process is not None and process.poll() is None:
                self._terminate(process)
                self.last_exit_code = process.poll()
            self.process = None
            self.started_at = None
            self.last_error = ""
            return self.status()

    def restart(self, port: int | None = None) -> dict[str, Any]:
        with self._lock:
            self.stop()
            return self.start(port=port)

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._poll_process()
            alive = self.process is not None
            ready = alive and self._health_probe(self.host, self.port)
            if alive:
                state = "running" if ready else "starting"
            elif self.last_error:
                state = "error"
            else:
                state = "stopped"
            return {
                "service": SERVICE_NAME,
                "state": state,
                "ready": ready,
                "pid": self.process.pid if self.process is not None else None,
                "host": self.host,
                "port": self.port,
                "app_url": f"http://{self.host}:{self.port}",
                "control_url": self.control_url,
                "started_at": self.started_at,
                "last_exit_code": self.last_exit_code,
                "error": self.last_error,
            }


class ControlHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def control_page(token: str) -> str:
    encoded_token = json.dumps(token)
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>GPTMOSS Server Control</title><style>
body{{font-family:system-ui;background:#111827;color:#e5e7eb;max-width:620px;margin:40px auto;padding:20px}}
.card{{background:#1f2937;border:1px solid #374151;border-radius:12px;padding:20px}}button,input{{padding:9px 12px;margin:4px;border-radius:7px;border:1px solid #4b5563;background:#111827;color:#fff}}button{{cursor:pointer}}pre{{white-space:pre-wrap;color:#a5b4fc}}a{{color:#93c5fd}}</style></head>
<body><div class="card"><h1>GPTMOSS</h1><p>Contrôle local du serveur applicatif.</p>
<p><label>Port <input id="port" type="number" min="1" max="65535" value="8000"></label></p>
<button onclick="act('start')">Démarrer</button><button onclick="act('stop')">Arrêter</button>
<button onclick="act('restart')">Redémarrer</button><button onclick="act('rebind')">Changer de port</button>
<p><a id="app" href="#">Ouvrir l'application</a></p><pre id="status">Chargement…</pre></div>
<script>const token={encoded_token};async function req(path,options={{}}){{options.headers={{...(options.headers||{{}}),'X-GPTMOSS-Control-Token':token,'Content-Type':'application/json'}};const r=await fetch('/api/'+path,options);const data=await r.json();if(!r.ok)throw new Error(data.error||('HTTP '+r.status));return data}}
async function refresh(){{try{{const s=await req('status');document.getElementById('status').textContent=JSON.stringify(s,null,2);document.getElementById('port').value=s.port;const a=document.getElementById('app');a.href=s.app_url;a.textContent='Ouvrir '+s.app_url}}catch(e){{document.getElementById('status').textContent=e.message}}}}
async function act(name){{try{{const body=(name==='rebind'||name==='start'||name==='restart')?JSON.stringify({{port:Number(document.getElementById('port').value)}}):'{{}}';await req(name,{{method:'POST',body}});await refresh()}}catch(e){{alert(e.message)}}}}setInterval(refresh,1500);refresh();</script></body></html>"""


def make_handler(controller: RuntimeController, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "GPTMOSSSupervisor/1"

        def _cors_origin(self) -> str:
            origin = self.headers.get("Origin", "")
            return origin if origin and origin_is_local(origin) else ""

        def _headers(self, status: int, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            origin = self._cors_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-GPTMOSS-Control-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._headers(status)
            self.wfile.write(body)

        def _authorized(self) -> bool:
            origin = self.headers.get("Origin", "")
            supplied = self.headers.get("X-GPTMOSS-Control-Token", "")
            return origin_is_local(origin) and hmac.compare_digest(supplied, token)

        def _payload(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Invalid request length.") from exc
            if length > 4096:
                raise ValueError("Request body is too large.")
            if not length:
                return {}
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Request body must be valid JSON.") from exc
            if not isinstance(payload, dict):
                raise ValueError("Request body must be an object.")
            return payload

        def do_OPTIONS(self) -> None:  # noqa: N802
            if not origin_is_local(self.headers.get("Origin", "")):
                self._json(403, {"error": "Only local browser origins are allowed."})
                return
            self._headers(204)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/":
                body = control_page(token).encode("utf-8")
                self._headers(200, "text/html")
                self.wfile.write(body)
                return
            if self.path == "/api/status":
                self._json(200, controller.status())
                return
            self._json(404, {"error": "Not found."})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(403, {"error": "Invalid local control token or origin."})
                return
            action = self.path.removeprefix("/api/")
            if action not in {"start", "stop", "restart", "rebind"}:
                self._json(404, {"error": "Not found."})
                return
            try:
                payload = self._payload()
                port = valid_port(payload["port"]) if "port" in payload else None
                if port == self.server.server_port:
                    raise ValueError("Application port cannot equal the supervisor port.")
                if action == "stop":
                    status = controller.stop()
                elif action == "restart":
                    status = controller.restart(port=port)
                elif action == "rebind":
                    if port is None:
                        raise ValueError("A port is required for rebind.")
                    status = controller.restart(port=port)
                else:
                    status = controller.start(port=port)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            http_status = 409 if status["state"] == "error" else 200
            self._json(http_status, status)

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[SUPERVISOR] {self.address_string()} - {format % args}")

    return Handler


def existing_supervisor(host: str, port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=0.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if payload.get("service") == SERVICE_NAME else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def is_one_shot(arguments: list[str]) -> bool:
    return "--help" in arguments or "-h" in arguments or "--task" in arguments


def main() -> int:
    parser = argparse.ArgumentParser(description="Supervise the local GPTMOSS web server.")
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--main", required=True, type=Path)
    parser.add_argument("--control-host", default=DEFAULT_CONTROL_HOST)
    parser.add_argument("--control-port", type=valid_port, default=DEFAULT_CONTROL_PORT)
    parser.add_argument("app_arguments", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    app_arguments = list(arguments.app_arguments)
    if app_arguments and app_arguments[0] == "--":
        app_arguments.pop(0)
    if not is_loopback_host(arguments.control_host):
        parser.error("The supervisor must bind to a loopback address.")
    if is_one_shot(app_arguments):
        return subprocess.call(
            [str(arguments.python), "-B", str(arguments.main), *app_arguments],
            cwd=str(arguments.main.resolve().parent),
        )
    previous = existing_supervisor(arguments.control_host, arguments.control_port)
    if previous:
        print(f"[INFO] GPTMOSS supervisor already running: {previous['control_url']}")
        print(f"[INFO] Application state: {previous['state']} ({previous['app_url']})")
        return 0
    token = secrets.token_urlsafe(32)
    controller = RuntimeController(
        arguments.python,
        arguments.main,
        arguments.main.resolve().parent,
        app_arguments,
    )
    server = ControlHTTPServer(
        (arguments.control_host, arguments.control_port), make_handler(controller, token)
    )
    control_url = f"http://{arguments.control_host}:{server.server_port}"
    controller.configure_control(control_url, token)
    initial = controller.start()
    print(f"[INFO] Supervisor control: {control_url}")
    print(f"[INFO] Application target: {initial['app_url']}")
    if initial["state"] == "error":
        print(f"[WARNING] {initial['error']}")
        print(f"[INFO] Open {control_url} to choose another port.")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n[INFO] Stopping GPTMOSS supervisor...")
    finally:
        server.shutdown()
        server.server_close()
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
