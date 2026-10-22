"""Per-subject outcomes and cross-subject route readiness (no catalog mutation)."""

from __future__ import annotations

from dataclasses import dataclass

from application_route_validation.contracts import CONFIRMED_CLOSED, CONFIRMED_OPEN, UNRESOLVED, RouteDimension
from application_route_validation.decision import decide_application_route
from application_route_validation.observation_contract import UNRESOLVED as OUT_UNRESOLVED
from application_route_validation.subjects import SUBJECT_APPLICATION_ROUTE, SUBJECT_LISTING, SUBJECT_REQUISITION


@dataclass(frozen=True, slots=True)
class SubjectDecisions:
    listing: RouteDimension
    requisition: RouteDimension
    application_route: RouteDimension
    route_ready: str
    route_ready_reason: str
    review_hold: bool


def _hold_conflict(requisition: RouteDimension, application_route: RouteDimension) -> bool:
    return requisition.status == CONFIRMED_CLOSED and application_route.status == CONFIRMED_OPEN


def decide_subjects(
    *,
    listing: RouteDimension,
    requisition: RouteDimension,
    application_route: RouteDimension,
    catalog_link_assured: bool | None,
) -> SubjectDecisions:
    """Decision table for subject-aware route validation."""
    review_hold = _hold_conflict(requisition, application_route)

    if review_hold:
        return SubjectDecisions(
            listing=listing,
            requisition=requisition,
            application_route=application_route,
            route_ready=OUT_UNRESOLVED,
            route_ready_reason="requisition_closed_route_open_conflict",
            review_hold=True,
        )

    if catalog_link_assured is False:
        return SubjectDecisions(
            listing=listing,
            requisition=requisition,
            application_route=application_route,
            route_ready=OUT_UNRESOLVED,
            route_ready_reason="catalog_identity_link_unassured",
            review_hold=False,
        )

    route_ready, reason = decide_application_route(
        listing=listing,
        requisition=requisition,
        application_route=application_route,
    )

    if (
        route_ready == CONFIRMED_OPEN
        and application_route.status == CONFIRMED_OPEN
        and catalog_link_assured is not True
    ):
        return SubjectDecisions(
            listing=listing,
            requisition=requisition,
            application_route=application_route,
            route_ready=OUT_UNRESOLVED,
            route_ready_reason="catalog_identity_link_unknown",
            review_hold=False,
        )

    return SubjectDecisions(
        listing=listing,
        requisition=requisition,
        application_route=application_route,
        route_ready=route_ready,
        route_ready_reason=reason,
        review_hold=False,
    )
