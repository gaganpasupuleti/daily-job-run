"""Combine dimension evidence into confirmed_open / confirmed_closed / unresolved."""

from __future__ import annotations

from application_route_validation.contracts import (
    CONFIRMED_CLOSED,
    CONFIRMED_OPEN,
    UNRESOLVED,
    RouteDimension,
)


def _contradiction(open_dim: RouteDimension, closed_dim: RouteDimension) -> bool:
    return open_dim.status == CONFIRMED_OPEN and closed_dim.status == CONFIRMED_CLOSED


def decide_application_route(
    *,
    listing: RouteDimension,
    requisition: RouteDimension,
    application_route: RouteDimension,
) -> tuple[str, str]:
    """Overall decision for accepting applications via the targeted route.

    A closed board listing does not imply confirmed_closed when the application route is
    authoritatively open with matching identity. Closure on the application route or an
    explicit requisition closure can still yield confirmed_closed.
    """
    if listing.identity_match is False or requisition.identity_match is False or application_route.identity_match is False:
        return UNRESOLVED, "identity_mismatch"

    if _contradiction(application_route, requisition):
        return UNRESOLVED, "contradictory_evidence"
    if listing.status == CONFIRMED_OPEN and application_route.status == CONFIRMED_CLOSED:
        return UNRESOLVED, "contradictory_evidence"
    if listing.status == CONFIRMED_CLOSED and application_route.status == CONFIRMED_OPEN:
        if requisition.status != CONFIRMED_CLOSED:
            return CONFIRMED_OPEN, "application_route_open_despite_closed_listing"
    if application_route.status == CONFIRMED_OPEN:
        if application_route.identity_match is True or application_route.identity_match is None:
            if requisition.status == CONFIRMED_CLOSED:
                return UNRESOLVED, "requisition_closed_route_open"
            return CONFIRMED_OPEN, application_route.reason
    if application_route.status == CONFIRMED_CLOSED:
        return CONFIRMED_CLOSED, application_route.reason
    if requisition.status == CONFIRMED_CLOSED and application_route.status != CONFIRMED_OPEN:
        return CONFIRMED_CLOSED, requisition.reason
    if listing.status == CONFIRMED_CLOSED and application_route.status != CONFIRMED_OPEN:
        return UNRESOLVED, "listing_closed_route_unknown"
    return UNRESOLVED, application_route.reason or listing.reason or requisition.reason or "insufficient_evidence"


def dimension_from_lifecycle(observation: str, reason: str, identity_match: bool | None) -> RouteDimension:
    if observation == "confirmed_active":
        return RouteDimension(CONFIRMED_OPEN, reason, identity_match)
    if observation == "confirmed_expired":
        return RouteDimension(CONFIRMED_CLOSED, reason, identity_match)
    return RouteDimension(UNRESOLVED, reason, identity_match)
