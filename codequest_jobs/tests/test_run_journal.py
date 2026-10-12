"""Local run journal for the canonical daily command (OPS-02).

Stage-boundary failures run against injected mocks. Commit and rollback
cases also run against disposable Postgres named by JOBREADY_DAILY_CATALOG_URL
and JOBREADY_DAILY_APP_URL; those are skipped when the variables are absent.
"""

from __future__ import annotations

import builtins
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


class _CommitFailingConnection:
    """A real connection whose commit fails without committing."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.commit_attempts = 0

    def commit(self) -> None:
        self.commit_attempts += 1
        raise psycopg2.OperationalError("server closed the connection during commit")

    def __getattr__(self, name):
        return getattr(self._inner, name)


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
        self.committed = False
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

    def upsert(self, conn, rows, *, on_plan_complete=None, on_commit=None) -> dict:
        self.hook("catalog_plan")
        if on_plan_complete is not None:
            on_plan_complete()
        self.hook("catalog_write")
        self.committed = True
        if on_commit is not None:
            on_commit()
        self.hook("catalog_post_commit")
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

    def _lock_owner_journal(self, stage: str = "catalog_write") -> run_journal.RunJournal:
        """A previous run that took the lock, then died while a stage was running."""
        owner = run_journal.RunJournal.start(self.runs, run={"command": "run_jobready_daily"})
        owner.begin("preflight")
        owner.record_lock_acquired()
        owner.complete("preflight")
        owner.begin(stage)
        return owner

    def _pending_journal(self) -> run_journal.RunJournal:
        """A live contender: journal created, preflight running, lock not yet acquired."""
        pending = run_journal.RunJournal.start(self.runs, run={"command": "run_jobready_daily"})
        pending.begin("preflight")
        return pending

    def _current_journal(self, *others: run_journal.RunJournal) -> dict:
        excluded = {other.path for other in others}
        current = [path for path in self.runs.glob("*.json") if path not in excluded]
        self.assertEqual(len(current), 1)
        return json.loads(current[0].read_text(encoding="utf-8"))

    # OPS-02 follow-up 3
    def test_lock_held_fails_preflight_and_modifies_no_other_journal(self) -> None:
        owner = self._lock_owner_journal()
        pending = self._pending_journal()
        before = {path: path.read_bytes() for path in (owner.path, pending.path)}
        self.fail_at = "lock"
        self.failure = SystemExit("another JobReady daily run is active")
        with self.assertRaises(SystemExit):
            self.run_command()
        journal = self._current_journal(owner, pending)
        self.assert_failed_run(journal, "preflight")
        self.assertEqual(journal["failure_code"], "RUN_LOCK_HELD")
        self.assertEqual(journal["stages"]["preflight"]["failure_code"], "RUN_LOCK_HELD")
        self.assertIsNone(journal["lock_acquired_at"])
        self.assertEqual(journal["abandoned_previous"], [])
        self.assertEqual(journal["stages"]["scrape"]["status"], "not_started")
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content, path.name)

    # OPS-02 follow-up 1
    def test_previous_lock_owner_is_abandoned_after_lock(self) -> None:
        owner = self._lock_owner_journal()
        done = run_journal.RunJournal.start(self.runs, run={"command": "run_jobready_daily"})
        for name in run_journal.STAGES:
            done.skip(name, "fixture")
        done.finish_completed(report_path=None)
        done_before = done.path.read_bytes()
        self.writes.clear()
        self.run_command()
        previous = json.loads(owner.path.read_text(encoding="utf-8"))
        self.assertEqual(previous["terminal_state"], "abandoned")
        self.assertIsNotNone(previous["lock_acquired_at"])
        self.assertEqual(previous["stages"]["catalog_write"]["status"], "failed")
        self.assertEqual(previous["stages"]["catalog_write"]["failure_code"], "RUN_ABANDONED")
        self.assertIsNone(previous["stages"]["catalog_write"]["committed"])
        self.assertEqual(previous["failed_stage"], "catalog_write")
        self.assertEqual(done.path.read_bytes(), done_before)
        current = self._current_journal(owner, done)
        self.assertEqual(current["abandoned_previous"], [owner.run_id])
        self.assertEqual(previous["abandoned_detected_by"], current["run_id"])

        written = [json.loads(text) for text in self.writes]
        lock_evidence = next(
            index for index, data in enumerate(written)
            if data["run_id"] == current["run_id"] and data["lock_acquired_at"]
        )
        reclassified = next(
            index for index, data in enumerate(written)
            if data["run_id"] == owner.run_id and data["terminal_state"] == "abandoned"
        )
        self.assertLess(lock_evidence, reclassified, "lock evidence must be persisted before the scan")
        self.assertIsNone(written[lock_evidence]["stages"]["scrape"]["started_at"])

    # OPS-02 follow-up 2
    def test_pending_journal_without_lock_evidence_is_not_abandoned(self) -> None:
        pending = self._pending_journal()
        before = pending.path.read_bytes()
        self.run_command()
        self.assertEqual(pending.path.read_bytes(), before)
        self.assertEqual(json.loads(before)["terminal_state"], "running")
        current = self._current_journal(pending)
        self.assertEqual(current["terminal_state"], "completed")
        self.assertEqual(current["abandoned_previous"], [])
        self.assertEqual(current["stages"]["preflight"]["counters"]["abandoned_previous"], 0)

    def test_only_lock_owners_are_abandoned_when_both_exist(self) -> None:
        owner = self._lock_owner_journal("validation_probe")
        pending = self._pending_journal()
        pending_before = pending.path.read_bytes()
        self.run_command()
        self.assertEqual(json.loads(owner.path.read_text(encoding="utf-8"))["terminal_state"], "abandoned")
        self.assertEqual(pending.path.read_bytes(), pending_before)
        self.assertEqual(self._current_journal(owner, pending)["abandoned_previous"], [owner.run_id])

    # OPS-02 follow-up 4
    def test_successful_run_persists_lock_evidence(self) -> None:
        self.run_command()
        journal = _only_journal(self.runs)[1]
        self.assertEqual(journal["terminal_state"], "completed")
        self.assertIsNotNone(journal["lock_acquired_at"])
        self.assertLessEqual(journal["started_at"], journal["lock_acquired_at"])
        self.assertLessEqual(journal["lock_acquired_at"], journal["stages"]["preflight"]["finished_at"])
        events = dict(self.events)
        self.assertIsNone(events["lock"]["lock_acquired_at"], "evidence is written only after the lock is held")
        self.assertIsNotNone(events["scrape"]["lock_acquired_at"])

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

    # Phase 0 closure: commit truth survives a post-commit failure
    def test_post_commit_logging_failure_keeps_committed_true(self) -> None:
        journal = self.run_failing("catalog_post_commit", BrokenPipeError(32, "Broken pipe"))
        self.assertTrue(self.committed)
        write = journal["stages"]["catalog_write"]
        self.assertEqual(journal["terminal_state"], "failed")
        self.assertEqual(journal["failed_stage"], "catalog_write")
        self.assertEqual(write["status"], "failed")
        self.assertIs(write["committed"], True)
        for text in self.writes:
            if json.loads(text)["run_id"] == journal["run_id"]:
                self.assertIsNot(json.loads(text)["stages"]["catalog_write"]["committed"], False)

    def test_commit_is_journaled_before_post_commit_work(self) -> None:
        self.fail_at = "catalog_post_commit"
        self.failure = BrokenPipeError(32, "Broken pipe")
        with self.assertRaises(BrokenPipeError):
            self.run_command()
        at_failure = dict(self.events)["catalog_post_commit"]
        self.assertEqual(at_failure["stages"]["catalog_write"]["status"], "running")
        self.assertIs(at_failure["stages"]["catalog_write"]["committed"], True)

    def test_failure_before_commit_is_not_committed(self) -> None:
        journal = self.run_failing("catalog_write", RuntimeError("write failed before commit"))
        self.assertFalse(self.committed)
        self.assertIs(journal["stages"]["catalog_write"]["committed"], False)

    def test_later_failure_keeps_catalog_commit(self) -> None:
        journal = self.run_failing("validation_write", RuntimeError("later failure"))
        self.assertIs(journal["stages"]["catalog_write"]["committed"], True)
        self.assertEqual(journal["stages"]["catalog_write"]["status"], "completed")

    # Phase 0 closure: quoted structured secrets never reach the written journal
    def test_quoted_structured_secrets_never_reach_serialized_journal(self) -> None:
        cases = {
            "python_dict_repr": (ValueError({"api_key": "SYNTHETIC_QUOTED_KEY_123"}), ["SYNTHETIC_QUOTED_KEY_123"]),
            "json_authorization": (
                RuntimeError('{"authorization": "Bearer SYNTHETIC TOKEN VALUE"}'),
                ["SYNTHETIC TOKEN VALUE", "SYNTHETIC", "TOKEN VALUE"],
            ),
            "single_quoted_password": (
                RuntimeError("{'password': 'synthetic password with spaces'}"),
                ["synthetic password with spaces", "password with spaces"],
            ),
            "compact_json_token": (RuntimeError('{"token":"abc123-secret-value"}'), ["abc123-secret-value"]),
            "nested_json": (RuntimeError('{"error":{"api_key":"SYNTHETIC_NESTED_KEY"}}'), ["SYNTHETIC_NESTED_KEY"]),
            "nested_repr": (ValueError({"error": {"apikey": "SYNTHETIC_NESTED_REPR"}}), ["SYNTHETIC_NESTED_REPR"]),
            "system_exit_json": (
                SystemExit('PROVIDER_FAILED {"api-key": "SYNTHETIC EXIT KEY", "secret": "SYNTHETIC SECRET"}'),
                ["SYNTHETIC EXIT KEY", "SYNTHETIC SECRET"],
            ),
            "escaped_json": (
                RuntimeError('payload="{\\"access_token\\": \\"SYNTHETIC ESCAPED TOKEN\\"}"'),
                ["SYNTHETIC ESCAPED TOKEN"],
            ),
            "mixed_keys": (
                RuntimeError(
                    "{'auth_token': 'SYNTH-AUTH-1', 'passwd': 'SYNTH PASSWD 2', 'pwd': 'SYNTH-PWD-3', "
                    "'Authorization': 'Basic SYNTHBASIC4', 'API_KEY': 'SYNTH-UPPER-5'}"
                ),
                ["SYNTH-AUTH-1", "SYNTH PASSWD 2", "SYNTH-PWD-3", "SYNTHBASIC4", "SYNTH-UPPER-5"],
            ),
        }
        for label, (failure, secrets) in cases.items():
            with self.subTest(label):
                for path in self.runs.glob("*.json"):
                    path.unlink()
                self.writes.clear()
                self.run_failing("validation_write", failure)
                texts = [path.read_text(encoding="utf-8") for path in self.runs.glob("*.json")] + self.writes
                self.assertTrue(texts)
                for text in texts:
                    json.loads(text)
                    for secret in secrets:
                        self.assertNotIn(secret, text)
                journal = _only_journal(self.runs)[1]
                self.assertIn("redacted", journal["stages"]["validation_write"]["failure_message"])
                self.assertIn("redacted", journal["failure_message"])


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

    def test_abandon_scan_requires_current_lock_evidence(self) -> None:
        owner = run_journal.RunJournal.start(self.dir, run={}, git_commit=None)
        owner.record_lock_acquired()
        owner.begin("scrape")
        before = owner.path.read_bytes()
        current = run_journal.RunJournal.start(self.dir, run={}, git_commit=None)
        with self.assertRaises(RuntimeError):
            current.mark_abandoned_previous()
        self.assertEqual(owner.path.read_bytes(), before)
        current.record_lock_acquired()
        self.assertEqual(current.mark_abandoned_previous(), [owner.run_id])

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

    def test_sanitize_redacts_quoted_structured_secrets(self) -> None:
        cases = [
            ("{'api_key': 'SYNTHETIC_QUOTED_KEY_123'}", "SYNTHETIC_QUOTED_KEY_123"),
            ('{"authorization": "Bearer SYNTHETIC TOKEN VALUE"}', "SYNTHETIC TOKEN VALUE"),
            ("{'password': 'synthetic password with spaces'}", "password with spaces"),
            ('{"token":"abc123-secret-value"}', "abc123-secret-value"),
            ('{"error":{"api_key":"SYNTHETIC_NESTED_KEY"}}', "SYNTHETIC_NESTED_KEY"),
            ('{"Api-Key" : "SYNTH A"}', "SYNTH A"),
            ("{'client_secret': 'SYNTH B'}", "SYNTH B"),
            ('{"access_token": "SYNTH \\"QUOTED\\" C"}', "QUOTED"),
            ("password='SYNTH D with spaces'", "with spaces"),
            ('token="SYNTH E"', "SYNTH E"),
            ('{"token": 123456789}', "123456789"),
        ]
        for text, secret in cases:
            with self.subTest(text):
                cleaned = run_journal.sanitize_text(text)
                self.assertNotIn(secret, cleaned)
                self.assertIn("redacted", cleaned)
                for token in FORBIDDEN:
                    self.assertNotIn(token, cleaned.lower())

    def test_sanitize_keeps_non_secret_keys(self) -> None:
        text = "{'stage': 'catalog_write', 'tokens_seen': 3, 'reason': 'lock_timeout'}"
        self.assertEqual(run_journal.sanitize_text(text), text)

    def test_committed_true_is_never_downgraded(self) -> None:
        journal = run_journal.RunJournal.start(self.dir, run={}, git_commit=None)
        journal.begin("catalog_write")
        journal.mark_committed("catalog_write")
        journal.fail("catalog_write", BrokenPipeError(32, "Broken pipe"), committed=False)
        record = json.loads(journal.path.read_text(encoding="utf-8"))["stages"]["catalog_write"]
        self.assertEqual(record["status"], "failed")
        self.assertIs(record["committed"], True)

    def test_abandoned_stage_keeps_durable_commit(self) -> None:
        owner = run_journal.RunJournal.start(self.dir, run={}, git_commit=None)
        owner.record_lock_acquired()
        owner.begin("catalog_write")
        owner.mark_committed("catalog_write")
        current = run_journal.RunJournal.start(self.dir, run={}, git_commit=None)
        current.record_lock_acquired()
        self.assertEqual(current.mark_abandoned_previous(), [owner.run_id])
        record = json.loads(owner.path.read_text(encoding="utf-8"))["stages"]["catalog_write"]
        self.assertEqual(record["failure_code"], "RUN_ABANDONED")
        self.assertIs(record["committed"], True)

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

    # Phase 0 closure: real commit, then the completion print fails
    def test_real_commit_then_logging_failure_never_records_uncommitted(self) -> None:
        real_print = builtins.print

        def broken_print(*args, **kwargs):
            if args and str(args[0]).startswith("STAGE catalog_write committed"):
                raise BrokenPipeError(32, "Broken pipe")
            real_print(*args, **kwargs)

        with patch("jobready_catalog.print", broken_print, create=True):
            with self.assertRaises(BrokenPipeError):
                self._run()
        self.assertEqual(self._count(), 1, "database commit observed")
        journal = self._journal()
        write = journal["stages"]["catalog_write"]
        self.assertEqual(journal["terminal_state"], "failed")
        self.assertEqual(write["status"], "failed")
        self.assertIs(write["committed"], True)
        self.assertEqual(journal["stages"]["validation_select"]["status"], "not_started")
        self._assert_clean()

    def test_real_commit_failure_is_not_committed(self) -> None:
        connection = _CommitFailingConnection(catalog.connect(CATALOG_URL))
        with patch("run_jobready_daily._open_catalog", lambda: connection):
            with self.assertRaises(SystemExit):
                self._run()
        self.assertEqual(connection.commit_attempts, 1)
        self.assertEqual(self._count(), 0)
        write = self._journal()["stages"]["catalog_write"]
        self.assertEqual(write["status"], "failed")
        self.assertIs(write["committed"], False)
        self.assertEqual(write["failure_code"], "CATALOG_WRITE_FAILED")

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
