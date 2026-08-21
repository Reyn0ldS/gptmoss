"""Adversarial and downstream contracts that a cooperative mock LLM never hits.

These lock the 2026-08-20 repair series and the next similar failure classes:
prose or nearby tools substituting for required evidence, mixed-up attachments,
first-run env vs persisted config, GUI settings omissions, and locator units.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

from gptmoss.capabilities.documents import DocumentCapability
from gptmoss.capabilities.filesystem import FilesystemCapability
from gptmoss.core.artifact_validation import validate_artifact
from gptmoss.core.artifacts import ArtifactStore
from gptmoss.core.context import ContextEngine
from gptmoss.core.delivery import extract_requirements
from gptmoss.core.event_bus import EventBus
from gptmoss.core.execution import ExecutionEngine
from gptmoss.core.plan_obligations import collect_plan_obligations
from gptmoss.core.settings import RuntimeSettings
from gptmoss.core.state import StateEngine
from gptmoss.memory.ram import RAMMemoryProvider
from gptmoss.planners.simple import SimplePlanner
from gptmoss.policies.simple import SimplePolicyProvider
from tests.mock_llm import MockLLMProvider


ROOT = Path(__file__).resolve().parents[1]
EXHAUSTIVE_STEP = {
    "id": 0,
    "role": "architect",
    "specialist": "Local Corpus Evidence Analyst",
    "description": "Inventory every explicit attachment and record complete coverage.",
    "operation": "inventory",
    "dependencies": [],
    "expertise": [],
    "required_artifacts": [],
    "acceptance_criteria": ["All normalized blocks were read."],
    "verification_commands": [],
}


def _engine(tmp_path, llm=None, max_iterations=8):
    state = StateEngine()
    provider = llm or MockLLMProvider()
    engine = ExecutionEngine(
        EventBus(), state, ContextEngine(state, RAMMemoryProvider()), provider,
        SimplePlanner(provider), SimplePolicyProvider(approval_required_capabilities=[]),
        max_step_iterations=max_iterations,
    )
    engine.register_capability("filesystem", FilesystemCapability(str(tmp_path), state))
    return engine, state


def _upload(store: ArtifactStore, filename: str, text: str):
    return store.save_base64(
        filename,
        base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "text/markdown" if filename.endswith(".md") else "text/plain",
    )


def _blocks_payload(store: ArtifactStore, artifact_id: str):
    document = store.document(artifact_id)
    return {
        "artifact_id": artifact_id,
        "total_blocks": len(document.blocks),
        "blocks": [block.to_dict() for block in document.blocks],
    }


def test_inventory_or_search_is_not_complete_document_coverage(tmp_path):
    engine, state = _engine(tmp_path)
    store = ArtifactStore(str(tmp_path / "artifacts"))
    uploaded = _upload(store, "source.md", "# A\n\none\n\n# B\n\ntwo\n")
    engine.artifact_store = store
    execution = state.get_execution("nearby-tools")
    execution.variables["attachment_ids"] = [uploaded["id"]]

    engine._record_tool_result(
        "nearby-tools", "documents", "inventory", {},
        json.dumps({"documents": [{"artifact_id": uploaded["id"], "block_count": 2}], "offset": 0}),
    )
    engine._record_tool_result(
        "nearby-tools", "documents", "search", {"query": "every block"},
        json.dumps({"results": [{"id": "chunk-1", "artifact_id": uploaded["id"]}]}),
    )
    engine._record_tool_result(
        "nearby-tools", "documents", "read_chunk", {"chunk_id": "chunk-1"},
        json.dumps({"id": "chunk-1", "artifact_id": uploaded["id"], "text": "one"}),
    )

    issues = engine._document_coverage_issues("nearby-tools", EXHAUSTIVE_STEP)
    assert issues
    assert "source.md" in issues[0]


def test_reading_the_wrong_attachment_does_not_cover_the_required_file(tmp_path):
    engine, state = _engine(tmp_path)
    store = ArtifactStore(str(tmp_path / "twins"))
    needed = _upload(store, "notes.md", "# Needed\n\nalpha\n\n# More\n\nbeta\n")
    decoy = _upload(store, "notes-final.md", "# Decoy\n\ngamma\n")
    engine.artifact_store = store
    execution = state.get_execution("wrong-id")
    execution.variables["attachment_ids"] = [needed["id"], decoy["id"]]

    engine._record_tool_result(
        "wrong-id", "documents", "read", {},
        json.dumps(_blocks_payload(store, decoy["id"])),
    )
    issues = engine._document_coverage_issues("wrong-id", EXHAUSTIVE_STEP)
    assert any("notes.md" in item for item in issues)
    assert not any("notes-final.md" in item and "notes.md" not in item for item in issues)

    engine._record_tool_result(
        "wrong-id", "documents", "read", {},
        json.dumps(_blocks_payload(store, needed["id"])),
    )
    assert engine._document_coverage_issues("wrong-id", EXHAUSTIVE_STEP) == []


def test_empty_read_payload_does_not_satisfy_block_coverage(tmp_path):
    engine, state = _engine(tmp_path)
    store = ArtifactStore(str(tmp_path / "empty-read"))
    uploaded = _upload(store, "evidence.md", "# Title\n\nbody\n")
    engine.artifact_store = store
    execution = state.get_execution("empty-read")
    execution.variables["attachment_ids"] = [uploaded["id"]]
    engine._record_tool_result(
        "empty-read", "documents", "read", {},
        json.dumps({"artifact_id": uploaded["id"], "total_blocks": 2, "blocks": []}),
    )
    assert engine._document_coverage_issues("empty-read", EXHAUSTIVE_STEP)


@pytest.mark.asyncio
async def test_inventory_then_prose_delivery_cannot_complete_without_reads(tmp_path):
    delivery = json.dumps({
        "summary": "inventoried", "artifacts": ["analysis/corpus-inventory.md"],
        "evidence": ["documents.inventory listed every file"],
        "risks": [], "next_action": "handoff",
    })
    llm = MockLLMProvider()
    llm.add_response(tool_calls=[{
        "id": "inv-1", "type": "function",
        "function": {"name": "documents__inventory", "arguments": {"limit": 50}},
    }])
    for _ in range(6):
        llm.add_response(content=delivery)
    engine, state = _engine(tmp_path, llm, max_iterations=8)
    store = ArtifactStore(str(tmp_path / "forced-inventory"))
    uploaded = _upload(store, "evidence.md", "# One\n\nfirst\n\n# Two\n\nsecond\n")
    engine.artifact_store = store
    engine.register_capability("documents", DocumentCapability(store))
    execution = state.get_execution("inventory-is-not-enough")
    execution.variables.update({
        "parent_execution_id": "parent",
        "role_key": "architect",
        "role_name": "Local Corpus Evidence Analyst",
        "attachment_ids": [uploaded["id"]],
    })
    execution.current_plan = {"steps": [dict(EXHAUSTIVE_STEP)]}
    project = tmp_path / "projects" / "proj-default" / "analysis"
    project.mkdir(parents=True)
    (project / "corpus-inventory.md").write_text("# Inventory\n\n- evidence.md\n", encoding="utf-8")
    execution.current_plan["steps"][0]["required_artifacts"] = ["analysis/corpus-inventory.md"]

    await engine.execute_task("inventory-is-not-enough", "Inventory every attached source")

    history = execution.variables.get("tool_call_history") or []
    assert any(
        item.get("capability") == "documents" and item.get("action") == "inventory"
        for item in history
    )
    assert execution.status != "completed", execution.results
    assert execution.status in {"failed", "running", "paused"}


def test_user_task_stays_verbatim_beside_corpus_policy():
    task = "Inventorie les pièces et rédige le dossier. Ne change pas cette phrase."
    obligations = collect_plan_obligations(
        task=task,
        planning_mode="direct",
        analysis={"level": "low", "domains": ["general"]},
        workload_profile={"attachment_count": 3, "document_count": 3},
        corpus_auto_workflow=True,
        corpus_policy={"enabled": True, "professional_delivery": False},
    )
    assert task == "Inventorie les pièces et rédige le dossier. Ne change pas cette phrase."
    assert "source_inventory" in {item["id"] for item in obligations}
    blob = json.dumps(obligations, ensure_ascii=False)
    assert "Ne change pas cette phrase" not in blob


def test_explicit_requirement_ids_are_not_renumbered():
    requirements = extract_requirements(
        "DEC-12 — Conserver l'identifiant métier.\n"
        "REQ-E2E-007: Produire la matrice.\n"
        "Une exigence sans identifiant explicite."
    )
    ids = [item["id"] for item in requirements]
    assert "DEC-12" in ids
    assert "REQ-E2E-007" in ids
    assert "REQ-001" in ids
    assert ids.count("REQ-001") == 1


def test_pptx_cannot_be_cited_with_normalized_blocks_when_inventory_is_slides(tmp_path):
    document = tmp_path / "mix-units.md"
    document.write_text(
        "# Evidence\n\nLa présentation est bornée. [vision.pptx > blocks 1-2]\n",
        encoding="utf-8",
    )
    report = validate_artifact(
        document,
        validator="document",
        constraints={
            "required_source_files": ["vision.pptx"],
            "source_inventory": {"vision.pptx": {"slides": 4}},
            "require_local_references": True,
            "require_bounded_references": True,
        },
    )
    assert not report["valid"]
    assert any("uses blocks but its inventory has no blocks count" in item for item in report["failures"])


def test_unsupported_mermaid_subset_is_not_a_useful_diagram(tmp_path):
    document = tmp_path / "gantt.md"
    document.write_text(
        """# Dossier

