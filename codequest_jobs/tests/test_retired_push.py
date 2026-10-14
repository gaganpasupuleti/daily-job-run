"""The retired CodeQuest-to-JobReady push refuses without database access.

Pure tests need no database. LegacyPushCannotReactivateTests uses disposable
Postgres named by JOBREADY_DAILY_CATALOG_URL and is skipped when it is absent.
"""

from __future__ import annotations

import ast
import io
import os
import re
import runpy
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
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
from psycopg2.extensions import make_dsn  # noqa: E402

import db  # noqa: E402
import jobready_catalog as catalog  # noqa: E402
import push_validated_to_job_ready as push  # noqa: E402
import run_jobready_daily as daily  # noqa: E402
import run_logger  # noqa: E402
import sync_job_ready_ui  # noqa: E402
from normalize import UnifiedJob  # noqa: E402

MODULE = ROOT / "push_validated_to_job_ready.py"
DUPLICATE = REPO / "link_validator_service" / "push_validated_to_job_ready.py"
SCRIPTS = (MODULE, DUPLICATE)
REFUSAL = "RETIRED_COMMAND push_validated_to_job_ready"
CATALOG_URL = os.environ.get("JOBREADY_DAILY_CATALOG_URL", "").strip()
FIXTURE_SCHEMA = "legacy_push_fixture"
SENTINEL_ENV = {
    "CODEQUEST_DATABASE_URL": "postgresql://cq_user:SENTINEL_CQ_SECRET@codequest.invalid:5432/codequest",
    "DATABASE_URL": "postgresql://legacy_user:SENTINEL_LEGACY_SECRET@legacy.invalid:5432/naukri_scraper",
    "JOB_READY_DATABASE_URL": "postgresql://catalog_user:SENTINEL_CATALOG_SECRET@catalog.invalid:5432/railway",
    "JOB_READY_APP_DATABASE_URL": "postgresql://app_user:SENTINEL_APP_SECRET@app.invalid:5432/railway",
}
OLD_INVOCATIONS = (
    [],
    ["--max-age-days", "1"],
    ["--max-age-days", "0"],
    ["--prune"],
    ["--prune", "--confirm-target", "app.invalid:5432/railway"],
    ["--max-age-days", "7", "--prune", "--confirm-target", "app.invalid:5432/railway"],
    ["--confirm-target", SENTINEL_ENV["JOB_READY_APP_DATABASE_URL"]],
    ["--force", "--yes"],
)
BLOCKED_MODULES = ("psycopg2", "dotenv", "db", "run_logger", "jobready_catalog", "sync_job_ready_ui")
T09 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
T10 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


def _fail(name: str):
    def raiser(*_args, **_kwargs):
        raise AssertionError(f"{name} must not be called by the retired command")

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


