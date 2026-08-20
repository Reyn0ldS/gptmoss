"""Freeze and verify the complete source inventory shipped in Git archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "release-source-manifest.json"
TOP_LEVEL_FILES = {
    ".coveragerc", ".env.template", ".gitattributes", ".gitignore",
    "LICENSE", "README.md", "SKILLS.md", "DELIVERY_ASSURANCE.md",
    "Manuel_utilisation.md", "config.json.template", "pytest.ini",
    "constraints-runtime.txt", "install.bat", "install.sh", "main.py",
    "offline-runtime-manifest.json", "prepare-offline-source.bat",
    "pyproject.toml", "requirements-dev.txt", "requirements-runtime.txt",
    "requirements.txt", "start.bat", "start.sh",
}
SOURCE_ROOTS = (".github", "benchmarks", "docs", "gptmoss", "scripts", "tests")
IGNORED_PARTS = {"__pycache__", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_bytes(content: bytes) -> bytes:
    """Normalize ordinary text while preserving binary content exactly."""
    if b"\0" in content:
        return content
    return content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def canonical_source_bytes(path: Path) -> bytes:
    """Return stable bytes for a source file across Git/Windows exports.

    Git normalizes text line endings to LF when ``core.autocrlf`` is enabled.
    Git for Windows may materialize them as CRLF again in an archive.  Hashing
    canonical text makes both representations verifiable.  Binary files remain
    byte-for-byte unchanged.
    """
    return canonical_bytes(path.read_bytes())


def source_files(root: Path = ROOT) -> list[Path]:
    files = [root / name for name in TOP_LEVEL_FILES if (root / name).is_file()]
    for directory in SOURCE_ROOTS:
        base = root / directory
        if not base.is_dir():
            continue
        files.extend(
            path for path in base.rglob("*")
            if path.is_file()
            and not any(part in IGNORED_PARTS for part in path.parts)
            and path.suffix not in IGNORED_SUFFIXES
        )
    return sorted(set(files))


def build_manifest(root: Path = ROOT) -> dict:
    entries = {}
    for path in source_files(root):
        relative = path.relative_to(root).as_posix()
        entries[relative] = sha256_bytes(canonical_source_bytes(path))
    return {"schema_version": 1, "files": entries}


def check_manifest(root: Path = ROOT) -> list[str]:
    path = root / MANIFEST.name
    if not path.is_file():
        return ["release-source-manifest.json is absent"]
    expected = build_manifest(root)
    actual = json.loads(path.read_text(encoding="utf-8"))
    return [] if actual == expected else [
        "release-source-manifest.json is stale; run python scripts/verify_source_release.py --write"
    ]


def check_archive(archive_path: Path, root: Path = ROOT) -> list[str]:
    manifest = json.loads((root / MANIFEST.name).read_text(encoding="utf-8"))
    expected = dict(manifest.get("files") or {})
    expected[MANIFEST.name] = sha256_bytes(
        canonical_source_bytes(root / MANIFEST.name)
    )
    errors = []
    with zipfile.ZipFile(archive_path) as archive:
        names = {name.rstrip("/") for name in archive.namelist() if not name.endswith("/")}
        prefixes = {name.split("/", 1)[0] for name in names if "/" in name}
        prefix = next(iter(prefixes)) + "/" if len(prefixes) == 1 else ""
        normalized = {name[len(prefix):] if prefix and name.startswith(prefix) else name: name for name in names}
        for relative, digest in expected.items():
            member = normalized.get(relative)
            if member is None:
                errors.append(f"archive is missing {relative}")
            elif sha256_bytes(canonical_bytes(archive.read(member))) != digest:
                errors.append(f"archive hash differs for {relative}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--archive", type=Path)
    arguments = parser.parse_args()
    if arguments.write:
        MANIFEST.write_text(
            json.dumps(build_manifest(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"[PASS] Wrote source release inventory to {MANIFEST.name}.")
        return 0
    errors = check_manifest()
    if not errors and arguments.archive:
        errors.extend(check_archive(arguments.archive.resolve()))
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1
    print("[PASS] Source release inventory and archive are complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
