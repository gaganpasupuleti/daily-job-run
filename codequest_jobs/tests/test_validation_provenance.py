"""P1.3 HTTP provenance: sanitized requested/final URLs and response status in the ledger.

Probe cases run offline against httpx.MockTransport. Database cases use disposable
Postgres named by JOBREADY_DAILY_CATALOG_URL and are skipped when it is absent.
"""

from __future__ import annotations

import io
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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
import jobready_catalog as catalog  # noqa: E402
import jobready_validator  # noqa: E402
import naukri_api  # noqa: E402
import run_jobready_daily as daily  # noqa: E402
from jobready_validator import (  # noqa: E402
    JOBREADY_EVIDENCE_VERSION,
    LinkObservation,
    ProbeResult,
    UnsupportedSourceError,
    interpret_linkedin,
    probe_indeed,
    probe_linkedin,
    probe_naukri,
    validate_candidates,
)
from link_policy import sanitize_evidence_url, valid_http_status  # noqa: E402
from linkedin_checker import GUEST_URL  # noqa: E402
from normalize import UnifiedJob  # noqa: E402
from run_all_sources import ScrapeResult  # noqa: E402
from test_validation_evidence import JOB_ID, linkedin_page  # noqa: E402

CATALOG_URL = os.environ.get("JOBREADY_DAILY_CATALOG_URL", "").strip()
TABLE = "jobready_link_observations"
T10 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
SECRET = "s3cr3t-P13-token"
GUEST = GUEST_URL.format(job_id=JOB_ID)
LINKEDIN_ROW = {"job_id": "CQJ-L", "source": "linkedin", "job_url": f"https://www.linkedin.com/jobs/view/{JOB_ID}"}
NAUKRI_ID = "051026501163"
NAUKRI_ENDPOINT = f"https://www.naukri.com/jobapi/v1/job/{NAUKRI_ID}"
NAUKRI_ROW = {"job_id": "CQJ-N", "source": "naukri", "job_url": f"https://www.naukri.com/job-listings-python-{NAUKRI_ID}"}
INDEED_ROW = {"job_id": "CQJ-I", "source": "indeed", "job_url": "https://in.indeed.com/viewjob?jk=0123456789abcdef"}
PROVENANCE = ("requested_url", "final_url", "http_status")


def _provenance(item) -> tuple:
    return tuple(getattr(item, name) for name in PROVENANCE)


