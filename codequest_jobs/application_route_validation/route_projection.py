"""Pure route projection per subject from immutable route observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from application_route_validation.observation_contract import (
    CONFIRMED_CLOSED,
    CONFIRMED_OPEN,
    UNRESOLVED,
    RouteObservation,
)
from application_route_validation.subjects import SUBJECT_APPLICATION_ROUTE, SUBJECT_LISTING, SUBJECT_REQUISITION

_POLICY_ID = "jobready-route-authority-v1"


@dataclass(frozen=True, slots=True)
class RouteProjection:
    listing_decision: str
    requisition_decision: str
    application_route_decision: str
    route_ready_decision: str
    reason: str
    review_hold: bool
    authority_policy_id: str
    controlling_refs: tuple[str, ...]
    conflicting_refs: tuple[str, ...]


def _latest_per_subject(observations: Sequence[RouteObservation]) -> dict[str, RouteObservation]:
    chosen: dict[str, RouteObservation] = {}
    for item in observations:
        prev = chosen.get(item.subject_type)
        if prev is None or item.observed_at > prev.observed_at:
            chosen[item.subject_type] = item
    return chosen


def _side(obs: RouteObservation | None) -> str:
    if obs is None:
        return UNRESOLVED
    return obs.outcome


def project_route(observations: Sequence[RouteObservation]) -> RouteProjection:
    latest = _latest_per_subject(observations)
    listing = _side(latest.get(SUBJECT_LISTING))
    requisition = _side(latest.get(SUBJECT_REQUISITION))
    application = _side(latest.get(SUBJECT_APPLICATION_ROUTE))

    review_hold = requisition == CONFIRMED_CLOSED and application == CONFIRMED_OPEN
    conflicting: list[str] = []
    if listing == CONFIRMED_OPEN and application == CONFIRMED_CLOSED:
        review_hold = True
        conflicting = [o.evidence_ref for o in observations if o.subject_type == SUBJECT_APPLICATION_ROUTE]

    route_ready = UNRESOLVED
    reason = "insufficient_evidence"
    if review_hold:
        reason = "subject_conflict"
    elif application == CONFIRMED_OPEN:
        assured = [o for o in observations if o.subject_type == SUBJECT_APPLICATION_ROUTE and o.catalog_link_assured]
        if assured and assured[-1].catalog_link_assured:
            route_ready = CONFIRMED_OPEN
            reason = "application_route_verified"
        else:
            reason = "catalog_identity_link_unknown"
    elif application == CONFIRMED_CLOSED:
        route_ready = CONFIRMED_CLOSED
        reason = "application_route_closed"

    controlling = tuple(
        sorted(
            {
                o.evidence_ref
                for o in observations
                if o.outcome in {CONFIRMED_OPEN, CONFIRMED_CLOSED}
                and o.subject_type in {SUBJECT_LISTING, SUBJECT_APPLICATION_ROUTE}
            }
        )
    )
    return RouteProjection(
        listing_decision=listing,
        requisition_decision=requisition,
        application_route_decision=application,
        route_ready_decision=route_ready,
        reason=reason,
        review_hold=review_hold,
        authority_policy_id=_POLICY_ID,
        controlling_refs=controlling,
        conflicting_refs=tuple(conflicting),
    )
