"""D17 versioned shadow eligibility export: offline contract and integration tests."""

from __future__ import annotations

import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _entry in (str(ROOT),):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

from eligibility_export import (  # noqa: E402
    CONTRACT_VERSION,
    DECISION_ELIGIBLE,
    DECISION_HELD,
    DECISION_INELIGIBLE,
    REASON_ELIGIBLE_ALL,
    REASON_FRESHNESS_STALE,
    REASON_GEOGRAPHY_INELIGIBLE,
    REASON_GEOGRAPHY_UNKNOWN,
    REASON_LIFECYCLE_CONFIRMED_CLOSED,
    REASON_LIFECYCLE_CONFLICT_REVIEW,
    REASON_LIFECYCLE_UNCONFIRMED,
    REASON_LIFECYCLE_UNSUPPORTED,
    REASON_MAPPING_REJECTED,
    REASON_MAPPING_UNKNOWN,
    REASON_PUBLICATION_REJECTED,
    REASON_PUBLICATION_UNKNOWN,
    REASON_STUDENT_INELIGIBLE,
    REASON_STUDENT_UNKNOWN,
    TRI_FALSE,
    TRI_TRUE,
    TRI_UNKNOWN,
    EligibilityPrerequisites,
    JobEligibilityArtifact,
    _assert_freshness_consistent,
    authoritative_open_anchor,
    export_job_eligibility,
    serialize_eligibility_artifact,
)
from lifecycle_freshness import (  # noqa: E402
    STATE_FRESH,
    STATE_STALE,
    LifecycleFreshnessPolicy,
    LifecycleFreshnessResult,
    evaluate_lifecycle_freshness,
)
from lifecycle_projection import (  # noqa: E402
    CONFIRMED_CLOSED,
    CONFIRMED_OPEN,
    REASON_NEWER_OPEN_RECOVERY,
    UNRESOLVED,
    LifecycleProjection,
    LifecycleProjectionObservation,
    project_lifecycle,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)
TTL = timedelta(hours=24)


def _all_true() -> EligibilityPrerequisites:
    return EligibilityPrerequisites(TRI_TRUE, TRI_TRUE, TRI_TRUE, TRI_TRUE)


def _policy() -> LifecycleFreshnessPolicy:
    return LifecycleFreshnessPolicy("synthetic-ttl-v1", TTL)


def _obs(
    ref: str,
    observation: str,
    observed_at: datetime,
    *,
    provider: str = "naukri",
) -> LifecycleProjectionObservation:
    return LifecycleProjectionObservation(
        evidence_ref=ref,
        observed_at=observed_at,
        observation=observation,
        provider=provider,
        reason="fixture",
        evidence_version="jobready-validator-v2",
        expected_identity="1",
        observed_identities=("1",),
    )


def _export(
    observations: list[LifecycleProjectionObservation],
    *,
    job_id: str = "job-1",
    source: str = "naukri",
    at: datetime,
    prerequisites: EligibilityPrerequisites | None = None,
) -> JobEligibilityArtifact:
    return export_job_eligibility(
        job_id=job_id,
        source=source,
        observations=observations,
        evaluation_time=at,
        freshness_policy=_policy(),
        prerequisites=prerequisites or _all_true(),
    )


