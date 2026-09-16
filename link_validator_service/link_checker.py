"""Multi-source link recheck for scraped_jobs (CodeQuest-compatible)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from db import connect
from indeed_checker import check_indeed_via_api, indeed_jk_from_url

LINK_STATUS_ACTIVE = "active"
LINK_STATUS_EXPIRED = "expired"
LINK_STATUS_LINK_FAILED = "link_failed"
LINK_STATUS_UNKNOWN = "unknown"

NON_ACTIVE = (LINK_STATUS_EXPIRED, LINK_STATUS_LINK_FAILED, LINK_STATUS_UNKNOWN)
REQUEST_TIMEOUT = 8.0
USER_AGENT = "CodeQuest-Sister-JobLinkChecker/1.0"
# Explicitly gone. Rate limits, gateway errors, and bot blocks are not this.
EXPIRED_STATUSES = {404, 410, 451}
# Could not check. Do not store these as a dead link.
INCONCLUSIVE_STATUSES = {401, 403, 408, 429, 500, 502, 503, 504}


def _status_from_response(status_code: int) -> str:
    if status_code in EXPIRED_STATUSES:
        return LINK_STATUS_EXPIRED
    if status_code in INCONCLUSIVE_STATUSES or status_code >= 500:
        return LINK_STATUS_UNKNOWN
    if status_code >= 400:
        return LINK_STATUS_LINK_FAILED
    return LINK_STATUS_ACTIVE


def check_url(url: str) -> str:
    if not url or not str(url).startswith("http"):
        return LINK_STATUS_LINK_FAILED
    try:
        with httpx.Client(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = client.head(url)
            # Some boards block HEAD — retry GET lightly
            if resp.status_code in (405, 403, 501):
                resp = client.get(url)
            return _status_from_response(resp.status_code)
    except httpx.TimeoutException:
        return LINK_STATUS_UNKNOWN
    except httpx.HTTPError:
        return LINK_STATUS_UNKNOWN


def _naukri_job_id_from_url(url: str) -> str | None:
    """Extract trailing numeric job id from naukri JD URLs when possible."""
    try:
        path = urlparse(url).path.rstrip("/")
        tail = path.split("-")[-1]
        if tail.isdigit() and len(tail) >= 6:
            return tail
    except Exception:
        return None
    return None


def check_naukri_via_api(job_id: str) -> str | None:
    """Stronger check using local Naukri detail client when available."""
    try:
        import sys
        from pathlib import Path

        scraper = Path(__file__).resolve().parents[1] / "naukri_jobs_scraper"
        if str(scraper) not in sys.path:
            sys.path.insert(0, str(scraper))
        from naukri_api import NaukriSearchClient

        client = NaukriSearchClient(delay_seconds=0.3)
        try:
            return client.check_job_status(job_id)
        finally:
            client.close()
    except Exception:
        return None


def cleanup_job_links(
    *,
    limit: int = 100,
    delay: float = 0.35,
    sources: list[str] | None = None,
    use_naukri_api: bool = True,
) -> dict[str, Any]:
    """Check active/unknown jobs and update link_status. Does not delete."""
    counts = {
        "checkedCount": 0,
        "markedActive": 0,
        "markedExpired": 0,
        "markedLinkFailed": 0,
        "markedUnknown": 0,
    }
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    with connect() as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT id, job_id, source, job_url, apply_url
                FROM scraped_jobs
                WHERE COALESCE(link_status, 'active') IN ('active', 'unknown')
            """
            params: list[Any] = []
            if sources:
                sql += " AND source = ANY(%s)"
                params.append(sources)
            sql += """
                ORDER BY link_checked_at ASC NULLS FIRST, created_at DESC
                LIMIT %s
            """
            params.append(limit)
            cur.execute(sql, params)
            rows = cur.fetchall()

            for idx, (row_id, job_id, source, job_url, apply_url) in enumerate(rows):
                src = (source or "").lower()
                if src == "indeed":
                    url = (job_url or apply_url or "").strip()
                else:
                    url = (apply_url or job_url or "")
                status = check_url(url or "")
                if src == "indeed":
                    jk = indeed_jk_from_url(job_url or url or "")
                    if jk:
                        api_status = check_indeed_via_api(jk)
                        if api_status == LINK_STATUS_EXPIRED:
                            status = LINK_STATUS_EXPIRED
                        elif api_status == LINK_STATUS_ACTIVE:
                            status = LINK_STATUS_ACTIVE
                elif use_naukri_api and src == "naukri":
                    nid = _naukri_job_id_from_url(url or "")
                    if nid:
                        api_status = check_naukri_via_api(nid)
                        if api_status == LINK_STATUS_ACTIVE:
                            status = LINK_STATUS_ACTIVE

                cur.execute(
                    """
                    UPDATE scraped_jobs
                    SET link_status = %s, link_checked_at = %s
                    WHERE id = %s
                    """,
                    (status, now, row_id),
                )
                counts["checkedCount"] += 1
                key = {
                    LINK_STATUS_ACTIVE: "markedActive",
                    LINK_STATUS_EXPIRED: "markedExpired",
                    LINK_STATUS_LINK_FAILED: "markedLinkFailed",
                }.get(status, "markedUnknown")
                counts[key] += 1
                if idx + 1 < len(rows) and delay > 0:
                    time.sleep(delay)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(link_status, 'unknown'), COUNT(*)
                FROM scraped_jobs
                GROUP BY 1
                """
            )
            for status, n in cur.fetchall():
                counts[f"total_{status}"] = n

    return counts


def purge_non_active() -> dict[str, int]:
    """Delete expired/failed/unknown scraped jobs + matching enrichments."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id FROM scraped_jobs
                WHERE link_status = ANY(%s) AND job_id IS NOT NULL
                """,
                (list(NON_ACTIVE),),
            )
            doomed = [r[0] for r in cur.fetchall() if r[0]]
            deleted_enrichments = 0
            if doomed:
                cur.execute(
                    "DELETE FROM job_enrichments WHERE job_id = ANY(%s)",
                    (doomed,),
                )
                deleted_enrichments = cur.rowcount
            cur.execute(
                "DELETE FROM scraped_jobs WHERE link_status = ANY(%s)",
                (list(NON_ACTIVE),),
            )
            deleted_jobs = cur.rowcount
        conn.commit()
    return {"deletedJobs": deleted_jobs, "deletedEnrichments": deleted_enrichments}
