"""Durable requisition identity evidence: expected_identity and observed_identities in the ledger.

Provider cases run offline against httpx.MockTransport. Database cases use disposable
Postgres named by JOBREADY_DAILY_CATALOG_URL and are skipped when it is absent.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import fields
from datetime import datetime, timezone
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
import psycopg2  # noqa: E402
import psycopg2.errors  # noqa: E402

import indeed_checker  # noqa: E402
import retry_policy  # noqa: E402
import jobready_catalog as catalog  # noqa: E402
import run_jobready_daily as daily  # noqa: E402
from jobready_validator import (  # noqa: E402
    JOBREADY_EVIDENCE_VERSION,
    LinkObservation,
    ProbeResult,
    UnsupportedSourceError,
    interpret_linkedin,
    interpret_linkedin_evidence,
    interpret_naukri,
    interpret_naukri_evidence,
    probe_indeed,
    probe_linkedin,
    probe_naukri,
    validate_candidates,
)
from link_policy import (  # noqa: E402
    EVIDENCE_IDENTITY_MAX_COUNT,
    REDACTED_IDENTITY,
    evidence_identities,
    evidence_identity,
)
from normalize import UnifiedJob  # noqa: E402
from run_all_sources import ScrapeResult  # noqa: E402
from test_validation_evidence import JOB_ID, linkedin_page  # noqa: E402
from test_validation_provenance import (  # noqa: E402
    GUEST,
    INDEED_ROW,
    LINKEDIN_ROW,
    NAUKRI_ENDPOINT,
    NAUKRI_ID,
    NAUKRI_ROW,
    SECRET,
    _linkedin_transport,
    _naukri_transport,
    _provenance,
    _refuse,
    _Requests,
)

CATALOG_URL = os.environ.get("JOBREADY_DAILY_CATALOG_URL", "").strip()
TABLE = "jobready_link_observations"
IDENTITY_COLUMNS = ("expected_identity", "observed_identities")
T10 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
T11 = datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc)
T12 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
JK = "0123456789abcdef"
OTHER_JK = "fedcba9876543210"
FIXTURE_KEY = "fixture-not-a-key"


def _retryable_status_attempts(status: int, policy: retry_policy.LifecycleRetryPolicy) -> int:
    if status in policy.retryable_status_codes:
        return policy.max_attempts
    return 1


def _identity(item) -> tuple:
    return item.expected_identity, item.observed_identities


def _verdict(item) -> tuple:
    return item.observation, item.reason


def _job(url: str, *, source: str = "naukri") -> UnifiedJob:
    return UnifiedJob(
        job_id="upstream-id",
        source=source,
        title="Python Developer",
        company="Example Co",
        location="Bengaluru",
        description="Build Python APIs with Django and PostgreSQL for fresher engineers.",
        job_url=url,
        role_family="python-dev",
        ingest_profile="python-dev",
        apply_url=url,
        job_type="fulltime",
    )


def _indeed_transport(handler):
    requests = _Requests(handler)
    real_client = httpx.Client

    def client(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(requests), **kwargs)

    return requests, patch.object(indeed_checker.httpx, "Client", client)


def _indeed_results(*jobs) -> dict:
    return {"data": {"jobData": {"results": [{"job": job} for job in jobs]}}}


class IdentityBoundingTests(unittest.TestCase):
    def test_plain_ids_are_kept_as_is(self) -> None:
        for value in (JOB_ID, NAUKRI_ID, JK, "A-1_b", "x" * 64):
            self.assertEqual(evidence_identity(value), value)

    def test_anything_else_is_redacted(self) -> None:
        for value in (
            f"https://example.com/job?token={SECRET}",
            f"<a href='{SECRET}'>",
            "x" * 65,
            "",
            " 123",
            "123\n",
            "-123",
            "12 34",
            "١٢٣",
            123,
            12.5,
            True,
            b"123",
            ["123"],
        ):
            self.assertEqual(evidence_identity(value), REDACTED_IDENTITY, repr(value))

    def test_none_is_kept(self) -> None:
        self.assertIsNone(evidence_identity(None))
        self.assertIsNone(evidence_identities(None))

    def test_none_and_empty_stay_distinct(self) -> None:
        self.assertEqual(evidence_identities([]), ())
        self.assertEqual(evidence_identities([None]), ())
        self.assertIsNone(evidence_identities(None))

    def test_identities_are_deduplicated_sorted_and_order_independent(self) -> None:
        values = ["9", "10", "9", None, f"?t={SECRET}", 5]
        stored = evidence_identities(values)
        self.assertEqual(stored, ("10", "9", REDACTED_IDENTITY))
        self.assertEqual(evidence_identities(reversed(values)), stored)
        self.assertEqual(evidence_identities(frozenset(["9", "10"])), ("10", "9"))
        self.assertNotIn(SECRET, repr(stored))

    def test_identities_are_capped(self) -> None:
        values = [f"{index:03d}" for index in range(40)]
        stored = evidence_identities(values)
        self.assertEqual(len(stored), EVIDENCE_IDENTITY_MAX_COUNT)
        self.assertEqual(stored, tuple(sorted(values))[:EVIDENCE_IDENTITY_MAX_COUNT])

    def test_a_single_string_is_not_a_collection(self) -> None:
        for value in ("123", b"123"):
            with self.assertRaises(TypeError):
                evidence_identities(value)

    def test_probe_result_stores_only_the_bounded_form(self) -> None:
        result = ProbeResult(
            "inconclusive",
            "r",
            "naukri",
            expected_identity=f"{NAUKRI_ID}?k={SECRET}",
            observed_identities=["2", "1", "2", f"<{SECRET}>"],
        )
        self.assertEqual(_identity(result), (REDACTED_IDENTITY, ("1", "2", REDACTED_IDENTITY)))
        self.assertNotIn(SECRET, repr(result))

    def test_identity_fields_default_to_none(self) -> None:
        self.assertEqual(_identity(ProbeResult("inconclusive", "r", "naukri")), (None, None))
        self.assertEqual(_identity(LinkObservation("CQJ-1", "active", "inconclusive", "naukri", "r")), (None, None))

    def test_existing_positional_fields_keep_their_order(self) -> None:
        self.assertEqual(
            [item.name for item in fields(ProbeResult)],
            ["observation", "reason", "provider", "requested_url", "final_url", "http_status", *IDENTITY_COLUMNS],
        )
        self.assertEqual(
            [item.name for item in fields(LinkObservation)][:11],
            [
                "job_id",
                "previous_status",
                "observation",
                "provider",
                "reason",
                "observed_at",
                "evidence_version",
                "requested_url",
                "final_url",
                "http_status",
                "expected_identity",
            ],
        )

    def test_legacy_probe_tuple_carries_no_identity(self) -> None:
        rows = [{"job_id": "CQJ-1", "source": "probe", "link_status": "active"}]
        (item,) = validate_candidates(rows, probes={"probe": lambda row: ("inconclusive", "legacy", "legacy")})
        self.assertEqual(_identity(item), (None, None))

    def test_probe_error_carries_no_identity(self) -> None:
        def broken(row):
            raise RuntimeError(SECRET)

        rows = [{"job_id": "CQJ-1", "source": "probe", "link_status": "active"}]
        (item,) = validate_candidates(rows, probes={"probe": broken})
        self.assertEqual((item.reason, *_identity(item)), ("probe_probe_error", None, None))

    def test_probe_identity_reaches_the_observation(self) -> None:
        rows = [{"job_id": "CQJ-1", "source": "probe", "link_status": "active"}]
        result = ProbeResult("confirmed_active", "r", "probe", expected_identity="7", observed_identities=["8", "7"])
        (item,) = validate_candidates(rows, probes={"probe": lambda row: result})
        self.assertEqual(_identity(item), ("7", ("7", "8")))
        self.assertEqual(item.evidence_version, "jobready-validator-v2")

    def test_unsupported_sources_are_refused_before_any_probe(self) -> None:
        calls: list[str] = []
        rows = [{"job_id": "CQJ-1", "source": "foundit", "link_status": "active"}]
        with self.assertRaises(UnsupportedSourceError):
            validate_candidates(rows, probes={"naukri": lambda row: calls.append("probe")})
        self.assertEqual(calls, [])


class LinkedInIdentityTests(unittest.TestCase):
    """Provider matrix 1-9."""

    def _probe(self, handler):
        requests, patcher = _linkedin_transport(handler)
        with patcher:
            result = probe_linkedin(LINKEDIN_ROW)
        return result, requests

    def _page(self, body: str, status: int = 200):
        expected = _retryable_status_attempts(status, retry_policy.LINKEDIN_LIFECYCLE_RETRY)
        result, requests = self._probe(lambda request: httpx.Response(status, text=body))
        self.assertEqual(len(requests.urls), expected)
        return result

    def test_01_match(self) -> None:
        result = self._page(linkedin_page())
        self.assertEqual(_verdict(result), ("confirmed_active", "linkedin_identity_apply"))
        self.assertEqual(_identity(result), (JOB_ID, (JOB_ID,)))

    def test_02_missing(self) -> None:
        result = self._page(linkedin_page(identity=None))
        self.assertEqual(_verdict(result), ("inconclusive", "linkedin_identity_missing"))
        self.assertEqual(_identity(result), (JOB_ID, ()))

    def test_03_mismatch(self) -> None:
        result = self._page(linkedin_page(identity="1234567890"))
        self.assertEqual(_verdict(result), ("inconclusive", "linkedin_identity_mismatch"))
        self.assertEqual(_identity(result), (JOB_ID, ("1234567890",)))

    def test_04_multiple(self) -> None:
        other = '<code id="decoratedJobPostingId"><!--"1234567890"--></code>'
        result = self._page(linkedin_page(after=other))
        self.assertEqual(_verdict(result), ("inconclusive", "linkedin_identity_mismatch"))
        self.assertEqual(_identity(result), (JOB_ID, tuple(sorted(["1234567890", JOB_ID]))))

    def test_05_redirect(self) -> None:
        landing = f"https://www.linkedin.com/jobs/view/{JOB_ID}"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GUEST:
                return httpx.Response(302, headers={"Location": f"{landing}?token={SECRET}"})
            return httpx.Response(200, text=linkedin_page())

        result, _ = self._probe(handler)
        self.assertEqual(_verdict(result), ("inconclusive", "linkedin_redirected"))
        self.assertEqual(_identity(result), (JOB_ID, None))
        self.assertNotIn(SECRET, repr(result))

    def test_06_blocked_status_reads_no_identity(self) -> None:
        for status in (401, 403, 429, 999, 503):
            result = self._page(linkedin_page(), status=status)
            self.assertEqual(_verdict(result), ("inconclusive", f"linkedin_http_{status}"))
            self.assertEqual(_identity(result), (JOB_ID, None), status)

    def test_06_captcha_page_reports_what_it_carried(self) -> None:
        result = self._page("<h1>Quick security check</h1><p>Complete the captcha</p>")
        self.assertEqual(_verdict(result), ("inconclusive", "linkedin_blocked"))
        self.assertEqual(_identity(result), (JOB_ID, ()))

    def test_07_connection_failure(self) -> None:
        result, requests = self._probe(_refuse)
        self.assertEqual(_verdict(result), ("inconclusive", "linkedin_connection_failure"))
        self.assertEqual(len(requests.urls), retry_policy.LINKEDIN_LIFECYCLE_RETRY.max_attempts)
        self.assertEqual(_identity(result), (JOB_ID, None))

    def test_08_404(self) -> None:
        result = self._page(linkedin_page(), status=404)
        self.assertEqual(_verdict(result), ("confirmed_expired", "linkedin_guest_gone"))
        self.assertEqual(_identity(result), (JOB_ID, None))

    def test_09_410(self) -> None:
        result = self._page(linkedin_page(), status=410)
        self.assertEqual(_verdict(result), ("confirmed_expired", "linkedin_guest_gone"))
        self.assertEqual(_identity(result), (JOB_ID, None))

    def test_unrecognized_url_has_no_identity_and_makes_no_request(self) -> None:
        requests, patcher = _linkedin_transport(lambda request: httpx.Response(200))
        with patcher:
            result = probe_linkedin(dict(LINKEDIN_ROW, job_url="https://example.com/not-linkedin"))
        self.assertEqual((result.reason, *_identity(result)), ("linkedin_unrecognized_url", None, None))
        self.assertEqual(requests.urls, [])

    def test_identity_never_comes_from_the_request(self) -> None:
        # The requested URL names JOB_ID, but the page does not: nothing is invented.
        result = self._page("<html><body>ok</body></html>")
        self.assertEqual(result.observed_identities, ())

    def test_evidence_wrapper_matches_the_verdict_function(self) -> None:
        cases = [
            dict(status_code=200, body=linkedin_page(), final_url=GUEST),
            dict(status_code=200, body=linkedin_page(identity="1"), final_url=GUEST),
            dict(status_code=200, body=linkedin_page(closed=True, apply=""), final_url=GUEST),
            dict(status_code=200, body="<h1>Page not found</h1>", final_url=GUEST),
            dict(status_code=404, body="", final_url=GUEST),
            dict(status_code=302, body="", final_url="https://www.linkedin.com/login"),
            dict(status_code=None, body=None, final_url=None, error="connection"),
        ]
        for case in cases:
            evidence = interpret_linkedin_evidence(expected_job_id=JOB_ID, **case)
            self.assertEqual(evidence[:2], interpret_linkedin(expected_job_id=JOB_ID, **case), case)


class NaukriIdentityTests(unittest.TestCase):
    """Provider matrix 10-17."""

    def _probe(self, handler):
        requests, patcher = _naukri_transport(handler)
        with patcher:
            result = probe_naukri(NAUKRI_ROW)
        return result, requests

    def _json(self, payload, status: int = 200):
        expected = _retryable_status_attempts(status, retry_policy.NAUKRI_LIFECYCLE_RETRY)
        result, requests = self._probe(lambda request: httpx.Response(status, json=payload))
        self.assertEqual(len(requests.urls), expected)
        return result

    def test_10_match(self) -> None:
        result = self._json({"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}})
        self.assertEqual(_verdict(result), ("confirmed_active", "naukri_explicit_open"))
        self.assertEqual(_identity(result), (NAUKRI_ID, (NAUKRI_ID,)))

    def test_11_missing(self) -> None:
        result = self._json({"job": {"jobStatus": "active", "title": "Python Developer"}})
        self.assertEqual(_verdict(result), ("inconclusive", "naukri_identity_missing"))
        self.assertEqual(_identity(result), (NAUKRI_ID, ()))

    def test_12_mismatch(self) -> None:
        result = self._json({"job": {"jobId": "999999999999", "jobStatus": "expired"}})
        self.assertEqual(_verdict(result), ("inconclusive", "naukri_identity_mismatch"))
        self.assertEqual(_identity(result), (NAUKRI_ID, ("999999999999",)))

    def test_13_multiple_fields(self) -> None:
        result = self._json({"jobDetails": {"jobId": NAUKRI_ID, "id": "777", "jobStatus": "active"}})
        self.assertEqual(_verdict(result), ("inconclusive", "naukri_identity_mismatch"))
        self.assertEqual(_identity(result), (NAUKRI_ID, (NAUKRI_ID, "777")))

    def test_13_multiple_agreeing_fields_are_deduplicated(self) -> None:
        result = self._json({"job": {"jobId": NAUKRI_ID, "jobid": f" {NAUKRI_ID} ", "jobStatus": "active"}})
        self.assertEqual(_verdict(result), ("confirmed_active", "naukri_explicit_open"))
        self.assertEqual(_identity(result), (NAUKRI_ID, (NAUKRI_ID,)))

    def test_14_malformed(self) -> None:
        result, _ = self._probe(lambda request: httpx.Response(200, text="<html>not json</html>"))
        self.assertEqual(_verdict(result), ("inconclusive", "naukri_malformed"))
        self.assertEqual(_identity(result), (NAUKRI_ID, None))
        result = self._json([{"jobId": NAUKRI_ID}])
        self.assertEqual(_verdict(result), ("inconclusive", "naukri_malformed"))
        self.assertEqual(_identity(result), (NAUKRI_ID, None))

    def test_14_unsafe_identity_values_are_redacted(self) -> None:
        result = self._json({"job": {"jobId": f"https://evil.example/?k={SECRET}", "jobStatus": "active"}})
        self.assertEqual(_verdict(result), ("inconclusive", "naukri_identity_mismatch"))
        self.assertEqual(_identity(result), (NAUKRI_ID, (REDACTED_IDENTITY,)))
        self.assertNotIn(SECRET, repr(result))

    def test_15_empty_detail(self) -> None:
        result = self._json({"job": {}})
        self.assertEqual(_verdict(result), ("inconclusive", "naukri_empty_detail"))
        self.assertEqual(_identity(result), (NAUKRI_ID, ()))

    def test_15_no_detail_object(self) -> None:
        for payload in ({}, {"job": None}, {"job": "x", "jobDetails": []}):
            result = self._json(payload)
            self.assertEqual(_verdict(result), ("inconclusive", "naukri_empty_detail"), payload)
            self.assertEqual(_identity(result), (NAUKRI_ID, None), payload)

    def test_16_connection_failure(self) -> None:
        result, requests = self._probe(_refuse)
        self.assertEqual(_verdict(result), ("inconclusive", "naukri_connection_failure"))
        self.assertEqual(len(requests.urls), retry_policy.NAUKRI_LIFECYCLE_RETRY.max_attempts)
        self.assertEqual(_identity(result), (NAUKRI_ID, None))

    def test_17_non_200(self) -> None:
        for status in (404, 410, 429, 503):
            result = self._json({"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}}, status=status)
            self.assertEqual(_verdict(result), ("inconclusive", f"naukri_http_{status}"))
            self.assertEqual(_identity(result), (NAUKRI_ID, None), status)

    def test_redirect_reads_no_identity(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/jobapi/"):
                return httpx.Response(302, headers={"Location": f"https://www.naukri.com/nlogin/login?k={SECRET}"})
            return httpx.Response(200, json={"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}})

        result, _ = self._probe(handler)
        self.assertEqual(_verdict(result), ("inconclusive", "naukri_redirected"))
        self.assertEqual(_identity(result), (NAUKRI_ID, None))

    def test_unrecognized_url_has_no_identity(self) -> None:
        requests, patcher = _naukri_transport(lambda request: httpx.Response(200))
        with patcher:
            result = probe_naukri(dict(NAUKRI_ROW, job_url="https://www.naukri.com/no-id-here"))
        self.assertEqual((result.reason, *_identity(result)), ("naukri_unrecognized_url", None, None))
        self.assertEqual(requests.urls, [])

    def test_evidence_wrapper_matches_the_verdict_function(self) -> None:
        cases = [
            dict(status_code=200, payload={"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}}, final_url=NAUKRI_ENDPOINT),
            dict(status_code=200, payload={"job": {"jobId": "1", "jobStatus": "active"}}, final_url=NAUKRI_ENDPOINT),
            dict(status_code=200, payload={"job": {}}, final_url=NAUKRI_ENDPOINT),
            dict(status_code=200, payload=None, final_url=NAUKRI_ENDPOINT),
            dict(status_code=503, payload=None, final_url=NAUKRI_ENDPOINT),
            dict(status_code=None, payload=None, final_url=None, error="connection"),
        ]
        for case in cases:
            evidence = interpret_naukri_evidence(expected_job_id=NAUKRI_ID, **case)
            self.assertEqual(evidence[:2], interpret_naukri(expected_job_id=NAUKRI_ID, **case), case)


class IndeedIdentityTests(unittest.TestCase):
    """Provider matrix 18-26. The GraphQL endpoint is a mock transport; no request leaves the process."""

    def _probe(self, handler, row=INDEED_ROW, key: str = FIXTURE_KEY):
        requests, patcher = _indeed_transport(handler)
        with patch.dict(os.environ, {"INDEED_API_KEY": key}), patcher:
            result = probe_indeed(row)
        return result, requests

    def _answer(self, payload, status: int = 200):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(status, json=payload)

        result, requests = self._probe(handler)
        self.assertEqual(requests.urls, [indeed_checker.INDEED_GRAPHQL])
        (request,) = seen
        self.assertEqual(request.method, "POST")
        self.assertIn(json.dumps([JK]), json.loads(request.content)["query"])
        self.assertEqual(_provenance(result), (indeed_checker.INDEED_GRAPHQL, None, None))
        self.assertNotIn(FIXTURE_KEY, repr(result))
        return result

    def test_18_matching_active_key(self) -> None:
        result = self._answer(_indeed_results({"key": JK, "expired": False}))
        self.assertEqual(_verdict(result), ("confirmed_active", "indeed_expired_false"))
        self.assertEqual(_identity(result), (JK, (JK,)))

    def test_19_matching_expired_key(self) -> None:
        result = self._answer(_indeed_results({"key": JK, "expired": True}))
        self.assertEqual(_verdict(result), ("confirmed_expired", "indeed_expired_true"))
        self.assertEqual(_identity(result), (JK, (JK,)))

    def test_20_missing_requested_key(self) -> None:
        result = self._answer(_indeed_results())
        self.assertEqual(_verdict(result), ("inconclusive", "indeed_api_error"))
        self.assertEqual(_identity(result), (JK, ()))

    def test_21_unexpected_returned_key(self) -> None:
        result = self._answer(_indeed_results({"key": OTHER_JK, "expired": True}))
        self.assertEqual(_verdict(result), ("inconclusive", "indeed_api_error"))
        self.assertEqual(_identity(result), (JK, (OTHER_JK,)))

    def test_21_unexpected_key_beside_the_requested_one(self) -> None:
        result = self._answer(_indeed_results({"key": JK, "expired": False}, {"key": OTHER_JK, "expired": True}))
        self.assertEqual(_verdict(result), ("confirmed_active", "indeed_expired_false"))
        self.assertEqual(_identity(result), (JK, (JK, OTHER_JK)))

    def test_22_conflicting_duplicate_key(self) -> None:
        result = self._answer(_indeed_results({"key": JK, "expired": True}, {"key": JK, "expired": False}))
        self.assertEqual(_verdict(result), ("inconclusive", "indeed_api_error"))
        self.assertEqual(_identity(result), (JK, (JK,)))

    def test_23_malformed_response(self) -> None:
        for payload in (None, {"data": None}, {"data": {"jobData": {"results": "x"}}}, ["x"]):
            result = self._answer(payload)
            self.assertEqual(_verdict(result), ("inconclusive", "indeed_api_error"), payload)
            self.assertEqual(_identity(result), (JK, None), payload)

    def test_23_undecodable_and_non_200_responses(self) -> None:
        cases = (
            (lambda request: httpx.Response(200, text="<html>not json</html>"), 1),
            (lambda request: httpx.Response(500, json=_indeed_results({"key": JK, "expired": True})), retry_policy.INDEED_LIFECYCLE_RETRY.max_attempts),
            (lambda request: httpx.Response(403, text="forbidden"), 1),
        )
        for handler, expected_attempts in cases:
            result, requests = self._probe(handler)
            self.assertEqual(len(requests.urls), expected_attempts)
            self.assertEqual(_verdict(result), ("inconclusive", "indeed_api_error"))
            self.assertEqual(_identity(result), (JK, None))

    def test_23_unsafe_returned_keys_are_redacted(self) -> None:
        result = self._answer(
            _indeed_results({"key": f"https://x.example/?t={SECRET}", "expired": False}, {"key": 42, "expired": False})
        )
        self.assertEqual(_verdict(result), ("inconclusive", "indeed_api_error"))
        self.assertEqual(_identity(result), (JK, (REDACTED_IDENTITY,)))
        self.assertNotIn(SECRET, repr(result))

    def test_24_connection_failure(self) -> None:
        result, requests = self._probe(_refuse)
        self.assertEqual(len(requests.urls), retry_policy.INDEED_LIFECYCLE_RETRY.max_attempts)
        self.assertEqual(_verdict(result), ("inconclusive", "indeed_api_error"))
        self.assertEqual(_identity(result), (JK, None))

    def test_25_missing_api_key(self) -> None:
        result, requests = self._probe(lambda request: httpx.Response(200), key="")
        self.assertEqual(requests.urls, [])
        self.assertEqual(_verdict(result), ("inconclusive", "indeed_missing_api_key"))
        self.assertEqual(_identity(result), (JK, None))
        self.assertEqual(_provenance(result), (None, None, None))

    def test_26_missing_url_jk(self) -> None:
        result, requests = self._probe(
            lambda request: httpx.Response(200), row=dict(INDEED_ROW, job_url="https://in.indeed.com/viewjob")
        )
        self.assertEqual(requests.urls, [])
        self.assertEqual(_verdict(result), ("inconclusive", "indeed_missing_job_key"))
        self.assertEqual(_identity(result), (None, None))
        self.assertEqual(_provenance(result), (None, None, None))


class IndeedHelperCompatibilityTests(unittest.TestCase):
    """The batch helper and check_indeed_via_api keep their verdicts after the lookup refactor."""

    def _batch(self, handler, jks, key: str = FIXTURE_KEY):
        requests, patcher = _indeed_transport(handler)
        with patch.dict(os.environ, {"INDEED_API_KEY": key}), patcher:
            return indeed_checker.batch_indeed_expired(jks), requests

    def test_batch_flags(self) -> None:
        payload = _indeed_results(
            {"key": JK, "expired": True},
            {"key": OTHER_JK, "expired": False},
            {"key": "aaaaaaaaaaaaaaaa", "expired": None},
            {"key": "unrequested00000", "expired": True},
        )
        result, requests = self._batch(
            lambda request: httpx.Response(200, json=payload), [JK, OTHER_JK, "aaaaaaaaaaaaaaaa", JK]
        )
        self.assertEqual(result, {JK: True, OTHER_JK: False, "aaaaaaaaaaaaaaaa": None})
        self.assertEqual(len(requests.urls), 1)

    def test_batch_failures_are_unknown(self) -> None:
        for handler in (lambda request: httpx.Response(500), _refuse, lambda request: httpx.Response(200, text="x")):
            result, _ = self._batch(handler, [JK])
            self.assertEqual(result, {JK: None})

    def test_batch_without_key_makes_no_request(self) -> None:
        result, requests = self._batch(lambda request: httpx.Response(200), [JK], key="")
        self.assertEqual((result, requests.urls), ({JK: None}, []))

    def test_check_via_api(self) -> None:
        for flag, expected in ((True, "expired"), (False, "active"), (None, None)):
            requests, patcher = _indeed_transport(
                lambda request, flag=flag: httpx.Response(200, json=_indeed_results({"key": JK, "expired": flag}))
            )
            with patch.dict(os.environ, {"INDEED_API_KEY": FIXTURE_KEY}), patcher:
                self.assertEqual(indeed_checker.check_indeed_via_api(JK), expected)

    def test_returned_keys_keep_none_and_empty_distinct(self) -> None:
        self.assertIsNone(indeed_checker.indeed_returned_keys(None))
        self.assertIsNone(indeed_checker.indeed_returned_keys({"data": {"jobData": {}}}))
        self.assertEqual(indeed_checker.indeed_returned_keys(_indeed_results()), [])
        self.assertEqual(
            indeed_checker.indeed_returned_keys({"data": {"jobData": {"results": [{"job": {"key": JK}}, "x", {"job": {}}]}}}),
            [JK],
        )


class WriterIdentityValidationTests(unittest.TestCase):
    class _NoSql:
        def cursor(self):
            raise AssertionError("SQL attempted for a malformed batch")

    def test_unbounded_identity_is_rejected_before_sql(self) -> None:
        bad = (
            {"expected_identity": f"https://example.com/?k={SECRET}"},
            {"expected_identity": 123},
            {"expected_identity": ""},
            {"observed_identities": (f"<{SECRET}>",)},
            {"observed_identities": ("2", "1")},
            {"observed_identities": ("1", "1")},
            {"observed_identities": "123"},
            {"observed_identities": frozenset({"1"})},
            {"observed_identities": (None,)},
            {"observed_identities": tuple(f"{index:03d}" for index in range(EVIDENCE_IDENTITY_MAX_COUNT + 1))},
        )
        for extra in bad:
            item = LinkObservation("CQJ-1", "active", "inconclusive", "naukri", "r", observed_at=T10, **extra)
            with self.assertRaises(ValueError) as raised:
                catalog.apply_link_observations(self._NoSql(), [item])
            self.assertNotIn(SECRET, str(raised.exception), extra)


class _CatalogCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.conn = psycopg2.connect(CATALOG_URL, connect_timeout=8)
        except Exception:
            raise unittest.SkipTest("disposable database unavailable") from None
        catalog.ensure_schema(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.rollback()
        catalog.ensure_schema(cls.conn)
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.rollback()
        catalog.ensure_schema(self.conn)
        self._exec(f"TRUNCATE {TABLE}, validated_jobs RESTART IDENTITY")
        self.addCleanup(self._restore_schema)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        for target in ("OUTPUT_DIR", "RUNS_DIR"):
            patcher = patch.object(daily, target, self.base / target.lower())
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore_schema(self) -> None:
        self.conn.rollback()
        catalog.ensure_schema(self.conn)

    def _exec(self, sql: str, params=None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
        self.conn.commit()

    def _fetch(self, sql: str, params=None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        self.conn.commit()
        return rows

    def _seed(self, count: int, prefix: str = "d4") -> list[str]:
        urls = [f"https://example.test/jobs/{prefix}-{index:04d}" for index in range(count)]
        with redirect_stdout(io.StringIO()):
            catalog.upsert_jobs(self.conn, [daily.prepare_catalog_row(_job(url)) for url in urls])
        by_url = dict(self._fetch("SELECT job_url, job_id FROM validated_jobs WHERE job_url = ANY(%s)", (urls,)))
        return [by_url[url] for url in urls]

    def _rows(self, job_id: str | None = None) -> list[tuple]:
        where = "WHERE job_id = %s" if job_id else ""
        return self._fetch(
            f"SELECT observation, reason, expected_identity, observed_identities, evidence_version "
            f"FROM {TABLE} {where} ORDER BY id",
            (job_id,) if job_id else None,
        )

    def _state(self, job_id: str) -> tuple:
        return self._fetch(
            "SELECT link_status, last_confirmed_open_at, last_confirmed_closed_at FROM validated_jobs WHERE job_id = %s",
            (job_id,),
        )[0]

    def _count(self) -> int:
        return self._fetch(f"SELECT COUNT(*) FROM {TABLE}")[0][0]

    def _with_catalog_env(self, function):
        output = io.StringIO()
        with patch.dict(os.environ, {"JOB_READY_DATABASE_URL": CATALOG_URL}), redirect_stdout(output):
            code = function()
        return code, output.getvalue()

    def _drop_identity_columns(self) -> None:
        """Simulate a ledger created before identity evidence (production before the explicit helper)."""
        self._exec(
            f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS expected_identity, DROP COLUMN IF EXISTS observed_identities"
        )
        self.assertEqual(self._identity_columns(), {})

    def _identity_columns(self) -> dict[str, tuple]:
        return {
            row[0]: (row[1], row[2], row[3], row[4])
            for row in self._fetch(
                """
                SELECT column_name, data_type, udt_name, is_nullable, column_default FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s AND column_name = ANY(%s)
                """,
                (TABLE, list(IDENTITY_COLUMNS)),
            )
        }

    def _historical(self, job_id: str, observed_at: datetime = T10) -> None:
        self._exec(
            f"INSERT INTO {TABLE} (job_id, observed_at, observation, reason, provider, evidence_version) "
            "VALUES (%s, %s, 'confirmed_active', 'naukri_explicit_open', 'naukri', 'jobready-validator-v1')",
            (job_id, observed_at),
        )


EXPECTED_IDENTITY_COLUMNS = {
    "expected_identity": ("text", "text", "YES", None),
    "observed_identities": ("ARRAY", "_text", "YES", None),
}


@unittest.skipUnless(CATALOG_URL, "disposable daily catalog is not configured")
class IdentitySchemaTests(_CatalogCase):
    def test_fresh_schema_has_the_identity_columns(self) -> None:
        self._exec(f"DROP TABLE IF EXISTS {TABLE}; DROP TABLE IF EXISTS validated_jobs")
        catalog.ensure_schema(self.conn)
        self.assertEqual(self._identity_columns(), EXPECTED_IDENTITY_COLUMNS)
        self.assertEqual(catalog.schema_problems(self.conn), [])

    def test_preflight_reports_the_missing_columns(self) -> None:
        self._drop_identity_columns()
        problems = catalog.schema_problems(self.conn)
        self.conn.rollback()
        self.assertEqual(problems, [f"missing {TABLE} columns: expected_identity, observed_identities"])
        code, output = self._with_catalog_env(daily.run_preflight)
        self.assertEqual(code, 1)
        self.assertIn("PREFLIGHT_FAILED", output)
        self.assertEqual(self._identity_columns(), {})

    def test_daily_refuses_without_the_columns_and_does_not_add_them(self) -> None:
        self._drop_identity_columns()
        calls: list[str] = []

        def collect(**_kwargs):
            calls.append("scrape")
            raise AssertionError("scrape ran without the identity columns")

        args = daily.parse_args(["--sources", "naukri", "--validation-limit", "10"])
        with patch.dict(os.environ, {"JOB_READY_DATABASE_URL": CATALOG_URL}), patch.object(
            daily, "apply_schema", side_effect=AssertionError("daily applied schema")
        ), patch.object(catalog, "ensure_schema", side_effect=AssertionError("daily ensured schema")), redirect_stdout(
            io.StringIO()
        ), self.assertRaises(SystemExit) as raised:
            daily.execute_daily(args, collect=collect, journal_dir=self.base / "runs")
        self.assertIn("SCHEMA_REQUIRED", str(raised.exception))
        self.assertIn("expected_identity", str(raised.exception))
        self.assertEqual(calls, [])
        self.assertEqual(self._identity_columns(), {})

    def test_explicit_helper_upgrades_and_keeps_historical_rows(self) -> None:
        (job_id,) = self._seed(1)
        self._drop_identity_columns()
        self._historical(job_id)
        before = self._fetch(f"SELECT id, job_id, observed_at, recorded_at, observation, evidence_version FROM {TABLE}")
        code, output = self._with_catalog_env(daily.run_apply_schema)
        self.assertEqual(code, 0)
        self.assertIn("SCHEMA_APPLIED", output)
        self.assertEqual(self._identity_columns(), EXPECTED_IDENTITY_COLUMNS)
        self.assertEqual(
            self._fetch(f"SELECT id, job_id, observed_at, recorded_at, observation, evidence_version FROM {TABLE}"), before
        )
        self.assertEqual(self._rows(job_id), [("confirmed_active", "naukri_explicit_open", None, None, "jobready-validator-v1")])
        self.assertEqual(catalog.schema_problems(self.conn), [])

    def test_helper_is_idempotent(self) -> None:
        (job_id,) = self._seed(1)
        catalog.apply_link_observations(
            self.conn,
            [LinkObservation(job_id, "active", "confirmed_active", "naukri", "r", observed_at=T10, expected_identity="5", observed_identities=("5",))],
        )
        snapshot = self._fetch(f"SELECT * FROM {TABLE} ORDER BY id")
        for _ in range(2):
            catalog.ensure_schema(self.conn)
        self.assertEqual(self._identity_columns(), EXPECTED_IDENTITY_COLUMNS)
        self.assertEqual(self._fetch(f"SELECT * FROM {TABLE} ORDER BY id"), snapshot)
        self.assertEqual(catalog.schema_problems(self.conn), [])

    def test_historical_null_rows_stay_valid_beside_new_evidence(self) -> None:
        (job_id,) = self._seed(1)
        self._drop_identity_columns()
        self._historical(job_id)
        catalog.ensure_schema(self.conn)
        catalog.apply_link_observations(
            self.conn,
            [
                LinkObservation(
                    job_id, "active", "confirmed_expired", "naukri", "naukri_explicit_closed",
                    observed_at=T11, expected_identity=NAUKRI_ID, observed_identities=(NAUKRI_ID,),
                )
            ],
        )
        self.assertEqual(
            self._rows(job_id),
            [
                ("confirmed_active", "naukri_explicit_open", None, None, "jobready-validator-v1"),
                ("confirmed_expired", "naukri_explicit_closed", NAUKRI_ID, [NAUKRI_ID], JOBREADY_EVIDENCE_VERSION),
            ],
        )


@unittest.skipUnless(CATALOG_URL, "disposable daily catalog is not configured")
class IdentityPersistenceTests(_CatalogCase):
    """Persistence matrix 1-15."""

    def _apply(self, job_id: str, observation: str, observed_at: datetime, **extra) -> None:
        item = LinkObservation(job_id, "active", observation, "naukri", f"fixture_{observation}", observed_at=observed_at, **extra)
        catalog.apply_link_observations(self.conn, [item])

    def _probed(self, results: dict[str, ProbeResult]) -> list[LinkObservation]:
        rows = [{"job_id": job_id, "source": "probe", "link_status": "active"} for job_id in results]
        return validate_candidates(rows, probes={"probe": lambda row: results[row["job_id"]]})

    def test_01_02_expected_and_observed_persist(self) -> None:
        (job_id,) = self._seed(1)
        observations = self._probed(
            {job_id: ProbeResult("confirmed_active", "naukri_explicit_open", "naukri", expected_identity=NAUKRI_ID, observed_identities=[NAUKRI_ID])}
        )
        catalog.apply_link_observations(self.conn, observations, run_id="run-d4")
        self.assertEqual(
            self._rows(job_id), [("confirmed_active", "naukri_explicit_open", NAUKRI_ID, [NAUKRI_ID], JOBREADY_EVIDENCE_VERSION)]
        )

    def test_03_multiple_observed_persist_deterministically(self) -> None:
        first, second = self._seed(2)
        observations = self._probed(
            {
                first: ProbeResult("inconclusive", "m", "naukri", expected_identity="9", observed_identities=["9", "10", "9"]),
                second: ProbeResult("inconclusive", "m", "naukri", expected_identity="9", observed_identities=("10", "9")),
            }
        )
        catalog.apply_link_observations(self.conn, observations)
        self.assertEqual(self._rows(first)[0][3], ["10", "9"])
        self.assertEqual(self._rows(second)[0][3], ["10", "9"])

    def test_04_expected_known_observed_null(self) -> None:
        (job_id,) = self._seed(1)
        self._apply(job_id, "inconclusive", T10, expected_identity=JOB_ID)
        self.assertEqual(self._rows(job_id)[0][2:4], (JOB_ID, None))
        self.assertEqual(self._fetch(f"SELECT observed_identities IS NULL FROM {TABLE}"), [(True,)])

    def test_05_expected_known_observed_empty_is_not_null(self) -> None:
        (job_id,) = self._seed(1)
        self._apply(job_id, "inconclusive", T10, expected_identity=JOB_ID, observed_identities=())
        self.assertEqual(self._rows(job_id)[0][2:4], (JOB_ID, []))
        self.assertEqual(
            self._fetch(f"SELECT observed_identities IS NULL, cardinality(observed_identities) FROM {TABLE}"), [(False, 0)]
        )

    def test_06_07_08_identity_on_every_observation_label(self) -> None:
        ids = self._seed(3)
        observations = [
            LinkObservation(job_id, "active", label, "linkedin", f"r_{label}", observed_at=T10, expected_identity=JOB_ID, observed_identities=(JOB_ID,))
            for job_id, label in zip(ids, ("confirmed_active", "confirmed_expired", "inconclusive"), strict=True)
        ]
        counts = catalog.apply_link_observations(self.conn, observations)
        self.assertEqual(counts["recorded"], 3)
        for job_id, label in zip(ids, ("confirmed_active", "confirmed_expired", "inconclusive"), strict=True):
            self.assertEqual(self._rows(job_id), [(label, f"r_{label}", JOB_ID, [JOB_ID], JOBREADY_EVIDENCE_VERSION)])
        self.assertEqual([self._state(job_id)[0] for job_id in ids], ["active", "expired", "active"])

    def test_09_historical_null_row_is_valid(self) -> None:
        (job_id,) = self._seed(1)
        self._historical(job_id)
        self.assertEqual(self._rows(job_id), [("confirmed_active", "naukri_explicit_open", None, None, "jobready-validator-v1")])
        self._apply(job_id, "inconclusive", T11, expected_identity=NAUKRI_ID, observed_identities=())
        self.assertEqual([row[2:4] for row in self._rows(job_id)], [(None, None), (NAUKRI_ID, [])])

    def test_10_injected_tuple_stores_null_identity(self) -> None:
        (job_id,) = self._seed(1)
        catalog.apply_link_observations(self.conn, [(job_id, "active", "timeout")])
        self.assertEqual(self._rows(job_id), [("inconclusive", "timeout", None, None, catalog.INJECTED_EVIDENCE_VERSION)])

    def test_11_rollback_is_atomic(self) -> None:
        (job_id,) = self._seed(1)
        before = self._state(job_id)
        item = LinkObservation(job_id, "active", "confirmed_expired", "naukri", "r", observed_at=T10, expected_identity=NAUKRI_ID, observed_identities=(NAUKRI_ID,))
        missing = LinkObservation("CQJ-missing", "active", "confirmed_active", "naukri", "r", observed_at=T10, expected_identity="1", observed_identities=())
        commits: list[str] = []
        with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
            catalog.apply_link_observations(self.conn, [item, missing], on_commit=lambda: commits.append("commit"))
        self.assertEqual((commits, self._count(), self._state(job_id)), ([], 0, before))

    def test_11_rejected_identity_writes_nothing(self) -> None:
        good, bad = self._seed(2)
        items = [
            LinkObservation(good, "active", "confirmed_expired", "naukri", "r", observed_at=T10, expected_identity="1"),
            LinkObservation(bad, "active", "confirmed_expired", "naukri", "r", observed_at=T10, observed_identities=("2", "1")),
        ]
        with self.assertRaises(ValueError):
            catalog.apply_link_observations(self.conn, items)
        self.assertEqual((self._count(), self._state(good)[0]), (0, "active"))

    def test_12_append_only_across_observations(self) -> None:
        (job_id,) = self._seed(1)
        self._apply(job_id, "confirmed_active", T10, expected_identity="1", observed_identities=("1",))
        self._apply(job_id, "inconclusive", T11, expected_identity="1", observed_identities=())
        self._apply(job_id, "confirmed_expired", T12, expected_identity="1", observed_identities=("1", "2"))
        self.assertEqual(
            [row[2:4] for row in self._rows(job_id)], [("1", ["1"]), ("1", []), ("1", ["1", "2"])]
        )
        self.assertEqual(self._state(job_id), ("expired", T10, T12))

    def test_13_stale_observation_keeps_its_own_identity(self) -> None:
        (job_id,) = self._seed(1)
        self._apply(job_id, "confirmed_expired", T11, expected_identity="1", observed_identities=("1",))
        self._apply(job_id, "confirmed_active", T10, expected_identity="1", observed_identities=("7",))
        self.assertEqual([row[:1] + row[2:4] for row in self._rows(job_id)], [("confirmed_expired", "1", ["1"]), ("confirmed_active", "1", ["7"])])
        self.assertEqual(self._state(job_id), ("expired", T10, T11))

    def test_14_equal_time_conflict_keeps_each_evidence(self) -> None:
        (job_id,) = self._seed(1)
        self._apply(job_id, "confirmed_active", T10, expected_identity="1", observed_identities=("1",))
        self._apply(job_id, "confirmed_expired", T10, expected_identity="1", observed_identities=())
        self.assertEqual([row[:1] + row[2:4] for row in self._rows(job_id)], [("confirmed_active", "1", ["1"]), ("confirmed_expired", "1", [])])
        self.assertEqual(self._state(job_id), ("active", T10, T10))

    def test_15_status_is_governed_only_by_the_ordering_rules(self) -> None:
        plain, identified = self._seed(2)
        sequence = (("confirmed_expired", T11), ("confirmed_active", T10), ("confirmed_active", T11), ("inconclusive", T12), ("confirmed_active", T12))
        for index, (observation, observed_at) in enumerate(sequence):
            self._apply(plain, observation, observed_at)
            mismatched = ("999",) if index % 2 else None
            self._apply(identified, observation, observed_at, expected_identity="1", observed_identities=mismatched)
            self.assertEqual(self._state(identified), self._state(plain), index)
        self.assertEqual(self._state(plain), ("active", T12, T11))

    def test_probe_identity_reaches_the_ledger(self) -> None:
        job_ids = self._seed(3)
        rows = [
            {"job_id": job_ids[0], "source": "linkedin", "job_url": LINKEDIN_ROW["job_url"], "link_status": "active"},
            {"job_id": job_ids[1], "source": "naukri", "job_url": NAUKRI_ROW["job_url"], "link_status": "active"},
            {"job_id": job_ids[2], "source": "indeed", "job_url": INDEED_ROW["job_url"], "link_status": "active"},
        ]
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "www.linkedin.com":
                return httpx.Response(200, text=linkedin_page())
            if request.url.host == "www.naukri.com":
                return httpx.Response(200, json={"job": {"jobStatus": "active"}})
            raise httpx.ConnectError("connection refused", request=request)

        # One httpx.Client patch serves every provider: the LinkedIn and Indeed modules share httpx.
        requests = _Requests(handler)
        real_client = httpx.Client

        def client(*args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(requests))
            return real_client(*args, **kwargs)

        naukri_requests, naukri = _naukri_transport(handler)
        with patch.dict(os.environ, {"INDEED_API_KEY": FIXTURE_KEY}), patch.object(httpx, "Client", client), naukri:
            observations = validate_candidates(rows, probes={"linkedin": probe_linkedin, "naukri": probe_naukri, "indeed": probe_indeed})
        catalog.apply_link_observations(self.conn, observations, run_id="run-d4-probes")
        self.assertEqual(
            [self._rows(job_id)[0][:4] for job_id in job_ids],
            [
                ("confirmed_active", "linkedin_identity_apply", JOB_ID, [JOB_ID]),
                ("inconclusive", "naukri_identity_missing", NAUKRI_ID, []),
                ("inconclusive", "indeed_api_error", JK, None),
            ],
        )
        self.assertEqual(
            self._fetch(f"SELECT requested_url, final_url, http_status FROM {TABLE} ORDER BY id"),
            [(GUEST, GUEST, 200), (NAUKRI_ENDPOINT, NAUKRI_ENDPOINT, 200), (indeed_checker.INDEED_GRAPHQL, None, None)],
        )
        self.assertEqual(
            sorted(requests.urls),
            sorted([GUEST] + [indeed_checker.INDEED_GRAPHQL] * retry_policy.INDEED_LIFECYCLE_RETRY.max_attempts),
        )
        self.assertEqual(len(naukri_requests.urls), 1)

    def test_daily_path_persists_identity(self) -> None:
        other = "051026501199"
        urls = [f"https://www.naukri.com/job-listings-python-{job}" for job in (NAUKRI_ID, other)]
        health = {"naukri": {"attempted": True, "succeeded": True, "failed": False, "jobs_returned": 2, "errors": []}}

        def collect(**_kwargs):
            return ScrapeResult(
                jobs=[_job(url) for url in urls],
                per_source={"naukri": 2},
                per_family={"python-dev": 2},
                errors=[],
                sources_attempted=["naukri"],
                started_at="2026-10-10T00:00:00+00:00",
                finished_at="2026-10-10T00:00:01+00:00",
                source_health=health,
            )

        def handler(request: httpx.Request) -> httpx.Response:
            requested = request.url.path.rsplit("/", 1)[-1]
            job = {"jobId": NAUKRI_ID, "jobStatus": "active"} if requested == NAUKRI_ID else {"jobId": "1", "jobStatus": "expired"}
            return httpx.Response(200, json={"job": job})

        _requests, naukri = _naukri_transport(handler)
        args = daily.parse_args(["--sources", "naukri", "--validation-limit", "10"])
        with patch.dict(os.environ, {"JOB_READY_DATABASE_URL": CATALOG_URL}), naukri, redirect_stdout(io.StringIO()):
            report = daily.execute_daily(args, collect=collect, probes={"naukri": probe_naukri}, journal_dir=self.base / "runs")
        self.assertEqual(report["lifecycle_observations_recorded"], 2)
        stored = self._fetch(
            f"SELECT v.job_url, l.reason, l.expected_identity, l.observed_identities, l.evidence_version, l.run_id "
            f"FROM {TABLE} l JOIN validated_jobs v USING (job_id) ORDER BY v.job_url"
        )
        self.assertEqual(
            stored,
            [
                (urls[0], "naukri_explicit_open", NAUKRI_ID, [NAUKRI_ID], JOBREADY_EVIDENCE_VERSION, report["run_id"]),
                (urls[1], "naukri_identity_mismatch", other, ["1"], JOBREADY_EVIDENCE_VERSION, report["run_id"]),
            ],
        )


class ProvenanceRegressionTests(unittest.TestCase):
    """Identity evidence rides beside HTTP provenance and leaves it exactly as it was."""

    def test_probe_provenance_is_unchanged(self) -> None:
        cases = (
            (_linkedin_transport, probe_linkedin, LINKEDIN_ROW, lambda r: httpx.Response(200, text=linkedin_page()), (GUEST, GUEST, 200)),
            (_linkedin_transport, probe_linkedin, LINKEDIN_ROW, _refuse, (GUEST, None, None)),
            (_naukri_transport, probe_naukri, NAUKRI_ROW, lambda r: httpx.Response(503), (NAUKRI_ENDPOINT, NAUKRI_ENDPOINT, 503)),
            (_naukri_transport, probe_naukri, NAUKRI_ROW, _refuse, (NAUKRI_ENDPOINT, None, None)),
            (_indeed_transport, probe_indeed, INDEED_ROW, _refuse, (indeed_checker.INDEED_GRAPHQL, None, None)),
        )
        for transport, probe, row, handler, expected in cases:
            _requests, patcher = transport(handler)
            with patch.dict(os.environ, {"INDEED_API_KEY": FIXTURE_KEY}), patcher:
                result = probe(row)
            self.assertEqual(_provenance(result), expected, probe.__name__)

    def test_identity_does_not_touch_url_sanitization(self) -> None:
        plain = ProbeResult("inconclusive", "r", "naukri", requested_url=f"{NAUKRI_ENDPOINT}?k={SECRET}", http_status=200)
        identified = ProbeResult(
            "inconclusive", "r", "naukri", requested_url=f"{NAUKRI_ENDPOINT}?k={SECRET}", http_status=200,
            expected_identity=NAUKRI_ID, observed_identities=[NAUKRI_ID],
        )
        self.assertEqual(_provenance(identified), _provenance(plain))
        self.assertEqual(_provenance(identified), (NAUKRI_ENDPOINT, None, 200))

    def test_bad_status_is_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ProbeResult("inconclusive", "r", "naukri", http_status=42, expected_identity="1", observed_identities=[])


if __name__ == "__main__":
    unittest.main()
