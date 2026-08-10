import json
import subprocess
import sys
from pathlib import Path

from gptmoss.core.artifact_validation import validate_artifact
from gptmoss.core.delivery import build_delivery_contract, evaluate_delivery
from gptmoss.core.document_quality import format_quality_report, main
from gptmoss.core.execution import normalize_plan


POLICY = {
    "required_headings": ["Executive Summary", "Security", "Traceability"],
    "required_requirement_ids": ["REQ-001", "REQ-002"],
    "required_traceability_ids": ["REQ-001", "REQ-002"],
    "required_source_files": ["requirements.docx", "vision.pptx"],
    "source_inventory": {
        "requirements.docx": {"blocks": 12},
        "vision.pptx": {"slides": 4},
    },
    "require_local_references": True,
    "require_bounded_references": True,
    "forbid_external_links": True,
    "forbid_placeholders": True,
    "max_duplicate_paragraphs": 0,
    "terminology": {"identity provider": ["identity server"]},
    "minimums": {"words": 45},
}


VALID_DOCUMENT = """# Architecture dossier

## Executive Summary

REQ-001 requires a governed local service with auditable access and a bounded operating model. [requirements.docx > Access controls > blocks 2-3]

## Security

REQ-002 is addressed by the identity provider, explicit authorization policy, immutable audit events, and tested recovery procedures. [vision.pptx > slide 3]

## Traceability

| Requirement | Architecture response | Evidence |
|---|---|---|
| REQ-001 | Governed local service | requirements.docx, blocks 2-3 |
| REQ-002 | Identity provider and audit | vision.pptx, slide 3 |
"""


def test_unconfigured_markdown_validation_remains_format_only(tmp_path):
    document = tmp_path / "notes.md"
    paragraph = "This intentionally repeated paragraph remains acceptable without a declared quality policy because generic Markdown may contain drafts."
    document.write_text(
        f"# Notes\n\nTODO: see https://example.test\n\n{paragraph}\n\n{paragraph}\n",
        encoding="utf-8",
    )

    report = validate_artifact(document)

    assert report["valid"], json.dumps(report, indent=2)
    assert report["validator"] == "document"
    assert report["metrics"]["external_links"] == 1
    assert report["metrics"]["duplicate_paragraphs"] == 1


def test_document_validator_accepts_complete_local_traceable_content(tmp_path):
    document = tmp_path / "architecture.md"
    document.write_text(VALID_DOCUMENT, encoding="utf-8")

    report = validate_artifact(document, validator="document", constraints=POLICY)

    assert report["valid"], json.dumps(report, indent=2)
    assert report["metrics"]["required_headings_covered"] == 3
    assert report["metrics"]["traceability_ids_covered"] == 2
    assert report["metrics"]["required_sources_cited"] == 2
    assert "Document quality report — PASS" in format_quality_report(report)


def test_document_validator_reports_content_and_provenance_defects(tmp_path):
    repeated = "This repeated architecture paragraph contains enough material words to be detected as duplicated content across the professional dossier."
    document = tmp_path / "defective.md"
    document.write_text(
        "# Architecture dossier\n\n## Executive Summary\n\nTODO\n\n"
        "## Security\n\n" + repeated + " [unknown.docx > blocks 99-100]\n\n"
        + repeated + "\n\nSee https://example.invalid for details.\n\n"
        "The identity server remains an unsupported legacy assertion with many additional words and no local evidence attached to this material paragraph.\n",
        encoding="utf-8",
    )
    policy = dict(POLICY)
    policy["require_claim_references"] = True
    policy["claim_min_words"] = 12

    report = validate_artifact(document, validator="document", constraints=policy)

    assert not report["valid"]
    failures = "\n".join(report["failures"])
    assert "missing required heading" in failures
    assert "empty required section" in failures
    assert "missing requirement ID" in failures
    assert "traceability" in failures
    assert "placeholder" in failures
    assert "external link" in failures
    assert "duplicate paragraph" in failures
    assert "undeclared local source" in failures
    assert "uncited required source" in failures
    assert "lack a local reference" in failures
    assert "inconsistent terminology" in failures


def test_document_validator_rejects_out_of_range_local_locator(tmp_path):
    document = tmp_path / "architecture.md"
    document.write_text(
        VALID_DOCUMENT.replace("blocks 2-3", "blocks 2-30", 1), encoding="utf-8"
    )

    report = validate_artifact(document, validator="document", constraints=POLICY)

    assert not report["valid"]
    assert any("out-of-range block 30" in failure for failure in report["failures"])


