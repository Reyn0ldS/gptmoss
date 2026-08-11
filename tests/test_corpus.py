from __future__ import annotations

import json
from pathlib import Path

from gptmoss.core.corpus import LocalDocumentIndex, chunk_document
from gptmoss.core.documents import parse_document


def _document(tmp_path: Path, filename: str, text: str):
    path = tmp_path / filename
    path.write_text(text, encoding="utf-8")
    return parse_document(path)


def test_hierarchical_chunking_covers_every_block_and_large_middle(tmp_path: Path):
    middle = "milieu-rare " + ("contenu intermédiaire " * 90)
    document = _document(
        tmp_path,
        "large.txt",
        (
            "# Début\n\nIntroduction repérable.\n\n"
            f"## Partie centrale\n\n{middle}\n\n"
            "## Conclusion\n\nmarqueur-final vérifiable."
        ),
    )

    chunks = chunk_document("artifact-large", document, target_chars=512)

    assert len(chunks) >= 5
    covered_ids = {block_id for chunk in chunks for block_id in chunk.block_ids}
    assert covered_ids == {block.id for block in document.blocks}
    assert any("milieu-rare" in chunk.text for chunk in chunks)
    assert any("marqueur-final" in chunk.text for chunk in chunks)
    assert all(chunk.provenance for chunk in chunks)
    assert all(chunk.heading_path for chunk in chunks)


def test_local_search_is_accent_insensitive_ranked_and_traceable(tmp_path: Path):
    architecture = _document(
        tmp_path,
        "architecture.txt",
        (
            "# Architecture\n\n"
            "## Sécurité\n\nChiffrement local et contrôle des accès sensibles.\n\n"
            "## Données\n\nCatalogue des documents internes."
        ),
    )
    operations = _document(
        tmp_path,
        "operations.txt",
        "# Exploitation\n\nSupervision locale et continuité de service.",
    )
    index = LocalDocumentIndex(tmp_path / "index.json", target_chunk_chars=512)
    index.add_document("arch", architecture)
    index.add_document("ops", operations)

    results = index.search("securite controle acces", limit=5)

    assert results
    assert results[0]["artifact_id"] == "arch"
    assert results[0]["heading_path"] == ["Architecture", "Sécurité"]
    assert results[0]["provenance"][0]["source_name"] == "architecture.txt"
    assert {"securite", "controle", "acces"}.issubset(results[0]["matched_terms"])
    assert results[0]["score"] > 0


def test_search_filters_source_type_heading_and_kind(tmp_path: Path):
    first = _document(
        tmp_path,
        "first.txt",
        "# Programme\n\n## Risques\n\nRisque fournisseur critique.",
    )
    second = _document(
        tmp_path,
        "second.md",
        "# Programme\n\n## Décisions\n\nFournisseur secondaire retenu.",
    )
    index = LocalDocumentIndex(tmp_path / "filtered-index.json")
    index.add_document("first-id", first)
    index.add_document("second-id", second)

    source_results = index.search("fournisseur", artifact_ids=["second-id"])
    heading_results = index.search("fournisseur", heading="risques")
    kind_results = index.search("programme", kinds=["heading"])

    assert {item["artifact_id"] for item in source_results} == {"second-id"}
    assert {item["artifact_id"] for item in heading_results} == {"first-id"}
    assert kind_results
    assert all("heading" in item["block_kinds"] for item in kind_results)


def test_index_persists_and_reloads_deterministically(tmp_path: Path):
    document = _document(
        tmp_path,
        "persistent.txt",
        "# Persistance\n\nInformation retrouvée après redémarrage.",
    )
    path = tmp_path / "persistent-index.json"
    first = LocalDocumentIndex(path)
    first.add_document("persistent-id", document)
    first_results = first.search("retrouvee redemarrage")
    payload_before = json.loads(path.read_text(encoding="utf-8"))

    second = LocalDocumentIndex(path)
    second_results = second.search("retrouvee redemarrage")
    payload_after = json.loads(path.read_text(encoding="utf-8"))

    assert first_results == second_results
    assert payload_before == payload_after
    assert second.fingerprints() == {"persistent-id": document.id}
    assert second.stats()["documents"] == 1


def test_rebuild_invalidates_removed_and_changed_documents(tmp_path: Path):
    old = _document(
        tmp_path,
        "old.txt",
        "# Ancien\n\nmarqueur-obsolète.",
    )
    stable = _document(
        tmp_path,
        "stable.txt",
        "# Stable\n\nmarqueur-conservé.",
    )
    index = LocalDocumentIndex(tmp_path / "rebuild-index.json")
    index.rebuild([("old-id", old), ("stable-id", stable)])

    replacement = _document(
        tmp_path,
        "replacement.txt",
        "# Nouveau\n\nmarqueur-actuel.",
    )
    counts = index.rebuild([("new-id", replacement), ("stable-id", stable)])

    assert set(counts) == {"new-id", "stable-id"}
    assert not index.search("obsolete")
    assert index.search("actuel")[0]["artifact_id"] == "new-id"
    assert index.search("conserve")[0]["artifact_id"] == "stable-id"
    assert set(index.fingerprints()) == {"new-id", "stable-id"}


def test_corrupt_index_fails_closed_and_can_be_rebuilt(tmp_path: Path):
    path = tmp_path / "corrupt-index.json"
    path.write_text("{broken", encoding="utf-8")

    index = LocalDocumentIndex(path)

    assert index.load_error
    assert index.inventory() == []
    assert index.search("anything") == []

    document = _document(
        tmp_path,
        "recovered.txt",
        "# Reprise\n\nIndex reconstruit localement.",
    )
    index.rebuild([("recovered-id", document)])

    assert index.load_error == ""
    assert index.search("reconstruit")[0]["artifact_id"] == "recovered-id"
