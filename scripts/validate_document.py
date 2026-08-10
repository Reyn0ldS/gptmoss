"""Portable source-tree entry point for deterministic document validation."""

from __future__ import annotations

import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from gptmoss.core.document_quality import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