class _Requests:
    """Mock transport that records every request URL it serves."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.urls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        return self.handler(request)


def _linkedin_transport(handler):
    requests = _Requests(handler)
    real_client = httpx.Client

    def client(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(requests), **kwargs)

    return requests, patch.object(jobready_validator.httpx, "Client", client)


def _naukri_transport(handler, *, warm_up_fails: bool = False):
    requests = _Requests(handler)

    class _Client:
        def __init__(self, delay_seconds: float = 1.5) -> None:
            if warm_up_fails:
                raise httpx.ConnectError("warm-up refused")
            self._client = httpx.Client(transport=httpx.MockTransport(requests), follow_redirects=True)

        def close(self) -> None:
            self._client.close()

    return requests, patch.object(naukri_api, "NaukriSearchClient", _Client)


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


class EvidenceUrlSanitizerTests(unittest.TestCase):
    def test_query_is_stripped(self) -> None:
        self.assertEqual(sanitize_evidence_url("https://example.com/job/123?token=abc"), "https://example.com/job/123")

    def test_fragment_is_stripped(self) -> None:
        self.assertEqual(sanitize_evidence_url("https://example.com/job/123#apply"), "https://example.com/job/123")
        self.assertEqual(
            sanitize_evidence_url("https://example.com/job/123?token=abc#apply"), "https://example.com/job/123"
        )

    def test_userinfo_is_never_kept(self) -> None:
        for raw in (
            f"https://user:{SECRET}@example.com/job/123",
            f"https://{SECRET}@example.com/job/123",
            f"https://user:{SECRET}@example.com:8443/job/123?k={SECRET}#{SECRET}",
        ):
            cleaned = sanitize_evidence_url(raw)
            self.assertIsNotNone(cleaned, raw)
            self.assertNotIn(SECRET, cleaned)
            self.assertNotIn("@", cleaned)
        self.assertEqual(sanitize_evidence_url(f"https://user:{SECRET}@example.com/job/123"), "https://example.com/job/123")

    def test_unrepresentable_urls_are_refused(self) -> None:
        for raw in (
            None,
            "",
            200,
            b"https://example.com/x",
            "ftp://example.com/x",
            "javascript:alert(1)",
            f"postgresql://user:{SECRET}@db.internal:5432/railway",
            "https:///no-host",
            "//example.com/protocol-relative",
            "/relative/path",
            "https://exa mple.com/x",
            "https://example.com/a\nb",
            "https://example.com\\@evil.test/x",
            "https://example.com:99999/x",
            "https://example.com:0/x",
            "https://example.com:port/x",
            "https://[::1]/x",
            "https://-bad-.example.com/x",
            "https://bad_host.example.com/x",
            "https://example.com/" + "a" * 3000,
        ):
            self.assertIsNone(sanitize_evidence_url(raw), repr(raw))

    def test_http_and_https_are_both_allowed_and_normalized(self) -> None:
        self.assertEqual(sanitize_evidence_url("http://example.com/a"), "http://example.com/a")
        self.assertEqual(sanitize_evidence_url("https://example.com/a"), "https://example.com/a")
        self.assertEqual(sanitize_evidence_url("HTTPS://Example.COM/Job/A"), "https://example.com/Job/A")

    def test_host_and_path_are_preserved(self) -> None:
        self.assertEqual(sanitize_evidence_url(GUEST), GUEST)
        self.assertEqual(sanitize_evidence_url(NAUKRI_ENDPOINT + "?microsite=y&src=x"), NAUKRI_ENDPOINT)
        self.assertEqual(sanitize_evidence_url("https://example.com"), "https://example.com")
        self.assertEqual(sanitize_evidence_url("https://example.com/a%20b/c/"), "https://example.com/a%20b/c/")

    def test_ports_are_deterministic(self) -> None:
        self.assertEqual(sanitize_evidence_url("https://example.com:443/a"), "https://example.com/a")
        self.assertEqual(sanitize_evidence_url("http://example.com:80/a"), "http://example.com/a")
        self.assertEqual(sanitize_evidence_url("https://example.com:8443/a"), "https://example.com:8443/a")
        self.assertEqual(sanitize_evidence_url("http://example.com:443/a"), "http://example.com:443/a")
        for raw in ("https://example.com:8443/a?x=1", "http://Example.com:80/a#b", GUEST):
            once = sanitize_evidence_url(raw)
            self.assertEqual(sanitize_evidence_url(once), once)

    def test_http_status_must_be_a_real_integer_code(self) -> None:
        for value in (100, 200, 429, 599, 999):
            self.assertTrue(valid_http_status(value), value)
        for value in (None, 99, 1000, -1, "200", 200.0, True, False):
            self.assertFalse(valid_http_status(value), repr(value))

    def test_probe_result_sanitizes_and_rejects_bad_status(self) -> None:
        result = ProbeResult(
            "inconclusive",
            "linkedin_redirected",
            "linkedin",
            requested_url=f"https://u:{SECRET}@www.linkedin.com/jobs-guest/jobs/api/jobPosting/1?x={SECRET}",
            final_url="not a url",
            http_status=302,
        )
        self.assertEqual(result.requested_url, "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/1")
        self.assertIsNone(result.final_url)
        self.assertNotIn(SECRET, repr(result))
        for status in ("200", 42, True):
            with self.assertRaises(ValueError) as raised:
                ProbeResult("inconclusive", "r", "p", http_status=status)
            self.assertNotIn(SECRET, str(raised.exception))


class ObservationCompatibilityTests(unittest.TestCase):
    def test_five_field_observation_has_no_provenance(self) -> None:
        item = LinkObservation("CQJ-1", "active", "confirmed_active", "naukri", "naukri_explicit_open")
        self.assertEqual(_provenance(item), (None, None, None))
        self.assertEqual(item.evidence_version, JOBREADY_EVIDENCE_VERSION)
        self.assertEqual(JOBREADY_EVIDENCE_VERSION, "jobready-validator-v1")

    def test_legacy_probe_tuple_carries_no_provenance(self) -> None:
        rows = [{"job_id": "CQJ-a", "source": "naukri", "job_url": "https://example.test/a", "link_status": "active"}]
        (item,) = validate_candidates(rows, probes={"naukri": lambda row: ("confirmed_active", "naukri_explicit_open", "naukri")})
        self.assertEqual((item.observation, item.reason, item.provider), ("confirmed_active", "naukri_explicit_open", "naukri"))
        self.assertEqual(_provenance(item), (None, None, None))

    def test_probe_result_provenance_reaches_the_observation(self) -> None:
        stamps: list[datetime] = []

        def probe(row):
            stamps.append(datetime.now(timezone.utc))
            return ProbeResult(
                "inconclusive",
                "naukri_empty_detail",
                "naukri",
                requested_url=NAUKRI_ENDPOINT,
                final_url=NAUKRI_ENDPOINT + "?src=x",
                http_status=200,
            )

        (item,) = validate_candidates([dict(NAUKRI_ROW, link_status="active")], probes={"naukri": probe})
        self.assertEqual(_provenance(item), (NAUKRI_ENDPOINT, NAUKRI_ENDPOINT, 200))
        self.assertGreaterEqual(item.observed_at, stamps[0])

    def test_probe_error_carries_no_provenance(self) -> None:
        def probe(row):
            raise RuntimeError(f"exploded at https://u:{SECRET}@example.com")

        (item,) = validate_candidates([dict(NAUKRI_ROW)], probes={"naukri": probe})
        self.assertEqual((item.observation, item.reason), ("inconclusive", "naukri_probe_error"))
        self.assertEqual(_provenance(item), (None, None, None))
        self.assertNotIn(SECRET, repr(item))

    def test_unsupported_source_is_refused_without_provenance(self) -> None:
        rows = [{"job_id": "CQJ-g", "source": "foundit", "job_url": "https://www.foundit.in/job/1?token=x"}]
        with self.assertRaises(UnsupportedSourceError) as caught:
            validate_candidates(rows)
        self.assertEqual(str(caught.exception), "unsupported_source")
        self.assertEqual(caught.exception.sources, ["foundit"])
        self.assertNotIn("token", repr(caught.exception))


class LinkedInProvenanceTests(unittest.TestCase):
    def _probe(self, handler):
        requests, patcher = _linkedin_transport(handler)
        with patcher:
            result = probe_linkedin(LINKEDIN_ROW)
        return result, requests

    def test_active(self) -> None:
        body = linkedin_page()
        result, requests = self._probe(lambda request: httpx.Response(200, text=body))
        self.assertEqual((result.observation, result.reason), ("confirmed_active", "linkedin_identity_apply"))
        self.assertEqual((result.observation, result.reason), interpret_linkedin(status_code=200, body=body, expected_job_id=JOB_ID, final_url=GUEST))
        self.assertEqual(requests.urls, [GUEST])
        self.assertEqual(_provenance(result), (GUEST, GUEST, 200))

    def test_closed_marker(self) -> None:
        result, _ = self._probe(lambda request: httpx.Response(200, text=linkedin_page(closed=True, apply="")))
        self.assertEqual((result.observation, result.reason), ("confirmed_expired", "linkedin_closed_marker"))
        self.assertEqual(_provenance(result), (GUEST, GUEST, 200))

    def test_rate_limited(self) -> None:
        result, _ = self._probe(lambda request: httpx.Response(429, text="slow down"))
        self.assertEqual((result.observation, result.reason), ("inconclusive", "linkedin_http_429"))
        self.assertEqual(_provenance(result), (GUEST, GUEST, 429))

    def test_bot_check_status_999_is_kept(self) -> None:
        result, _ = self._probe(lambda request: httpx.Response(999, text=""))
        self.assertEqual((result.observation, result.reason), ("inconclusive", "linkedin_http_999"))
        self.assertEqual(result.http_status, 999)

    def test_soft_404(self) -> None:
        result, _ = self._probe(lambda request: httpx.Response(200, text="<h1>Page not found</h1>"))
        self.assertEqual((result.observation, result.reason), ("inconclusive", "linkedin_soft_404"))
        self.assertEqual(_provenance(result), (GUEST, GUEST, 200))

    def test_redirect(self) -> None:
        landing = f"https://www.linkedin.com/jobs/view/{JOB_ID}"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GUEST:
                return httpx.Response(302, headers={"Location": f"{landing}?trk=guest&token={SECRET}"})
            return httpx.Response(200, text=linkedin_page())

        result, requests = self._probe(handler)
        self.assertEqual((result.observation, result.reason), ("inconclusive", "linkedin_redirected"))
        self.assertEqual(requests.urls[0], GUEST)
        self.assertEqual(_provenance(result), (GUEST, landing, 200))
        self.assertNotIn(SECRET, repr(result))

    def test_connection_failure(self) -> None:
        result, requests = self._probe(_refuse)
        self.assertEqual((result.observation, result.reason), ("inconclusive", "linkedin_connection_failure"))
        self.assertEqual(requests.urls, [GUEST])
        self.assertEqual(_provenance(result), (GUEST, None, None))

    def test_unrecognized_url_makes_no_request(self) -> None:
        requests, patcher = _linkedin_transport(lambda request: httpx.Response(200))
        with patcher:
            result = probe_linkedin(dict(LINKEDIN_ROW, job_url="https://example.com/not-linkedin"))
        self.assertEqual(result.reason, "linkedin_unrecognized_url")
        self.assertEqual(requests.urls, [])
        self.assertEqual(_provenance(result), (None, None, None))


class NaukriProvenanceTests(unittest.TestCase):
    def _probe(self, handler, **kwargs):
        requests, patcher = _naukri_transport(handler, **kwargs)
        with patcher:
            result = probe_naukri(NAUKRI_ROW)
        return result, requests

    def test_explicit_open(self) -> None:
        payload = {"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}}
        result, requests = self._probe(lambda request: httpx.Response(200, json=payload))
        self.assertEqual((result.observation, result.reason), ("confirmed_active", "naukri_explicit_open"))
        self.assertEqual(len(requests.urls), 1)
        self.assertTrue(requests.urls[0].startswith(NAUKRI_ENDPOINT + "?"))
        self.assertEqual(_provenance(result), (NAUKRI_ENDPOINT, NAUKRI_ENDPOINT, 200))

    def test_explicit_closed(self) -> None:
        payload = {"job": {"jobId": NAUKRI_ID, "jobStatus": "expired"}}
        result, _ = self._probe(lambda request: httpx.Response(200, json=payload))
        self.assertEqual((result.observation, result.reason), ("confirmed_expired", "naukri_explicit_closed"))
        self.assertEqual(_provenance(result), (NAUKRI_ENDPOINT, NAUKRI_ENDPOINT, 200))

    def test_empty_detail(self) -> None:
        result, _ = self._probe(lambda request: httpx.Response(200, json={}))
        self.assertEqual((result.observation, result.reason), ("inconclusive", "naukri_empty_detail"))
        self.assertEqual(_provenance(result), (NAUKRI_ENDPOINT, NAUKRI_ENDPOINT, 200))

    def test_http_failure(self) -> None:
        result, _ = self._probe(lambda request: httpx.Response(503, text="unavailable"))
        self.assertEqual((result.observation, result.reason), ("inconclusive", "naukri_http_503"))
        self.assertEqual(_provenance(result), (NAUKRI_ENDPOINT, NAUKRI_ENDPOINT, 503))

    def test_redirect(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/jobapi/"):
                return httpx.Response(302, headers={"Location": f"https://www.naukri.com/nlogin/login?k={SECRET}"})
            return httpx.Response(200, json={"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}})

        result, _ = self._probe(handler)
        self.assertEqual((result.observation, result.reason), ("inconclusive", "naukri_redirected"))
        self.assertEqual(_provenance(result), (NAUKRI_ENDPOINT, "https://www.naukri.com/nlogin/login", 200))
        self.assertNotIn(SECRET, repr(result))

    def test_connection_failure(self) -> None:
        result, requests = self._probe(_refuse)
        self.assertEqual((result.observation, result.reason), ("inconclusive", "naukri_connection_failure"))
        self.assertEqual(len(requests.urls), 1)
        self.assertEqual(_provenance(result), (NAUKRI_ENDPOINT, None, None))

    def test_failed_warm_up_never_issued_the_detail_request(self) -> None:
        result, requests = self._probe(_refuse, warm_up_fails=True)
        self.assertEqual(result.reason, "naukri_connection_failure")
        self.assertEqual(requests.urls, [])
        self.assertEqual(_provenance(result), (None, None, None))

    def test_unrecognized_url_makes_no_request(self) -> None:
        requests, patcher = _naukri_transport(lambda request: httpx.Response(200))
        with patcher:
            result = probe_naukri(dict(NAUKRI_ROW, job_url="https://www.naukri.com/no-id-here"))
        self.assertEqual(result.reason, "naukri_unrecognized_url")
        self.assertEqual(requests.urls, [])
        self.assertEqual(_provenance(result), (None, None, None))


class IndeedProvenanceTests(unittest.TestCase):
    def test_missing_key_makes_no_request(self) -> None:
        with patch.dict(os.environ, {"INDEED_API_KEY": ""}), patch.object(indeed_checker, "check_indeed_via_api") as check:
            result = probe_indeed(INDEED_ROW)
        check.assert_not_called()
        self.assertEqual((result.observation, result.reason), ("inconclusive", "indeed_missing_api_key"))
        self.assertEqual(_provenance(result), (None, None, None))

    def test_missing_job_key_makes_no_request(self) -> None:
        with patch.dict(os.environ, {"INDEED_API_KEY": "fixture-not-a-key"}), patch.object(
            indeed_checker, "check_indeed_via_api"
        ) as check:
            result = probe_indeed(dict(INDEED_ROW, job_url="https://in.indeed.com/viewjob"))
        check.assert_not_called()
        self.assertEqual(result.reason, "indeed_missing_job_key")
        self.assertEqual(_provenance(result), (None, None, None))

    def test_api_request_records_only_the_endpoint(self) -> None:
        for helper_result, expected in (
            ("active", ("confirmed_active", "indeed_expired_false")),
            ("expired", ("confirmed_expired", "indeed_expired_true")),
            (None, ("inconclusive", "indeed_api_error")),
        ):
            with patch.dict(os.environ, {"INDEED_API_KEY": "fixture-not-a-key"}), patch.object(
                indeed_checker, "check_indeed_via_api", return_value=helper_result
            ) as check:
                result = probe_indeed(INDEED_ROW)
            check.assert_called_once_with("0123456789abcdef")
            self.assertEqual((result.observation, result.reason), expected)
            self.assertEqual(_provenance(result), (indeed_checker.INDEED_GRAPHQL, None, None))


class TransportLoggingTests(unittest.TestCase):
    """Real probes under process-wide INFO logging must not let httpx print raw provider URLs."""

    def setUp(self) -> None:
        self.captured = io.StringIO()
        handler = logging.StreamHandler(self.captured)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        saved_root = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        self.addCleanup(root.setLevel, saved_root)
        self.addCleanup(root.removeHandler, handler)
        # The surrounding process may opt the transport loggers into INFO explicitly.
        for name in ("httpx", "httpcore"):
            transport = logging.getLogger(name)
            self.addCleanup(transport.setLevel, transport.level)
            transport.setLevel(logging.INFO)

    def _run(self, probe, row: dict) -> tuple[ProbeResult, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), patch.object(sys, "stderr", stderr):
            result = probe(row)
            logging.getLogger("jobready.p13.marker").info("application INFO still flows")
        return result, self.captured.getvalue() + stdout.getvalue() + stderr.getvalue()

    def test_linkedin_redirect_secret_never_reaches_logs(self) -> None:
        landing = f"https://www.linkedin.com/jobs/view/{JOB_ID}"

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == GUEST:
                return httpx.Response(
                    302, headers={"Location": f"https://u:{SECRET}@www.linkedin.com/jobs/view/{JOB_ID}?token={SECRET}"}
                )
            return httpx.Response(200, text=linkedin_page())

        requests, patcher = _linkedin_transport(handler)
        with patcher:
            result, output = self._run(probe_linkedin, LINKEDIN_ROW)
        self.assertTrue(any(SECRET in url for url in requests.urls), "the mock must carry the secret")
        self.assertEqual((result.observation, result.reason, result.provider), ("inconclusive", "linkedin_redirected", "linkedin"))
        self.assertEqual(_provenance(result), (GUEST, landing, 200))
        self.assertIn("application INFO still flows", output)
        self.assertNotIn(SECRET, output)
        self.assertNotIn("HTTP Request", output)

    def test_naukri_request_secret_never_reaches_logs(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/jobapi/"):
                return httpx.Response(302, headers={"Location": f"https://www.naukri.com/nlogin/login?k={SECRET}"})
            return httpx.Response(200, json={})

        requests, patcher = _naukri_transport(handler)
        with patcher:
            result, output = self._run(probe_naukri, NAUKRI_ROW)
        self.assertTrue(any(SECRET in url for url in requests.urls), "the mock must carry the secret")
        self.assertEqual((result.reason, result.http_status), ("naukri_redirected", 200))
        self.assertIn("application INFO still flows", output)
        self.assertNotIn(SECRET, output)
        self.assertNotIn("HTTP Request", output)

    def test_indeed_helper_transport_logging_is_quiet(self) -> None:
        def helper(jk: str) -> str:
            logging.getLogger("httpx").info("HTTP Request: POST https://apis.indeed.com/graphql?k=%s", SECRET)
            return "active"

        with patch.dict(os.environ, {"INDEED_API_KEY": "fixture-not-a-key"}), patch.object(
            indeed_checker, "check_indeed_via_api", side_effect=helper
        ):
            result, output = self._run(probe_indeed, INDEED_ROW)
        self.assertEqual(result.reason, "indeed_expired_false")
        self.assertIn("application INFO still flows", output)
        self.assertNotIn(SECRET, output)


class WriterValidationTests(unittest.TestCase):
    class _NoSql:
        def cursor(self):
            raise AssertionError("SQL attempted for a malformed batch")

    def test_malformed_provenance_is_rejected_before_sql(self) -> None:
        bad = (
            {"requested_url": f"https://user:{SECRET}@example.com/job"},
            {"final_url": f"https://example.com/job?token={SECRET}"},
            {"final_url": "ftp://example.com/job"},
            {"requested_url": b"https://example.com/job"},
            {"http_status": "200"},
            {"http_status": True},
            {"http_status": 42},
            {"http_status": 200.0},
        )
        for fields in bad:
            item = LinkObservation("CQJ-1", "active", "inconclusive", "naukri", "r", observed_at=T10, **fields)
            with self.assertRaises(ValueError) as raised:
                catalog.apply_link_observations(self._NoSql(), [item])
            self.assertNotIn(SECRET, str(raised.exception), fields)


@unittest.skipUnless(CATALOG_URL, "disposable daily catalog is not configured")
class ProvenanceLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.conn = psycopg2.connect(CATALOG_URL, connect_timeout=8)
        except Exception:
            raise unittest.SkipTest("disposable database unavailable") from None
        catalog.ensure_schema(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.rollback()
        catalog.ensure_schema(self.conn)
        self._exec(f"TRUNCATE {TABLE}, validated_jobs RESTART IDENTITY")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        for target in ("OUTPUT_DIR", "RUNS_DIR"):
            patcher = patch.object(daily, target, self.base / target.lower())
            patcher.start()
            self.addCleanup(patcher.stop)

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

    @staticmethod
    def _job(url: str) -> UnifiedJob:
        return UnifiedJob(
            job_id="upstream-id",
            source="naukri",
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

    def _seed(self, count: int, prefix: str = "p13") -> list[str]:
        urls = [f"https://example.test/jobs/{prefix}-{index:04d}" for index in range(count)]
        with redirect_stdout(io.StringIO()):
            catalog.upsert_jobs(self.conn, [daily.prepare_catalog_row(self._job(url)) for url in urls])
        by_url = dict(self._fetch("SELECT job_url, job_id FROM validated_jobs WHERE job_url = ANY(%s)", (urls,)))
        return [by_url[url] for url in urls]

    def _ledger(self) -> dict[str, dict]:
        names = (
            "job_id",
            "observed_at",
            "recorded_at",
            "observation",
            "reason",
            "provider",
            "run_id",
            "requested_url",
            "final_url",
            "http_status",
            "evidence_version",
        )
        rows = self._fetch(f"SELECT {', '.join(names)} FROM {TABLE} ORDER BY id")
        return {row[0]: dict(zip(names, row, strict=True)) for row in rows}

    def _state(self, job_id: str) -> tuple:
        return self._fetch(
            "SELECT link_status, last_confirmed_open_at, last_confirmed_closed_at FROM validated_jobs WHERE job_id = %s",
            (job_id,),
        )[0]

    def _count(self) -> int:
        return self._fetch(f"SELECT COUNT(*) FROM {TABLE}")[0][0]

    def test_ledger_stores_exactly_the_sanitized_provenance(self) -> None:
        active_id, soft_id, redirect_id, generic_id, injected_id = self._seed(5)
        landing = f"https://www.linkedin.com/jobs/view/{JOB_ID}"
        results = {
            active_id: ProbeResult(
                "confirmed_active",
                "naukri_explicit_open",
                "naukri",
                requested_url=NAUKRI_ENDPOINT,
                final_url=f"https://svc:{SECRET}@www.naukri.com/jobapi/v1/job/{NAUKRI_ID}?nkparam={SECRET}#x",
                http_status=200,
            ),
            soft_id: ProbeResult("inconclusive", "linkedin_soft_404", "linkedin", requested_url=GUEST, final_url=GUEST, http_status=200),
            redirect_id: ProbeResult(
                "inconclusive",
                "linkedin_redirected",
                "linkedin",
                requested_url=GUEST,
                final_url=f"{landing}?trk={SECRET}",
                http_status=200,
            ),
            generic_id: ProbeResult("inconclusive", "foundit_no_explicit_closure", "foundit"),
        }
        rows = [{"job_id": job_id, "source": "probe", "link_status": "active"} for job_id in results]
        observations = validate_candidates(rows, probes={"probe": lambda row: results[row["job_id"]]})
        observed = {item.job_id: item.observed_at for item in observations}
        counts = catalog.apply_link_observations(
            self.conn, [*observations, (injected_id, "active", "timeout")], run_id="run-p13"
        )
        self.assertEqual(counts["recorded"], 5)

        ledger = self._ledger()
        expected = {
            active_id: ("confirmed_active", "naukri_explicit_open", "naukri", NAUKRI_ENDPOINT, NAUKRI_ENDPOINT, 200),
            soft_id: ("inconclusive", "linkedin_soft_404", "linkedin", GUEST, GUEST, 200),
            redirect_id: ("inconclusive", "linkedin_redirected", "linkedin", GUEST, landing, 200),
            generic_id: ("inconclusive", "foundit_no_explicit_closure", "foundit", None, None, None),
            injected_id: ("inconclusive", "timeout", catalog.INJECTED_PROVIDER, None, None, None),
        }
        for job_id, values in expected.items():
            row = ledger[job_id]
            self.assertEqual(
                (row["observation"], row["reason"], row["provider"], row["requested_url"], row["final_url"], row["http_status"]),
                values,
                job_id,
            )
            self.assertEqual(row["run_id"], "run-p13")
            self.assertGreaterEqual(row["recorded_at"], row["observed_at"])
        for job_id, observed_at in observed.items():
            self.assertEqual(ledger[job_id]["observed_at"], observed_at)
            self.assertEqual(ledger[job_id]["evidence_version"], JOBREADY_EVIDENCE_VERSION)
        self.assertEqual(ledger[injected_id]["evidence_version"], catalog.INJECTED_EVIDENCE_VERSION)
        self.assertEqual(len({row["recorded_at"] for row in ledger.values()}), 1)
        self.assertNotIn(SECRET, repr(self._fetch(f"SELECT * FROM {TABLE}")))

        self.assertEqual(self._state(active_id), ("active", observed[active_id], None))
        for job_id in (soft_id, redirect_id, generic_id, injected_id):
            self.assertEqual(self._state(job_id), ("active", None, None), job_id)

    def test_ledger_failure_rolls_back_provenance_and_summary(self) -> None:
        (job_id,) = self._seed(1)
        before = self._state(job_id)
        commits: list[str] = []
        provenance = {"observed_at": T10, "requested_url": GUEST, "final_url": GUEST, "http_status": 200}
        item = LinkObservation(job_id, "active", "confirmed_expired", "linkedin", "linkedin_closed_marker", **provenance)
        missing = LinkObservation("CQJ-missing", "active", "confirmed_active", "linkedin", "linkedin_identity_apply", **provenance)
        with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
            catalog.apply_link_observations(self.conn, [item, missing], on_commit=lambda: commits.append("commit"))
        self.assertEqual(commits, [])
        self.assertEqual(self._count(), 0)
        self.assertEqual(self._state(job_id), before)

    def test_200_observations_with_provenance_stay_set_based(self) -> None:
        job_ids = self._seed(205, prefix="bulk")

        class _Counting:
            def __init__(self, inner) -> None:
                self.inner = inner
                self.executes = 0

            def cursor(self):
                owner = self
                cursor = self.inner.cursor()

                class _Cursor:
                    def execute(self, query, vars=None):
                        owner.executes += 1
                        return cursor.execute(query, vars)

                    def __getattr__(self, name):
                        return getattr(cursor, name)

                    def __enter__(self):
                        cursor.__enter__()
                        return self

                    def __exit__(self, *exc):
                        return cursor.__exit__(*exc)

                return _Cursor()

            def __getattr__(self, name):
                return getattr(self.inner, name)

        def observation(job_id: str, index: int) -> LinkObservation:
            endpoint = f"https://www.naukri.com/jobapi/v1/job/{index:06d}"
            return LinkObservation(
                job_id,
                "active",
                "inconclusive",
                "naukri",
                "naukri_empty_detail",
                observed_at=T10,
                requested_url=endpoint,
                final_url=endpoint,
                http_status=200,
            )

        small = _Counting(self.conn)
        catalog.apply_link_observations(small, [observation(job_id, i) for i, job_id in enumerate(job_ids[200:])])
        large = _Counting(self.conn)
        result = catalog.apply_link_observations(
            large, [observation(job_id, i) for i, job_id in enumerate(job_ids[:200])], run_id="run-p13-bulk"
        )
        self.assertEqual(large.executes, small.executes)
        self.assertLessEqual(large.executes, 10)
        self.assertEqual(result["recorded"], 200)
        self.assertEqual(
            self._fetch(
                f"SELECT COUNT(*), COUNT(DISTINCT requested_url), MIN(http_status), MAX(http_status) "
                f"FROM {TABLE} WHERE run_id = 'run-p13-bulk' AND requested_url = final_url"
            ),
            [(200, 200, 200, 200)],
        )

    def test_daily_path_persists_provenance_without_leaking_credentials(self) -> None:
        urls = [f"https://example.test/jobs/daily-p13-{index}" for index in range(2)]
        health = {"naukri": {"attempted": True, "succeeded": True, "failed": False, "jobs_returned": 2, "errors": []}}

        def collect(**_kwargs):
            return ScrapeResult(
                jobs=[self._job(url) for url in urls],
                per_source={"naukri": 2},
                per_family={"python-dev": 2},
                errors=[],
                sources_attempted=["naukri"],
                started_at="2026-10-10T00:00:00+00:00",
                finished_at="2026-10-10T00:00:01+00:00",
                source_health=health,
            )

        def probe(row):
            return ProbeResult(
                "inconclusive",
                "naukri_empty_detail",
                "naukri",
                requested_url=f"{NAUKRI_ENDPOINT}?nkparam={SECRET}",
                final_url=f"https://svc:{SECRET}@www.naukri.com/jobapi/v1/job/{NAUKRI_ID}#{SECRET}",
                http_status=200,
            )

        stdout = io.StringIO()
        with patch.dict(os.environ, {"JOB_READY_DATABASE_URL": CATALOG_URL}):
            args = daily.parse_args(["--sources", "naukri", "--validation-limit", "10"])
            with redirect_stdout(stdout):
                report = daily.execute_daily(
                    args, collect=collect, probes={"naukri": probe, "generic": probe}, journal_dir=self.base / "runs"
                )

        self.assertEqual(report["lifecycle_observations_recorded"], 2)
        self.assertEqual(report["validated_checked"], 2)
        self.assertEqual(report["unknown_or_inconclusive"], 2)
        self.assertEqual((report["confirmed_active"], report["confirmed_expired"]), (0, 0))
        self.assertEqual(report["validation_reasons"], {"inconclusive:naukri_empty_detail": 2})
        for row in self._ledger().values():
            self.assertEqual((row["requested_url"], row["final_url"], row["http_status"]), (NAUKRI_ENDPOINT, NAUKRI_ENDPOINT, 200))
            self.assertEqual(row["run_id"], report["run_id"])
        written = [
            path.read_text(encoding="utf-8", errors="replace") for path in self.base.rglob("*") if path.is_file()
        ]
        for text in (stdout.getvalue(), repr(report), *written):
            self.assertNotIn(SECRET, text)
        self.assertNotIn("jobapi", stdout.getvalue())
        self.assertNotIn("jobapi", repr(report))


if __name__ == "__main__":
    unittest.main()
