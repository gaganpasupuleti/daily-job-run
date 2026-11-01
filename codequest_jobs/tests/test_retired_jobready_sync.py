"""validate_links.py and audit_job_ready.py never write JobReady lifecycle state.

Pure tests need no database. DirectExpiryBypassTests uses disposable Postgres
named by JOBREADY_DAILY_CATALOG_URL and is skipped when it is absent. The
CodeQuest and scraper sentinel databases are checked when
JOBREADY_SENTINEL_CODEQUEST_URL and JOBREADY_SENTINEL_SCRAPER_URL are set.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
SCRAPER = REPO / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

import psycopg2  # noqa: E402

import audit_job_ready  # noqa: E402
import db  # noqa: E402
import jobready_catalog as catalog  # noqa: E402
import run_jobready_daily as daily  # noqa: E402
import run_logger  # noqa: E402
import sync_job_ready_ui  # noqa: E402
import validate_links  # noqa: E402
from jobready_validator import JOBREADY_EVIDENCE_VERSION, LinkObservation  # noqa: E402
from normalize import UnifiedJob  # noqa: E402

VALIDATE = ROOT / "validate_links.py"
AUDIT = ROOT / "audit_job_ready.py"
MARKER = "JOBREADY_SYNC_RETIRED"
CATALOG_URL = os.environ.get("JOBREADY_DAILY_CATALOG_URL", "").strip()
CODEQUEST_SENTINEL = os.environ.get("JOBREADY_SENTINEL_CODEQUEST_URL", "").strip()
SCRAPER_SENTINEL = os.environ.get("JOBREADY_SENTINEL_SCRAPER_URL", "").strip()
APP_TARGET = "app.invalid:5432/railway"
SENTINEL_ENV = {
    "CODEQUEST_DATABASE_URL": "postgresql://cq_user:SENTINEL_CQ_SECRET@codequest.invalid:5432/codequest",
    "DATABASE_URL": "postgresql://legacy_user:SENTINEL_LEGACY_SECRET@legacy.invalid:5432/naukri_scraper",
    "JOB_READY_DATABASE_URL": "postgresql://catalog_user:SENTINEL_CATALOG_SECRET@catalog.invalid:5432/railway",
    "JOB_READY_APP_DATABASE_URL": "postgresql://app_user:SENTINEL_APP_SECRET@app.invalid:5432/railway",
}
APP_DSN = SENTINEL_ENV["JOB_READY_APP_DATABASE_URL"]
VALIDATE_INVOCATIONS = (
    ["--sync-job-ready-every", "1"],
    ["--sync-job-ready-every", "0"],
    ["--sync-job-ready-every", ""],
    ["--sync-job-ready-every", "25", "--confirm-target", APP_TARGET],
    ["--confirm-target", APP_TARGET],
    ["--confirm-target", ""],
    [f"--confirm-target={APP_DSN}"],
    ["--sync-job-ready-every=not-a-number"],
    ["--sync", "5", "--confirm", APP_TARGET],
    ["--limit", "10", "--sources", "naukri", "--sync-job-ready-every", "1", "--confirm-target", APP_DSN],
)
AUDIT_INVOCATIONS = (
    ["--prune-job-ready"],
    ["--prune-job-ready", "--confirm-target", APP_TARGET],
    ["--confirm-target", APP_TARGET],
    ["--confirm-target", ""],
    [f"--confirm-target={APP_DSN}", "--prune-job-ready"],
    ["--prune", "--confirm", APP_TARGET],
)
JOBREADY_WRITE_MODULES = {"sync_job_ready_ui", "jobready_catalog", "run_jobready_daily", "validate_links"}
WRITE_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|TRUNCATE|MERGE|UPSERT)\b", re.IGNORECASE)
UPDATE_CATALOG = re.compile(r"\bUPDATE\s+validated_jobs\b", re.IGNORECASE)
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "tests", ".ruff_cache"}


def _fail(name: str):
    def raiser(*_args, **_kwargs):
        raise AssertionError(f"{name} must not be called")

    return raiser


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    return imported - {"__future__"}


def _code_strings(path: Path) -> list[str]:
    """String constants other than the module docstring."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring = ast.get_docstring(tree, clean=False)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value != docstring
    ]


