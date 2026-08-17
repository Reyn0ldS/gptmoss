import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import prepare_offline_source as builder


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def test_default_runtime_source_is_pinned_and_verified():
    spec = builder.runtime_spec(builder.DEFAULT_VERSION, None, None)

    assert spec.version == "3.13.14"
    assert spec.source_url.startswith("https://www.python.org/ftp/python/")
    assert len(spec.sha256) == 64
    assert spec.directory_name == "python-3.13.14-embed-amd64"


def test_requirements_hash_is_independent_of_checked_out_line_endings(tmp_path):
    lf_file = tmp_path / "requirements-lf.txt"
    crlf_file = tmp_path / "requirements-crlf.txt"
    lf_file.write_bytes(b"first>=1\nsecond>=2\n")
    crlf_file.write_bytes(b"first>=1\r\nsecond>=2\r\n")

    assert builder.sha256_normalized_text_file(lf_file) == builder.sha256_normalized_text_file(
        crlf_file
    )


def test_runtime_requirements_use_lf_in_git_archives():
    attributes = (PROJECT_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()

    assert "requirements-runtime.txt text eol=lf" in attributes
    assert "constraints-runtime.txt text eol=lf" in attributes


def test_archive_extraction_rejects_parent_traversal(tmp_path):
    archive_path = tmp_path / "unsafe.zip"
    destination = tmp_path / "runtime"
    destination.mkdir()
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside.txt", "unsafe")

    with pytest.raises(RuntimeError, match="Unsafe path"):
        builder.extract_verified_archive(archive_path, destination)

    assert not (tmp_path / "outside.txt").exists()


def test_dependency_install_uses_short_same_volume_temp_and_disables_bytecode(
    tmp_path, monkeypatch
):
    build_root = tmp_path / ".gptmoss-offline-build"
    site_packages = (
        build_root / "python-3.13.14-embed-amd64" / "Lib" / "site-packages"
    )
    site_packages.mkdir(parents=True)
    calls = []

    def record(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(builder.subprocess, "run", record)
    spec = builder.runtime_spec(builder.DEFAULT_VERSION, None, None)

    builder.install_target_dependencies(spec, site_packages)

    assert len(calls) == 2
    command, options = calls[1]
    assert "--no-compile" in command
    assert command[command.index("--target") + 1] == str(site_packages)
    environment = options["env"]
    pip_temp = Path(environment["TEMP"])
    assert pip_temp.parent == build_root
    assert environment["TMP"] == environment["TEMP"]
    assert environment["TMPDIR"] == environment["TEMP"]
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PIP_NO_COMPILE"] == "1"
    assert options["check"] is True
    assert not pip_temp.exists(), "pip temporary directory must be removed after install"


def test_dependency_install_removes_same_volume_temp_after_pip_failure(
    tmp_path, monkeypatch
):
    build_root = tmp_path / "build"
    site_packages = build_root / "runtime" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    recorded_temp = None
    calls = 0

    def fail_install(command, **kwargs):
        nonlocal calls, recorded_temp
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(command, 0)
        recorded_temp = Path(kwargs["env"]["TEMP"])
        assert recorded_temp.is_dir()
        raise subprocess.CalledProcessError(2, command)

    monkeypatch.setattr(builder.subprocess, "run", fail_install)
    spec = builder.runtime_spec(builder.DEFAULT_VERSION, None, None)

    with pytest.raises(subprocess.CalledProcessError):
        builder.install_target_dependencies(spec, site_packages)

    assert recorded_temp is not None
    assert not recorded_temp.exists()


def test_builder_selects_a_short_writable_parent_on_the_project_volume(
    tmp_path, monkeypatch
):
    project = tmp_path / "very" / "deep" / "project"
    project.mkdir(parents=True)
    too_long = tmp_path / ("x" * 85)
    short = tmp_path / "b"
    monkeypatch.setattr(builder, "MAX_SAFE_BUILD_ROOT_CHARS", len(str(short.resolve())) + 2)

    selected = builder.same_volume_build_parent(
        project,
        candidates=[too_long, short],
    )

    assert selected == short.resolve()
    assert selected.anchor == project.resolve().anchor
    assert not too_long.exists()
    selected.rmdir()


def test_builder_fails_with_actionable_diagnostic_without_short_parent(
    tmp_path, monkeypatch
):
    project = tmp_path / "project"
    project.mkdir()
    denied = tmp_path / "denied"

    def reject_mkdir(self, *args, **kwargs):
        raise PermissionError("denied by test")

    monkeypatch.setattr(Path, "mkdir", reject_mkdir)
    with pytest.raises(RuntimeError, match="Enable Windows long paths"):
        builder.same_volume_build_parent(project, candidates=[denied])


def test_committed_runtime_matches_manifest():
    manifest = json.loads((PROJECT_ROOT / "offline-runtime-manifest.json").read_text(encoding="utf-8"))
    runtime = PROJECT_ROOT / manifest["runtime_directory"]
    files = [
        path for path in runtime.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
    ]

    assert runtime.is_dir()
    assert (runtime / "python.exe").is_file()
    requirements = PROJECT_ROOT / manifest["requirements_file"]
    assert manifest["requirements_hash_mode"] == "utf-8-lf"
    assert manifest["requirements_sha256"] == builder.sha256_normalized_text_file(requirements)
    constraints = PROJECT_ROOT / manifest["constraints_file"]
    assert manifest["constraints_hash_mode"] == "utf-8-lf"
    assert manifest["constraints_sha256"] == builder.sha256_normalized_text_file(constraints)
    assert manifest["runtime_file_count"] == len(files)
    assert manifest["runtime_size_bytes"] == sum(path.stat().st_size for path in files)
    assert max(path.stat().st_size for path in files) < 100 * 1024 * 1024

    path_file = next(runtime.glob("python*._pth"))
    path_lines = path_file.read_text(encoding="ascii").splitlines()
    assert "Lib" in path_lines
    assert r"Lib\site-packages" in path_lines
    assert "import site" in path_lines


@pytest.mark.skipif(os.name != "nt", reason="Windows embedded runtime")
def test_committed_runtime_imports_all_dependencies():
    manifest = json.loads((PROJECT_ROOT / "offline-runtime-manifest.json").read_text(encoding="utf-8"))
    python = PROJECT_ROOT / manifest["runtime_directory"] / "python.exe"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    subprocess.run(
        [str(python), "-B", "-c", f"import {builder.REQUIRED_IMPORTS}"],
        check=True,
        env=environment,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows embedded runtime")
def test_committed_runtime_runs_document_validator_entry_point():
    manifest = json.loads(
        (PROJECT_ROOT / "offline-runtime-manifest.json").read_text(encoding="utf-8")
    )
    python = PROJECT_ROOT / manifest["runtime_directory"] / "python.exe"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [
            str(python),
            "-B",
            str(PROJECT_ROOT / "scripts" / "validate_document.py"),
            "--help",
        ],
        cwd=PROJECT_ROOT,
        check=False,
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Validate a local Markdown or text deliverable" in completed.stdout
