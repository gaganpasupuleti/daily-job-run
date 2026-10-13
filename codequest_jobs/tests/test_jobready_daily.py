"""JobReady-only daily catalog behavior.

Database cases use disposable Postgres named by JOBREADY_DAILY_CATALOG_URL
and JOBREADY_DAILY_APP_URL. They are skipped when those variables are absent.
"""

from __future__ import annotations

import inspect
import os
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
SCRAPER = REPO / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

import jobready_catalog as catalog  # noqa: E402
import link_checker  # noqa: E402
import link_policy  # noqa: E402
import push_validated_to_job_ready as push  # noqa: E402
import run_jobready_daily as daily  # noqa: E402
import sync_job_ready_ui as sync  # noqa: E402
from normalize import UnifiedJob, source_job_id  # noqa: E402
from role_keywords import load_families  # noqa: E402
from run_all_sources import collect_jobs, dedupe_jobs, parse_sources  # noqa: E402

CATALOG_URL = os.environ.get("JOBREADY_DAILY_CATALOG_URL", "").strip()
APP_URL = os.environ.get("JOBREADY_DAILY_APP_URL", "").strip()
CODEQUEST_SENTINEL = os.environ.get("JOBREADY_SENTINEL_CODEQUEST_URL", "").strip()
SCRAPER_SENTINEL = os.environ.get("JOBREADY_SENTINEL_SCRAPER_URL", "").strip()


def _job(url: str, *, source: str = "naukri", title: str = "Python Developer", family: str = "python-dev") -> UnifiedJob:
    return UnifiedJob(
        job_id="upstream-id",
        source=source,
        title=title,
        company="Example Co",
        location="Bengaluru",
        description="Build Python APIs with Django and PostgreSQL for fresher engineers.",
        job_url=url,
        role_family=family,
        ingest_profile=family,
        apply_url=url,
        job_type="fulltime",
    )


def _confirm(database_url: str) -> str:
    host, port, database = sync.database_identity(database_url)
    return f"{host}:{port}/{database}"


class _CountingCursor:
    def __init__(self, inner, owner: "_CountingConnection") -> None:
        self._inner = inner
        self._owner = owner

    def execute(self, query, vars=None):
        self._owner.executes += 1
        if vars is None:
            return self._inner.execute(query)
        return self._inner.execute(query, vars)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._inner.__exit__(exc_type, exc, tb)


class _CountingConnection:
    """Count cursor executes. The wrapped connection still performs the writes."""

    def __init__(self, connection) -> None:
        self._connection = connection
        self.executes = 0

    def cursor(self, *args, **kwargs):
        return _CountingCursor(self._connection.cursor(*args, **kwargs), self)

    def commit(self):
        return self._connection.commit()

    def rollback(self):
        return self._connection.rollback()