def _jobready_sentinels(stack: ExitStack) -> None:
    """Every JobReady, membership, and CodeQuest entry point raises if reached."""
    for target, name in (
        (psycopg2, "connect"),
        (db, "connect"),
        (catalog, "connect"),
        (catalog, "ensure_schema"),
        (catalog, "apply_schema"),
        (catalog, "upsert_jobs"),
        (catalog, "apply_link_observations"),
        (catalog, "mark_confirmed_expired"),
        (sync_job_ready_ui, "assert_application_write_target"),
        (sync_job_ready_ui, "archive_ui_jobs_not_in_catalog"),
        (sync_job_ready_ui, "apply_membership"),
        (run_logger, "start_validation_run"),
        (run_logger, "finish_validation_run"),
    ):
        stack.enter_context(mock.patch.object(target, name, side_effect=_fail(f"{target.__name__}.{name}")))


def _leaks(output: str) -> list[str]:
    return [token for token in ("://", "@", "app.invalid", "SENTINEL", "not-a-number") if token in output]


class ValidateLinksRetiredSyncTests(unittest.TestCase):
    def run_main(self, argv: list[str], **overrides) -> tuple[object, str]:
        patched = {
            "load_env": _fail("load_env"),
            "fetch_jobs": _fail("fetch_jobs"),
            "validate_one": _fail("validate_one"),
            "apply_updates": _fail("apply_updates"),
            "status_summary": _fail("status_summary"),
            "build_indeed_expired_map": _fail("build_indeed_expired_map"),
            "start_validation_run": _fail("start_validation_run"),
            "finish_validation_run": _fail("finish_validation_run"),
            **overrides,
        }
        out = io.StringIO()
        with ExitStack() as stack, tempfile.TemporaryDirectory() as tmp:
            _jobready_sentinels(stack)
            stack.enter_context(mock.patch.dict(os.environ, SENTINEL_ENV))
            stack.enter_context(mock.patch.object(validate_links, "OUTPUT_DIR", Path(tmp)))
            for name, value in patched.items():
                stack.enter_context(mock.patch.object(validate_links, name, side_effect=value))
            stack.enter_context(mock.patch.object(sys, "argv", ["validate_links.py", *argv]))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(out))
            try:
                validate_links.main()
                code: object = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_old_sync_options_fail_closed_before_any_access(self) -> None:
        for argv in VALIDATE_INVOCATIONS:
            code, output = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(code, validate_links.EXIT_RETIRED)
            self.assertEqual(output.splitlines()[0], MARKER, argv)
            self.assertIn("run_jobready_daily.py", output, argv)

    def test_refusal_never_echoes_arguments_or_secrets(self) -> None:
        for argv in VALIDATE_INVOCATIONS:
            _code, output = self.run_main(argv)
            self.assertEqual(_leaks(output), [], argv)

    def test_help_marks_the_sync_options_retired(self) -> None:
        code, output = self.run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(output.count("RETIRED. Refused with JOBREADY_SYNC_RETIRED"), 2)
        self.assertNotIn("align the application DB", output)

    def test_writer_function_is_removed(self) -> None:
        self.assertFalse(hasattr(validate_links, "sync_dead_to_job_ready"))

    def test_default_run_validates_scraped_jobs_without_jobready_access(self) -> None:
        written: list[list[tuple[str, str, str, str]]] = []
        code, output = self.run_main(
            [],
            load_env=lambda *_args, **_kwargs: None,
            fetch_jobs=lambda **_kwargs: [("1", "CQJ-1", "naukri", "https://example.test/j", "", "active")],
            validate_one=lambda row, **_kwargs: (row[0], row[1], row[2], "expired"),
            apply_updates=lambda rows: written.append(list(rows)),
            status_summary=lambda: {"by_status": {}},
            build_indeed_expired_map=lambda rows: {},
            start_validation_run=lambda **_kwargs: ("run", None),
            finish_validation_run=lambda *_args, **_kwargs: None,
        )
        self.assertEqual(code, 0)
        self.assertEqual(written, [[("1", "CQJ-1", "naukri", "expired")]])
        self.assertIn("JobReady catalog and application database will not be modified", output)
        self.assertEqual(_leaks(output.replace("https://example.test/j", "")), [])


