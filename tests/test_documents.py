from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from gptmoss.core.documents import (
    ArchiveSafetyPolicy,
    DOCX_CONTENT_TYPE,
    HTML_CONTENT_TYPE,
    PPTX_CONTENT_TYPE,
    DocumentParseError,
    UnsafeDocumentError,
    UnsupportedDocumentError,
    detect_document_type,
    parse_document,
)


WORD_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Architecture cible</w:t></w:r>
    </w:p>
    <w:p><w:r><w:t>Un paragraphe métier.</w:t></w:r></w:p>
    <w:p>
      <w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr>
      <w:r><w:t>Exigence prioritaire</w:t></w:r>
    </w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Composant</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Rôle</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Index</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Recherche</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
  </w:body>
</w:document>
"""

CORE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties
 xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <dc:title>Dossier de référence</dc:title>
</cp:coreProperties>
"""

PRESENTATION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>
"""

SLIDE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
 <p:cSld><p:spTree>
  <p:sp>
   <p:nvSpPr><p:cNvPr id="1" name="Title"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
   <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody>
  </p:sp>
  <p:sp>
   <p:nvSpPr><p:cNvPr id="2" name="Body"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
   <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{body}</a:t></a:r></a:p></p:txBody>
  </p:sp>
 </p:spTree></p:cSld>
</p:sld>
"""


def _make_docx(path: Path, *, unsafe_member: bool = False) -> Path:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", WORD_XML)
        archive.writestr("docProps/core.xml", CORE_XML)
        if unsafe_member:
            archive.writestr("../outside.txt", "must never be extracted")
    return path


def _make_pptx(path: Path) -> Path:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("ppt/presentation.xml", PRESENTATION_XML)
        archive.writestr(
            "ppt/slides/slide2.xml",
            SLIDE_TEMPLATE.format(title="Déploiement", body="Mode hors ligne"),
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            SLIDE_TEMPLATE.format(title="Vision", body="Corpus local"),
        )
    return path


def test_plain_text_preserves_structure_provenance_and_determinism(tmp_path: Path):
    source = tmp_path / "requirements.txt"
    source.write_text(
        "# Programme\n\nContexte général.\n\n## Exigences\n- Accès local\n- Traçabilité\n",
        encoding="utf-8",
    )

    first = parse_document(source)
    second = parse_document(source)

    assert first.id == second.id
    assert first.to_json() == second.to_json()
    assert type(first).from_dict(first.to_dict()).to_json() == first.to_json()
    renamed = first.with_filename("renamed.txt")
    assert renamed.filename == "renamed.txt"
    assert renamed.blocks[0].provenance.source_name == "renamed.txt"
    assert first.title == "Programme"
    assert [block.kind for block in first.blocks] == [
        "heading",
        "paragraph",
        "heading",
        "list_item",
        "list_item",
    ]
    assert first.blocks[-1].heading_path == ("Programme", "Exigences")
    assert first.blocks[-1].provenance.source_name == source.name
    assert "# Programme" in first.to_markdown()


def test_plain_text_supports_utf16_and_cp1252(tmp_path: Path):
    utf16 = tmp_path / "utf16.txt"
    utf16.write_bytes("# Décision\n\nDonnées locales".encode("utf-16"))
    legacy = tmp_path / "legacy.txt"
    legacy.write_bytes("Résumé de l'équipe".encode("cp1252"))

    assert "Données locales" in parse_document(utf16).text
    assert "équipe" in parse_document(legacy).text


def test_binary_file_with_text_extension_is_rejected(tmp_path: Path):
    source = tmp_path / "not-text.txt"
    source.write_bytes(b"hello\x00binary")

    with pytest.raises(DocumentParseError, match="Binary"):
        parse_document(source)