class EligibilityMatrixTests(unittest.TestCase):
    def test_01_all_true_fresh_open_eligible(self) -> None:
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0 + timedelta(hours=1))
        self.assertEqual(artifact.eligibility_decision, DECISION_ELIGIBLE)
        self.assertIn(REASON_ELIGIBLE_ALL, artifact.eligibility_reasons)

    def test_02_confirmed_closed_ineligible(self) -> None:
        artifact = _export([_obs("a", "confirmed_expired", T0)], at=T0)
        self.assertEqual(artifact.eligibility_decision, DECISION_INELIGIBLE)
        self.assertIn(REASON_LIFECYCLE_CONFIRMED_CLOSED, artifact.eligibility_reasons)

    def test_03_stale_held_not_closed(self) -> None:
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0 + TTL)
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertIn(REASON_FRESHNESS_STALE, artifact.eligibility_reasons)
        self.assertNotIn(REASON_LIFECYCLE_CONFIRMED_CLOSED, artifact.eligibility_reasons)

    def test_04_unconfirmed_held(self) -> None:
        artifact = _export([], at=T0)
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertIn(REASON_LIFECYCLE_UNCONFIRMED, artifact.eligibility_reasons)

    def test_05_unsupported_held(self) -> None:
        artifact = export_job_eligibility(
            job_id="job-1",
            source="google",
            observations=[_obs("a", "confirmed_active", T0)],
            evaluation_time=T0,
            freshness_policy=_policy(),
            prerequisites=_all_true(),
        )
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertIn(REASON_LIFECYCLE_UNSUPPORTED, artifact.eligibility_reasons)

    def test_06_equal_time_conflict_held(self) -> None:
        items = [_obs("a", "confirmed_active", T0), _obs("b", "confirmed_expired", T0)]
        artifact = _export(items, at=T0)
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertIn(REASON_LIFECYCLE_CONFLICT_REVIEW, artifact.eligibility_reasons)

    def test_07_authority_time_conflict_held(self) -> None:
        from lifecycle_projection import LifecycleAuthorityPolicy

        policy = LifecycleAuthorityPolicy("ranked", {"high": 20, "low": 10})
        items = [
            _obs("high", "confirmed_expired", T0, provider="high"),
            _obs("low", "confirmed_active", T1, provider="low"),
        ]
        proj = project_lifecycle(items, policy=policy)
        artifact = export_job_eligibility(
            job_id="job-1",
            source="naukri",
            observations=items,
            evaluation_time=T1,
            freshness_policy=_policy(),
            prerequisites=_all_true(),
            lifecycle_projection=proj,
        )
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)

    def test_08_mapping_false_ineligible(self) -> None:
        prereq = EligibilityPrerequisites(TRI_FALSE, TRI_TRUE, TRI_TRUE, TRI_TRUE)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=prereq)
        self.assertEqual(artifact.eligibility_decision, DECISION_INELIGIBLE)
        self.assertIn(REASON_MAPPING_REJECTED, artifact.eligibility_reasons)

    def test_09_geography_false_ineligible(self) -> None:
        prereq = EligibilityPrerequisites(TRI_TRUE, TRI_FALSE, TRI_TRUE, TRI_TRUE)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=prereq)
        self.assertIn(REASON_GEOGRAPHY_INELIGIBLE, artifact.eligibility_reasons)

    def test_10_student_false_ineligible(self) -> None:
        prereq = EligibilityPrerequisites(TRI_TRUE, TRI_TRUE, TRI_FALSE, TRI_TRUE)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=prereq)
        self.assertIn(REASON_STUDENT_INELIGIBLE, artifact.eligibility_reasons)

    def test_11_publication_false_ineligible(self) -> None:
        prereq = EligibilityPrerequisites(TRI_TRUE, TRI_TRUE, TRI_TRUE, TRI_FALSE)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=prereq)
        self.assertIn(REASON_PUBLICATION_REJECTED, artifact.eligibility_reasons)

    def test_12_mapping_unknown_held(self) -> None:
        prereq = EligibilityPrerequisites(TRI_UNKNOWN, TRI_TRUE, TRI_TRUE, TRI_TRUE)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=prereq)
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertIn(REASON_MAPPING_UNKNOWN, artifact.eligibility_reasons)

    def test_13_geography_unknown_held(self) -> None:
        prereq = EligibilityPrerequisites(TRI_TRUE, TRI_UNKNOWN, TRI_TRUE, TRI_TRUE)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=prereq)
        self.assertIn(REASON_GEOGRAPHY_UNKNOWN, artifact.eligibility_reasons)

    def test_14_student_unknown_held(self) -> None:
        prereq = EligibilityPrerequisites(TRI_TRUE, TRI_TRUE, TRI_UNKNOWN, TRI_TRUE)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=prereq)
        self.assertIn(REASON_STUDENT_UNKNOWN, artifact.eligibility_reasons)

    def test_15_publication_unknown_held(self) -> None:
        prereq = EligibilityPrerequisites(TRI_TRUE, TRI_TRUE, TRI_TRUE, TRI_UNKNOWN)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=prereq)
        self.assertIn(REASON_PUBLICATION_UNKNOWN, artifact.eligibility_reasons)

    def test_16_multiple_false_reasons(self) -> None:
        prereq = EligibilityPrerequisites(TRI_FALSE, TRI_FALSE, TRI_TRUE, TRI_TRUE)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=prereq)
        self.assertEqual(
            artifact.eligibility_reasons,
            (REASON_MAPPING_REJECTED, REASON_GEOGRAPHY_INELIGIBLE),
        )

    def test_17_multiple_unknown_reasons(self) -> None:
        prereq = EligibilityPrerequisites(TRI_UNKNOWN, TRI_UNKNOWN, TRI_TRUE, TRI_TRUE)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=prereq)
        self.assertEqual(
            artifact.eligibility_reasons,
            (REASON_MAPPING_UNKNOWN, REASON_GEOGRAPHY_UNKNOWN),
        )

    def test_18_false_and_unknown_precedence_ineligible(self) -> None:
        prereq = EligibilityPrerequisites(TRI_FALSE, TRI_UNKNOWN, TRI_TRUE, TRI_TRUE)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=prereq)
        self.assertEqual(artifact.eligibility_decision, DECISION_INELIGIBLE)
        self.assertIn(REASON_MAPPING_REJECTED, artifact.eligibility_reasons)
        self.assertIn(REASON_GEOGRAPHY_UNKNOWN, artifact.eligibility_reasons)

    def test_19_job_id_required(self) -> None:
        with self.assertRaises(ValueError):
            _export([_obs("a", "confirmed_active", T0)], job_id="", at=T0)

    def test_20_naive_evaluation_time_rejected(self) -> None:
        with self.assertRaises(ValueError):
            export_job_eligibility(
                job_id="job-1",
                source="naukri",
                observations=[_obs("a", "confirmed_active", T0)],
                evaluation_time=datetime(2026, 1, 1, 10, 0),
                freshness_policy=_policy(),
                prerequisites=_all_true(),
            )

    def test_21_repeat_evaluation_identical(self) -> None:
        items = [_obs("a", "confirmed_active", T0)]
        first = _export(items, at=T0)
        second = _export(items, at=T0)
        self.assertEqual(first, second)

    def test_22_repeat_serialization_byte_identical(self) -> None:
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0)
        self.assertEqual(serialize_eligibility_artifact(artifact), serialize_eligibility_artifact(artifact))

    def test_23_timezone_normalization_deterministic(self) -> None:
        east = timezone(timedelta(hours=5, minutes=30))
        local_eval = datetime(2026, 1, 1, 15, 30, tzinfo=east)
        artifact = _export([_obs("a", "confirmed_active", T0)], at=local_eval)
        payload = json.loads(serialize_eligibility_artifact(artifact))
        self.assertEqual(payload["evaluation_time"], "2026-01-01T10:00:00Z")

    def test_24_evidence_refs_sorted(self) -> None:
        items = [
            _obs("z", "confirmed_active", T0),
            _obs("a", "confirmed_active", T0),
            _obs("m", "confirmed_active", T0),
        ]
        artifact = _export(list(reversed(items)), at=T0)
        self.assertEqual(artifact.evidence_refs.controlling_evidence_refs, ("a", "m", "z"))

    def test_25_conflict_refs_retained(self) -> None:
        items = [_obs("a", "confirmed_active", T0), _obs("b", "confirmed_expired", T0)]
        artifact = _export(items, at=T0)
        self.assertEqual(artifact.evidence_refs.conflicting_evidence_refs, ("a", "b"))

    def test_26_injected_cannot_renew_anchor(self) -> None:
        items = [_obs("canon", "confirmed_active", T0, provider="linkedin"), _obs("inj", "confirmed_active", T1, provider="injected")]
        proj = project_lifecycle(items)
        anchor = authoritative_open_anchor(proj, items)
        self.assertEqual(anchor, T0)

    def test_27_no_catalog_summary_timestamp_reference(self) -> None:
        text = (ROOT / "eligibility_export.py").read_text(encoding="utf-8")
        self.assertNotIn("validated_jobs", text)
        self.assertNotIn("link_status", text)
        self.assertIn("authoritative_open_anchor", text)

    def test_28_anchor_from_controlling_only(self) -> None:
        items = [_obs("canon", "confirmed_active", T0)]
        proj = project_lifecycle(items)
        self.assertEqual(authoritative_open_anchor(proj, items), T0)

    def test_29_missing_controlling_open_raises(self) -> None:
        proj = LifecycleProjection(
            decision=CONFIRMED_OPEN,
            review_required=False,
            reason="confirmed_open",
            authority_policy_id="x",
            controlling_evidence_refs=("missing",),
            conflicting_evidence_refs=(),
            superseded_evidence_refs=(),
            non_authoritative_evidence_refs=(),
            latest_attempt_ref=None,
        )
        with self.assertRaises(ValueError):
            authoritative_open_anchor(proj, [_obs("a", "confirmed_active", T0)])

    def test_30_freshness_decision_mismatch_rejected(self) -> None:
        proj = project_lifecycle([_obs("a", "confirmed_active", T0)])
        freshness = LifecycleFreshnessResult(
            state=STATE_FRESH,
            hold_required=False,
            reason="fresh_confirmed_open",
            freshness_policy_id="synthetic-ttl-v1",
            evaluation_time=T0,
            lifecycle_decision=UNRESOLVED,
            last_confirmed_open_at=T0,
            expires_at=T0 + TTL,
        )
        with self.assertRaises(ValueError):
            _assert_freshness_consistent(proj, freshness)

    def test_31_fresh_with_non_open_rejected(self) -> None:
        proj = project_lifecycle([_obs("a", "confirmed_expired", T0)])
        freshness = LifecycleFreshnessResult(
            state=STATE_FRESH,
            hold_required=False,
            reason="fresh_confirmed_open",
            freshness_policy_id="synthetic-ttl-v1",
            evaluation_time=T0,
            lifecycle_decision=CONFIRMED_CLOSED,
            last_confirmed_open_at=T0,
            expires_at=T0 + TTL,
        )
        with self.assertRaises(ValueError):
            _assert_freshness_consistent(proj, freshness)

    def test_32_closed_freshness_mismatch_rejected(self) -> None:
        proj = project_lifecycle([_obs("a", "confirmed_active", T0)])
        freshness = evaluate_lifecycle_freshness(
            lifecycle_projection=proj,
            source="naukri",
            last_confirmed_open_at=T0,
            evaluation_time=T0,
            policy=_policy(),
        )
        closed = freshness.__class__(
            state="closed",
            hold_required=False,
            reason="confirmed_closed",
            freshness_policy_id=freshness.freshness_policy_id,
            evaluation_time=T0,
            lifecycle_decision=CONFIRMED_OPEN,
            last_confirmed_open_at=None,
            expires_at=None,
        )
        with self.assertRaises(ValueError):
            _assert_freshness_consistent(proj, closed)

    def test_33_d16_recovery_label_ignored_for_eligibility(self) -> None:
        proj = LifecycleProjection(
            decision=CONFIRMED_OPEN,
            review_required=False,
            reason=REASON_NEWER_OPEN_RECOVERY,
            authority_policy_id="jobready-canonical-v1",
            controlling_evidence_refs=("a",),
            conflicting_evidence_refs=(),
            superseded_evidence_refs=(),
            non_authoritative_evidence_refs=(),
            latest_attempt_ref=None,
        )
        artifact = export_job_eligibility(
            job_id="job-1",
            source="naukri",
            observations=[_obs("a", "confirmed_active", T0)],
            evaluation_time=T0,
            freshness_policy=_policy(),
            prerequisites=_all_true(),
            lifecycle_projection=proj,
        )
        self.assertEqual(artifact.eligibility_decision, DECISION_ELIGIBLE)

    def test_34_no_database_writes(self) -> None:
        text = (ROOT / "eligibility_export.py").read_text(encoding="utf-8")
        forbidden = re.compile(
            r"(?:INSERT\s+INTO|UPDATE\s+\w+|DELETE\s+FROM|ALTER\s+TABLE|CREATE\s+TABLE|\bCOMMIT\b)"
            r"|import\s+psycopg2|psycopg2\.connect|JOB_READY_DATABASE_URL",
            re.IGNORECASE,
        )
        self.assertIsNone(forbidden.search(text))

    def test_35_no_application_membership_writes(self) -> None:
        text = (ROOT / "eligibility_export.py").read_text(encoding="utf-8")
        self.assertNotIn("would_archive", text)
        self.assertNotIn("sync-application", text)
        self.assertNotIn("JOB_READY_APP_DATABASE_URL", text)

    def test_36_no_hidden_wall_clock(self) -> None:
        text = (ROOT / "eligibility_export.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"datetime\.(now|utcnow)\(|time\.time\s*\(", text))

    def test_37_no_production_ttl_default(self) -> None:
        text = (ROOT / "eligibility_export.py").read_text(encoding="utf-8")
        self.assertNotIn("timedelta(days=", text)
        self.assertNotIn("timedelta(hours=24", text)

    def test_38_tri_state_serialization(self) -> None:
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=EligibilityPrerequisites(TRI_UNKNOWN, TRI_TRUE, TRI_TRUE, TRI_TRUE))
        payload = json.loads(serialize_eligibility_artifact(artifact))
        self.assertEqual(payload["mapping_accepted"], TRI_UNKNOWN)

    def test_39_contract_version_present(self) -> None:
        artifact = _export([_obs("a", "confirmed_active", T0)], at=T0)
        self.assertEqual(artifact.contract_version, CONTRACT_VERSION)
        self.assertEqual(json.loads(serialize_eligibility_artifact(artifact))["contract_version"], CONTRACT_VERSION)

    def test_40_unknown_never_eligible(self) -> None:
        for unknown_field in (
            EligibilityPrerequisites(TRI_UNKNOWN, TRI_TRUE, TRI_TRUE, TRI_TRUE),
            EligibilityPrerequisites(TRI_TRUE, TRI_UNKNOWN, TRI_TRUE, TRI_TRUE),
            EligibilityPrerequisites(TRI_TRUE, TRI_TRUE, TRI_UNKNOWN, TRI_TRUE),
            EligibilityPrerequisites(TRI_TRUE, TRI_TRUE, TRI_TRUE, TRI_UNKNOWN),
        ):
            artifact = _export([_obs("a", "confirmed_active", T0)], at=T0, prerequisites=unknown_field)
            self.assertNotEqual(artifact.eligibility_decision, DECISION_ELIGIBLE)


class UnconfirmedVsConflictReasonTests(unittest.TestCase):
    def test_empty_observations_unconfirmed_not_conflict(self) -> None:
        artifact = _export([], at=T0)
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertIn(REASON_LIFECYCLE_UNCONFIRMED, artifact.eligibility_reasons)
        self.assertNotIn(REASON_LIFECYCLE_CONFLICT_REVIEW, artifact.eligibility_reasons)

    def test_injected_only_unconfirmed_not_conflict(self) -> None:
        artifact = _export([_obs("inj", "confirmed_active", T0, provider="injected")], at=T0)
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertIn(REASON_LIFECYCLE_UNCONFIRMED, artifact.eligibility_reasons)
        self.assertNotIn(REASON_LIFECYCLE_CONFLICT_REVIEW, artifact.eligibility_reasons)

    def test_unsupported_without_d16_conflict(self) -> None:
        artifact = export_job_eligibility(
            job_id="job-1",
            source="google",
            observations=[],
            evaluation_time=T0,
            freshness_policy=_policy(),
            prerequisites=_all_true(),
        )
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertIn(REASON_LIFECYCLE_UNSUPPORTED, artifact.eligibility_reasons)
        self.assertNotIn(REASON_LIFECYCLE_CONFLICT_REVIEW, artifact.eligibility_reasons)

    def test_equal_time_conflict_reason_present(self) -> None:
        items = [_obs("a", "confirmed_active", T0), _obs("b", "confirmed_expired", T0)]
        artifact = _export(items, at=T0)
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertIn(REASON_LIFECYCLE_CONFLICT_REVIEW, artifact.eligibility_reasons)

    def test_authority_time_conflict_reason_present(self) -> None:
        from lifecycle_projection import LifecycleAuthorityPolicy

        policy = LifecycleAuthorityPolicy("ranked", {"high": 20, "low": 10})
        items = [
            _obs("high", "confirmed_expired", T0, provider="high"),
            _obs("low", "confirmed_active", T1, provider="low"),
        ]
        artifact = export_job_eligibility(
            job_id="job-1",
            source="naukri",
            observations=items,
            evaluation_time=T1,
            freshness_policy=_policy(),
            prerequisites=_all_true(),
            lifecycle_projection=project_lifecycle(items, policy=policy),
        )
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertIn(REASON_LIFECYCLE_CONFLICT_REVIEW, artifact.eligibility_reasons)


class IntegrationFlowTests(unittest.TestCase):
    def test_canonical_fresh_open_eligible(self) -> None:
        items = [_obs("a", "confirmed_active", T0)]
        proj = project_lifecycle(items)
        anchor = authoritative_open_anchor(proj, items)
        freshness = evaluate_lifecycle_freshness(
            lifecycle_projection=proj,
            source="naukri",
            last_confirmed_open_at=anchor,
            evaluation_time=T0 + timedelta(hours=1),
            policy=_policy(),
        )
        self.assertEqual(freshness.state, STATE_FRESH)
        artifact = export_job_eligibility(
            job_id="job-1",
            source="naukri",
            observations=items,
            evaluation_time=T0 + timedelta(hours=1),
            freshness_policy=_policy(),
            prerequisites=_all_true(),
        )
        self.assertEqual(artifact.eligibility_decision, DECISION_ELIGIBLE)

    def test_canonical_stale_open_held(self) -> None:
        items = [_obs("a", "confirmed_active", T0)]
        artifact = export_job_eligibility(
            job_id="job-1",
            source="naukri",
            observations=items,
            evaluation_time=T0 + TTL,
            freshness_policy=_policy(),
            prerequisites=_all_true(),
        )
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertEqual(artifact.freshness_state, STATE_STALE)

    def test_canonical_conflict_held(self) -> None:
        items = [_obs("a", "confirmed_active", T0), _obs("b", "confirmed_expired", T0)]
        artifact = export_job_eligibility(
            job_id="job-1",
            source="naukri",
            observations=items,
            evaluation_time=T0,
            freshness_policy=_policy(),
            prerequisites=_all_true(),
        )
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)

    def test_injected_only_held(self) -> None:
        items = [_obs("inj", "confirmed_active", T0, provider="injected")]
        artifact = export_job_eligibility(
            job_id="job-1",
            source="naukri",
            observations=items,
            evaluation_time=T0,
            freshness_policy=_policy(),
            prerequisites=_all_true(),
        )
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)
        self.assertIn(REASON_LIFECYCLE_UNCONFIRMED, artifact.eligibility_reasons)

    def test_injected_newer_does_not_extend_ttl(self) -> None:
        items = [
            _obs("canon", "confirmed_active", T0, provider="linkedin"),
            _obs("inj", "confirmed_active", T1, provider="injected"),
        ]
        eval_time = T0 + TTL + timedelta(hours=1)
        self.assertLess(eval_time, T1 + TTL)
        artifact = export_job_eligibility(
            job_id="job-1",
            source="naukri",
            observations=items,
            evaluation_time=eval_time,
            freshness_policy=_policy(),
            prerequisites=_all_true(),
        )
        self.assertEqual(artifact.freshness_state, STATE_STALE)
        self.assertEqual(artifact.eligibility_decision, DECISION_HELD)


if __name__ == "__main__":
    unittest.main()