class AuditRetiredPruneTests(unittest.TestCase):
    def run_main(self, argv: list[str], **overrides) -> tuple[int, str, Path | None]:
        patched = {
            "load_env": _fail("load_env"),
            "fetch_job_ready_rows": _fail("fetch_job_ready_rows"),
            "default_probes": _fail("default_probes"),
            **overrides,
        }
        out = io.StringIO()
        report: dict | None = None
        with ExitStack() as stack, tempfile.TemporaryDirectory() as tmp:
            _jobready_sentinels(stack)
            stack.enter_context(mock.patch.dict(os.environ, SENTINEL_ENV))
            stack.enter_context(mock.patch.object(audit_job_ready, "ROOT", Path(tmp)))
            for name, value in patched.items():
                stack.enter_context(mock.patch.object(audit_job_ready, name, side_effect=value))
            stack.enter_context(redirect_stdout(out))
            stack.enter_context(redirect_stderr(out))
            try:
                code = audit_job_ready.main(argv)
            except SystemExit as exc:
                code = exc.code
            reports = list((Path(tmp) / "output").glob("job_ready_audit_*.json"))
            if reports:
                report = json.loads(reports[0].read_text(encoding="utf-8"))
        return code, out.getvalue(), report

    def test_old_prune_options_fail_closed_before_any_access(self) -> None:
        for argv in AUDIT_INVOCATIONS:
            code, output, report = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(code, audit_job_ready.EXIT_RETIRED)
            self.assertEqual(output.splitlines()[0], MARKER, argv)
            self.assertIn("run_jobready_daily.py", output, argv)
            self.assertIsNone(report, argv)

    def test_refusal_never_echoes_arguments_or_secrets(self) -> None:
        for argv in AUDIT_INVOCATIONS:
            _code, output, _report = self.run_main(argv)
            self.assertEqual(_leaks(output), [], argv)

    def test_help_marks_the_prune_options_retired(self) -> None:
        code, output, _report = self.run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(output.count("RETIRED. Refused with JOBREADY_SYNC_RETIRED"), 2)
        self.assertNotIn("unless prune is explicit", output)

    def test_default_audit_reports_confirmed_expired_without_writing(self) -> None:
        row = {
            "job_id": "CQJ-1",
            "source": "naukri",
            "job_url": "https://example.test/j",
            "apply_url": "",
            "link_status": "active",
        }
        code, output, report = self.run_main(
            [],
            load_env=lambda *_args, **_kwargs: None,
            fetch_job_ready_rows=lambda **_kwargs: [row],
            default_probes=lambda: {"naukri": lambda _row: ("confirmed_expired", "test_closed", "test")},
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["confirmed_expired"], 1)
        self.assertNotIn("removed", report)
        self.assertNotIn("prune", report)
        self.assertIn("Read-only audit", output)
        self.assertNotIn("removed=", output)

    def test_audit_has_no_write_path(self) -> None:
        self.assertEqual(_imports(AUDIT) & JOBREADY_WRITE_MODULES, set())
        self.assertFalse(hasattr(audit_job_ready, "sync_dead_to_job_ready"))
        for value in _code_strings(AUDIT):
            self.assertIsNone(WRITE_SQL.search(value), value)
        self.assertIn("readonly=True", AUDIT.read_text(encoding="utf-8"))


class StaticDirectWriteTests(unittest.TestCase):
    def test_only_the_canonical_catalog_module_updates_validated_jobs(self) -> None:
        found: set[str] = set()
        for path in REPO.rglob("*.py"):
            if SKIP_DIRS & set(path.relative_to(REPO).parts):
                continue
            if UPDATE_CATALOG.search(path.read_text(encoding="utf-8", errors="replace")):
                found.add(path.relative_to(REPO).as_posix())
        self.assertEqual(found, {"codequest_jobs/jobready_catalog.py"})

    def test_validate_links_holds_no_jobready_write_dependency(self) -> None:
        source = VALIDATE.read_text(encoding="utf-8")
        self.assertEqual(_imports(VALIDATE) & JOBREADY_WRITE_MODULES, set())
        for token in (
            "validated_jobs",
            "JOB_READY_DATABASE_URL",
            "JOB_READY_APP_DATABASE_URL",
            "sync_dead_to_job_ready",
            "archive_ui_jobs_not_in_catalog",
            "apply_link_observations",
            "assert_application_write_target",
        ):
            self.assertFalse(token in source, token)


class RetiredEntrypointTests(unittest.TestCase):
    def test_scripts_refuse_old_write_options(self) -> None:
        cases = [(VALIDATE, argv) for argv in VALIDATE_INVOCATIONS] + [(AUDIT, argv) for argv in AUDIT_INVOCATIONS]
        env = {key: value for key, value in os.environ.items() if not key.startswith("JOBREADY_")}
        env.update(SENTINEL_ENV)
        env["PYTHONIOENCODING"] = "utf-8"
        for script, argv in cases:
            with tempfile.TemporaryDirectory() as cwd:
                proc = subprocess.run(
                    [sys.executable, str(script), *argv],
                    capture_output=True,
                    text=True,
                    env=env,
                    cwd=cwd,
                    timeout=120,
                )
            self.assertEqual(proc.returncode, 2, (script.name, argv, proc.stderr[-400:]))
            self.assertEqual(proc.stdout.splitlines()[0], MARKER, (script.name, argv))
            self.assertEqual(proc.stderr.strip(), "", (script.name, argv))
            self.assertEqual(_leaks(proc.stdout + proc.stderr), [], (script.name, argv))


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


