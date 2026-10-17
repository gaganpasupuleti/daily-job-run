"""Provider lifecycle transport retries (offline mocks)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
SCRAPER = REPO / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

import httpx  # noqa: E402
import indeed_checker  # noqa: E402
import retry_policy  # noqa: E402
from jobready_validator import probe_indeed, probe_linkedin, probe_naukri, validate_candidates  # noqa: E402
from link_policy import CONFIRMED_EXPIRED, INCONCLUSIVE  # noqa: E402
from test_validation_evidence import linkedin_page  # noqa: E402
from test_validation_provenance import (  # noqa: E402
    INDEED_ROW,
    LINKEDIN_ROW,
    NAUKRI_ROW,
    _linkedin_transport,
    _naukri_transport,
    _Requests,
)

NAUKRI_ID = "051026501163"
NAUKRI_OPEN = {"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}}


def _indeed_client_patch(handler):
    requests = _Requests(handler)
    real_client = httpx.Client

    def client(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(requests), **kwargs)

    return requests, patch.object(indeed_checker.httpx, "Client", client)


class LifecycleRetryProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps: list[float] = []
        self._sleep_patcher = patch.object(retry_policy.time, "sleep", self._record_sleep)
        self._sleep_patcher.start()

    def tearDown(self) -> None:
        self._sleep_patcher.stop()

    def _record_sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def test_linkedin_transient_then_success(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, request=request)
            return httpx.Response(200, text=linkedin_page(), request=request)

        requests, patcher = _linkedin_transport(handler)
        with patcher:
            result = probe_linkedin(LINKEDIN_ROW)
        self.assertEqual(result.observation, "confirmed_active")
        self.assertEqual(calls, 2)
        self.assertEqual(len(self.sleeps), 1)

    def test_linkedin_exhaustion_stays_inconclusive(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(503, request=request)

        requests, patcher = _linkedin_transport(handler)
        with patcher:
            result = probe_linkedin(LINKEDIN_ROW)
        self.assertEqual(result.observation, INCONCLUSIVE)
        self.assertNotEqual(result.observation, CONFIRMED_EXPIRED)
        self.assertEqual(calls, retry_policy.LINKEDIN_LIFECYCLE_RETRY.max_attempts)
        self.assertEqual(len(self.sleeps), retry_policy.LINKEDIN_LIFECYCLE_RETRY.max_attempts - 1)

    def test_linkedin_terminal_closed_no_extra_retry(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(404, request=request)

        requests, patcher = _linkedin_transport(handler)
        with patcher:
            result = probe_linkedin(LINKEDIN_ROW)
        self.assertEqual((result.observation, result.reason), (CONFIRMED_EXPIRED, "linkedin_guest_gone"))
        self.assertEqual(calls, 1)
        self.assertEqual(self.sleeps, [])

    def test_naukri_transient_then_success(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(500, request=request)
            return httpx.Response(200, json=NAUKRI_OPEN, request=request)

        requests, patcher = _naukri_transport(handler)
        with patcher:
            result = probe_naukri(NAUKRI_ROW)
        self.assertEqual(result.observation, "confirmed_active")
        self.assertEqual(calls, 2)

    def test_naukri_exhaustion_stays_inconclusive(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(502, request=request)

        requests, patcher = _naukri_transport(handler)
        with patcher:
            result = probe_naukri(NAUKRI_ROW)
        self.assertEqual(result.observation, INCONCLUSIVE)
        self.assertIn("naukri_http_502", result.reason)
        self.assertNotEqual(result.observation, CONFIRMED_EXPIRED)

    def test_naukri_open_parsing_unchanged_without_retry(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=NAUKRI_OPEN, request=request)

        requests, patcher = _naukri_transport(handler)
        with patcher:
            result = probe_naukri(NAUKRI_ROW)
        self.assertEqual((result.observation, result.reason), ("confirmed_active", "naukri_explicit_open"))

    def test_indeed_transient_then_success(self) -> None:
        attempts = 0
        payload = {"data": {"jobData": {"results": [{"job": {"key": "0123456789abcdef", "expired": False}}]}}}

        def post_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(500, request=request)
            return httpx.Response(200, json=payload, request=request)

        requests, patcher = _indeed_client_patch(post_handler)
        with patch.dict(os.environ, {"INDEED_API_KEY": "fixture-not-a-key"}):
            with patcher:
                result = probe_indeed(INDEED_ROW)
        self.assertEqual(result.observation, "confirmed_active")
        self.assertEqual(attempts, 2)

    def test_indeed_exhaustion_stays_inconclusive(self) -> None:
        attempts = 0

        def post_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503, request=request)

        requests, patcher = _indeed_client_patch(post_handler)
        with patch.dict(os.environ, {"INDEED_API_KEY": "fixture-not-a-key"}):
            with patcher:
                result = probe_indeed(INDEED_ROW)
        self.assertEqual(result.observation, INCONCLUSIVE)
        self.assertEqual(result.reason, "indeed_api_error")
        self.assertNotEqual(result.observation, CONFIRMED_EXPIRED)
        self.assertEqual(attempts, retry_policy.INDEED_LIFECYCLE_RETRY.max_attempts)

    def test_indeed_terminal_expired_no_extra_retry(self) -> None:
        attempts = 0
        payload = {"data": {"jobData": {"results": [{"job": {"key": "0123456789abcdef", "expired": True}}]}}}

        def post_handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, json=payload, request=request)

        requests, patcher = _indeed_client_patch(post_handler)
        with patch.dict(os.environ, {"INDEED_API_KEY": "fixture-not-a-key"}):
            with patcher:
                result = probe_indeed(INDEED_ROW)
        self.assertEqual((result.observation, result.reason), (CONFIRMED_EXPIRED, "indeed_expired_true"))
        self.assertEqual(attempts, 1)

    def test_validate_candidates_one_observation_with_retries(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(503, request=request)
            return httpx.Response(200, text=linkedin_page(), request=request)

        requests, patcher = _linkedin_transport(handler)
        with patcher:
            observations = validate_candidates([LINKEDIN_ROW])
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observation, "confirmed_active")

    def test_linkedin_uses_linkedin_policy_constant(self) -> None:
        self.assertEqual(retry_policy.LINKEDIN_LIFECYCLE_RETRY.policy_id, "linkedin-lifecycle-retry-v1")


if __name__ == "__main__":
    unittest.main()
