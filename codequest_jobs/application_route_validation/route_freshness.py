"""Freshness over route projection (stale positive evidence is held, not closed)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from application_route_validation.observation_contract import CONFIRMED_OPEN, UNRESOLVED
from application_route_validation.route_projection import RouteProjection

STATE_FRESH = "fresh"
STATE_STALE = "stale"
STATE_UNCONFIRMED = "unconfirmed"
STATE_REVIEW_HOLD = "review_hold"


@dataclass(frozen=True, slots=True)
class RouteFreshnessPolicy:
    policy_id: str
    confirmed_open_ttl: timedelta

    def __post_init__(self) -> None:
        if self.confirmed_open_ttl.total_seconds() <= 0:
            raise ValueError("confirmed_open_ttl must be positive")


@dataclass(frozen=True, slots=True)
class RouteFreshnessResult:
    state: str
    reason: str
    freshness_policy_id: str
    route_decision: str


def evaluate_route_freshness(
    *,
    projection: RouteProjection,
    last_confirmed_open_at: datetime | None,
    evaluation_time: datetime,
    policy: RouteFreshnessPolicy,
) -> RouteFreshnessResult:
    if projection.review_hold:
        return RouteFreshnessResult(STATE_REVIEW_HOLD, "route_review_hold", policy.policy_id, projection.application_route_decision)
    if projection.application_route_decision == UNRESOLVED:
        return RouteFreshnessResult(STATE_UNCONFIRMED, "route_unconfirmed", policy.policy_id, projection.application_route_decision)
    if projection.application_route_decision != CONFIRMED_OPEN:
        return RouteFreshnessResult(STATE_UNCONFIRMED, "route_not_open", policy.policy_id, projection.application_route_decision)
    if last_confirmed_open_at is None:
        return RouteFreshnessResult(STATE_UNCONFIRMED, "route_open_anchor_missing", policy.policy_id, projection.application_route_decision)
    age = evaluation_time - last_confirmed_open_at
    if age > policy.confirmed_open_ttl:
        return RouteFreshnessResult(STATE_STALE, "route_open_ttl_expired", policy.policy_id, projection.application_route_decision)
    return RouteFreshnessResult(STATE_FRESH, "route_open_fresh", policy.policy_id, projection.application_route_decision)
