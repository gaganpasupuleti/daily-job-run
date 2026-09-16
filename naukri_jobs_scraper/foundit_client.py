"""Foundit.in jobs via Google JobSpy (site:foundit.in) — no Apify."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from jobspy_client import scrape_jobs_safe
from normalize import UnifiedJob, from_jobspy_row, source_job_id

logger = logging.getLogger(__name__)

FOUNDIT_HOSTS = ("foundit.in", "monsterindia.com")


def _is_foundit_url(url: str) -> bool:
    host = (urlparse(url).netloc or "").lower()
    return any(h in host for h in FOUNDIT_HOSTS)


def _row_as_foundit(row: dict[str, Any]) -> dict[str, Any]:
    """Force source=foundit when the listing URL is on foundit/monster."""
    job_url = str(row.get("jobUrl") or row.get("job_url") or "").strip()
    if not job_url:
        return row
    source = "foundit"
    row = dict(row)
    row["source"] = source
    row["id"] = source_job_id(source, job_url)
    return row


def scrape_foundit_safe(
    *,
    search_term: str,
    results_wanted: int = 10,
    hours_old: int = 72,
) -> tuple[list[UnifiedJob], list[str]]:
    """
    Pull Foundit listings using Google Jobs search (site:foundit.in).
    Never raises.
    """
    term = (search_term or "").strip()
    if not term:
        return [], []

    queries = [
        f"{term} site:foundit.in",
        f"{term} foundit india fresher",
    ]
    errors: list[str] = []
    seen: set[str] = set()
    out: list[UnifiedJob] = []

    for query in queries:
        rows, _breakdown, errs = scrape_jobs_safe(
            search_term=query,
            results_wanted=results_wanted,
            hours_old=hours_old,
            sources=["google"],
        )
        errors.extend(errs)
        for row in rows:
            job_url = str(row.get("jobUrl") or row.get("job_url") or "").strip()
            if not job_url:
                continue
            if not _is_foundit_url(job_url):
                # Google sometimes returns aggregator links — keep explicit foundit URLs only.
                continue
            row = _row_as_foundit(row)
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            uj = from_jobspy_row(row, search_keyword=term)
            if uj is None:
                continue
            out.append(uj)

    logger.info("Foundit via Google: term=%r found=%s", term, len(out))
    return out, errors
