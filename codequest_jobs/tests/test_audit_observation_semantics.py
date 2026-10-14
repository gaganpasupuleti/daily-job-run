"""The catalog audit reports stored lifecycle state and canonical observations separately.

Pure tests: probes are injected or never reach the network, and psycopg2 is faked.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRAPER = ROOT.parent / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

import audit_job_ready  # noqa: E402
import sync_job_ready_ui  # noqa: E402
from jobready_validator import interpret_linkedin, interpret_naukri  # noqa: E402
from linkedin_checker import GUEST_URL  # noqa: E402

LINKEDIN_ID = "4475277813"
GUEST = GUEST_URL.format(job_id=LINKEDIN_ID)
NAUKRI_ID = "051026501163"
NAUKRI_DETAIL = f"https://www.naukri.com/jobapi/v1/job/{NAUKRI_ID}"


def row(job_id: str, source: str, status: str | None, url: str = "https://jobs.example.test/1") -> dict:
    return {"job_id": job_id, "source": source, "job_url": url, "apply_url": None, "link_status": status}


def linkedin_probe(*, status_code=200, body="", error=None):
    def probe(_row):
        return (
            *interpret_linkedin(
                status_code=status_code, body=body, expected_job_id=LINKEDIN_ID, final_url=GUEST, error=error
            ),
            "linkedin",
        )

    return probe


def naukri_probe(*, status_code=200, payload=None, error=None):
    def probe(_row):
        return (
            *interpret_naukri(
                status_code=status_code, payload=payload, expected_job_id=NAUKRI_ID, final_url=NAUKRI_DETAIL, error=error
            ),
            "naukri",
        )

    return probe


def audit(source: str, status: str | None, probe) -> dict:
    return audit_job_ready.audit_row(row("CQJ-1", source, status), probes={source: probe})


class ObservationTests(unittest.TestCase):
    def test_active_row_with_timeout_stays_active_and_is_inconclusive(self) -> None:
        result = audit("linkedin", "active", linkedin_probe(status_code=None, error="timeout"))
        self.assertEqual(
            (result["persisted_status"], result["observation"], result["reason"]),
            ("active", "inconclusive", "linkedin_connection_failure"),
        )

    def test_expired_row_with_timeout_stays_expired_and_is_inconclusive(self) -> None:
        result = audit("naukri", "expired", naukri_probe(status_code=None, error="timeout"))
        self.assertEqual(
            (result["persisted_status"], result["observation"], result["reason"]),
            ("expired", "inconclusive", "naukri_connection_failure"),
        )

    def test_active_row_behind_captcha_is_inconclusive(self) -> None:
        result = audit("linkedin", "active", linkedin_probe(body="<html>Please complete this captcha</html>"))
        self.assertEqual((result["persisted_status"], result["observation"]), ("active", "inconclusive"))
        self.assertEqual(result["reason"], "linkedin_blocked")

    def test_active_row_with_provider_errors_is_inconclusive(self) -> None:
        for status in (403, 429, 500, 503):
            result = audit("linkedin", "active", linkedin_probe(status_code=status))
            self.assertEqual((result["observation"], result["reason"]), ("inconclusive", f"linkedin_http_{status}"))
        result = audit("naukri", "active", naukri_probe(status_code=500))
        self.assertEqual((result["observation"], result["reason"]), ("inconclusive", "naukri_http_500"))

    def test_parse_failure_redirect_and_probe_error_are_inconclusive(self) -> None:
        self.assertEqual(audit("naukri", "active", naukri_probe(payload=["unexpected"]))["reason"], "naukri_malformed")
        redirected = audit_job_ready.audit_row(
            row("CQJ-1", "linkedin", "active"),
            probes={
                "linkedin": lambda _row: (
                    *interpret_linkedin(
                        status_code=200, body="", expected_job_id=LINKEDIN_ID, final_url="https://www.linkedin.com/login"
                    ),
                    "linkedin",
                )
            },
        )
        self.assertEqual((redirected["observation"], redirected["reason"]), ("inconclusive", "linkedin_redirected"))

        def broken(_row):
            raise ValueError("unexpected markup")

        crashed = audit("linkedin", "active", broken)
        self.assertEqual((crashed["observation"], crashed["reason"]), ("inconclusive", "linkedin_probe_error"))

    def test_active_row_with_confirmed_open_evidence_is_confirmed_active(self) -> None:
        payload = {"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}}
        result = audit("naukri", "active", naukri_probe(payload=payload))
        self.assertEqual(
            (result["persisted_status"], result["observation"], result["reason"], result["provider"]),
            ("active", "confirmed_active", "naukri_explicit_open", "naukri"),
        )

    def test_active_row_with_confirmed_closed_evidence_is_confirmed_expired(self) -> None:
        payload = {"job": {"jobId": NAUKRI_ID, "jobStatus": "expired"}}
        result = audit("naukri", "active", naukri_probe(payload=payload))
        self.assertEqual(
            (result["persisted_status"], result["observation"], result["reason"]),
            ("active", "confirmed_expired", "naukri_explicit_closed"),
        )

    def test_expired_row_with_confirmed_open_evidence_keeps_its_stored_status(self) -> None:
        payload = {"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}}
        result = audit("naukri", "expired", naukri_probe(payload=payload))
        self.assertEqual((result["persisted_status"], result["observation"]), ("expired", "confirmed_active"))

    def test_generic_providers_are_inconclusive_without_network(self) -> None:
        for source in ("foundit", "google", "", None):
            result = audit_job_ready.audit_row(row("CQJ-1", source, "active"))
            self.assertEqual(result["observation"], "inconclusive", source)
            self.assertTrue(result["reason"].endswith("_no_explicit_closure"), result["reason"])

    def test_missing_indeed_key_is_inconclusive_without_network(self) -> None:
        with mock.patch.dict(os.environ, {"INDEED_API_KEY": ""}):
            result = audit_job_ready.audit_row(row("CQJ-1", "indeed", "active", "https://in.indeed.com/viewjob?jk=abc"))
        self.assertEqual((result["observation"], result["reason"]), ("inconclusive", "indeed_missing_api_key"))

    def test_unknown_probe_labels_never_become_confirmed_active(self) -> None:
        for label in ("active", "unknown", "link_failed", "", None):
            result = audit("linkedin", "active", lambda _row, label=label: (label, "legacy_label", "linkedin"))
            self.assertEqual(result["observation"], "inconclusive", repr(label))

    def test_null_stored_status_is_reported_as_stored(self) -> None:
        result = audit("linkedin", None, linkedin_probe(status_code=429))
        self.assertIsNone(result["persisted_status"])
        self.assertEqual(result["observation"], "inconclusive")


class SummaryTests(unittest.TestCase):
    def test_persisted_counts_are_separate_from_observation_counts(self) -> None:
        results = [
            {"job_id": "1", "source": "linkedin", "persisted_status": "active", "observation": "inconclusive", "reason": "linkedin_http_429", "provider": "linkedin"},
            {"job_id": "2", "source": "linkedin", "persisted_status": "active", "observation": "inconclusive", "reason": "linkedin_http_429", "provider": "linkedin"},
            {"job_id": "3", "source": "naukri", "persisted_status": "active", "observation": "confirmed_active", "reason": "naukri_explicit_open", "provider": "naukri"},
            {"job_id": "4", "source": "naukri", "persisted_status": "expired", "observation": "inconclusive", "reason": "naukri_empty_detail", "provider": "naukri"},
            {"job_id": "5", "source": "naukri", "persisted_status": "expired", "observation": "confirmed_expired", "reason": "naukri_explicit_closed", "provider": "naukri"},
            {"job_id": "6", "source": "foundit", "persisted_status": None, "observation": "inconclusive", "reason": "foundit_no_explicit_closure", "provider": "foundit"},
        ]
        summary = audit_job_ready.summarize(results)
        self.assertEqual(
            {key: summary[key] for key in ("checked", "confirmed_active", "confirmed_expired", "inconclusive")},
            {"checked": 6, "confirmed_active": 1, "confirmed_expired": 1, "inconclusive": 4},
        )
        self.assertEqual(
            {key: summary[key] for key in ("persisted_active", "persisted_expired", "persisted_other")},
            {"persisted_active": 3, "persisted_expired": 2, "persisted_other": 1},
        )
        self.assertNotIn("active", summary)
        self.assertNotIn("expired", summary)
        self.assertEqual(
            summary["persisted_by_observation"],
            {
                "None": {"inconclusive": 1},
                "active": {"confirmed_active": 1, "inconclusive": 2},
                "expired": {"confirmed_expired": 1, "inconclusive": 1},
            },
        )
        self.assertEqual(
            summary["validation_reasons"],
            {
                "confirmed_active:naukri_explicit_open": 1,
                "confirmed_expired:naukri_explicit_closed": 1,
                "inconclusive:foundit_no_explicit_closure": 1,
                "inconclusive:linkedin_http_429": 2,
                "inconclusive:naukri_empty_detail": 1,
            },
        )


class FakeCursor:
    def __init__(self, statements: list[str], rows: list[tuple]) -> None:
        self.statements = statements
        self.rows = rows

    def execute(self, sql, params=None) -> None:
        self.statements.append(" ".join(str(sql).split()))

    def fetchall(self) -> list[tuple]:
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class FakeConnection:
    def __init__(self, rows: list[tuple]) -> None:
        self.statements: list[str] = []
        self.session: dict = {}
        self.rows = rows
        self.closed = False
        self.committed = False

    def set_session(self, **kwargs) -> None:
        self.session.update(kwargs)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.statements, self.rows)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


class ReadOnlyAuditTests(unittest.TestCase):
    STORED = [
        ("CQJ-A", "naukri", "https://www.naukri.com/job-listings-a-051026501163", None, "active"),
        ("CQJ-E", "naukri", "https://www.naukri.com/job-listings-e-051026501164", None, "expired"),
        ("CQJ-N", "linkedin", GUEST, None, None),
    ]

    def test_fetch_reads_stored_status_in_a_read_only_session(self) -> None:
        fake = FakeConnection(self.STORED)
        with mock.patch.dict(os.environ, {"JOB_READY_DATABASE_URL": "postgresql://fixture.invalid/catalog"}), mock.patch.object(
            audit_job_ready.psycopg2, "connect", return_value=fake
        ):
            rows = audit_job_ready.fetch_job_ready_rows()
        self.assertEqual({item["job_id"]: item["link_status"] for item in rows}, {"CQJ-A": "active", "CQJ-E": "expired", "CQJ-N": None})
        self.assertEqual(fake.session, {"readonly": True, "autocommit": True})
        self.assertEqual(len(fake.statements), 1)
        self.assertTrue(fake.statements[0].startswith("SELECT "), fake.statements[0])
        for verb in ("UPDATE", "DELETE", "INSERT"):
            self.assertNotIn(verb, fake.statements[0].upper())
        self.assertFalse(fake.committed)
        self.assertTrue(fake.closed)

    def run_audit(self, argv: list[str], rows: list[dict], probe) -> tuple[dict, str]:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(audit_job_ready, "ROOT", Path(tmp)), mock.patch.object(
            audit_job_ready, "load_env"
        ), mock.patch.object(audit_job_ready, "fetch_job_ready_rows", return_value=rows), mock.patch.object(
            audit_job_ready, "default_probes", return_value={"naukri": probe, "linkedin": probe}
        ), mock.patch.object(
            sync_job_ready_ui, "assert_application_write_target", side_effect=AssertionError("no application target expected")
        ), mock.patch.object(
            sync_job_ready_ui, "archive_ui_jobs_not_in_catalog", side_effect=AssertionError("no membership write expected")
        ), mock.patch.object(
            audit_job_ready.psycopg2, "connect", side_effect=AssertionError("no database connection expected")
        ), redirect_stdout(stdout):
            code = audit_job_ready.main(argv)
            self.assertEqual(code, 0)
            report_path = next((Path(tmp) / "output").glob("job_ready_audit_*.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
        return report, stdout.getvalue()

    def stored_rows(self) -> list[dict]:
        return [dict(zip(audit_job_ready.ROW_COLUMNS, raw)) for raw in self.STORED]

    def test_inconclusive_rows_are_never_reported_as_confirmed_active(self) -> None:
        report, output = self.run_audit([], self.stored_rows(), linkedin_probe(status_code=429))
        self.assertEqual(report["confirmed_active"], 0)
        self.assertEqual(report["inconclusive"], 3)
        self.assertEqual((report["persisted_active"], report["persisted_expired"], report["persisted_other"]), (1, 1, 1))
        self.assertEqual({item["observation"] for item in report["results"]}, {"inconclusive"})
        self.assertEqual(
            {item["job_id"]: item["persisted_status"] for item in report["results"]},
            {"CQJ-A": "active", "CQJ-E": "expired", "CQJ-N": None},
        )
        self.assertEqual(
            set(report["results"][0]), {"job_id", "source", "persisted_status", "observation", "reason", "provider"}
        )
        self.assertIn("will not be modified", output)
        self.assertIn('"confirmed_active": 0', output)

    def test_read_only_audit_does_not_write_confirmed_results(self) -> None:
        closed = naukri_probe(payload={"job": {"jobId": NAUKRI_ID, "jobStatus": "expired"}})
        report, _output = self.run_audit([], self.stored_rows(), closed)
        self.assertEqual(report["confirmed_expired"], 3)
        self.assertNotIn("removed", report)
        self.assertNotIn("prune", report)

    def test_expired_row_confirmed_open_is_reported_but_not_reactivated(self) -> None:
        opened = naukri_probe(payload={"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}})
        report, _output = self.run_audit([], self.stored_rows(), opened)
        expired_row = next(item for item in report["results"] if item["job_id"] == "CQJ-E")
        self.assertEqual((expired_row["persisted_status"], expired_row["observation"]), ("expired", "confirmed_active"))
        self.assertEqual(report["persisted_by_observation"]["expired"], {"confirmed_active": 1})

    def test_retired_prune_option_is_refused_before_reading_the_catalog(self) -> None:
        for argv in (
            ["--prune-job-ready", "--confirm-target", "app.invalid:5432/railway"],
            ["--prune-job-ready"],
            ["--confirm-target", "app.invalid:5432/railway"],
        ):
            stdout = io.StringIO()
            with mock.patch.object(audit_job_ready, "load_env", side_effect=AssertionError("load_env")), mock.patch.object(
                audit_job_ready, "fetch_job_ready_rows", side_effect=AssertionError("catalog read")
            ), mock.patch.object(
                audit_job_ready, "default_probes", side_effect=AssertionError("network probes")
            ), redirect_stdout(stdout):
                code = audit_job_ready.main(argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(stdout.getvalue().splitlines()[0], "JOBREADY_SYNC_RETIRED", argv)
            self.assertNotIn("app.invalid", stdout.getvalue(), argv)


if __name__ == "__main__":
    unittest.main()
