"""Evidence-age shadow freshness decisions (D18).

Pure, read-only layer over D16 ``LifecycleProjection``. Distinguishes fresh vs stale
confirmed-open evidence, unconfirmed lifecycle, closed, unsupported sources, and D16
review holds. Does not alter catalog state, eligibility, or publication (D17).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from link_policy import UNSUPPORTED, lifecycle_capability
from lifecycle_projection import (
    CONFIRMED_CLOSED,
    CONFIRMED_OPEN,
    REASON_AUTHORITY_TIME_CONFLICT,
    REASON_EQUAL_TIME_CONFLICT,
    REASON_INCOMPARABLE_CONFLICT,
    REASON_NO_AUTHORITATIVE_CONCLUSIVE,
    REASON_NO_OBSERVATIONS,
    UNRESOLVED,
    LifecycleProjection,
)

STATE_FRESH = "fresh"
STATE_STALE = "stale"
STATE_UNCONFIRMED = "unconfirmed"
STATE_CLOSED = "closed"
STATE_UNSUPPORTED = "unsupported"
STATE_REVIEW_HOLD = "review_hold"

REASON_FRESH_CONFIRMED_OPEN = "fresh_confirmed_open"
REASON_POSITIVE_EVIDENCE_TTL_EXPIRED = "positive_evidence_ttl_expired"
REASON_NEVER_CONFIRMED_OPEN = "never_confirmed_open"
REASON_CONFIRMED_CLOSED = "confirmed_closed"
REASON_UNSUPPORTED_SOURCE = "unsupported_source"
REASON_LIFECYCLE_CONFLICT_REVIEW = "lifecycle_conflict_review"

_CONFLICT_REASONS = frozenset(
    {
        REASON_EQUAL_TIME_CONFLICT,
        REASON_AUTHORITY_TIME_CONFLICT,
        REASON_INCOMPARABLE_CONFLICT,
    }
)
_UNCONFIRMED_REASONS = frozenset({REASON_NO_OBSERVATIONS, REASON_NO_AUTHORITATIVE_CONCLUSIVE})


def _require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


@dataclass(frozen=True, slots=True)
class LifecycleFreshnessPolicy:
    """Explicit positive-evidence TTL for shadow freshness. No production default."""

    policy_id: str
    confirmed_open_ttl: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ValueError("policy_id is required")
        if not isinstance(self.confirmed_open_ttl, timedelta):
            raise ValueError("confirmed_open_ttl must be a timedelta")
        if self.confirmed_open_ttl <= timedelta(0):
            raise ValueError("confirmed_open_ttl must be strictly positive")


@dataclass(frozen=True, slots=True)
class LifecycleFreshnessResult:
    """Shadow freshness decision. Not eligibility or publication."""

    state: str
    hold_required: bool
    reason: str
    freshness_policy_id: str
    evaluation_time: datetime
    lifecycle_decision: str
    last_confirmed_open_at: datetime | None
    expires_at: datetime | None


def evaluate_lifecycle_freshness(
    *,
    lifecycle_projection: LifecycleProjection,
    source: str,
    last_confirmed_open_at: datetime | None,
    evaluation_time: datetime,
    policy: LifecycleFreshnessPolicy,
) -> LifecycleFreshnessResult:
    """Evaluate evidence age from D16 output and caller-supplied open timestamp.

    Does not read the database or call wall-clock APIs.
    """
    _require_aware("evaluation_time", evaluation_time)
    if last_confirmed_open_at is not None:
        _require_aware("last_confirmed_open_at", last_confirmed_open_at)
        if evaluation_time < last_confirmed_open_at:
            raise ValueError("evaluation_time cannot precede last_confirmed_open_at")

    projection = lifecycle_projection
    base = {
        "freshness_policy_id": policy.policy_id,
        "evaluation_time": evaluation_time,
        "lifecycle_decision": projection.decision,
        "last_confirmed_open_at": last_confirmed_open_at,
        "expires_at": None,
    }

    if lifecycle_capability(source) == UNSUPPORTED:
        return LifecycleFreshnessResult(
            state=STATE_UNSUPPORTED,
            hold_required=False,
            reason=REASON_UNSUPPORTED_SOURCE,
            **base,
        )

    if projection.decision == UNRESOLVED:
        if projection.reason in _CONFLICT_REASONS or projection.conflicting_evidence_refs:
            return LifecycleFreshnessResult(
                state=STATE_REVIEW_HOLD,
                hold_required=True,
                reason=REASON_LIFECYCLE_CONFLICT_REVIEW,
                **base,
            )
        if projection.reason in _UNCONFIRMED_REASONS:
            return LifecycleFreshnessResult(
                state=STATE_UNCONFIRMED,
                hold_required=False,
                reason=REASON_NEVER_CONFIRMED_OPEN,
                **base,
            )
        return LifecycleFreshnessResult(
            state=STATE_REVIEW_HOLD,
            hold_required=True,
            reason=REASON_LIFECYCLE_CONFLICT_REVIEW,
            **base,
        )

    if projection.decision == CONFIRMED_CLOSED:
        return LifecycleFreshnessResult(
            state=STATE_CLOSED,
            hold_required=False,
            reason=REASON_CONFIRMED_CLOSED,
            **base,
        )

    if projection.decision == CONFIRMED_OPEN:
        if last_confirmed_open_at is None:
            raise ValueError("last_confirmed_open_at is required when lifecycle is confirmed_open")
        expires_at = last_confirmed_open_at + policy.confirmed_open_ttl
        base["expires_at"] = expires_at
        if evaluation_time < expires_at:
            return LifecycleFreshnessResult(
                state=STATE_FRESH,
                hold_required=False,
                reason=REASON_FRESH_CONFIRMED_OPEN,
                **base,
            )
        return LifecycleFreshnessResult(
            state=STATE_STALE,
            hold_required=True,
            reason=REASON_POSITIVE_EVIDENCE_TTL_EXPIRED,
            **base,
        )

    raise ValueError(f"unsupported lifecycle decision: {projection.decision}")
