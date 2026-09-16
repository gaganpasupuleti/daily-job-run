"""LinkedIn closed-job check via the public guest job page.

HTTP 200 is not enough: LinkedIn still serves closed postings that say
"No longer accepting applications".
"""

from __future__ import annotations

import re

import httpx

from link_checker import LINK_STATUS_ACTIVE, LINK_STATUS_EXPIRED, LINK_STATUS_UNKNOWN

GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
JOB_ID_RE = re.compile(r"/jobs/view/(\d+)")
CLOSED_MARKERS = (
    "no longer accepting applications",
    "no longer accepting application",
    "this job is no longer available",
    "closed-job__flavor--closed",
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}


def linkedin_job_id_from_url(url: str) -> str | None:
    if not url:
        return None
    match = JOB_ID_RE.search(url)
    return match.group(1) if match else None


def check_linkedin_guest(url: str, *, client: httpx.Client | None = None) -> str | None:
    """Return expired/active/unknown, or None if the URL is not a LinkedIn view URL."""
    job_id = linkedin_job_id_from_url(url)
    if not job_id:
        return None
    own_client = client is None
    http = client or httpx.Client(timeout=20, follow_redirects=True, headers=HEADERS)
    try:
        resp = http.get(GUEST_URL.format(job_id=job_id))
    except httpx.HTTPError:
        return LINK_STATUS_UNKNOWN
    finally:
        if own_client:
            http.close()

    if resp.status_code in (404, 410):
        return LINK_STATUS_EXPIRED
    if resp.status_code == 429 or resp.status_code >= 500:
        return LINK_STATUS_UNKNOWN
    if resp.status_code >= 400:
        return LINK_STATUS_UNKNOWN

    text = (resp.text or "").lower()
    if any(marker in text for marker in CLOSED_MARKERS):
        return LINK_STATUS_EXPIRED
    if "topcard" in text or "job-details" in text or "description__text" in text:
        return LINK_STATUS_ACTIVE
    return LINK_STATUS_UNKNOWN
