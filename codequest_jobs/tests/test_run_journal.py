"""Local run journal for the canonical daily command (OPS-02).

Stage-boundary failures run against injected mocks. Commit and rollback
cases also run against disposable Postgres named by JOBREADY_DAILY_CATALOG_URL
and JOBREADY_DAILY_APP_URL; those are skipped when the variables are absent.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
SCRAPER = REPO / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

import psycopg2  # noqa: E402

import jobready_catalog as catalog  # noqa: E402
import run_jobready_daily as daily  # noqa: E402
import run_journal  # noqa: E402
from normalize import UnifiedJob  # noqa: E402
from run_all_sources import ScrapeResult  # noqa: E402

CATALOG_URL = os.environ.get("JOBREADY_DAILY_CATALOG_URL", "").strip()
APP_URL = os.environ.get("JOBREADY_DAILY_APP_URL", "").strip()

SENTINEL_PASSWORD = "S3ntinel-Pw-OPS02"
SENTINEL_DSN = f"postgresql://jobready:{SENTINEL_PASSWORD}@db.sentinel.invalid:5432/railway"
SENTINEL_API_KEY = "sk-sentinel-ops02-abcdef123456"
SENTINEL_TOKEN = "tok-sentinel-ops02-xyz987654"
SENTINELS = (SENTINEL_PASSWORD, SENTINEL_DSN, SENTINEL_API_KEY, SENTINEL_TOKEN)
FORBIDDEN = ("postgresql://", "postgres://", "password=")
RUN_ID = re.compile(r"^\d{8}T\d{6}Z_[0-9a-z]{5,7}_[0-9a-f]{6}$")


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


def _scrape(urls: list[str], *, source_failed: bool = False) -> ScrapeResult:
    health = {
        "naukri": {
            "attempted": True,
            "succeeded": not source_failed,
            "failed": source_failed,
            "jobs_returned": len(urls),
            "errors": [],
        }
    }
    return ScrapeResult(
        jobs=[_job(url) for url in urls],
        per_source={"naukri": len(urls)},
        per_family={"python-dev": len(urls)},
        errors=[],
        sources_attempted=["naukri"],
        started_at="2026-10-12T02:15:00+00:00",
        finished_at="2026-10-12T02:15:01+00:00",
        source_health=health,
    )


def _only_journal(directory: Path) -> tuple[Path, dict]:
    files = sorted(directory.glob("*.json"))
    if len(files) != 1:
        raise AssertionError(f"expected one journal, found {len(files)}")
    return files[0], json.loads(files[0].read_text(encoding="utf-8"))


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def rollback(self) -> None:
        return None

    def commit(self) -> None:
        return None


class _FakeLock:
    def __init__(self, harness: "MockedRunTests") -> None:
        self.harness = harness
        self.released = False

    def acquire(self) -> None:
        self.harness.hook("lock")

    def release(self) -> None:
        self.released = True


class MockedRunTests(unittest.TestCase):
    """execute_daily with every database and network boundary injected."""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.runs = self.base / "runs"
        self.reports = self.base / "reports"
        self.fail_at: str | None = None
        self.failure: BaseException = RuntimeError("forced failure")
        self.source_failed = False
        self.events: list[tuple[str, dict | None]] = []
        self.writes: list[str] = []
        self.lock = _FakeLock(self)
        real_replace = os.replace

        def spy_replace(src, dst):
            text = Path(src).read_text(encoding="utf-8")
            json.loads(text)
            self.writes.append(text)
            real_replace(src, dst)

        patches = {
            "run_jobready_daily.OUTPUT_DIR": self.reports,
            "run_jobready_daily.RUNS_DIR": self.runs,
            "run_jobready_daily.DailyRunLock": lambda: self.lock,
            "run_jobready_daily._open_catalog": _FakeConn,
            "run_jobready_daily.schema_problems": lambda conn: [],
            "run_jobready_daily.release_read_transaction": lambda conn: None,
            "run_jobready_daily.upsert_jobs": self.upsert,
            "run_jobready_daily._priority_ids": lambda conn, upserted: list(upserted["new_ids"]),
            "run_jobready_daily.select_validation_candidates": self.select,
            "run_jobready_daily.validate_candidates": self.probe,
            "run_jobready_daily.apply_observations": self.apply,
            "run_jobready_daily.catalog_counts": lambda conn: {"total": 2, "active": 2, "expired": 0},
            "run_jobready_daily.membership_would_change": self.membership,
            "sync_job_ready_ui._app_url": lambda: "unused",
            "sync_job_ready_ui._connect": lambda url: _FakeConn(),
            "sync_job_ready_ui.archive_ui_jobs_not_in_catalog": self.archive,
            "run_journal.current_git_commit": lambda: "a" * 40,
            "run_journal.os.replace": spy_replace,
        }
        for target, value in patches.items():
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def snapshot(self) -> dict | None:
        files = sorted(self.runs.glob("*.json")) if self.runs.is_dir() else []
        return json.loads(files[-1].read_text(encoding="utf-8")) if files else None

    def hook(self, name: str) -> None:
        self.events.append((name, self.snapshot()))
        if self.fail_at == name:
            raise self.failure

    def collect(self, **_kwargs) -> ScrapeResult:
        self.hook("scrape")
        return _scrape(["https://example.test/jobs/a", "https://example.test/jobs/b"], source_failed=self.source_failed)

    def upsert(self, conn, rows, *, on_plan_complete=None) -> dict:
        self.hook("catalog_plan")
        if on_plan_complete is not None:
            on_plan_complete()
        self.hook("catalog_write")
        return {
            "new": len(rows),
            "updated": 0,
            "unchanged": 0,
            "reobserved": 0,
            "new_ids": [f"CQJ-20261012-{index:04d}" for index in range(len(rows))],
            "reobserved_ids": [],
            "catalog_plan_sec": 0.01,
            "catalog_write_sec": 0.02,
        }

    def select(self, conn, **kwargs) -> list[dict]:
        self.hook("validation_select")
        return [{"job_id": job_id} for job_id in kwargs["priority_ids"]]

    def probe(self, candidates, probes=None) -> list[SimpleNamespace]:
        self.hook("validation_probe")
        return [
            SimpleNamespace(job_id=row["job_id"], observation="confirmed_active", reason="fixture_live")
            for row in candidates
        ]

    def apply(self, conn, built) -> dict:
        self.hook("validation_write")
        return {"checked": len(built), "confirmed_active": len(built), "confirmed_expired": 0, "inconclusive": 0}

    def membership(self, catalog_conn, app_conn) -> dict:
        self.hook("membership_count")
        return {"would_archive": 0, "would_reactivate": 0}

    def archive(self, *, confirm_target: str = "") -> dict:
        self.hook("application_sync")
        return {"archived": 0, "reactivated": 0}

    def run_command(self, *extra: str) -> dict:
        args = daily.parse_args(["--sources", "naukri", "--validation-limit", "5", *extra])
        return daily.execute_daily(args, collect=self.collect, probes={})

    def run_failing(self, stage: str, failure: BaseException, *extra: str) -> dict:
        self.fail_at = stage
        self.failure = failure
        with self.assertRaises(type(failure)):
            self.run_command(*extra)
        return _only_journal(self.runs)[1]

    def assert_failed_run(self, journal: dict, stage: str) -> None:
        self.assertEqual(journal["terminal_state"], "failed")
        self.assertEqual(journal["failed_stage"], stage)
        self.assertEqual(journal["stages"][stage]["status"], "failed")
        self.assertIsNotNone(journal["finished_at"])
        self.assertTrue(journal["stages"][stage]["failure_code"])
        self.assertIsNone(journal["report_path"])

    # 1, 10, 14
    def test_success_journal_exists_before_work_and_completes(self) -> None:
        report = self.run_command()
        path, journal = _only_journal(self.runs)
        lock_name, at_lock = self.events[0]
        self.assertEqual(lock_name, "lock")
        self.assertIsNotNone(at_lock, "journal must exist before the lock is taken")
        self.assertEqual(at_lock["terminal_state"], "running")
        self.assertEqual(at_lock["stages"]["preflight"]["status"], "running")
        self.assertEqual(at_lock["run_id"], journal["run_id"])
        at_scrape = dict(self.events)["scrape"]
        self.assertEqual(at_scrape["stages"]["preflight"]["status"], "completed")
        self.assertEqual(at_scrape["stages"]["scrape"]["status"], "running")
        at_write = dict(self.events)["catalog_write"]
        self.assertEqual(at_write["stages"]["catalog_plan"]["status"], "completed")
        self.assertEqual(at_write["stages"]["catalog_write"]["status"], "running")

        self.assertEqual(path.name, f"{journal['run_id']}.json")
        self.assertRegex(journal["run_id"], RUN_ID)
        self.assertEqual(journal["terminal_state"], "completed")
        self.assertEqual(journal["git_commit"], "a" * 40)
        self.assertEqual(journal["command"], "run_jobready_daily")
        self.assertEqual(journal["effective_mode"], "daily")
        self.assertEqual(journal["validation_limit"], 5)
        self.assertFalse(journal["full_audit"])
        self.assertFalse(journal["apply_schema"])
        self.assertFalse(journal["membership_sync_requested"])
        self.assertIsNotNone(journal["finished_at"])
        for name in run_journal.STAGES:
            self.assertIn(journal["stages"][name]["status"], ("completed", "skipped"), name)
        stages = journal["stages"]
        self.assertIsNone(stages["scrape"]["committed"])
        self.assertEqual(stages["scrape"]["counters"]["jobs"], 2)
        self.assertTrue(stages["catalog_write"]["committed"])
        self.assertEqual(stages["catalog_write"]["counters"]["new"], 2)
        self.assertIsNone(stages["validation_probe"]["committed"])
        self.assertTrue(stages["validation_write"]["committed"])
        self.assertEqual(stages["application_sync"]["status"], "skipped")
        self.assertEqual(stages["application_sync"]["skip_reason"], "not_requested")
        self.assertIsNone(stages["application_sync"]["committed"])
        self.assertEqual(journal["report_path"], report["report_path"])
        self.assertTrue(Path(journal["report_path"]).is_file())
        self.assertEqual(report["run_id"], journal["run_id"])
        self.assertTrue(self.lock.released)

    # 10
    def test_successful_report_path_recorded_on_report_stage(self) -> None:
        report = self.run_command()
        journal = _only_journal(self.runs)[1]
        self.assertEqual(journal["stages"]["report"]["status"], "completed")
        self.assertEqual(journal["stages"]["report"]["counters"]["report_path"], report["report_path"])
        self.assertEqual(journal["report_path"], report["report_path"])

    # 11
    def test_journal_is_valid_json_after_every_write(self) -> None:
        self.run_command()
        self.assertGreaterEqual(len(self.writes), 2 * len(run_journal.STAGES))
        for text in self.writes:
            data = json.loads(text)
            self.assertIn(data["terminal_state"], run_journal.TERMINAL_STATES)
            for record in data["stages"].values():
                self.assertIn(record["status"], run_journal.STAGE_STATES)
        self.assertEqual(json.loads(self.writes[0])["terminal_state"], "running")
        self.assertEqual(json.loads(self.writes[-1])["terminal_state"], "completed")

    # 2
    def test_scrape_failure(self) -> None:
        journal = self.run_failing("scrape", RuntimeError("source exploded"))
        self.assert_failed_run(journal, "scrape")
        self.assertEqual(journal["stages"]["scrape"]["failure_code"], "SCRAPE_FAILED")
        self.assertEqual(journal["stages"]["preflight"]["status"], "completed")
        self.assertEqual(journal["stages"]["catalog_plan"]["status"], "not_started")
        self.assertTrue(self.lock.released)

    # 3
    def test_catalog_plan_failure(self) -> None:
        journal = self.run_failing(
            "catalog_plan", SystemExit("CATALOG_WRITE_FAILED stage=catalog_plan catalog write failed")
        )
        self.assert_failed_run(journal, "catalog_plan")
        self.assertEqual(journal["stages"]["catalog_plan"]["failure_code"], "CATALOG_WRITE_FAILED")
        self.assertIsNone(journal["stages"]["catalog_plan"]["committed"])
        self.assertEqual(journal["stages"]["catalog_write"]["status"], "not_started")

    # 4
    def test_catalog_write_rollback_is_not_committed(self) -> None:
        journal = self.run_failing(
            "catalog_write", SystemExit("CATALOG_WRITE_FAILED stage=catalog_write lock_timeout")
        )
        self.assert_failed_run(journal, "catalog_write")
        write = journal["stages"]["catalog_write"]
        self.assertIs(write["committed"], False)
        self.assertEqual(write["failure_code"], "CATALOG_WRITE_FAILED")
        self.assertEqual(journal["stages"]["catalog_plan"]["status"], "completed")
        self.assertEqual(journal["stages"]["validation_select"]["status"], "not_started")

    # 5
    def test_failure_after_catalog_commit_keeps_commit_state(self) -> None:
        journal = self.run_failing("validation_select", RuntimeError("selection query failed"))
        self.assert_failed_run(journal, "validation_select")
        self.assertEqual(journal["stages"]["catalog_write"]["status"], "completed")
        self.assertIs(journal["stages"]["catalog_write"]["committed"], True)
        self.assertEqual(journal["stages"]["validation_write"]["status"], "not_started")

    # 6
    def test_validation_probe_failure_escaping_stage(self) -> None:
        journal = self.run_failing("validation_probe", RuntimeError("probe pool crashed"))
        self.assert_failed_run(journal, "validation_probe")
        self.assertIsNone(journal["stages"]["validation_probe"]["committed"])
        self.assertIs(journal["stages"]["catalog_write"]["committed"], True)
        self.assertEqual(journal["stages"]["validation_select"]["counters"]["selected"], 2)

    # 7
    def test_validation_write_failure_is_not_committed(self) -> None:
        journal = self.run_failing("validation_write", RuntimeError("update failed"))
        self.assert_failed_run(journal, "validation_write")
        self.assertIs(journal["stages"]["validation_write"]["committed"], False)
        self.assertEqual(journal["stages"]["validation_write"]["failure_code"], "VALIDATION_WRITE_FAILED")
        self.assertEqual(journal["stages"]["validation_probe"]["status"], "completed")

    # 8
    def test_membership_refusal_keeps_earlier_commits(self) -> None:
        self.source_failed = True
        with self.assertRaises(SystemExit):
            self.run_command("--sync-application", "--confirm-target", "app.invalid:5432/railway")
        journal = _only_journal(self.runs)[1]
        self.assert_failed_run(journal, "application_sync")
        sync = journal["stages"]["application_sync"]
        self.assertEqual(sync["failure_code"], "MEMBERSHIP_REFUSED")
        self.assertIs(sync["committed"], False)
        self.assertTrue(journal["membership_sync_requested"])
        self.assertTrue(journal["confirm_target_provided"])
        self.assertNotIn("app.invalid", json.dumps(journal))
        self.assertIs(journal["stages"]["catalog_write"]["committed"], True)
        self.assertIs(journal["stages"]["validation_write"]["committed"], True)
        self.assertNotIn("application_sync", [name for name, _ in self.events])
        self.assertEqual(journal["stages"]["report"]["status"], "not_started")

    # 8
    def test_membership_apply_failure(self) -> None:
        journal = self.run_failing(
            "application_sync",
            SystemExit("MEMBERSHIP_REFUSED archive would remove too many rows"),
            "--sync-application",
            "--confirm-target",
            "app.invalid:5432/railway",
        )
        self.assert_failed_run(journal, "application_sync")
        self.assertIs(journal["stages"]["application_sync"]["committed"], False)
        self.assertIs(journal["stages"]["catalog_write"]["committed"], True)

    def test_membership_apply_success_is_committed(self) -> None:
        self.run_command("--sync-application", "--confirm-target", "app.invalid:5432/railway")
        journal = _only_journal(self.runs)[1]
        self.assertEqual(journal["terminal_state"], "completed")
        self.assertEqual(journal["stages"]["application_sync"]["status"], "completed")
        self.assertIs(journal["stages"]["application_sync"]["committed"], True)

    def test_application_connect_failure(self) -> None:
        with patch("sync_job_ready_ui._connect", side_effect=SystemExit("UI_SYNC_FAILED")):
            with self.assertRaises(SystemExit):
                self.run_command("--sync-application", "--confirm-target", "app.invalid:5432/railway")
        journal = _only_journal(self.runs)[1]
        self.assert_failed_run(journal, "application_sync")
        self.assertEqual(journal["stages"]["application_sync"]["failure_code"], "APPLICATION_CONNECT_FAILED")
        self.assertEqual(journal["stages"]["catalog_plan"]["status"], "not_started")

    # 9
    def test_report_write_failure(self) -> None:
        with patch(
            "run_jobready_daily.write_report",
            side_effect=SystemExit("report refused because it contained a credential"),
        ):
            with self.assertRaises(SystemExit):
                self.run_command()
        journal = _only_journal(self.runs)[1]
        self.assert_failed_run(journal, "report")
        self.assertEqual(journal["stages"]["report"]["failure_code"], "REPORT_REFUSED")
        self.assertIs(journal["stages"]["validation_write"]["committed"], True)

    def test_lock_held_fails_preflight_and_leaves_other_journal_alone(self) -> None:
        other = run_journal.RunJournal.start(self.runs, run={"command": "run_jobready_daily"})
        other.begin("scrape")
        self.fail_at = "lock"
        self.failure = SystemExit("another JobReady daily run is active")
        with self.assertRaises(SystemExit):
            self.run_command()
        mine = [path for path in self.runs.glob("*.json") if path != other.path]
        self.assertEqual(len(mine), 1)
        journal = json.loads(mine[0].read_text(encoding="utf-8"))
        self.assert_failed_run(journal, "preflight")
        self.assertEqual(journal["stages"]["preflight"]["failure_code"], "RUN_LOCK_HELD")
        self.assertEqual(journal["stages"]["scrape"]["status"], "not_started")
        still_running = json.loads(other.path.read_text(encoding="utf-8"))
        self.assertEqual(still_running["terminal_state"], "running")

    def test_previous_running_journal_is_abandoned_after_lock(self) -> None:
        other = run_journal.RunJournal.start(self.runs, run={"command": "run_jobready_daily"})
        other.begin("catalog_write")
        done = run_journal.RunJournal.start(self.runs, run={"command": "run_jobready_daily"})
        for name in run_journal.STAGES:
            done.skip(name, "fixture")
        done.finish_completed(report_path=None)
        self.run_command()
        previous = json.loads(other.path.read_text(encoding="utf-8"))
        self.assertEqual(previous["terminal_state"], "abandoned")
        self.assertEqual(previous["stages"]["catalog_write"]["status"], "failed")
        self.assertEqual(previous["stages"]["catalog_write"]["failure_code"], "RUN_ABANDONED")
        self.assertIsNone(previous["stages"]["catalog_write"]["committed"])
        self.assertEqual(json.loads(done.path.read_text(encoding="utf-8"))["terminal_state"], "completed")
        current = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in self.runs.glob("*.json")
            if path not in (other.path, done.path)
        ]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["abandoned_previous"], [other.run_id])
        self.assertEqual(previous["abandoned_detected_by"], current[0]["run_id"])

    # 13
    def test_no_dsn_or_sentinel_credentials_in_journal(self) -> None:
        message = (
            f"connect to {SENTINEL_DSN} failed password={SENTINEL_PASSWORD} "
            f"Authorization: Bearer {SENTINEL_TOKEN} api_key={SENTINEL_API_KEY}"
        )
        env = {
            "JOB_READY_DATABASE_URL": SENTINEL_DSN,
            "JOB_READY_APP_DATABASE_URL": SENTINEL_DSN.replace("railway", "app"),
            "INDEED_API_KEY": SENTINEL_API_KEY,
        }
        with patch.dict(os.environ, env):
            self.run_failing("validation_write", RuntimeError(message))
        self.assertTrue(self.writes)
        for text in self.writes + [path.read_text(encoding="utf-8") for path in self.runs.glob("*.json")]:
            lowered = text.lower()
            for token in FORBIDDEN:
                self.assertNotIn(token, lowered)
            for secret in SENTINELS:
                self.assertNotIn(secret.lower(), lowered)
        journal = _only_journal(self.runs)[1]
        self.assertIn("[redacted", journal["stages"]["validation_write"]["failure_message"])

    # 15
    def test_main_returns_nonzero_on_failure(self) -> None:
        def failing_collect(**_kwargs):
            raise SystemExit("SCRAPE_ABORTED fixture")

        with patch("run_all_sources.collect_jobs", failing_collect):
            code = daily.main(["--sources", "naukri"])
        self.assertEqual(code, 1)
        journal = _only_journal(self.runs)[1]
        self.assert_failed_run(journal, "scrape")
        self.assertEqual(journal["failure_code"], "SCRAPE_ABORTED")

    # 15
    def test_main_returns_nonzero_on_catalog_write_failure(self) -> None:
        self.fail_at = "catalog_write"
        self.failure = SystemExit("CATALOG_WRITE_FAILED stage=catalog_write statement_timeout")
        with patch("run_all_sources.collect_jobs", self.collect):
            code = daily.main(["--sources", "naukri"])
        self.assertNotEqual(code, 0)
        self.assertEqual(_only_journal(self.runs)[1]["terminal_state"], "failed")

    def test_interrupt_is_journaled_and_propagates(self) -> None:
        journal = self.run_failing("validation_probe", KeyboardInterrupt())
        self.assert_failed_run(journal, "validation_probe")
        self.assertEqual(journal["failure_code"], "INTERRUPTED")


class JournalUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def test_run_ids_are_unique_and_sortable(self) -> None:
        ids = [
            run_journal.RunJournal(self.dir, run={}, git_commit="b" * 40).run_id
            for _ in range(50)
        ]
        self.assertEqual(len(set(ids)), 50)
        for run_id in ids:
            self.assertRegex(run_id, RUN_ID)
            self.assertIn("_bbbbbbb_", run_id)
        early = run_journal.new_run_id(datetime(2026, 10, 12, 2, 15, tzinfo=timezone.utc), None)
        late = run_journal.new_run_id(datetime(2026, 10, 12, 2, 16, tzinfo=timezone.utc), None)
        self.assertLess(early, late)
        self.assertTrue(early.startswith("20261012T021500Z_nogit_"))

    def test_start_persists_before_any_stage(self) -> None:
        journal = run_journal.RunJournal.start(self.dir, run={"command": "run_jobready_daily"}, git_commit="c" * 40)
        data = json.loads(journal.path.read_text(encoding="utf-8"))
        self.assertEqual(data["run_id"], journal.run_id)
        self.assertEqual(data["terminal_state"], "running")
        self.assertIsNotNone(data["started_at"])
        self.assertTrue(all(record["status"] == "not_started" for record in data["stages"].values()))

    # 12
    def test_replace_failure_keeps_previous_file_intact(self) -> None:
        path = self.dir / "run.json"
        run_journal.atomic_write_text(path, json.dumps({"version": 1}))
        with patch("run_journal.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                run_journal.atomic_write_text(path, json.dumps({"version": 2}))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 1})
        self.assertEqual(sorted(item.name for item in self.dir.iterdir()), ["run.json"])

    # 12
    def test_write_goes_through_same_directory_temp_and_replace(self) -> None:
        calls: list[tuple[Path, Path]] = []
        real_replace = os.replace
        fsyncs: list[int] = []
        real_fsync = os.fsync

        def spy_replace(src, dst):
            calls.append((Path(src), Path(dst)))
            real_replace(src, dst)

        def spy_fsync(fd):
            fsyncs.append(fd)
            real_fsync(fd)

        path = self.dir / "run.json"
        with patch("run_journal.os.replace", spy_replace), patch("run_journal.os.fsync", spy_fsync):
            run_journal.atomic_write_text(path, "{}")
        self.assertEqual(len(calls), 1)
        source, target = calls[0]
        self.assertEqual(target, path)
        self.assertEqual(source.parent, path.parent)
        self.assertTrue(source.name.endswith(".tmp"))
        self.assertGreaterEqual(len(fsyncs), 1)

    def test_finish_completed_refuses_unfinished_stages(self) -> None:
        journal = run_journal.RunJournal.start(self.dir, run={}, git_commit=None)
        journal.complete("preflight")
        with self.assertRaises(SystemExit):
            journal.finish_completed(report_path=None)
        data = json.loads(journal.path.read_text(encoding="utf-8"))
        self.assertEqual(data["terminal_state"], "failed")
        self.assertEqual(data["failure_code"], "RUN_INCOMPLETE")

    def test_stage_context_reraises_and_records(self) -> None:
        journal = run_journal.RunJournal.start(self.dir, run={}, git_commit=None)
        with self.assertRaises(ValueError):
            with journal.stage("catalog_write", committed_on_failure=False) as stage:
                stage.counters["rows"] = 3
                raise ValueError("bad row")
        record = json.loads(journal.path.read_text(encoding="utf-8"))["stages"]["catalog_write"]
        self.assertEqual(record["status"], "failed")
        self.assertIs(record["committed"], False)
        self.assertEqual(record["counters"], {"rows": 3})
        self.assertEqual(record["failure_code"], "CATALOG_WRITE_FAILED")
        self.assertEqual(record["failure_message"], "ValueError: bad row")
        self.assertIsNotNone(record["elapsed_sec"])

    def test_disabled_journal_never_writes(self) -> None:
        journal = run_journal.RunJournal.disabled()
        with patch("run_journal.atomic_write_text") as writer:
            journal.begin("catalog_plan")
            journal.complete("catalog_plan")
        writer.assert_not_called()
        self.assertIsNone(journal.path)

    def test_sanitize_redacts_credentials(self) -> None:
        cases = [
            SENTINEL_DSN,
            f"postgres://u:{SENTINEL_PASSWORD}@h/db",
            f"postgresql+psycopg2://u:{SENTINEL_PASSWORD}@h/db",
            f"password={SENTINEL_PASSWORD}",
            f"PASSWORD: {SENTINEL_PASSWORD}",
            f"Authorization: Bearer {SENTINEL_TOKEN}",
            f"authorization=Basic {SENTINEL_TOKEN}",
            f"api_key={SENTINEL_API_KEY}",
            f"https://api.example.test/v1?api-key={SENTINEL_API_KEY}&x=1",
            f"token={SENTINEL_TOKEN}",
            f"https://user:{SENTINEL_PASSWORD}@railway.example.test/path",
        ]
        for case in cases:
            text = run_journal.sanitize_text(f"failure: {case} end").lower()
            for token in FORBIDDEN:
                self.assertNotIn(token, text, case)
            for secret in SENTINELS:
                self.assertNotIn(secret.lower(), text, case)

    def test_sanitize_patterns_win_over_short_environment_secrets(self) -> None:
        with patch.dict(os.environ, {"POSTGRES_PASSWORD": "postgres"}):
            text = run_journal.sanitize_text(f"x postgres://u:{SENTINEL_PASSWORD}@h/db y").lower()
        self.assertNotIn(SENTINEL_PASSWORD.lower(), text)
        self.assertNotIn("://", text)

    def test_sanitize_redacts_environment_secret_values(self) -> None:
        with patch.dict(os.environ, {"RAILWAY_TOKEN": SENTINEL_TOKEN, "JOB_READY_APP_DATABASE_URL": SENTINEL_DSN}):
            text = run_journal.sanitize_text(f"x {SENTINEL_TOKEN} y {SENTINEL_PASSWORD} z")
        self.assertNotIn(SENTINEL_TOKEN, text)
        self.assertNotIn(SENTINEL_PASSWORD, text)

    def test_sanitize_keeps_safe_text_and_truncates(self) -> None:
        self.assertEqual(
            run_journal.sanitize_text("CATALOG_WRITE_FAILED stage=catalog_write lock_timeout"),
            "CATALOG_WRITE_FAILED stage=catalog_write lock_timeout",
        )
        self.assertEqual(len(run_journal.failure_message(RuntimeError("x" * 2000))), run_journal.MESSAGE_LIMIT)

    def test_failure_codes(self) -> None:
        code = run_journal.failure_code
        self.assertEqual(code(SystemExit("SCHEMA_REQUIRED table is missing"), "X_FAILED"), "SCHEMA_REQUIRED")
        self.assertEqual(code(SystemExit("membership sync refused: empty scrape"), "X_FAILED"), "MEMBERSHIP_REFUSED")
        self.assertEqual(code(SystemExit("another JobReady daily run is active"), "X_FAILED"), "RUN_LOCK_HELD")
        self.assertEqual(code(SystemExit("catalog target is unreachable"), "X_FAILED"), "CATALOG_UNREACHABLE")
        self.assertEqual(code(SystemExit(2), "X_FAILED"), "EXIT_2")
        self.assertEqual(code(RuntimeError("boom"), "X_FAILED"), "X_FAILED")
        self.assertEqual(code(KeyboardInterrupt(), "X_FAILED"), "INTERRUPTED")


@unittest.skipUnless(CATALOG_URL, "JOBREADY_DAILY_CATALOG_URL is not set")
class DatabaseJournalTests(unittest.TestCase):
    """Real commit and rollback against disposable Postgres."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.catalog = psycopg2.connect(CATALOG_URL, connect_timeout=8)
        except Exception:
            raise unittest.SkipTest("disposable database unavailable") from None
        catalog.ensure_schema(cls.catalog)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalog.close()

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.runs = self.base / "runs"
        self.catalog.rollback()
        with self.catalog.cursor() as cur:
            cur.execute("TRUNCATE validated_jobs RESTART IDENTITY")
        self.catalog.commit()
        for target, value in {
            "run_jobready_daily.OUTPUT_DIR": self.base / "reports",
            "run_jobready_daily.RUNS_DIR": self.runs,
        }.items():
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {"JOB_READY_DATABASE_URL": CATALOG_URL})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("JOB_READY_APP_DATABASE_URL", None)

    def _count(self, where: str = "TRUE") -> int:
        self.catalog.rollback()
        with self.catalog.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM validated_jobs WHERE {where}")
            return int(cur.fetchone()[0])

    def _run(self, *extra: str, urls: list[str] | None = None, source_failed: bool = False) -> dict:
        def collect(**_kwargs):
            return _scrape(urls or [f"https://example.test/jobs/journal-{uuid.uuid4().hex[:8]}"], source_failed=source_failed)

        def probe(row):
            return ("confirmed_active", "fixture_live", row.get("source") or "naukri")

        args = daily.parse_args(["--sources", "naukri", "--validation-limit", "5", *extra])
        return daily.execute_daily(args, collect=collect, probes={"naukri": probe, "generic": probe})

    def _journal(self) -> dict:
        return _only_journal(self.runs)[1]

    def _assert_clean(self) -> None:
        text = _only_journal(self.runs)[0].read_text(encoding="utf-8").lower()
        for token in FORBIDDEN:
            self.assertNotIn(token, text)
        self.assertNotIn(CATALOG_URL.lower(), text)

    def test_real_success(self) -> None:
        report = self._run()
        journal = self._journal()
        self.assertEqual(journal["terminal_state"], "completed")
        self.assertIs(journal["stages"]["catalog_write"]["committed"], True)
        self.assertIs(journal["stages"]["validation_write"]["committed"], True)
        self.assertEqual(journal["stages"]["application_sync"]["status"], "skipped")
        self.assertEqual(journal["report_path"], report["report_path"])
        self.assertEqual(self._count("link_checked_at IS NOT NULL"), 1)
        self._assert_clean()

    def test_real_catalog_write_rollback(self) -> None:
        with patch("jobready_catalog._write_resolved", side_effect=RuntimeError(f"boom {CATALOG_URL}")):
            with self.assertRaises(SystemExit):
                self._run()
        journal = self._journal()
        self.assertEqual(journal["terminal_state"], "failed")
        self.assertEqual(journal["stages"]["catalog_plan"]["status"], "completed")
        self.assertEqual(journal["stages"]["catalog_write"]["status"], "failed")
        self.assertIs(journal["stages"]["catalog_write"]["committed"], False)
        self.assertEqual(journal["stages"]["catalog_write"]["failure_code"], "CATALOG_WRITE_FAILED")
        self.assertEqual(self._count(), 0)
        self._assert_clean()

    def test_real_failure_after_catalog_commit(self) -> None:
        with patch("run_jobready_daily.validate_candidates", side_effect=RuntimeError("probe pool crashed")):
            with self.assertRaises(RuntimeError):
                self._run()
        journal = self._journal()
        self.assertEqual(journal["terminal_state"], "failed")
        self.assertEqual(journal["failed_stage"], "validation_probe")
        self.assertIs(journal["stages"]["catalog_write"]["committed"], True)
        self.assertEqual(self._count(), 1)
        self.assertEqual(self._count("link_checked_at IS NOT NULL"), 0)
        self._assert_clean()

    def test_real_validation_write_failure(self) -> None:
        with patch("run_jobready_daily.apply_link_observations", side_effect=RuntimeError("update failed")):
            with self.assertRaises(RuntimeError):
                self._run()
        journal = self._journal()
        self.assertEqual(journal["failed_stage"], "validation_write")
        self.assertIs(journal["stages"]["validation_write"]["committed"], False)
        self.assertIs(journal["stages"]["catalog_write"]["committed"], True)
        self.assertEqual(self._count("link_checked_at IS NOT NULL"), 0)

    @unittest.skipUnless(APP_URL, "JOBREADY_DAILY_APP_URL is not set")
    def test_real_membership_refusal(self) -> None:
        app = psycopg2.connect(APP_URL, connect_timeout=8)
        try:
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
            app.commit()
        finally:
            app.close()
        with patch.dict(os.environ, {"JOB_READY_APP_DATABASE_URL": APP_URL}):
            with self.assertRaises(SystemExit):
                self._run("--sync-application", "--confirm-target", "unused.invalid:5432/app", source_failed=True)
        journal = self._journal()
        self.assertEqual(journal["failed_stage"], "application_sync")
        self.assertEqual(journal["stages"]["application_sync"]["failure_code"], "MEMBERSHIP_REFUSED")
        self.assertIs(journal["stages"]["application_sync"]["committed"], False)
        self.assertIs(journal["stages"]["catalog_write"]["committed"], True)
        self.assertIs(journal["stages"]["validation_write"]["committed"], True)
        self.assertEqual(self._count(), 1)
        text = _only_journal(self.runs)[0].read_text(encoding="utf-8").lower()
        self.assertNotIn(APP_URL.lower(), text)


if __name__ == "__main__":
    unittest.main()
