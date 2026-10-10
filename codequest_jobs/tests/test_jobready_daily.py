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
            cur.execute("TRUNCATE validated_jobs RESTART IDENTITY")
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
