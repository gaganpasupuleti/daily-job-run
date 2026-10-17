"""D16 conflict-aware shadow lifecycle projection: pure, offline, order-independent."""

from __future__ import annotations

import itertools
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
for _entry in (str(ROOT),):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

from link_policy import REDACTED_IDENTITY  # noqa: E402
from lifecycle_projection import (  # noqa: E402
    CONFIRMED_CLOSED,
    CONFIRMED_OPEN,
    DEFAULT_AUTHORITY_POLICY_ID,
    IDENTITY_ASSURANCE_ABSENT,
    IDENTITY_ASSURANCE_PRESENT,
    IDENTITY_ASSURANCE_REDACTED,
    IDENTITY_ASSURANCE_UNKNOWN,
    LifecycleAuthorityPolicy,
    LifecycleProjection,
    LifecycleProjectionObservation,
    REASON_AUTHORITY_TIME_CONFLICT,
    REASON_CONFIRMED_CLOSED,
    REASON_CONFIRMED_OPEN,
    REASON_EQUAL_TIME_CONFLICT,
    REASON_INCOMPARABLE_CONFLICT,
    REASON_NEWER_CLOSED_TRANSITION,
    REASON_NEWER_OPEN_RECOVERY,
    REASON_NO_AUTHORITATIVE_CONCLUSIVE,
    REASON_NO_OBSERVATIONS,
    UNRESOLVED,
    default_authority_policy,
    observation_from_ledger_row,
    project_lifecycle,
)

UTC = timezone.utc
T10 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
T11 = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
T12 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _obs(
    ref: str,
    observation: str,
    observed_at: datetime,
    *,
    provider: str = "naukri",
    reason: str = "fixture",
    evidence_version: str = "jobready-validator-v2",
    expected_identity: str | None = "1",
    observed_identities: tuple[str, ...] | None = ("1",),
) -> LifecycleProjectionObservation:
    return LifecycleProjectionObservation(
        evidence_ref=ref,
        observed_at=observed_at,
        observation=observation,
        provider=provider,
        reason=reason,
        evidence_version=evidence_version,
        expected_identity=expected_identity,
        observed_identities=observed_identities,
    )


def _semantic(result: LifecycleProjection) -> tuple:
    return (
        result.decision,
        result.review_required,
        result.reason,
        result.authority_policy_id,
        result.controlling_evidence_refs,
        result.conflicting_evidence_refs,
        result.superseded_evidence_refs,
        result.non_authoritative_evidence_refs,
        result.latest_attempt_ref,
        result.identity_assurance,
    )


def _assert_permutation_stable(test: unittest.TestCase, items: list[LifecycleProjectionObservation], **kwargs) -> LifecycleProjection:
    expected = _semantic(project_lifecycle(items, **kwargs))
    # Full permutations when small; otherwise a fixed set of rotations and reversals.
    variants: list[list[LifecycleProjectionObservation]]
    if len(items) <= 4:
        variants = [list(order) for order in itertools.permutations(items)]
    else:
        variants = [list(items), list(reversed(items))]
        for offset in range(len(items)):
            variants.append(items[offset:] + items[:offset])
    for order in variants:
        test.assertEqual(_semantic(project_lifecycle(order, **kwargs)), expected)
    return project_lifecycle(items, **kwargs)


class StaticSafetyTests(unittest.TestCase):
    def test_module_contains_no_write_sql_or_db_connector(self) -> None:
        text = (ROOT / "lifecycle_projection.py").read_text(encoding="utf-8")
        # Match SQL write statements / connectors, not English prose such as "does not alter".
        forbidden = re.compile(
            r"(?:INSERT\s+INTO|UPDATE\s+\w+|DELETE\s+FROM|ALTER\s+TABLE|CREATE\s+TABLE|\bCOMMIT\b)"
            r"|import\s+psycopg2|psycopg2\.connect|JOB_READY_DATABASE_URL",
            re.IGNORECASE,
        )
        self.assertIsNone(forbidden.search(text))
        self.assertNotIn("jobready_catalog", text)


class AuthorityPolicyTests(unittest.TestCase):
    def test_default_policy_has_no_provider_ranking(self) -> None:
        policy = default_authority_policy()
        self.assertEqual(policy.policy_id, DEFAULT_AUTHORITY_POLICY_ID)
        ranks = {policy.authority_rank(name) for name in ("greenhouse", "linkedin", "naukri", "indeed")}
        self.assertEqual(ranks, {100})
        self.assertFalse(policy.is_authoritative("injected"))
        self.assertFalse(policy.is_authoritative("foundit"))
        self.assertFalse(policy.is_authoritative("google"))
        self.assertFalse(policy.is_authoritative(""))


