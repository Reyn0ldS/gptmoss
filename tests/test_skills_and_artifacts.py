import base64
import errno
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from gptmoss.core.artifacts import ArtifactStore
from gptmoss.core import durable_io
from gptmoss.core.skills import SkillRegistry


def _minimal_docx_bytes() -> bytes:
    payload = BytesIO()
    document_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body>
        <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Exigences locales</w:t></w:r></w:p>
        <w:p><w:r><w:t>Les sources restent sur le poste.</w:t></w:r></w:p>
      </w:body>
    </w:document>"""
    with ZipFile(payload, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return payload.getvalue()


def test_skill_registry_discovers_and_selects_builtin_skill():
    registry = SkillRegistry([str(Path(__file__).resolve().parents[1] / "gptmoss" / "skills")])
    selected = registry.select("Write Python code with tests", requested=["secure-python"])
    assert selected[0].name == "secure-python"
    assert selected[0].allowed_capabilities == ["filesystem", "shell"]


def test_explicit_skill_selection_is_complete_and_not_auto_augmented():
    registry = SkillRegistry([str(Path(__file__).resolve().parents[1] / "gptmoss" / "skills")])

    selected = registry.select(
        "Analyze ingestion from vision.pptx and deliver final validation",
        requested=["document-analysis", "documentation", "project-architecture"],
        limit=1,
    )

    assert [skill.name for skill in selected] == [
        "document-analysis", "documentation", "project-architecture",
    ]


def test_professional_document_skills_use_standard_tools_and_quality_gates():
    root = Path(__file__).resolve().parents[1] / "gptmoss" / "skills"
    registry = SkillRegistry([str(root)])

    analysis = registry.skills["document-analysis"]
    documentation = registry.skills["documentation"]
    architecture = registry.skills["project-architecture"]

    for skill in (analysis, documentation, architecture):
        assert skill.allowed_capabilities == ["documents", "filesystem"]
        assert "TODO" not in skill.instructions
        report = registry.validate(
            name=skill.name,
            description=skill.description,
            instructions=skill.instructions,
            allowed_capabilities=skill.allowed_capabilities,
            registered_capabilities={"documents", "filesystem"},
        )
        assert report["valid"], report

    assert registry.select(
        "Analyze the attached DOCX corpus and trace every requirement",
        requested=["document-analysis"],
    )[0].name == "document-analysis"
    assert "documents.search" in analysis.instructions
    assert "traceability matrix" in documentation.instructions
    assert "migration" in architecture.instructions

    interface = (
        root / "document-analysis" / "agents" / "openai.yaml"
    ).read_text(encoding="utf-8")
    assert "$document-analysis" in interface


def test_skill_compatibility_report_maps_known_external_tools(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("Use shell_command and apply_patch, then image_gen.", encoding="utf-8")
    report = SkillRegistry().compatibility_report(str(path))
    assert report["mapped"] == {"apply_patch": "filesystem", "shell_command": "shell"}
    assert report["unsupported"] == ["image_gen"]


def test_artifact_store_handles_text_and_rejects_invalid_image(tmp_path):
    store = ArtifactStore(str(tmp_path))
    payload = base64.b64encode(b"# Notes\\nUse a blue theme.").decode("ascii")
    metadata = store.save_base64("notes.md", payload, "text/markdown")
    context = store.context_items([metadata["id"]])
    assert "blue theme" in context[0]["text"]
    assert context[0]["sha256"] == metadata["sha256"]

    with pytest.raises(ValueError, match="Invalid PNG"):
        store.save_base64("bad.png", payload, "image/png")


def test_artifact_store_normalizes_docx_and_reuses_cached_structure(tmp_path):
    store = ArtifactStore(str(tmp_path))
    metadata = store.save_base64(
        "requirements.bin",
        base64.b64encode(_minimal_docx_bytes()).decode("ascii"),
        "application/octet-stream",
    )

    assert metadata["content_type"].endswith("wordprocessingml.document")
    assert metadata["document_title"] == "Exigences locales"
    assert metadata["document_blocks"] == 2
    assert Path(metadata["document_path"]).is_file()

    document = store.document(metadata["id"])
    context = store.context_items([metadata["id"]])
    assert document.title == "Exigences locales"
    assert "Les sources restent sur le poste." in store.preview_text(metadata["id"])
    assert context[0]["document"]["block_count"] == 2
    assert context[0]["text_compacted"] is False
    results = store.search_documents("sources poste")
    assert results[0]["artifact_id"] == metadata["id"]
    assert results[0]["provenance"][0]["source_name"] == "requirements.bin"

    reloaded = ArtifactStore(str(tmp_path))
    assert reloaded.search_documents("sources poste") == results
    reloaded.delete(metadata["id"])
    assert reloaded.search_documents("sources poste") == []
    assert reloaded.document_index.stats()["documents"] == 0


def test_artifact_store_removes_failed_document_upload(tmp_path):
    store = ArtifactStore(str(tmp_path))

    with pytest.raises(ValueError):
        store.save_base64(
            "broken.docx",
            base64.b64encode(b"not an OOXML archive").decode("ascii"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    assert list(store.root.iterdir()) == []


def test_artifact_store_retries_transient_unc_document_writes(tmp_path, monkeypatch):
    store = ArtifactStore(str(tmp_path))
    original_write = durable_io._write_once
    attempts = 0

    def flaky_write(path, payload):
        nonlocal attempts
        if ".document.json." in path.name:
            attempts += 1
            if attempts < 3:
                raise OSError(errno.EINVAL, "transient UNC redirector failure")
        return original_write(path, payload)

    monkeypatch.setattr(durable_io, "_write_once", flaky_write)
    monkeypatch.setattr(durable_io.time, "sleep", lambda _delay: None)

    metadata = store.save_base64(
        "requirements.docx",
        base64.b64encode(_minimal_docx_bytes()).decode("ascii"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert attempts == 3
    assert store.document(metadata["id"]).title == "Exigences locales"
    assert not list(store.root.glob(".*.tmp"))


def test_artifact_store_cleans_a_persistent_unc_write_failure(tmp_path, monkeypatch):
    store = ArtifactStore(str(tmp_path))
    original_write = durable_io._write_once

    def failing_write(path, payload):
        if ".document.json." in path.name:
            raise OSError(errno.EINVAL, "persistent UNC redirector failure")
        return original_write(path, payload)

    monkeypatch.setattr(durable_io, "_write_once", failing_write)
    monkeypatch.setattr(durable_io.time, "sleep", lambda _delay: None)

    with pytest.raises(OSError, match="persistent UNC"):
        store.save_base64(
            "requirements.docx",
            base64.b64encode(_minimal_docx_bytes()).decode("ascii"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    assert list(store.root.iterdir()) == []
    assert store.document_index.stats()["documents"] == 0


def test_artifact_store_rebuilds_a_corrupt_persistent_index(tmp_path):
    store = ArtifactStore(str(tmp_path))
    metadata = store.save_base64(
        "requirements.docx",
        base64.b64encode(_minimal_docx_bytes()).decode("ascii"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    index_path = store.document_index.path
    index_path.write_text("{corrupt", encoding="utf-8")

    recovered = ArtifactStore(str(tmp_path))

    assert recovered.document_index.load_error == ""
    assert recovered.search_documents("sources locales")[0]["artifact_id"] == metadata["id"]


def test_artifact_store_recovers_an_empty_corrupt_index(tmp_path):
    store = ArtifactStore(str(tmp_path))
    store.document_index.path.write_text("{corrupt", encoding="utf-8")

    recovered = ArtifactStore(str(tmp_path))

    assert recovered.document_index.load_error == ""
    assert recovered.document_index.stats()["documents"] == 0
    assert json.loads(recovered.document_index.path.read_text(encoding="utf-8"))["version"] == 1


@pytest.mark.parametrize(
    ("filename", "content_type", "payload"),
    [
        ("reference.png", "image/png", b"\x89PNG\r\n\x1a\nminimal-payload"),
        ("reference.jpg", "image/jpeg", b"\xff\xd8minimal-payload"),
        ("reference.webp", "image/webp", b"RIFF\x04\x00\x00\x00WEBP"),
    ],
)
def test_artifact_store_accepts_valid_image_signatures(
    tmp_path, filename, content_type, payload
):
    store = ArtifactStore(str(tmp_path))
    metadata = store.save_base64(
        filename, base64.b64encode(payload).decode("ascii"), content_type
    )

    assert Path(metadata["path"]).read_bytes() == payload
    assert metadata["content_type"] == content_type


def test_folder_corpus_preserves_relative_paths_deduplicates_and_resumes(tmp_path):
    store = ArtifactStore(str(tmp_path))
    corpus, resumed = store.create_corpus(
        "Dossier métier", root_label="sources", resume=True
    )
    assert resumed is False
    payload = b"# Decision\nUse the local evidence only."

    first = store.save_bytes(
        "decision.md",
        payload,
        "text/markdown",
        corpus_id=corpus["id"],
        relative_path="sources/governance/decision.md",
        last_modified=123,
    )
    duplicate = store.save_bytes(
        "decision.md",
        payload,
        "text/markdown",
        corpus_id=corpus["id"],
        relative_path="sources/governance/decision.md",
        last_modified=123,
    )

    assert duplicate["id"] == first["id"]
    assert duplicate["deduplicated"] is True
    assert store.document(first["id"]).filename == "sources/governance/decision.md"
    finalized = store.finalize_corpus(
        corpus["id"],
        present_paths=["sources/governance/decision.md"],
        skipped=[{"relative_path": "sources/archive.zip", "reason": "unsupported"}],
    )
    assert finalized["state"] == "ready"
    assert finalized["entries"]["sources/governance/decision.md"]["artifact_id"] == first["id"]

    reopened, resumed = ArtifactStore(str(tmp_path)).create_corpus(
        "Dossier métier", root_label="sources", resume=True
    )
    assert resumed is True
    assert reopened["id"] == corpus["id"]
    assert len(list(store.root.glob(f"*_{first['filename']}"))) == 1


@pytest.mark.parametrize(
    "relative_path",
    ["../secret.md", "sources/../../secret.md", "C:/secret.md", "", "/", "/secret.md", "\\\\server\\share\\secret.md"],
)
def test_folder_corpus_rejects_unsafe_relative_paths(tmp_path, relative_path):
    store = ArtifactStore(str(tmp_path))
    corpus, _ = store.create_corpus("Safe", root_label="sources")
    with pytest.raises(ValueError, match="relative path"):
        store.save_bytes(
            "secret.md", b"secret", "text/markdown",
            corpus_id=corpus["id"], relative_path=relative_path,
        )


def test_folder_corpus_finalization_removes_stale_manifest_entries(tmp_path):
    store = ArtifactStore(str(tmp_path))
    corpus, _ = store.create_corpus("Refresh", root_label="sources")
    artifacts = {}
    for name in ("keep.md", "removed.md"):
        artifacts[name] = store.save_bytes(
            name, name.encode(), "text/markdown", corpus_id=corpus["id"],
            relative_path=f"sources/{name}",
        )
    finalized = store.finalize_corpus(
        corpus["id"], present_paths=["sources/keep.md"],
        errors=[{"relative_path": "sources/broken.pdf", "error": "unreadable"}],
    )
    assert finalized["state"] == "partial"
    assert set(finalized["entries"]) == {"sources/keep.md"}
    assert "corpus_memberships" not in store.get(artifacts["removed.md"]["id"])


def test_folder_corpus_replacement_cleans_previous_membership(tmp_path):
    store = ArtifactStore(str(tmp_path))
    corpus, _ = store.create_corpus("Refresh", root_label="sources")
    old = store.save_bytes(
        "scope.md", b"old", "text/markdown", corpus_id=corpus["id"],
        relative_path="sources/scope.md",
    )
    new = store.save_bytes(
        "scope.md", b"new", "text/markdown", corpus_id=corpus["id"],
        relative_path="sources/scope.md",
    )

    assert old["id"] != new["id"]
    assert "corpus_memberships" not in store.get(old["id"])
    assert store.get_corpus(corpus["id"])["entries"]["sources/scope.md"]["artifact_id"] == new["id"]


def test_folder_corpus_deletion_retains_evidence_and_removes_membership(tmp_path):
    store = ArtifactStore(str(tmp_path))
    corpus, _ = store.create_corpus("Library", root_label="sources")
    artifact = store.save_bytes(
        "evidence.md", b"retained evidence", "text/markdown",
        corpus_id=corpus["id"], relative_path="sources/evidence.md",
    )
    store.finalize_corpus(corpus["id"], present_paths=["sources/evidence.md"])

    deleted = store.delete_corpus(corpus["id"])

    assert deleted["id"] == corpus["id"]
    with pytest.raises(FileNotFoundError):
        store.get_corpus(corpus["id"])
    assert Path(store.get(artifact["id"])["path"]).is_file()
    assert "corpus_memberships" not in store.get(artifact["id"])
