import subprocess
from pathlib import Path
from types import SimpleNamespace

from scripts.browser_layout_audit import audit_case


def test_edge_audit_retries_one_bounded_cold_start_timeout(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '<body data-layout-global-overflow="false" '
                'data-layout-offender-count="0"></body>'
            ),
            stderr="",
        )

    monkeypatch.setattr("scripts.browser_layout_audit.subprocess.run", fake_run)

    report = audit_case(
        Path("msedge.exe"),
        "http://127.0.0.1:8000/",
        360,
        740,
        1.5,
        "content",
        timeout_seconds=3,
        attempts=2,
    )

    assert len(calls) == 2
    assert all(call[1]["timeout"] == 3 for call in calls)
    assert report["attempts"] == 2
    assert report["timed_out"] is False
    assert report["audit_present"] is True
    assert report["global_horizontal_overflow"] is False
    assert report["offender_count"] == 0
