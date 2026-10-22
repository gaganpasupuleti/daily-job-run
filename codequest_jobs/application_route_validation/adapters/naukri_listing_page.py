"""Naukri listing page render experiment (feasibility only)."""

from __future__ import annotations

import re
from typing import Any

from application_route_validation.contracts import CONFIRMED_CLOSED, CONFIRMED_OPEN, UNRESOLVED, RouteDimension, RouteValidationObservation
from link_checker import _naukri_job_id_from_url

ADAPTER_VERSION = "naukri-listing-playwright-v1"
_CLOSED_RE = re.compile(r"(job you are trying to view is not available|no longer accepting|application closed)", re.IGNORECASE)
_JOB_ID_RE = re.compile(r"jobid[\"']?\s*[:=]\s*[\"']?(\d{6,})", re.IGNORECASE)


def observe_naukri_listing_playwright(page: Any, job_url: str, expected_job_id: str, timeout_ms: int) -> RouteValidationObservation:
    response = page.goto(job_url, wait_until="domcontentloaded", timeout=timeout_ms)
    status = response.status if response else None
    html = page.content()
    parsed_id = _naukri_job_id_from_url(job_url)
    match = _JOB_ID_RE.search(html[:20000])
    observed = match.group(1) if match else parsed_id
    identity_match = observed == expected_job_id if observed else None

    if re.search(r"captcha|security check|login to continue", html[:8000], re.IGNORECASE):
        dim = RouteDimension(UNRESOLVED, "naukri_access_block", identity_match)
        return RouteValidationObservation(
            channel="naukri_listing_page",
            reason="naukri_access_block",
            final_url=page.url,
            expected_identity=expected_job_id,
            observed_identities=(observed,) if observed else None,
            listing=dim,
            extra={"adapter_version": ADAPTER_VERSION, "http_status": status},
        )

    if _CLOSED_RE.search(html[:12000]):
        dim = RouteDimension(CONFIRMED_CLOSED, "naukri_page_closed_copy", identity_match)
        return RouteValidationObservation(
            channel="naukri_listing_page",
            reason="naukri_page_closed_copy",
            final_url=page.url,
            expected_identity=expected_job_id,
            observed_identities=(observed,) if observed else None,
            listing=dim,
            extra={"adapter_version": ADAPTER_VERSION, "http_status": status},
        )

    if re.search(r"\bapply\b", html[:12000], re.IGNORECASE):
        dim = RouteDimension(UNRESOLVED, "naukri_apply_text_without_status", identity_match)
        return RouteValidationObservation(
            channel="naukri_listing_page",
            reason="naukri_apply_text_without_status",
            final_url=page.url,
            expected_identity=expected_job_id,
            observed_identities=(observed,) if observed else None,
            listing=dim,
            extra={"adapter_version": ADAPTER_VERSION, "http_status": status},
        )

    dim = RouteDimension(UNRESOLVED, "naukri_page_no_evidence", identity_match)
    return RouteValidationObservation(
        channel="naukri_listing_page",
        reason="naukri_page_no_evidence",
        final_url=page.url,
        expected_identity=expected_job_id,
        observed_identities=(observed,) if observed else None,
        listing=dim,
        extra={"adapter_version": ADAPTER_VERSION, "http_status": status},
    )
