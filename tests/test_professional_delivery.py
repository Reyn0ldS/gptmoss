from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

from gptmoss.core.delivery_package import build_delivery_package
from gptmoss.core.documents import parse_document
from gptmoss.core.professional_delivery import apply_professional_profile


def test_professional_profile_enforces_quality_and_attachment_inventory(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("# Décision\n\nLa conservation est limitée à trente jours.", encoding="utf-8")

    class Store:
        def document(self, artifact_id):
            assert artifact_id == "a1"
            return parse_document(source)

    plan = {
        "delivery_profile": "professional-local", "primary_artifact": "report.md",
        "steps": [{"required_artifacts": ["report.md", "analysis/inventory.md"]}],
        "artifact_validations": [{
            "path": "analysis/inventory.md", "validator": "document", "required": True,
            "constraints": {
                "source_inventory": {"legacy-basename.txt": {"blocks": 99}},
                "required_source_files": ["legacy-basename.txt"],
                "require_local_references": True,
            },
        }],
    }
    apply_professional_profile(plan, Store(), ["a1"])
    policies = {item["path"]: item for item in plan["artifact_validations"]}
    constraints = policies["report.md"]["constraints"]
    assert constraints["minimums"]["words"] == 600
    assert constraints["source_inventory"] == {"source.txt": {"blocks": 2}}
    assert constraints["require_claim_references"] is True
    assert constraints["require_bounded_references"] is True
    supporting = policies["analysis/inventory.md"]["constraints"]
    assert supporting["source_inventory"] == {"source.txt": {"blocks": 2}}
    assert supporting["required_source_files"] == ["source.txt"]


def test_delivery_package_contains_docx_manifest_assurance_and_sources(tmp_path):
    markdown = tmp_path / "report.md"
    markdown.write_text("# Rapport final\n\nContenu validé et prêt à livrer.\n", encoding="utf-8")
    plan = {
        "delivery_profile": "professional-local",
        "professional_profile": {"primary_artifact": "report.md"},
        "artifact_validations": [{"path": "report.md", "required": True}],
    }
    package = build_delivery_package(tmp_path, "exec-1", plan, {"passed": True})
    assert package and package["archive_sha256"]
    rendered = parse_document(Path(package["docx_path"]))
    assert rendered.title == "Rapport final"
    assert "Contenu validé" in rendered.text
    archive = Path(package["archive_path"])
    with ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        assert {"artifacts/report.md", "report.docx", "manifest.json", "delivery-assurance.json"} <= names
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["assurance_passed"] is True
        with ZipFile(bundle.open("report.docx")) as docx:
            assert "word/document.xml" in docx.namelist()
