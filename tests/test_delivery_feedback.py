from gptmoss.core.delivery_feedback import (
    classify_assurance_report,
    classify_issue_texts,
    disjoint_owned_paths,
    select_reopen_step,
    steps_to_reopen,
)
from gptmoss.core.plan_obligations import (
    AUTONOMOUS_REPAIR,
    DOCUMENT_RENDER,
    IMPLEMENTATION,
    SOURCE_INVENTORY,
)


def _plan():
    return {
        "steps": [
            {
                "id": 0, "role": "architect", "operation": "inventory",
                "satisfies_obligations": [SOURCE_INVENTORY],
                "specialist": "Corpus analyst",
            },
            {
                "id": 1, "role": "writer", "operation": "document_render",
                "satisfies_obligations": [DOCUMENT_RENDER],
                "specialist": "Writer",
            },
            {
                "id": 2, "role": "debugger", "operation": "repair",
                "specialist": "Repair",
            },
            {
                "id": 3, "role": "coordinator", "operation": "audit",
                "specialist": "Auditor",
            },
        ]
    }


def test_classify_coverage_issues_target_inventory():
    target = classify_issue_texts(["read every normalized block of source.md"])
    assert target.obligation == SOURCE_INVENTORY
    assert select_reopen_step(_plan(), target)["id"] == 0


def test_classify_duplicate_paragraph_targets_writer_paragraph_tool():
    target = classify_issue_texts(["duplicate paragraph occurrence(s)"])
    assert target.obligation == DOCUMENT_RENDER
    assert target.required_tool == "filesystem__replace_paragraph"
    assert select_reopen_step(_plan(), target)["role"] == "writer"


def test_classify_heading_number_restart_targets_bounded_heading_removal():
    target = classify_issue_texts([
        "document contains 1 heading numbering restart(s), suggesting an appended "
        "duplicate section series: ## 4. Architecture (number 4 after 9)",
    ])
    assert target.obligation == DOCUMENT_RENDER
    assert target.required_tool == "filesystem__replace_paragraph"


def test_classify_code_wrapped_citations_authorizes_document_rewrite():
    target = classify_issue_texts([
        "48 citation-like pattern(s) inside Markdown code do not count as evidence; "
        "write actual citations without backticks or code fences",
    ])
    assert target.obligation == DOCUMENT_RENDER
    assert target.required_tool == "filesystem__write"
    assert select_reopen_step(_plan(), target)["role"] == "writer"


def test_classify_missing_source_coverage_requires_append_not_ungated_edits():
    target = classify_issue_texts([
        "uncited required source file(s): source-a.docx, source-b.pptx; "
        "cited_sources=3 is below required minimum 5",
    ])
    assert target.obligation == DOCUMENT_RENDER
    assert target.required_tool == "filesystem__append"


def test_classify_missing_source_precedes_code_example_rewrite():
    target = classify_issue_texts([
        "uncited required source file(s): source-a.docx; "
        "2 citation-like pattern(s) inside Markdown code do not count as evidence; "
        "write actual citations without backticks or code fences",
    ])
    assert target.obligation == DOCUMENT_RENDER
    assert target.required_tool == "filesystem__append"


def test_classify_semantically_incomplete_records_requires_section_repair():
    target = classify_issue_texts([
        "4 record section(s) violate the declared semantic schema",
    ])
    assert target.obligation == DOCUMENT_RENDER
    assert target.required_tool == "filesystem__replace_section"


def test_classify_invalid_diagram_requires_section_repair():
    target = classify_issue_texts([
        "document contains 1 invalid diagram(s): line 20 under section selector "
        "'### Runtime flow': self-loop is not allowed",
    ])
    assert target.obligation == DOCUMENT_RENDER
    assert target.required_tool == "filesystem__replace_section"


def test_classify_cli_smoke_keeps_debugger_fallback():
    target = classify_assurance_report({
        "passed": False, "checks": [], "failures": ["CLI smoke failed"],
    })
    assert target.role == "debugger"
    assert select_reopen_step(_plan(), target)["id"] == 2


def test_corpus_policy_check_reopens_inventory_not_debugger():
    target = classify_assurance_report({
        "passed": False,
        "checks": [{"name": "corpus_policy_evidence", "passed": False}],
        "failures": ["corpus policy lacks machine evidence: no documents.read evidence"],
    })
    assert target.obligation == SOURCE_INVENTORY
    reopened = steps_to_reopen(_plan(), target, select_reopen_step(_plan(), target))
    roles = {step["role"] for step in reopened}
    assert "architect" in roles
    assert "debugger" not in roles


def test_software_check_names_reopen_debugger():
    target = classify_assurance_report({
        "passed": False,
        "checks": [{"name": "syntax_imports_signatures", "passed": False}],
        "failures": ["1 static integration issue(s)"],
    })
    assert target.obligation == AUTONOMOUS_REPAIR
    assert select_reopen_step(_plan(), target)["role"] == "debugger"


def test_software_smoke_does_not_reopen_decorated_developer():
    plan = {
        "steps": [
            {
                "id": 0, "role": "developer", "operation": "implement",
                "satisfies_obligations": [IMPLEMENTATION],
            },
            {
                "id": 1, "role": "debugger", "operation": "repair",
                "satisfies_obligations": [AUTONOMOUS_REPAIR],
            },
            {"id": 2, "role": "coordinator", "operation": "audit"},
        ]
    }
    target = classify_issue_texts(["CLI smoke failed"])
    assert select_reopen_step(plan, target)["id"] == 1


def test_disjoint_owned_paths_require_both_claims():
    assert disjoint_owned_paths(["analysis/a.md"], ["tests/test_a.py"])
    assert not disjoint_owned_paths(["docs/a.md"], ["docs/a.md"])
    assert not disjoint_owned_paths([], ["tests/test_a.py"])
