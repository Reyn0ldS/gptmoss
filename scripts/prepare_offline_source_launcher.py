"""Interactive Windows launcher for the offline runtime builder.

This helper deliberately uses only the Python standard library so the bundled
embedded runtime can run it even though that private runtime does not ship pip.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterable, TextIO


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIRECTORY.parent
BUILDER_SCRIPT = SCRIPT_DIRECTORY / "prepare_offline_source.py"
MANIFEST_PATH = PROJECT_ROOT / "offline-runtime-manifest.json"
LOG_PATH = PROJECT_ROOT / "offline-preparation.log"
REQUIRED_IMPORTS = "fastapi, httpx, openai, pydantic, pytest, uvicorn, websockets"


class TeeStream:
    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, value: str) -> int:
        for stream in self.streams:
            stream.write(value)
            stream.flush()
        return len(value)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def _candidate_commands() -> Iterable[tuple[str, list[str], str]]:
    seen: set[tuple[str, tuple[str, ...]]] = set()
    candidates = [
        (sys.executable, [], "Python courant"),
        (shutil.which("py.exe") or "", ["-3"], "Python Launcher"),
        (shutil.which("python.exe") or "", [], "Python"),
    ]
    for executable, prefix, label in candidates:
        if not executable:
            continue
        normalized = os.path.normcase(os.path.abspath(executable))
        key = (normalized, tuple(prefix))
        if key in seen or "\\microsoft\\windowsapps\\" in normalized:
            continue
        seen.add(key)
        yield executable, prefix, label


def find_complete_python() -> tuple[str, list[str], str] | None:
    probe = (
        "import pip, platform, sys; "
        "raise SystemExit(0 if sys.version_info >= (3, 10) and "
        "platform.machine().lower() in {'amd64', 'x86_64'} else 1)"
    )
    for executable, prefix, label in _candidate_commands():
        completed = subprocess.run(
            [executable, *prefix, "-c", probe],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode == 0:
            return executable, prefix, label
    return None


def verify_existing_runtime() -> bool:
    if not MANIFEST_PATH.is_file():
        print("[ERROR] Missing offline-runtime-manifest.json.")
        return False
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        runtime_directory = PROJECT_ROOT / manifest["runtime_directory"]
        embedded_python = runtime_directory / "python.exe"
        if not embedded_python.is_file():
            print(f"[ERROR] Embedded Python is missing: {embedded_python}")
            return False
        exit_code = run_streamed(
            [
                str(embedded_python),
                "-B",
                "-c",
                f"import {REQUIRED_IMPORTS}; print('[INFO] Embedded runtime imports verified.')",
            ]
        )
        if exit_code != 0:
            print("[ERROR] The embedded runtime is present but incomplete.")
            return False
        print("[SUCCESS] The autonomous runtime is already present and operational.")
        print(f"[INFO] Runtime: {runtime_directory}")
        return True
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(f"[ERROR] Unable to validate the embedded runtime: {error}")
        return False


def run_streamed(command: list[str]) -> int:
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
    return process.wait()


def run(arguments: list[str]) -> int:
    verify_only = "--verify-only" in arguments
    builder_arguments = [argument for argument in arguments if argument != "--verify-only"]
    print(f"[INFO] Project directory: {PROJECT_ROOT}")
    print(
        "[INFO] This script rebuilds Python and its dependencies; the GPTMOSS sources "
        "come from the Git clone or ZIP archive."
    )

    if verify_only:
        return 0 if verify_existing_runtime() else 1

    complete_python = find_complete_python()
    if complete_python is None:
        print("[WARNING] No complete 64-bit Python 3.10+ installation with pip was found.")
        print("[INFO] The Microsoft Store python.exe alias is not a usable Python installation.")
        if verify_existing_runtime():
            print("[INFO] No download is required. Use install.bat on the offline computer.")
            print(
                "[INFO] To force a rebuild, install a complete 64-bit Python with pip "
                "on this online computer."
            )
            return 0
        print("[ERROR] Install a complete 64-bit Python with pip, then run this file again.")
        return 1

    executable, prefix, label = complete_python
    print(f"[INFO] Using {label}: {executable} {' '.join(prefix)}".rstrip())
    print("[WARNING] Stop GPTMOSS before rebuilding so the embedded runtime is not locked.")
    exit_code = run_streamed([executable, *prefix, str(BUILDER_SCRIPT), *builder_arguments])
    if exit_code:
        print(f"[ERROR] The Python offline builder returned code {exit_code}.")
    return exit_code


def main() -> int:
    try:
        with LOG_PATH.open("w", encoding="utf-8", buffering=1) as log:
            original_stdout, original_stderr = sys.stdout, sys.stderr
            sys.stdout = TeeStream(original_stdout, log)
            sys.stderr = TeeStream(original_stderr, log)
            try:
                return run(sys.argv[1:])
            except KeyboardInterrupt:
                print("\n[ERROR] Offline preparation was cancelled.", file=sys.stderr)
                return 130
            except Exception as error:
                print(f"[ERROR] Offline preparation failed: {error}", file=sys.stderr)
                return 1
            finally:
                sys.stdout, sys.stderr = original_stdout, original_stderr
    except OSError as error:
        print(f"[ERROR] Unable to create {LOG_PATH}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
