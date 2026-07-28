"""Offline planning benchmark for complex GPTMOSS delivery requests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gptmoss.core.delivery import build_delivery_contract
from gptmoss.core.execution import normalize_plan
from gptmoss.planners.simple import SimplePlanner, analyze_task_complexity


def evaluate_prompt(identifier: str, prompt: str) -> Dict[str, Any]:
    analysis = analyze_task_complexity(prompt)
    plan = normalize_plan(SimplePlanner._fallback_plan(prompt, analysis))
    contract = build_delivery_contract(plan, prompt)
    steps = plan["steps"]
    specialists = [str(step.get("specialist") or "") for step in steps]
    mandatory_rows = [
        row for row in contract["traceability"] if row.get("mandatory")
    ]
    violations: List[str] = []
    if analysis["level"] in {"high", "very_high"}:
        if len(steps) < analysis["suggested_min_steps"]:
            violations.append("undersized_plan")
        if len(set(specialists)) < max(6, len(steps) * 3 // 4):
            violations.append("generic_specialist_reuse")
        if not any(step.get("role") == "debugger" for step in steps):
            violations.append("missing_autonomous_repair")
        if not steps or steps[-1].get("role") != "coordinator":
            violations.append("missing_final_auditor")
    if any(not row.get("implementation_steps") for row in mandatory_rows):
        violations.append("requirement_without_implementation")
    if any(not row.get("validation_steps") for row in mandatory_rows):
        violations.append("requirement_without_independent_validation")
    artifact_steps = [step for step in steps if step.get("required_artifacts")]
    if any(not step.get("owned_paths") for step in artifact_steps):
        violations.append("artifact_without_owner")
    return {
        "id": identifier,
        "level": analysis["level"],
        "domains": analysis["domains"],
        "steps": len(steps),
        "specialists": len(set(specialists)),
        "requirements": len(contract["requirements"]),
        "traceability_coverage": (
            sum(
                bool(row.get("implementation_steps")) and bool(row.get("validation_steps"))
                for row in mandatory_rows
            ) / max(1, len(mandatory_rows))
        ),
        "ownership_claims": len(contract["ownership"]),
        "violations": sorted(set(violations)),
        "passed": not violations,
    }


def run_benchmark(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = [
        evaluate_prompt(str(item["id"]), str(item["prompt"]))
        for item in payload.get("prompts", [])
    ]
    return {
        "schema_version": 1,
        "benchmark": str(path),
        "passed": bool(results) and all(result["passed"] for result in results),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompts",
        type=Path,
        default=PROJECT_ROOT / "benchmarks" / "complex_prompts.json",
    )
    parser.add_argument("--compact", action="store_true")
    arguments = parser.parse_args()
    report = run_benchmark(arguments.prompts.resolve())
    print(json.dumps(
        report,
        ensure_ascii=False,
        indent=None if arguments.compact else 2,
    ))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