class AuthorityPolicyImmutabilityTests(unittest.TestCase):
    def test_ranks_detach_from_caller_mapping(self) -> None:
        source = {"high": 20, "low": 10}
        policy = LifecycleAuthorityPolicy("synthetic-v1", source)
        source["high"] = 1
        source["new"] = 999
        self.assertEqual(policy.authority_rank("high"), 20)
        self.assertIsNone(policy.authority_rank("new"))

    def test_ranks_reject_direct_mutation(self) -> None:
        policy = LifecycleAuthorityPolicy("synthetic-v1", {"high": 20, "low": 10})
        with self.assertRaises(TypeError):
            policy.ranks["high"] = 5
        with self.assertRaises(TypeError):
            policy.ranks["new"] = 100
        with self.assertRaises(TypeError):
            del policy.ranks["low"]

    def test_projection_unchanged_when_mutation_blocked(self) -> None:
        policy = LifecycleAuthorityPolicy("synthetic-v1", {"high": 20, "low": 10})
        items = [
            _obs("high", "confirmed_expired", T10, provider="high"),
            _obs("low", "confirmed_active", T11, provider="low"),
        ]
        before = project_lifecycle(items, policy=policy)
        with self.assertRaises(TypeError):
            policy.ranks["high"] = 5
        with self.assertRaises(TypeError):
            policy.ranks["new"] = 100
        with self.assertRaises(TypeError):
            del policy.ranks["low"]
        after = project_lifecycle(items, policy=policy)
        self.assertEqual(before, after)

    def test_greenhouse_shares_canonical_authority_with_linkedin(self) -> None:
        policy = default_authority_policy()
        self.assertTrue(policy.is_authoritative("greenhouse"))
        linkedin_open = _obs("li", "confirmed_active", T11, provider="linkedin")
        greenhouse_closed = _obs("gh", "confirmed_expired", T11, provider="greenhouse")
        result = project_lifecycle([linkedin_open, greenhouse_closed])
        self.assertEqual((result.decision, result.reason), (UNRESOLVED, REASON_EQUAL_TIME_CONFLICT))
        self.assertTrue(result.review_required)

    def test_default_policy_ranks_cannot_be_mutated(self) -> None:
        policy = default_authority_policy()
        for provider in ("injected", "foundit", "google", "unknown-provider"):
            with self.assertRaises(TypeError):
                policy.ranks[provider] = 999
        self.assertFalse(policy.is_authoritative("injected"))
        self.assertFalse(policy.is_authoritative("foundit"))
        self.assertFalse(policy.is_authoritative("google"))
        self.assertFalse(policy.is_authoritative("unknown-provider"))
        self.assertEqual(
            {policy.authority_rank(name) for name in ("greenhouse", "linkedin", "naukri", "indeed")},
            {100},
        )


