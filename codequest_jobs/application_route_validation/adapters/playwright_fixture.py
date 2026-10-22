"""Playwright collector for prototype fixture pages (data-* contract on #job-validation-root)."""

from __future__ import annotations

from typing import Any

from application_route_validation.contracts import (
    CONFIRMED_CLOSED,
    CONFIRMED_OPEN,
    UNRESOLVED,
    RouteDimension,
    RouteValidationObservation,
)

ROOT_SELECTOR = "#job-validation-root"
BLOCKERS = frozenset({"login", "captcha", "rate_limit"})


def _status_from_attr(value: str | None) -> str:
    if not value:
        return UNRESOLVED
    text = value.strip().lower()
    if text in {"open", "active", "live"}:
        return CONFIRMED_OPEN
    if text in {"closed", "expired", "filled"}:
        return CONFIRMED_CLOSED
    return UNRESOLVED


def parse_fixture_dom(attrs: dict[str, str]) -> tuple[RouteDimension, RouteDimension, RouteDimension, str]:
    """Pure DOM attribute parser for unit tests without Playwright."""
    blocker = (attrs.get("data-blocker") or "").strip().lower()
    if blocker in BLOCKERS:
        dim = RouteDimension(UNRESOLVED, f"access_{blocker}", None)
        return dim, dim, dim, f"access_{blocker}"

    expected = attrs.get("data-expected-job-id") or ""
    observed = attrs.get("data-observed-job-id") or ""
    identity_match: bool | None = None
    if expected and observed:
        identity_match = expected == observed
    elif expected and not observed:
        identity_match = False

    listing = RouteDimension(
        _status_from_attr(attrs.get("data-listing-status")),
        attrs.get("data-listing-reason") or "fixture_listing",
        identity_match,
    )
    requisition = RouteDimension(
        _status_from_attr(attrs.get("data-requisition-status")),
        attrs.get("data-requisition-reason") or "fixture_requisition",
        identity_match,
    )
    application = RouteDimension(
        _status_from_attr(attrs.get("data-application-route")),
        attrs.get("data-application-reason") or "fixture_application_route",
        identity_match,
    )
    if listing.status == CONFIRMED_OPEN and application.status == CONFIRMED_CLOSED:
        return listing, requisition, application, "fixture_contradiction"
    reason = application.reason
    return listing, requisition, application, reason


def observe_page_playwright(page: Any, *, expected_job_id: str, channel: str, timeout_ms: int) -> RouteValidationObservation:
    """Inspect a loaded Playwright page. Caller must navigate first."""
    try:
        page.wait_for_selector(ROOT_SELECTOR, timeout=timeout_ms, state="attached")
    except Exception:
        return RouteValidationObservation(
            channel=channel,
            reason="timeout_or_empty",
            expected_identity=expected_job_id,
            listing=RouteDimension(UNRESOLVED, "timeout_or_empty", None),
            requisition=RouteDimension(UNRESOLVED, "timeout_or_empty", None),
            application_route=RouteDimension(UNRESOLVED, "timeout_or_empty", None),
        )

    handle = page.query_selector(ROOT_SELECTOR)
    if handle is None:
        return RouteValidationObservation(
            channel=channel,
            reason="missing_root",
            expected_identity=expected_job_id,
            final_url=page.url,
        )

    attrs = handle.evaluate(
        """(el) => {
            const out = {};
            for (const name of el.getAttributeNames()) out[name] = el.getAttribute(name);
            return out;
        }"""
    )
    listing, requisition, application, reason = parse_fixture_dom(attrs)
    observed = attrs.get("data-observed-job-id")
    identities = (observed,) if observed else None
    return RouteValidationObservation(
        channel=channel,
        reason=reason,
        final_url=page.url,
        expected_identity=expected_job_id,
        observed_identities=identities,
        listing=listing,
        requisition=requisition,
        application_route=application,
    )