class ContractTests(unittest.TestCase):
    def test_clean_clone_does_not_require_role_reports(self) -> None:
        text = (SCRAPER / "role_keywords.py").read_text(encoding="utf-8")
        self.assertNotIn("Role_Reports", text)
        self.assertTrue((SCRAPER / "families.json").is_file())
        families = load_families()
        self.assertGreaterEqual(len(families), 1)
        self.assertTrue(all(row["id"] and row["keywords"] for row in families))

    def test_canonical_path_does_not_require_codequest_or_local_scraper_db(self) -> None:
        daily_source = inspect.getsource(daily.run_daily) + inspect.getsource(daily.main)
        collect_source = inspect.getsource(collect_jobs)
        self.assertNotIn("CODEQUEST_DATABASE_URL", daily_source)
        self.assertNotIn("DATABASE_URL", daily_source)
        self.assertNotIn("quiz_pack", daily_source)
        self.assertNotIn("job_enrichments", daily_source)
        self.assertNotIn("save_jobs", collect_source)
        self.assertNotIn("save_to_scraped_jobs", collect_source)
        self.assertNotIn("CODEQUEST_DATABASE_URL", collect_source)

    def test_source_identity_and_dedupe(self) -> None:
        url = "https://example.test/jobs/1"
        self.assertEqual(source_job_id("naukri", url), source_job_id("naukri", url))
        self.assertNotEqual(source_job_id("naukri", url), source_job_id("indeed", url))
        jobs = dedupe_jobs([_job(url), _job(url), _job("https://example.test/jobs/2")])
        self.assertEqual(len(jobs), 2)
        want_naukri, sites, want_foundit = parse_sources("naukri,indeed,linkedin,google,foundit")
        self.assertTrue(want_naukri)
        self.assertTrue(want_foundit)
        self.assertEqual(sites, ["indeed", "linkedin", "google"])

    def test_generic_http_failures_do_not_expire(self) -> None:
        self.assertEqual(link_checker._status_from_response(404), link_checker.LINK_STATUS_LINK_FAILED)
        self.assertEqual(link_checker._status_from_response(410), link_checker.LINK_STATUS_LINK_FAILED)
        for status in (401, 403, 408, 429, 500, 503):
            observation = link_policy.observation_from_http_status(status)
            self.assertFalse(link_policy.may_mark_expired(observation))
            self.assertEqual(link_policy.next_link_status("active", observation), "active")
        for observation in ("unknown", "timeout", "link_failed", "connection_failure", "bot_block"):
            self.assertFalse(link_policy.may_mark_expired(observation))
            self.assertEqual(link_policy.next_link_status("active", observation), "active")
        self.assertTrue(link_policy.may_mark_expired("confirmed_expired"))
        self.assertEqual(link_policy.next_link_status("active", "confirmed_expired"), "expired")

    def test_indeed_checker_verifies_tls(self) -> None:
        text = (ROOT / "indeed_checker.py").read_text(encoding="utf-8")
        duplicate = (REPO / "link_validator_service" / "indeed_checker.py").read_text(encoding="utf-8")
        self.assertNotIn("verify=False", text)
        self.assertNotIn("verify=False", duplicate)

    def test_prune_is_refused_before_a_database_connection(self) -> None:
        previous = sys.argv
        sys.argv = ["push_validated_to_job_ready.py", "--prune", "--max-age-days", "1"]
        try:
            with self.assertRaises(SystemExit) as caught:
                push.main()
        finally:
            sys.argv = previous
        self.assertIn("PRUNE_REFUSED", str(caught.exception))

    def test_experience_buckets_use_the_jobready_vocabulary(self) -> None:
        from jobready_catalog import catalog_experience_bucket

        self.assertEqual(
            catalog_experience_bucket(experience_level="fresher", title="Python Intern", job_type=""),
            "Internship",
        )
        self.assertEqual(
            catalog_experience_bucket(experience_level="fresher", title="Python Developer", job_type="fulltime"),
            "Fresher",
        )
        self.assertEqual(
            catalog_experience_bucket(experience_level="entry", title="Java Developer", job_type=""),
            "Entry (1-2 yrs)",
        )
        self.assertEqual(
            catalog_experience_bucket(experience_level="experienced", title="Data Engineer", job_type=""),
            "Experienced",
        )
        self.assertEqual(
            catalog_experience_bucket(experience_level="", title="Account Manager", job_type=""),
            "Unknown / Unclassified",
        )

    def test_provider_rules_are_conservative(self) -> None:
        from jobready_validator import interpret_indeed, interpret_linkedin, interpret_naukri
        from linkedin_checker import GUEST_URL

        self.assertEqual(interpret_indeed(expired=True, key_configured=True)[0], "confirmed_expired")
        self.assertEqual(interpret_indeed(expired=False, key_configured=True)[0], "confirmed_active")
        self.assertEqual(interpret_indeed(expired=None, key_configured=False)[0], "inconclusive")
        self.assertEqual(interpret_indeed(expired=None, key_configured=True)[0], "inconclusive")
        guest = GUEST_URL.format(job_id="123")

        def linkedin(status: int, body: str) -> str:
            return interpret_linkedin(status_code=status, body=body, expected_job_id="123", final_url=guest)[0]

        self.assertEqual(linkedin(200, "No longer accepting applications"), "inconclusive")
        self.assertEqual(linkedin(404, ""), "confirmed_expired")
        self.assertEqual(linkedin(410, ""), "confirmed_expired")
        self.assertEqual(linkedin(429, ""), "inconclusive")
        self.assertEqual(linkedin(503, ""), "inconclusive")
        detail = "https://www.naukri.com/jobapi/v1/job/123"

        def naukri(status: int, payload) -> str:
            return interpret_naukri(status_code=status, payload=payload, expected_job_id="123", final_url=detail)[0]

        self.assertEqual(naukri(404, None), "inconclusive")
        self.assertEqual(naukri(410, None), "inconclusive")
        self.assertEqual(naukri(500, {"error": "system"}), "inconclusive")
        self.assertEqual(naukri(200, {"job": []}), "inconclusive")
        self.assertEqual(
            naukri(200, {"job": {"jobId": "123", "title": "Python Developer", "jobStatus": "expired"}}),
            "confirmed_expired",
        )
        self.assertEqual(naukri(200, {"job": {"jobId": "123", "title": "Python Developer"}}), "inconclusive")

    def test_report_refuses_credentials(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            daily.write_report({"started_at": "2026-10-10T00:00:00+00:00", "note": "postgresql://user:secret@host/db"})
        self.assertIn("credential", str(caught.exception))
        with self.assertRaises(SystemExit):
            daily.write_report({"started_at": "2026-10-10T00:00:00+00:00", "note": "password=secret"})


@unittest.skipUnless(CATALOG_URL and APP_URL, "disposable daily databases are not configured")
class CatalogTests(unittest.TestCase):
    reports: list[Path] = []

    @classmethod
    def setUpClass(cls) -> None:
        import psycopg2

        cls.psycopg2 = psycopg2
        try:
            cls.catalog = psycopg2.connect(CATALOG_URL, connect_timeout=8)
            cls.app = psycopg2.connect(APP_URL, connect_timeout=8)
        except Exception:
            raise unittest.SkipTest("disposable database unavailable") from None
        catalog.ensure_schema(cls.catalog)
        with cls.app.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id uuid PRIMARY KEY,
                    external_id text,
                    status text,
                    is_active boolean,
                    updated_at timestamptz,
                    first_seen_at timestamptz,
                    last_seen_at timestamptz
                )
                """
            )
        cls.app.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalog.close()
        cls.app.close()
        for path in cls.reports:
            path.unlink(missing_ok=True)

    def setUp(self) -> None:
        self.catalog.rollback()
        self.app.rollback()
        with self.catalog.cursor() as cur:
            cur.execute("TRUNCATE jobready_link_observations, validated_jobs RESTART IDENTITY")
        self.catalog.commit()
        with self.app.cursor() as cur:
            cur.execute("TRUNCATE jobs")
        self.app.commit()

    def _run(self, jobs, **kwargs):
        report = daily.run_daily(catalog_conn=self.catalog, jobs=jobs, **kwargs)
        self.reports.append(Path(report["report_path"]))
        text = Path(report["report_path"]).read_text(encoding="utf-8")
        self.assertNotIn("postgresql://", text.lower())
        self.assertNotIn("password=", text.lower())
        return report

    def _row(self, url: str):
        with self.catalog.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, link_status, scraped_created_at, synced_at, updated_at, source_id
                FROM validated_jobs WHERE job_url = %s
                """,
                (url,),
            )
            return cur.fetchone()

    def _count(self) -> int:
        with self.catalog.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM validated_jobs")
            return int(cur.fetchone()[0])

    def test_preserves_existing_cqj_and_allocates_new_ids(self) -> None:
        url = "https://example.test/jobs/existing"
        identity = source_job_id("naukri", url)
        with self.catalog.cursor() as cur:
            cur.execute(
                """
                INSERT INTO validated_jobs (
                    job_id, source_id, source, title, job_url, link_status, scraped_created_at
                ) VALUES ('CQJ-20200101-0007', %s, 'naukri', 'Python Developer', %s, 'active', '2020-01-01T00:00:00Z')
                """,
                (identity, url),
            )
        self.catalog.commit()
        report = self._run([_job(url), _job("https://example.test/jobs/new")])
        kept = self._row(url)
        created = self._row("https://example.test/jobs/new")
        self.assertEqual(kept[0], "CQJ-20200101-0007")
        self.assertRegex(created[0], r"^CQJ-\d{8}-\d{4}$")
        self.assertNotEqual(created[0], "CQJ-20200101-0007")
        self.assertEqual(report["new_catalog_jobs"], 1)
        self.assertEqual(self._count(), 2)

    def test_concurrent_allocation_is_unique(self) -> None:
        errors: list[BaseException] = []

        def worker(url: str) -> None:
            conn = self.psycopg2.connect(CATALOG_URL, connect_timeout=8)
            try:
                catalog.upsert_jobs(conn, [daily.prepare_catalog_row(_job(url))])
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=worker, args=(f"https://example.test/jobs/concurrent-{index}",))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        with self.catalog.cursor() as cur:
            cur.execute("SELECT job_id FROM validated_jobs ORDER BY job_id")
            ids = [row[0] for row in cur.fetchall()]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)

    def test_timestamps_and_repeat_run(self) -> None:
        url = "https://example.test/jobs/repeat"
        self._run([_job(url)])
        job_id, status, created, synced, updated, source_id = self._row(url)
        self.assertEqual(status, "active")
        self.assertEqual(source_id, source_job_id("naukri", url))
        time.sleep(0.05)
        again = self._run([_job(url)])
        job_id_2, status_2, created_2, synced_2, updated_2, _source_2 = self._row(url)
        self.assertEqual(job_id_2, job_id)
        self.assertEqual(created_2, created)
        self.assertGreater(synced_2, synced)
        self.assertEqual(updated_2, updated)
        self.assertEqual(status_2, "active")
        self.assertEqual(again["new_catalog_jobs"], 0)
        self.assertEqual(again["updated_catalog_jobs"], 0)
        self.assertEqual(again["unchanged_catalog_jobs"], 1)
        self.assertEqual(again["reobserved_jobs"], 1)

    def test_confirmed_expired_keeps_the_row(self) -> None:
        first = "https://example.test/jobs/close"
        second = "https://example.test/jobs/stay"
        self._run([_job(first), _job(second, source="indeed", title="Java Backend Developer", family="java-backend")])
        closing = self._row(first)[0]
        staying = self._row(second)[0]
        report = self._run(
            [_job(first), _job(second, source="indeed", title="Java Backend Developer", family="java-backend")],
            observations=[
                (closing, "active", "confirmed_expired"),
                (staying, "active", "confirmed_active"),
            ],
        )
        self.assertEqual(self._row(first)[1], "expired")
        self.assertEqual(self._row(second)[1], "active")
        self.assertEqual(self._count(), 2)
        self.assertEqual(report["confirmed_expired"], 1)
        self.assertEqual(report["catalog_expired"], 1)
        self.assertEqual(report["catalog_active"], 1)
        self.assertEqual(catalog.active_job_ids(self.catalog), [staying])

    def test_inconclusive_observations_do_not_expire(self) -> None:
        url = "https://example.test/jobs/inconclusive"
        self._run([_job(url)])
        job_id = self._row(url)[0]
        for observation in ("unknown", "timeout", "http_403", "http_429", "http_5xx", "link_failed"):
            self._run([_job(url)], observations=[(job_id, "active", observation)])
            self.assertEqual(self._row(url)[1], "active", observation)
        self.assertEqual(self._count(), 1)

    def test_missing_scrape_rows_stay_in_the_catalog(self) -> None:
        urls = [f"https://example.test/jobs/keep-{index}" for index in range(3)]
        self._run([_job(url) for url in urls])
        self._run([])
        self._run([_job(urls[0])])
        self.assertEqual(self._count(), 3)
        for url in urls:
            self.assertEqual(self._row(url)[1], "active")

    def test_same_source_url_is_one_catalog_row(self) -> None:
        url = "https://example.test/jobs/same"
        self._run([_job(url)])
        other = daily.prepare_catalog_row(_job(url))
        other["source_id"] = "different-upstream-id"
        catalog.upsert_jobs(self.catalog, [other])
        self.assertEqual(self._count(), 1)
        self.assertEqual(self._row(url)[0][:4], "CQJ-")

    def test_membership_uses_active_rows_and_restores_returned_jobs(self) -> None:
        url = "https://example.test/jobs/member"
        self._run([_job(url)])
        job_id = self._row(url)[0]
        app_id = str(uuid.uuid4())
        with self.app.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (id, external_id, status, is_active, updated_at, first_seen_at, last_seen_at)
                VALUES (%s, %s, 'active', true, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', '2026-01-03T00:00:00Z')
                """,
                (app_id, f"jobs-server:{job_id}"),
            )
        self.app.commit()
        previous_app = os.environ.get("JOB_READY_APP_DATABASE_URL")
        previous_catalog = os.environ.get("JOB_READY_DATABASE_URL")
        os.environ["JOB_READY_APP_DATABASE_URL"] = APP_URL
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        try:
            self._run([_job(url)], observations=[(job_id, "active", "confirmed_expired")])
            dry = daily.membership_would_change(self.catalog, self.app)
            self.assertEqual(dry["would_archive"], 1)
            self._run(
                [_job(url)],
                observations=[(job_id, "expired", "confirmed_expired")],
                app_conn=self.app,
                apply_membership=True,
                confirm_target=_confirm(APP_URL),
            )
            with self.app.cursor() as cur:
                cur.execute("SELECT status, is_active FROM jobs WHERE id = %s", (app_id,))
                self.assertEqual(cur.fetchone(), ("archived", False))
            self._run(
                [_job(url)],
                observations=[(job_id, "expired", "confirmed_active")],
                app_conn=self.app,
                apply_membership=True,
                confirm_target=_confirm(APP_URL),
            )
            with self.app.cursor() as cur:
                cur.execute("SELECT status, is_active FROM jobs WHERE id = %s", (app_id,))
                self.assertEqual(cur.fetchone(), ("active", True))
            self.assertEqual(self._row(url)[1], "active")
        finally:
            if previous_app is None:
                os.environ.pop("JOB_READY_APP_DATABASE_URL", None)
            else:
                os.environ["JOB_READY_APP_DATABASE_URL"] = previous_app
            if previous_catalog is None:
                os.environ.pop("JOB_READY_DATABASE_URL", None)
            else:
                os.environ["JOB_READY_DATABASE_URL"] = previous_catalog

    def test_empty_scrape_refuses_membership_writes(self) -> None:
        app_id = str(uuid.uuid4())
        with self.app.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (id, external_id, status, is_active, updated_at)
                VALUES (%s, 'jobs-server:CQJ-STAY', 'active', true, NOW())
                """,
                (app_id,),
            )
        self.app.commit()
        with self.assertRaises(SystemExit) as caught:
            self._run(
                [],
                app_conn=self.app,
                apply_membership=True,
                confirm_target=_confirm(APP_URL),
                sources_attempted=["naukri"],
            )
        self.assertIn("membership sync refused", str(caught.exception))
        with self.app.cursor() as cur:
            cur.execute("SELECT status, is_active FROM jobs WHERE id = %s", (app_id,))
            self.assertEqual(cur.fetchone(), ("active", True))

    def test_target_confirmation_and_database_swap(self) -> None:
        previous_app = os.environ.get("JOB_READY_APP_DATABASE_URL")
        previous_catalog = os.environ.get("JOB_READY_DATABASE_URL")
        os.environ["JOB_READY_APP_DATABASE_URL"] = APP_URL
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        try:
            with self.assertRaises(SystemExit) as mismatch:
                sync.archive_ui_jobs_not_in_catalog(confirm_target="127.0.0.1:1/wrong")
            self.assertIn("does not match", str(mismatch.exception))
            os.environ["JOB_READY_APP_DATABASE_URL"] = CATALOG_URL
            os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
            with self.assertRaises(SystemExit) as swapped:
                sync.archive_ui_jobs_not_in_catalog(confirm_target=_confirm(CATALOG_URL))
            self.assertIn("catalog database", str(swapped.exception))
        finally:
            if previous_app is None:
                os.environ.pop("JOB_READY_APP_DATABASE_URL", None)
            else:
                os.environ["JOB_READY_APP_DATABASE_URL"] = previous_app
            if previous_catalog is None:
                os.environ.pop("JOB_READY_DATABASE_URL", None)
            else:
                os.environ["JOB_READY_DATABASE_URL"] = previous_catalog

    def test_run_lock_rejects_a_second_run(self) -> None:
        lock = catalog.DailyRunLock(CATALOG_URL)
        lock.acquire()
        try:
            with self.assertRaises(SystemExit) as caught:
                catalog.DailyRunLock(CATALOG_URL).acquire()
            self.assertIn("another JobReady daily run is active", str(caught.exception))
        finally:
            lock.release()
        again = catalog.DailyRunLock(CATALOG_URL)
        again.acquire()
        again.release()

    def test_disposable_end_to_end_does_not_write_legacy_databases(self) -> None:
        url = "https://example.test/jobs/e2e"
        first = self._run([_job(url)], sources_attempted=["naukri"])
        self.assertEqual(first["new_catalog_jobs"], 1)
        job_id, _status, created, _synced, _updated, _source = self._row(url)
        second = self._run([_job(url)], sources_attempted=["naukri"])
        self.assertEqual(second["unchanged_catalog_jobs"], 1)
        self.assertEqual(self._row(url)[2], created)
        self._run(
            [_job(url)],
            observations=[(job_id, "active", "confirmed_active")],
            sources_attempted=["naukri"],
        )
        self.assertEqual(self._row(url)[1], "active")
        self._run([_job(url)], observations=[(job_id, "active", "confirmed_expired")])
        self.assertEqual(self._row(url)[1], "expired")
        self.assertEqual(self._count(), 1)
        app_id = str(uuid.uuid4())
        with self.app.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (id, external_id, status, is_active, updated_at)
                VALUES (%s, %s, 'active', true, NOW())
                """,
                (app_id, f"jobs-server:{job_id}"),
            )
        self.app.commit()
        previous_app = os.environ.get("JOB_READY_APP_DATABASE_URL")
        previous_catalog = os.environ.get("JOB_READY_DATABASE_URL")
        previous_codequest = os.environ.get("CODEQUEST_DATABASE_URL")
        previous_local = os.environ.get("DATABASE_URL")
        os.environ["JOB_READY_APP_DATABASE_URL"] = APP_URL
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        if CODEQUEST_SENTINEL:
            os.environ["CODEQUEST_DATABASE_URL"] = CODEQUEST_SENTINEL
        if SCRAPER_SENTINEL:
            os.environ["DATABASE_URL"] = SCRAPER_SENTINEL
        try:
            self._run(
                [_job(url)],
                observations=[(job_id, "expired", "confirmed_expired")],
                app_conn=self.app,
                apply_membership=True,
                confirm_target=_confirm(APP_URL),
                sources_attempted=["naukri"],
            )
            with self.app.cursor() as cur:
                cur.execute("SELECT status, is_active FROM jobs WHERE id = %s", (app_id,))
                self.assertEqual(cur.fetchone(), ("archived", False))
            self._run(
                [_job(url)],
                observations=[(job_id, "expired", "confirmed_active")],
                app_conn=self.app,
                apply_membership=True,
                confirm_target=_confirm(APP_URL),
                sources_attempted=["naukri"],
            )
            self.assertEqual(self._row(url)[1], "active")
            with self.app.cursor() as cur:
                cur.execute("SELECT status, is_active FROM jobs WHERE id = %s", (app_id,))
                self.assertEqual(cur.fetchone(), ("active", True))
            self._assert_sentinel_empty(CODEQUEST_SENTINEL)
            self._assert_sentinel_empty(SCRAPER_SENTINEL)
        finally:
            self._restore("JOB_READY_APP_DATABASE_URL", previous_app)
            self._restore("JOB_READY_DATABASE_URL", previous_catalog)
            self._restore("CODEQUEST_DATABASE_URL", previous_codequest)
            self._restore("DATABASE_URL", previous_local)

    def _restore(self, name: str, value: str | None) -> None:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def test_expired_search_hit_stays_expired_until_confirmed_active(self) -> None:
        url = "https://example.test/jobs/stay-expired"
        self._run([_job(url)])
        job_id, _status, _created, synced, _updated, _source = self._row(url)
        self._run([], observations=[(job_id, "active", "confirmed_expired")])
        expired_row = self._row(url)
        self.assertEqual(expired_row[1], "expired")
        self.assertEqual(expired_row[3], synced)
        self._run([_job(url)])
        after_search = self._row(url)
        self.assertEqual(after_search[1], "expired")
        self.assertGreater(after_search[3], synced)
        self.assertEqual(after_search[4], expired_row[4])
        expired_updated = after_search[4]
        app_id = str(uuid.uuid4())
        with self.app.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (id, external_id, status, is_active, updated_at)
                VALUES (%s, %s, 'active', true, NOW())
                """,
                (app_id, f"jobs-server:{job_id}"),
            )
        self.app.commit()
        previous_app = os.environ.get("JOB_READY_APP_DATABASE_URL")
        previous_catalog = os.environ.get("JOB_READY_DATABASE_URL")
        os.environ["JOB_READY_APP_DATABASE_URL"] = APP_URL
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        try:
            self._run(
                [],
                app_conn=self.app,
                apply_membership=True,
                confirm_target=_confirm(APP_URL),
            )
            with self.app.cursor() as cur:
                cur.execute("SELECT status, is_active FROM jobs WHERE id = %s", (app_id,))
                self.assertEqual(cur.fetchone(), ("archived", False))
            self._run([_job(url)])
            self.assertEqual(self._row(url)[1], "expired")
            self._run(
                [],
                observations=[(job_id, "expired", "confirmed_active")],
                app_conn=self.app,
                apply_membership=True,
                confirm_target=_confirm(APP_URL),
            )
        finally:
            self._restore("JOB_READY_APP_DATABASE_URL", previous_app)
            self._restore("JOB_READY_DATABASE_URL", previous_catalog)
        self.assertEqual(self._row(url)[1], "active")
        self.assertGreater(self._row(url)[4], expired_updated)
        with self.app.cursor() as cur:
            cur.execute("SELECT status, is_active FROM jobs WHERE id = %s", (app_id,))
            self.assertEqual(cur.fetchone(), ("active", True))

    def test_date_salary_and_enrichment_survive_a_thin_rescrape(self) -> None:
        from datetime import datetime, timezone

        url = "https://example.test/jobs/keep-fields"
        posted = datetime(2026, 1, 15, 8, 30, tzinfo=timezone.utc)
        rich = _job(url)
        rich.date_posted = posted
        rich.salary_min = 120000
        rich.salary_max = 180000
        rich.currency = "INR"
        rich.description = (
            "Build Python APIs with Django and PostgreSQL. "
            "Requirements: python, django, sql. Responsibilities: design services."
        )
        self._run([rich])
        with self.catalog.cursor() as cur:
            cur.execute(
                """
                SELECT date_posted, salary_min, salary_max, currency, jd_clean, required_skills, experience_bucket
                FROM validated_jobs WHERE job_url = %s
                """,
                (url,),
            )
            stored = cur.fetchone()
        self.assertIsNotNone(stored[0])
        self.assertEqual(stored[6], "Fresher")
        thin = _job(url)
        thin.description = ""
        thin.date_posted = None
        thin.salary_min = None
        thin.salary_max = None
        thin.currency = ""
        self._run([thin])
        with self.catalog.cursor() as cur:
            cur.execute(
                """
                SELECT date_posted, salary_min, salary_max, currency, jd_clean, required_skills
                FROM validated_jobs WHERE job_url = %s
                """,
                (url,),
            )
            again = cur.fetchone()
        self.assertEqual(again[0], stored[0])
        self.assertEqual(again[1], stored[1])
        self.assertEqual(again[2], stored[2])
        self.assertEqual(again[3], stored[3])
        self.assertEqual(again[4], stored[4])
        self.assertEqual(again[5], stored[5])

    def test_validation_queue_rotates_by_oldest_check(self) -> None:
        urls = [f"https://example.test/jobs/queue-{index}" for index in range(3)]
        self._run([_job(url) for url in urls])
        with self.catalog.cursor() as cur:
            for index, url in enumerate(urls):
                cur.execute(
                    """
                    UPDATE validated_jobs
                    SET link_checked_at = NOW() - (%s || ' days')::interval,
                        last_link_observation = 'confirmed_active:fixture'
                    WHERE job_url = %s
                    """,
                    (3 - index, url),
                )
        self.catalog.commit()
        first = catalog.select_validation_candidates(self.catalog, limit=1, priority_ids=[])
        self.assertEqual(len(first), 1)
        catalog.apply_link_observations(
            self.catalog,
            [(first[0]["job_id"], "active", "unknown")],
        )
        second = catalog.select_validation_candidates(self.catalog, limit=1, priority_ids=[])
        self.assertNotEqual(second[0]["job_id"], first[0]["job_id"])

    def test_validation_alone_does_not_move_synced_at(self) -> None:
        url = "https://example.test/jobs/check-only"
        self._run([_job(url)])
        job_id, _status, _created, synced, updated, _source = self._row(url)
        self._run([], observations=[(job_id, "active", "unknown")])
        self.assertEqual(self._row(url)[3], synced)
        self.assertEqual(self._row(url)[4], updated)
        self.assertEqual(self._row(url)[1], "active")
        self._run([], observations=[(job_id, "active", "confirmed_expired")])
        self.assertEqual(self._row(url)[1], "expired")
        self.assertEqual(self._row(url)[3], synced)

    def test_source_wide_failure_does_not_change_membership(self) -> None:
        url = "https://example.test/jobs/source-down"
        self._run([_job(url)])
        job_id = self._row(url)[0]
        app_id = str(uuid.uuid4())
        with self.app.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (id, external_id, status, is_active, updated_at)
                VALUES (%s, %s, 'active', true, NOW())
                """,
                (app_id, f"jobs-server:{job_id}"),
            )
        self.app.commit()
        previous_app = os.environ.get("JOB_READY_APP_DATABASE_URL")
        previous_catalog = os.environ.get("JOB_READY_DATABASE_URL")
        os.environ["JOB_READY_APP_DATABASE_URL"] = APP_URL
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        health = {
            "naukri": {
                "attempted": True,
                "succeeded": False,
                "failed": True,
                "jobs_returned": 0,
                "errors": ["naukri unavailable"],
            }
        }
        try:
            with self.assertRaises(SystemExit) as caught:
                self._run(
                    [],
                    app_conn=self.app,
                    apply_membership=True,
                    confirm_target=_confirm(APP_URL),
                    sources_attempted=["naukri"],
                    source_health=health,
                )
        finally:
            self._restore("JOB_READY_APP_DATABASE_URL", previous_app)
            self._restore("JOB_READY_DATABASE_URL", previous_catalog)
        self.assertIn("source-wide failure", str(caught.exception))
        self.assertEqual(self._row(url)[1], "active")
        with self.app.cursor() as cur:
            cur.execute("SELECT status, is_active FROM jobs WHERE id = %s", (app_id,))
            self.assertEqual(cur.fetchone(), ("active", True))

    def test_cli_path_validates_catalog_rows(self) -> None:
        from run_all_sources import ScrapeResult

        url = "https://example.test/jobs/cli"
        health = {
            "naukri": {
                "attempted": True,
                "succeeded": True,
                "failed": False,
                "jobs_returned": 1,
                "errors": [],
            }
        }

        def collect(**_kwargs):
            return ScrapeResult(
                jobs=[_job(url)],
                per_source={"naukri": 1},
                per_family={"python-dev": 1},
                errors=[],
                sources_attempted=["naukri"],
                started_at="2026-10-10T00:00:00+00:00",
                finished_at="2026-10-10T00:00:01+00:00",
                source_health=health,
            )

        def probe(row):
            return ("confirmed_active", "fixture_live", row.get("source") or "naukri")

        previous = os.environ.get("JOB_READY_DATABASE_URL")
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        try:
            args = daily.parse_args(["--sources", "naukri", "--validation-limit", "10"])
            report = daily.execute_daily(args, collect=collect, probes={"naukri": probe, "generic": probe})
        finally:
            self._restore("JOB_READY_DATABASE_URL", previous)
        self.reports.append(Path(report["report_path"]))
        self.assertGreater(report["validated_checked"], 0)
        self.assertEqual(report["confirmed_active"], 1)
        self.assertEqual(report["validation_reasons"], {"confirmed_active:fixture_live": 1})
        self.assertEqual(self._row(url)[1], "active")
        self.assertIn("naukri", report["sources_successful"])
        self.assertNotIn("postgresql://", Path(report["report_path"]).read_text(encoding="utf-8").lower())

    def test_mass_application_archive_is_refused(self) -> None:
        previous_app = os.environ.get("JOB_READY_APP_DATABASE_URL")
        previous_catalog = os.environ.get("JOB_READY_DATABASE_URL")
        os.environ["JOB_READY_APP_DATABASE_URL"] = APP_URL
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        with self.app.cursor() as cur:
            for index in range(100):
                cur.execute(
                    """
                    INSERT INTO jobs (id, external_id, status, is_active, updated_at)
                    VALUES (%s, %s, 'active', true, NOW())
                    """,
                    (str(uuid.uuid4()), f"jobs-server:CQJ-MASS-{index:04d}"),
                )
        self.app.commit()
        try:
            with self.assertRaises(SystemExit) as caught:
                sync.archive_ui_jobs_not_in_catalog(confirm_target=_confirm(APP_URL))
        finally:
            self._restore("JOB_READY_APP_DATABASE_URL", previous_app)
            self._restore("JOB_READY_DATABASE_URL", previous_catalog)
        self.assertIn("MEMBERSHIP_REFUSED", str(caught.exception))
        with self.app.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'active' AND is_active IS TRUE")
            self.assertEqual(cur.fetchone()[0], 100)

    def test_preflight_is_read_only(self) -> None:
        url = "https://example.test/jobs/preflight"
        self._run([_job(url)])
        before_count = self._count()
        before_updated = self._row(url)[4]
        previous = os.environ.get("JOB_READY_DATABASE_URL")
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        try:
            code = daily.run_preflight()
        finally:
            self._restore("JOB_READY_DATABASE_URL", previous)
        self.assertEqual(code, 0)
        self.assertEqual(self._count(), before_count)
        self.assertEqual(self._row(url)[4], before_updated)
        if CODEQUEST_SENTINEL:
            os.environ["JOB_READY_DATABASE_URL"] = CODEQUEST_SENTINEL
            try:
                missing = daily.run_preflight()
            finally:
                self._restore("JOB_READY_DATABASE_URL", previous)
            self.assertEqual(missing, 1)
            self._assert_sentinel_empty(CODEQUEST_SENTINEL)

    def test_schema_apply_refuses_duplicate_identities(self) -> None:
        with self.catalog.cursor() as cur:
            cur.execute("DROP INDEX IF EXISTS idx_validated_jobs_source_id_unique")
            cur.execute(
                """
                INSERT INTO validated_jobs (job_id, source_id, source, title, job_url, link_status)
                VALUES
                  ('CQJ-20200101-0001', 'same-source', 'naukri', 'One', 'https://example.test/a', 'active'),
                  ('CQJ-20200101-0002', 'same-source', 'naukri', 'Two', 'https://example.test/b', 'active')
                """
            )
        self.catalog.commit()
        try:
            with self.assertRaises(SystemExit) as caught:
                catalog.apply_schema(self.catalog)
            self.assertIn("SCHEMA_REFUSED", str(caught.exception))
            self.assertEqual(self._count(), 2)
            with self.catalog.cursor() as cur:
                cur.execute("SELECT to_regclass('public.idx_validated_jobs_source_id_unique')")
                self.assertIsNone(cur.fetchone()[0])
        finally:
            with self.catalog.cursor() as cur:
                cur.execute("DELETE FROM validated_jobs WHERE source_id = 'same-source'")
            self.catalog.commit()
            catalog.ensure_schema(self.catalog)

    def test_write_timeouts_are_transaction_local(self) -> None:
        from psycopg2.extensions import TRANSACTION_STATUS_IDLE

        with self.catalog.cursor() as cur:
            cur.execute("SELECT 1")
        self.assertNotEqual(self.catalog.info.transaction_status, TRANSACTION_STATUS_IDLE)
        catalog.release_read_transaction(self.catalog)
        self.assertEqual(self.catalog.info.transaction_status, TRANSACTION_STATUS_IDLE)
        with self.catalog.cursor() as cur:
            catalog.apply_write_timeouts(cur)
            cur.execute(
                """
                SELECT current_setting('lock_timeout'),
                       current_setting('statement_timeout'),
                       current_setting('idle_in_transaction_session_timeout')
                """
            )
            self.assertEqual(cur.fetchone(), ("10s", "2min", "3min"))
        self.catalog.rollback()
        with self.catalog.cursor() as cur:
            cur.execute("SELECT current_setting('lock_timeout')")
            self.assertEqual(cur.fetchone()[0], "0")

    def test_catalog_connections_use_application_names(self) -> None:
        conn = catalog.connect(CATALOG_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_setting('application_name')")
                self.assertEqual(cur.fetchone()[0], "daily-job-run")
        finally:
            conn.close()
        lock = catalog.DailyRunLock(CATALOG_URL)
        lock.acquire()
        try:
            with lock.conn.cursor() as cur:
                cur.execute("SELECT current_setting('application_name')")
                self.assertEqual(cur.fetchone()[0], "daily-job-run-lock")
        finally:
            lock.release()

    def test_bulk_upsert_uses_few_statements_and_keeps_semantics(self) -> None:
        """The old path issued about 3 statements per existing row and 5 per new row.

        One hundred existing rows were about 300 statements, one hundred new rows
        about 500, and a 1,000-row mix about 4,000. The batch path stays small.
        """
        existing_urls = [f"https://example.test/jobs/bulk-existing-{index}" for index in range(1000)]
        seeded = [daily.prepare_catalog_row(_job(url, title=f"Role {index}")) for index, url in enumerate(existing_urls)]
        catalog.upsert_jobs(self.catalog, seeded)
        expired_url = existing_urls[0]
        unchanged_url = existing_urls[1]
        changed_url = existing_urls[2]
        with self.catalog.cursor() as cur:
            cur.execute(
                "UPDATE validated_jobs SET link_status = 'expired' WHERE job_url = %s",
                (expired_url,),
            )
        self.catalog.commit()
        expired_before = self._row(expired_url)
        unchanged_before = self._row(unchanged_url)
        time.sleep(0.05)
        reobserved = []
        for index, url in enumerate(existing_urls[:750]):
            title = "Changed role" if url == changed_url else f"Role {index}"
            reobserved.append(daily.prepare_catalog_row(_job(url, title=title)))
        new_rows = [
            daily.prepare_catalog_row(_job(f"https://example.test/jobs/bulk-new-{index}"))
            for index in range(250)
        ]
        counter = _CountingConnection(self.catalog)
        started = time.perf_counter()
        result = catalog.upsert_jobs(counter, reobserved + new_rows)
        elapsed = time.perf_counter() - started
        print(f"bulk_upsert_sql={counter.executes} elapsed_sec={elapsed:.3f}", flush=True)
        self.assertLessEqual(counter.executes, 20)
        self.assertEqual(result["new"], 250)
        self.assertEqual(result["reobserved"], 750)
        self.assertGreater(result["updated"], 0)
        self.assertGreater(result["unchanged"], 0)
        expired_after = self._row(expired_url)
        unchanged_after = self._row(unchanged_url)
        self.assertEqual(expired_after[0], expired_before[0])
        self.assertEqual(expired_after[1], "expired")
        self.assertEqual(expired_after[2], expired_before[2])
        self.assertGreater(expired_after[3], expired_before[3])
        self.assertEqual(unchanged_after[0], unchanged_before[0])
        self.assertEqual(unchanged_after[2], unchanged_before[2])
        self.assertGreater(unchanged_after[3], unchanged_before[3])
        self.assertEqual(unchanged_after[4], unchanged_before[4])
        self.assertEqual(self._count(), 1250)
        with self.catalog.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT source_id FROM validated_jobs
                    WHERE source_id IS NOT NULL AND btrim(source_id) <> ''
                    GROUP BY source_id HAVING COUNT(*) > 1
                ) dupes
                """
            )
            self.assertEqual(cur.fetchone()[0], 0)
            cur.execute("SELECT COUNT(DISTINCT job_id), COUNT(*) FROM validated_jobs")
            distinct_ids, total = cur.fetchone()
            self.assertEqual(distinct_ids, total)

    def test_upsert_rolls_back_the_whole_batch(self) -> None:
        url = "https://example.test/jobs/kept"
        self._run([_job(url)])
        bad = daily.prepare_catalog_row(_job("https://example.test/jobs/bad-title"))
        bad["title"] = None
        with self.assertRaises(SystemExit) as caught:
            catalog.upsert_jobs(self.catalog, [bad, daily.prepare_catalog_row(_job("https://example.test/jobs/also-new"))])
        self.assertIn("CATALOG_WRITE_FAILED", str(caught.exception))
        self.assertIn("stage=catalog_write", str(caught.exception))
        self.assertEqual(self._count(), 1)
        self.assertIsNotNone(self._row(url))
        self.assertIsNone(self._row("https://example.test/jobs/also-new"))

    def test_lock_timeout_rolls_back_and_releases_allocation(self) -> None:
        previous = catalog.LOCK_TIMEOUT
        catalog.LOCK_TIMEOUT = "300ms"
        holder = self.psycopg2.connect(CATALOG_URL, connect_timeout=8)
        try:
            holder.autocommit = False
            with holder.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (catalog.ALLOC_LOCK_KEY,))
            with self.assertRaises(SystemExit) as caught:
                catalog.upsert_jobs(
                    self.catalog,
                    [daily.prepare_catalog_row(_job("https://example.test/jobs/lock-wait"))],
                )
            self.assertIn("lock_timeout", str(caught.exception))
            self.assertEqual(self._count(), 0)
        finally:
            catalog.LOCK_TIMEOUT = previous
            holder.rollback()
            holder.close()
        catalog.upsert_jobs(self.catalog, [daily.prepare_catalog_row(_job("https://example.test/jobs/lock-wait"))])
        self.assertEqual(self._count(), 1)

    def test_run_lock_releases_when_catalog_write_fails(self) -> None:
        from run_all_sources import ScrapeResult
        from unittest.mock import patch

        def collect(**_kwargs):
            return ScrapeResult(
                jobs=[_job("https://example.test/jobs/lock-release")],
                per_source={"naukri": 1},
                per_family={"python-dev": 1},
                errors=[],
                sources_attempted=["naukri"],
                started_at="2026-10-10T00:00:00+00:00",
                finished_at="2026-10-10T00:00:01+00:00",
                source_health={
                    "naukri": {
                        "attempted": True,
                        "succeeded": True,
                        "failed": False,
                        "jobs_returned": 1,
                        "errors": [],
                    }
                },
            )

        previous = os.environ.get("JOB_READY_DATABASE_URL")
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        try:
            args = daily.parse_args(["--sources", "naukri", "--validation-limit", "1"])
            with patch("run_jobready_daily.upsert_jobs", side_effect=RuntimeError("forced catalog write failure")):
                with self.assertRaises(RuntimeError):
                    daily.execute_daily(args, collect=collect, probes={"generic": lambda row: ("inconclusive", "fixture", "naukri")})
            lock = catalog.DailyRunLock(CATALOG_URL)
            lock.acquire()
            lock.release()
        finally:
            self._restore("JOB_READY_DATABASE_URL", previous)

    def test_concurrent_block_allocation_is_unique(self) -> None:
        errors: list[BaseException] = []

        def worker(offset: int) -> None:
            conn = self.psycopg2.connect(CATALOG_URL, connect_timeout=8)
            try:
                rows = [
                    daily.prepare_catalog_row(_job(f"https://example.test/jobs/block-{offset}-{index}"))
                    for index in range(5)
                ]
                catalog.upsert_jobs(conn, rows)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(offset,)) for offset in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        with self.catalog.cursor() as cur:
            cur.execute("SELECT job_id FROM validated_jobs")
            ids = [row[0] for row in cur.fetchall()]
        self.assertEqual(len(ids), 20)
        self.assertEqual(len(set(ids)), 20)

    def _assert_sentinel_empty(self, url: str) -> None:
        if not url:
            return
        conn = self.psycopg2.connect(url, connect_timeout=8)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                self.assertEqual(cur.fetchall(), [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
