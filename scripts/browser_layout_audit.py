"""Run GPTMOSS's in-page overflow audit in real Microsoft Edge."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


EDGE_CANDIDATES = (
    Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
)
VIEWPORTS = ((360, 740), (480, 800), (768, 900), (1024, 768), (1366, 768), (1920, 1080))
SCALE_FACTORS = (1.0, 1.25, 1.5, 2.0)


def find_edge() -> Path:
    from_path = shutil.which("msedge") or shutil.which("msedge.exe")
    if from_path:
        return Path(from_path)
    for candidate in EDGE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Microsoft Edge executable was not found.")


def audit_case(
    edge: Path, url: str, width: int, height: int, scale: float, scenario: str
) -> dict:
    separator = "&" if "?" in url else "?"
    case_url = url if scenario == "empty" else f"{url}{separator}layout_audit={scenario}"
    with tempfile.TemporaryDirectory(prefix="gptmoss-edge-") as profile:
        result = subprocess.run(
            [
                str(edge),
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--no-first-run",
                "--disable-extensions",
                f"--user-data-dir={profile}",
                f"--window-size={width},{height}",
                f"--force-device-scale-factor={scale}",
                "--virtual-time-budget=2500",
                "--dump-dom",
                case_url,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    overflow = re.search(r'data-layout-global-overflow="([^"]+)"', result.stdout)
    offenders = re.search(r'data-layout-offender-count="([^"]+)"', result.stdout)
    return {
        "viewport": [width, height],
        "scale_factor": scale,
        "scenario": scenario,
        "browser_exit_code": result.returncode,
        "audit_present": bool(overflow and offenders),
        "global_horizontal_overflow": overflow.group(1) == "true" if overflow else None,
        "offender_count": int(offenders.group(1)) if offenders else None,
        "stderr": result.stderr[-2_000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:8000/")
    parser.add_argument("--quick", action="store_true", help="Use representative cases instead of the full matrix.")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    edge = find_edge()
    cases = (
        [
            (360, 740, 1.5, "content"),
            (480, 800, 2.0, "approval"),
            (768, 900, 1.25, "settings"),
            (1024, 768, 1.5, "library"),
            (1024, 768, 1.25, "server"),
            (1366, 768, 1.0, "content"),
            (1920, 1080, 2.0, "empty"),
        ]
        if arguments.quick
        else (
            [
                (width, height, scale, "empty")
                for width, height in VIEWPORTS for scale in SCALE_FACTORS
            ]
            + [
                (width, height, scale, scenario)
                for width, height, scale in (
                    (360, 740, 1.5),
                    (480, 800, 2.0),
                    (768, 900, 1.25),
                    (1024, 768, 1.5),
                    (1366, 768, 1.0),
                    (1920, 1080, 2.0),
                )
                for scenario in ("content", "approval", "settings", "library", "server")
            ]
        )
    )
    reports = [
        audit_case(edge, arguments.url, width, height, scale, scenario)
        for width, height, scale, scenario in cases
    ]
    payload = {
        "browser": str(edge),
        "url": arguments.url,
        "passed": all(
            report["browser_exit_code"] == 0
            and report["audit_present"]
            and report["global_horizontal_overflow"] is False
            and report["offender_count"] == 0
            for report in reports
        ),
        "cases": reports,
    }
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    print(rendered)
    if arguments.output:
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
