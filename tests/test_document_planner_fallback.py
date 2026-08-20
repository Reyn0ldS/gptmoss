from gptmoss.core.delivery import build_delivery_contract
from gptmoss.core.execution import normalize_plan
from gptmoss.planners.simple import SimplePlanner, analyze_task_complexity


DOCUMENT_TASK = """Rédige un dossier d'architecture professionnel depuis les pièces jointes locales
requirements.docx, vision.pptx, decisions.txt et existing.html. Utilise documents.inventory
et n'utilise aucune source Internet. Traite BR-001 à BR-002, FR-001 à FR-003,
NFR-001 à NFR-002 et SEC-001 à SEC-002.

Crée les huit fichiers suivants :
1. architecture.md — livrable principal ;
2. requirements-matrix.md — exigences ;
3. evidence-matrix.md — preuves ;
4. decisions-register.md — décisions ;
5. quality-policy.json — politique ;
6. quality-report.json — rapport machine ;
7. quality-report.md — rapport lisible ;
8. review-report.md — revue indépendante.

architecture.md doit contenir des sections non vides nommées exactement :
Synthèse exécutive ; Architecture logique ; Matrice de traçabilité

Chaque affirmation doit citer un fichier local. Le plan déclare validator=document,
required=true, min_section_words=30, required_requirement_ids et
required_traceability_ids. source_inventory : requirements.docx blocks=73,
vision.pptx slides=12, decisions.txt blocks=41, existing.html blocks=36.
require_local_references=true, require_bounded_references=true,
forbid_external_links=true, forbid_placeholders=true,
max_duplicate_paragraphs=0, minimums words=3500 et local_references=30.
"""


def test_document_complexity_markers_do_not_confuse_filenames_with_other_domains():
    analysis = analyze_task_complexity(
        DOCUMENT_TASK + " Une conclusion doit porter une référence à vision.pptx."
    )

    assert "document-workflow" in analysis["domains"]
    assert "computer-vision" not in analysis["domains"]
    assert "digital-garments" not in analysis["domains"]
    assert "user-interface" not in analysis["domains"]


def test_document_fallback_preserves_outputs_roles_and_repair_gates():
    analysis = analyze_task_complexity(DOCUMENT_TASK)
    plan = normalize_plan(SimplePlanner._fallback_plan(DOCUMENT_TASK, analysis))

    assert len(plan["steps"]) == 13
    assert plan["steps"][-1]["role"] == "coordinator"
    assert any(step["role"] == "debugger" for step in plan["steps"])
    assert not any(step["role"] == "developer" for step in plan["steps"])
    assert plan["steps"][10]["role"] == "writer"
    assert plan["steps"][11]["role"] == "qa"
    assert plan["steps"][11]["required_artifacts"] == [
        "analysis/final-delivery-audit.md"
    ]
    artifacts = {
        artifact
        for step in plan["steps"]
        for artifact in step["required_artifacts"]
    }
    expected = {
        "architecture.md",
        "requirements-matrix.md",
        "evidence-matrix.md",
        "decisions-register.md",
        "quality-policy.json",
        "quality-report.json",
        "quality-report.md",
        "review-report.md",
    }
    assert expected <= artifacts
    assert "README.md" not in artifacts
    assert "tests/test_acceptance.py" not in artifacts

    contract = build_delivery_contract(plan, DOCUMENT_TASK)
    assert not contract["software_delivery"]
    assert len(contract["requirements"]) >= 8
    assert all(
        row["implementation_steps"] and row["validation_steps"]
        for row in contract["traceability"]
        if row["mandatory"]
    )


def test_document_fallback_honors_primary_filename_named_in_a_sentence():
    task = (
        "Rédige un dossier professionnel depuis le corpus local et les fichiers "
        "joints DOCX/PPTX.\n"
        "REQ-E2E-001 — Inventorier toutes les pièces.\n"
        "REQ-E2E-002 — Produire la matrice de traçabilité.\n"
        "Le livrable principal doit s'appeler dossier-architecture-gptmoss.md."
    )

    plan = SimplePlanner._fallback_plan(task, analyze_task_complexity(task))

    assert plan["primary_artifact"] == "dossier-architecture-gptmoss.md"
    assert any(
        "dossier-architecture-gptmoss.md" in step.get("required_artifacts", [])
        for step in plan["steps"]
    )
    assert {"REQ-E2E-001", "REQ-E2E-002"} <= {
        item["id"] for item in plan["requirements"]
    }


