#!/usr/bin/env python3
"""RETIRED. Delegates to codequest_jobs/push_validated_to_job_ready.py, which
refuses every invocation without contacting a database.
"""

from __future__ import annotations

import runpy
from pathlib import Path

RETIRED_SCRIPT = Path(__file__).resolve().parents[1] / "codequest_jobs" / "push_validated_to_job_ready.py"


if __name__ == "__main__":
    runpy.run_path(str(RETIRED_SCRIPT), run_name="__main__")
