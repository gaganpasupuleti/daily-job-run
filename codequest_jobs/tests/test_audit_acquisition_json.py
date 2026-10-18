"""Offline tests for acquisition JSON audit mode in audit_job_ready.py."""

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
from jobready_validator import interpret_naukri  # noqa: E402
from normalize import source_job_id  # noqa: E402

NAUKRI_ID = "051026501163"
NAUKRI_URL = f"https://www.naukri.com/job-listings-test-{NAUKRI_ID}"
NAUKRI_DETAIL = f"https://www.naukri.com/jobapi/v1/job/{NAUKRI_ID}"


def naukri_probe(*, status_code=200, payload=None, error=None):
    def probe(_row):
        return (
            *interpret_naukri(
                status_code=status_code, payload=payload, expected_job_id=NAUKRI_ID, final_url=NAUKRI_DETAIL, error=error
            ),
            "naukri",
        )

    return probe


def artifact(*jobs: dict) -> dict:
    return {"meta": {"source": "fixture"}, "jobs": list(jobs)}


def job_row(**fields):
    base = {
        "source": "naukri",
        "job_url": NAUKRI_URL,
        "title": "Python Dev",
        "company": "Acme",
        "role_family": "python-dev",
        "role_family_name": "Python Developer",
        "search_keyword": "python developer",
    }
    base.update(fields)
    return base


