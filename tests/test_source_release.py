import json
import zipfile
from pathlib import Path

from scripts.verify_source_release import (
    MANIFEST,
    build_manifest,
    canonical_source_bytes,
    check_archive,
    check_manifest,
)


def test_source_release_manifest_covers_code_gui_skills_scripts_and_docs():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    files = set(manifest["files"])

    assert manifest == build_manifest(root)
    assert not check_manifest(root)
    assert "gptmoss/api/gui.html" in files
    assert "gptmoss/core/execution.py" in files
    assert "gptmoss/skills/document-analysis/SKILL.md" in files
    assert "scripts/prepare_offline_source.py" in files
    assert "prepare-offline-source.bat" in files
    assert "docs/application-map.json" in files


def test_source_hashes_are_stable_across_windows_and_git_line_endings(tmp_path):
    windows = tmp_path / "windows.txt"
    git_blob = tmp_path / "git.txt"
    windows.write_bytes(b"first\r\nsecond\r\n")
    git_blob.write_bytes(b"first\nsecond\n")

    assert canonical_source_bytes(windows) == canonical_source_bytes(git_blob)


def test_source_manifest_validates_git_canonical_archive_bytes(tmp_path):
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    archive_path = tmp_path / "release.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for index, relative in enumerate(manifest["files"]):
            content = canonical_source_bytes(root / relative)
            if index == 0:
                content = content.replace(b"\n", b"\r\n")
            archive.writestr(
                f"gptmoss/{relative}",
                content,
            )
        archive.writestr(
            f"gptmoss/{MANIFEST.name}",
            canonical_source_bytes(MANIFEST),
        )

    assert not check_archive(archive_path, root)
