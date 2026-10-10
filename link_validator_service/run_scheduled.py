#!/usr/bin/env python3
"""DEPRECATED Railway entrypoint.

This process only delegates to the JobReady daily runner.
It does not require CodeQuest and it does not prune the catalog.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "codequest_jobs"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from run_jobready_daily import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
