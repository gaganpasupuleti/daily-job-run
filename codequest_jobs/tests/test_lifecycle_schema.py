"""Phase 1 lifecycle evidence schema: additive, append-only, required by the daily run.

Database cases use disposable Postgres named by JOBREADY_DAILY_CATALOG_URL.
They are skipped when that variable is absent.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
SCRAPER = REPO / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

import psycopg2  # noqa: E402
import psycopg2.errors  # noqa: E402

import jobready_catalog as catalog  # noqa: E402
import run_jobready_daily as daily  # noqa: E402
from normalize import UnifiedJob  # noqa: E402

CATALOG_URL = os.environ.get("JOBREADY_DAILY_CATALOG_URL", "").strip()

TABLE = "jobready_link_observations"
NEW_COLUMNS = ("last_confirmed_open_at", "last_confirmed_closed_at")
NEW_INDEXES = (
    "idx_jobready_link_observations_job_time",
    "idx_jobready_link_observations_observed_at",
)
NEW_NAMES = (TABLE, *NEW_COLUMNS, *NEW_INDEXES)
LABELS = ("confirmed_active", "confirmed_expired", "inconclusive")

EXPECTED_COLUMNS = {
    "id": ("bigint", "NO"),
    "job_id": ("text", "NO"),
    "observed_at": ("timestamp with time zone", "NO"),
    "recorded_at": ("timestamp with time zone", "NO"),
    "observation": ("text", "NO"),
    "reason": ("text", "NO"),
    "provider": ("text", "NO"),
    "previous_link_status": ("text", "YES"),
    "resulting_link_status": ("text", "YES"),
    "run_id": ("text", "YES"),
    "requested_url": ("text", "YES"),
    "final_url": ("text", "YES"),
    "http_status": ("integer", "YES"),
    "evidence_version": ("text", "NO"),
}
EXPECTED_INDEXDEFS = {
    "idx_jobready_link_observations_job_time": (
        "CREATE INDEX idx_jobready_link_observations_job_time ON public.jobready_link_observations "
        "USING btree (job_id, observed_at DESC, id DESC)"
    ),
    "idx_jobready_link_observations_observed_at": (
        "CREATE INDEX idx_jobready_link_observations_observed_at ON public.jobready_link_observations "
        "USING btree (observed_at DESC)"
    ),
}

DEACTIVATE_SQL = f"""
DROP TABLE IF EXISTS {TABLE};
ALTER TABLE validated_jobs
    DROP COLUMN IF EXISTS last_confirmed_open_at,
    DROP COLUMN IF EXISTS last_confirmed_closed_at;
