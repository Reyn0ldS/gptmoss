from pathlib import Path
import asyncio
import json

import httpx

from gptmoss.api.server import app, app_state
from gptmoss.core.document_model import DocumentModel, DocumentModelStore, EvidenceReference
from gptmoss.core.document_planning import adapt_document_steps, estimate_document_work
from gptmoss.core.long_document_engine import LongDocumentEngine
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


def test_document_checkpoint_rejects_unsafe_ids_and_quarantines_corruption(tmp_path: Path):
    store = DocumentModelStore(tmp_path / "state")
    for unsafe in ("../outside", "a/b", "", "x" * 129):
        try:
            store.path_for(unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe execution id accepted: {unsafe!r}")
    path = store.path_for("exec-corrupt")
    path.write_text("{not-json", encoding="utf-8")
    try:
        store.load("exec-corrupt")
    except ValueError as exc:
        assert "corrupt" in str(exc)
    else:
        raise AssertionError("corrupt checkpoint was accepted")
    assert not path.exists()
    assert list(path.parent.glob(path.name + ".corrupt-*"))


def test_long_document_engine_plans_sections_records_and_resumes(tmp_path: Path):
    engine = LongDocumentEngine(tmp_path / "state")
    model = engine.create_model("exec-2", "Dossier", str(tmp_path / "deliverable.md"))
    engine.plan_sections(
        model,
        ["Synthèse", "Architecture", "Exploitation"],
        [EvidenceReference("spec.docx", "blocks 1-2")],
    )
    engine.record_section(model, "SEC-001", "Un contenu source-grounded suffisamment stable.")
    resumed = engine.resume("exec-2")
    assert resumed is not None
    assert resumed.section("SEC-001").word_count == 5
    assert "## Synthèse" in resumed.assemble_markdown()
    assert "spec.docx" in resumed.sections[0].contract.evidence_refs[0].source


def test_long_document_contract_uses_brief_targets_and_owns_every_requirement(tmp_path: Path):
    engine = LongDocumentEngine(tmp_path / "state")
    requirements = [{"id": "REQ-001"}, {"id": "REQ-002", "section": "Architecture"}]
    model = engine.create_model(
        "exec-contract",
        "Rédige un dossier; section_words=720",
        str(tmp_path / "deliverable.md"),
        requirements,
    )
    engine.plan_sections(model, ["Synthèse", "Architecture"])
    assert {item.contract.target_words for item in model.sections} == {720}
    ownership = [
        requirement_id
        for item in model.sections
        for requirement_id in item.contract.requirement_ids
    ]
    assert sorted(ownership) == ["REQ-001", "REQ-002"]
    assert len(ownership) == len(set(ownership))
    before = model.updated_at
    engine.consolidate(model)
    assert model.status == "writing"
    assert model.revision >= 2
    assert model.updated_at >= before


def test_document_work_is_adaptive_for_small_requests():
    small = "Rédige un court livrable deliverable.md depuis notes.txt."
    large = (
        "Rédige un dossier professionnel avec 8 fichiers et diagrams, minimums words=3500. "
        + " ".join(f"source{i}.docx" for i in range(6))
    )
    small_estimate = estimate_document_work(small)
    large_estimate = estimate_document_work(large, {"level": "very_high"})
    assert small_estimate.source_count == 1
    assert small_estimate.output_count == 1
    generic = estimate_document_work("Crée architecture.md à partir de spec.docx")
    assert generic.source_count == 1
    assert generic.output_count == 1
    assert small_estimate.stage_budget < large_estimate.stage_budget
    analysis = analyze_task_complexity(small + " documents.inventory")
    plan = SimplePlanner._document_fallback(small, analysis)
    assert len(plan["steps"]) < 13
    assert plan["steps"][-1]["role"] == "coordinator"
    assert plan["analysis"]["document_work_estimate"]["stage_budget"] == len(plan["steps"])
    assert all(
        not step["dependencies"] or max(step["dependencies"]) < step["id"]
        for step in plan["steps"]
    )


def test_document_progress_api_exposes_only_workspace_checkpoint(tmp_path: Path, monkeypatch):
    state_engine = StateEngine()
    state = state_engine.get_execution("doc-api")
    engine = LongDocumentEngine(tmp_path / ".gptmoss" / "document-state")
    model = engine.create_model("doc-api", "Dossier", str(tmp_path / "deliverable.md"))
    engine.plan_sections(model, ["Résumé"])
    engine.record_section(model, "SEC-001", "contenu")
    state.variables["document_model_checkpoint"] = ".gptmoss/document-state/doc-api.document.json"
    monkeypatch.setattr(app_state, "state_engine", state_engine)

    class FakeFilesystem:
        def _get_workspace_for_execution(self, execution_id):
            assert execution_id == "doc-api"
            return str(tmp_path)

    monkeypatch.setattr("gptmoss.api.server._filesystem_capability", lambda: FakeFilesystem())

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/executions/doc-api/document")

    response = asyncio.run(request())
    assert response.status_code == 200
    payload = response.json()
    assert payload["progress"] == 1.0
    assert payload["sections"][0]["heading"] == "Résumé"
    assert payload["checkpoint_available"] is True
    assert "checkpoint" not in payload


def test_document_progress_api_ignores_untrusted_checkpoint_path(tmp_path: Path, monkeypatch):
    state_engine = StateEngine()
    state = state_engine.get_execution("doc-safe")
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"status": "complete", "sections": [{"content": "secret"}]}), encoding="utf-8")
    state.variables["document_model_checkpoint"] = str(secret)
    monkeypatch.setattr(app_state, "state_engine", state_engine)

    class FakeFilesystem:
        def _get_workspace_for_execution(self, execution_id):
            return str(tmp_path / "workspace")

    monkeypatch.setattr("gptmoss.api.server._filesystem_capability", lambda: FakeFilesystem())

    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get("/executions/doc-safe/document")

    payload = asyncio.run(request()).json()
    assert payload["status"] == "not_initialized"
    assert payload["checkpoint_available"] is False
    assert "secret" not in json.dumps(payload)
    assert not (tmp_path / "workspace").exists()
