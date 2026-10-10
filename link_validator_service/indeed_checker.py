"""DEPRECATED. Load the canonical Indeed checker.

Do not edit a second copy of the GraphQL client in this folder.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parents[1] / "codequest_jobs" / "indeed_checker.py"
_spec = importlib.util.spec_from_file_location("jobready_indeed_checker", _path)
if _spec is None or _spec.loader is None:
    raise ImportError("canonical Indeed checker is missing")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

indeed_headers = _mod.indeed_headers
indeed_jk_from_url = _mod.indeed_jk_from_url
batch_indeed_expired = _mod.batch_indeed_expired
check_indeed_via_api = _mod.check_indeed_via_api
