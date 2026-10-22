"""Shadow eligibility v2: composes D17 v1 with route readiness (opt-in contract)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from application_route_validation.route_freshness import RouteFreshnessPolicy, RouteFreshnessResult, evaluate_route_freshness
from application_route_validation.route_projection import RouteProjection, project_route
from application_route_validation.observation_contract import RouteObservation, UNRESOLVED
from eligibility_export import (
    CONTRACT_VERSION as V1_CONTRACT,
    DECISION_ELIGIBLE,
    DECISION_HELD,
    DECISION_INELIGIBLE,
    EligibilityPrerequisites,
    JobEligibilityArtifact,
    export_job_eligibility,
    serialize_eligibility_artifact,
)

CONTRACT_VERSION = "jobready-eligibility-v2"

REASON_ROUTE_UNCONFIRMED = "route_unconfirmed"
REASON_ROUTE_STALE = "route_stale"
REASON_ROUTE_CONFLICT = "route_conflict_review"
REASON_ROUTE_LINK_UNKNOWN = "route_identity_link_unknown"


@dataclass(frozen=True, slots=True)
class JobEligibilityArtifactV2:
    base: JobEligibilityArtifact
    route_projection: RouteProjection
    route_freshness: RouteFreshnessResult
    route_ready_decision: str
    route_evidence_refs: tuple[str, ...]


def export_job_eligibility_v2(
    *,
    job_id: str,
    source: str,
    lifecycle_observations,
    evaluation_time: datetime,
    freshness_policy,
    prerequisites: EligibilityPrerequisites,
    route_observations: tuple[RouteObservation, ...],
    route_freshness_policy: RouteFreshnessPolicy,
) -> JobEligibilityArtifactV2:
    base = export_job_eligibility(
        job_id=job_id,
        source=source,
        observations=lifecycle_observations,
        evaluation_time=evaluation_time,
        freshness_policy=freshness_policy,
        prerequisites=prerequisites,
    )
    route_projection = project_route(route_observations)
    open_anchor = max(
        (o.observed_at for o in route_observations if o.outcome == "confirmed_open" and o.subject_type == "application_route"),
        default=None,
    )
    route_freshness = evaluate_route_freshness(
        projection=route_projection,
        last_confirmed_open_at=open_anchor,
        evaluation_time=evaluation_time,
        policy=route_freshness_policy,
    )
    return JobEligibilityArtifactV2(
        base=base,
        route_projection=route_projection,
        route_freshness=route_freshness,
        route_ready_decision=route_projection.route_ready_decision,
        route_evidence_refs=route_projection.controlling_refs,
    )


def decide_v2_eligibility(artifact: JobEligibilityArtifactV2) -> tuple[str, tuple[str, ...]]:
    """v1 decision gated by route readiness; never upgrades ineligible to eligible."""
    decision, reasons = artifact.base.eligibility_decision, tuple(artifact.base.eligibility_reasons)
    if artifact.route_projection.review_hold:
        return DECISION_HELD, (*reasons, REASON_ROUTE_CONFLICT)
    if artifact.route_projection.route_ready_decision == UNRESOLVED:
        return DECISION_HELD, (*reasons, REASON_ROUTE_UNCONFIRMED)
    if artifact.route_freshness.state == "stale":
        return DECISION_HELD, (*reasons, REASON_ROUTE_STALE)
    if artifact.route_projection.route_ready_decision != "confirmed_open":
        return DECISION_HELD, (*reasons, REASON_ROUTE_UNCONFIRMED)
    if decision == DECISION_ELIGIBLE and artifact.route_freshness.state == "fresh":
        return DECISION_ELIGIBLE, reasons
    if decision == DECISION_INELIGIBLE:
        return DECISION_INELIGIBLE, reasons
    return DECISION_HELD, reasons
