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

    assert len(plan["steps"]) == 12
    assert plan["steps"][-1]["role"] == "coordinator"
    assert any(step["role"] == "debugger" for step in plan["steps"])
    assert not any(step["role"] == "developer" for step in plan["steps"])
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


def test_small_software_readme_request_keeps_software_fallback():
    task = "Build a Python API and create README.md with setup instructions."
    plan = SimplePlanner._fallback_plan(task, analyze_task_complexity(task))

    assert any(step["role"] == "developer" for step in plan["steps"])
    assert not plan.get("artifact_validations")
