"""Validate JobReady catalog rows. This module does not read CodeQuest.

Observations are confirmed_active, confirmed_expired, or inconclusive.
A conclusive observation needs provider evidence bound to the requested job:
the response must come from that job's provider endpoint, carry the same job
identity, and state its status. Anything weaker is inconclusive. Generic HTTP
failure is inconclusive. A missing Apply action is not closure.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from link_policy import CONFIRMED_ACTIVE, CONFIRMED_EXPIRED, INCONCLUSIVE
from linkedin_checker import CLOSED_MARKERS, GUEST_URL, HEADERS, linkedin_job_id_from_url

Probe = Callable[[dict[str, Any]], tuple[str, str, str]]

LINKEDIN_IDENTITY_RE = re.compile(
    r'id="decoratedJobPostingId"[^>]*>\s*<!--\s*"?(\d+)"?\s*-->',
    re.IGNORECASE,
)
LINKEDIN_BLOCK_MARKERS = (
    "captcha",
    "authwall",
    "/checkpoint/challenge",
    "security verification",
    "quick security check",
)
LINKEDIN_SOFT_404_MARKERS = ("page not found", "job not found")
LINKEDIN_STATUS_START = "top-card-layout"
LINKEDIN_STATUS_END = ("description__text", "show-more-less-html")
LINKEDIN_PRIMARY_CTA = "top-card-layout__cta--primary"
NON_RENDERED_RE = re.compile(
    r"<(script|style|template|noscript)\b.*?</\1\s*>|<!--.*?-->",
    re.IGNORECASE | re.DOTALL,
)
CALL_TO_ACTION_RE = re.compile(r"<(button|a)\b([^>]*)>(.*?)</\1\s*>", re.IGNORECASE | re.DOTALL)
CLASS_RE = re.compile(r'class="([^"]*)"', re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")
DISABLED_RE = re.compile(r"\bdisabled\b", re.IGNORECASE)

NAUKRI_DETAIL_PATH = "/jobapi/v1/job/{job_id}"
NAUKRI_ID_KEYS = ("jobId", "jobid", "id")
NAUKRI_STATUS_KEYS = ("jobStatus", "status", "applyStatus")
NAUKRI_FLAG_KEYS = ("isExpired", "expired", "isClosed")
NAUKRI_CLOSED_STATUS = {"expired", "closed", "withdrawn", "inactive"}
NAUKRI_OPEN_STATUS = {"active", "open", "live"}


@dataclass(frozen=True)
class LinkObservation:
    job_id: str
    previous_status: str
    observation: str
    provider: str
    reason: str


def interpret_indeed(*, expired: Any, key_configured: bool) -> tuple[str, str]:
    """Only a real boolean from the Indeed API is evidence."""
    if not key_configured:
        return INCONCLUSIVE, "indeed_missing_api_key"
    if expired is True:
        return CONFIRMED_EXPIRED, "indeed_expired_true"
    if expired is False:
        return CONFIRMED_ACTIVE, "indeed_expired_false"
    return INCONCLUSIVE, "indeed_api_error"


def _same_endpoint(final_url: str | None, expected_url: str) -> bool:
    if not final_url:
        return False
    final = httpx.URL(final_url)
    expected = httpx.URL(expected_url)
    return (final.host, final.path.rstrip("/")) == (expected.host, expected.path.rstrip("/"))


def _linkedin_status_region(rendered: str) -> str | None:
    """Top card only. Description, similar jobs, and hidden markup are ignored."""
    lowered = rendered.lower()
    start = lowered.find(LINKEDIN_STATUS_START)
    if start < 0:
        return None
    ends = [index for index in (lowered.find(marker, start) for marker in LINKEDIN_STATUS_END) if index >= 0]
    if not ends:
        return None
    return rendered[start : min(ends)]


def _linkedin_apply_state(region: str) -> str:
    """Return enabled, disabled, or absent for the top-card Apply action."""
    state = "absent"
    for match in CALL_TO_ACTION_RE.finditer(region):
        attributes = match.group(2)
        classes = CLASS_RE.search(attributes)
        tokens = set((classes.group(1) if classes else "").split())
        if LINKEDIN_PRIMARY_CTA not in tokens and "apply-button" not in tokens:
            continue
        if "apply" not in TAG_RE.sub(" ", match.group(3)).lower():
            continue
        cleaned = re.sub(r'aria-disabled="false"', "", attributes, flags=re.IGNORECASE)
        if DISABLED_RE.search(cleaned):
            state = "disabled"
            continue
        return "enabled"
    return state


def interpret_linkedin(
    *,
    status_code: int | None,
    body: str | None,
    expected_job_id: str,
    final_url: str | None,
    error: str | None = None,
) -> tuple[str, str]:
    """Interpret the LinkedIn guest job endpoint for one requested job id."""
    if error or status_code is None:
        return INCONCLUSIVE, "linkedin_connection_failure"
    if not _same_endpoint(final_url, GUEST_URL.format(job_id=expected_job_id)):
        return INCONCLUSIVE, "linkedin_redirected"
    if status_code in (401, 403, 408, 429, 999) or status_code >= 500:
        return INCONCLUSIVE, f"linkedin_http_{status_code}"
    if status_code in (404, 410):
        return CONFIRMED_EXPIRED, "linkedin_guest_gone"
    if status_code != 200:
        return INCONCLUSIVE, f"linkedin_http_{status_code}"
    html = body or ""
    identities = set(LINKEDIN_IDENTITY_RE.findall(html))
    rendered = NON_RENDERED_RE.sub(" ", html)
    visible = rendered.lower()
    if any(marker in visible for marker in LINKEDIN_BLOCK_MARKERS):
        return INCONCLUSIVE, "linkedin_blocked"
    if any(marker in visible for marker in LINKEDIN_SOFT_404_MARKERS):
        return INCONCLUSIVE, "linkedin_soft_404"
    if not identities:
        return INCONCLUSIVE, "linkedin_identity_missing"
    if identities != {expected_job_id}:
        return INCONCLUSIVE, "linkedin_identity_mismatch"
    region = _linkedin_status_region(rendered)
    if region is None:
        return INCONCLUSIVE, "linkedin_no_status_region"
    closed = any(marker in region.lower() for marker in CLOSED_MARKERS)
    apply_state = _linkedin_apply_state(region)
    if closed and apply_state == "enabled":
        return INCONCLUSIVE, "linkedin_conflict"
    if closed:
        return CONFIRMED_EXPIRED, "linkedin_closed_marker"
    if apply_state == "enabled":
        return CONFIRMED_ACTIVE, "linkedin_identity_apply"
    if apply_state == "disabled":
        return INCONCLUSIVE, "linkedin_apply_disabled"
    return INCONCLUSIVE, "linkedin_no_apply_evidence"


def _naukri_flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _naukri_status_signals(job: dict[str, Any]) -> tuple[bool, bool]:
    """Return (closed, open) from explicit provider status fields only."""
    closed = opened = False
    for key in NAUKRI_STATUS_KEYS:
        value = job.get(key)
        text = value.strip().lower() if isinstance(value, str) else ""
        closed = closed or text in NAUKRI_CLOSED_STATUS
        opened = opened or text in NAUKRI_OPEN_STATUS
    for key in NAUKRI_FLAG_KEYS:
        flag = _naukri_flag(job.get(key))
        closed = closed or flag is True
        opened = opened or flag is False
    return closed, opened


def interpret_naukri(
    *,
    status_code: int | None,
    payload: Any,
    expected_job_id: str,
    final_url: str | None,
    error: str | None = None,
) -> tuple[str, str]:
    """A Naukri verdict needs the same job id and an explicit status field."""
    if error or status_code is None:
        return INCONCLUSIVE, "naukri_connection_failure"
    if status_code != 200:
        return INCONCLUSIVE, f"naukri_http_{status_code}"
    if not final_url or httpx.URL(final_url).path.rstrip("/") != NAUKRI_DETAIL_PATH.format(job_id=expected_job_id):
        return INCONCLUSIVE, "naukri_redirected"
    if not isinstance(payload, dict):
        return INCONCLUSIVE, "naukri_malformed"
    job = payload.get("job")
    if not isinstance(job, dict):
        job = payload.get("jobDetails")
    if not isinstance(job, dict) or not job:
        return INCONCLUSIVE, "naukri_empty_detail"
    identities = {str(job[key]).strip() for key in NAUKRI_ID_KEYS if job.get(key) not in (None, "")}
    if not identities:
        return INCONCLUSIVE, "naukri_identity_missing"
    if identities != {expected_job_id}:
        return INCONCLUSIVE, "naukri_identity_mismatch"
    closed, opened = _naukri_status_signals(job)
    if closed and opened:
        return INCONCLUSIVE, "naukri_conflict"
    if closed:
        return CONFIRMED_EXPIRED, "naukri_explicit_closed"
    if opened:
        return CONFIRMED_ACTIVE, "naukri_explicit_open"
    return INCONCLUSIVE, "naukri_no_status_evidence"


def interpret_generic(source: str) -> tuple[str, str]:
    return INCONCLUSIVE, f"{source or 'unknown'}_no_explicit_closure"


def validate_candidates(rows: list[dict[str, Any]], probes: dict[str, Probe] | None = None) -> list[LinkObservation]:
    """Return one observation per catalog row. Probes are injectable so tests stay offline.

    A probe that raises is recorded as inconclusive for that row only.
    """
    chosen = probes if probes is not None else default_probes()
    generic = chosen.get("generic", lambda row: (*interpret_generic(row.get("source") or ""), row.get("source") or "unknown"))
    observations: list[LinkObservation] = []
    for row in rows:
        source = (row.get("source") or "").strip().lower()
        probe = chosen.get(source, generic)
        try:
            observation, reason, provider = probe(row)
        except Exception:
            observation, reason, provider = INCONCLUSIVE, f"{source or 'unknown'}_probe_error", source or "unknown"
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
    job_id = linkedin_job_id_from_url(row.get("job_url") or "")
    if not job_id:
        return INCONCLUSIVE, "linkedin_unrecognized_url", "linkedin"
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True, headers=HEADERS) as client:
            response = client.get(GUEST_URL.format(job_id=job_id))
    except httpx.HTTPError:
        observation, reason = interpret_linkedin(
            status_code=None, body=None, expected_job_id=job_id, final_url=None, error="connection"
        )
        return observation, reason, "linkedin"
    observation, reason = interpret_linkedin(
        status_code=response.status_code,
        body=response.text,
        expected_job_id=job_id,
        final_url=str(response.url),
    )
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
        observation, reason = interpret_naukri(
            status_code=None, payload=None, expected_job_id=job_id, final_url=None, error="connection"
        )
        return observation, reason, "naukri"
    payload = None
    if response.status_code == 200:
        try:
            payload = response.json()
        except Exception:
            payload = None
    observation, reason = interpret_naukri(
        status_code=response.status_code,
        payload=payload,
        expected_job_id=job_id,
        final_url=str(response.url),
    )
    return observation, reason, "naukri"
