from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_route_validation.observation_contract import (  # noqa: E402
    CONFIRMED_CLOSED,
    CONFIRMED_OPEN,
    ROUTE_POLICY_VERSION,
    RouteObservation,
    new_evidence_ref,
)
from application_route_validation.route_evidence_store import JsonlRouteEvidenceStore  # noqa: E402
from application_route_validation.route_projection import project_route  # noqa: E402
from application_route_validation.subjects import SUBJECT_APPLICATION_ROUTE, SUBJECT_LISTING  # noqa: E402


def _obs(subject: str, outcome: str, assured: bool | None = None) -> RouteObservation:
    return RouteObservation(
        evidence_ref=new_evidence_ref(),
        run_id="run-1",
        job_id="J1",
        subject_type=subject,
        subject_id="s1",
        route_id="r1",
        provider="greenhouse_board",
        adapter_version="v2",
        policy_version=ROUTE_POLICY_VERSION,
        observed_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        outcome=outcome,
        reason="test",
        catalog_link_assured=assured,
    )


class RouteProjectionStoreTests(unittest.TestCase):
    def test_projection_requires_assured_link_for_open(self) -> None:
        p = project_route((_obs(SUBJECT_APPLICATION_ROUTE, CONFIRMED_OPEN, False),))
        self.assertNotEqual(p.route_ready_decision, CONFIRMED_OPEN)

    def test_projection_open_when_assured(self) -> None:
        p = project_route((_obs(SUBJECT_APPLICATION_ROUTE, CONFIRMED_OPEN, True),))
        self.assertEqual(p.route_ready_decision, CONFIRMED_OPEN)

    def test_conflict_hold(self) -> None:
        p = project_route(
            (
                _obs(SUBJECT_LISTING, CONFIRMED_OPEN),
                RouteObservation(
                    evidence_ref=new_evidence_ref(),
                    run_id="run-1",
                    job_id="J1",
                    subject_type="employer_requisition",
                    subject_id="rq",
                    route_id="r1",
                    provider="x",
                    adapter_version="v1",
                    policy_version=ROUTE_POLICY_VERSION,
                    observed_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
                    outcome=CONFIRMED_CLOSED,
                    reason="filled",
                ),
                _obs(SUBJECT_APPLICATION_ROUTE, CONFIRMED_OPEN, True),
            )
        )
        self.assertTrue(p.review_hold)

    def test_jsonl_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonlRouteEvidenceStore(Path(tmp) / "route.jsonl")
            item = _obs(SUBJECT_LISTING, CONFIRMED_OPEN)
            store.append(item)
            loaded = store.load_all()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].outcome, CONFIRMED_OPEN)


if __name__ == "__main__":
    unittest.main()
