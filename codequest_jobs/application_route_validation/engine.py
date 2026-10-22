"""HTTP-first validation with optional Playwright fallback (prototype, no catalog writes)."""

from __future__ import annotations

import time
from typing import Any, Callable

from application_route_validation.adapters.http_board import observe_listing_http
from application_route_validation.adapters.playwright_fixture import observe_page_playwright
from application_route_validation.contracts import (
    UNRESOLVED,
    ApplicationRouteValidationResult,
    JobValidationTarget,
    RouteDimension,
    RouteValidationObservation,
)
from application_route_validation.decision import decide_application_route
NavigateFn = Callable[[Any, str], None]


def _unknown_dim(reason: str) -> RouteDimension:
    return RouteDimension(UNRESOLVED, reason, None)


def validate_application_route(
    target: JobValidationTarget,
    *,
    http_listing: dict[str, Any] | None = None,
    enable_playwright_fallback: bool = False,
    playwright_page: Any | None = None,
    navigate: NavigateFn | None = None,
    playwright_timeout_ms: int = 5000,
) -> ApplicationRouteValidationResult:
    """Validate listing + application route for one job.

    http_listing: optional injected transport for tests
      {status_code, body, payload, final_url, error}.
    When enable_playwright_fallback is true, playwright_page and navigate must reach application_url
    (or listing_url when application_url is absent).
    """
    started = time.perf_counter()
    observations: list[RouteValidationObservation] = []

    http_kwargs = http_listing or {}
    listing_obs = observe_listing_http(
        source=target.source,
        expected_job_id=target.expected_job_id,
        status_code=http_kwargs.get("status_code"),
        body=http_kwargs.get("body"),
        payload=http_kwargs.get("payload"),
        final_url=http_kwargs.get("final_url"),
        error=http_kwargs.get("error"),
    )
    observations.append(listing_obs)

    listing = listing_obs.listing or _unknown_dim(listing_obs.reason)
    requisition = _unknown_dim("not_collected")
    application_route = _unknown_dim("not_collected")

    route_url = (target.application_url or target.listing_url).strip()
    from application_route_validation.route_policy import should_verify_application_route

    listing_observation = "confirmed_active" if listing.status == "confirmed_open" else (
        "confirmed_expired" if listing.status == "confirmed_closed" else "inconclusive"
    )
    should_browser, _trigger = should_verify_application_route(
        source=target.source,
        job_url=target.listing_url,
        apply_url=target.application_url,
        listing_observation=listing_observation,
        listing_reason=listing_obs.reason,
    )
    need_browser = enable_playwright_fallback and should_browser

    if need_browser and playwright_page is not None and navigate is not None and route_url:
        navigate(playwright_page, route_url)
        browser_obs = observe_page_playwright(
            playwright_page,
            expected_job_id=target.expected_job_id,
            channel="playwright_application_route",
            timeout_ms=playwright_timeout_ms,
        )
        observations.append(browser_obs)
        if browser_obs.listing is not None:
            listing = browser_obs.listing
        if browser_obs.requisition is not None:
            requisition = browser_obs.requisition
        if browser_obs.application_route is not None:
            application_route = browser_obs.application_route
    elif not need_browser and listing.status != UNRESOLVED:
        application_route = listing
        requisition = listing

    decision, reason = decide_application_route(
        listing=listing,
        requisition=requisition,
        application_route=application_route,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return ApplicationRouteValidationResult(
        decision=decision,
        reason=reason,
        listing=listing,
        requisition=requisition,
        application_route=application_route,
        observations=tuple(observations),
        elapsed_ms=elapsed_ms,
    )
