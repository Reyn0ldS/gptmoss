"""Deterministic, dependency-free quality checks for local text deliverables."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Sequence, Tuple


ValidationReport = Dict[str, Any]
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)\s*#*\s*$")
_LOCAL_REFERENCE = re.compile(r"\[([^\[\]\n]+?)\s+>\s+([^\[\]\n]+?)\]")
_EXTERNAL_LINK = re.compile(r"(?i)(?:https?://|ftp://|www\.)\S+")
_DEFAULT_PLACEHOLDERS = (
    r"\bTODO\b",
    r"\bTBD\b",
    r"\bFIXME\b",
    r"\bXXX\b",
    r"\blorem\s+ipsum\b",
    r"\b(?:a|à)\s+compl(?:e|é)ter\b",
    r"\[(?:insert|placeholder|complete|compl(?:e|é)ter)[^\]]*\]",
    r"(?im)^\s*(?:[-*]\s*)?(?:[^:\n]{1,80}:\s*)?(?:\.\.\.|â¦)\s*$",
    r"(?i)</?think>",
)


def _failure(report: ValidationReport, message: str) -> None:
    report["valid"] = False
    report.setdefault("failures", []).append(message)


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value))
    return " ".join(
        "".join(character for character in decomposed if not unicodedata.combining(character))
        .casefold()
        .split()
    )


def _strings(value: Any, name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{name} must be a list of strings")
    return [item.strip() for item in value if item.strip()]


def _positive_int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    result = int(value)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _compact_integer_ranges(values: Sequence[int]) -> str:
    """Render every integer exactly once as concise contiguous ranges."""
    ordered = sorted({int(value) for value in values})
    if not ordered:
        return ""
    spans: List[str] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value == previous + 1:
            previous = value
            continue
        spans.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    spans.append(str(start) if start == previous else f"{start}-{previous}")
    return ", ".join(spans)


def _without_markdown_code(text: str) -> str:
    """Exclude fenced and inline code examples from evidence detection."""
    visible: List[str] = []
    in_fence = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            visible.append("")
            continue
        if in_fence:
            visible.append("")
            continue
        visible.append(re.sub(r"`+[^`\n]*`+", "", line))
    return "\n".join(visible)


def _words(value: str) -> List[str]:
    return re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", value, flags=re.UNICODE)


def _contains_identifier(text: str, identifier: str) -> bool:
    return bool(
        re.search(
            r"(?<!\w)" + re.escape(_fold(identifier)) + r"(?!\w)",
            _fold(text),
        )
    )


def _headings(lines: Sequence[str]) -> List[Tuple[int, int, str]]:
    found = []
    for line_number, line in enumerate(lines):
        match = _HEADING.match(line)
        if match:
            found.append((line_number, len(match.group(1)), match.group(2).strip()))
    return found


def _section_text(
    lines: Sequence[str], headings: Sequence[Tuple[int, int, str]], index: int
) -> str:
    start, level, _ = headings[index]
    end = len(lines)
    for next_start, next_level, _ in headings[index + 1 :]:
        if next_level <= level:
            end = next_start
            break
    content = []
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if not stripped or re.fullmatch(r"\|?[\s:|-]+\|?", stripped):
            continue
        if _HEADING.match(stripped):
            continue
        content.append(stripped)
    return "\n".join(content)


def _paragraphs(text: str) -> List[str]:
    paragraphs = []
    buffer: List[str] = []
    in_fence = False
    for line in text.splitlines() + [""]:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            if buffer:
                paragraphs.append(" ".join(buffer))
                buffer = []
            continue
        if in_fence or _HEADING.match(stripped):
            continue
        if not stripped:
            if buffer:
                paragraphs.append(" ".join(buffer))
                buffer = []
            continue
        if stripped.startswith("|") or re.match(r"^[-*+]\s+", stripped):
            if buffer:
                paragraphs.append(" ".join(buffer))
                buffer = []
            continue
        buffer.append(stripped)
    return paragraphs


def _normalized_paragraph(value: str) -> str:
    without_references = _LOCAL_REFERENCE.sub("", value)
    return " ".join(re.findall(r"[^\W_]+", _fold(without_references)))


def _normalize_source(value: str) -> str:
    return value.strip().replace(chr(92), "/").casefold()


def _is_safe_local_source(value: str) -> bool:
    normalized = value.strip().replace(chr(92), "/")
    path = PurePosixPath(normalized)
    return bool(
        normalized
        and "://" not in normalized
        and not normalized.startswith("/")
        and not re.match(r"^[A-Za-z]:", normalized)
        and ".." not in path.parts
    )


def _locator_range(locator: str) -> Tuple[str, int, int] | None:
    block_match = re.search(
        r"(?i)\bbloc(?:k)?s?\s+(\d+)(?:\s*[-–]\s*(\d+))?", locator
    )
    if block_match:
        start = int(block_match.group(1))
        return "blocks", start, int(block_match.group(2) or start)
    slide_match = re.search(
        r"(?i)\b(?:slide|diapositive)s?\s+(\d+)(?:\s*[-–]\s*(\d+))?",
        locator,
    )
    if slide_match:
        start = int(slide_match.group(1))
        return "slides", start, int(slide_match.group(2) or start)
    return None


def _validate_reference_locator(
    source: str,
    locator: str,
    inventory: Dict[str, Any],
    require_bounds: bool,
) -> str | None:
    details = inventory.get(_normalize_source(source))
    if details is None:
        return f"reference to {source!r} has no source inventory entry" if require_bounds else None
    if isinstance(details, int):
        details = {"blocks": details}
    if not isinstance(details, dict):
        raise TypeError("source_inventory values must be integers or objects")
    span = _locator_range(locator)
    if require_bounds and not span:
        return f"reference to {source!r} lacks a block or slide locator"
    if span:
        unit, start, last = span
        if unit not in details:
            return f"reference to {source!r} uses {unit} but its inventory has no {unit} count"
        if start < 1 or last < start or last > int(details[unit]):
            maximum = int(details[unit])
            return (
                f"reference to {source!r} uses invalid {unit} range {start}-{last}; "
                f"expected 1-{maximum}"
            )
    return None


def validate_document(path: Path, constraints: Dict[str, Any]) -> ValidationReport:
    """Validate a UTF-8 Markdown or text deliverable against a declared policy."""
    report: ValidationReport = {
        "validator": "document",
        "valid": True,
        "failures": [],
        "warnings": [],
    }
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as error:
        _failure(report, f"cannot read UTF-8 document: {error}")
        report["metrics"] = {}
        return report

    lines = text.splitlines()
    headings = _headings(lines)
    paragraphs = _paragraphs(text)
    evidence_text = _without_markdown_code(text)
    all_reference_count = len(list(_LOCAL_REFERENCE.finditer(text)))
    references = [
        {"source": match.group(1).strip(), "locator": match.group(2).strip()}
        for match in _LOCAL_REFERENCE.finditer(evidence_text)
    ]
    code_reference_count = max(0, all_reference_count - len(references))
    external_links = _EXTERNAL_LINK.findall(text)
    required_headings = _strings(constraints.get("required_headings"), "required_headings")
    required_ids = _strings(
        constraints.get("required_requirement_ids"), "required_requirement_ids"
    )
    traceability_ids = _strings(
        constraints.get("required_traceability_ids"), "required_traceability_ids"
    )
    required_sources = _strings(
        constraints.get("required_source_files"), "required_source_files"
    )
    min_section_words = _positive_int(
        constraints.get("min_section_words"), "min_section_words", 1
    )
    duplicate_min_words = _positive_int(
        constraints.get("duplicate_min_words"), "duplicate_min_words", 12
    )
    max_duplicates = _positive_int(
        constraints.get("max_duplicate_paragraphs"), "max_duplicate_paragraphs", 0
    )
    claim_min_words = _positive_int(
        constraints.get("claim_min_words"), "claim_min_words", 20
    )
    placeholder_patterns = constraints.get("placeholder_patterns")
    if placeholder_patterns is None:
        placeholder_patterns = list(_DEFAULT_PLACEHOLDERS)
    else:
        placeholder_patterns = _strings(placeholder_patterns, "placeholder_patterns")

    folded_headings: Dict[str, List[int]] = {}
    for index, (_, _, title) in enumerate(headings):
        folded_headings.setdefault(_fold(title), []).append(index)
    missing_headings = []
    empty_headings = []
    for title in required_headings:
        matches = folded_headings.get(_fold(title), [])
        if not matches:
            missing_headings.append(title)
            continue
        section_versions = []
        for index in matches:
            section = _section_text(lines, headings, index)
            for pattern in placeholder_patterns:
                section = re.sub(pattern, "", section, flags=re.IGNORECASE)
            section_versions.append(section)
        if not any(len(_words(section)) >= min_section_words for section in section_versions):
            empty_headings.append(title)
    if missing_headings:
        _failure(report, "missing required heading(s): " + ", ".join(missing_headings))
    if empty_headings:
        _failure(report, "empty required section(s): " + ", ".join(empty_headings))

    missing_ids = [
        identifier for identifier in required_ids if not _contains_identifier(text, identifier)
    ]
    if missing_ids:
        _failure(report, "missing requirement ID(s): " + ", ".join(missing_ids))

    table_lines = [line for line in lines if line.strip().startswith("|")]
    missing_traceability = [
        identifier
        for identifier in traceability_ids
        if not _contains_identifier("\n".join(table_lines), identifier)
    ]
    if missing_traceability:
        _failure(
            report,
            "requirement ID(s) absent from Markdown traceability tables: "
            + ", ".join(missing_traceability),
        )

    placeholder_hits = []
    if constraints.get("forbid_placeholders", False):
        for pattern in placeholder_patterns:
            matches = re.findall(pattern, text, flags=re.IGNORECASE)
            placeholder_hits.extend(str(match) for match in matches)
        if placeholder_hits:
            _failure(
                report,
                f"document contains {len(placeholder_hits)} placeholder marker(s)",
            )

    if constraints.get("forbid_external_links", False) and external_links:
        _failure(report, f"document contains {len(external_links)} external link(s)")

    normalized_paragraphs = [
        _normalized_paragraph(paragraph)
        for paragraph in paragraphs
        if len(_words(paragraph)) >= duplicate_min_words
    ]
    counts = Counter(paragraph for paragraph in normalized_paragraphs if paragraph)
    duplicate_count = sum(count - 1 for count in counts.values() if count > 1)
    if "max_duplicate_paragraphs" in constraints and duplicate_count > max_duplicates:
        duplicate_samples = [
            paragraph[:120] for paragraph, count in counts.items() if count > 1
        ]
        _failure(
            report,
            f"document contains {duplicate_count} duplicate paragraph occurrence(s); "
            f"maximum is {max_duplicates}; repeated paragraph prefix(es): "
            + "; ".join(duplicate_samples[:5]),
        )

    allowed_sources = {_normalize_source(source) for source in required_sources}
    cited_sources = {_normalize_source(reference["source"]) for reference in references}
    invalid_references = []
    raw_inventory = constraints.get("source_inventory") or {}
    if not isinstance(raw_inventory, dict):
        raise TypeError("source_inventory must be an object")
    inventory = {_normalize_source(str(key)): value for key, value in raw_inventory.items()}
    require_bounds = bool(constraints.get("require_bounded_references"))
    for reference in references:
        source = reference["source"]
        if not _is_safe_local_source(source):
            invalid_references.append(f"unsafe or non-local source {source!r}")
            continue
        if allowed_sources and _normalize_source(source) not in allowed_sources:
            invalid_references.append(f"undeclared local source {source!r}")
            continue
        locator_failure = _validate_reference_locator(
            source, reference["locator"], inventory, require_bounds
        )
        if locator_failure:
            invalid_references.append(locator_failure)
    if invalid_references:
        _failure(report, "invalid local reference(s): " + "; ".join(invalid_references[:10]))
    missing_sources = [
        source for source in required_sources if _normalize_source(source) not in cited_sources
    ]
    if missing_sources:
        message = "uncited required source file(s): " + ", ".join(missing_sources)
        if code_reference_count:
            message += (
                f"; {code_reference_count} citation-like pattern(s) inside Markdown code do not "
                "count as evidence; write actual citations without backticks or code fences"
            )
        _failure(report, message)
    if constraints.get("require_local_references") and not references:
        message = "document contains no local source reference"
        if code_reference_count:
            message += (
                "; citation-like patterns inside Markdown code are examples, not evidence; "
                "write actual citations without backticks or code fences"
            )
        _failure(report, message)

    source_units_covered = 0
    source_units_total = 0
    if constraints.get("require_source_coverage"):
        coverage_failures = []
        references_by_source: Dict[str, List[Dict[str, str]]] = {}
        for reference in references:
            references_by_source.setdefault(
                _normalize_source(reference["source"]), []
            ).append(reference)
        for source, details in inventory.items():
            if isinstance(details, int):
                details = {"blocks": details}
            if not isinstance(details, dict):
                continue
            unit = "slides" if "slides" in details else "blocks"
            total = int(details.get(unit) or 0)
            expected = set(range(1, total + 1))
            covered = set()
            for reference in references_by_source.get(source, []):
                span = _locator_range(reference["locator"])
                if not span or span[0] != unit:
                    continue
                covered.update(range(span[1], span[2] + 1))
            missing_units = sorted(expected - covered)
            source_units_covered += len(expected & covered)
            source_units_total += len(expected)
            if missing_units:
                display = _compact_integer_ranges(missing_units)
                coverage_failures.append(
                    f"{source} has uncovered required {unit}: {display}; "
                    "add bounded local reference(s) covering these exact ranges"
                )
        if coverage_failures:
            _failure(
                report,
                "incomplete source coverage: " + "; ".join(coverage_failures[:10]),
            )

    unsupported_claims = []
    if constraints.get("require_claim_references"):
        for paragraph in paragraphs:
            if len(_words(paragraph)) >= claim_min_words and not _LOCAL_REFERENCE.search(paragraph):
                unsupported_claims.append(" ".join(paragraph.split())[:120])
        if unsupported_claims:
            _failure(
                report,
                f"{len(unsupported_claims)} material paragraph(s) lack a local reference: "
                + "; ".join(unsupported_claims[:5]),
            )

    terminology = constraints.get("terminology") or {}
    if not isinstance(terminology, dict):
        raise TypeError("terminology must be an object mapping canonical terms to variants")
    forbidden_variants = []
    for canonical, variants in terminology.items():
        for variant in _strings(variants, f"terminology.{canonical}"):
            if re.search(r"(?<!\w)" + re.escape(variant) + r"(?!\w)", text, re.IGNORECASE):
                forbidden_variants.append(f"{variant!r} (use {str(canonical)!r})")
    if forbidden_variants:
        _failure(report, "inconsistent terminology: " + ", ".join(forbidden_variants))

    heading_levels = [level for _, level, _ in headings]
    skipped_levels = sum(
        current > previous + 1
        for previous, current in zip(heading_levels, heading_levels[1:])
    )
    if skipped_levels:
        report["warnings"].append(
            f"Markdown heading hierarchy skips {skipped_levels} level(s)"
        )

    metrics = {
        "characters": len(text),
        "words": len(_words(text)),
        "lines": len(lines),
        "headings": len(headings),
        "paragraphs": len(paragraphs),
        "local_references": len(references),
        "cited_sources": len(cited_sources),
        "external_links": len(external_links),
        "placeholder_markers": len(placeholder_hits),
        "duplicate_paragraphs": duplicate_count,
        "unsupported_claim_paragraphs": len(unsupported_claims),
        "required_headings_covered": len(required_headings) - len(missing_headings) - len(empty_headings),
        "required_headings_total": len(required_headings),
        "requirement_ids_covered": len(required_ids) - len(missing_ids),
        "requirement_ids_total": len(required_ids),
        "traceability_ids_covered": len(traceability_ids) - len(missing_traceability),
        "traceability_ids_total": len(traceability_ids),
        "required_sources_cited": len(required_sources) - len(missing_sources),
        "required_sources_total": len(required_sources),
        "source_units_covered": source_units_covered,
        "source_units_total": source_units_total,
        "empty_required_sections": len(empty_headings),
        "invalid_local_references": len(invalid_references),
        "uncited_required_sources": len(missing_sources),
    }
    report["metrics"] = metrics
    for metric, minimum in (constraints.get("minimums") or {}).items():
        value = metrics.get(metric)
        if not isinstance(value, (int, float)) or value < minimum:
            _failure(report, f"{metric}={value!r} is below required minimum {minimum!r}")
    for metric, maximum in (constraints.get("maximums") or {}).items():
        value = metrics.get(metric)
        if not isinstance(value, (int, float)) or value > maximum:
            _failure(report, f"{metric}={value!r} exceeds required maximum {maximum!r}")
    return report


def format_quality_report(report: ValidationReport) -> str:
    """Render a compact Markdown synthesis of a document quality report."""
    status = "PASS" if report.get("valid") else "FAIL"
    lines = [f"# Document quality report — {status}", "", f"- File: `{report.get('path', '')}`"]
    if report.get("sha256"):
        lines.append(f"- SHA-256: `{report['sha256']}`")
    lines.extend(["", "## Metrics", "", "| Metric | Value |", "|---|---:|"])
    for name, value in sorted((report.get("metrics") or {}).items()):
        lines.append(f"| {name} | {value} |")
    for title, key in (("Failures", "failures"), ("Warnings", "warnings")):
        lines.extend(["", f"## {title}", ""])
        values = report.get(key) or []
        lines.extend(f"- {value}" for value in values)
        if not values:
            lines.append("- None")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a local Markdown or text deliverable.")
    parser.add_argument("document", type=Path)
    parser.add_argument("--constraints", type=Path, help="UTF-8 JSON quality policy")
    parser.add_argument("--json", dest="json_output", type=Path, help="write the complete JSON report")
    parser.add_argument("--markdown", type=Path, help="write a Markdown report synthesis")
    arguments = parser.parse_args(argv)
    constraints: Dict[str, Any] = {}
    if arguments.constraints:
        constraints = json.loads(arguments.constraints.read_text(encoding="utf-8"))
        if not isinstance(constraints, dict):
            raise TypeError("constraints JSON root must be an object")
    from gptmoss.core.artifact_validation import validate_artifact

    report = validate_artifact(arguments.document, validator="document", constraints=constraints)
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if arguments.json_output:
        arguments.json_output.write_text(encoded, encoding="utf-8")
    if arguments.markdown:
        arguments.markdown.write_text(format_quality_report(report), encoding="utf-8")
    if not arguments.json_output:
        print(encoded, end="")
    return 0 if report.get("valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
