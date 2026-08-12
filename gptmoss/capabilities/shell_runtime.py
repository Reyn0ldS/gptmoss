"""Independent safety policy and active process registry for shell execution."""

import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Optional


class ShellSafetyPolicy:
    def __init__(self, safe_mode: bool = True, timeout_seconds: int = 60):
        self.safe_mode = bool(safe_mode)
        self.timeout_seconds = max(0, int(timeout_seconds))

    def configure(self, safe_mode: bool, timeout_seconds: int) -> None:
        self.safe_mode = bool(safe_mode)
        self.timeout_seconds = max(0, int(timeout_seconds))

    def effective_timeout(self, command: str) -> Optional[int]:
        if self.timeout_seconds:
            return self.timeout_seconds
        normalized = command.lower()
        if any(marker in normalized for marker in ("pytest", "unittest", " test", " build", "compile")):
            return 900
        if any(marker in normalized for marker in ("download", "install", "pip ", "npm ", "cargo ")):
            return 1_800
        return 120

    def blocked_reason(self, command: str) -> Optional[str]:
        if not self.safe_mode:
            return None
        normalized = command.lower().replace("\\", "/")
        broad_termination = (
            re.search(r"(?:^|[\s&|])taskkill\b[^\r\n]*(?:/im\s+|\*)", normalized)
            or re.search(r"(?:^|[\s&|])stop-process\b[^\r\n]*-name\b", normalized)
            or re.search(r"(?:^|[\s&|])(?:pkill|killall)\b", normalized)
        )
        if broad_termination:
            return (
                "Command blocked by shell safe mode because process-wide "
                "termination by name can stop the runtime or unrelated work."
            )
        destructive = (
            "rm -rf /", "del /s", "format ", "diskpart", "reg delete",
            "remove-item -recurse", "clear-disk",
        )
        power_control = (
            re.search(r"(?:^|[;&|]\s*)shutdown(?:\.exe)?\s+(?:now\b|(?:/|--?)[a-z])", normalized)
            or re.search(r"(?:^|[;&|]\s*)reboot(?:\.exe)?(?:\s|$)", normalized)
        )
        if power_control or any(pattern in normalized for pattern in destructive):
            return "Command blocked by shell safe mode because it is destructive."
        return None


class ProcessRegistry:
    def __init__(self):
        self._processes = {}
        self._lock = threading.Lock()

    def register(self, execution_id: Optional[str], process) -> None:
        if execution_id:
            with self._lock:
                self._processes.setdefault(execution_id, set()).add(process)

    def unregister(self, execution_id: Optional[str], process) -> None:
        if not execution_id:
            return
        with self._lock:
            processes = self._processes.get(execution_id)
            if processes is not None:
                processes.discard(process)
                if not processes:
                    self._processes.pop(execution_id, None)

    @staticmethod
    def terminate(process) -> None:
        if process.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                killer = subprocess.Popen(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                killer.communicate(timeout=5)
            else:
                os.killpg(os.getpgid(process.pid), 15)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def cancel(self, execution_id: str) -> None:
        with self._lock:
            processes = list(self._processes.get(execution_id, set()))
        for process in processes:
            self.terminate(process)

    def count(self, execution_id: Optional[str] = None) -> int:
        with self._lock:
            if execution_id is not None:
                return len(self._processes.get(execution_id, set()))
            return sum(len(items) for items in self._processes.values())


class ProcessRunner:
    """Launch and observe one process without owning command policy or normalization."""

    def __init__(self, registry: ProcessRegistry):
        self.registry = registry

    def run(self, command, *, use_shell: bool, cwd: str, execution_id: Optional[str],
            timeout: Optional[int], max_output_chars: int,
            cancelled: Callable[[Optional[str]], bool]) -> str:
        environment = os.environ.copy()
        environment.update({
            "PYTHONUTF8": environment.get("PYTHONUTF8", "1"),
            "PYTHONIOENCODING": environment.get("PYTHONIOENCODING", "utf-8"),
            "PYTHONUNBUFFERED": environment.get("PYTHONUNBUFFERED", "1"),
            "PAGER": environment.get("PAGER", "cat"),
            "GIT_PAGER": environment.get("GIT_PAGER", "cat"),
        })
        options = (
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            if sys.platform == "win32" else {"start_new_session": True}
        )
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stdout_file, \
                tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as stderr_file:
            process = subprocess.Popen(
                command, shell=use_shell, cwd=cwd, stdout=stdout_file,
                stderr=stderr_file, text=True, encoding="utf-8", errors="replace",
                env=environment, **options,
            )
            self.registry.register(execution_id, process)
            deadline = time.monotonic() + timeout if timeout else None
            try:
                while True:
                    if cancelled(execution_id):
                        self.registry.terminate(process)
                        process.wait()
                        return "Error: Command execution cancelled."
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        self.registry.terminate(process)
                        process.wait()
                        return f"Error: Command execution timed out ({timeout}s)."
                    try:
                        process.wait(
                            timeout=min(0.25, remaining) if remaining is not None else 0.25
                        )
                        break
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                self.registry.unregister(execution_id, process)

            budget = max_output_chars if max_output_chars else None
            stdout_file.seek(0)
            stdout = stdout_file.read(budget)
            stdout_truncated = bool(budget and stdout_file.read(1))
            remaining_budget = None if budget is None else max(0, budget - len(stdout))
            stderr_file.seek(0)
            stderr = stderr_file.read(remaining_budget)
            stderr_truncated = bool(budget and stderr_file.read(1))
        output = f"EXIT_CODE: {process.returncode}\n"
        if stdout:
            output += f"STDOUT:\n{stdout}\n"
        if stderr:
            output += f"STDERR:\n{stderr}\n"
        if not stdout and not stderr:
            output += "Command produced no output."
        if stdout_truncated or stderr_truncated:
            output += "\n… [output truncated by shell safety limit]"
        return output
