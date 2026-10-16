"""Versioned per-job shadow eligibility export (D17).

Pure, read-only composition of D16 lifecycle projection and D18 freshness. Does not
write catalog or application state, publish jobs, or choose production TTL.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from link_policy import CONFIRMED_ACTIVE
from lifecycle_freshness import (
    STATE_CLOSED,
    STATE_FRESH,
    STATE_REVIEW_HOLD,
    STATE_STALE,
    STATE_UNCONFIRMED,
    STATE_UNSUPPORTED,
    LifecycleFreshnessPolicy,
    LifecycleFreshnessResult,
    evaluate_lifecycle_freshness,
)
from lifecycle_projection import (
    CONFIRMED_CLOSED,
    CONFIRMED_OPEN,
    LifecycleProjection,
    LifecycleProjectionObservation,
    project_lifecycle,
)

CONTRACT_VERSION = "jobready-eligibility-v1"

TRI_TRUE = "true"
TRI_FALSE = "false"
TRI_UNKNOWN = "unknown"
_TRI_STATES = frozenset({TRI_TRUE, TRI_FALSE, TRI_UNKNOWN})

DECISION_ELIGIBLE = "eligible"
DECISION_INELIGIBLE = "ineligible"
DECISION_HELD = "held"

REASON_ELIGIBLE_ALL = "eligible_all_requirements_satisfied"
REASON_LIFECYCLE_CONFIRMED_CLOSED = "lifecycle_confirmed_closed"
REASON_FRESHNESS_STALE = "freshness_stale"
REASON_LIFECYCLE_UNCONFIRMED = "lifecycle_unconfirmed"
REASON_LIFECYCLE_UNSUPPORTED = "lifecycle_unsupported"
REASON_LIFECYCLE_CONFLICT_REVIEW = "lifecycle_conflict_review"
REASON_MAPPING_REJECTED = "mapping_rejected"
REASON_MAPPING_UNKNOWN = "mapping_unknown"
REASON_GEOGRAPHY_INELIGIBLE = "geography_ineligible"
REASON_GEOGRAPHY_UNKNOWN = "geography_unknown"
REASON_STUDENT_INELIGIBLE = "student_ineligible"
REASON_STUDENT_UNKNOWN = "student_unknown"
REASON_PUBLICATION_REJECTED = "publication_rejected"
REASON_PUBLICATION_UNKNOWN = "publication_unknown"

_REASON_ORDER: Mapping[str, int] = {
    REASON_LIFECYCLE_CONFIRMED_CLOSED: 10,
    REASON_MAPPING_REJECTED: 20,
    REASON_GEOGRAPHY_INELIGIBLE: 30,
    REASON_STUDENT_INELIGIBLE: 40,
    REASON_PUBLICATION_REJECTED: 50,
    REASON_FRESHNESS_STALE: 60,
    REASON_LIFECYCLE_UNCONFIRMED: 70,
    REASON_LIFECYCLE_UNSUPPORTED: 80,
    REASON_LIFECYCLE_CONFLICT_REVIEW: 90,
    REASON_MAPPING_UNKNOWN: 100,
    REASON_GEOGRAPHY_UNKNOWN: 110,
    REASON_STUDENT_UNKNOWN: 120,
    REASON_PUBLICATION_UNKNOWN: 130,
    REASON_ELIGIBLE_ALL: 1000,
}


def _require_aware(name: str, value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _require_job_id(job_id: str) -> None:
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("job_id is required")


def _require_tri_state(name: str, value: str) -> None:
    if value not in _TRI_STATES:
        raise ValueError(f"{name} must be true, false, or unknown")


def _sorted_reasons(reasons: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(reasons), key=lambda item: (_REASON_ORDER.get(item, 500), item)))


def _format_utc_instant(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    text = utc.isoformat(timespec="seconds")
    return text.replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class EligibilityPrerequisites:
    """Explicit tri-state inputs. Unknown must not be treated as satisfied."""

    mapping_accepted: str
    geography_eligible: str
    student_eligible: str
    publication_approved: str

    def __post_init__(self) -> None:
        _require_tri_state("mapping_accepted", self.mapping_accepted)
        _require_tri_state("geography_eligible", self.geography_eligible)
        _require_tri_state("student_eligible", self.student_eligible)
        _require_tri_state("publication_approved", self.publication_approved)


@dataclass(frozen=True, slots=True)
class EligibilityEvidenceRefs:
    controlling_evidence_refs: tuple[str, ...]
    conflicting_evidence_refs: tuple[str, ...]
    superseded_evidence_refs: tuple[str, ...]
    non_authoritative_evidence_refs: tuple[str, ...]
    latest_attempt_ref: str | None


@dataclass(frozen=True, slots=True)
class JobEligibilityArtifact:
    """Immutable shadow eligibility report for one job."""

    contract_version: str
    job_id: str
    evaluation_time: datetime
    eligibility_decision: str
    eligibility_reasons: tuple[str, ...]
    lifecycle_decision: str
    lifecycle_reason: str
    lifecycle_review_required: bool
    freshness_state: str
    freshness_reason: str
    freshness_policy_id: str
    authority_policy_id: str
    mapping_accepted: str
    geography_eligible: str
    student_eligible: str
    publication_approved: str
    evidence_refs: EligibilityEvidenceRefs


def authoritative_open_anchor(
    lifecycle_projection: LifecycleProjection,
    observations: Sequence[LifecycleProjectionObservation],
) -> datetime | None:
    """Latest observed_at among controlling authoritative confirmed_active evidence."""
    if lifecycle_projection.decision != CONFIRMED_OPEN:
        return None
    controlling = set(lifecycle_projection.controlling_evidence_refs)
    matches = [
        item
        for item in observations
        if item.evidence_ref in controlling and item.observation == CONFIRMED_ACTIVE
    ]
    if not matches:
        raise ValueError("confirmed_open projection lacks controlling confirmed_active observation")
    return max(item.observed_at for item in matches)


def _evidence_from_projection(projection: LifecycleProjection) -> EligibilityEvidenceRefs:
    return EligibilityEvidenceRefs(
        controlling_evidence_refs=projection.controlling_evidence_refs,
        conflicting_evidence_refs=projection.conflicting_evidence_refs,
        superseded_evidence_refs=projection.superseded_evidence_refs,
        non_authoritative_evidence_refs=projection.non_authoritative_evidence_refs,
        latest_attempt_ref=projection.latest_attempt_ref,
    )


def _assert_freshness_consistent(
    projection: LifecycleProjection,
    freshness: LifecycleFreshnessResult,
) -> None:
    if freshness.lifecycle_decision != projection.decision:
        raise ValueError("freshness lifecycle_decision disagrees with lifecycle projection")
    if freshness.state in {STATE_FRESH, STATE_STALE} and projection.decision != CONFIRMED_OPEN:
        raise ValueError("fresh/stale freshness requires confirmed_open lifecycle")
    if freshness.state == STATE_CLOSED and projection.decision != CONFIRMED_CLOSED:
        raise ValueError("closed freshness requires confirmed_closed lifecycle")


def _prerequisite_reasons(prerequisites: EligibilityPrerequisites) -> tuple[str, ...]:
    reasons: list[str] = []
    if prerequisites.mapping_accepted == TRI_FALSE:
        reasons.append(REASON_MAPPING_REJECTED)
    elif prerequisites.mapping_accepted == TRI_UNKNOWN:
        reasons.append(REASON_MAPPING_UNKNOWN)
    if prerequisites.geography_eligible == TRI_FALSE:
        reasons.append(REASON_GEOGRAPHY_INELIGIBLE)
    elif prerequisites.geography_eligible == TRI_UNKNOWN:
        reasons.append(REASON_GEOGRAPHY_UNKNOWN)
    if prerequisites.student_eligible == TRI_FALSE:
        reasons.append(REASON_STUDENT_INELIGIBLE)
    elif prerequisites.student_eligible == TRI_UNKNOWN:
        reasons.append(REASON_STUDENT_UNKNOWN)
    if prerequisites.publication_approved == TRI_FALSE:
        reasons.append(REASON_PUBLICATION_REJECTED)
    elif prerequisites.publication_approved == TRI_UNKNOWN:
        reasons.append(REASON_PUBLICATION_UNKNOWN)
    return _sorted_reasons(reasons)


def _freshness_hold_reason(freshness: LifecycleFreshnessResult) -> str | None:
    if freshness.state == STATE_STALE:
        return REASON_FRESHNESS_STALE
    if freshness.state == STATE_UNCONFIRMED:
        return REASON_LIFECYCLE_UNCONFIRMED
    if freshness.state == STATE_UNSUPPORTED:
        return REASON_LIFECYCLE_UNSUPPORTED
    if freshness.state == STATE_REVIEW_HOLD:
        return REASON_LIFECYCLE_CONFLICT_REVIEW
    return None


def _decide_eligibility(
    projection: LifecycleProjection,
    freshness: LifecycleFreshnessResult,
    prerequisites: EligibilityPrerequisites,
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    if projection.decision == CONFIRMED_CLOSED:
        reasons.append(REASON_LIFECYCLE_CONFIRMED_CLOSED)
    reasons.extend(_prerequisite_reasons(prerequisites))
    hold = _freshness_hold_reason(freshness)
    if hold is not None:
        reasons.append(hold)

    false_prereq = any(
        value == TRI_FALSE
        for value in (
            prerequisites.mapping_accepted,
            prerequisites.geography_eligible,
            prerequisites.student_eligible,
            prerequisites.publication_approved,
        )
    )
    unknown_prereq = any(
        value == TRI_UNKNOWN
        for value in (
            prerequisites.mapping_accepted,
            prerequisites.geography_eligible,
            prerequisites.student_eligible,
            prerequisites.publication_approved,
        )
    )

    if projection.decision == CONFIRMED_CLOSED or false_prereq:
        return DECISION_INELIGIBLE, _sorted_reasons(reasons)

    if hold is not None or unknown_prereq or projection.review_required:
        return DECISION_HELD, _sorted_reasons(reasons)

    if (
        projection.decision == CONFIRMED_OPEN
        and freshness.state == STATE_FRESH
        and not projection.review_required
        and prerequisites.mapping_accepted == TRI_TRUE
        and prerequisites.geography_eligible == TRI_TRUE
        and prerequisites.student_eligible == TRI_TRUE
        and prerequisites.publication_approved == TRI_TRUE
    ):
        reasons.append(REASON_ELIGIBLE_ALL)
        return DECISION_ELIGIBLE, _sorted_reasons(reasons)

    return DECISION_HELD, _sorted_reasons(reasons)


def export_job_eligibility(
    *,
    job_id: str,
    source: str,
    observations: Sequence[LifecycleProjectionObservation],
    evaluation_time: datetime,
    freshness_policy: LifecycleFreshnessPolicy,
    prerequisites: EligibilityPrerequisites,
    lifecycle_projection: LifecycleProjection | None = None,
) -> JobEligibilityArtifact:
    """Build a versioned shadow eligibility artifact from observations and prerequisites."""
    _require_job_id(job_id)
    _require_aware("evaluation_time", evaluation_time)
    projection = lifecycle_projection or project_lifecycle(observations)
    open_anchor = authoritative_open_anchor(projection, observations)
    freshness = evaluate_lifecycle_freshness(
        lifecycle_projection=projection,
        source=source,
        last_confirmed_open_at=open_anchor,
        evaluation_time=evaluation_time,
        policy=freshness_policy,
    )
    _assert_freshness_consistent(projection, freshness)
    decision, reasons = _decide_eligibility(projection, freshness, prerequisites)
    return JobEligibilityArtifact(
        contract_version=CONTRACT_VERSION,
        job_id=job_id,
        evaluation_time=evaluation_time,
        eligibility_decision=decision,
        eligibility_reasons=reasons,
        lifecycle_decision=projection.decision,
        lifecycle_reason=projection.reason,
        lifecycle_review_required=projection.review_required,
        freshness_state=freshness.state,
        freshness_reason=freshness.reason,
        freshness_policy_id=freshness.freshness_policy_id,
        authority_policy_id=projection.authority_policy_id,
        mapping_accepted=prerequisites.mapping_accepted,
        geography_eligible=prerequisites.geography_eligible,
        student_eligible=prerequisites.student_eligible,
        publication_approved=prerequisites.publication_approved,
        evidence_refs=_evidence_from_projection(projection),
    )


def eligibility_artifact_to_mapping(artifact: JobEligibilityArtifact) -> dict[str, Any]:
    """Deterministic JSON-serializable mapping for one artifact."""
    refs = artifact.evidence_refs
    return {
        "authority_policy_id": artifact.authority_policy_id,
        "contract_version": artifact.contract_version,
        "eligibility_decision": artifact.eligibility_decision,
        "eligibility_reasons": list(artifact.eligibility_reasons),
        "evaluation_time": _format_utc_instant(artifact.evaluation_time),
        "evidence_refs": {
            "conflicting_evidence_refs": list(artifact.evidence_refs.conflicting_evidence_refs),
            "controlling_evidence_refs": list(refs.controlling_evidence_refs),
            "latest_attempt_ref": refs.latest_attempt_ref,
            "non_authoritative_evidence_refs": list(refs.non_authoritative_evidence_refs),
            "superseded_evidence_refs": list(refs.superseded_evidence_refs),
        },
        "freshness_policy_id": artifact.freshness_policy_id,
        "freshness_reason": artifact.freshness_reason,
        "freshness_state": artifact.freshness_state,
        "geography_eligible": artifact.geography_eligible,
        "job_id": artifact.job_id,
        "lifecycle_decision": artifact.lifecycle_decision,
        "lifecycle_reason": artifact.lifecycle_reason,
        "lifecycle_review_required": artifact.lifecycle_review_required,
        "mapping_accepted": artifact.mapping_accepted,
        "publication_approved": artifact.publication_approved,
        "student_eligible": artifact.student_eligible,
    }


def serialize_eligibility_artifact(artifact: JobEligibilityArtifact) -> str:
    """Canonical JSON bytes as text (stable key order and separators)."""
    return json.dumps(
        eligibility_artifact_to_mapping(artifact),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
