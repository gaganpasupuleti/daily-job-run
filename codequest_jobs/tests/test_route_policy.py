from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_route_validation.route_policy import should_verify_application_route  # noqa: E402
from link_policy import INCONCLUSIVE  # noqa: E402


class RoutePolicyTests(unittest.TestCase):
    def test_indeed_missing_key_still_verifies_greenhouse_apply(self) -> None:
        ok, reason = should_verify_application_route(
            source="indeed",
            job_url="https://in.indeed.com/viewjob?jk=abc",
            apply_url="https://job-boards.greenhouse.io/acme/jobs/123",
            listing_observation=INCONCLUSIVE,
            listing_reason="indeed_missing_api_key",
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "greenhouse_board_route")

    def test_indeed_missing_key_skips_indeed_only_apply(self) -> None:
        ok, reason = should_verify_application_route(
            source="indeed",
            job_url="https://in.indeed.com/viewjob?jk=abc",
            apply_url="https://in.indeed.com/viewjob?jk=abc",
            listing_observation=INCONCLUSIVE,
            listing_reason="indeed_missing_api_key",
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "listing_config_skip")

    def test_naukri_empty_detail_enables_render_experiment(self) -> None:
        ok, reason = should_verify_application_route(
            source="naukri",
            job_url="https://www.naukri.com/job-listings-x-121026007036",
            apply_url="https://www.naukri.com/job-listings-x-121026007036",
            listing_observation=INCONCLUSIVE,
            listing_reason="naukri_empty_detail",
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "naukri_rendered_page_experiment")


if __name__ == "__main__":
    unittest.main()
