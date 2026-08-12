from zipfile import ZipFile
from io import BytesIO

from gptmoss.core.diagrams import DiagramEdge, DiagramNode, DiagramSpec, parse_mermaid, render_svg, validate_diagram
from gptmoss.core.delivery_package import render_docx


def test_diagram_semantics_and_renderer_are_deterministic():
    spec = DiagramSpec(
        "architecture",
        "Architecture",
        "Data flow",
        "Architecture data flow",
        nodes=[DiagramNode("api", "API", zone="edge"), DiagramNode("store", "Store", zone="core")],
        edges=[DiagramEdge("api", "store", "writes", trust_boundary=True)],
    )
    assert validate_diagram(spec)["valid"]
    assert render_svg(spec) == render_svg(spec)
    assert "Architecture" in render_svg(spec)


def test_mermaid_parser_rejects_missing_nodes():
    spec = parse_mermaid("graph TD\nA[API] --> B[Store]")
    assert len(spec.nodes) == 2
    assert validate_diagram(spec)["valid"]
    invalid = DiagramSpec("bad", "Bad", "Bad", "Bad", nodes=[DiagramNode("a", "A")], edges=[DiagramEdge("a", "missing")])
    assert not validate_diagram(invalid)["valid"]


def test_docx_contains_real_table_and_embedded_svg():
    markdown = """# Dossier\n\n| Élément | Valeur |\n| --- | --- |\n| API | local |\n\n```mermaid\ngraph TD\nA[API] --> B[Store]\n```\n"""
    payload = render_docx(markdown, title="Dossier")
    with ZipFile(BytesIO(payload)) as archive:
        names = set(archive.namelist())
        assert "word/document.xml" in names
        assert "word/media/diagram-1.svg" in names
        document = archive.read("word/document.xml").decode("utf-8")
        assert "<w:tbl>" in document
        assert "r:embed=\"rId2\"" in document