def test_document_fallback_keeps_requirement_ownership_bounded():
    plan = SimplePlanner._fallback_plan(
        DOCUMENT_TASK, analyze_task_complexity(DOCUMENT_TASK)
    )
    requirements = {item["id"]: item["statement"] for item in plan["requirements"]}
    inventory_statements = [
        requirements[requirement_id]
        for requirement_id in plan["steps"][0]["requirement_ids"]
    ]

    assert any(
        "inventorier" in statement.casefold()
        or "documents.inventory" in statement.casefold()
        for statement in inventory_statements
    )
    assert not any("quality-report" in statement for statement in inventory_statements)
    assert not any("review-report" in statement for statement in inventory_statements)
    assert not any("evidence-matrix" in statement for statement in inventory_statements)
    assert {
        requirement["id"] for requirement in plan["requirements"]
    } == set(plan["steps"][11]["requirement_ids"])


def test_document_fallback_reconstructs_explicit_quality_policy():
    plan = SimplePlanner._fallback_plan(
        DOCUMENT_TASK, analyze_task_complexity(DOCUMENT_TASK)
    )
    validation = plan["artifact_validations"][0]
    constraints = validation["constraints"]

    assert validation["path"] == "architecture.md"
    assert validation["validator"] == "document"
    assert validation["required"] is True
    assert constraints["required_headings"] == [
        "Synthèse exécutive",
        "Architecture logique",
        "Matrice de traçabilité",
    ]
    assert constraints["required_requirement_ids"] == [
        "BR-001", "BR-002", "FR-001", "FR-002", "FR-003",
        "NFR-001", "NFR-002", "SEC-001", "SEC-002",
    ]
    assert constraints["required_traceability_ids"] == constraints["required_requirement_ids"]
    assert constraints["required_source_files"] == [
        "requirements.docx", "vision.pptx", "decisions.txt", "existing.html"
    ]
    assert constraints["source_inventory"]["vision.pptx"] == {"slides": 12}
    assert constraints["source_inventory"]["requirements.docx"] == {"blocks": 73}
    assert constraints["min_section_words"] == 30
    assert constraints["max_duplicate_paragraphs"] == 0
    assert constraints["minimums"] == {"words": 3500, "local_references": 30}
    assert constraints["require_local_references"] is True
    assert constraints["require_bounded_references"] is True
    assert constraints["forbid_external_links"] is True
    assert constraints["forbid_placeholders"] is True


def test_document_fallback_validates_intermediate_and_supporting_outputs():
    plan = SimplePlanner._fallback_plan(
        DOCUMENT_TASK, analyze_task_complexity(DOCUMENT_TASK)
    )
    policies = {item["path"]: item for item in plan["artifact_validations"]}

    assert policies["analysis/corpus-inventory.md"]["validator"] == "document"
    inventory_constraints = policies["analysis/corpus-inventory.md"]["constraints"]
    assert inventory_constraints["required_source_files"] == [
        "requirements.docx", "vision.pptx", "decisions.txt", "existing.html"
    ]
    assert inventory_constraints["require_bounded_references"] is True
    assert inventory_constraints["require_source_coverage"] is True
    matrix_constraints = policies["requirements-matrix.md"]["constraints"]
    assert matrix_constraints["required_requirement_ids"] == [
        "BR-001", "BR-002", "FR-001", "FR-002", "FR-003",
        "NFR-001", "NFR-002", "SEC-001", "SEC-002",
    ]
    assert matrix_constraints["required_traceability_ids"] == matrix_constraints["required_requirement_ids"]
    assert policies["quality-policy.json"]["validator"] == "json"
    assert policies["review-report.md"]["constraints"]["forbid_external_links"] is True


def test_small_software_readme_request_keeps_software_fallback():
    task = "Build a Python API and create README.md with setup instructions."
    plan = SimplePlanner._fallback_plan(task, analyze_task_complexity(task))

    assert any(step["role"] == "developer" for step in plan["steps"])
    assert len(plan["steps"]) <= 5
    assert not plan.get("artifact_validations")


def test_simple_repair_request_does_not_inflate_to_a_full_software_team():
    task = "Corrige les tests qui échouent dans ce projet."
    analysis = analyze_task_complexity(task)
    plan = SimplePlanner._fallback_plan(task, analysis)

    assert analysis["level"] == "low"
    assert len(plan["steps"]) <= 5
    assert plan["steps"][-1]["role"] == "coordinator"
