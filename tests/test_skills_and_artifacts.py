import base64
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from gptmoss.core.artifacts import ArtifactStore
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


def test_artifact_store_removes_failed_document_upload(tmp_path):
    store = ArtifactStore(str(tmp_path))

    with pytest.raises(ValueError):
        store.save_base64(
            "broken.docx",
            base64.b64encode(b"not an OOXML archive").decode("ascii"),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    assert list(store.root.iterdir()) == []


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
