"""Create a portable, professionally formatted delivery package after assurance."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

from gptmoss.core.durable_io import write_bytes_atomic, write_text_atomic


def _xml_text(value: str) -> str:
    return escape(value, {'"': "&quot;", "'": "&apos;"})


def _paragraph(text: str, style: str = "Normal") -> str:
    return (
        "<w:p><w:pPr><w:pStyle w:val=\"" + style + "\"/></w:pPr>"
        "<w:r><w:t xml:space=\"preserve\">" + _xml_text(text) + "</w:t></w:r></w:p>"
    )


def _markdown_body(markdown: str) -> str:
    output = []
    for raw in markdown.splitlines():
        line = raw.rstrip()
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        bullet = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if heading:
            output.append(_paragraph(heading.group(2), f"Heading{min(len(heading.group(1)), 3)}"))
        elif bullet:
            output.append(_paragraph("• " + bullet.group(1), "ListParagraph"))
        elif not line:
            output.append("<w:p/>")
        elif re.fullmatch(r"\|?[\s:|-]+\|?", line):
            continue
        else:
            output.append(_paragraph(line))
    return "".join(output)


def render_docx(markdown: str, *, title: str, subject: str = "GPTMOSS delivery") -> bytes:
    """Render Markdown to a standards-compliant DOCX without optional dependencies."""
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>' + _markdown_body(markdown) +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" '
        'w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr></w:body></w:document>'
    )
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/><w:sz w:val="22"/></w:rPr><w:pPr><w:spacing w:after="120" w:line="276" w:lineRule="auto"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="320" w:after="160"/></w:pPr><w:rPr><w:b/><w:color w:val="17365D"/><w:sz w:val="34"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="260" w:after="120"/></w:pPr><w:rPr><w:b/><w:color w:val="24527A"/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:before="220" w:after="100"/></w:pPr><w:rPr><w:b/><w:sz w:val="24"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="360"/></w:pPr></w:style>
</w:styles>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/></Types>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>'''
    document_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'''
    core = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:title>{_xml_text(title)}</dc:title><dc:subject>{_xml_text(subject)}</dc:subject><dc:creator>GPTMOSS</dc:creator><dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created></cp:coreProperties>'''
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/styles.xml", styles)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
        archive.writestr("docProps/core.xml", core)
    return stream.getvalue()


def _safe_artifacts(workspace: Path, plan: Dict[str, Any]) -> Iterable[tuple[str, Path]]:
    seen = set()
    for item in plan.get("artifact_validations", []):
        if not isinstance(item, dict) or not item.get("required", True):
            continue
        name = str(item.get("path") or "").replace("\\", "/").lstrip("/")
        path = (workspace / name).resolve()
        if not name or workspace not in path.parents or not path.is_file() or name in seen:
            continue
        seen.add(name)
        yield name, path


def build_delivery_package(
    workspace: str | Path,
    execution_id: str,
    plan: Dict[str, Any],
    assurance_report: Dict[str, Any],
) -> Dict[str, Any] | None:
    """Package validated files, a styled DOCX, assurance evidence, and hashes."""
    profile = plan.get("professional_profile") or {}
    if plan.get("delivery_profile") != "professional-local":
        return None
    root = Path(workspace).resolve()
    primary_name = str(profile.get("primary_artifact") or plan.get("primary_artifact") or "")
    primary_path = (root / primary_name).resolve()
    if not primary_name or root not in primary_path.parents or not primary_path.is_file():
        return None
    markdown = primary_path.read_text(encoding="utf-8")
    title_match = re.search(r"(?m)^#\s+(.+)$", markdown)
    title = title_match.group(1).strip() if title_match else primary_path.stem.replace("-", " ").title()
    docx_bytes = render_docx(markdown, title=title)
    delivery_dir = root / ".gptmoss" / "deliveries" / execution_id
    delivery_dir.mkdir(parents=True, exist_ok=True)
    docx_path = delivery_dir / (primary_path.stem + ".docx")
    assurance_path = delivery_dir / "delivery-assurance.json"
    manifest_path = delivery_dir / "manifest.json"
    zip_path = delivery_dir / (primary_path.stem + "-delivery.zip")
    write_bytes_atomic(docx_path, docx_bytes)
    write_text_atomic(assurance_path, json.dumps(assurance_report, ensure_ascii=False, indent=2) + "\n")
    artifacts = list(_safe_artifacts(root, plan))
    entries = [
        {"path": f"artifacts/{name}", "sha256": sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}
        for name, path in artifacts
    ]
    entries.extend([
        {"path": docx_path.name, "sha256": sha256(docx_bytes).hexdigest(), "size_bytes": len(docx_bytes)},
        {"path": assurance_path.name, "sha256": sha256(assurance_path.read_bytes()).hexdigest(), "size_bytes": assurance_path.stat().st_size},
    ])
    manifest = {
        "schema_version": 1, "execution_id": execution_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "profile": "professional-local", "primary_artifact": primary_name,
        "assurance_passed": bool(assurance_report.get("passed")), "files": entries,
    }
    write_text_atomic(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name, path in artifacts:
            archive.write(path, f"artifacts/{name}")
        archive.writestr(docx_path.name, docx_bytes)
        archive.write(assurance_path, assurance_path.name)
        archive.write(manifest_path, manifest_path.name)
    write_bytes_atomic(zip_path, stream.getvalue())
    return {
        "profile": "professional-local", "title": title,
        "primary_artifact": primary_name, "docx_path": str(docx_path),
        "manifest_path": str(manifest_path), "archive_path": str(zip_path),
        "archive_sha256": sha256(zip_path.read_bytes()).hexdigest(),
        "archive_size_bytes": zip_path.stat().st_size,
    }
