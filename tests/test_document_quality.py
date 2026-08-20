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


def test_document_policy_counts_only_semantically_valid_diagrams(tmp_path):
    document = tmp_path / "diagrams.md"
    document.write_text(
        """# Dossier

```mermaid
graph TD
A[API] --> B[Store]
```

```mermaid
sequenceDiagram
participant U as User
U->>A: request
A-->>U: response
```

## Isolated diagram

```mermaid
graph TD
ONLY[Isolated]
```
""",
        encoding="utf-8",
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={
            "reject_invalid_diagrams": True,
            "minimums": {"valid_diagrams": 3},
        },
    )

    assert not report["valid"]
    assert report["metrics"]["diagrams"] == 3
    assert report["metrics"]["valid_diagrams"] == 2
    assert report["metrics"]["invalid_diagrams"] == 1
    assert "section selector '## Isolated diagram'" in "\n".join(report["failures"])


def test_json_validator_requires_declared_semantic_keys(tmp_path):
    report_path = tmp_path / "quality-report.json"
    report_path.write_text('{"valid": true}', encoding="utf-8")

    report = validate_artifact(
        report_path,
        validator="json",
        constraints={
            "top_level_type": "dict",
            "required_keys": ["valid", "metrics", "failures"],
        },
    )

    assert not report["valid"]
    assert report["metrics"]["required_keys_covered"] == 1
    assert "metrics, failures" in report["failures"][0]


def test_duplicate_failure_exposes_machine_actionable_paragraph_prefix(tmp_path):
    document = tmp_path / "duplicate.md"
    paragraph = (
        "This repeated architectural decision paragraph is long enough for the "
        "quality validator to identify and repair deterministically."
    )
    document.write_text(
        f"# Review\n\n{paragraph}\n\n{paragraph}\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={"max_duplicate_paragraphs": 0, "duplicate_min_words": 8},
    )

    assert not report["valid"]
    failure = next(item for item in report["failures"] if "duplicate paragraph" in item)
    assert "repeated paragraph prefix(es):" in failure
    assert "this repeated architectural decision paragraph" in failure


def test_document_policy_rejects_duplicate_headings_and_incomplete_record_sections(
    tmp_path,
):
    document = tmp_path / "decisions.md"
    document.write_text(
        """# Decision register

## Repeated

First section.

## Repeated

Second section.

### DEC-001: Storage

- **Context:** Local durability is required.
- **Decision:** Use an atomic local store.
""",
        encoding="utf-8",
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={
            "max_duplicate_headings": 0,
            "record_section_policy": {
                "heading_pattern": r"\bDEC-\d{3}\b",
                "minimum_records": 1,
                "required_fields": {
                    "context": ["context"],
                    "decision": ["decision"],
                    "alternatives": ["alternative"],
                    "risks": ["risk"],
                },
            },
        },
    )

    assert not report["valid"]
    assert report["metrics"]["duplicate_headings"] == 1
    assert report["metrics"]["record_sections"] == 1
    assert report["metrics"]["invalid_record_sections"] == 1
    failures = "\n".join(report["failures"])
    assert "duplicate heading occurrence" in failures
    assert "repeated Markdown heading selector(s): ## Repeated" in failures
    assert "record section(s) violate the declared semantic schema" in failures
    assert "'### DEC-001: Storage' missing alternatives, risks" in failures
    assert "alternatives, risks" in failures


def test_document_policy_rejects_a_second_numbered_section_series(tmp_path):
    document = tmp_path / "architecture.md"
    document.write_text(
        "# Architecture\n\n## 1. Context\n\nA.\n\n## 2. Data\n\nB.\n\n"
        "## 3. Runtime\n\nC.\n\n## 2. Interfaces bis\n\nAppended duplicate series.\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        document, validator="document",
        constraints={"reject_heading_number_restarts": True},
    )

    assert not report["valid"]
    assert report["metrics"]["heading_number_restarts"] == 1
    assert "## 2. Interfaces bis (number 2 after 3)" in "\n".join(report["failures"])


