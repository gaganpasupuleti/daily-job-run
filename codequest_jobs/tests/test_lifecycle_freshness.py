"""D18 evidence-age shadow freshness: pure, offline, explicit clock and policy."""

from __future__ import annotations

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

from lifecycle_freshness import (  # noqa: E402
    REASON_CONFIRMED_CLOSED,
    REASON_FRESH_CONFIRMED_OPEN,
    REASON_LIFECYCLE_CONFLICT_REVIEW,
    REASON_NEVER_CONFIRMED_OPEN,
    REASON_POSITIVE_EVIDENCE_TTL_EXPIRED,
    REASON_UNSUPPORTED_SOURCE,
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
from lifecycle_projection import (  # noqa: E402
    CONFIRMED_OPEN,
    REASON_EQUAL_TIME_CONFLICT,
    REASON_NO_OBSERVATIONS,
    UNRESOLVED,
    LifecycleProjection,
    LifecycleProjectionObservation,
    project_lifecycle,
)

UTC = timezone.utc
T_OPEN = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
T_INCONCLUSIVE = datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
T_EVAL_STALE = datetime(2026, 1, 2, 11, 0, tzinfo=UTC)
TTL_24H = timedelta(hours=24)


def _policy(ttl: timedelta = TTL_24H, policy_id: str = "synthetic-ttl-v1") -> LifecycleFreshnessPolicy:
    return LifecycleFreshnessPolicy(policy_id, ttl)


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


def _projection(**kwargs) -> LifecycleProjection:
    defaults = {
        "decision": UNRESOLVED,
        "review_required": True,
        "reason": REASON_NO_OBSERVATIONS,
        "authority_policy_id": "jobready-canonical-v1",
        "controlling_evidence_refs": (),
        "conflicting_evidence_refs": (),
        "superseded_evidence_refs": (),
        "non_authoritative_evidence_refs": (),
        "latest_attempt_ref": None,
    }
    defaults.update(kwargs)
    return LifecycleProjection(**defaults)


def _evaluate(
    projection: LifecycleProjection,
    *,
    source: str = "naukri",
    last_open: datetime | None = None,
    at: datetime,
    policy: LifecycleFreshnessPolicy | None = None,
) -> LifecycleFreshnessResult:
    return evaluate_lifecycle_freshness(
        lifecycle_projection=projection,
        source=source,
        last_confirmed_open_at=last_open,
        evaluation_time=at,
        policy=policy or _policy(),
    )


class FreshnessDecisionTests(unittest.TestCase):
    def test_01_no_observations_unconfirmed(self) -> None:
        proj = project_lifecycle([])
        result = _evaluate(proj, at=T_OPEN)
        self.assertEqual(result.state, STATE_UNCONFIRMED)
        self.assertEqual(result.reason, REASON_NEVER_CONFIRMED_OPEN)

    def test_02_no_authoritative_conclusive_unconfirmed(self) -> None:
        proj = project_lifecycle([_obs("i", "confirmed_active", T_OPEN, provider="injected")])
        result = _evaluate(proj, at=T_OPEN)
        self.assertEqual(result.state, STATE_UNCONFIRMED)

    def test_03_fresh_confirmed_open(self) -> None:
        proj = project_lifecycle([_obs("a", "confirmed_active", T_OPEN)])
        result = _evaluate(proj, last_open=T_OPEN, at=T_OPEN + timedelta(hours=1))
        self.assertEqual(result.state, STATE_FRESH)
        self.assertEqual(result.reason, REASON_FRESH_CONFIRMED_OPEN)
        self.assertFalse(result.hold_required)

    def test_04_one_instant_before_ttl_expiry_fresh(self) -> None:
        expires = T_OPEN + TTL_24H
        result = _evaluate(
            project_lifecycle([_obs("a", "confirmed_active", T_OPEN)]),
            last_open=T_OPEN,
            at=expires - timedelta(microseconds=1),
        )
        self.assertEqual(result.state, STATE_FRESH)

    def test_05_exact_ttl_boundary_stale(self) -> None:
        expires = T_OPEN + TTL_24H
        result = _evaluate(
            project_lifecycle([_obs("a", "confirmed_active", T_OPEN)]),
            last_open=T_OPEN,
            at=expires,
        )
        self.assertEqual(result.state, STATE_STALE)
        self.assertTrue(result.hold_required)
        self.assertEqual(result.reason, REASON_POSITIVE_EVIDENCE_TTL_EXPIRED)
        self.assertEqual(result.expires_at, expires)

    def test_06_after_ttl_stale(self) -> None:
        result = _evaluate(
            project_lifecycle([_obs("a", "confirmed_active", T_OPEN)]),
            last_open=T_OPEN,
            at=T_OPEN + TTL_24H + timedelta(seconds=1),
        )
        self.assertEqual(result.state, STATE_STALE)

    def test_07_stale_not_closed(self) -> None:
        result = _evaluate(
            project_lifecycle([_obs("a", "confirmed_active", T_OPEN)]),
            last_open=T_OPEN,
            at=T_OPEN + TTL_24H,
        )
        self.assertNotEqual(result.state, STATE_CLOSED)
        self.assertEqual(result.lifecycle_decision, CONFIRMED_OPEN)

    def test_08_confirmed_closed_ignores_historical_open(self) -> None:
        proj = project_lifecycle([_obs("c", "confirmed_expired", T_OPEN + timedelta(days=1))])
        result = _evaluate(proj, last_open=T_OPEN, at=T_OPEN + timedelta(days=30))
        self.assertEqual(result.state, STATE_CLOSED)
        self.assertEqual(result.reason, REASON_CONFIRMED_CLOSED)

    def test_09_inconclusive_does_not_renew_ttl(self) -> None:
        items = [
            _obs("open", "confirmed_active", T_OPEN),
            _obs("inc", "inconclusive", T_INCONCLUSIVE),
        ]
        proj = project_lifecycle(items)
        self.assertEqual(proj.decision, CONFIRMED_OPEN)
        result = _evaluate(proj, last_open=T_OPEN, at=T_EVAL_STALE)
        self.assertEqual(result.state, STATE_STALE)

    def test_10_equal_time_conflict_review_hold(self) -> None:
        items = [_obs("a", "confirmed_active", T_OPEN), _obs("b", "confirmed_expired", T_OPEN)]
        proj = project_lifecycle(items)
        result = _evaluate(proj, last_open=T_OPEN, at=T_OPEN)
        self.assertEqual(result.state, STATE_REVIEW_HOLD)
        self.assertEqual(result.reason, REASON_LIFECYCLE_CONFLICT_REVIEW)

    def test_11_authority_time_conflict_review_hold(self) -> None:
        from lifecycle_projection import LifecycleAuthorityPolicy

        policy = LifecycleAuthorityPolicy("ranked", {"high": 20, "low": 10})
        items = [
            _obs("high", "confirmed_expired", T_OPEN, provider="high"),
            _obs("low", "confirmed_active", T_OPEN + timedelta(hours=1), provider="low"),
        ]
        proj = project_lifecycle(items, policy=policy)
        result = _evaluate(proj, last_open=T_OPEN, at=T_OPEN)
        self.assertEqual(result.state, STATE_REVIEW_HOLD)

    def test_12_conflict_overrides_apparently_fresh_timestamp(self) -> None:
        proj = _projection(
            decision=UNRESOLVED,
            reason=REASON_EQUAL_TIME_CONFLICT,
            conflicting_evidence_refs=("a", "b"),
        )
        result = _evaluate(proj, last_open=T_OPEN, at=T_OPEN + timedelta(minutes=5))
        self.assertEqual(result.state, STATE_REVIEW_HOLD)

    def test_13_google_unsupported(self) -> None:
        proj = project_lifecycle([])
        self.assertEqual(_evaluate(proj, source="google", at=T_OPEN).state, STATE_UNSUPPORTED)

    def test_14_foundit_unsupported(self) -> None:
        self.assertEqual(
            _evaluate(project_lifecycle([]), source="foundit", at=T_OPEN).reason,
            REASON_UNSUPPORTED_SOURCE,
        )

    def test_15_blank_source_unsupported(self) -> None:
        self.assertEqual(_evaluate(project_lifecycle([]), source="  ", at=T_OPEN).state, STATE_UNSUPPORTED)

    def test_16_unknown_source_unsupported(self) -> None:
        self.assertEqual(_evaluate(project_lifecycle([]), source="random", at=T_OPEN).state, STATE_UNSUPPORTED)

    def test_17_supported_no_authoritative_unconfirmed(self) -> None:
        proj = project_lifecycle([_obs("u", "confirmed_expired", T_OPEN, provider="foundit")])
        result = _evaluate(proj, source="naukri", at=T_OPEN)
        self.assertEqual(result.state, STATE_UNCONFIRMED)

    def test_18_naive_evaluation_time_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _evaluate(project_lifecycle([]), at=datetime(2026, 1, 1, 10, 0))

    def test_19_naive_last_confirmed_open_rejected(self) -> None:
        proj = project_lifecycle([_obs("a", "confirmed_active", T_OPEN)])
        with self.assertRaises(ValueError):
            evaluate_lifecycle_freshness(
                lifecycle_projection=proj,
                source="naukri",
                last_confirmed_open_at=datetime(2026, 1, 1, 10, 0),
                evaluation_time=T_OPEN,
                policy=_policy(),
            )

    def test_20_evaluation_before_open_timestamp_rejected(self) -> None:
        proj = project_lifecycle([_obs("a", "confirmed_active", T_OPEN)])
        with self.assertRaises(ValueError):
            _evaluate(proj, last_open=T_OPEN, at=T_OPEN - timedelta(seconds=1))

    def test_21_zero_ttl_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LifecycleFreshnessPolicy("bad", timedelta(0))

    def test_22_negative_ttl_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LifecycleFreshnessPolicy("bad", timedelta(hours=-1))

    def test_23_timezone_offset_equivalent_instant(self) -> None:
        east = timezone(timedelta(hours=5, minutes=30))
        open_local = datetime(2026, 1, 1, 15, 30, tzinfo=east)
        eval_local = datetime(2026, 1, 2, 15, 29, tzinfo=east)
        proj = project_lifecycle([_obs("a", "confirmed_active", open_local)])
        r_utc = _evaluate(proj, last_open=T_OPEN, at=T_OPEN + TTL_24H - timedelta(microseconds=1))
        r_local = _evaluate(proj, last_open=open_local, at=eval_local)
        self.assertEqual(r_utc.state, r_local.state)

    def test_24_deterministic_repeat_evaluation(self) -> None:
        proj = project_lifecycle([_obs("a", "confirmed_active", T_OPEN)])
        first = _evaluate(proj, last_open=T_OPEN, at=T_OPEN + timedelta(hours=1))
        second = _evaluate(proj, last_open=T_OPEN, at=T_OPEN + timedelta(hours=1))
        self.assertEqual(first, second)

    def test_25_no_hidden_wall_clock(self) -> None:
        text = (ROOT / "lifecycle_freshness.py").read_text(encoding="utf-8")
        forbidden = re.compile(r"datetime\.(now|utcnow)\(|time\.time\s*\(")
        self.assertIsNone(forbidden.search(text))

    def test_26_no_database_writes_or_connectors(self) -> None:
        text = (ROOT / "lifecycle_freshness.py").read_text(encoding="utf-8")
        forbidden = re.compile(
            r"(?:INSERT\s+INTO|UPDATE\s+\w+|DELETE\s+FROM|ALTER\s+TABLE|CREATE\s+TABLE|\bCOMMIT\b)"
            r"|import\s+psycopg2|psycopg2\.connect|JOB_READY_DATABASE_URL",
            re.IGNORECASE,
        )
        self.assertIsNone(forbidden.search(text))

    def test_27_no_catalog_link_status_dependency(self) -> None:
        text = (ROOT / "lifecycle_freshness.py").read_text(encoding="utf-8")
        self.assertNotIn("link_status", text)
        self.assertNotIn("validated_jobs", text)

    def test_28_d16_projection_not_mutated(self) -> None:
        proj = project_lifecycle([_obs("a", "confirmed_active", T_OPEN)])
        before = repr(proj)
        _evaluate(proj, last_open=T_OPEN, at=T_OPEN + timedelta(hours=1))
        self.assertEqual(repr(proj), before)

    def test_29_policy_immutable(self) -> None:
        policy = _policy()
        with self.assertRaises(Exception):
            policy.confirmed_open_ttl = timedelta(hours=1)  # type: ignore[misc]

    def test_30_fresh_not_eligibility_vocabulary(self) -> None:
        proj = project_lifecycle([_obs("a", "confirmed_active", T_OPEN)])
        result = _evaluate(proj, last_open=T_OPEN, at=T_OPEN + timedelta(hours=1))
        payload = result.state + result.reason
        for word in ("eligible", "publishable", "approved"):
            self.assertNotIn(word, payload)


class PolicyContractTests(unittest.TestCase):
    def test_confirmed_open_requires_last_open_timestamp(self) -> None:
        proj = project_lifecycle([_obs("a", "confirmed_active", T_OPEN)])
        with self.assertRaises(ValueError):
            _evaluate(proj, last_open=None, at=T_OPEN)


if __name__ == "__main__":
    unittest.main()
