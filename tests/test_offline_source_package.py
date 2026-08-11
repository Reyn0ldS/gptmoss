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


def test_archive_extraction_rejects_parent_traversal(tmp_path):
    archive_path = tmp_path / "unsafe.zip"
    destination = tmp_path / "runtime"
    destination.mkdir()
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside.txt", "unsafe")

    with pytest.raises(RuntimeError, match="Unsafe path"):
        builder.extract_verified_archive(archive_path, destination)

    assert not (tmp_path / "outside.txt").exists()


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
