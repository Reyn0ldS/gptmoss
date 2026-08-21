from io import BytesIO
from zipfile import ZipFile

from gptmoss.core.delivery_package import render_docx
from gptmoss.core.diagrams import (
    DiagramEdge,
    DiagramNode,
    DiagramSpec,
    parse_mermaid,
    render_svg,
    validate_diagram,
)


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


def test_mermaid_parser_rejects_missing_or_empty_nodes():
    spec = parse_mermaid("graph TD\nA[API] --> B[Store]")
    assert len(spec.nodes) == 2
    assert validate_diagram(spec)["valid"]
    invalid = DiagramSpec(
        "bad",
        "Bad",
        "Bad",
        "Bad",
        nodes=[DiagramNode("a", "A")],
        edges=[DiagramEdge("a", "missing")],
    )
    assert not validate_diagram(invalid)["valid"]
    assert not validate_diagram(parse_mermaid("graph TD"))["valid"]


def test_mermaid_pie_with_two_slices_is_valid_and_embeds_in_docx():
    spec = parse_mermaid('pie title Parts\n"A" : 40\n"B" : 60')
    report = validate_diagram(spec)
    assert spec.kind == "pie"
    assert report["valid"]
    assert {node.label for node in spec.nodes} == {"A", "B"}
    first = render_svg(spec)
    assert first == render_svg(spec)
    assert "Parts" in first
    assert "A (40)" in first
    markdown = """# Dossier

```mermaid
pie title Parts
"A" : 40
"B" : 60
```
"""
    payload = render_docx(markdown, title="Dossier")
    with ZipFile(BytesIO(payload)) as archive:
        assert "word/media/diagram-1.svg" in archive.namelist()
        assert "Parts" in archive.read("word/media/diagram-1.svg").decode("utf-8")


def test_unsupported_gantt_diagram_is_named_not_empty_flowchart():
    spec = parse_mermaid("gantt\ntitle Schedule\nsection Work\nTask :done, 2024-01-01, 1d")
    report = validate_diagram(spec)
    assert spec.kind == "unsupported"
    assert report["valid"] is False
    assert "unsupported mermaid diagram type 'gantt'" in report["issues"][0]


def test_flowchart_node_named_pie_does_not_hijack_diagram_kind():
    spec = parse_mermaid("graph TD\npie[Slice node]\npie --> Store")
    assert spec.kind == "flowchart"
    assert validate_diagram(spec)["valid"]
    assert {node.node_id for node in spec.nodes} == {"pie", "Store"}


def test_mermaid_pie_accepts_percent_suffix_and_draws_a_full_circle():
    spec = parse_mermaid('pie title Only\n"A" : 100%\n"B" : 0%')
    assert spec.kind == "pie"
    assert validate_diagram(spec)["valid"]
    svg = render_svg(spec)
    assert "<circle " in svg
    assert "A (100)" in svg


def test_mermaid_pie_with_one_slice_is_invalid():
    spec = parse_mermaid('pie title Only\n"A" : 1')
    assert validate_diagram(spec)["valid"] is False


def test_mermaid_parser_supports_useful_sequence_and_state_diagrams():
    sequence = parse_mermaid(
        "sequenceDiagram\nparticipant User as Utilisateur\nUser->>API: requête\nAPI-->>User: réponse"
    )
    state = parse_mermaid(
        "stateDiagram-v2\n[*] --> Idle\nIdle --> Running: start\nRunning --> [*]: stop"
    )

    assert validate_diagram(sequence)["valid"]
    assert {node.node_id for node in sequence.nodes} == {"User", "API"}
    assert len(sequence.edges) == 2
    assert validate_diagram(state)["valid"]
    assert len(state.edges) == 3


def test_docx_contains_real_table_and_embedded_svg():
    markdown = """# Dossier

| Élément | Valeur |
| --- | --- |
| API | local |

```mermaid
graph TD
A[API] --> B[Store]
```
"""
    payload = render_docx(markdown, title="Dossier")
    with ZipFile(BytesIO(payload)) as archive:
        names = set(archive.namelist())
        assert "word/document.xml" in names
        assert "word/media/diagram-1.svg" in names
        document = archive.read("word/document.xml").decode("utf-8")
        assert "<w:tbl>" in document
        assert 'r:embed="rId2"' in document


def test_docx_multiple_diagrams_have_unique_ids_and_accessible_descriptions():
    markdown = """# Dossier
```mermaid
graph TD
A[API] --> B[Store]
```
```mermaid
graph TD
C[Client] --> D[Service]
```
"""
    with ZipFile(BytesIO(render_docx(markdown, title="Dossier"))) as archive:
        names = set(archive.namelist())
        assert {"word/media/diagram-1.svg", "word/media/diagram-2.svg"} <= names
        document = archive.read("word/document.xml").decode("utf-8")
        assert 'wp:docPr id="1"' in document
        assert 'wp:docPr id="2"' in document
        assert 'pic:cNvPr id="1"' in document
        assert 'pic:cNvPr id="2"' in document
        assert document.count(" descr=") == 2
        relationships = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert 'Id="rId2"' in relationships and 'Id="rId3"' in relationships


def test_docx_diagram_feature_flags_change_the_rendered_delivery():
    markdown = """# Dossier
```mermaid
graph TD
A[API] --> B[Store]
```
"""
    for options, expected_text in (
        ({"render_diagrams": False}, "Diagram source (rendering disabled)"),
        ({"embed_diagrams": False}, "Diagram:"),
    ):
        with ZipFile(BytesIO(render_docx(markdown, title="Dossier", **options))) as archive:
            assert not any(name.startswith("word/media/") for name in archive.namelist())
            document = archive.read("word/document.xml").decode("utf-8")
            assert expected_text in document