def test_document_validator_accepts_french_bounded_locator_terms(tmp_path):
    document = tmp_path / "localized.md"
    document.write_text(
        "# Sources\n\n"
        "Exigence locale. [requirements.docx > Exigences > blocs 2-3]\n\n"
        "Vision locale. [vision.pptx > diapositive 3]\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={
            "source_inventory": POLICY["source_inventory"],
            "require_local_references": True,
            "require_bounded_references": True,
        },
    )

    assert report["valid"], json.dumps(report, indent=2)


def test_document_validator_requires_union_of_full_source_inventory(tmp_path):
    document = tmp_path / "inventory.md"
    document.write_text(
        "# Inventaire\n\n"
        "Couverture partielle. [requirements.docx > Exigences > blocs 1-8]\n\n"
        "Vision complète. [vision.pptx > Présentation > diapositives 1-4]\n",
        encoding="utf-8",
    )
    constraints = {
        "source_inventory": POLICY["source_inventory"],
        "required_source_files": ["requirements.docx", "vision.pptx"],
        "require_bounded_references": True,
        "require_source_coverage": True,
    }

    rejected = validate_artifact(
        document, validator="document", constraints=constraints
    )
    document.write_text(
        document.read_text(encoding="utf-8").replace(
            "blocs 1-8", "blocs 1-12"
        ),
        encoding="utf-8",
    )
    accepted = validate_artifact(
        document, validator="document", constraints=constraints
    )

    assert not rejected["valid"]
    assert any("incomplete source coverage" in item for item in rejected["failures"])
    assert rejected["metrics"]["source_units_covered"] == 12
    assert rejected["metrics"]["source_units_total"] == 16
    assert accepted["valid"], json.dumps(accepted, indent=2)
    assert accepted["metrics"]["source_units_covered"] == 16


def test_requirement_coverage_uses_complete_identifiers(tmp_path):
    document = tmp_path / "near-match.md"
    document.write_text(
        "# Notes\n\nREQ-0010 is a different requirement.\n", encoding="utf-8"
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={"required_requirement_ids": ["REQ-001"]},
    )

    assert not report["valid"]
    assert report["metrics"]["requirement_ids_covered"] == 0


def test_document_quality_cli_writes_json_and_markdown_reports(tmp_path):
    document = tmp_path / "architecture.md"
    policy = tmp_path / "quality-policy.json"
    json_report = tmp_path / "quality-report.json"
    markdown_report = tmp_path / "quality-report.md"
    document.write_text(VALID_DOCUMENT, encoding="utf-8")
    policy.write_text(json.dumps(POLICY), encoding="utf-8")

    exit_code = main([
        str(document), "--constraints", str(policy), "--json", str(json_report),
        "--markdown", str(markdown_report),
    ])

    assert exit_code == 0
    assert json.loads(json_report.read_text(encoding="utf-8"))["valid"] is True
    assert "Document quality report — PASS" in markdown_report.read_text(encoding="utf-8")


def test_portable_source_tree_cli_runs_in_a_clean_process(tmp_path):
    document = tmp_path / "architecture.md"
    policy = tmp_path / "quality-policy.json"
    json_report = tmp_path / "quality-report.json"
    document.write_text(VALID_DOCUMENT, encoding="utf-8")
    policy.write_text(json.dumps(POLICY), encoding="utf-8")
    repository = Path(__file__).resolve().parents[1]

    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "validate_document.py"),
            str(document),
            "--constraints",
            str(policy),
            "--json",
            str(json_report),
        ],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(json_report.read_text(encoding="utf-8"))["valid"] is True


def test_delivery_gate_blocks_and_then_accepts_document_correction(tmp_path):
    plan = normalize_plan({
        "artifact_validations": [{
            "path": "architecture.md",
            "validator": "document",
            "constraints": POLICY,
        }],
        "steps": [{
            "id": 0,
            "role": "writer",
            "specialist": "Architecture writer",
            "description": "Produce the architecture dossier",
            "dependencies": [],
            "required_artifacts": ["architecture.md"],
        }],
    })
    document = tmp_path / "architecture.md"
    document.write_text("# Architecture dossier\n\nTODO\n", encoding="utf-8")
    contract = build_delivery_contract(plan, "Produce a traceable architecture dossier")

    rejected = evaluate_delivery(tmp_path, contract, plan["steps"], [])
    document.write_text(VALID_DOCUMENT, encoding="utf-8")
    accepted = evaluate_delivery(tmp_path, contract, plan["steps"], [])

    assert not rejected["passed"]
    assert accepted["passed"], json.dumps(accepted, indent=2)
