import argparse
import json
import platform
import subprocess
import sys
from pathlib import Path

from configure_embedded_python import configure_runtime


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_IMPORTS = "fastapi, httpx, openai, pydantic, uvicorn, websockets"


def find_runtime(selected_directory: Path | None) -> Path:
    if selected_directory is not None:
        return selected_directory.resolve(strict=True)

    candidates = [path for path in PROJECT_ROOT.glob("python-*-embed-amd64") if path.is_dir()]
    if not candidates:
        raise RuntimeError("No python-*-embed-amd64 directory was found at the project root.")
    if len(candidates) > 1:
        raise RuntimeError("Multiple embedded Python directories were found; select one explicitly.")
    return candidates[0].resolve()


def interpreter_info(executable: Path) -> dict[str, object]:
    command = (
        "import json, platform, sys; "
        "print(json.dumps({'version': list(sys.version_info[:2]), "
        "'machine': platform.machine().lower()}))"
    )
    result = subprocess.run(
        [str(executable), "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def prepare_runtime(runtime_directory: Path, allow_source_packages: bool) -> None:
    configure_runtime(runtime_directory)
    embedded_python = runtime_directory / "python.exe"
    builder_python = Path(sys.executable).resolve()

    runtime = interpreter_info(embedded_python)
    builder = {
        "version": list(sys.version_info[:2]),
        "machine": platform.machine().lower(),
    }
    if runtime["version"] != builder["version"]:
        raise RuntimeError(
            f"Builder Python {'.'.join(map(str, builder['version']))} does not match "
            f"embedded Python {'.'.join(map(str, runtime['version']))}."
        )
    if runtime["machine"] != builder["machine"]:
        raise RuntimeError(
            f"Builder architecture '{builder['machine']}' does not match "
            f"embedded architecture '{runtime['machine']}'."
        )

    site_packages = runtime_directory / "Lib" / "site-packages"
    command = [
        str(builder_python),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--target",
        str(site_packages),
        "--requirement",
        str(PROJECT_ROOT / "requirements.txt"),
    ]
    if not allow_source_packages:
        command.append("--only-binary=:all:")

    print("Installing GPTMOSS dependencies into the portable runtime...")
    subprocess.run(command, check=True)
    subprocess.run([str(embedded_python), "-c", f"import {REQUIRED_IMPORTS}"], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vendor GPTMOSS dependencies into a CPython embeddable runtime."
    )
    parser.add_argument("--python-directory", type=Path)
    parser.add_argument("--allow-source-packages", action="store_true")
    arguments = parser.parse_args()

    runtime_directory = find_runtime(arguments.python_directory)
    prepare_runtime(runtime_directory, arguments.allow_source_packages)
    print("Portable runtime prepared successfully.")
    print("Copy the complete GPTMOSS directory to the offline Windows computer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
