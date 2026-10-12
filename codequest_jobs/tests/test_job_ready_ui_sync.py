"""JobReady application sync keeps status and is_active together.

Integration cases use disposable databases named by JOBREADY_UI_SYNC_APP_URL
and JOBREADY_UI_SYNC_CATALOG_URL. They are skipped when those variables are absent.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_URL = os.environ.get("JOBREADY_UI_SYNC_APP_URL", "").strip()
CATALOG_URL = os.environ.get("JOBREADY_UI_SYNC_CATALOG_URL", "").strip()
if APP_URL:
    os.environ["JOB_READY_APP_DATABASE_URL"] = APP_URL
if CATALOG_URL:
    os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL

import sync_job_ready_ui as sync  # noqa: E402

REPO = ROOT.parent
KEY_ASSIGNMENT = re.compile(r"""indeed-api-key["']?\s*[:=]\s*["'][0-9a-fA-F]{16,}""")


class SafetyTests(unittest.TestCase):
    def test_write_without_confirmation_is_refused(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = sync.main([])
        self.assertEqual(code, 2)
        self.assertIn("APPLY_REFUSED", stdout.getvalue())
        self.assertNotIn("://", stdout.getvalue())

    def test_confirmation_cannot_be_a_dsn(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = sync.main(["--confirm-target", "postgresql://user:SENTINELPASSWORD@127.0.0.1:5433/appdb"])
        combined = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("APPLY_REFUSED", combined)
        self.assertNotIn("SENTINELPASSWORD", combined)

    def test_audit_without_prune_does_not_call_the_writer(self) -> None:
        import audit_job_ready

        original_fetch = audit_job_ready.fetch_job_ready_rows
        original_sync = audit_job_ready.sync_dead_to_job_ready
        calls: list[str] = []
        audit_job_ready.fetch_job_ready_rows = lambda: []
        audit_job_ready.sync_dead_to_job_ready = lambda *args, **kwargs: calls.append("sync")
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = audit_job_ready.main([])
        finally:
            audit_job_ready.fetch_job_ready_rows = original_fetch
            audit_job_ready.sync_dead_to_job_ready = original_sync
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertIn("will not be modified", stdout.getvalue())

    def test_retired_prune_does_not_call_the_writer(self) -> None:
        import prune_job_ready_now

        calls: list[str] = []
        original = sync.archive_ui_jobs_not_in_catalog
        sync.archive_ui_jobs_not_in_catalog = lambda **kwargs: calls.append("archive")
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = prune_job_ready_now.main([])
        finally:
            sync.archive_ui_jobs_not_in_catalog = original
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])
        self.assertIn("RETIRED_COMMAND prune_job_ready_now", stdout.getvalue())

    def test_validate_links_default_does_not_sync_application_jobs(self) -> None:
        import validate_links

        originals = {
            name: getattr(validate_links, name)
            for name in (
                "fetch_jobs",
                "validate_one",
                "apply_updates",
                "start_validation_run",
                "finish_validation_run",
                "status_summary",
                "sync_dead_to_job_ready",
            )
        }
        calls: list[str] = []
        validate_links.fetch_jobs = lambda **kwargs: [
            ("1", "J1", "board", "http://example.test/j", "", "active")
        ]
        validate_links.validate_one = lambda row, **kwargs: (row[0], row[1], row[2], "expired")
        validate_links.apply_updates = lambda rows: calls.append("codequest")
        validate_links.start_validation_run = lambda **kwargs: ("run", None)
        validate_links.finish_validation_run = lambda *args, **kwargs: None
        validate_links.status_summary = lambda: {"by_status": {}}
        validate_links.sync_dead_to_job_ready = lambda *args, **kwargs: calls.append("app")
        previous = sys.argv
        sys.argv = ["validate_links.py", "--env-file", str(REPO / "missing.env")]
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                validate_links.main()
        finally:
            sys.argv = previous
            for name, original in originals.items():
                setattr(validate_links, name, original)
        self.assertEqual(calls, ["codequest"])
        self.assertIn("will not be modified", stdout.getvalue())

    def test_validate_links_sync_flag_requires_confirmation_before_fetch(self) -> None:
        import validate_links

        fetched: list[int] = []
        original_fetch = validate_links.fetch_jobs
        validate_links.fetch_jobs = lambda **kwargs: fetched.append(1)
        previous = sys.argv
        sys.argv = ["validate_links.py", "--sync-job-ready-every", "1", "--env-file", str(REPO / "missing.env")]
        try:
            with self.assertRaises(SystemExit) as caught:
                validate_links.main()
        finally:
            sys.argv = previous
            validate_links.fetch_jobs = original_fetch
        self.assertIn("explicit target confirmation", str(caught.exception))
        self.assertEqual(fetched, [])

    def test_indeed_key_is_not_hardcoded(self) -> None:
        previous = os.environ.pop("INDEED_API_KEY", None)
        try:
            for relative in (
                Path("codequest_jobs") / "indeed_checker.py",
                Path("link_validator_service") / "indeed_checker.py",
            ):
                text = (REPO / relative).read_text(encoding="utf-8")
                self.assertIsNone(KEY_ASSIGNMENT.search(text), relative.as_posix())
            from indeed_checker import batch_indeed_expired, indeed_headers

            self.assertNotIn("indeed-api-key", indeed_headers())
            self.assertEqual(batch_indeed_expired(["abc"]), {"abc": None})
        finally:
            if previous is not None:
                os.environ["INDEED_API_KEY"] = previous

    def test_env_example_names_the_key_without_a_value(self) -> None:
        lines = (REPO / "naukri_jobs_scraper" / ".env.example").read_text(encoding="utf-8").splitlines()
        self.assertIn("INDEED_API_KEY=", lines)
        self.assertIn("JOB_READY_APP_DATABASE_URL=", lines)
        self.assertNotIn(True, [bool(KEY_ASSIGNMENT.search(line)) for line in lines])

    def test_credentials_are_not_printed_when_the_target_is_unreachable(self) -> None:
        script = """
import os, sys
sys.path.insert(0, sys.argv[1])
os.environ["JOB_READY_APP_DATABASE_URL"] = "postgresql://user:SENTINELPASSWORD@127.0.0.1:59999/appdb"
os.environ["JOB_READY_DATABASE_URL"] = "postgresql://user:OTHERSECRETTOKEN@127.0.0.1:59999/catalogdb"
import sync_job_ready_ui
raise SystemExit(sync_job_ready_ui.main(["--confirm-target", "127.0.0.1:59999/appdb"]))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(ROOT)],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
        combined = completed.stdout + completed.stderr
        self.assertNotIn("SENTINELPASSWORD", combined)
        self.assertNotIn("OTHERSECRETTOKEN", combined)
        self.assertNotIn("postgresql://", combined)
        self.assertTrue("UI_SYNC_FAILED" in combined or "APPLY_REFUSED" in combined)


@unittest.skipUnless(APP_URL and CATALOG_URL, "disposable sync databases are not configured")
class InvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg2

        cls.psycopg2 = psycopg2
        try:
            app = psycopg2.connect(APP_URL, connect_timeout=8)
            catalog = psycopg2.connect(CATALOG_URL, connect_timeout=8)
        except Exception:
            raise unittest.SkipTest("disposable database unavailable") from None
        cls.app = app
        cls.catalog = catalog
        cls.confirm = _confirm(APP_URL)
        with app.cursor() as cur:
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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_jobs (
                    id integer PRIMARY KEY,
                    job_id uuid,
                    note text
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS job_applications (
                    id integer PRIMARY KEY,
                    job_id uuid,
                    note text
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS job_publication_decisions (
                    id integer PRIMARY KEY,
                    job_id uuid,
                    decision text
                )
                """
            )
        app.commit()
        with catalog.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS validated_jobs")
            cur.execute(
                """
                CREATE TABLE validated_jobs (
                    job_id text PRIMARY KEY,
                    source text,
                    job_url text,
                    apply_url text,
                    link_status text NOT NULL DEFAULT 'active'
                )
                """
            )
        catalog.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.close()
        cls.catalog.close()

    def setUp(self) -> None:
        self._truncate()

    def _truncate(self) -> None:
        with self.app.cursor() as cur:
            cur.execute(
                "TRUNCATE jobs, saved_jobs, job_applications, job_publication_decisions"
            )
        self.app.commit()
        with self.catalog.cursor() as cur:
            cur.execute("TRUNCATE validated_jobs")
        self.catalog.commit()

    def _insert_job(self, job_key: str, status: str, is_active: bool) -> str:
        job_id = str(uuid.uuid4())
        with self.app.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (
                    id, external_id, status, is_active, updated_at, first_seen_at, last_seen_at
                )
                VALUES (%s, %s, %s, %s, '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z', '2026-01-03T00:00:00Z')
                """,
                (job_id, f"jobs-server:{job_key}", status, is_active),
            )
        self.app.commit()
        return job_id

    def _row(self, job_id: str) -> tuple:
        with self.app.cursor() as cur:
            cur.execute(
                """
                SELECT status, is_active, updated_at, first_seen_at, last_seen_at
                FROM jobs WHERE id = %s
                """,
                (job_id,),
            )
            return cur.fetchone()

    def _catalog(self, *job_keys: str) -> None:
        with self.catalog.cursor() as cur:
            for job_key in job_keys:
                cur.execute("INSERT INTO validated_jobs (job_id) VALUES (%s)", (job_key,))
        self.catalog.commit()

    def _sync(self) -> dict[str, int]:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = sync.archive_ui_jobs_not_in_catalog(confirm_target=self.confirm)
        self.assertNotIn("://", stdout.getvalue())
        self.assertNotIn("@", stdout.getvalue())
        return result

    def test_missing_catalog_job_is_archived_and_inactive(self) -> None:
        job_id = self._insert_job("CQJ-MISSING", "active", True)
        result = self._sync()
        status, is_active, _updated, first_seen, last_seen = self._row(job_id)
        self.assertEqual(status, "archived")
        self.assertIs(is_active, False)
        self.assertEqual(result["archived"], 1)
        self.assertEqual(first_seen, last_seen.replace(day=2))
        self.assertEqual(last_seen, first_seen.replace(day=3))

    def test_returning_job_is_active_and_flagged_active(self) -> None:
        job_id = self._insert_job("CQJ-BACK", "archived", False)
        self._catalog("CQJ-BACK")
        result = self._sync()
        status, is_active, *_rest = self._row(job_id)
        self.assertEqual(status, "active")
        self.assertIs(is_active, True)
        self.assertEqual(result["reactivated"], 1)

    def test_repeated_archive_is_idempotent(self) -> None:
        job_id = self._insert_job("CQJ-ONCE", "active", True)
        self._sync()
        _status, _flag, updated, *_rest = self._row(job_id)
        again = self._sync()
        status, is_active, updated_again, *_rest = self._row(job_id)
        self.assertEqual(again["archived"], 0)
        self.assertEqual(status, "archived")
        self.assertIs(is_active, False)
        self.assertEqual(updated_again, updated)

    def test_repeated_reactivation_is_idempotent(self) -> None:
        job_id = self._insert_job("CQJ-AGAIN", "archived", False)
        self._catalog("CQJ-AGAIN")
        self._sync()
        _status, _flag, updated, *_rest = self._row(job_id)
        again = self._sync()
        status, is_active, updated_again, *_rest = self._row(job_id)
        self.assertEqual(again["reactivated"], 0)
        self.assertEqual(status, "active")
        self.assertIs(is_active, True)
        self.assertEqual(updated_again, updated)

    def test_archive_leaves_saved_jobs_applications_and_decisions(self) -> None:
        job_id = self._insert_job("CQJ-KEEP", "active", True)
        with self.app.cursor() as cur:
            cur.execute("INSERT INTO saved_jobs VALUES (1, %s, 'saved')", (job_id,))
            cur.execute("INSERT INTO job_applications VALUES (1, %s, 'applied')", (job_id,))
            cur.execute(
                "INSERT INTO job_publication_decisions VALUES (1, %s, 'publish')",
                (job_id,),
            )
        self.app.commit()
        self._sync()
        with self.app.cursor() as cur:
            cur.execute("SELECT note FROM saved_jobs")
            saved = cur.fetchall()
            cur.execute("SELECT note FROM job_applications")
            applications = cur.fetchall()
            cur.execute("SELECT decision FROM job_publication_decisions")
            decisions = cur.fetchall()
        self.assertEqual(saved, [("saved",)])
        self.assertEqual(applications, [("applied",)])
        self.assertEqual(decisions, [("publish",)])

    def test_historical_archived_but_active_row_is_corrected(self) -> None:
        job_id = self._insert_job("CQJ-DRIFT", "archived", True)
        before = self._row(job_id)
        self.assertEqual(before[0], "archived")
        self.assertIs(before[1], True)
        self._sync()
        status, is_active, *_rest = self._row(job_id)
        self.assertEqual(status, "archived")
        self.assertIs(is_active, False)

    def test_active_archive_then_reappear_round_trip(self) -> None:
        job_id = self._insert_job("CQJ-ROUND", "active", True)
        self._sync()
        status, is_active, *_rest = self._row(job_id)
        self.assertEqual((status, is_active), ("archived", False))
        self._catalog("CQJ-ROUND")
        self._sync()
        status, is_active, *_rest = self._row(job_id)
        self.assertEqual((status, is_active), ("active", True))

    def test_audit_and_unconfirmed_commands_do_not_write(self) -> None:
        import audit_job_ready
        import prune_job_ready_now

        job_id = self._insert_job("CQJ-READONLY", "active", True)
        before = self._row(job_id)
        with self.catalog.cursor() as cur:
            cur.executemany(
                "INSERT INTO validated_jobs (job_id, source, job_url, link_status) VALUES (%s, 'foundit', %s, %s)",
                [
                    ("CQJ-AUDIT-ACTIVE", "https://www.foundit.in/job/a-1", "active"),
                    ("CQJ-AUDIT-EXPIRED", "https://www.foundit.in/job/e-2", "expired"),
                ],
            )
        self.catalog.commit()
        catalog_query = "SELECT job_id, source, job_url, apply_url, link_status FROM validated_jobs ORDER BY job_id"
        with self.catalog.cursor() as cur:
            cur.execute(catalog_query)
            catalog_before = cur.fetchall()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            audit_code = audit_job_ready.main([])
            prune_code = prune_job_ready_now.main([])
            sync_code = sync.main([])
        self.assertEqual(audit_code, 0)
        self.assertEqual(prune_code, 2)
        self.assertEqual(sync_code, 2)
        self.assertEqual(self._row(job_id), before)
        with self.catalog.cursor() as cur:
            cur.execute(catalog_query)
            self.assertEqual(cur.fetchall(), catalog_before)
        output = stdout.getvalue()
        self.assertIn('"confirmed_active": 0', output)
        self.assertIn('"inconclusive": 2', output)
        self.assertIn('"persisted_active": 1', output)
        self.assertIn('"persisted_expired": 1', output)
        self.assertNotIn("://", output)

    def test_catalog_database_cannot_be_the_application_target(self) -> None:
        previous_app = os.environ["JOB_READY_APP_DATABASE_URL"]
        previous_catalog = os.environ["JOB_READY_DATABASE_URL"]
        os.environ["JOB_READY_APP_DATABASE_URL"] = CATALOG_URL
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        try:
            with self.assertRaises(SystemExit) as caught:
                sync.archive_ui_jobs_not_in_catalog(confirm_target=_confirm(CATALOG_URL))
        finally:
            os.environ["JOB_READY_APP_DATABASE_URL"] = previous_app
            os.environ["JOB_READY_DATABASE_URL"] = previous_catalog
        self.assertIn("catalog database", str(caught.exception))
        with self.catalog.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM validated_jobs")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_dry_run_does_not_change_rows(self) -> None:
        job_id = self._insert_job("CQJ-DRY", "active", True)
        before = self._row(job_id)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = sync.archive_ui_jobs_not_in_catalog(dry_run=True)
        self.assertEqual(result["archived"], 0)
        self.assertEqual(result["would_archive"], 1)
        self.assertEqual(self._row(job_id), before)
        self.assertNotIn("://", stdout.getvalue())


def _confirm(database_url: str) -> str:
    host, port, database = sync.database_identity(database_url)
    return f"{host}:{port}/{database}"


if __name__ == "__main__":
    unittest.main(verbosity=2)
