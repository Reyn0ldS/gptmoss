import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_documentation_describes_current_local_document_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    skills = (ROOT / "SKILLS.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "local-document-workflow.md").read_text(encoding="utf-8")

    combined = "\n".join((readme, skills, guide))
    for value in (
        "DOCX",
        "PPTX",
        "TXT",
        "HTML local",
        "document-analysis",
        "GET /artifacts/search",
        "documents.read_chunk",
        "scripts/validate_document.py",
        "required_traceability_ids",
        "require_bounded_references",
        "offline-runtime-manifest.json",
    ):
        assert value in combined

    assert "PDF et DOCX ne sont pas pris en charge actuellement" not in readme
    assert "Uploads are limited to 10 MiB" not in skills
    assert "PDF and DOCX extraction deliberately remains" not in skills
    assert "ne consulte pas les liens" in guide
    assert "aucune dépendance Python" in guide


def test_document_workflow_does_not_invalidate_offline_runtime_manifest():
    manifest = json.loads(
        (ROOT / "offline-runtime-manifest.json").read_text(encoding="utf-8")
    )
    requirements = ROOT / manifest["requirements_file"]
    normalized_requirements = (
        requirements.read_text(encoding="utf-8")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .encode("utf-8")
    )
    digest = hashlib.sha256(normalized_requirements).hexdigest()

    assert manifest["requirements_hash_mode"] == "utf-8-lf"
    assert manifest["requirements_sha256"] == digest
    assert "python-docx" not in requirements.read_text(encoding="utf-8").lower()
    assert "python-pptx" not in requirements.read_text(encoding="utf-8").lower()