class ProjectionMatrixTests(unittest.TestCase):
    """Cases 1-28 from the D16 matrix."""

    def test_01_no_observations(self) -> None:
        result = project_lifecycle([])
        self.assertEqual(result.decision, UNRESOLVED)
        self.assertTrue(result.review_required)
        self.assertEqual(result.reason, REASON_NO_OBSERVATIONS)
        self.assertIsNone(result.latest_attempt_ref)
        self.assertEqual(result.controlling_evidence_refs, ())

    def test_02_inconclusive_only(self) -> None:
        result = _assert_permutation_stable(self, [_obs("a", "inconclusive", T10)])
        self.assertEqual((result.decision, result.reason), (UNRESOLVED, REASON_NO_AUTHORITATIVE_CONCLUSIVE))
        self.assertEqual(result.latest_attempt_ref, "a")
        self.assertEqual(result.non_authoritative_evidence_refs, ())

    def test_03_one_confirmed_open(self) -> None:
        result = project_lifecycle([_obs("a", "confirmed_active", T10)])
        self.assertEqual((result.decision, result.reason, result.review_required), (CONFIRMED_OPEN, REASON_CONFIRMED_OPEN, False))
        self.assertEqual(result.controlling_evidence_refs, ("a",))

    def test_04_one_confirmed_closed(self) -> None:
        result = project_lifecycle([_obs("a", "confirmed_expired", T10)])
        self.assertEqual((result.decision, result.reason), (CONFIRMED_CLOSED, REASON_CONFIRMED_CLOSED))
        self.assertEqual(result.controlling_evidence_refs, ("a",))

    def test_05_older_open_newer_closed(self) -> None:
        items = [_obs("open", "confirmed_active", T10), _obs("closed", "confirmed_expired", T11)]
        result = _assert_permutation_stable(self, items)
        self.assertEqual(result.decision, CONFIRMED_CLOSED)
        self.assertEqual(result.reason, REASON_NEWER_CLOSED_TRANSITION)
        self.assertEqual(result.controlling_evidence_refs, ("closed",))
        self.assertEqual(result.superseded_evidence_refs, ("open",))
        self.assertFalse(result.review_required)

    def test_06_older_closed_newer_open_recovery(self) -> None:
        items = [_obs("closed", "confirmed_expired", T10), _obs("open", "confirmed_active", T11)]
        result = _assert_permutation_stable(self, items)
        self.assertEqual(result.decision, CONFIRMED_OPEN)
        self.assertEqual(result.reason, REASON_NEWER_OPEN_RECOVERY)
        self.assertEqual(result.controlling_evidence_refs, ("open",))
        self.assertEqual(result.superseded_evidence_refs, ("closed",))

    def test_07_equal_time_open_and_closed(self) -> None:
        items = [_obs("open", "confirmed_active", T10), _obs("closed", "confirmed_expired", T10)]
        result = _assert_permutation_stable(self, items)
        self.assertEqual(result.decision, UNRESOLVED)
        self.assertTrue(result.review_required)
        self.assertEqual(result.reason, REASON_EQUAL_TIME_CONFLICT)
        self.assertEqual(result.conflicting_evidence_refs, ("closed", "open"))
        self.assertEqual(result.controlling_evidence_refs, ())

    def test_08_newer_inconclusive_after_open(self) -> None:
        items = [_obs("open", "confirmed_active", T10), _obs("later", "inconclusive", T12)]
        result = _assert_permutation_stable(self, items)
        self.assertEqual(result.decision, CONFIRMED_OPEN)
        self.assertEqual(result.reason, REASON_CONFIRMED_OPEN)
        self.assertEqual(result.controlling_evidence_refs, ("open",))
        self.assertEqual(result.latest_attempt_ref, "later")

    def test_09_newer_inconclusive_after_closed(self) -> None:
        items = [_obs("closed", "confirmed_expired", T10), _obs("later", "inconclusive", T12)]
        result = project_lifecycle(items)
        self.assertEqual(result.decision, CONFIRMED_CLOSED)
        self.assertEqual(result.latest_attempt_ref, "later")

    def test_10_duplicate_same_state_observations(self) -> None:
        items = [
            _obs("a", "confirmed_active", T10),
            _obs("b", "confirmed_active", T10),
        ]
        result = _assert_permutation_stable(self, items)
        self.assertEqual(result.decision, CONFIRMED_OPEN)
        self.assertEqual(result.controlling_evidence_refs, ("a", "b"))
        self.assertEqual(result.conflicting_evidence_refs, ())

    def test_11_multiple_same_state_maximal_evidence(self) -> None:
        items = [
            _obs("a", "confirmed_expired", T11, provider="linkedin"),
            _obs("b", "confirmed_expired", T11, provider="naukri"),
            _obs("c", "confirmed_expired", T11, provider="indeed"),
        ]
        result = _assert_permutation_stable(self, items)
        self.assertEqual(result.decision, CONFIRMED_CLOSED)
        self.assertEqual(result.controlling_evidence_refs, ("a", "b", "c"))

    def test_12_stale_opposite_classified_superseded(self) -> None:
        items = [_obs("stale", "confirmed_active", T10), _obs("fresh", "confirmed_expired", T11)]
        result = project_lifecycle(items)
        self.assertEqual(result.decision, CONFIRMED_CLOSED)
        self.assertEqual(result.superseded_evidence_refs, ("stale",))
        self.assertNotEqual(result.reason, REASON_EQUAL_TIME_CONFLICT)

    def test_13_synthetic_higher_authority_newer_dominates(self) -> None:
        policy = LifecycleAuthorityPolicy("synthetic-ranked-v1", {"high": 20, "low": 10})
        items = [
            _obs("low", "confirmed_expired", T10, provider="low"),
            _obs("high", "confirmed_active", T11, provider="high"),
        ]
        result = _assert_permutation_stable(self, items, policy=policy)
        self.assertEqual(result.decision, CONFIRMED_OPEN)
        self.assertEqual(result.controlling_evidence_refs, ("high",))
        self.assertEqual(result.superseded_evidence_refs, ("low",))

    def test_14_synthetic_lower_authority_newer_authority_time_conflict(self) -> None:
        policy = LifecycleAuthorityPolicy("synthetic-ranked-v1", {"high": 20, "low": 10})
        items = [
            _obs("high", "confirmed_expired", T10, provider="high"),
            _obs("low", "confirmed_active", T11, provider="low"),
        ]
        result = _assert_permutation_stable(self, items, policy=policy)
        self.assertEqual(result.decision, UNRESOLVED)
        self.assertTrue(result.review_required)
        self.assertEqual(result.reason, REASON_AUTHORITY_TIME_CONFLICT)
        self.assertEqual(result.conflicting_evidence_refs, ("high", "low"))

    def test_15_incomparable_synthetic_authority_conflict(self) -> None:
        # Three-way frontier: older high-authority open vs two newer lower-authority
        # opposite sides that do not dominate the older row. Not last-writer-wins.
        policy = LifecycleAuthorityPolicy("synthetic-partial-v1", {"high": 30, "mid": 20, "low": 10})
        items = [
            _obs("high", "confirmed_active", T10, provider="high"),
            _obs("mid", "confirmed_expired", T11, provider="mid"),
            _obs("low", "confirmed_expired", T12, provider="low"),
        ]
        result = _assert_permutation_stable(self, items, policy=policy)
        self.assertEqual(result.decision, UNRESOLVED)
        self.assertTrue(result.review_required)
        self.assertIn(result.reason, {REASON_INCOMPARABLE_CONFLICT, REASON_AUTHORITY_TIME_CONFLICT})
        self.assertIn("high", result.conflicting_evidence_refs)
        self.assertTrue(set(result.conflicting_evidence_refs) >= {"high", "mid"} or set(result.conflicting_evidence_refs) >= {"high", "low"})

    def test_16_injected_only_unresolved_under_default_policy(self) -> None:
        items = [_obs("inj", "confirmed_active", T10, provider="injected", evidence_version="jobready-injected-v1")]
        result = project_lifecycle(items)
        self.assertEqual(result.decision, UNRESOLVED)
        self.assertEqual(result.reason, REASON_NO_AUTHORITATIVE_CONCLUSIVE)
        self.assertEqual(result.non_authoritative_evidence_refs, ("inj",))

    def test_17_canonical_plus_injected_opposite(self) -> None:
        items = [
            _obs("canon", "confirmed_active", T10, provider="linkedin"),
            _obs("inj", "confirmed_expired", T11, provider="injected", evidence_version="jobready-injected-v1"),
        ]
        result = _assert_permutation_stable(self, items)
        self.assertEqual(result.decision, CONFIRMED_OPEN)
        self.assertEqual(result.controlling_evidence_refs, ("canon",))
        self.assertEqual(result.non_authoritative_evidence_refs, ("inj",))
        self.assertEqual(result.latest_attempt_ref, "inj")

    def test_18_historical_v1_null_identity_fields(self) -> None:
        item = _obs(
            "v1",
            "confirmed_active",
            T10,
            evidence_version="jobready-validator-v1",
            expected_identity=None,
            observed_identities=None,
        )
        result = project_lifecycle([item])
        self.assertEqual(result.decision, CONFIRMED_OPEN)
        self.assertEqual(result.identity_assurance, IDENTITY_ASSURANCE_UNKNOWN)

    def test_19_redacted_identity_fields(self) -> None:
        item = _obs(
            "r",
            "confirmed_expired",
            T10,
            expected_identity=REDACTED_IDENTITY,
            observed_identities=(REDACTED_IDENTITY,),
        )
        result = project_lifecycle([item])
        self.assertEqual(result.decision, CONFIRMED_CLOSED)
        self.assertEqual(result.identity_assurance, IDENTITY_ASSURANCE_REDACTED)

    def test_20_unknown_provider_evidence(self) -> None:
        items = [_obs("u", "confirmed_expired", T10, provider="foundit")]
        result = project_lifecycle(items)
        self.assertEqual(result.decision, UNRESOLVED)
        self.assertEqual(result.reason, REASON_NO_AUTHORITATIVE_CONCLUSIVE)
        self.assertEqual(result.non_authoritative_evidence_refs, ("u",))

    def test_21_deterministic_evidence_reference_ordering(self) -> None:
        items = [
            _obs("z", "confirmed_active", T10),
            _obs("a", "confirmed_active", T10),
            _obs("m", "confirmed_active", T10),
        ]
        result = project_lifecycle(list(reversed(items)))
        self.assertEqual(result.controlling_evidence_refs, ("a", "m", "z"))

    def test_22_all_permutations_identical_semantic_output(self) -> None:
        items = [
            _obs("o", "confirmed_active", T10),
            _obs("c", "confirmed_expired", T11),
            _obs("i", "inconclusive", T12),
            _obs("inj", "confirmed_active", T11, provider="injected"),
        ]
        _assert_permutation_stable(self, items)

    def test_23_timezone_aware_timestamp_handling(self) -> None:
        east = timezone(timedelta(hours=5, minutes=30))
        local = datetime(2026, 10, 1, 15, 30, tzinfo=east)  # == T10 UTC
        items = [_obs("a", "confirmed_active", local), _obs("b", "confirmed_expired", T11)]
        result = project_lifecycle(items)
        self.assertEqual(result.decision, CONFIRMED_CLOSED)
        self.assertEqual(result.superseded_evidence_refs, ("a",))

    def test_24_reject_naive_datetime(self) -> None:
        with self.assertRaises(ValueError):
            _obs("a", "confirmed_active", datetime(2026, 10, 1, 10, 0))

    def test_25_duplicate_evidence_ref_behavior(self) -> None:
        twin = _obs("same", "confirmed_active", T10)
        # Identical payload duplicates are accepted (deduped by uniqueness of ref content).
        result = project_lifecycle([twin, twin])
        self.assertEqual(result.controlling_evidence_refs, ("same",))
        conflicting = _obs("same", "confirmed_expired", T10)
        with self.assertRaises(ValueError):
            project_lifecycle([twin, conflicting])

    def test_26_same_timestamp_same_state_resolved(self) -> None:
        items = [_obs("a", "confirmed_active", T10), _obs("b", "confirmed_active", T10)]
        result = project_lifecycle(items)
        self.assertEqual(result.decision, CONFIRMED_OPEN)
        self.assertFalse(result.review_required)

    def test_27_same_timestamp_opposite_state_conflict(self) -> None:
        items = [_obs("a", "confirmed_active", T10), _obs("b", "confirmed_expired", T10)]
        result = project_lifecycle(items)
        self.assertEqual(result.reason, REASON_EQUAL_TIME_CONFLICT)
        self.assertTrue(result.review_required)

    def test_28_latest_attempt_independent_of_controlling(self) -> None:
        items = [
            _obs("control", "confirmed_active", T10),
            _obs("attempt", "inconclusive", T12, reason="linkedin_blocked"),
        ]
        result = project_lifecycle(items)
        self.assertEqual(result.controlling_evidence_refs, ("control",))
        self.assertEqual(result.latest_attempt_ref, "attempt")
        self.assertEqual(result.decision, CONFIRMED_OPEN)


