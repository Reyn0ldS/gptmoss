from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

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
    assert constraints["validate_arithmetic"] is True
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


def test_professional_profile_derives_long_form_and_diagram_gates_from_requirements():
    requirements = [
        {
            "id": "REQ-001", "mandatory": True,
            "statement": "Produire un dossier professionnel autonome de 35 à 45 pages.",
        },
        {
            "id": "REQ-002", "mandatory": True,
            "statement": (
                "Inclure une synthèse exécutive, une matrice de traçabilité, un registre "
                "des risques, une feuille de route 30/60/90 jours, un plan de tests et "
                "des critères d’acceptation."
            ),
        },
        {
            "id": "REQ-003", "mandatory": True,
            "statement": "Produire au moins trois diagrammes utiles sans preuve Internet.",
        },
    ]
    plan = {
        "delivery_profile": "professional-local",
        "primary_artifact": "dossier.md",
        "requirements": requirements,
        "steps": [{
            "role": "writer", "required_artifacts": ["dossier.md"],
            "requirement_ids": [],
        }],
        "artifact_validations": [],
    }

    apply_professional_profile(plan)

    constraints = plan["artifact_validations"][0]["constraints"]
    assert constraints["minimums"]["words"] == 8750
    assert constraints["minimums"]["valid_diagrams"] == 3
    assert constraints["reject_invalid_diagrams"] is True
    assert constraints["forbid_external_links"] is True
    assert constraints["required_requirement_ids"] == ["REQ-001", "REQ-002", "REQ-003"]
    assert constraints["required_traceability_ids"] == ["REQ-001", "REQ-002", "REQ-003"]
    assert "Synthèse exécutive" in constraints["required_headings"]
    assert "Plan de tests" in constraints["required_headings"]
    assert plan["steps"][0]["requirement_ids"] == ["REQ-001", "REQ-002", "REQ-003"]


def test_professional_profile_enforces_semantic_decisions_and_clean_support_diagrams():
    plan = {
        "delivery_profile": "professional-local",
        "primary_artifact": "dossier.md",
        "requirements": [{
            "id": "REQ-001", "mandatory": True,
            "statement": "Produire un dossier avec au moins trois diagrammes.",
        }],
        "steps": [
            {
                "role": "architect",
                "specialist": "Local Corpus Evidence Analyst",
                "description": "Inventory sources and search every decision topic.",
                "required_artifacts": ["analysis/corpus-inventory.md"],
            },
            {
                "role": "architect",
                "specialist": "Architecture Decision Analyst",
                "description": "Record decisions and alternatives.",
                "required_artifacts": ["analysis/decision-register.md"],
            },
            {
                "role": "architect",
                "specialist": "Application, Integration & Data Architect",
                "description": "Design architecture diagrams.",
                "required_artifacts": ["analysis/application-data-architecture.md"],
            },
            {
                "role": "writer", "specialist": "Professional Editor",
                "required_artifacts": ["dossier.md"], "requirement_ids": [],
            },
        ],
        "artifact_validations": [],
    }

    apply_professional_profile(plan)
    policies = {item["path"]: item["constraints"] for item in plan["artifact_validations"]}
    decisions = policies["analysis/decision-register.md"]
    application = policies["analysis/application-data-architecture.md"]
    inventory = policies["analysis/corpus-inventory.md"]

    assert decisions["record_section_policy"]["minimum_records"] == 1
    assert "owner" in decisions["record_section_policy"]["required_fields"]
    assert decisions["max_duplicate_headings"] == 0
    assert decisions["max_duplicate_list_items"] == 0
    assert "record_section_policy" not in inventory
    assert application["reject_invalid_diagrams"] is True
    assert application["minimums"]["valid_diagrams"] == 2

    for constraints in policies.values():
        assert constraints["max_duplicate_paragraphs"] == 0
        assert constraints["max_duplicate_list_items"] == 0
        assert constraints["max_duplicate_headings"] == 0
        assert constraints["reject_heading_number_restarts"] is True

    # A persisted policy generated by the former description-based matcher is
    # removed when the profile is reapplied, without retaining the false gate.
    inventory["record_section_policy"] = dict(decisions["record_section_policy"])
    apply_professional_profile(plan)
    policies = {item["path"]: item["constraints"] for item in plan["artifact_validations"]}
    assert "record_section_policy" not in policies["analysis/corpus-inventory.md"]


def test_delivery_package_rejects_missing_required_embedded_diagrams(tmp_path):
    (tmp_path / "report.md").write_text("# Rapport\n\nTexte sans diagramme.\n", encoding="utf-8")
    plan = {
        "delivery_profile": "professional-local",
        "professional_profile": {
            "primary_artifact": "report.md", "minimum_valid_diagrams": 1,
        },
        "artifact_validations": [{"path": "report.md", "required": True}],
    }

    with pytest.raises(ValueError, match="embeds 0 diagram"):
        build_delivery_package(tmp_path, "exec-diagram", plan, {"passed": True})
