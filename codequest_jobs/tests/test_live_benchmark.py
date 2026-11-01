"""Safety and accuracy tests for live collection / validation benchmarks."""
from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

import audit_job_ready as audit  # noqa: E402
from benchmark_isolation import (  # noqa: E402
    check_public_collection_isolation,
    check_validation_benchmark_environment,
    redact_urls,
)
from cost_calculator import estimate_queue_runtime, project_costs  # noqa: E402
from link_policy import CONFIRMED_ACTIVE, CONFIRMED_EXPIRED, INCONCLUSIVE  # noqa: E402
from live_collection_benchmark import build_report, format_public_log_line, sanitize_errors  # noqa: E402


class LiveBenchmarkIsolationTests(unittest.TestCase):
    def test_public_collection_rejects_catalog_database_url(self):
        with self.assertRaises(ValueError):
            check_public_collection_isolation({"JOB_READY_DATABASE_URL": "postgresql://x/y"})

    def test_validation_rejects_github_actions(self):
        with self.assertRaises(ValueError):
            check_validation_benchmark_environment(
                {"GITHUB_ACTIONS": "true", "JOBREADY_AUDIT_READONLY_URL": "postgresql://localhost/db"}
            )

    def test_redact_urls_from_errors(self):
        text = redact_urls("failed https://example.com/jobs/123 secret")
        self.assertNotIn("example.com", text)
        self.assertIn("[url-redacted]", text)

    def test_public_workflow_has_no_database_or_artifacts(self):
        workflow = (REPO / ".github" / "workflows" / "jobready-live-benchmark.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("persist-credentials: false", workflow)
        for forbidden in ("secrets.", "schedule:", "postgres:", "upload-artifact", "--sync-application", "JOB_READY_DATABASE_URL"):
            self.assertNotIn(forbidden, workflow)

    def test_public_log_line_has_no_urls(self):
        report = {
            "status": "pass",
            "sources_requested": "naukri,indeed",
            "jobs_collected_total_raw": 3,
            "jobs_unique_after_dedupe": 2,
            "source_error_count": 0,
            "collection_elapsed_seconds": 1.2,
            "dedupe_elapsed_seconds": 0.01,
            "workflow_runtime_seconds": 1.5,
            "peak_memory_mb": 42.0,
        }
        line = format_public_log_line(report)
        self.assertNotIn("http", line.lower())
        self.assertIn("BENCH_COLLECT", line)


class AuditLimitTests(unittest.TestCase):
    def _mock_catalog_rows(self, count: int) -> None:
        rows = [
            (f"id-{index:03d}", "naukri", f"https://n.example/{index}", "", "active")
            for index in range(count)
        ]
        connection = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.cursor.return_value.__exit__.return_value = False
        connection.set_session = mock.Mock()
        self.connect_patch = mock.patch.object(audit.psycopg2, "connect", return_value=connection)
        self.connect_patch.start()
        self.addCleanup(self.connect_patch.stop)

    def test_limit_enforced_at_fetch(self):
        self._mock_catalog_rows(30)
        limited = audit.fetch_job_ready_rows(limit=25, catalog_url="postgresql://readonly@localhost/catalog")
        self.assertEqual(len(limited), 25)
        self.assertEqual(limited[0]["job_id"], "id-000")

    def test_limit_above_cap_refused(self):
        self._mock_catalog_rows(1)
        with self.assertRaises(SystemExit):
            audit.fetch_job_ready_rows(limit=201, catalog_url="postgresql://readonly@localhost/catalog")

    def test_inconclusive_never_counts_as_confirmed_expired(self):
        row = {"job_id": "1", "source": "naukri", "job_url": "https://n.example/1", "apply_url": "", "link_status": "active"}
        with mock.patch.object(audit, "probe_for", return_value=object()), mock.patch.object(
            audit,
            "validate_candidates",
            return_value=[mock.Mock(observation=INCONCLUSIVE, reason="timeout", provider="naukri")],
        ):
            result = audit.audit_row(row)
        self.assertEqual(result["observation"], INCONCLUSIVE)
        self.assertNotEqual(result["observation"], CONFIRMED_EXPIRED)

    def test_summarize_preserves_lifecycle_semantics(self):
        results = [
            {"observation": CONFIRMED_ACTIVE, "reason": "open", "source": "naukri", "persisted_status": "active"},
            {"observation": INCONCLUSIVE, "reason": "timeout", "source": "indeed", "persisted_status": "active"},
            {"observation": audit.UNSUPPORTED, "reason": "unsupported_source", "source": "other", "persisted_status": "active"},
        ]
        summary = audit.summarize(results)
        self.assertEqual(summary[CONFIRMED_ACTIVE], 1)
        self.assertEqual(summary[INCONCLUSIVE], 1)
        self.assertEqual(summary["checked"], 2)


class CostCalculatorTests(unittest.TestCase):
    def test_runtime_estimates_scale_linearly(self):
        estimates = estimate_queue_runtime(10, 20.0)
        self.assertEqual(estimates[1000], 2000.0)

    def test_cost_projection_marks_rates_unverified(self):
        costs = project_costs(collection_seconds=120.0, validation_seconds_by_limit={25: 30.0})
        self.assertFalse(costs["collection"]["1x_daily"]["github_public"]["rates_verified"])

    def test_railway_monthly_cost_does_not_double_count_days(self):
        costs = project_costs(collection_seconds=3600.0, validation_seconds_by_limit={})
        railway = costs["collection"]["1x_daily"]["railway_cron"]
        self.assertLess(railway["estimated_usd_assumption"], 5.0)


class LiveCollectionReportTests(unittest.TestCase):
    def test_sanitize_errors_strips_urls(self):
        errors = sanitize_errors(["timeout for https://secret.example/path"])
        self.assertNotIn("secret.example", errors[0])

    def test_build_report_counts(self):
        class Result:
            per_source = {"naukri": 2, "indeed": 1}
            sources_attempted = ["naukri", "indeed"]
            errors = []
            source_health = {
                "naukri": {"calls": 2, "call_failures": 0, "succeeded": True, "failed": False, "jobs_returned": 2, "errors": []},
                "indeed": {"calls": 2, "call_failures": 1, "succeeded": True, "failed": False, "jobs_returned": 1, "errors": ["indeed:timeout"]},
            }

        report = build_report(
            sources="naukri,indeed",
            max_pages=1,
            results_wanted=5,
            family_ids=["python-dev"],
            scrape_result=Result(),
            raw_count=3,
            unique_count=2,
            collection_seconds=4.0,
            dedupe_seconds=0.01,
            workflow_started=0.0,
        )
        self.assertEqual(report["jobs_unique_after_dedupe"], 2)
        self.assertFalse(report["production_database_accessed"])
        self.assertIn("naukri", report["sources_completed"])
        self.assertNotIn("indeed", report["sources_failed"])


if __name__ == "__main__":
    unittest.main()
