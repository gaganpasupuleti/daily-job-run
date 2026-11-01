"""Safety tests for the public, synthetic-only GitHub Actions pilot."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from public_pilot import check_isolation, summarize, sanitized_result


TEST_URLS = {
    "JOBREADY_UI_SYNC_APP_URL": "postgresql://postgres:postgres@localhost:5432/jobready_ui_sync_app_test",
    "JOBREADY_UI_SYNC_CATALOG_URL": "postgresql://postgres:postgres@localhost:5432/jobready_ui_sync_catalog_test",
    "JOBREADY_DAILY_CATALOG_URL": "postgresql://postgres:postgres@localhost:5432/jobready_daily_catalog",
    "JOBREADY_DAILY_APP_URL": "postgresql://postgres:postgres@localhost:5432/jobready_daily_app",
    "JOBREADY_SENTINEL_CODEQUEST_URL": "postgresql://postgres:postgres@localhost:5432/jobready_sentinel_codequest",
    "JOBREADY_SENTINEL_SCRAPER_URL": "postgresql://postgres:postgres@localhost:5432/jobready_sentinel_scraper",
}


class PublicPilotIsolationTests(unittest.TestCase):
    def test_all_known_disposable_databases_are_allowed(self):
        self.assertEqual(check_isolation(TEST_URLS), 6)

    def test_rejects_railway_production_credentials(self):
        for key in (
            "JOB_READY_DATABASE_URL", "JOB_READY_APP_DATABASE_URL", "JOBS_SOURCE_DATABASE_URL",
            "DATABASE_URL", "SQL_SANDBOX_ADMIN_DATABASE_URL", "PGPASSWORD", "GREENHOUSE_HARVEST_CLIENT_SECRET",
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_isolation({**TEST_URLS, key: "top-secret"})

    def test_rejects_nonlocal_db_even_if_it_is_a_test_named_database(self):
        env = {**TEST_URLS, "JOBREADY_DAILY_APP_URL": "postgresql://user:x@acela.proxy.rlwy.net:43501/jobready_daily_app"}
        with self.assertRaises(ValueError):
            check_isolation(env)

    def test_rejects_wrong_db_and_wrong_port(self):
        with self.assertRaises(ValueError):
            check_isolation({**TEST_URLS, "JOBREADY_DAILY_APP_URL": "postgresql://postgres:postgres@localhost:5432/railway"})
        with self.assertRaises(ValueError):
            check_isolation({**TEST_URLS, "JOBREADY_DAILY_APP_URL": "postgresql://postgres:postgres@localhost:1234/jobready_daily_app"})

    def test_rejects_missing_test_db(self):
        env = dict(TEST_URLS)
        del env["JOBREADY_DAILY_APP_URL"]
        with self.assertRaises(ValueError):
            check_isolation(env)

    def test_rejects_variable_injection_via_pgservice(self):
        with self.assertRaises(ValueError):
            check_isolation({**TEST_URLS, "PGSERVICE": "production"})

    def test_summary_does_not_leak_test_exceptions(self):
        class MockResult:
            testsRun = 11
            failures = [(object(), "password=very-secret")]
            errors = [(object(), "postgresql://user:secret@example/db")]
            skipped = []
        r = summarize(MockResult(), 3.24)
        encoded = str(r)
        self.assertNotIn("secret", encoded)
        self.assertEqual(r["status"], "fail")
        self.assertEqual(r["passed"], 9)

    def test_all_skipped_or_no_tests_is_not_a_pass(self):
        class MockResult:
            testsRun = 8
            failures = []
            errors = []
            skipped = [(None, "reason")] * 8
        self.assertEqual(summarize(MockResult(), 0.1)["status"], "fail")
        MockResult.testsRun = 0
        MockResult.skipped = []
        self.assertEqual(summarize(MockResult(), 0.1)["status"], "fail")

    def test_public_workflow_never_loads_secrets_or_schedules_production(self):
        workflow = (ROOT.parent / ".github" / "workflows" / "jobready-public-pilot.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        for forbidden in ("secrets.", "schedule:", "--sync-application", "upload-artifact", "JOB_READY_DATABASE_URL"):
            self.assertNotIn(forbidden, workflow)

    def test_public_workflow_does_not_connect_to_railway(self):
        workflow = (ROOT.parent / ".github" / "workflows" / "jobready-public-pilot.yml").read_text(encoding="utf-8")
        self.assertNotIn("proxy.rlwy.net", workflow)
        self.assertNotIn("railway run", workflow)
        self.assertNotIn("run_jobready_daily.py", workflow)

    def test_report_exports_only_approved_keys(self):
        report = sanitized_result({"status":"pass", "tests":10,"passed":10,"skipped":0,"failed":0,"errors":0,"seconds":1.5, "connection_string":"postgresql://very-private"})
        self.assertNotIn("connection_string", report)
        self.assertEqual(report["data_access"], "disposable_postgres_only")


if __name__ == "__main__":
    unittest.main()
