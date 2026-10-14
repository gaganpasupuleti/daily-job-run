"""P1.2 lifecycle evidence recording: one atomic, set-based write per validation batch.

Database cases use disposable Postgres named by JOBREADY_DAILY_CATALOG_URL.
They are skipped when that variable is absent.
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError
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
from jobready_validator import JOBREADY_EVIDENCE_VERSION, LinkObservation, validate_candidates  # noqa: E402
from normalize import UnifiedJob  # noqa: E402
from run_all_sources import ScrapeResult  # noqa: E402

CATALOG_URL = os.environ.get("JOBREADY_DAILY_CATALOG_URL", "").strip()
TABLE = "jobready_link_observations"
T09 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
T10 = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
T11 = datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc)
LEDGER_COLUMNS = (
    "id",
    "job_id",
    "observed_at",
    "recorded_at",
    "observation",
    "reason",
    "provider",
    "previous_link_status",
    "resulting_link_status",
    "run_id",
    "requested_url",
    "final_url",
    "http_status",
    "evidence_version",
)


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


def _obs(job_id: str, observation: str, observed_at: datetime, *, reason: str = "fixture", previous: str = "active"):
    return LinkObservation(
        job_id=job_id,
        previous_status=previous,
        observation=observation,
        provider="naukri",
        reason=reason,
        observed_at=observed_at,
    )


class _CountingCursor:
    def __init__(self, inner, owner: "_CountingConnection") -> None:
        self._inner = inner
        self._owner = owner

    def execute(self, query, vars=None):
        self._owner.executes += 1
        return self._inner.execute(query, vars)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


class _CountingConnection:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.executes = 0

    def cursor(self, *args, **kwargs):
        return _CountingCursor(self._inner.cursor(*args, **kwargs), self)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _CommitFailingConnection:
    def __init__(self, inner) -> None:
        self._inner = inner

    def commit(self) -> None:
        raise psycopg2.OperationalError("server closed the connection during commit")

    def __getattr__(self, name):
        return getattr(self._inner, name)


class ObservationContractTests(unittest.TestCase):
    def test_evidence_version_is_one_stable_constant(self) -> None:
        self.assertEqual(JOBREADY_EVIDENCE_VERSION, "jobready-validator-v1")
        self.assertEqual(catalog.INJECTED_EVIDENCE_VERSION, "jobready-injected-v1")
        self.assertNotEqual(JOBREADY_EVIDENCE_VERSION, catalog.INJECTED_EVIDENCE_VERSION)

    def test_five_field_construction_still_works(self) -> None:
        before = datetime.now(timezone.utc)
        item = LinkObservation("CQJ-1", "active", "confirmed_active", "naukri", "naukri_explicit_open")
        self.assertEqual(item.evidence_version, JOBREADY_EVIDENCE_VERSION)
        self.assertIsNotNone(item.observed_at.utcoffset())
        self.assertEqual(item.observed_at.utcoffset(), timedelta(0))
        self.assertGreaterEqual(item.observed_at, before)
        with self.assertRaises(FrozenInstanceError):
            item.observed_at = T10  # type: ignore[misc]

    def test_validate_candidates_stamps_observed_at_after_the_probe(self) -> None:
        probed: list[datetime] = []

        def probe(row):
            probed.append(datetime.now(timezone.utc))
            return ("confirmed_expired", "naukri_explicit_closed", "naukri")

        rows = [
            {"job_id": "CQJ-a", "source": "naukri", "job_url": "https://example.test/a", "link_status": "active"},
            {"job_id": "CQJ-b", "source": "naukri", "job_url": "https://example.test/b", "link_status": "expired"},
        ]
        observations = validate_candidates(rows, probes={"naukri": probe})
        self.assertEqual(len(observations), 2)
        for item, probe_time in zip(observations, probed, strict=True):
            self.assertGreaterEqual(item.observed_at, probe_time)
            self.assertEqual(item.observed_at.utcoffset(), timedelta(0))
            self.assertEqual(item.evidence_version, JOBREADY_EVIDENCE_VERSION)
            self.assertEqual(item.provider, "naukri")
            self.assertEqual(item.reason, "naukri_explicit_closed")

    def test_probe_error_is_inconclusive_with_evidence_metadata(self) -> None:
        def probe(row):
            raise RuntimeError("provider exploded with a body dump")

        rows = [{"job_id": "CQJ-a", "source": "naukri", "job_url": "https://example.test/a", "link_status": "active"}]
        (item,) = validate_candidates(rows, probes={"naukri": probe})
        self.assertEqual((item.observation, item.reason, item.provider), ("inconclusive", "naukri_probe_error", "naukri"))
        self.assertEqual(item.evidence_version, JOBREADY_EVIDENCE_VERSION)

    def test_writer_rejects_malformed_evidence_before_any_sql(self) -> None:
        class _NoSql:
            def cursor(self):
                raise AssertionError("SQL attempted for a malformed batch")

        naive = LinkObservation("CQJ-1", "active", "confirmed_active", "naukri", "r", observed_at=datetime(2026, 10, 1))
        blank_version = LinkObservation("CQJ-1", "active", "confirmed_active", "naukri", "r", evidence_version=" ")
        duplicate = [_obs("CQJ-1", "confirmed_active", T10), _obs("CQJ-1", "inconclusive", T11)]
        for batch in ([naive], [blank_version], duplicate):
            with self.assertRaises(ValueError):
                catalog.apply_link_observations(_NoSql(), batch)
        self.assertEqual(catalog.apply_link_observations(_NoSql(), [])["recorded"], 0)


class ImmutabilityContractTests(unittest.TestCase):
    def test_runtime_only_inserts_into_the_ledger(self) -> None:
        sources = [
            path
            for directory in (ROOT, SCRAPER, REPO / "link_validator_service")
            if directory.is_dir()
            for path in directory.rglob("*.py")
            if "tests" not in path.relative_to(REPO).parts
        ]
        mutation = re.compile(
            rf"\b(?:UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|ALTER\s+TABLE|DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s+"
            rf"(?:ONLY\s+)?(?:public\.)?{TABLE}\b",
            re.IGNORECASE,
        )
        inserts = 0
        for path in sources:
            text = path.read_text(encoding="utf-8", errors="replace")
            self.assertIsNone(mutation.search(text), f"ledger mutation in {path.relative_to(REPO)}")
            inserts += len(re.findall(rf"INSERT\s+INTO\s+{TABLE}\b", text, re.IGNORECASE))
        self.assertEqual(inserts, 1)
        self.assertNotIn("ON CONFLICT", catalog.EVIDENCE_LEDGER_SQL.upper())


@unittest.skipUnless(CATALOG_URL, "disposable daily catalog is not configured")
class LifecycleWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.conn = psycopg2.connect(CATALOG_URL, connect_timeout=8)
        except Exception:
            raise unittest.SkipTest("disposable database unavailable") from None
        catalog.ensure_schema(cls.conn)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self.conn.rollback()
        catalog.ensure_schema(self.conn)
        self._exec(f"TRUNCATE {TABLE}, validated_jobs RESTART IDENTITY")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        for target in ("OUTPUT_DIR", "RUNS_DIR"):
            patcher = patch.object(daily, target, self.base / target.lower())
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def _seed(self, count: int, prefix: str = "job") -> list[str]:
        urls = [f"https://example.test/jobs/{prefix}-{index:04d}" for index in range(count)]
        with redirect_stdout(io.StringIO()):
            catalog.upsert_jobs(self.conn, [daily.prepare_catalog_row(_job(url)) for url in urls])
        by_url = dict(self._fetch("SELECT job_url, job_id FROM validated_jobs WHERE job_url = ANY(%s)", (urls,)))
        return [by_url[url] for url in urls]

    def _set(self, job_id: str, *, status: str = "active", opened=None, closed=None) -> None:
        self._exec(
            """
            UPDATE validated_jobs
            SET link_status = %s, last_confirmed_open_at = %s, last_confirmed_closed_at = %s
            WHERE job_id = %s
            """,
            (status, opened, closed, job_id),
        )

    def _state(self, job_id: str) -> dict:
        names = (
            "link_status",
            "last_confirmed_open_at",
            "last_confirmed_closed_at",
            "link_checked_at",
            "last_link_observation",
            "updated_at",
            "synced_at",
        )
        row = self._fetch(f"SELECT {', '.join(names)} FROM validated_jobs WHERE job_id = %s", (job_id,))[0]
        return dict(zip(names, row, strict=True))

    def _ledger(self, job_id: str | None = None) -> list[dict]:
        where, params = ("WHERE job_id = %s", (job_id,)) if job_id else ("", None)
        rows = self._fetch(f"SELECT {', '.join(LEDGER_COLUMNS)} FROM {TABLE} {where} ORDER BY id", params)
        return [dict(zip(LEDGER_COLUMNS, row, strict=True)) for row in rows]

    def _count(self) -> int:
        return self._fetch(f"SELECT COUNT(*) FROM {TABLE}")[0][0]

    def _apply(self, observations, **kwargs) -> dict:
        return catalog.apply_link_observations(self.conn, observations, **kwargs)

    # 16, A: the behavior matrix in one committed batch.
    def test_status_and_timestamp_matrix(self) -> None:
        a_active, a_expired, e_active, i_active, i_expired, untouched = self._seed(6)
        self._set(e_active, status="expired", closed=T09)
        self._set(i_active, status="active", opened=T09, closed=T09)
        self._set(i_expired, status="expired", opened=T09, closed=T09)
        before = {job_id: self._state(job_id) for job_id in (i_active, i_expired, untouched)}

        result = self._apply(
            [
                _obs(a_active, "confirmed_active", T10),
                _obs(a_expired, "confirmed_expired", T10),
                _obs(e_active, "confirmed_active", T10, previous="expired"),
                _obs(i_active, "inconclusive", T10),
                _obs(i_expired, "inconclusive", T10, previous="expired"),
            ],
            run_id="run-matrix",
        )

        self.assertEqual(
            result,
            {"checked": 5, "confirmed_active": 2, "confirmed_expired": 1, "inconclusive": 2, "recorded": 5},
        )
        expected = {
            a_active: ("active", T10, None, "active", "active"),
            a_expired: ("expired", None, T10, "active", "expired"),
            e_active: ("active", T10, T09, "expired", "active"),
            i_active: ("active", T09, T09, "active", "active"),
            i_expired: ("expired", T09, T09, "expired", "expired"),
        }
        for job_id, (status, opened, closed, previous, resulting) in expected.items():
            state = self._state(job_id)
            self.assertEqual(
                (state["link_status"], state["last_confirmed_open_at"], state["last_confirmed_closed_at"]),
                (status, opened, closed),
                job_id,
            )
            (row,) = self._ledger(job_id)
            self.assertEqual((row["previous_link_status"], row["resulting_link_status"]), (previous, resulting), job_id)
        self.assertEqual(self._count(), 5)
        self.assertEqual(self._state(untouched), before[untouched])
        self.assertEqual(self._ledger(untouched), [])
        for job_id in (i_active, i_expired):
            self.assertIsNotNone(self._state(job_id)["link_checked_at"])
            self.assertEqual(self._state(job_id)["updated_at"], before[job_id]["updated_at"])

    def test_previous_status_comes_from_the_catalog_row(self) -> None:
        (job_id,) = self._seed(1)
        self._set(job_id, status="expired")
        self._apply([_obs(job_id, "inconclusive", T10, previous="active")])
        (row,) = self._ledger(job_id)
        self.assertEqual((row["previous_link_status"], row["resulting_link_status"]), ("expired", "expired"))
        self.assertEqual(self._state(job_id)["link_status"], "expired")

    def test_ledger_row_content(self) -> None:
        live_id, historical_id = self._seed(2)
        historical = datetime.now(timezone.utc) - timedelta(days=3)
        live = datetime.now(timezone.utc)
        self._apply(
            [
                _obs(live_id, "confirmed_active", live, reason="naukri_explicit_open"),
                _obs(historical_id, "confirmed_expired", historical, reason="naukri_explicit_closed"),
            ],
            run_id="20261013T000000Z-abc",
        )
        (live_row,) = self._ledger(live_id)
        (old_row,) = self._ledger(historical_id)
        self.assertEqual(
            {key: live_row[key] for key in LEDGER_COLUMNS if key not in ("id", "recorded_at")},
            {
                "job_id": live_id,
                "observed_at": live,
                "observation": "confirmed_active",
                "reason": "naukri_explicit_open",
                "provider": "naukri",
                "previous_link_status": "active",
                "resulting_link_status": "active",
                "run_id": "20261013T000000Z-abc",
                "requested_url": None,
                "final_url": None,
                "http_status": None,
                "evidence_version": JOBREADY_EVIDENCE_VERSION,
            },
        )
        self.assertGreaterEqual(live_row["recorded_at"], live)
        self.assertEqual(old_row["observed_at"], historical)
        self.assertGreater(old_row["recorded_at"] - old_row["observed_at"], timedelta(days=2))
        self.assertEqual(live_row["recorded_at"], old_row["recorded_at"])
        self.assertEqual(self._state(historical_id)["last_confirmed_closed_at"], historical)

    def test_compatibility_fields_keep_their_meaning(self) -> None:
        job_id, injected_id = self._seed(2)
        before = self._state(job_id)
        self._apply([_obs(job_id, "confirmed_active", T10, reason="naukri_explicit_open"), (injected_id, "active", "timeout")])
        after = self._state(job_id)
        self.assertEqual(after["last_link_observation"], "confirmed_active:naukri_explicit_open")
        self.assertIsNotNone(after["link_checked_at"])
        self.assertEqual(after["synced_at"], before["synced_at"])
        self.assertEqual(after["updated_at"], before["updated_at"])
        injected = self._state(injected_id)
        self.assertEqual((injected["link_status"], injected["last_link_observation"]), ("active", "timeout"))
        (row,) = self._ledger(injected_id)
        self.assertEqual(
            (row["observation"], row["reason"], row["provider"], row["evidence_version"], row["run_id"]),
            ("inconclusive", "timeout", catalog.INJECTED_PROVIDER, catalog.INJECTED_EVIDENCE_VERSION, None),
        )
        self.assertEqual((row["requested_url"], row["final_url"], row["http_status"]), (None, None, None))

    def test_status_change_moves_updated_at(self) -> None:
        (job_id,) = self._seed(1)
        before = self._state(job_id)
        self._apply([_obs(job_id, "confirmed_expired", T10)])
        self.assertGreater(self._state(job_id)["updated_at"], before["updated_at"])

    def _assert_repeated(self, observation: str, column: str) -> None:
        (job_id,) = self._seed(1)
        self._apply([_obs(job_id, observation, T10)])
        first = self._ledger(job_id)
        self._apply([_obs(job_id, observation, T11)])
        self.assertEqual(self._state(job_id)[column], T11)
        self._apply([_obs(job_id, observation, T09)])
        self.assertEqual(self._state(job_id)[column], T11)
        rows = self._ledger(job_id)
        self.assertEqual([row["observed_at"] for row in rows], [T10, T11, T09])
        self.assertEqual(rows[0], first[0])

    def test_repeated_confirmed_active_appends_and_only_moves_forward(self) -> None:
        self._assert_repeated("confirmed_active", "last_confirmed_open_at")

    def test_repeated_confirmed_expired_appends_and_only_moves_forward(self) -> None:
        self._assert_repeated("confirmed_expired", "last_confirmed_closed_at")

    def test_out_of_order_active_never_moves_open_backward(self) -> None:
        (job_id,) = self._seed(1)
        self._set(job_id, opened=T10)
        self._apply([_obs(job_id, "confirmed_active", T09)])
        state = self._state(job_id)
        self.assertEqual((state["link_status"], state["last_confirmed_open_at"]), ("active", T10))
        self.assertEqual(self._ledger(job_id)[0]["observed_at"], T09)

    def test_out_of_order_closed_never_moves_closed_backward(self) -> None:
        (job_id,) = self._seed(1)
        self._set(job_id, status="expired", closed=T10)
        self._apply([_obs(job_id, "confirmed_expired", T09, previous="expired")])
        state = self._state(job_id)
        self.assertEqual((state["link_status"], state["last_confirmed_closed_at"]), ("expired", T10))
        self.assertEqual(self._ledger(job_id)[0]["observed_at"], T09)

    def _assert_status_kept(self, job_id: str, before: dict, *, observation: str, observed_at: datetime) -> None:
        after = self._state(job_id)
        self.assertEqual(after["link_status"], before["link_status"])
        self.assertEqual(after["updated_at"], before["updated_at"])
        (row,) = self._ledger(job_id)
        self.assertEqual((row["observation"], row["observed_at"]), (observation, observed_at))
        self.assertEqual(row["previous_link_status"], before["link_status"])
        self.assertEqual(row["resulting_link_status"], before["link_status"])

    def test_stale_confirmed_active_does_not_reopen_a_newer_closure(self) -> None:
        (job_id,) = self._seed(1)
        self._set(job_id, status="expired", opened=T09, closed=T11)
        before = self._state(job_id)
        result = self._apply([_obs(job_id, "confirmed_active", T10, previous="expired")])
        self._assert_status_kept(job_id, before, observation="confirmed_active", observed_at=T10)
        state = self._state(job_id)
        self.assertEqual((state["last_confirmed_open_at"], state["last_confirmed_closed_at"]), (T10, T11))
        self.assertEqual(result["confirmed_active"], 1)

    def test_stale_confirmed_expired_does_not_close_a_newer_open(self) -> None:
        (job_id,) = self._seed(1)
        self._set(job_id, status="active", opened=T11, closed=T09)
        before = self._state(job_id)
        result = self._apply([_obs(job_id, "confirmed_expired", T10)])
        self._assert_status_kept(job_id, before, observation="confirmed_expired", observed_at=T10)
        state = self._state(job_id)
        self.assertEqual((state["last_confirmed_open_at"], state["last_confirmed_closed_at"]), (T11, T10))
        self.assertEqual(result["confirmed_expired"], 1)

    def test_simultaneous_contradictory_evidence_keeps_the_status(self) -> None:
        expired_id, active_id = self._seed(2)
        self._set(expired_id, status="expired", closed=T10)
        self._set(active_id, status="active", opened=T10)
        before = {job_id: self._state(job_id) for job_id in (expired_id, active_id)}
        self._apply(
            [
                _obs(expired_id, "confirmed_active", T10, previous="expired"),
                _obs(active_id, "confirmed_expired", T10),
            ]
        )
        self._assert_status_kept(expired_id, before[expired_id], observation="confirmed_active", observed_at=T10)
        self._assert_status_kept(active_id, before[active_id], observation="confirmed_expired", observed_at=T10)

    def test_newer_confirmed_evidence_still_changes_status(self) -> None:
        reopen_id, close_id, legacy_expired_id, legacy_active_id = self._seed(4)
        self._set(reopen_id, status="expired", opened=T09, closed=T10)
        self._set(close_id, status="active", opened=T10, closed=T09)
        self._set(legacy_expired_id, status="expired")
        self._apply(
            [
                _obs(reopen_id, "confirmed_active", T11, previous="expired"),
                _obs(close_id, "confirmed_expired", T11),
                _obs(legacy_expired_id, "confirmed_active", T10, previous="expired"),
                _obs(legacy_active_id, "confirmed_expired", T10),
            ]
        )
        expected = {
            reopen_id: ("active", T11, T10, "expired"),
            close_id: ("expired", T10, T11, "active"),
            legacy_expired_id: ("active", T10, None, "expired"),
            legacy_active_id: ("expired", None, T10, "active"),
        }
        for job_id, (status, opened, closed, previous) in expected.items():
            state = self._state(job_id)
            self.assertEqual(
                (state["link_status"], state["last_confirmed_open_at"], state["last_confirmed_closed_at"]),
                (status, opened, closed),
                job_id,
            )
            (row,) = self._ledger(job_id)
            self.assertEqual((row["previous_link_status"], row["resulting_link_status"]), (previous, status), job_id)

    def test_stale_and_fresh_evidence_in_one_batch_resolve_per_row(self) -> None:
        stale_id, fresh_id, unknown_id = self._seed(3)
        for job_id in (stale_id, fresh_id):
            self._set(job_id, status="active", opened=T10)
        self._set(unknown_id, status="expired", closed=T10)
        before = self._state(unknown_id)
        result = self._apply(
            [
                _obs(stale_id, "confirmed_expired", T09),
                _obs(fresh_id, "confirmed_expired", T11),
                _obs(unknown_id, "inconclusive", T11, previous="expired"),
            ],
            run_id="run-ordering",
        )
        self.assertEqual(
            result, {"checked": 3, "confirmed_active": 0, "confirmed_expired": 2, "inconclusive": 1, "recorded": 3}
        )
        self.assertEqual(self._state(stale_id)["link_status"], "active")
        self.assertEqual(self._state(fresh_id)["link_status"], "expired")
        after = self._state(unknown_id)
        self.assertEqual(
            (after["link_status"], after["last_confirmed_open_at"], after["last_confirmed_closed_at"]),
            (before["link_status"], before["last_confirmed_open_at"], before["last_confirmed_closed_at"]),
        )
        self.assertEqual(
            sorted((row["job_id"], row["resulting_link_status"]) for row in self._ledger()),
            sorted([(stale_id, "active"), (fresh_id, "expired"), (unknown_id, "expired")]),
        )

    def test_injected_confirmed_expired_still_applies_now(self) -> None:
        (job_id,) = self._seed(1)
        self._set(job_id, status="active", opened=T10)
        self.assertEqual(catalog.mark_confirmed_expired(self.conn, [job_id]), 1)
        state = self._state(job_id)
        self.assertEqual(state["link_status"], "expired")
        (row,) = self._ledger(job_id)
        self.assertEqual((row["provider"], row["resulting_link_status"]), (catalog.INJECTED_PROVIDER, "expired"))

    # 15 B: ledger failure rolls back the summary.
    def test_ledger_failure_rolls_back_status_and_timestamps(self) -> None:
        (job_id,) = self._seed(1)
        before = self._state(job_id)
        commits: list[str] = []
        with self.assertRaises(psycopg2.errors.ForeignKeyViolation):
            self._apply(
                [_obs(job_id, "confirmed_expired", T10), _obs("CQJ-missing", "confirmed_active", T10)],
                on_commit=lambda: commits.append("commit"),
            )
        self.assertEqual(commits, [])
        self.assertEqual(self._state(job_id), before)
        self.assertEqual(self._count(), 0)

    # 15 C: summary failure rolls back the ledger.
    def test_summary_failure_rolls_back_ledger(self) -> None:
        (job_id,) = self._seed(1)
        before = self._state(job_id)
        # Raises only once this transaction's ledger row is visible, so the failure proves the
        # insert happened before the summary update.
        self._exec(
            f"""
            CREATE FUNCTION p12_fail_summary() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF EXISTS (SELECT 1 FROM {TABLE} WHERE job_id = NEW.job_id) THEN
                    RAISE EXCEPTION 'p12 forced summary failure after ledger insert';
                END IF;
                RETURN NEW;
            END
            $$;
            CREATE TRIGGER p12_fail_summary BEFORE UPDATE ON validated_jobs
                FOR EACH ROW EXECUTE FUNCTION p12_fail_summary();
            """
        )
        self.addCleanup(
            self._exec,
            "DROP TRIGGER IF EXISTS p12_fail_summary ON validated_jobs; DROP FUNCTION IF EXISTS p12_fail_summary();",
        )
        commits: list[str] = []
        with self.assertRaises(psycopg2.errors.RaiseException) as raised:
            self._apply([_obs(job_id, "confirmed_expired", T10)], on_commit=lambda: commits.append("commit"))
        self.assertIn("after ledger insert", str(raised.exception))
        self.assertEqual(commits, [])
        self.assertEqual(self._count(), 0)
        self.assertEqual(self._state(job_id), before)

    def test_commit_failure_records_nothing_and_skips_on_commit(self) -> None:
        (job_id,) = self._seed(1)
        before = self._state(job_id)
        commits: list[str] = []
        with self.assertRaises(psycopg2.OperationalError):
            catalog.apply_link_observations(
                _CommitFailingConnection(self.conn),
                [_obs(job_id, "confirmed_expired", T10)],
                on_commit=lambda: commits.append("commit"),
            )
        self.assertEqual(commits, [])
        self.assertEqual(self._state(job_id), before)
        self.assertEqual(self._count(), 0)

    def test_on_commit_runs_once_after_the_commit_is_durable(self) -> None:
        job_ids = self._seed(3)
        seen: list[int] = []

        def on_commit() -> None:
            other = psycopg2.connect(CATALOG_URL, connect_timeout=8)
            try:
                with other.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM {TABLE}")
                    seen.append(cur.fetchone()[0])
            finally:
                other.close()

        result = self._apply([_obs(job_id, "confirmed_active", T10) for job_id in job_ids], on_commit=on_commit)
        self.assertEqual(seen, [3])
        self.assertEqual(result["recorded"], self._count())

    # 19: at least 200 observations with bounded, size-independent SQL.
    def test_200_observation_batch_uses_bounded_set_based_sql(self) -> None:
        job_ids = self._seed(220, prefix="bulk")
        labels = ("confirmed_active", "confirmed_expired", "inconclusive")
        expired = set(job_ids[::4])
        for job_id in expired:
            self._set(job_id, status="expired")
        small = _CountingConnection(self.conn)
        catalog.apply_link_observations(small, [_obs(job_id, "inconclusive", T09) for job_id in job_ids[200:]])
        observations = [_obs(job_id, labels[index % 3], T10) for index, job_id in enumerate(job_ids[:200])]
        large = _CountingConnection(self.conn)
        result = catalog.apply_link_observations(large, observations, run_id="run-bulk")

        self.assertEqual(large.executes, small.executes)
        self.assertLessEqual(large.executes, 10)
        self.assertEqual(result["recorded"], 200)
        self.assertEqual(result["checked"], 200)
        self.assertEqual(self._fetch(f"SELECT COUNT(*) FROM {TABLE} WHERE run_id = 'run-bulk'"), [(200,)])
        rows = {
            row[0]: row[1:]
            for row in self._fetch(
                f"""
                SELECT v.job_id, v.link_status, v.last_confirmed_open_at, v.last_confirmed_closed_at,
                       o.previous_link_status, o.resulting_link_status
                FROM validated_jobs v JOIN {TABLE} o ON o.job_id = v.job_id AND o.run_id = 'run-bulk'
                """
            )
        }
        self.assertEqual(len(rows), 200)
        for index, job_id in enumerate(job_ids[:200]):
            label = labels[index % 3]
            previous = "expired" if job_id in expired else "active"
            if label == "confirmed_active":
                expected = ("active", T10, None, previous, "active")
            elif label == "confirmed_expired":
                expected = ("expired", None, T10, previous, "expired")
            else:
                expected = (previous, None, None, previous, previous)
            self.assertEqual(rows[job_id], expected, job_id)

    def test_canonical_daily_path_records_evidence_with_run_id(self) -> None:
        urls = [f"https://example.test/jobs/daily-{index}" for index in range(3)]
        verdicts = dict(zip(urls, ("confirmed_active", "confirmed_expired", "inconclusive"), strict=True))
        health = {"naukri": {"attempted": True, "succeeded": True, "failed": False, "jobs_returned": 3, "errors": []}}

        def collect(**_kwargs):
            return ScrapeResult(
                jobs=[_job(url) for url in urls],
                per_source={"naukri": 3},
                per_family={"python-dev": 3},
                errors=[],
                sources_attempted=["naukri"],
                started_at="2026-10-10T00:00:00+00:00",
                finished_at="2026-10-10T00:00:01+00:00",
                source_health=health,
            )

        def probe(row):
            return (verdicts[row["job_url"]], "fixture_verdict", "naukri")

        previous = os.environ.get("JOB_READY_DATABASE_URL")
        os.environ["JOB_READY_DATABASE_URL"] = CATALOG_URL
        started = datetime.now(timezone.utc)
        try:
            args = daily.parse_args(["--sources", "naukri", "--validation-limit", "10"])
            with redirect_stdout(io.StringIO()):
                report = daily.execute_daily(
                    args, collect=collect, probes={"naukri": probe, "generic": probe}, journal_dir=self.base / "runs"
                )
        finally:
            if previous is None:
                os.environ.pop("JOB_READY_DATABASE_URL", None)
            else:
                os.environ["JOB_READY_DATABASE_URL"] = previous

        self.assertTrue(report["run_id"])
        ledger = self._ledger()
        self.assertEqual(len(ledger), 3)
        self.assertEqual(report["lifecycle_observations_recorded"], 3)
        self.assertEqual(report["validated_checked"], 3)
        self.assertEqual(report["confirmed_active"], 1)
        self.assertEqual(report["confirmed_expired"], 1)
        self.assertEqual(report["unknown_or_inconclusive"], 1)
        self.assertEqual(
            report["validation_reasons"],
            {
                "confirmed_active:fixture_verdict": 1,
                "confirmed_expired:fixture_verdict": 1,
                "inconclusive:fixture_verdict": 1,
            },
        )
        url_of = dict(self._fetch("SELECT job_id, job_url FROM validated_jobs"))
        for row in ledger:
            self.assertEqual(row["run_id"], report["run_id"])
            self.assertEqual(row["provider"], "naukri")
            self.assertEqual(row["reason"], "fixture_verdict")
            self.assertEqual(row["evidence_version"], JOBREADY_EVIDENCE_VERSION)
            self.assertEqual(row["observation"], verdicts[url_of[row["job_id"]]])
            self.assertGreaterEqual(row["observed_at"], started)
            self.assertGreaterEqual(row["recorded_at"], row["observed_at"])
        states = {url_of[job_id]: self._state(job_id) for job_id in url_of}
        self.assertEqual(states[urls[0]]["link_status"], "active")
        self.assertIsNotNone(states[urls[0]]["last_confirmed_open_at"])
        self.assertEqual(states[urls[1]]["link_status"], "expired")
        self.assertIsNotNone(states[urls[1]]["last_confirmed_closed_at"])
        self.assertEqual(
            (states[urls[2]]["last_confirmed_open_at"], states[urls[2]]["last_confirmed_closed_at"]), (None, None)
        )


@unittest.skipUnless(CATALOG_URL, "disposable daily catalog is not configured")
class LifecycleSchemaRequirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.conn = psycopg2.connect(CATALOG_URL, connect_timeout=8)
        except Exception:
            raise unittest.SkipTest("disposable database unavailable") from None

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()

    def setUp(self) -> None:
        self._restore()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        self.conn.rollback()
        with self.conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        self.conn.commit()
        catalog.ensure_schema(self.conn)

    def _problems_after(self, sql: str) -> list[str]:
        with self.conn.cursor() as cur:
            cur.execute(sql)
        self.conn.commit()
        problems = catalog.schema_problems(self.conn)
        self.conn.rollback()
        return problems

    def test_activated_schema_passes(self) -> None:
        self.assertEqual(catalog.schema_problems(self.conn), [])

    def test_incomplete_lifecycle_schema_is_detected(self) -> None:
        cases = {
            f"ALTER TABLE {TABLE} DROP CONSTRAINT {TABLE}_job_id_fkey": "foreign key",
            (
                f"ALTER TABLE {TABLE} DROP CONSTRAINT {TABLE}_job_id_fkey, "
                f"ADD CONSTRAINT {TABLE}_job_id_fkey FOREIGN KEY (job_id) "
                "REFERENCES validated_jobs (job_id) ON DELETE CASCADE"
            ): "foreign key",
            f"ALTER TABLE {TABLE} DROP CONSTRAINT {TABLE}_observation_check": "observation check",
            (
                f"ALTER TABLE {TABLE} DROP CONSTRAINT {TABLE}_observation_check, "
                f"ADD CONSTRAINT {TABLE}_observation_check CHECK (observation IN "
                "('confirmed_active', 'confirmed_expired', 'inconclusive', 'unknown'))"
            ): "observation check",
            "DROP INDEX idx_jobready_link_observations_job_time": "idx_jobready_link_observations_job_time",
            "DROP INDEX idx_jobready_link_observations_observed_at": "idx_jobready_link_observations_observed_at",
            f"ALTER TABLE {TABLE} DROP COLUMN evidence_version": "evidence_version",
            f"ALTER TABLE {TABLE} DROP COLUMN run_id": "run_id",
            "ALTER TABLE validated_jobs DROP COLUMN last_confirmed_closed_at": "last_confirmed_closed_at",
            f"DROP TABLE {TABLE}": "table is missing",
        }
        for sql, expected in cases.items():
            with self.subTest(sql=sql):
                problems = self._problems_after(sql)
                self.assertTrue(any(expected in problem for problem in problems), problems)
                self._restore()
                self.assertEqual(catalog.schema_problems(self.conn), [])
                self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