```mermaid
gantt
title Schedule
section Work
Task :done, 2024-01-01, 1d
```
""",
        encoding="utf-8",
    )
    report = validate_artifact(
        document,
        validator="document",
        constraints={"reject_invalid_diagrams": True, "minimums": {"valid_diagrams": 1}},
    )
    assert not report["valid"]
    assert report["metrics"]["valid_diagrams"] == 0
    assert report["metrics"]["invalid_diagrams"] >= 1
    assert any("unsupported mermaid diagram type 'gantt'" in item for item in report["failures"])


def test_mermaid_pie_counts_as_a_valid_document_diagram(tmp_path):
    document = tmp_path / "pie.md"
    document.write_text(
        """# Dossier

```mermaid
pie title Parts
"A" : 40
"B" : 60
```
""",
        encoding="utf-8",
    )
    report = validate_artifact(
        document,
        validator="document",
        constraints={"reject_invalid_diagrams": True, "minimums": {"valid_diagrams": 1}},
    )
    assert report["valid"]
    assert report["metrics"]["valid_diagrams"] >= 1
    assert report["metrics"]["invalid_diagrams"] == 0


def test_gui_settings_payload_covers_every_runtime_settings_field():
    gui = (ROOT / "gptmoss" / "api" / "gui.html").read_text(encoding="utf-8")
    start = gui.index("function collectSettingsPayload")
    end = gui.index("async function revealApiKey")
    body = gui[start:end]
    missing = [
        name for name in RuntimeSettings.model_fields
        if not re.search(rf"\b{re.escape(name)}\s*:", body)
    ]
    assert missing == [], f"GUI omits runtime settings fields: {missing}"


@pytest.mark.asyncio
async def test_french_paragraph_prefix_repairs_without_mojibake(tmp_path):
    engine, state = _engine(tmp_path)
    execution = state.get_execution("utf8-repair")
    execution.variables.update({
        "delivery_contract": {
            "steps": [{"step_id": 0, "role": "writer", "owned_paths": ["dossier.md"]}]
        },
        "plan_step_id": 0,
        "role_key": "writer",
    })
    project = tmp_path / "projects" / "proj-default"
    project.mkdir(parents=True)
    target = project / "dossier.md"
    original = (
        "La synthèse exécutive reprend les décisions DEC-04 et ADR-11 "
        "sans citer encore la source locale obligatoire."
    )
    target.write_text(f"# Dossier\n\n{original}\n", encoding="utf-8")

    result = await engine._call_tool(
        "utf8-repair", "filesystem", "replace_paragraph",
        {
            "path": "dossier.md",
            "paragraph_prefix": "La synthese executive reprend les decisions DEC-04",
            "content": (
                "La synthèse exécutive reprend les décisions DEC-04 et ADR-11. "
                "[requirements.docx > blocks 2-3]"
            ),
        },
    )
    content = target.read_text(encoding="utf-8")
    assert "replaced successfully" in result
    assert "synthèse exécutive" in content
    assert "Ã©" not in content
    assert "requirements.docx" in content


def test_read_images_rejects_an_oversized_batch(tmp_path):
    store = ArtifactStore(str(tmp_path))
    images = [
        store.save_bytes(f"shot-{index}.png", b"\x89PNG\r\n\x1a\n" + bytes([index]), "image/png")
        for index in range(5)
    ]
    capability = DocumentCapability(store)
    context = {"variables": {"attachment_ids": [item["id"] for item in images]}}
    with pytest.raises(ValueError, match="1 and 4"):
        capability.read_images([item["id"] for item in images], context=context)


def test_fifth_attached_image_remains_missing_after_a_full_batch(tmp_path):
    engine, state = _engine(tmp_path)
    engine.llm_provider.supports_vision = True
    store = ArtifactStore(str(tmp_path / "five-images"))
    images = [
        store.save_bytes(f"shot-{index}.png", b"\x89PNG\r\n\x1a\n" + bytes([index]), "image/png")
        for index in range(5)
    ]
    engine.artifact_store = store
    execution = state.get_execution("five-images")
    execution.variables["attachment_ids"] = [item["id"] for item in images]
    execution.variables["visualized_artifact_ids"] = [item["id"] for item in images[:4]]
    issues = engine._document_coverage_issues("five-images", EXHAUSTIVE_STEP)
    assert issues
    assert "shot-4.png" in issues[0]
    assert "shot-0.png" not in issues[0]
