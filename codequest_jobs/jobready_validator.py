"""Validate JobReady catalog rows. This module does not read CodeQuest.

Observations are confirmed_active, confirmed_expired, or inconclusive.
Generic HTTP failure is inconclusive. Provider rules below are the only
automatic expiry signals.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from link_policy import CONFIRMED_ACTIVE, CONFIRMED_EXPIRED, INCONCLUSIVE

Probe = Callable[[dict[str, Any]], tuple[str, str, str]]


@dataclass(frozen=True)
class LinkObservation:
    job_id: str
    previous_status: str
    observation: str
    provider: str
    reason: str


def interpret_indeed(*, expired: bool | None, key_configured: bool) -> tuple[str, str]:
    if not key_configured or expired is None:
        reason = "indeed_missing_api_key" if not key_configured else "indeed_api_error"
        return INCONCLUSIVE, reason
    if expired:
        return CONFIRMED_EXPIRED, "indeed_expired_true"
    return CONFIRMED_ACTIVE, "indeed_expired_false"


def interpret_linkedin(*, status_code: int | None, body: str | None, error: str | None = None) -> tuple[str, str]:
    from linkedin_checker import CLOSED_MARKERS

    if error or status_code is None:
        return INCONCLUSIVE, "linkedin_connection_failure"
    if status_code in (401, 403, 408, 429) or status_code >= 500:
        return INCONCLUSIVE, f"linkedin_http_{status_code}"
    text = (body or "").lower()
    if any(marker in text for marker in CLOSED_MARKERS):
        return CONFIRMED_EXPIRED, "linkedin_closed_marker"
    if status_code in (404, 410):
        return CONFIRMED_EXPIRED, "linkedin_guest_gone"
    if status_code == 200 and any(marker in text for marker in ("topcard", "job-details", "description__text")):
        return CONFIRMED_ACTIVE, "linkedin_live_content"
    return INCONCLUSIVE, "linkedin_inconclusive"


def interpret_naukri(*, status_code: int | None, payload: Any, error: str | None = None) -> tuple[str, str]:
    """Only an explicit closed flag on a valid JSON body can expire a Naukri job."""
    if error or status_code != 200 or not isinstance(payload, dict):
        return INCONCLUSIVE, "naukri_ambiguous"
    job = payload.get("job")
    if isinstance(job, list) or job is None:
        job = payload.get("jobDetails") if isinstance(payload.get("jobDetails"), dict) else None
    if not isinstance(job, dict):
        return INCONCLUSIVE, "naukri_ambiguous"
    if _naukri_explicitly_closed(job):
        return CONFIRMED_EXPIRED, "naukri_explicit_closed"
    title = job.get("title") or job.get("jobTitle")
    if title:
        return CONFIRMED_ACTIVE, "naukri_live_job"
    return INCONCLUSIVE, "naukri_ambiguous"


def _naukri_explicitly_closed(job: dict[str, Any]) -> bool:
    closed_tokens = {"expired", "closed", "withdrawn", "inactive"}
    for key in ("jobStatus", "status", "applyStatus"):
        value = str(job.get(key) or "").strip().lower()
        if value in closed_tokens:
            return True
    for key in ("isExpired", "expired", "isClosed"):
        value = job.get(key)
        if value is True:
            return True
        text = str(value or "").strip().lower()
        if text in closed_tokens or text in {"true", "1"}:
            return True
    return False


def interpret_generic(source: str) -> tuple[str, str]:
    return INCONCLUSIVE, f"{source or 'unknown'}_no_explicit_closure"


def validate_candidates(rows: list[dict[str, Any]], probes: dict[str, Probe] | None = None) -> list[LinkObservation]:
    """Return one observation per catalog row. Probes are injectable so tests stay offline."""
    chosen = probes if probes is not None else default_probes()
    generic = chosen.get("generic", lambda row: (*interpret_generic(row.get("source") or ""), row.get("source") or "unknown"))
    observations: list[LinkObservation] = []
    for row in rows:
        source = (row.get("source") or "").strip().lower()
        probe = chosen.get(source, generic)
        observation, reason, provider = probe(row)
        observations.append(
            LinkObservation(
                job_id=row["job_id"],
                previous_status=row.get("link_status") or "active",
                observation=observation,
                provider=provider,
                reason=reason,
            )
        )
    return observations


def default_probes() -> dict[str, Probe]:
    return {
        "indeed": probe_indeed,
        "linkedin": probe_linkedin,
        "naukri": probe_naukri,
        "generic": lambda row: (*interpret_generic(row.get("source") or ""), row.get("source") or "unknown"),
    }


def probe_indeed(row: dict[str, Any]) -> tuple[str, str, str]:
    from indeed_checker import check_indeed_via_api, indeed_jk_from_url

    if not (os.getenv("INDEED_API_KEY") or "").strip():
        observation, reason = interpret_indeed(expired=None, key_configured=False)
        return observation, reason, "indeed"
    jk = indeed_jk_from_url(row.get("job_url") or "")
    if not jk:
        return INCONCLUSIVE, "indeed_missing_job_key", "indeed"
    result = check_indeed_via_api(jk)
    expired = True if result == "expired" else False if result == "active" else None
    observation, reason = interpret_indeed(expired=expired, key_configured=True)
    return observation, reason, "indeed"


def probe_linkedin(row: dict[str, Any]) -> tuple[str, str, str]:
    from linkedin_checker import GUEST_URL, HEADERS, linkedin_job_id_from_url

    job_id = linkedin_job_id_from_url(row.get("job_url") or "")
    if not job_id:
        return INCONCLUSIVE, "linkedin_unrecognized_url", "linkedin"
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True, headers=HEADERS) as client:
            response = client.get(GUEST_URL.format(job_id=job_id))
    except httpx.HTTPError:
        observation, reason = interpret_linkedin(status_code=None, body=None, error="connection")
        return observation, reason, "linkedin"
    observation, reason = interpret_linkedin(status_code=response.status_code, body=response.text)
    return observation, reason, "linkedin"


def probe_naukri(row: dict[str, Any]) -> tuple[str, str, str]:
    from link_checker import _naukri_job_id_from_url

    job_id = _naukri_job_id_from_url(row.get("job_url") or "")
    if not job_id:
        return INCONCLUSIVE, "naukri_unrecognized_url", "naukri"
    try:
        import sys
        from pathlib import Path

        scraper = Path(__file__).resolve().parents[1] / "naukri_jobs_scraper"
        if str(scraper) not in sys.path:
            sys.path.append(str(scraper))
        from naukri_api import JOB_DETAIL_URL, NaukriSearchClient, generate_nkparam

        client = NaukriSearchClient(delay_seconds=0.2)
        try:
            response = client._client.get(
                JOB_DETAIL_URL.format(job_id=job_id),
                params={"microsite": "y", "src": "jobsearchDesk", "xp": "1", "px": "1"},
                headers={
                    "Accept": "application/json",
                    "appid": "121",
                    "systemid": "Naukri",
                    "clientid": "d3skt0p",
                    "nkparam": generate_nkparam("jd"),
                },
            )
        finally:
            client.close()
    except Exception:
        observation, reason = interpret_naukri(status_code=None, payload=None, error="connection")
        return observation, reason, "naukri"
    payload = None
    if response.status_code == 200:
        try:
            payload = response.json()
        except Exception:
            payload = None
    observation, reason = interpret_naukri(status_code=response.status_code, payload=payload)
    return observation, reason, "naukri"
