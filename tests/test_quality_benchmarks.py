import json
from pathlib import Path

from scripts.run_quality_benchmarks import run_benchmark


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_professional_document_quality_corpus_has_no_false_accepts(tmp_path):
    path = PROJECT_ROOT / "benchmarks" / "document_quality_cases.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    report = run_benchmark(path, tmp_path)

    assert len(payload["cases"]) >= 6
    assert any(case["expected_valid"] for case in payload["cases"])
    assert any(not case["expected_valid"] for case in payload["cases"])
    assert report["passed"], json.dumps(report, ensure_ascii=False, indent=2)
    assert report["valid_recall"] == 1
    assert report["defect_recall"] == 1
    assert report["false_accept_rate"] == 0