@unittest.skipUnless(CATALOG_URL, "disposable daily catalog is not configured")
class DirectExpiryBypassTests(unittest.TestCase):
    """The old sync options cannot expire a catalog row outside the ledger writer.

    The row was confirmed open moments ago and the legacy result carries no
    observation time, so no lifecycle change is justified.
    """

    STATE = (
        "link_status",
        "last_confirmed_open_at",
        "last_confirmed_closed_at",
        "link_checked_at",
        "last_link_observation",
        "updated_at",
        "synced_at",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.conn = psycopg2.connect(CATALOG_URL, connect_timeout=8)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.rollback()
        catalog.ensure_schema(self.conn)
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE jobready_link_observations, validated_jobs RESTART IDENTITY")
        self.conn.commit()
        url = "https://example.test/jobs/direct-expiry"
        with redirect_stdout(io.StringIO()):
            catalog.upsert_jobs(self.conn, [daily.prepare_catalog_row(_job(url))])
        self.opened_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE validated_jobs SET last_confirmed_open_at = %s WHERE job_url = %s RETURNING job_id",
                (self.opened_at, url),
            )
            (self.job_id,) = cur.fetchone()
        self.conn.commit()
        self.connects: list[str] = []
        self.membership: list[str] = []
        self.canonical: list[str] = []

    def _fetch(self, sql: str, params=None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        self.conn.commit()
        return rows

    def _state(self) -> dict:
        (row,) = self._fetch(f"SELECT {', '.join(self.STATE)} FROM validated_jobs WHERE job_id = %s", (self.job_id,))
        return dict(zip(self.STATE, row, strict=True))

    def _schema(self) -> tuple:
        return (
            self._fetch(
                "SELECT table_name, column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1, 2"
            ),
            self._fetch("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1"),
            self._fetch(
                "SELECT conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public' ORDER BY 1"
            ),
        )

    def _assert_sentinel_empty(self, url: str) -> None:
        if not url:
            return
        conn = psycopg2.connect(url, connect_timeout=8)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                self.assertEqual(cur.fetchall(), [])
        finally:
            conn.close()

    def _patched(self, stack: ExitStack) -> None:
        """Real catalog connections are allowed and recorded; the application side is recorded only."""
        real_connect = psycopg2.connect

        def connect(*args, **kwargs):
            self.connects.append("connect")
            return real_connect(*args, **kwargs)

        stack.enter_context(mock.patch.object(psycopg2, "connect", side_effect=connect))
        stack.enter_context(
            mock.patch.dict(
                os.environ,
                {
                    "JOB_READY_DATABASE_URL": CATALOG_URL,
                    "JOB_READY_APP_DATABASE_URL": APP_DSN,
                    "CODEQUEST_DATABASE_URL": CODEQUEST_SENTINEL or SENTINEL_ENV["CODEQUEST_DATABASE_URL"],
                    "DATABASE_URL": SCRAPER_SENTINEL or SENTINEL_ENV["DATABASE_URL"],
                },
            )
        )
        stack.enter_context(
            mock.patch.object(
                sync_job_ready_ui,
                "assert_application_write_target",
                side_effect=lambda *_a, **_k: self.membership.append("target") or APP_TARGET,
            )
        )
        stack.enter_context(
            mock.patch.object(
                sync_job_ready_ui,
                "archive_ui_jobs_not_in_catalog",
                side_effect=lambda **_k: self.membership.append("archive") or {"archived": 1, "reactivated": 0},
            )
        )
        stack.enter_context(
            mock.patch.object(
                sync_job_ready_ui, "apply_membership", side_effect=lambda *_a, **_k: self.membership.append("apply")
            )
        )
        stack.enter_context(
            mock.patch.object(
                catalog, "apply_link_observations", side_effect=lambda *_a, **_k: self.canonical.append("apply")
            )
        )

    def _assert_unchanged(self, before: dict, schema: tuple) -> None:
        after = self._state()
        self.assertEqual(after["link_status"], "active")
        self.assertIsNone(after["last_confirmed_closed_at"])
        self.assertEqual(after, before)
        self.assertEqual(self._fetch("SELECT COUNT(*) FROM jobready_link_observations"), [(0,)])
        self.assertEqual(self._fetch("SELECT COUNT(*) FROM validated_jobs"), [(1,)])
        self.assertEqual(self._schema(), schema)
        self.assertEqual(self.membership, [])
        self.assertEqual(self.canonical, [])
        self.assertEqual(self.connects, [])
        self._assert_sentinel_empty(CODEQUEST_SENTINEL)
        self._assert_sentinel_empty(SCRAPER_SENTINEL)

    def test_validate_links_sync_options_cannot_expire_a_catalog_row(self) -> None:
        before, schema = self._state(), self._schema()
        out = io.StringIO()
        legacy_row = ("1", self.job_id, "naukri", "https://example.test/jobs/direct-expiry", "", "active")
        with ExitStack() as stack, tempfile.TemporaryDirectory() as tmp:
            self._patched(stack)
            for name, value in (
                ("load_env", lambda *_a, **_k: None),
                ("fetch_jobs", lambda **_k: [legacy_row]),
                ("validate_one", lambda row, **_k: (row[0], row[1], row[2], "expired")),
                ("apply_updates", lambda rows: None),
                ("status_summary", lambda: {"by_status": {}}),
                ("build_indeed_expired_map", lambda rows: {}),
                ("start_validation_run", lambda **_k: ("run", None)),
                ("finish_validation_run", lambda *_a, **_k: None),
            ):
                stack.enter_context(mock.patch.object(validate_links, name, side_effect=value))
            stack.enter_context(mock.patch.object(validate_links, "OUTPUT_DIR", Path(tmp)))
            stack.enter_context(
                mock.patch.object(
                    sys, "argv", ["validate_links.py", "--sync-job-ready-every", "1", "--confirm-target", APP_TARGET]
                )
            )
            stack.enter_context(redirect_stdout(out))
            try:
                validate_links.main()
                code: object = 0
            except SystemExit as exc:
                code = exc.code
        self._assert_unchanged(before, schema)
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue().splitlines()[0], MARKER)

    def test_audit_prune_cannot_expire_a_catalog_row(self) -> None:
        before, schema = self._state(), self._schema()
        out = io.StringIO()
        with ExitStack() as stack, tempfile.TemporaryDirectory() as tmp:
            self._patched(stack)
            stack.enter_context(mock.patch.object(audit_job_ready, "ROOT", Path(tmp)))
            stack.enter_context(mock.patch.object(audit_job_ready, "load_env", side_effect=lambda *_a, **_k: None))
            stack.enter_context(
                mock.patch.object(
                    audit_job_ready,
                    "default_probes",
                    side_effect=lambda: {"naukri": lambda _row: ("confirmed_expired", "test_closed", "test")},
                )
            )
            stack.enter_context(redirect_stdout(out))
            code = audit_job_ready.main(["--prune-job-ready", "--confirm-target", APP_TARGET])
        self._assert_unchanged(before, schema)
        self.assertEqual(code, 2)
        self.assertEqual(out.getvalue().splitlines()[0], MARKER)

    def test_canonical_writer_still_records_and_orders_evidence(self) -> None:
        stale = LinkObservation(
            job_id=self.job_id,
            previous_status="active",
            observation="confirmed_expired",
            provider="test",
            reason="test_closed",
            observed_at=self.opened_at - timedelta(minutes=5),
        )
        catalog.apply_link_observations(self.conn, [stale], run_id="retired-sync-test")
        self.assertEqual(self._state()["link_status"], "active")
        fresh = LinkObservation(
            job_id=self.job_id,
            previous_status="active",
            observation="confirmed_expired",
            provider="test",
            reason="test_closed",
            observed_at=self.opened_at + timedelta(seconds=30),
        )
        catalog.apply_link_observations(self.conn, [fresh], run_id="retired-sync-test")
        after = self._state()
        self.assertEqual(after["link_status"], "expired")
        self.assertEqual(after["last_confirmed_closed_at"], fresh.observed_at)
        self.assertEqual(
            self._fetch(
                "SELECT previous_link_status, resulting_link_status, evidence_version "
                "FROM jobready_link_observations ORDER BY id"
            ),
            [("active", "active", JOBREADY_EVIDENCE_VERSION), ("active", "expired", JOBREADY_EVIDENCE_VERSION)],
        )


if __name__ == "__main__":
    unittest.main()