class AcquisitionJsonAuditTests(unittest.TestCase):
    def write_artifact(self, tmp: Path, payload: dict) -> Path:
        path = tmp / "acquisition.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def run_json_audit(self, path: Path, argv_extra: list[str] | None = None, probe=None) -> dict:
        argv = ["--input-json", str(path)]
        if argv_extra:
            argv.extend(argv_extra)
        probes = {"naukri": probe or naukri_probe(), "indeed": probe or naukri_probe(), "linkedin": probe or naukri_probe()}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(audit_job_ready, "ROOT", Path(tmp)), mock.patch.object(
            audit_job_ready, "default_probes", return_value=probes
        ), mock.patch.object(
            audit_job_ready.psycopg2, "connect", side_effect=AssertionError("no database connection expected")
        ), redirect_stdout(io.StringIO()):
            code = audit_job_ready.main(argv)
            self.assertEqual(code, 0)
            report_path = next((Path(tmp) / "output").glob("acquisition_audit_*.json"))
            return json.loads(report_path.read_text(encoding="utf-8"))

    def test_valid_acquisition_artifact_loads_without_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row()))
            report = self.run_json_audit(path)
        self.assertEqual(report["mode"], "input_json")
        self.assertEqual(report["queued"], 1)

    def test_input_json_never_calls_psycopg2_connect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row()))
            with mock.patch.object(audit_job_ready.psycopg2, "connect", side_effect=AssertionError("connect")):
                self.run_json_audit(path)

    def test_missing_job_ready_database_url_does_not_block_json_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            path = self.write_artifact(Path(tmp), artifact(job_row()))
            report = self.run_json_audit(path)
        self.assertEqual(report["checked"], 1)

    def test_source_job_id_from_source_and_job_url(self) -> None:
        url = NAUKRI_URL
        expected = source_job_id("naukri", url)
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row(job_url=url)))
            report = self.run_json_audit(path)
        self.assertEqual(report["results"][0]["job_id"], expected)

    def test_metadata_fields_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row()))
            report = self.run_json_audit(path)
        row = report["results"][0]
        self.assertEqual(row["title"], "Python Dev")
        self.assertEqual(row["company"], "Acme")
        self.assertEqual(row["role_family"], "python-dev")
        self.assertEqual(row["search_keyword"], "python developer")

    def test_persisted_status_none_in_json_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row()))
            report = self.run_json_audit(path)
        self.assertIsNone(report["results"][0]["persisted_status"])

    def test_naukri_confirmed_active_counted(self) -> None:
        active = naukri_probe(payload={"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}})
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row()))
            report = self.run_json_audit(path, probe=active)
        self.assertEqual(report["confirmed_active"], 1)

    def test_naukri_confirmed_expired_counted(self) -> None:
        expired = naukri_probe(payload={"job": {"jobId": NAUKRI_ID, "jobStatus": "expired"}})
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row()))
            report = self.run_json_audit(path, probe=expired)
        self.assertEqual(report["confirmed_expired"], 1)

    def test_inconclusive_counted(self) -> None:
        inconclusive = naukri_probe(status_code=429)
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row()))
            report = self.run_json_audit(path, probe=inconclusive)
        self.assertEqual(report["inconclusive"], 1)

    def test_google_unsupported_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row(source="google", job_url="https://www.google.com/search?q=jobs")))
            report = self.run_json_audit(path)
        self.assertEqual(report["unsupported"], 1)
        self.assertEqual(report["results"][0]["reason"], "unsupported_source")

    def test_foundit_unsupported_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(
                Path(tmp), artifact(job_row(source="foundit", job_url="https://www.foundit.in/job/123"))
            )
            report = self.run_json_audit(path)
        self.assertEqual(report["unsupported"], 1)

    def test_missing_indeed_key_inconclusive(self) -> None:
        from jobready_validator import default_probes as real_default_probes

        indeed_url = "https://in.indeed.com/viewjob?jk=abc123"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True):
            path = self.write_artifact(Path(tmp), artifact(job_row(source="indeed", job_url=indeed_url)))
            argv = ["--input-json", str(path)]
            with mock.patch.object(audit_job_ready, "ROOT", Path(tmp)), mock.patch.object(
                audit_job_ready, "default_probes", real_default_probes
            ), mock.patch.object(
                audit_job_ready.psycopg2, "connect", side_effect=AssertionError("no database connection expected")
            ), redirect_stdout(io.StringIO()):
                audit_job_ready.main(argv)
            report_path = next((Path(tmp) / "output").glob("acquisition_audit_*.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
        row = report["results"][0]
        self.assertEqual((row["observation"], row["reason"]), ("inconclusive", "indeed_missing_api_key"))
        self.assertIn("indeed_missing_api_key", report["validation_reasons_by_source"]["indeed"])

    def test_by_source_queued_and_observation(self) -> None:
        active = naukri_probe(payload={"job": {"jobId": NAUKRI_ID, "jobStatus": "active"}})
        payload = artifact(
            job_row(source="naukri"),
            job_row(source="google", job_url="https://www.google.com/search?q=x"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), payload)
            report = self.run_json_audit(path, probe=active)
        self.assertEqual(report["by_source_queued"], {"google": 1, "naukri": 1})
        self.assertEqual(report["by_source_observation"]["naukri"]["confirmed_active"], 1)
        self.assertEqual(report["by_source_observation"]["google"]["unsupported"], 1)

    def test_by_role_family_summaries(self) -> None:
        payload = artifact(job_row(role_family="python-dev"), job_row(role_family=""))
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), payload)
            report = self.run_json_audit(path)
        self.assertEqual(report["by_role_family_queued"], {"python-dev": 1, audit_job_ready.UNKNOWN_LABEL: 1})
        self.assertIn("python-dev", report["by_role_family_observation"])

    def test_by_search_keyword_summaries(self) -> None:
        payload = artifact(job_row(search_keyword="alpha"), job_row(search_keyword="beta"))
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), payload)
            report = self.run_json_audit(path)
        self.assertEqual(report["by_search_keyword_queued"], {"alpha": 1, "beta": 1})
        self.assertEqual(len(report["by_search_keyword_observation"]), 2)

    def test_validation_reasons_by_source(self) -> None:
        inconclusive = naukri_probe(status_code=503)
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row()))
            report = self.run_json_audit(path, probe=inconclusive)
        self.assertEqual(report["validation_reasons_by_source"]["naukri"]["naukri_http_503"], 1)

    def test_malformed_top_level_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps([]), encoding="utf-8")
            with self.assertRaises(SystemExit):
                audit_job_ready.load_acquisition_jobs(path)

    def test_missing_jobs_list_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), {"meta": {}})
            with self.assertRaises(SystemExit):
                audit_job_ready.load_acquisition_jobs(path)

    def test_malformed_job_row_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), {"jobs": ["not-an-object"]})
            with self.assertRaises(SystemExit):
                audit_job_ready.load_acquisition_jobs(path)

    def test_limit_deterministic(self) -> None:
        payload = artifact(job_row(job_url=f"{NAUKRI_URL}-a"), job_row(job_url=f"{NAUKRI_URL}-b"))
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), payload)
            report = self.run_json_audit(path, argv_extra=["--limit", "1"])
        self.assertEqual(report["limit_requested"], 1)
        self.assertEqual(report["queued"], 1)

    def test_invalid_limit_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row()))
            with self.assertRaises(SystemExit):
                audit_job_ready.main(["--input-json", str(path), "--limit", "0"])

    def test_retired_flags_refused_before_input_reading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_artifact(Path(tmp), artifact(job_row()))
            with mock.patch.object(audit_job_ready, "load_acquisition_jobs", side_effect=AssertionError("read")):
                code = audit_job_ready.main(["--input-json", str(path), "--prune-job-ready"])
            self.assertEqual(code, audit_job_ready.EXIT_RETIRED)


class DbAuditRegressionTests(unittest.TestCase):
    def test_db_audit_still_read_only(self) -> None:
        rows = [
            {
                "job_id": "CQJ-1",
                "source": "naukri",
                "job_url": NAUKRI_URL,
                "apply_url": None,
                "link_status": "active",
            }
        ]
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(audit_job_ready, "ROOT", Path(tmp)), mock.patch.object(
            audit_job_ready, "load_env"
        ), mock.patch.object(audit_job_ready, "fetch_job_ready_rows", return_value=rows), mock.patch.object(
            audit_job_ready, "default_probes", return_value={"naukri": naukri_probe()}
        ), mock.patch.object(
            audit_job_ready.psycopg2, "connect", side_effect=AssertionError("no database connection expected")
        ), redirect_stdout(stdout):
            code = audit_job_ready.main([])
            self.assertEqual(code, 0)
            report_path = next((Path(tmp) / "output").glob("job_ready_audit_*.json"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertNotIn("mode", report)
        self.assertEqual(set(report["results"][0]), {"job_id", "source", "persisted_status", "observation", "reason", "provider"})


if __name__ == "__main__":
    unittest.main()