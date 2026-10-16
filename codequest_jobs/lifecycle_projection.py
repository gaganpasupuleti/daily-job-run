"""Conflict-aware shadow lifecycle projection (D16).

Pure, read-only decision layer over immutable lifecycle observations. It explains
resolved open/closed, unresolved conflicts, superseded evidence, and non-authoritative
evidence using observation time and an explicit authority policy.

This does not alter ``validated_jobs.link_status``, the ledger writer, freshness/TTL
(D18), or eligibility/publication (D17). Provider validators are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from link_policy import (
    CONFIRMED_ACTIVE,
    CONFIRMED_EXPIRED,
    REDACTED_IDENTITY,
    SUPPORTED_LIFECYCLE_SOURCES,
)

# Shadow vocabulary: separate from catalog link_status (active/expired).
CONFIRMED_OPEN = "confirmed_open"
CONFIRMED_CLOSED = "confirmed_closed"
UNRESOLVED = "unresolved"

# Machine-readable reason codes.
REASON_NO_OBSERVATIONS = "no_observations"
REASON_NO_AUTHORITATIVE_CONCLUSIVE = "no_authoritative_conclusive_evidence"
REASON_CONFIRMED_OPEN = "confirmed_open"
REASON_CONFIRMED_CLOSED = "confirmed_closed"
REASON_NEWER_OPEN_RECOVERY = "newer_open_recovery"
REASON_NEWER_CLOSED_TRANSITION = "newer_closed_transition"
REASON_EQUAL_TIME_CONFLICT = "equal_time_conflict"
REASON_AUTHORITY_TIME_CONFLICT = "authority_time_conflict"
REASON_INCOMPARABLE_CONFLICT = "incomparable_conflict"

# Identity assurance metadata (does not rewrite the recorded verdict).
IDENTITY_ASSURANCE_UNKNOWN = "identity_unknown"
IDENTITY_ASSURANCE_REDACTED = "identity_redacted"
IDENTITY_ASSURANCE_PRESENT = "identity_present"
IDENTITY_ASSURANCE_ABSENT = "identity_absent"

DEFAULT_AUTHORITY_POLICY_ID = "jobready-canonical-v1"
# Canonical supported providers share one authority class. There is no LinkedIn>Indeed
# or similar ranking in the default policy.
DEFAULT_CANONICAL_AUTHORITY_RANK = 100

_SIDE_OPEN = "open"
_SIDE_CLOSED = "closed"
_CONCLUSIVE = frozenset({CONFIRMED_ACTIVE, CONFIRMED_EXPIRED})


@dataclass(frozen=True, slots=True)
class LifecycleProjectionObservation:
    """One immutable lifecycle evidence record for shadow projection."""

    evidence_ref: str
    observed_at: datetime
    observation: str
    provider: str
    reason: str = ""
    evidence_version: str = ""
    expected_identity: str | None = None
    observed_identities: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_ref, str) or not self.evidence_ref:
            raise ValueError("evidence_ref must be a non-empty string")
        if not isinstance(self.observed_at, datetime) or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be a timezone-aware datetime")
        if not isinstance(self.observation, str) or not self.observation:
            raise ValueError("observation is required")
        if not isinstance(self.provider, str):
            raise ValueError("provider must be a string")
        if self.observed_identities is not None and not isinstance(self.observed_identities, tuple):
            object.__setattr__(self, "observed_identities", tuple(self.observed_identities))


@dataclass(frozen=True, slots=True)
class LifecycleAuthorityPolicy:
    """Explicit authority classification and comparison for projection.

    ``ranks`` maps a provider name to a non-negative integer authority rank.
    Higher is more authoritative. Providers absent from ``ranks`` are non-authoritative.
    """

    policy_id: str
    ranks: Mapping[str, int]

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ValueError("policy_id is required")
        copied = {str(key): int(value) for key, value in dict(self.ranks).items()}
        if any(rank < 0 for rank in copied.values()):
            raise ValueError("authority ranks must be non-negative")
        object.__setattr__(self, "ranks", MappingProxyType(copied))

    def authority_rank(self, provider: str) -> int | None:
        """Return the authority rank, or None when the provider is non-authoritative."""
        if provider not in self.ranks:
            return None
        return self.ranks[provider]

    def is_authoritative(self, provider: str) -> bool:
        return self.authority_rank(provider) is not None


@dataclass(frozen=True, slots=True)
class LifecycleProjection:
    """Explainable shadow lifecycle decision. Does not mutate catalog state."""

    decision: str
    review_required: bool
    reason: str
    authority_policy_id: str
    controlling_evidence_refs: tuple[str, ...]
    conflicting_evidence_refs: tuple[str, ...]
    superseded_evidence_refs: tuple[str, ...]
    non_authoritative_evidence_refs: tuple[str, ...]
    latest_attempt_ref: str | None
    identity_assurance: str = IDENTITY_ASSURANCE_ABSENT


def default_authority_policy() -> LifecycleAuthorityPolicy:
    """Production-style default: LinkedIn, Naukri, and Indeed share one canonical class.

    Injected and unknown providers are omitted (non-authoritative). No relative ranking
    among the three supported providers is invented.
    """
    ranks = {name: DEFAULT_CANONICAL_AUTHORITY_RANK for name in sorted(SUPPORTED_LIFECYCLE_SOURCES)}
    return LifecycleAuthorityPolicy(DEFAULT_AUTHORITY_POLICY_ID, ranks)


def observation_side(observation: str) -> str | None:
    """Map a recorded ledger observation to a shadow side, or None when inconclusive."""
    if observation == CONFIRMED_ACTIVE:
        return _SIDE_OPEN
    if observation == CONFIRMED_EXPIRED:
        return _SIDE_CLOSED
    return None


def identity_assurance_for(item: LifecycleProjectionObservation) -> str:
    """Metadata-only identity assurance. Never rewrites the recorded verdict."""
    expected = item.expected_identity
    observed = item.observed_identities
    if expected is None and observed is None:
        return IDENTITY_ASSURANCE_UNKNOWN
    if expected == REDACTED_IDENTITY or (observed is not None and REDACTED_IDENTITY in observed):
        return IDENTITY_ASSURANCE_REDACTED
    if expected is not None or (observed is not None and len(observed) > 0):
        return IDENTITY_ASSURANCE_PRESENT
    return IDENTITY_ASSURANCE_ABSENT


def observation_from_ledger_row(row: Mapping[str, Any]) -> LifecycleProjectionObservation:
    """Narrow read-only adapter from a lifecycle ledger row mapping.

    Accepts dict-like rows with ledger column names (``id``, ``observed_at``, …).
    Does not open a database or reference catalog DDL.
    """
    evidence_ref = row.get("id", row.get("evidence_ref"))
    if evidence_ref is None:
        raise ValueError("ledger row needs id or evidence_ref")
    identities = row.get("observed_identities")
    if identities is not None and not isinstance(identities, tuple):
        identities = tuple(identities)
    return LifecycleProjectionObservation(
        evidence_ref=str(evidence_ref),
        observed_at=row["observed_at"],
        observation=str(row["observation"]),
        provider=str(row.get("provider") or ""),
        reason=str(row.get("reason") or ""),
        evidence_version=str(row.get("evidence_version") or ""),
        expected_identity=row.get("expected_identity"),
        observed_identities=identities,
    )


def _sorted_refs(refs: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(refs))


def _sort_key(item: LifecycleProjectionObservation) -> tuple:
    # Deterministic total order independent of input list order.
    return (item.observed_at, item.evidence_ref, item.observation, item.provider, item.reason)


def _dominates(
    left: LifecycleProjectionObservation,
    right: LifecycleProjectionObservation,
    policy: LifecycleAuthorityPolicy,
) -> bool:
    """True when left safely dominates right under authority + observed_at.

    left must be at least as authoritative and at least as recent, with at least one
    dimension strictly stronger. Ranks are required (both authoritative).
    """
    left_rank = policy.authority_rank(left.provider)
    right_rank = policy.authority_rank(right.provider)
    if left_rank is None or right_rank is None:
        return False
    if left_rank < right_rank:
        return False
    if left.observed_at < right.observed_at:
        return False
    return left_rank > right_rank or left.observed_at > right.observed_at


def _frontier(
    items: Sequence[LifecycleProjectionObservation],
    policy: LifecycleAuthorityPolicy,
) -> list[LifecycleProjectionObservation]:
    """Non-dominated authoritative conclusive observations."""
    result: list[LifecycleProjectionObservation] = []
    for candidate in items:
        if any(_dominates(other, candidate, policy) for other in items if other is not candidate):
            continue
        result.append(candidate)
    return result


def _conflict_reason(
    frontier: Sequence[LifecycleProjectionObservation],
    policy: LifecycleAuthorityPolicy,
) -> str:
    ranks = {policy.authority_rank(item.provider) for item in frontier}
    times = {item.observed_at for item in frontier}
    if len(ranks) == 1 and len(times) == 1:
        return REASON_EQUAL_TIME_CONFLICT
    if len(ranks) > 1 and len(times) > 1:
        # Higher authority is older than lower authority: classic authority/time clash,
        # or mixed incomparable frontiers.
        ordered = sorted(frontier, key=_sort_key)
        newer = max(ordered, key=lambda item: (item.observed_at, item.evidence_ref))
        older = min(ordered, key=lambda item: (item.observed_at, item.evidence_ref))
        newer_rank = policy.authority_rank(newer.provider)
        older_rank = policy.authority_rank(older.provider)
        if (
            newer_rank is not None
            and older_rank is not None
            and older_rank > newer_rank
            and older.observed_at < newer.observed_at
        ):
            return REASON_AUTHORITY_TIME_CONFLICT
        return REASON_INCOMPARABLE_CONFLICT
    if len(ranks) > 1:
        return REASON_INCOMPARABLE_CONFLICT
    return REASON_AUTHORITY_TIME_CONFLICT


def _resolved_reason(
    decision: str,
    frontier: Sequence[LifecycleProjectionObservation],
    authoritative: Sequence[LifecycleProjectionObservation],
) -> str:
    opposing = CONFIRMED_EXPIRED if decision == CONFIRMED_OPEN else CONFIRMED_ACTIVE
    had_opposing = any(item.observation == opposing for item in authoritative)
    if decision == CONFIRMED_OPEN and had_opposing:
        return REASON_NEWER_OPEN_RECOVERY
    if decision == CONFIRMED_CLOSED and had_opposing:
        return REASON_NEWER_CLOSED_TRANSITION
    if decision == CONFIRMED_OPEN:
        return REASON_CONFIRMED_OPEN
    return REASON_CONFIRMED_CLOSED


def _dedupe_observations(
    observations: Sequence[LifecycleProjectionObservation],
) -> list[LifecycleProjectionObservation]:
    """Reject conflicting duplicate refs; collapse identical duplicate refs."""
    by_ref: dict[str, LifecycleProjectionObservation] = {}
    for item in observations:
        prior = by_ref.get(item.evidence_ref)
        if prior is None:
            by_ref[item.evidence_ref] = item
            continue
        if (
            prior.observed_at != item.observed_at
            or prior.observation != item.observation
            or prior.provider != item.provider
            or prior.reason != item.reason
            or prior.evidence_version != item.evidence_version
            or prior.expected_identity != item.expected_identity
            or prior.observed_identities != item.observed_identities
        ):
            raise ValueError("duplicate evidence_ref with conflicting observation payloads")
    return list(by_ref.values())


def project_lifecycle(
    observations: Sequence[LifecycleProjectionObservation] | Iterable[LifecycleProjectionObservation],
    *,
    policy: LifecycleAuthorityPolicy | None = None,
) -> LifecycleProjection:
    """Project a deterministic shadow lifecycle decision from immutable observations.

    Input order does not affect the semantic result. Does not write to any database.
    """
    chosen = policy or default_authority_policy()
    items = _dedupe_observations(list(observations))
    items.sort(key=_sort_key)

    if not items:
        return LifecycleProjection(
            decision=UNRESOLVED,
            review_required=True,
            reason=REASON_NO_OBSERVATIONS,
            authority_policy_id=chosen.policy_id,
            controlling_evidence_refs=(),
            conflicting_evidence_refs=(),
            superseded_evidence_refs=(),
            non_authoritative_evidence_refs=(),
            latest_attempt_ref=None,
            identity_assurance=IDENTITY_ASSURANCE_ABSENT,
        )

    latest = max(items, key=_sort_key)
    # Aggregate identity assurance across the set without inventing identities.
    assurances = {identity_assurance_for(item) for item in items}
    if IDENTITY_ASSURANCE_REDACTED in assurances:
        identity = IDENTITY_ASSURANCE_REDACTED
    elif assurances == {IDENTITY_ASSURANCE_UNKNOWN}:
        identity = IDENTITY_ASSURANCE_UNKNOWN
    elif IDENTITY_ASSURANCE_PRESENT in assurances:
        identity = IDENTITY_ASSURANCE_PRESENT
    elif IDENTITY_ASSURANCE_UNKNOWN in assurances:
        identity = IDENTITY_ASSURANCE_UNKNOWN
    else:
        identity = IDENTITY_ASSURANCE_ABSENT

    non_authoritative = [item for item in items if not chosen.is_authoritative(item.provider)]
    authoritative_conclusive = [
        item
        for item in items
        if chosen.is_authoritative(item.provider) and item.observation in _CONCLUSIVE
    ]

    if not authoritative_conclusive:
        return LifecycleProjection(
            decision=UNRESOLVED,
            review_required=True,
            reason=REASON_NO_AUTHORITATIVE_CONCLUSIVE,
            authority_policy_id=chosen.policy_id,
            controlling_evidence_refs=(),
            conflicting_evidence_refs=(),
            superseded_evidence_refs=(),
            non_authoritative_evidence_refs=_sorted_refs(item.evidence_ref for item in non_authoritative),
            latest_attempt_ref=latest.evidence_ref,
            identity_assurance=identity,
        )

    frontier = _frontier(authoritative_conclusive, chosen)
    frontier_refs = _sorted_refs(item.evidence_ref for item in frontier)
    superseded = [
        item
        for item in authoritative_conclusive
        if item.evidence_ref not in frontier_refs
    ]
    sides = {observation_side(item.observation) for item in frontier}
    sides.discard(None)

    if len(sides) > 1:
        return LifecycleProjection(
            decision=UNRESOLVED,
            review_required=True,
            reason=_conflict_reason(frontier, chosen),
            authority_policy_id=chosen.policy_id,
            controlling_evidence_refs=(),
            conflicting_evidence_refs=frontier_refs,
            superseded_evidence_refs=_sorted_refs(item.evidence_ref for item in superseded),
            non_authoritative_evidence_refs=_sorted_refs(item.evidence_ref for item in non_authoritative),
            latest_attempt_ref=latest.evidence_ref,
            identity_assurance=identity,
        )

    side = next(iter(sides))
    decision = CONFIRMED_OPEN if side == _SIDE_OPEN else CONFIRMED_CLOSED
    return LifecycleProjection(
        decision=decision,
        review_required=False,
        reason=_resolved_reason(decision, frontier, authoritative_conclusive),
        authority_policy_id=chosen.policy_id,
        controlling_evidence_refs=frontier_refs,
        conflicting_evidence_refs=(),
        superseded_evidence_refs=_sorted_refs(item.evidence_ref for item in superseded),
        non_authoritative_evidence_refs=_sorted_refs(item.evidence_ref for item in non_authoritative),
        latest_attempt_ref=latest.evidence_ref,
        identity_assurance=identity,
    )
