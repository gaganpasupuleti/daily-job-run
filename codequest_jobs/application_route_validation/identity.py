"""Identity namespaces and links between board ids and ATS post ids."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from link_checker import _naukri_job_id_from_url
from linkedin_checker import linkedin_job_id_from_url

NS_LINKEDIN_JOB = "linkedin:job"
NS_NAUKRI_JOB = "naukri:job"
NS_INDEED_JK = "indeed:jk"
NS_GREENHOUSE_POST = "greenhouse:job_post"

_GREENHOUSE_HOST_SUFFIX = ".greenhouse.io"
_GREENHOUSE_JOBS_RE = re.compile(r"/jobs/(\d+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class IdentityRef:
    namespace: str
    value: str


@dataclass(frozen=True, slots=True)
class IdentityLink:
    """Evidence that two ids refer to the same requisition (not title/company match)."""

    from_ref: IdentityRef
    to_ref: IdentityRef
    link_type: str
    evidence: str


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def listing_identity(source: str, job_url: str) -> IdentityRef | None:
    src = source.strip().lower()
    if src == "linkedin":
        job_id = linkedin_job_id_from_url(job_url)
        return IdentityRef(NS_LINKEDIN_JOB, job_id) if job_id else None
    if src == "naukri":
        job_id = _naukri_job_id_from_url(job_url)
        return IdentityRef(NS_NAUKRI_JOB, job_id) if job_id else None
    if src == "indeed":
        from indeed_checker import indeed_jk_from_url

        jk = indeed_jk_from_url(job_url)
        return IdentityRef(NS_INDEED_JK, jk) if jk else None
    return None


def greenhouse_post_from_apply_url(apply_url: str) -> IdentityRef | None:
    host = _host(apply_url)
    if not host.endswith(_GREENHOUSE_HOST_SUFFIX) and host not in {"greenhouse.io"}:
        return None
    match = _GREENHOUSE_JOBS_RE.search(urlsplit(apply_url).path or "")
    if not match:
        return None
    return IdentityRef(NS_GREENHOUSE_POST, match.group(1))


def link_board_to_greenhouse(
    listing: IdentityRef | None, greenhouse: IdentityRef | None, apply_url: str
) -> IdentityLink | None:
    """Catalog apply_url points at a Greenhouse post; path id is the employer-side anchor."""
    if listing is None or greenhouse is None:
        return None
    return IdentityLink(
        from_ref=listing,
        to_ref=greenhouse,
        link_type="apply_url_points_to_greenhouse_post",
        evidence=f"apply_url_host={_host(apply_url)}",
    )
