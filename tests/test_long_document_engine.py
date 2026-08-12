from pathlib import Path
import asyncio
import json

import httpx

from gptmoss.core.document_model import DocumentModel, DocumentModelStore, EvidenceReference
from gptmoss.core.document_planning import adapt_document_steps, estimate_document_work
from gptmoss.core.long_document_engine import LongDocumentEngine
from gptmoss.api.server import app, app_state
from gptmoss.core.state import StateEngine
from gptmoss.planners.simple import SimplePlanner, analyze_task_complexity


def test_document_checkpoint_is_atomic_and_round_trips(tmp_path: Path):
    store = DocumentModelStore(tmp_path / "state")
    model = DocumentModel("exec-1", "Dossier", str(tmp_path / "deliverable.md"))
    path = store.save(model)
    assert path.is_file()
    loaded = store.load("exec-1")
    assert loaded is not None
    assert loaded.to_dict() == model.to_dict()


def test_long_document_engine_plans_sections_records_and_resumes(tmp_path: Path):
    engine = LongDocumentEngine(tmp_path / "state")
    model = engine.create_model("exec-2", "Dossier", str(tmp_path / "deliverable.md"))
    engine.plan_sections(model, ["Synthèse", "Architecture", "Exploitation"], [EvidenceReference("spec.docx", "blocks 1-2")])
    engine.record_section(model, "SEC-001", "Un contenu source-grounded suffisamment stable.")
    resumed = engine.resume("exec-2")
    assert resumed is not None
    assert resumed.section("SEC-001").word_count == 5
    assert "## Synthèse" in resumed.assemble_markdown()
    assert "spec.docx" in resumed.sections[0].contract.evidence_refs[0].source


def test_document_work_is_adaptive_for_small_requests():
    small = "Rédige un court livrable deliverable.md depuis notes.txt."
    large = "Rédige un dossier professionnel avec 8 fichiers et diagrams, minimums words=3500. " + " ".join(f"source{i}.docx" for i in range(6))
    small_estimate = estimate_document_work(small)
    large_estimate = estimate_document_work(large, {"level": "very_high"})
    assert small_estimate.stage_budget < large_estimate.stage_budget
    analysis = analyze_task_complexity(small + " documents.inventory")
    plan = SimplePlanner._document_fallback(small, analysis)
    assert len(plan["steps"]) < 13
    assert plan["steps"][-1]["role"] == "coordinator"


def test_document_progress_api_exposes_checkpoint_state(tmp_path: Path, monkeypatch):
    state_engine = StateEngine()
    state = state_engine.get_execution("doc-api")
    checkpoint = tmp_path / "doc-api.document.json"
    checkpoint.write_text(json.dumps({
        "status": "writing",
        "revision": 2,
        "sections": [{"contract": {"section_id": "SEC-001", "heading": "Résumé", "status": "complete"}, "word_count": 120, "content": "ok"}],
    }), encoding="utf-8")
    state.variables["document_model_checkpoint"] = str(checkpoint)
    monkeypatch.setattr(app_state, "state_engine", state_engine)

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/executions/doc-api/document")

    response = asyncio.run(request())
    assert response.status_code == 200
    payload = response.json()
    assert payload["progress"] == 1.0
    assert payload["sections"][0]["heading"] == "Résumé"
