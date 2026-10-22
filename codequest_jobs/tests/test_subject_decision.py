from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_route_validation.contracts import CONFIRMED_CLOSED, CONFIRMED_OPEN, UNRESOLVED, RouteDimension  # noqa: E402
from application_route_validation.subject_decision import decide_subjects  # noqa: E402


class SubjectDecisionTests(unittest.TestCase):
    def test_requisition_closed_form_open_conflict(self) -> None:
        out = decide_subjects(
            listing=RouteDimension(CONFIRMED_OPEN, "l", True),
            requisition=RouteDimension(CONFIRMED_CLOSED, "filled", True),
            application_route=RouteDimension(CONFIRMED_OPEN, "form", True),
            catalog_link_assured=True,
        )
        self.assertTrue(out.review_hold)
        self.assertEqual(out.route_ready, UNRESOLVED)

    def test_open_route_requires_assured_link(self) -> None:
        out = decide_subjects(
            listing=RouteDimension(UNRESOLVED, "l", None),
            requisition=RouteDimension(UNRESOLVED, "r", None),
            application_route=RouteDimension(CONFIRMED_OPEN, "apply", True),
            catalog_link_assured=None,
        )
        self.assertEqual(out.route_ready, UNRESOLVED)
        self.assertEqual(out.route_ready_reason, "catalog_identity_link_unknown")


if __name__ == "__main__":
    unittest.main()