def test_html_extracts_content_table_and_never_loads_resources(tmp_path: Path):
    source = tmp_path / "existing.html"
    source.write_text(
        """<!doctype html><html><head><title>Existant</title>
        <script>secret_noise()</script><style>.hidden {x:y}</style></head>
        <body><h1>Système actuel</h1><p>Service <strong>local</strong>.</p>
        <ul><li>API interne</li></ul>
        <table><tr><th>Flux</th><th>État</th></tr>
        <tr><td>Documents</td><td>Actif</td></tr></table>
        <img src="https://invalid.example/image.png">
        <a href="https://invalid.example/page">Libellé local</a></body></html>""",
        encoding="utf-8",
    )

    document = parse_document(source)

    assert document.content_type == HTML_CONTENT_TYPE
    assert document.title == "Existant"
    assert "secret_noise" not in document.text
    assert "https://invalid.example" not in document.text
    assert "Service local." in document.text
    assert "| Flux | État |" in document.text
    assert document.metadata["external_resources_loaded"] is False


def test_html_signature_overrides_misleading_extension_and_mime(tmp_path: Path):
    source = tmp_path / "misleading.txt"
    source.write_text("<html><body><h1>Réel</h1></body></html>", encoding="utf-8")

    assert detect_document_type(source, "application/json") == HTML_CONTENT_TYPE
    assert parse_document(source, supplied_content_type="application/json").title == "Réel"


def test_url_inputs_are_refused_without_network_access():
    with pytest.raises(UnsafeDocumentError, match="local"):
        parse_document("https://example.invalid/source.docx")


def test_docx_extracts_headings_lists_tables_and_core_title(tmp_path: Path):
    source = _make_docx(tmp_path / "requirements.docx")

    document = parse_document(source)

    assert document.content_type == DOCX_CONTENT_TYPE
    assert document.title == "Dossier de référence"
    assert [block.kind for block in document.blocks] == [
        "heading",
        "paragraph",
        "list_item",
        "table",
    ]
    assert document.blocks[1].heading_path == ("Architecture cible",)
    assert "Un paragraphe métier." in document.text
    assert "| Composant | Rôle |" in document.text
    assert document.metadata["external_resources_loaded"] is False


def test_docx_is_detected_by_content_with_unknown_extension(tmp_path: Path):
    source = _make_docx(tmp_path / "renamed.bin")

    assert detect_document_type(source, "text/plain") == DOCX_CONTENT_TYPE
    assert parse_document(source).parser == "docx-ooxml"


def test_pptx_extracts_slides_in_numeric_order_with_provenance(tmp_path: Path):
    source = _make_pptx(tmp_path / "vision.pptx")

    document = parse_document(source)

    assert document.content_type == PPTX_CONTENT_TYPE
    assert document.title == "Vision"
    headings = [block.text for block in document.blocks if block.kind == "heading"]
    assert headings == ["Vision", "Déploiement"]
    assert document.blocks[0].provenance.slide_number == 1
    assert document.blocks[-1].provenance.slide_number == 2
    assert "Corpus local" in document.text
    assert document.metadata["slide_count"] == 2


def test_ooxml_unsafe_member_path_is_rejected(tmp_path: Path):
    source = _make_docx(tmp_path / "unsafe.docx", unsafe_member=True)

    with pytest.raises(UnsafeDocumentError, match="unsafe member path"):
        parse_document(source)


def test_ooxml_policy_is_configurable(tmp_path: Path):
    source = _make_docx(tmp_path / "bounded.docx")

    with pytest.raises(UnsafeDocumentError, match="too many entries"):
        parse_document(source, archive_policy=ArchiveSafetyPolicy(max_entries=1))


def test_unsupported_zip_archive_is_rejected(tmp_path: Path):
    source = tmp_path / "generic.zip"
    with ZipFile(source, "w") as archive:
        archive.writestr("readme.txt", "not OOXML")

    with pytest.raises(UnsupportedDocumentError, match="not a supported DOCX or PPTX"):
        parse_document(source)