class AdapterAndContractTests(unittest.TestCase):
    def test_ledger_row_adapter(self) -> None:
        row = {
            "id": 42,
            "observed_at": T10,
            "observation": "confirmed_active",
            "provider": "indeed",
            "reason": "indeed_expired_false",
            "evidence_version": "jobready-validator-v2",
            "expected_identity": "0123456789abcdef",
            "observed_identities": ["0123456789abcdef"],
        }
        item = observation_from_ledger_row(row)
        result = project_lifecycle([item])
        self.assertEqual(result.controlling_evidence_refs, ("42",))
        self.assertEqual(result.identity_assurance, IDENTITY_ASSURANCE_PRESENT)

    def test_empty_identity_tuple_is_absent_not_unknown(self) -> None:
        item = _obs("a", "confirmed_active", T10, expected_identity=None, observed_identities=())
        # expected None + empty observed: treated as absent detail on an inspected response.
        self.assertEqual(project_lifecycle([item]).identity_assurance, IDENTITY_ASSURANCE_ABSENT)

    def test_output_collections_are_tuples(self) -> None:
        result = project_lifecycle([_obs("a", "confirmed_active", T10)])
        for name in (
            "controlling_evidence_refs",
            "conflicting_evidence_refs",
            "superseded_evidence_refs",
            "non_authoritative_evidence_refs",
        ):
            self.assertIsInstance(getattr(result, name), tuple)

    def test_does_not_return_catalog_vocabulary(self) -> None:
        for observation in ("confirmed_active", "confirmed_expired"):
            decision = project_lifecycle([_obs("a", observation, T10)]).decision
            self.assertNotIn(decision, {"active", "expired"})


class DocumentationContractTests(unittest.TestCase):
    def test_module_docstring_states_exclusions(self) -> None:
        import lifecycle_projection as module

        doc = module.__doc__ or ""
        for needle in ("link_status", "freshness", "eligibility", "authority"):
            self.assertIn(needle, doc.lower())


if __name__ == "__main__":
    unittest.main()
