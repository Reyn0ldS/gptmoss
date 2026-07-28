import json
from pathlib import Path

from scripts.run_delivery_benchmarks import run_benchmark


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_complex_prompt_benchmark_covers_multiple_domains():
    path = PROJECT_ROOT / "benchmarks" / "complex_prompts.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    report = run_benchmark(path)

    assert len(payload["prompts"]) >= 6
    assert report["passed"], json.dumps(report, indent=2, ensure_ascii=False)
    assert all(result["traceability_coverage"] == 1 for result in report["results"])
    assert len({
        domain
        for result in report["results"]
        for domain in result["domains"]
    }) >= 4

