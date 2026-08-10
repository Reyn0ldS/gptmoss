from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from gptmoss.capabilities.documents import DocumentCapability
from gptmoss.core.artifacts import ArtifactStore
from gptmoss.interfaces.capability import generate_action_schema, get_actions


def _upload_text(store: ArtifactStore, filename: str, text: str):
    return store.save_base64(
        filename,
        base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "text/markdown" if filename.endswith(".md") else "text/plain",
    )


def _context(*artifact_ids: str, budget: int = 12_000):
    return {
        "variables": {"attachment_ids": list(artifact_ids)},
        "context_budget_chars": budget,
    }


def test_document_capability_inventory_is_scoped_to_explicit_attachments(tmp_path: Path):
    store = ArtifactStore(str(tmp_path))
    attached = _upload_text(
        store,
        "attached.txt",
        "# Attaché\n\nDocument autorisé.",
    )
    _upload_text(
        store,
        "private.txt",
        "# Privé\n\nDocument non sélectionné.",
    )
    capability = DocumentCapability(store)

    inventory = json.loads(capability.inventory(_context(attached["id"])))

    assert inventory["count"] == 1
    assert inventory["documents"][0]["artifact_id"] == attached["id"]
    assert inventory["scope"] == "explicitly attached local files"


def test_document_capability_search_read_and_read_chunk_keep_provenance(tmp_path: Path):
    store = ArtifactStore(str(tmp_path))
    uploaded = _upload_text(
        store,
        "architecture.md",
        (
            "# Architecture\n\n"
            "## Sécurité\n\nContrôle des accès et chiffrement local.\n\n"
            "## Exploitation\n\nSupervision et reprise."
        ),
    )
    context = _context(uploaded["id"])
    capability = DocumentCapability(store)

    searched = json.loads(
        capability.search(
            "securite chiffrement",
            artifact_id=uploaded["id"],
            context=context,
        )
    )
    first = searched["results"][0]
    chunk = json.loads(capability.read_chunk(first["id"], context=context))
    first_page = json.loads(
        capability.read(uploaded["id"], start_block=0, block_count=1, context=context)
    )

    assert first["artifact_id"] == uploaded["id"]
    assert first["provenance"][0]["source_name"] == "architecture.md"
    assert chunk["id"] == first["id"]
    assert "chiffrement local" in chunk["text"]
    assert first_page["returned_blocks"] == 1
    assert first_page["has_more"] is True
    assert first_page["next_start"] == 1
    assert first_page["blocks"][0]["provenance"]["source_name"] == "architecture.md"


def test_document_capability_refuses_unattached_document_access(tmp_path: Path):
    store = ArtifactStore(str(tmp_path))
    attached = _upload_text(store, "attached.txt", "# Attaché\n\nVisible.")
    hidden = _upload_text(store, "hidden.txt", "# Caché\n\nNon visible.")
    capability = DocumentCapability(store)
    context = _context(attached["id"])

    with pytest.raises(PermissionError, match="not attached"):
        capability.read(hidden["id"], context=context)

    with pytest.raises(PermissionError, match="not attached"):
        capability.search(
            "cache",
            artifact_id=hidden["id"],
            context=context,
        )


def test_document_search_output_is_budgeted_and_points_to_full_chunk(tmp_path: Path):
    store = ArtifactStore(str(tmp_path))
    long_section = "contexte " * 1_500
    uploaded = _upload_text(
        store,
        "large.txt",
        (
            "# Début\n\n"
            f"{long_section}\n\n"
            "## Centre\n\nindicateur-central " + ("détail " * 1_500) + "\n\n"
            "## Fin\n\nconclusion."
        ),
    )
    capability = DocumentCapability(store)

    payload = json.loads(
        capability.search(
            "indicateur central",
            limit=10,
            context=_context(uploaded["id"], budget=3_000),
        )
    )

    assert payload["results"]
    assert len(json.dumps(payload, ensure_ascii=False)) < 6_000
    assert payload["results"][0]["text_truncated"] is True
    assert payload["results"][0]["read_chunk_id"] == payload["results"][0]["id"]


def test_initial_context_retrieves_relevant_middle_instead_of_head_tail(tmp_path: Path):
    store = ArtifactStore(str(tmp_path))
    uploaded = _upload_text(
        store,
        "requirements.txt",
        (
            "# Introduction\n\n" + ("début banal " * 700) + "\n\n"
            "## Exigence centrale\n\njeton-ultra-specifique exigence décisive.\n\n"
            "## Annexes\n\n" + ("fin banale " * 700)
        ),
    )

    items = store.context_items(
        [uploaded["id"]],
        max_text_chars=2_500,
        query="jeton ultra specifique exigence decisive",
    )

    assert "jeton-ultra-specifique" in items[0]["text"]
    assert items[0]["retrieval"]["strategy"] == "ranked_local_search"
    assert items[0]["retrieval"]["selected_chunk_count"] >= 1
    assert "Local source: requirements.txt" in items[0]["text"]
    assert items[0]["text_compacted"] is True


def test_structural_sampling_represents_beginning_middle_and_end(tmp_path: Path):
    store = ArtifactStore(str(tmp_path))
    uploaded = _upload_text(
        store,
        "three-sections.txt",
        (
            "# Première\n\nmarqueur-debut " + ("alpha " * 500) + "\n\n"
            "## Deuxième\n\nmarqueur-milieu " + ("beta " * 500) + "\n\n"
            "## Troisième\n\nmarqueur-fin " + ("gamma " * 500)
        ),
    )

    item = store.context_items(
        [uploaded["id"]],
        max_text_chars=6_000,
    )[0]

    assert item["retrieval"]["strategy"] == "even_structural_sampling"
    assert "marqueur-debut" in item["text"]
    assert "marqueur-milieu" in item["text"]
    assert "marqueur-fin" in item["text"]


def test_document_capability_exposes_read_only_action_schemas(tmp_path: Path):
    capability = DocumentCapability(ArtifactStore(str(tmp_path)))
    actions = get_actions(type(capability))
    schemas = {
        name: generate_action_schema("documents", name, method)
        for name, method in actions.items()
    }

    assert set(schemas) == {"inventory", "search", "read", "read_chunk"}
    assert schemas["search"]["function"]["name"] == "documents__search"
    assert schemas["read"]["function"]["parameters"]["required"] == ["artifact_id"]
