"""Public Greenhouse job board pages (application route subject)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from application_route_validation.contracts import (
    CONFIRMED_CLOSED,
    CONFIRMED_OPEN,
    UNRESOLVED,
    RouteDimension,
    RouteValidationObservation,
)
from application_route_validation.identity import greenhouse_post_from_apply_url
from greenhouse_checker import greenhouse_job_post_id_from_url

ADAPTER_VERSION = "greenhouse-board-v2"

_CLOSED_RE = re.compile(
    r"(no longer accepting applications|job you(?:'|’)re looking for isn(?:'|’)t available|position has been filled|this job is no longer)",
    re.IGNORECASE,
)
_APPLY_RE = re.compile(r"(submit application|apply for this job|apply now)", re.IGNORECASE)
_APPLICATION_SECTION_RE = re.compile(
    r'<div[^>]*id=["\']application["\'][^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_RECOMMENDED_RE = re.compile(r'<section[^>]*class=["\'][^"\']*recommended[^"\']*["\'][^>]*>.*?</section>', re.IGNORECASE | re.DOTALL)


def _primary_job_snippet(html: str) -> str:
    """Prefer Greenhouse application container; drop recommended-job noise."""
    stripped = _RECOMMENDED_RE.sub("", html)
    match = _APPLICATION_SECTION_RE.search(stripped)
    if match:
        return match.group(1)[:10000]
    return stripped[:15000]


def expected_post_id(apply_url: str) -> tuple[str | None, str | None]:
    post_id, conflict = greenhouse_job_post_id_from_url(apply_url)
    if conflict:
        return None, conflict
    if post_id:
        return post_id, None
    ref = greenhouse_post_from_apply_url(apply_url)
    return (ref.value if ref else None), None


def _observed_post_id(final_url: str) -> str | None:
    post_id, _ = greenhouse_job_post_id_from_url(final_url)
    if post_id:
        return post_id
    ref = greenhouse_post_from_apply_url(final_url)
    return ref.value if ref else None


def interpret_greenhouse_board_html(
    *,
    apply_url: str,
    final_url: str,
    html: str,
    http_status: int | None,
    apply_control_visible: bool = False,
) -> RouteValidationObservation:
    expected, conflict = expected_post_id(apply_url)
    observed = _observed_post_id(final_url)
    identity_match: bool | None = None
    if conflict:
        return RouteValidationObservation(
            channel="greenhouse_board",
            reason=conflict,
            final_url=final_url,
            expected_identity=expected,
            application_route=RouteDimension(UNRESOLVED, conflict, False),
            extra={"adapter_version": ADAPTER_VERSION, "http_status": http_status},
        )
    host = (urlsplit(final_url).hostname or "").lower()
    if not host.endswith("greenhouse.io"):
        dim = RouteDimension(UNRESOLVED, "redirect_off_greenhouse", None)
        return RouteValidationObservation(
            channel="greenhouse_board",
            reason="redirect_off_greenhouse",
            final_url=final_url,
            expected_identity=expected,
            application_route=dim,
            extra={"adapter_version": ADAPTER_VERSION, "http_status": http_status},
        )

    if expected and observed:
        identity_match = expected == observed
    elif expected and not observed:
        identity_match = False

    if identity_match is False:
        dim = RouteDimension(UNRESOLVED, "greenhouse_identity_mismatch", False)
        return RouteValidationObservation(
            channel="greenhouse_board",
            reason="greenhouse_identity_mismatch",
            final_url=final_url,
            expected_identity=expected,
            observed_identities=(observed,) if observed else (),
            application_route=dim,
            extra={"adapter_version": ADAPTER_VERSION, "http_status": http_status},
        )

    if http_status and http_status >= 400:
        dim = RouteDimension(UNRESOLVED, f"http_{http_status}", identity_match)
        return RouteValidationObservation(
            channel="greenhouse_board",
            reason=f"http_{http_status}",
            final_url=final_url,
            expected_identity=expected,
            application_route=dim,
            extra={"adapter_version": ADAPTER_VERSION, "http_status": http_status},
        )

    snippet = _primary_job_snippet(html)
    if _CLOSED_RE.search(snippet):
        dim = RouteDimension(CONFIRMED_CLOSED, "greenhouse_board_closed_copy", identity_match)
        return RouteValidationObservation(
            channel="greenhouse_board",
            reason="greenhouse_board_closed_copy",
            final_url=final_url,
            expected_identity=expected,
            observed_identities=(observed,) if observed else None,
            application_route=dim,
            extra={"adapter_version": ADAPTER_VERSION, "http_status": http_status},
        )

    if apply_control_visible or _APPLY_RE.search(snippet):
        dim = RouteDimension(CONFIRMED_OPEN, "greenhouse_board_apply_evidence", identity_match)
        return RouteValidationObservation(
            channel="greenhouse_board",
            reason="greenhouse_board_apply_evidence",
            final_url=final_url,
            expected_identity=expected,
            observed_identities=(observed,) if observed else None,
            application_route=dim,
            extra={"adapter_version": ADAPTER_VERSION, "http_status": http_status},
        )

    dim = RouteDimension(UNRESOLVED, "greenhouse_board_no_apply_evidence", identity_match)
    return RouteValidationObservation(
        channel="greenhouse_board",
        reason="greenhouse_board_no_apply_evidence",
        final_url=final_url,
        expected_identity=expected,
        observed_identities=(observed,) if observed else None,
        application_route=dim,
        extra={"adapter_version": ADAPTER_VERSION, "http_status": http_status},
    )


def observe_greenhouse_board_playwright(page: Any, apply_url: str, timeout_ms: int) -> RouteValidationObservation:
    from application_route_validation.browser_runner import SafeBrowserSession

    session = SafeBrowserSession(page)
    session.attach()
    response = session.goto(apply_url)
    if session.blocked_reason():
        dim = RouteDimension(UNRESOLVED, session.blocked_reason() or "navigation_blocked", None)
        return RouteValidationObservation(
            channel="greenhouse_board",
            reason=dim.reason,
            final_url=page.url if hasattr(page, "url") else None,
            application_route=dim,
            extra={"adapter_version": ADAPTER_VERSION},
        )
    status = response.status if response else None
    try:
        page.get_by_text("Submit application", exact=False).first.wait_for(state="visible", timeout=min(timeout_ms, 12000))
    except Exception:
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 8000))
        except Exception:
            pass
    html = page.content()
    apply_visible = page.get_by_text("Submit application", exact=False).count() > 0
    return interpret_greenhouse_board_html(
        apply_url=apply_url,
        final_url=page.url,
        html=html,
        http_status=status,
        apply_control_visible=apply_visible,
    )