"""

OBSERVED = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)


def _job(url: str, *, source: str = "naukri") -> UnifiedJob:
    return UnifiedJob(
        job_id="upstream-id",
        source=source,
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
class LifecycleSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.conn = psycopg2.connect(CATALOG_URL, connect_timeout=8)
        except Exception:
            raise unittest.SkipTest("disposable database unavailable") from None
        catalog.ensure_schema(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        catalog.ensure_schema(cls.conn)
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.rollback()
        catalog.ensure_schema(self.conn)
        self._exec(f"TRUNCATE {TABLE}, validated_jobs RESTART IDENTITY")
        self.addCleanup(self._restore_schema)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        for target in ("OUTPUT_DIR", "RUNS_DIR"):
            patcher = patch.object(daily, target, self.base / target.lower())
            patcher.start()
            self.addCleanup(patcher.stop)

    def _restore_schema(self) -> None:
        self.conn.rollback()
        catalog.ensure_schema(self.conn)

    def _exec(self, sql: str, params=None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
        self.conn.commit()

    def _fetch(self, sql: str, params=None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        self.conn.commit()
        return rows

    def _deactivate(self) -> None:
        """Simulate a catalog that predates Phase 1 (production before --apply-schema)."""
        self._exec(DEACTIVATE_SQL)
        self.assertFalse(self._phase1_present())

    def _phase1_present(self) -> bool:
        table = self._fetch("SELECT to_regclass(%s)", (f"public.{TABLE}",))[0][0]
        columns = self._validated_columns()
        return table is not None or any(name in columns for name in NEW_COLUMNS)

    def _validated_columns(self) -> list[str]:
        return [
            row[0]
            for row in self._fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'validated_jobs'
                ORDER BY ordinal_position
                """
            )
        ]

    def _seed(self, urls: list[str]) -> list[str]:
        rows = [daily.prepare_catalog_row(_job(url)) for url in urls]
        catalog.upsert_jobs(self.conn, rows)
        return [
            row[0]
            for row in self._fetch(
                "SELECT job_id FROM validated_jobs WHERE job_url = ANY(%s) ORDER BY job_url",
                (urls,),
            )
        ]

    def _snapshot(self, columns: list[str]) -> list[tuple]:
        listed = ", ".join(columns)
        return self._fetch(f"SELECT {listed} FROM validated_jobs ORDER BY id")

    def _observation_count(self) -> int:
        return self._fetch(f"SELECT COUNT(*) FROM {TABLE}")[0][0]

    def _confirmed(self) -> list[tuple]:
        return self._fetch(
            "SELECT last_confirmed_open_at, last_confirmed_closed_at FROM validated_jobs ORDER BY id"
        )

    def _insert(self, job_id: str, observation: str = "confirmed_active", **fields) -> None:
        values = {
            "job_id": job_id,
            "observed_at": OBSERVED,
            "observation": observation,
            "reason": "fixture",
            "provider": "naukri",
            "evidence_version": "test-v1",
            **fields,
        }
        names = ", ".join(values)
        marks = ", ".join(["%s"] * len(values))
        self._exec(f"INSERT INTO {TABLE} ({names}) VALUES ({marks})", tuple(values.values()))

    def _assert_rejected(self, error: type[Exception], job_id: str, observation: str = "confirmed_active", **fields):
        with self.assertRaises(error):
            self._insert(job_id, observation, **fields)
        self.conn.rollback()

    def _assert_phase1_shape(self) -> None:
        columns = {
            row[0]: (row[1], row[2])
            for row in self._fetch(
                """
                SELECT column_name, data_type, is_nullable FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                """,
                (TABLE,),
            )
        }
        self.assertEqual(columns, EXPECTED_COLUMNS)
        confirmed = {
            row[0]: (row[1], row[2], row[3])
            for row in self._fetch(
                """
                SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'validated_jobs'
                  AND column_name = ANY(%s)
                """,
                (list(NEW_COLUMNS),),
            )
        }
        for name in NEW_COLUMNS:
            self.assertEqual(confirmed[name], ("timestamp with time zone", "YES", None), name)
        indexes = dict(
            self._fetch(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = %s",
                (TABLE,),
            )
        )
        self.assertEqual(set(indexes), {f"{TABLE}_pkey", *EXPECTED_INDEXDEFS})
        for name, definition in EXPECTED_INDEXDEFS.items():
            self.assertEqual(indexes[name], definition)

    def _constraints(self) -> list[tuple]:
        return self._fetch(
            """
            SELECT conname, contype, pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conrelid = %s::regclass ORDER BY conname
            """,
            (f"public.{TABLE}",),
        )

    def _with_catalog_env(self, function):
        previous = os.environ.get("JOB_READY_DATABASE_URL")
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        output = io.StringIO()
        try:
            with redirect_stdout(output):
                code = function()
        finally:
            _restore_env("JOB_READY_DATABASE_URL", previous)
        return code, output.getvalue()

    # A, C, D, E: first application on a pre-Phase-1 catalog with existing rows.
    def test_first_application_adds_objects_without_touching_existing_rows(self) -> None:
        self._deactivate()
        self._seed(["https://example.test/jobs/a", "https://example.test/jobs/b"])
        self._exec(
            "UPDATE validated_jobs SET link_status = 'expired', link_checked_at = NOW(), "
            "last_link_observation = 'confirmed_expired' WHERE job_url = 'https://example.test/jobs/b'"
        )
        before_columns = self._validated_columns()
        before = self._snapshot(before_columns)

        catalog.ensure_schema(self.conn)

        self._assert_phase1_shape()
        self.assertEqual(self._snapshot(before_columns), before)
        self.assertEqual(self._validated_columns()[: len(before_columns)], before_columns)
        self.assertEqual(self._confirmed(), [(None, None), (None, None)])
        self.assertEqual(self._observation_count(), 0)

    def test_fresh_catalog_gets_phase1_objects(self) -> None:
        self._exec(f"DROP TABLE IF EXISTS {TABLE}; DROP TABLE IF EXISTS validated_jobs")
        catalog.ensure_schema(self.conn)
        self._assert_phase1_shape()
        self.assertEqual(self._observation_count(), 0)

    def test_apply_schema_command_creates_phase1_objects(self) -> None:
        self._deactivate()
        self._seed(["https://example.test/jobs/command"])
        code, output = self._with_catalog_env(daily.run_apply_schema)
        self.assertEqual(code, 0)
        self.assertIn("SCHEMA_APPLIED", output)
        self._assert_phase1_shape()
        self.assertEqual(self._confirmed(), [(None, None)])
        self.assertEqual(self._observation_count(), 0)

    # B
    def test_application_is_idempotent(self) -> None:
        job_id = self._seed(["https://example.test/jobs/idem"])[0]
        self._insert(job_id)
        columns = self._validated_columns()
        before = (self._snapshot(columns), self._constraints(), self._observation_count())
        catalog.ensure_schema(self.conn)
        catalog.apply_schema(self.conn)
        self._assert_phase1_shape()
        self.assertEqual(self._validated_columns(), columns)
        self.assertEqual((self._snapshot(columns), self._constraints(), self._observation_count()), before)

    def test_constraints_are_exactly_pk_fk_and_check(self) -> None:
        constraints = {name: (kind, definition) for name, kind, definition in self._constraints()}
        self.assertEqual(
            constraints,
            {
                f"{TABLE}_pkey": ("p", "PRIMARY KEY (id)"),
                f"{TABLE}_job_id_fkey": (
                    "f",
                    "FOREIGN KEY (job_id) REFERENCES validated_jobs(job_id) ON DELETE RESTRICT",
                ),
                f"{TABLE}_observation_check": (
                    "c",
                    "CHECK ((observation = ANY (ARRAY['confirmed_active'::text, "
                    "'confirmed_expired'::text, 'inconclusive'::text])))",
                ),
            },
        )
        unique_indexes = self._fetch(
            """
            SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
            WHERE i.indrelid = %s::regclass AND i.indisunique
            """,
            (f"public.{TABLE}",),
        )
        self.assertEqual(unique_indexes, [(f"{TABLE}_pkey",)])

    # F, G
    def test_check_accepts_exactly_three_labels(self) -> None:
        job_id = self._seed(["https://example.test/jobs/labels"])[0]
        for label in LABELS:
            self._insert(job_id, label)
        self.assertEqual(
            self._fetch(f"SELECT observation FROM {TABLE} ORDER BY id"),
            [(label,) for label in LABELS],
        )
        for label in (
            "",
            "active",
            "expired",
            "unknown",
            "timeout",
            "link_failed",
            "http_404",
            "CONFIRMED_ACTIVE",
            "confirmed_active ",
            "confirmed-expired",
        ):
            self._assert_rejected(psycopg2.errors.CheckViolation, job_id, label)
        self._assert_rejected(psycopg2.errors.NotNullViolation, job_id, None)
        self.assertEqual(self._observation_count(), 3)

    # H
    def test_repeated_identical_observations_are_appended(self) -> None:
        job_id = self._seed(["https://example.test/jobs/repeat"])[0]
        for _ in range(3):
            self._insert(job_id, "confirmed_active")
            self._insert(job_id, "inconclusive")
        self.assertEqual(self._observation_count(), 6)
        self.assertEqual(
            self._fetch(f"SELECT COUNT(DISTINCT observed_at) FROM {TABLE} WHERE job_id = %s", (job_id,)),
            [(1,)],
        )

    # I
    def test_foreign_key_requires_existing_job(self) -> None:
        self._assert_rejected(psycopg2.errors.ForeignKeyViolation, "CQJ-does-not-exist")
        self.assertEqual(self._observation_count(), 0)

    # J
    def test_deleting_a_job_with_observations_is_refused(self) -> None:
        job_id = self._seed(["https://example.test/jobs/restrict"])[0]
        self._insert(job_id)
        with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
            self._exec("DELETE FROM validated_jobs WHERE job_id = %s", (job_id,))
        self.conn.rollback()
        self.assertEqual(self._fetch("SELECT COUNT(*) FROM validated_jobs WHERE job_id = %s", (job_id,)), [(1,)])
        self.assertEqual(self._observation_count(), 1)
        self.assertEqual(
            self._fetch(
                "SELECT confdeltype FROM pg_constraint WHERE conname = %s",
                (f"{TABLE}_job_id_fkey",),
            ),
            [("r",)],
        )

    # K
    def test_provenance_is_nullable_and_required_fields_are_not(self) -> None:
        job_id = self._seed(["https://example.test/jobs/minimal"])[0]
        self._insert(job_id)
        row = self._fetch(
            f"""
            SELECT recorded_at, previous_link_status, resulting_link_status, run_id,
                   requested_url, final_url, http_status
            FROM {TABLE}
            """
        )[0]
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1:], (None,) * 6)
        for field in ("observed_at", "reason", "provider", "evidence_version"):
            self._assert_rejected(psycopg2.errors.NotNullViolation, job_id, **{field: None})
        self._assert_rejected(psycopg2.errors.NotNullViolation, None)
        self.assertEqual(self._observation_count(), 1)

    def test_observed_at_is_kept_separate_from_recorded_at(self) -> None:
        job_id = self._seed(["https://example.test/jobs/times"])[0]
        probed = datetime.now(timezone.utc) - timedelta(days=3)
        self._insert(job_id, observed_at=probed)
        observed_at, recorded_at = self._fetch(f"SELECT observed_at, recorded_at FROM {TABLE}")[0]
        self.assertEqual(observed_at, probed)
        self.assertGreater(recorded_at, probed + timedelta(days=2))

    def test_failed_activation_is_atomic(self) -> None:
        """A catalog whose job_id is not unique cannot take the FK; nothing is half-applied."""
        self._deactivate()
        unique = self._fetch(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'public.validated_jobs'::regclass AND contype = 'u'
              AND pg_get_constraintdef(oid) = 'UNIQUE (job_id)'
            """
        )
        self.assertEqual(len(unique), 1)
        name = unique[0][0]
        self._exec(f'ALTER TABLE validated_jobs DROP CONSTRAINT "{name}"')
        self.addCleanup(self._exec, f'ALTER TABLE validated_jobs ADD CONSTRAINT "{name}" UNIQUE (job_id)')
        with self.assertRaises(psycopg2.errors.InvalidForeignKey):
            catalog.ensure_schema(self.conn)
        self.conn.rollback()
        self.assertFalse(self._phase1_present())

    # P1.2: the runtime writes lifecycle evidence, so the daily run requires the Phase 1 objects.
    def test_daily_requirements_include_phase1_objects(self) -> None:
        for name in NEW_COLUMNS:
            self.assertIn(name, catalog.REQUIRED_COLUMNS)
        self.assertEqual(catalog.LIFECYCLE_TABLE, TABLE)
        self.assertEqual(set(catalog.LIFECYCLE_COLUMNS), set(EXPECTED_COLUMNS))
        self.assertEqual(catalog.LIFECYCLE_INDEXES, NEW_INDEXES)
        self.assertEqual(catalog.schema_problems(self.conn), [])

    def test_preflight_refuses_without_phase1_objects(self) -> None:
        self._deactivate()
        problems = catalog.schema_problems(self.conn)
        self.conn.rollback()
        self.assertIn("missing columns: last_confirmed_open_at, last_confirmed_closed_at", problems)
        self.assertIn(f"{TABLE} table is missing", problems)
        code, output = self._with_catalog_env(daily.run_preflight)
        self.assertEqual(code, 1)
        self.assertIn("PREFLIGHT_FAILED", output)
        self.assertFalse(self._phase1_present())

    def test_daily_refuses_without_phase1_objects_and_does_not_create_them(self) -> None:
        self._deactivate()
        calls: list[str] = []

        def collect(**_kwargs):
            calls.append("scrape")
            raise AssertionError("scrape ran without the lifecycle schema")

        previous = os.environ.get("JOB_READY_DATABASE_URL")
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        try:
            args = daily.parse_args(["--sources", "naukri", "--validation-limit", "10"])
            with patch.object(daily, "apply_schema", side_effect=AssertionError("daily applied schema")), patch.object(
                catalog, "ensure_schema", side_effect=AssertionError("daily ensured schema")
            ), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                daily.execute_daily(args, collect=collect, journal_dir=self.base / "runs")
        finally:
            _restore_env("JOB_READY_DATABASE_URL", previous)
        self.assertIn("SCHEMA_REQUIRED", str(raised.exception))
        self.assertEqual(calls, [])
        self.assertFalse(self._phase1_present())


class RuntimeReferenceTests(unittest.TestCase):
    def test_only_the_catalog_module_touches_phase1_objects(self) -> None:
        sources = [
            path
            for directory in (ROOT, SCRAPER, REPO / "link_validator_service")
            if directory.is_dir()
            for path in directory.rglob("*.py")
            if "tests" not in path.relative_to(REPO).parts and path.name != "jobready_catalog.py"
        ]
        self.assertTrue(sources)
        for path in sources:
            text = path.read_text(encoding="utf-8", errors="replace")
            for name in NEW_NAMES:
                self.assertNotIn(name, text, f"{name} referenced in {path.relative_to(REPO)}")


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