def test_document_policy_rejects_duplicate_list_items(tmp_path):
    document = tmp_path / "architecture.md"
    repeated = (
        "- **Access control**: `server_supervisor.py` requires an ephemeral token for every "
        "state-changing request. [architecture.md > Security > blocks 1-2]"
    )
    document.write_text(
        f"# Architecture\n\n{repeated}\n\n## Review\n\n{repeated}\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={"max_duplicate_list_items": 0, "duplicate_min_words": 8},
    )

    assert not report["valid"]
    assert report["metrics"]["duplicate_list_items"] == 1
    assert "- **Access control**" in "\n".join(report["failures"])
    assert "server_supervisor.py" in "\n".join(report["failures"])


def test_document_policy_allows_nested_numbering_to_restart(tmp_path):
    document = tmp_path / "architecture.md"
    document.write_text(
        "# Architecture\n\n## 1. Context\n\n### 1. Scope\n\n### 2. Actors\n\n"
        "## 2. Data\n\n### 1. Sources\n\n### 2. Flows\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        document, validator="document",
        constraints={"reject_heading_number_restarts": True},
    )

    assert report["valid"]
    assert report["metrics"]["heading_number_restarts"] == 0


def test_professional_document_rejects_incorrect_integer_sum_with_repair_prefix(tmp_path):
    document = tmp_path / "inventory.md"
    document.write_text(
        "# Coverage\n\n- **Blocks read**: 4 + 10 + 97 + 16 = **117 blocks**.\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={"validate_arithmetic": True},
    )

    assert not report["valid"]
    assert report["metrics"]["arithmetic_mismatches"] == 1
    failure = next(item for item in report["failures"] if "arithmetic sum" in item)
    assert "equals 127, not 117" in failure
    assert "paragraph prefix: - **Blocks read**" in failure


def test_professional_document_checks_normalized_total_against_source_inventory(tmp_path):
    document = tmp_path / "inventory.md"
    document.write_text(
        "# Coverage\n\n**Total**: 2 documents; 9 normalized blocks.\n\n"
        "- **Blocks read**: 4 + 6 = **10 blocks** of 9 expected.\n\n"
        "- The PPTX contains 2 slides and 6 normalized blocks; the total corpus "
        "comprises 10 normalized blocks.\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={
            "validate_arithmetic": True,
            "source_inventory": {
                "requirements.txt": {"blocks": 4},
                "roadmap.pptx": {"slides": 2, "normalized_blocks": 6},
            },
        },
    )

    assert not report["valid"]
    assert report["metrics"]["inventory_total_mismatches"] == 2
    failures = "\n".join(report["failures"])
    assert "expected 10 normalized blocks, not 9" in failures
    assert "paragraph prefix: **Total**" in failures


def test_document_validator_accepts_complete_local_traceable_content(tmp_path):
    document = tmp_path / "architecture.md"
    document.write_text(VALID_DOCUMENT, encoding="utf-8")

    report = validate_artifact(document, validator="document", constraints=POLICY)

    assert report["valid"], json.dumps(report, indent=2)
    assert report["metrics"]["required_headings_covered"] == 3
    assert report["metrics"]["traceability_ids_covered"] == 2
    assert report["metrics"]["required_sources_cited"] == 2
    assert "Document quality report — PASS" in format_quality_report(report)


def test_document_validator_accepts_tool_provenance_colon_locators(tmp_path):
    document = tmp_path / "tool-provenance.md"
    document.write_text(
        "# Evidence\n\n"
        "The normalized source is bounded. [requirements.docx > block:2]\n\n"
        "The presentation evidence is bounded. [vision.pptx > slide:3]\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={
            "required_source_files": ["requirements.docx", "vision.pptx"],
            "source_inventory": {
                "requirements.docx": {"blocks": 12},
                "vision.pptx": {"slides": 4},
            },
            "require_local_references": True,
            "require_bounded_references": True,
        },
    )

    assert report["valid"], json.dumps(report, indent=2)
    assert report["metrics"]["invalid_local_references"] == 0


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