def _run_script(script: Path, argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as cwd:
        return subprocess.run(
            [sys.executable, str(script), *argv],
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=120,
        )


class RetiredPushTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, mock.MagicMock]:
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, SENTINEL_ENV), mock.patch.object(
            psycopg2, "connect", side_effect=_fail("psycopg2.connect")
        ) as connect, mock.patch.object(db, "connect", side_effect=_fail("db.connect")), mock.patch.object(
            run_logger, "start_push_run", side_effect=_fail("start_push_run")
        ), mock.patch.object(
            run_logger, "finish_push_run", side_effect=_fail("finish_push_run")
        ), mock.patch.object(
            catalog, "ensure_schema", side_effect=_fail("ensure_schema")
        ), mock.patch.object(
            sync_job_ready_ui, "assert_application_write_target", side_effect=_fail("assert_application_write_target")
        ), mock.patch.object(
            sync_job_ready_ui, "archive_ui_jobs_not_in_catalog", side_effect=_fail("archive_ui_jobs_not_in_catalog")
        ), mock.patch.object(
            sync_job_ready_ui, "apply_membership", side_effect=_fail("apply_membership")
        ), redirect_stdout(stdout):
            code = push.main(argv)
        return code, stdout.getvalue(), connect

    def test_default_invocation_fails_closed(self) -> None:
        code, output, connect = self.run_main([])
        self.assertEqual(code, 2)
        self.assertEqual(code, push.EXIT_RETIRED)
        self.assertEqual(output.strip(), REFUSAL)
        connect.assert_not_called()

    def test_old_write_arguments_do_not_re_enable_the_push(self) -> None:
        for argv in OLD_INVOCATIONS:
            code, output, connect = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(output.strip().splitlines()[-1], REFUSAL, argv)
            connect.assert_not_called()

    def test_help_explains_retirement_and_still_fails(self) -> None:
        for argv in (["--help"], ["-h"], ["--max-age-days", "1", "--help"]):
            code, output, connect = self.run_main(argv)
            self.assertEqual(code, 2, argv)
            self.assertIn("Pushing CodeQuest scraped_jobs into the JobReady catalog is no longer supported", output)
            self.assertIn("CodeQuest is not authoritative for JobReady lifecycle state", output)
            self.assertIn("run_jobready_daily.py", output)
            self.assertIn("No arguments or environment variables re-enable this command", output)
            self.assertEqual(output.strip().splitlines()[-1], REFUSAL)
            connect.assert_not_called()

    def test_no_database_variable_is_required(self) -> None:
        cleared = {name: "" for name in SENTINEL_ENV}
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, cleared), mock.patch.object(
            psycopg2, "connect", side_effect=_fail("psycopg2.connect")
        ), redirect_stdout(stdout):
            code = push.main(["--max-age-days", "1"])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue().strip(), REFUSAL)

    def test_configured_database_urls_are_never_used(self) -> None:
        for argv in OLD_INVOCATIONS:
            _code, _output, connect = self.run_main(argv)
            self.assertEqual(connect.call_count, 0, argv)

    def test_refusal_never_echoes_arguments_or_secrets(self) -> None:
        self.assertEqual(push.RETIRED_MESSAGE, REFUSAL)
        for argv in OLD_INVOCATIONS + (["--help"],):
            _code, output, _connect = self.run_main(argv)
            self.assertIn(REFUSAL, output)
            self.assertNotIn("://", output)
            self.assertNotIn("@", output)
            self.assertNotIn("app.invalid", output)
            self.assertNotIn("SENTINEL", output)

    def test_retired_modules_import_no_database_or_write_dependency(self) -> None:
        self.assertEqual(_imports(MODULE), {"sys"})
        self.assertEqual(_imports(DUPLICATE), {"runpy", "pathlib"})
        for name in ("FETCH_SQL", "UPSERT_SQL", "SCHEMA_SQL", "dedupe_rows", "experience_bucket", "as_json"):
            self.assertFalse(hasattr(push, name), name)

    def test_retired_modules_hold_no_executable_catalog_sql(self) -> None:
        sql = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|ALTER|TRUNCATE|SELECT|CONFLICT|EXCLUDED)\b|validated_jobs")
        for script in SCRIPTS:
            source = script.read_text(encoding="utf-8")
            self.assertFalse("execute" in source, script)
            self.assertFalse("connect" in source, script)
            for value in _code_strings(script):
                self.assertIsNone(sql.search(value), (script, value))

    def test_duplicate_delegates_to_the_retired_command(self) -> None:
        namespace = runpy.run_path(str(DUPLICATE))
        self.assertEqual(Path(namespace["RETIRED_SCRIPT"]).resolve(), MODULE.resolve())
        self.assertEqual(Path(push.__file__).resolve(), MODULE.resolve())

    def test_script_entrypoints_fail_without_importing_database_modules(self) -> None:
        blocked = "".join(f"sys.modules[{name!r}] = None\n" for name in BLOCKED_MODULES)
        for script in SCRIPTS:
            for argv in OLD_INVOCATIONS:
                program = (
                    "import runpy, sys\n"
                    f"{blocked}"
                    f"sys.argv = [{str(script)!r}, *{argv!r}]\n"
                    f"runpy.run_path({str(script)!r}, run_name='__main__')\n"
                )
                proc = subprocess.run(
                    [sys.executable, "-c", program],
                    capture_output=True,
                    text=True,
                    env={**os.environ, **SENTINEL_ENV},
                    timeout=60,
                )
                self.assertEqual(proc.returncode, 2, (script, argv, proc.stderr))
                self.assertEqual(proc.stdout.strip(), REFUSAL, (script, argv))
                self.assertEqual(proc.stderr.strip(), "", (script, argv))
                self.assertNotIn("SENTINEL", proc.stdout + proc.stderr)


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
class LegacyPushCannotReactivateTests(unittest.TestCase):
    """An expired catalog row stays expired when the legacy push is invoked.

    Legacy CodeQuest tables live in a throwaway schema of the disposable catalog
    database. The push is pointed at them and at the catalog, exactly as an
    operator would configure it.
    """

    STATE = (
        "link_status",
        "last_confirmed_open_at",
        "last_confirmed_closed_at",
        "link_checked_at",
        "last_link_observation",
        "updated_at",
        "synced_at",
        "title",
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
            cur.execute(f"DROP SCHEMA IF EXISTS {FIXTURE_SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {FIXTURE_SCHEMA}")
            cur.execute(
                f"""
                CREATE TABLE {FIXTURE_SCHEMA}.scraped_jobs (
                    id BIGSERIAL PRIMARY KEY, job_id TEXT, source TEXT, title TEXT, company TEXT,
                    location TEXT, job_type TEXT, job_url TEXT, apply_url TEXT, link_status TEXT,
                    ingest_profile TEXT, date_posted DATE, salary_min NUMERIC, salary_max NUMERIC,
                    currency TEXT, created_at TIMESTAMP
                );
                CREATE TABLE {FIXTURE_SCHEMA}.job_enrichments (
                    job_id TEXT, actual_role_id TEXT, actual_role_name TEXT, role_level_id TEXT,
                    experience_level TEXT, jd_summary TEXT, jd_clean TEXT, jd_responsibilities TEXT,
                    jd_requirements TEXT, keywords_must TEXT, keywords_nice TEXT, practice_focus TEXT,
                    required_skills TEXT, good_to_have_skills TEXT, tools TEXT, programming_languages TEXT,
                    databases TEXT, frameworks TEXT, student_preparation_topics TEXT, quiz_pack_id TEXT,
                    mapping_confidence NUMERIC, manual_review_needed BOOLEAN, approved_status TEXT
                );
                CREATE TABLE {FIXTURE_SCHEMA}.job_roles (role_id TEXT, role_family TEXT);
                """
            )
        self.conn.commit()
        self.addCleanup(self._drop_fixture)

    def _drop_fixture(self) -> None:
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {FIXTURE_SCHEMA} CASCADE")
        self.conn.commit()

    def _fetch(self, sql: str, params=None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        self.conn.commit()
        return rows

    def _state(self, job_id: str) -> dict:
        (row,) = self._fetch(f"SELECT {', '.join(self.STATE)} FROM validated_jobs WHERE job_id = %s", (job_id,))
        return dict(zip(self.STATE, row, strict=True))

    def _seed_expired_catalog_row_and_active_legacy_row(self) -> str:
        url = "https://example.test/jobs/legacy-reactivation"
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE jobready_link_observations, validated_jobs RESTART IDENTITY")
            cur.execute(f"TRUNCATE {FIXTURE_SCHEMA}.scraped_jobs RESTART IDENTITY")
        self.conn.commit()
        with redirect_stdout(io.StringIO()):
            catalog.upsert_jobs(self.conn, [daily.prepare_catalog_row(_job(url))])
        (job_id,) = self._fetch("SELECT job_id FROM validated_jobs WHERE job_url = %s", (url,))[0]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE validated_jobs
                SET link_status = 'expired', last_confirmed_open_at = %s, last_confirmed_closed_at = %s
                WHERE job_id = %s
                """,
                (T09, T10, job_id),
            )
            cur.execute(
                f"""
                INSERT INTO {FIXTURE_SCHEMA}.scraped_jobs
                    (job_id, source, title, job_url, apply_url, link_status, created_at)
                VALUES (%s, 'naukri', 'Legacy CodeQuest title', %s, %s, 'active', NOW())
                """,
                (job_id, url, url),
            )
        self.conn.commit()
        return job_id

    def _operator_env(self) -> dict[str, str]:
        legacy = make_dsn(CATALOG_URL, options=f"-c search_path={FIXTURE_SCHEMA}")
        env = {key: value for key, value in os.environ.items() if not key.startswith("JOBREADY_")}
        env.update(
            {
                "CODEQUEST_DATABASE_URL": legacy,
                "DATABASE_URL": legacy,
                "JOB_READY_DATABASE_URL": CATALOG_URL,
                "JOB_READY_APP_DATABASE_URL": "",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return env

    def test_legacy_push_cannot_reactivate_an_expired_row(self) -> None:
        env = self._operator_env()
        for script in SCRIPTS:
            for argv in ([], ["--max-age-days", "1"]):
                with self.subTest(script=script.parent.name, argv=argv):
                    job_id = self._seed_expired_catalog_row_and_active_legacy_row()
                    before = self._state(job_id)
                    self.assertEqual(before["link_status"], "expired")
                    proc = _run_script(script, argv, env)
                    self.assertEqual(self._state(job_id), before)
                    self.assertEqual(self._fetch("SELECT COUNT(*) FROM jobready_link_observations"), [(0,)])
                    self.assertEqual(self._fetch("SELECT COUNT(*) FROM validated_jobs"), [(1,)])
                    self.assertEqual(
                        self._fetch(
                            "SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY 1", (FIXTURE_SCHEMA,)
                        ),
                        [("job_enrichments",), ("job_roles",), ("scraped_jobs",)],
                    )
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(proc.stdout.strip(), REFUSAL)


if __name__ == "__main__":
    unittest.main()
