"""Run deterministic offline quality benchmarks against professional documents."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gptmoss.core.document_quality import validate_document


def evaluate_case(case: dict[str, Any], workspace: Path) -> dict[str, Any]:
    identifier = str(case["id"])
    document = workspace / f"{identifier}.md"
    document.write_text(str(case["document"]), encoding="utf-8")
    report = validate_document(document, dict(case.get("constraints") or {}))
    failures = "\n".join(str(item) for item in report["failures"])
    expected_valid = bool(case["expected_valid"])
    expected_markers = [str(item) for item in case.get("expected_failure_markers", [])]
    missing_markers = [marker for marker in expected_markers if marker not in failures]
    classification_matches = bool(report["valid"]) == expected_valid
    return {
        "id": identifier,
        "expected_valid": expected_valid,
        "actual_valid": bool(report["valid"]),
        "classification_matches": classification_matches,
        "missing_failure_markers": missing_markers,
        "passed": classification_matches and not missing_markers,
        "metrics": report.get("metrics", {}),
        "failures": report["failures"],
    }


def _run(path: Path, workspace: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = [evaluate_case(case, workspace) for case in payload.get("cases", [])]
    positive = [result for result in results if result["expected_valid"]]
    negative = [result for result in results if not result["expected_valid"]]
    true_accepts = sum(result["actual_valid"] for result in positive)
    true_rejects = sum(not result["actual_valid"] for result in negative)
    return {
        "schema_version": 1,
        "benchmark": str(path),
        "cases": len(results),
        "valid_recall": true_accepts / max(1, len(positive)),
        "defect_recall": true_rejects / max(1, len(negative)),
        "false_accept_rate": (len(negative) - true_rejects) / max(1, len(negative)),
        "passed": bool(results) and all(result["passed"] for result in results),
        "results": results,
    }


def run_benchmark(path: Path, workspace: Path | None = None) -> dict[str, Any]:
    if workspace is not None:
        workspace.mkdir(parents=True, exist_ok=True)
        return _run(path.resolve(), workspace.resolve())
    with tempfile.TemporaryDirectory(prefix="gptmoss-quality-") as directory:
        return _run(path.resolve(), Path(directory))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases",
        type=Path,
        default=PROJECT_ROOT / "benchmarks" / "document_quality_cases.json",
    )
    parser.add_argument("--compact", action="store_true")
    arguments = parser.parse_args()
    report = run_benchmark(arguments.cases)
    print(json.dumps(report, ensure_ascii=False, indent=None if arguments.compact else 2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
