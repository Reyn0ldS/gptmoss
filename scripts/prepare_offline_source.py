import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from configure_embedded_python import configure_runtime


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_REQUIREMENTS = PROJECT_ROOT / "requirements-runtime.txt"
DEFAULT_VERSION = "3.13.14"
KNOWN_SHA256 = {
    "3.13.14": "90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907",
}
REQUIRED_IMPORTS = "fastapi, httpx, openai, pydantic, pytest, uvicorn, websockets"


@dataclass(frozen=True)
class RuntimeSpec:
    version: str
    sha256: str
    source_url: str

    @property
    def major_minor(self) -> str:
        return ".".join(self.version.split(".")[:2])

    @property
    def abi(self) -> str:
        return "cp" + "".join(self.version.split(".")[:2])

    @property
    def directory_name(self) -> str:
        return f"python-{self.version}-embed-amd64"


def runtime_spec(version: str, sha256: str | None, source_url: str | None) -> RuntimeSpec:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Python version must use the form major.minor.patch.")
    expected_hash = (sha256 or KNOWN_SHA256.get(version, "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ValueError("A valid --sha256 is required for this Python version.")
    url = source_url or (
        f"https://www.python.org/ftp/python/{version}/"
        f"python-{version}-embed-amd64.zip"
    )
    return RuntimeSpec(version=version, sha256=expected_hash, source_url=url)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_normalized_text_file(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def download_runtime(spec: RuntimeSpec, archive_path: Path) -> None:
    print(f"Downloading {spec.source_url}")
    request = urllib.request.Request(spec.source_url, headers={"User-Agent": "GPTMOSS offline builder"})
    with urllib.request.urlopen(request, timeout=120) as response, archive_path.open("wb") as output:
        shutil.copyfileobj(response, output)
    actual_hash = sha256_file(archive_path)
    if actual_hash != spec.sha256:
        raise RuntimeError(
            f"Python archive checksum mismatch: expected {spec.sha256}, got {actual_hash}."
        )


def extract_verified_archive(archive_path: Path, destination: Path) -> None:
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            member_path = (destination / member.filename).resolve()
            try:
                member_path.relative_to(destination_root)
            except ValueError as error:
                raise RuntimeError(f"Unsafe path in Python archive: {member.filename}") from error
        archive.extractall(destination)


def install_target_dependencies(spec: RuntimeSpec, site_packages: Path) -> None:
    subprocess.run([sys.executable, "-m", "pip", "--version"], check=True)
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--only-binary=:all:",
        "--platform",
        "win_amd64",
        "--python-version",
        spec.major_minor,
        "--implementation",
        "cp",
        "--abi",
        spec.abi,
        "--target",
        str(site_packages),
        "--requirement",
        str(RUNTIME_REQUIREMENTS),
    ]
    print(f"Resolving wheels for CPython {spec.major_minor} on Windows amd64...")
    subprocess.run(command, check=True)


def validate_runtime(runtime_directory: Path, spec: RuntimeSpec) -> None:
    executable = runtime_directory / "python.exe"
    command = (
        "import json, platform, sys; "
        f"import {REQUIRED_IMPORTS}; "
        "print(json.dumps({'version': list(sys.version_info[:3]), "
        "'machine': platform.machine().lower()}))"
    )
    result = subprocess.run(
        [str(executable), "-B", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )
    information = json.loads(result.stdout)
    if information["version"] != [int(part) for part in spec.version.split(".")]:
        raise RuntimeError(f"Prepared runtime reported an unexpected version: {information['version']}")
    if information["machine"] not in {"amd64", "x86_64"}:
        raise RuntimeError(f"Prepared runtime reported an unexpected architecture: {information['machine']}")


def purge_caches(runtime_directory: Path) -> None:
    cache_directories = sorted(
        runtime_directory.rglob("__pycache__"), key=lambda path: len(path.parts), reverse=True
    )
    for cache_directory in cache_directories:
        shutil.rmtree(cache_directory)
    for compiled_file in runtime_directory.rglob("*.pyc"):
        compiled_file.unlink()


def package_versions(site_packages: Path) -> dict[str, str]:
    distributions = importlib.metadata.distributions(path=[str(site_packages)])
    return dict(
        sorted(
            (distribution.metadata["Name"], distribution.version)
            for distribution in distributions
            if distribution.metadata["Name"]
        )
    )


def replace_runtime(staged_runtime: Path, destination: Path) -> None:
    if destination.parent != PROJECT_ROOT or not destination.name.startswith("python-"):
        raise RuntimeError(f"Refusing to replace unexpected destination: {destination}")
    backup = PROJECT_ROOT / f".{destination.name}.backup"
    if backup.exists():
        raise RuntimeError(f"Remove or recover the existing backup first: {backup}")

    if destination.exists():
        destination.rename(backup)
    try:
        staged_runtime.rename(destination)
    except BaseException:
        if backup.exists() and not destination.exists():
            backup.rename(destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def write_manifest(spec: RuntimeSpec, runtime_directory: Path) -> None:
    site_packages = runtime_directory / "Lib" / "site-packages"
    requirements = RUNTIME_REQUIREMENTS
    files = [path for path in runtime_directory.rglob("*") if path.is_file()]
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "python_version": spec.version,
        "platform": "win_amd64",
        "source_url": spec.source_url,
        "source_sha256": spec.sha256,
        "requirements_sha256": sha256_normalized_text_file(requirements),
        "requirements_hash_mode": "utf-8-lf",
        "requirements_file": requirements.name,
        "packages": package_versions(site_packages),
        "runtime_directory": runtime_directory.name,
        "runtime_file_count": len(files),
        "runtime_size_bytes": sum(path.stat().st_size for path in files),
    }
    manifest_path = PROJECT_ROOT / "offline-runtime-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def prepare(spec: RuntimeSpec) -> Path:
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("The autonomous package must be prepared on 64-bit Windows.")

    destination = PROJECT_ROOT / spec.directory_name
    with tempfile.TemporaryDirectory(prefix=".gptmoss-offline-build-", dir=PROJECT_ROOT) as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / "python-embed.zip"
        staged_runtime = temporary_root / spec.directory_name
        staged_runtime.mkdir()

        download_runtime(spec, archive_path)
        extract_verified_archive(archive_path, staged_runtime)
        configure_runtime(staged_runtime)
        install_target_dependencies(spec, staged_runtime / "Lib" / "site-packages")
        validate_runtime(staged_runtime, spec)
        purge_caches(staged_runtime)
        replace_runtime(staged_runtime, destination)

    write_manifest(spec, destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a complete autonomous GPTMOSS source directory for offline Windows use."
    )
    parser.add_argument("--python-version", default=DEFAULT_VERSION)
    parser.add_argument("--sha256")
    parser.add_argument("--source-url")
    arguments = parser.parse_args()

    spec = runtime_spec(arguments.python_version, arguments.sha256, arguments.source_url)
    runtime_directory = prepare(spec)
    print("")
    print(f"Autonomous offline package ready: {runtime_directory}")
    print("Commit the generated runtime and offline-runtime-manifest.json to distribute it with Git.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
