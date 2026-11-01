"""Public GitHub Actions test pilot. Never uses production credentials.

Only uses disposable PostgreSQL databases on localhost and runs existing tests.
It does NOT collect or publish real job links or contact production.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASES = {
    "JOBREADY_UI_SYNC_APP_URL": "jobready_ui_sync_app_test",
    "JOBREADY_UI_SYNC_CATALOG_URL": "jobready_ui_sync_catalog_test",
    "JOBREADY_DAILY_CATALOG_URL": "jobready_daily_catalog",
    "JOBREADY_DAILY_APP_URL": "jobready_daily_app",
    "JOBREADY_SENTINEL_CODEQUEST_URL": "jobready_sentinel_codequest",
    "JOBREADY_SENTINEL_SCRAPER_URL": "jobready_sentinel_scraper",
}
FORBIDDEN_SECRETS = frozenset({
    "JOB_READY_DATABASE_URL", "JOB_READY_APP_DATABASE_URL", "JOBS_SOURCE_DATABASE_URL",
    "DATABASE_URL", "DATABASE_URL_SYNC", "JOBS_APPLY_DATABASE_URL",
    "SQL_SANDBOX_ADMIN_DATABASE_URL", "SQL_SANDBOX_RUNNER_DATABASE_URL",
    "PGPASSWORD", "PGHOST", "PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGPASSFILE",
    "GREENHOUSE_HARVEST_CLIENT_SECRET", "GREENHOUSE_HARVEST_CLIENT_ID",
    "INDEED_API_KEY", "APIFY_TOKEN", "RAILWAY_TOKEN", "RAILWAY_API_TOKEN",
})


def check_isolation(environ: dict[str, str]) -> int:
    """Fail closed unless every configured test DB is local and exactly named."""
    if any(environ.get(name) for name in FORBIDDEN_SECRETS):
        raise ValueError("Production connection settings are forbidden in the public pilot")
    for key in environ:
        if key.startswith(("JOB_READY_", "JOBS_SOURCE_", "JOBS_APPLY_", "GREENHOUSE_HARVEST_")) and environ.get(key):
            raise ValueError("Production job credentials must not enter the public pilot")
    for name, expected_database in TEST_DATABASES.items():
        url = environ.get(name, "")
        try:
            parsed = urlsplit(url)
            match = (parsed.scheme == "postgresql" and
                     parsed.hostname in ("localhost", "127.0.0.1") and
                     parsed.port == 5432 and
                     parsed.path == "/" + expected_database and
                     parsed.username == "postgres" and
                     not parsed.query and not parsed.fragment)
        except (TypeError, ValueError):
            match = False
        if not match:
            raise ValueError("The public pilot requires six exactly named localhost test databases")
    return len(TEST_DATABASES)


def summarize(result: unittest.TestResult, seconds: float) -> dict:
    tests = result.testsRun
    failures = len(result.failures)
    errors = len(result.errors)
    skipped = len(result.skipped)
    passed = max(0, tests - failures - errors - skipped)
    return {
        "status": "pass" if passed > 0 and failures == 0 and errors == 0 else "fail",
        "tests": tests,
        "passed": passed,
        "failed": failures,
        "errors": errors,
        "skipped": skipped,
        "seconds": round(seconds, 2),
    }


def sanitized_result(raw: dict) -> dict:
    """Explicit allowlist: never serialize exception traces, SQL, or links."""
    allowed = ("status", "tests", "passed", "failed", "errors", "skipped", "seconds")
    return {**{key: raw[key] for key in allowed if key in raw},
            "mode": "public_synthetic_pilot",
            "data_access": "disposable_postgres_only",
            "live_database_touched": False,
            "production_scrape_ran": False,
            "production_publish_ran": False}


def main() -> int:
    try:
        check_isolation(os.environ)
    except ValueError:
        # Never include connection strings or environment values in logs.
        print("PILOT_FAIL isolation guard rejected unsafe or missing test database settings")
        return 3
    started = time.monotonic()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "codequest_jobs" / "tests"), pattern="test_*.py")
    # Capture failing test messages locally; they must not leak into PUBLIC logs.
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=1, buffer=True).run(suite)
    report = sanitized_result(summarize(result, time.monotonic() - started))
    print("PILOT_" + report["status"].upper(),
          "tests=" + str(report["tests"]),
          "passed=" + str(report["passed"]),
          "failed=" + str(report["failed"]),
          "errors=" + str(report["errors"]),
          "skipped=" + str(report["skipped"]),
          "seconds=" + str(report["seconds"]))
    message = (f"### JobReady public pilot: {report['status'].upper()}\n\n"
               f"Synthetic/disposable tests: {report['tests']} total, {report['passed']} passed, "
               f"{report['failed']} failed, {report['errors']} errors, {report['skipped']} skipped.\n\n"
               "Production database, collection, and publication: **not accessed**.\n")
    # Only write counts, not test traces, job URLs or connection information.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(message)
    # The temporary runner directory is outside the checked-out public repo.
    runner_temp = os.environ.get("RUNNER_TEMP", "")
    if runner_temp:
        Path(runner_temp, "jobready_public_pilot.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
