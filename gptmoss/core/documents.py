"""Local, dependency-free document normalization for GPTMOSS.

These parsers never resolve external resources. They expose a common model so
indexing and agent workflows do not need to know each source format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
import json
import re
from typing import Any, Iterable, Protocol
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET


DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
HTML_CONTENT_TYPE = "text/html"
TEXT_CONTENT_TYPES = frozenset(
    {"text/plain", "text/markdown", "application/json", "text/csv"}
)
SUPPORTED_DOCUMENT_TYPES = frozenset(
    {*TEXT_CONTENT_TYPES, HTML_CONTENT_TYPE, DOCX_CONTENT_TYPE, PPTX_CONTENT_TYPE}
)

_EXTENSION_TYPES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".csv": "text/csv",
    ".html": HTML_CONTENT_TYPE,
    ".htm": HTML_CONTENT_TYPE,
    ".docx": DOCX_CONTENT_TYPE,
    ".pptx": PPTX_CONTENT_TYPE,
}
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SPACE_RE = re.compile(r"[ \t\f\v]+")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+] |\d+[.)] )(.*)$")
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class DocumentParseError(ValueError):
    """The document is recognized but cannot be parsed safely."""


class UnsupportedDocumentError(DocumentParseError):
    """The source does not use a supported document format."""


class UnsafeDocumentError(DocumentParseError):
    """The source violates local-input or archive safety rules."""


@dataclass(slots=True, frozen=True)
class ArchiveSafetyPolicy:
    """Boundaries that protect OOXML parsing from archive bombs."""

    max_entries: int = 20_000
    max_total_uncompressed_bytes: int = 1_073_741_824
    max_member_bytes: int = 268_435_456
    max_compression_ratio: float = 1_000.0


@dataclass(slots=True, frozen=True)
class DocumentProvenance:
    source_name: str
    block_index: int
    locator: str
    page_number: int | None = None
    slide_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_name": self.source_name,
            "block_index": self.block_index,
            "locator": self.locator,
        }
        if self.page_number is not None:
            result["page_number"] = self.page_number
        if self.slide_number is not None:
            result["slide_number"] = self.slide_number
        return result


@dataclass(slots=True, frozen=True)
class DocumentBlock:
    id: str
    kind: str
    text: str
    order: int
    heading_path: tuple[str, ...]
    provenance: DocumentProvenance
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "order": self.order,
            "heading_path": list(self.heading_path),
            "provenance": self.provenance.to_dict(),
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True, frozen=True)
class NormalizedDocument:
    id: str
    filename: str
    content_type: str
    title: str
    parser: str
    parser_version: str
    blocks: tuple[DocumentBlock, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(block.text for block in self.blocks if block.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "content_type": self.content_type,
            "title": self.title,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "blocks": [block.to_dict() for block in self.blocks],
            "metadata": dict(self.metadata),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def to_markdown(self) -> str:
        output: list[str] = []
        if self.title and not (
            self.blocks
            and self.blocks[0].kind == "heading"
            and self.blocks[0].text == self.title
        ):
            output.extend((f"# {self.title}", ""))
        for block in self.blocks:
            text = block.text.strip()
            if not text:
                continue
            if block.kind == "heading":
                level = max(1, min(6, int(block.metadata.get("level", 2))))
                output.append(f"{'#' * level} {text}")
            elif block.kind == "list_item":
                output.append(f"- {text}")
            elif block.kind == "code":
                output.extend(("```", text, "```"))
            else:
                output.append(text)
            output.append("")
        return "\n".join(output).rstrip() + ("\n" if output else "")


class DocumentParser(Protocol):
    name: str
    version: str
    content_types: frozenset[str]

    def parse(
        self,
        path: Path,
        *,
        document_id: str,
        content_type: str,
        archive_policy: ArchiveSafetyPolicy,
    ) -> NormalizedDocument: ...


class _BlockBuilder:
    def __init__(self, document_id: str, source_name: str) -> None:
        self.document_id = document_id
        self.source_name = source_name
        self.blocks: list[DocumentBlock] = []
        self.heading_path: list[str] = []

    def add(
        self,
        kind: str,
        text: str,
        *,
        heading_level: int | None = None,
        page_number: int | None = None,
        slide_number: int | None = None,
        locator: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        normalized = _normalize_text(text, preserve_newlines=kind in {"table", "code"})
        if not normalized:
            return
        item_metadata = dict(metadata or {})
        if kind == "heading":
            level = max(1, min(6, int(heading_level or 2)))
            del self.heading_path[level - 1 :]
            while len(self.heading_path) < level - 1:
                self.heading_path.append("")
            self.heading_path.append(normalized)
            item_metadata["level"] = level
        order = len(self.blocks)
        block_id = sha256(
            f"{self.document_id}:{order}:{kind}:{normalized}".encode("utf-8")
        ).hexdigest()[:24]
        self.blocks.append(
            DocumentBlock(
                id=block_id,
                kind=kind,
                text=normalized,
                order=order,
                heading_path=tuple(part for part in self.heading_path if part),
                provenance=DocumentProvenance(
                    source_name=self.source_name,
                    block_index=order,
                    locator=locator or f"block:{order + 1}",
                    page_number=page_number,
                    slide_number=slide_number,
                ),
                metadata=item_metadata,
            )
        )


def _normalize_text(value: str, *, preserve_newlines: bool = False) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    if preserve_newlines:
        lines = [_SPACE_RE.sub(" ", line).strip() for line in value.split("\n")]
        return "\n".join(lines).strip()
    return _SPACE_RE.sub(" ", " ".join(value.splitlines())).strip()


def _local_path(source: str | Path) -> Path:
    raw = str(source)
    if _URL_RE.match(raw):
        raise UnsafeDocumentError("Only local document paths are accepted")
    path = Path(source).expanduser()
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DocumentParseError(f"Document is not accessible: {path}") from exc
    if not path.is_file():
        raise DocumentParseError(f"Document is not a file: {path}")
    return path


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_html(sample: bytes) -> bool:
    text = sample.decode("utf-8", errors="ignore").lstrip("\ufeff\x00 \t\r\n").lower()
    return bool(
        re.match(r"<!doctype\s+html\b", text)
        or re.match(r"<html\b", text)
        or re.match(r"<(?:head|body|article|main|section)\b", text)
    )


def detect_document_type(
    source: str | Path, supplied_content_type: str | None = None
) -> str:
    """Detect a supported type using signatures before supplied metadata."""

    path = _local_path(source)
    with path.open("rb") as handle:
        sample = handle.read(8192)

    if sample.startswith(b"PK\x03\x04"):
        try:
            with ZipFile(path) as archive:
                names = set(archive.namelist())
        except BadZipFile as exc:
            raise DocumentParseError("Invalid OOXML ZIP archive") from exc
        if "word/document.xml" in names:
            return DOCX_CONTENT_TYPE
        if "ppt/presentation.xml" in names or any(
            re.fullmatch(r"ppt/slides/slide\d+\.xml", name) for name in names
        ):
            return PPTX_CONTENT_TYPE
        raise UnsupportedDocumentError("ZIP archive is not a supported DOCX or PPTX")

    if _looks_like_html(sample):
        return HTML_CONTENT_TYPE
    suffix_type = _EXTENSION_TYPES.get(path.suffix.lower())
    if suffix_type:
        return suffix_type
    normalized = (supplied_content_type or "").split(";", 1)[0].strip().lower()
    if normalized in SUPPORTED_DOCUMENT_TYPES:
        return normalized
    raise UnsupportedDocumentError(
        f"Unsupported local document format: {path.suffix or 'unknown'}"
    )


def _decode_text(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    if b"\x00" in data[:4096]:
        raise DocumentParseError("Binary data cannot be parsed as plain text")
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252"), "cp1252"


def _document(
    path: Path,
    document_id: str,
    content_type: str,
    title: str,
    parser: DocumentParser,
    builder: _BlockBuilder,
    **metadata: Any,
) -> NormalizedDocument:
    return NormalizedDocument(
        id=document_id,
        filename=path.name,
        content_type=content_type,
        title=title.strip(),
        parser=parser.name,
        parser_version=parser.version,
        blocks=tuple(builder.blocks),
        metadata=metadata,
    )


class PlainTextParser:
    name = "plain-text"
    version = "1"
    content_types = TEXT_CONTENT_TYPES

    def parse(
        self,
        path: Path,
        *,
        document_id: str,
        content_type: str,
        archive_policy: ArchiveSafetyPolicy,
    ) -> NormalizedDocument:
        text, encoding = _decode_text(path.read_bytes())
        builder = _BlockBuilder(document_id, path.name)
        paragraph: list[str] = []
        title = ""

        def flush_paragraph() -> None:
            if paragraph:
                builder.add("paragraph", " ".join(paragraph))
                paragraph.clear()

        for line in text.splitlines():
            heading = _HEADING_RE.match(line)
            list_item = _LIST_RE.match(line)
            if heading:
                flush_paragraph()
                level = len(heading.group(1))
                value = heading.group(2)
                builder.add("heading", value, heading_level=level)
                if not title:
                    title = value
            elif list_item:
                flush_paragraph()
                builder.add("list_item", list_item.group(1))
            elif not line.strip():
                flush_paragraph()
            else:
                paragraph.append(line.strip())
        flush_paragraph()
        return _document(
            path,
            document_id,
            content_type,
            title or path.stem,
            self,
            builder,
            encoding=encoding,
        )


class _HTMLExtractor(HTMLParser):
    _BLOCKS = {
        "p": ("paragraph", None),
        "li": ("list_item", None),
        "blockquote": ("quote", None),
        "pre": ("code", None),
        **{f"h{level}": ("heading", level) for level in range(1, 7)},
    }
    _IGNORED = {"script", "style", "noscript", "template", "svg", "canvas"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str, int | None]] = []
        self.title = ""
        self._ignored_depth = 0
        self._active_tag: str | None = None
        self._active_kind = ""
        self._active_level: int | None = None
        self._buffer: list[str] = []
        self._in_title = False
        self._title_buffer: list[str] = []
        self._in_table = False
        self._row: list[str] | None = None
        self._rows: list[list[str]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._IGNORED:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "table":
            self._finish_active()
            self._in_table = True
            self._rows = []
        elif self._in_table:
            if tag == "tr":
                self._row = []
            elif tag in {"td", "th"}:
                self._cell = []
            elif tag == "br" and self._cell is not None:
                self._cell.append("\n")
        elif tag in self._BLOCKS:
            self._finish_active()
            self._active_tag = tag
            self._active_kind, self._active_level = self._BLOCKS[tag]
            self._buffer = []
        elif tag == "br" and self._active_tag:
            self._buffer.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._ignored_depth:
            if tag in self._IGNORED:
                self._ignored_depth -= 1
            return
        if tag == "title":
            self._in_title = False
            self.title = _normalize_text("".join(self._title_buffer))
        elif self._in_table:
            if tag in {"td", "th"} and self._cell is not None:
                if self._row is None:
                    self._row = []
                self._row.append(_normalize_text("".join(self._cell)))
                self._cell = None
            elif tag == "tr" and self._row is not None:
                if any(self._row):
                    self._rows.append(self._row)
                self._row = None
            elif tag == "table":
                self._finish_table()
                self._in_table = False
        elif tag == self._active_tag:
            self._finish_active()

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self._title_buffer.append(data)
        elif self._in_table and self._cell is not None:
            self._cell.append(data)
        elif self._active_tag:
            self._buffer.append(data)

    def close(self) -> None:
        super().close()
        self._finish_active()
        if self._in_table:
            self._finish_table()

    def _finish_active(self) -> None:
        if not self._active_tag:
            return
        text = _normalize_text(
            "".join(self._buffer), preserve_newlines=self._active_kind == "code"
        )
        if text:
            self.items.append((self._active_kind, text, self._active_level))
        self._active_tag = None
        self._active_kind = ""
        self._active_level = None
        self._buffer = []

    def _finish_table(self) -> None:
        if not self._rows:
            return
        width = max(len(row) for row in self._rows)
        rows = [row + [""] * (width - len(row)) for row in self._rows]
        rendered = ["| " + " | ".join(row) + " |" for row in rows]
        rendered.insert(1, "| " + " | ".join("---" for _ in range(width)) + " |")
        self.items.append(("table", "\n".join(rendered), None))
        self._rows = []


class HTMLDocumentParser:
    name = "html-local"
    version = "1"
    content_types = frozenset({HTML_CONTENT_TYPE})

    def parse(
        self,
        path: Path,
        *,
        document_id: str,
        content_type: str,
        archive_policy: ArchiveSafetyPolicy,
    ) -> NormalizedDocument:
        text, encoding = _decode_text(path.read_bytes())
        extractor = _HTMLExtractor()
        try:
            extractor.feed(text)
            extractor.close()
        except Exception as exc:
            raise DocumentParseError(f"Invalid HTML document: {path.name}") from exc
        builder = _BlockBuilder(document_id, path.name)
        title = extractor.title
        for kind, value, level in extractor.items:
            builder.add(kind, value, heading_level=level)
            if not title and kind == "heading":
                title = value
        return _document(
            path,
            document_id,
            content_type,
            title or path.stem,
            self,
            builder,
            encoding=encoding,
            external_resources_loaded=False,
        )


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _attribute(element: ET.Element | None, local_name: str) -> str:
    if element is None:
        return ""
    for name, value in element.attrib.items():
        if name.rsplit("}", 1)[-1] == local_name:
            return value
    return ""


def _element_text(element: ET.Element) -> str:
    parts: list[str] = []
    for node in element.iter():
        name = _local_name(node)
        if name == "t" and node.text:
            parts.append(node.text)
        elif name == "tab":
            parts.append("\t")
        elif name in {"br", "cr"}:
            parts.append("\n")
    return _normalize_text("".join(parts))


def _paragraph_texts(element: ET.Element) -> str:
    paragraphs = [
        _element_text(node)
        for node in element.iter()
        if _local_name(node) == "p"
    ]
    return _normalize_text("\n".join(value for value in paragraphs if value))


def _validate_archive(archive: ZipFile, policy: ArchiveSafetyPolicy) -> None:
    infos = archive.infolist()
    if policy.max_entries and len(infos) > policy.max_entries:
        raise UnsafeDocumentError("OOXML archive contains too many entries")
    total = 0
    for info in infos:
        member = PurePosixPath(info.filename.replace("\\", "/"))
        if member.is_absolute() or ".." in member.parts or re.match(
            r"^[A-Za-z]:", info.filename
        ):
            raise UnsafeDocumentError("OOXML archive contains an unsafe member path")
        if info.flag_bits & 0x1:
            raise UnsafeDocumentError("Encrypted OOXML members are not supported")
        total += info.file_size
        if policy.max_member_bytes and info.file_size > policy.max_member_bytes:
            raise UnsafeDocumentError("OOXML member exceeds the safety boundary")
        ratio = info.file_size / max(1, info.compress_size)
        if (
            policy.max_compression_ratio
            and info.file_size > 1_048_576
            and ratio > policy.max_compression_ratio
        ):
            raise UnsafeDocumentError("OOXML archive has an unsafe compression ratio")
    if (
        policy.max_total_uncompressed_bytes
        and total > policy.max_total_uncompressed_bytes
    ):
        raise UnsafeDocumentError("OOXML archive exceeds the safety boundary")


def _read_xml(archive: ZipFile, member: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(member))
    except KeyError as exc:
        raise DocumentParseError(f"Required OOXML member is missing: {member}") from exc
    except ET.ParseError as exc:
        raise DocumentParseError(f"Invalid OOXML XML member: {member}") from exc


def _word_heading_level(paragraph: ET.Element) -> int | None:
    properties = paragraph.find(f"{{{_W_NS}}}pPr")
    if properties is None:
        return None
    style_name = _attribute(properties.find(f"{{{_W_NS}}}pStyle"), "val")
    match = re.search(r"(?:heading|titre)\s*([1-6])", style_name, re.IGNORECASE)
    if match:
        return int(match.group(1))
    outline = properties.find(f"{{{_W_NS}}}outlineLvl")
    value = _attribute(outline, "val")
    if value.isdigit():
        return min(6, int(value) + 1)
    return None


def _word_is_list(paragraph: ET.Element) -> bool:
    properties = paragraph.find(f"{{{_W_NS}}}pPr")
    return properties is not None and properties.find(f"{{{_W_NS}}}numPr") is not None


def _table_markdown(table: ET.Element) -> str:
    rows: list[list[str]] = []
    for row in table:
        if _local_name(row) != "tr":
            continue
        cells = [_paragraph_texts(cell) for cell in row if _local_name(cell) == "tc"]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = ["| " + " | ".join(row) + " |" for row in rows]
    lines.insert(1, "| " + " | ".join("---" for _ in range(width)) + " |")
    return "\n".join(lines)


class DOCXDocumentParser:
    name = "docx-ooxml"
    version = "1"
    content_types = frozenset({DOCX_CONTENT_TYPE})

    def parse(
        self,
        path: Path,
        *,
        document_id: str,
        content_type: str,
        archive_policy: ArchiveSafetyPolicy,
    ) -> NormalizedDocument:
        builder = _BlockBuilder(document_id, path.name)
        title = ""
        try:
            with ZipFile(path) as archive:
                _validate_archive(archive, archive_policy)
                root = _read_xml(archive, "word/document.xml")
                body = next(
                    (node for node in root.iter() if _local_name(node) == "body"),
                    None,
                )
                if body is None:
                    raise DocumentParseError("DOCX document body is missing")
                for child in body:
                    name = _local_name(child)
                    if name == "p":
                        text = _element_text(child)
                        level = _word_heading_level(child)
                        kind = (
                            "heading"
                            if level is not None
                            else "list_item"
                            if _word_is_list(child)
                            else "paragraph"
                        )
                        builder.add(kind, text, heading_level=level)
                        if not title and kind == "heading":
                            title = text
                    elif name == "tbl":
                        builder.add("table", _table_markdown(child))
                if "docProps/core.xml" in archive.namelist():
                    core = _read_xml(archive, "docProps/core.xml")
                    for node in core.iter():
                        if _local_name(node) == "title" and node.text:
                            title = _normalize_text(node.text)
                            break
        except BadZipFile as exc:
            raise DocumentParseError("Invalid DOCX archive") from exc
        return _document(
            path,
            document_id,
            content_type,
            title or path.stem,
            self,
            builder,
            archive_format="OOXML",
            external_resources_loaded=False,
        )


def _slide_sort_key(name: str) -> tuple[int, str]:
    match = re.search(r"slide(\d+)\.xml$", name)
    return (int(match.group(1)) if match else 2**31, name)


class PPTXDocumentParser:
    name = "pptx-ooxml"
    version = "1"
    content_types = frozenset({PPTX_CONTENT_TYPE})

    def parse(
        self,
        path: Path,
        *,
        document_id: str,
        content_type: str,
        archive_policy: ArchiveSafetyPolicy,
    ) -> NormalizedDocument:
        builder = _BlockBuilder(document_id, path.name)
        document_title = ""
        slide_count = 0
        try:
            with ZipFile(path) as archive:
                _validate_archive(archive, archive_policy)
                slide_names = sorted(
                    (
                        name
                        for name in archive.namelist()
                        if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                    ),
                    key=_slide_sort_key,
                )
                if not slide_names:
                    raise DocumentParseError("PPTX contains no slides")
                slide_count = len(slide_names)
                for slide_number, slide_name in enumerate(slide_names, start=1):
                    root = _read_xml(archive, slide_name)
                    slide_title = ""
                    shape_texts: list[tuple[str, bool]] = []
                    for shape in (
                        node for node in root.iter() if _local_name(node) == "sp"
                    ):
                        text = _paragraph_texts(shape)
                        if not text:
                            continue
                        placeholder_type = ""
                        for node in shape.iter():
                            if _local_name(node) == "ph":
                                placeholder_type = _attribute(node, "type")
                                break
                        is_title = placeholder_type in {"title", "ctrTitle"}
                        if is_title and not slide_title:
                            slide_title = text
                        shape_texts.append((text, is_title))
                    if slide_title:
                        builder.add(
                            "heading",
                            slide_title,
                            heading_level=2,
                            slide_number=slide_number,
                            locator=f"slide:{slide_number}:title",
                        )
                        if not document_title:
                            document_title = slide_title
                    else:
                        builder.add(
                            "heading",
                            f"Slide {slide_number}",
                            heading_level=2,
                            slide_number=slide_number,
                            locator=f"slide:{slide_number}",
                            metadata={"generated": True},
                        )
                    for text, is_title in shape_texts:
                        if not is_title:
                            builder.add(
                                "paragraph",
                                text,
                                slide_number=slide_number,
                                locator=f"slide:{slide_number}",
                            )
                    for table in (
                        node for node in root.iter() if _local_name(node) == "tbl"
                    ):
                        builder.add(
                            "table",
                            _table_markdown(table),
                            slide_number=slide_number,
                            locator=f"slide:{slide_number}:table",
                        )
                if "docProps/core.xml" in archive.namelist():
                    core = _read_xml(archive, "docProps/core.xml")
                    for node in core.iter():
                        if _local_name(node) == "title" and node.text:
                            document_title = _normalize_text(node.text)
                            break
        except BadZipFile as exc:
            raise DocumentParseError("Invalid PPTX archive") from exc
        return _document(
            path,
            document_id,
            content_type,
            document_title or path.stem,
            self,
            builder,
            archive_format="OOXML",
            slide_count=slide_count,
            external_resources_loaded=False,
        )


class DocumentParserRegistry:
    def __init__(self, parsers: Iterable[DocumentParser] = ()) -> None:
        self._parsers: dict[str, DocumentParser] = {}
        for parser in parsers:
            self.register(parser)

    def register(self, parser: DocumentParser) -> None:
        for content_type in parser.content_types:
            self._parsers[content_type] = parser

    def parser_for(self, content_type: str) -> DocumentParser:
        try:
            return self._parsers[content_type]
        except KeyError as exc:
            raise UnsupportedDocumentError(
                f"No parser registered for {content_type}"
            ) from exc

    @property
    def content_types(self) -> frozenset[str]:
        return frozenset(self._parsers)


DEFAULT_DOCUMENT_PARSERS = DocumentParserRegistry(
    (PlainTextParser(), HTMLDocumentParser(), DOCXDocumentParser(), PPTXDocumentParser())
)


def parse_document(
    source: str | Path,
    *,
    supplied_content_type: str | None = None,
    registry: DocumentParserRegistry = DEFAULT_DOCUMENT_PARSERS,
    archive_policy: ArchiveSafetyPolicy = ArchiveSafetyPolicy(),
) -> NormalizedDocument:
    """Parse one local document into GPTMOSS's deterministic common model."""

    path = _local_path(source)
    content_type = detect_document_type(path, supplied_content_type)
    parser = registry.parser_for(content_type)
    document_id = _file_digest(path)
    return parser.parse(
        path,
        document_id=document_id,
        content_type=content_type,
        archive_policy=archive_policy,
    )