def test_identifier_wildcards_are_not_treated_as_xxx_placeholders(tmp_path):
    document = tmp_path / "identifier-patterns.md"
    document.write_text(
        "# Identifier conventions\n\nREQ-xxx and DEC-xxx describe identifier families.\n",
        encoding="utf-8",
    )

    accepted = validate_artifact(
        document,
        validator="document",
        constraints={"forbid_placeholders": True},
    )
    document.write_text(
        document.read_text(encoding="utf-8") + "\nXXX\n",
        encoding="utf-8",
    )
    rejected = validate_artifact(
        document,
        validator="document",
        constraints={"forbid_placeholders": True},
    )

    assert accepted["valid"]
    assert accepted["metrics"]["placeholder_markers"] == 0
    assert not rejected["valid"]
    assert rejected["metrics"]["placeholder_markers"] == 1


def test_document_validator_rejects_out_of_range_local_locator(tmp_path):
    document = tmp_path / "architecture.md"
    document.write_text(
        VALID_DOCUMENT.replace("blocks 2-3", "blocks 2-30", 1), encoding="utf-8"
    )

    report = validate_artifact(document, validator="document", constraints=POLICY)

    assert not report["valid"]
    assert any(
        "invalid blocks range 2-30; expected 1-12" in failure
        for failure in report["failures"]
    )
    assert any(
        "paragraph prefix:" in failure and "requirements.docx" in failure
        for failure in report["failures"]
    )


def test_document_validator_reports_inverted_locator_range_unambiguously(tmp_path):
    document = tmp_path / "inverted.md"
    document.write_text(
        "# Sources\n\nLecture. [vision.pptx > diapositives 14-1]\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={
            "source_inventory": POLICY["source_inventory"],
            "require_bounded_references": True,
        },
    )

    assert not report["valid"]
    assert any(
        "invalid slides range 14-1; expected 1-4" in failure
        for failure in report["failures"]
    )


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


def test_document_validator_ignores_reference_syntax_inside_markdown_code(tmp_path):
    document = tmp_path / "examples.md"
    document.write_text(
        "# Sources\n\n"
        "Preuve rÃ©elle. [requirements.docx > Exigences > blocs 1-12]\n\n"
        "Syntaxe inline : `[filename > heading > blocks 1-2]`.\n\n"
        "```text\n[another-name > section > blocks 1-99]\n```\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={
            "source_inventory": {"requirements.docx": {"blocks": 12}},
            "required_source_files": ["requirements.docx"],
            "require_local_references": True,
            "require_bounded_references": True,
            "require_source_coverage": True,
        },
    )

    assert report["valid"], json.dumps(report, indent=2)
    assert report["metrics"]["local_references"] == 1


def test_document_validator_explains_that_code_only_citations_are_not_evidence(tmp_path):
    document = tmp_path / "code-only.md"
    document.write_text(
        "# Sources\n\n`[requirements.docx > Exigences > blocks 1-12]`\n",
        encoding="utf-8",
    )

    report = validate_artifact(
        document,
        validator="document",
        constraints={
            "required_source_files": ["requirements.docx"],
            "require_local_references": True,
        },
    )

    assert not report["valid"]
    assert any("write actual citations without backticks" in item for item in report["failures"])
    assert report["metrics"]["local_references"] == 0


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
    coverage_failure = next(
        item for item in rejected["failures"] if "incomplete source coverage" in item
    )
    assert "requirements.docx has uncovered required blocks: 9-12" in coverage_failure
    assert "and 4 more" not in coverage_failure
    assert "add bounded local reference(s) covering these exact ranges" in coverage_failure
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
