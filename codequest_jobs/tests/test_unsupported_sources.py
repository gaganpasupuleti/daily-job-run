"""Sources without a lifecycle probe are explicitly unsupported, not healthy validators.

Pure tests need no database. CatalogSelectionTests and DailyUnsupportedTests use
disposable Postgres named by JOBREADY_DAILY_CATALOG_URL and are skipped when it is absent.
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRAPER = ROOT.parent / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

import psycopg2  # noqa: E402

import audit_job_ready  # noqa: E402
import jobready_catalog as catalog  # noqa: E402
import jobready_validator as validator  # noqa: E402
import run_jobready_daily as daily  # noqa: E402
from link_policy import (  # noqa: E402
    SUPPORTED,
    SUPPORTED_LIFECYCLE_SOURCES,
    UNSUPPORTED,
    lifecycle_capability,
    source_label,
)

CATALOG_URL = os.environ.get("JOBREADY_DAILY_CATALOG_URL", "").strip()
SUPPORTED_CASES = ("linkedin", "LinkedIn", "  LINKEDIN ", "naukri", "Naukri", "indeed", "INDEED")
UNSUPPORTED_CASES = ("google", "Google", "foundit", "unknown", "generic", "", "   ", None, 7, "future-board", "linkedin.com")
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _row(job_id: str, source, url: str = "https://jobs.example.test/1") -> dict:
    return {"job_id": job_id, "source": source, "job_url": url, "apply_url": None, "link_status": "active"}


class _Network:
    """Records and refuses every outbound connection attempt."""

    def __init__(self, stack: ExitStack) -> None:
        self.attempts: list[str] = []
        stack.enter_context(mock.patch.object(socket.socket, "connect", side_effect=self._refuse))
        stack.enter_context(mock.patch.object(socket, "create_connection", side_effect=self._refuse))
        stack.enter_context(mock.patch.object(validator.httpx, "Client", side_effect=self._refuse))

    def _refuse(self, *_args, **_kwargs):
        self.attempts.append("connect")
        raise AssertionError("no network request is allowed for an unsupported source")


class CapabilityTests(unittest.TestCase):
    def test_supported_sources_are_exactly_those_with_a_provider_probe(self) -> None:
        self.assertEqual(SUPPORTED_LIFECYCLE_SOURCES, frozenset({"linkedin", "naukri", "indeed"}))
        self.assertEqual(set(validator.default_probes()), set(SUPPORTED_LIFECYCLE_SOURCES))
        self.assertIs(validator.default_probes()["linkedin"], validator.probe_linkedin)
        self.assertIs(validator.default_probes()["naukri"], validator.probe_naukri)
        self.assertIs(validator.default_probes()["indeed"], validator.probe_indeed)
        self.assertNotIn("generic", validator.default_probes())

    def test_classification_is_normalized_and_deterministic(self) -> None:
        for source in SUPPORTED_CASES:
            self.assertEqual(lifecycle_capability(source), SUPPORTED, repr(source))
            self.assertIsNotNone(validator.probe_for(source), repr(source))
        for source in UNSUPPORTED_CASES:
            self.assertEqual(lifecycle_capability(source), UNSUPPORTED, repr(source))
            self.assertIsNone(validator.probe_for(source), repr(source))
        first = [lifecycle_capability(source) for source in SUPPORTED_CASES + UNSUPPORTED_CASES]
        second = [lifecycle_capability(source) for source in SUPPORTED_CASES + UNSUPPORTED_CASES]
        self.assertEqual(first, second)

    def test_source_labels_are_bounded(self) -> None:
        self.assertEqual(source_label("Google "), "google")
        self.assertEqual(source_label(""), "unknown")
        self.assertEqual(source_label(None), "unknown")
        self.assertEqual(source_label("future-board"), "future-board")
        self.assertEqual(source_label("https://evil.example/?token=secret"), "other")
        self.assertEqual(source_label("x" * 33), "other")


class ValidatorDispatchTests(unittest.TestCase):
    def test_unsupported_rows_are_refused_before_any_probe_or_network(self) -> None:
        calls: list[str] = []
        probes = {"linkedin": lambda row: calls.append(row["job_id"]) or ("confirmed_active", "fixture", "linkedin")}
        batches = (
            [_row("U1", "google")],
            [_row("U1", "foundit"), _row("U2", ""), _row("U3", None), _row("U4", "future-board")],
            [_row("S1", "linkedin"), _row("U1", "google")],
        )
        for rows in batches:
            with ExitStack() as stack:
                network = _Network(stack)
                for chosen in (None, probes):
                    with self.assertRaises(validator.UnsupportedSourceError) as caught:
                        validator.validate_candidates(rows, probes=chosen)
                    self.assertEqual(str(caught.exception), "unsupported_source")
            self.assertEqual(network.attempts, [])
            self.assertEqual(calls, [])

    def test_refusal_names_sources_only_by_bounded_label(self) -> None:
        rows = [_row("U1", "Google"), _row("U2", "foundit"), _row("U3", ""), _row("U4", "https://x.example/?token=SECRET")]
        with self.assertRaises(validator.UnsupportedSourceError) as caught:
            validator.validate_candidates(rows)
        self.assertEqual(caught.exception.sources, ["foundit", "google", "other", "unknown"])
        self.assertNotIn("SECRET", repr(caught.exception) + str(caught.exception))

    def test_injected_probes_still_dispatch_by_source(self) -> None:
        (item,) = validator.validate_candidates(
            [_row("T1", "fixture-board")], probes={"fixture-board": lambda row: ("confirmed_active", "fixture", "fixture")}
        )
        self.assertEqual((item.observation, item.reason, item.provider), ("confirmed_active", "fixture", "fixture"))
        (item,) = validator.validate_candidates(
            [_row("S1", " LinkedIn ")], probes={"linkedin": lambda row: ("confirmed_expired", "fixture", "linkedin")}
        )
        self.assertEqual(item.observation, "confirmed_expired")

    def test_no_generic_fallback_even_when_injected(self) -> None:
        with self.assertRaises(validator.UnsupportedSourceError):
            validator.validate_candidates(
                [_row("U1", "google")], probes={"generic": lambda row: ("confirmed_active", "fixture", "google")}
            )

    def test_supported_probe_error_reason_is_unchanged(self) -> None:
        def broken(_row):
            raise ValueError("markup")

        (item,) = validator.validate_candidates([_row("S1", "linkedin")], probes={"linkedin": broken})
        self.assertEqual((item.observation, item.reason, item.provider), ("inconclusive", "linkedin_probe_error", "linkedin"))


class AuditUnsupportedTests(unittest.TestCase):
    def test_audit_labels_unsupported_sources_without_probing(self) -> None:
        calls: list[str] = []
        probes = {"linkedin": lambda row: calls.append(row["job_id"]) or ("confirmed_active", "fixture", "linkedin")}
        for source in ("google", "foundit", "", None, "future-board"):
            for chosen in (None, probes):
                with ExitStack() as stack:
                    network = _Network(stack)
                    result = audit_job_ready.audit_row(_row("U1", source), probes=chosen)
                self.assertEqual(
                    (result["observation"], result["reason"], result["provider"]),
                    ("unsupported", "unsupported_source", None),
                    source,
                )
                self.assertNotIn(result["observation"], ("confirmed_active", "confirmed_expired", "inconclusive"))
                self.assertEqual(network.attempts, [])
        self.assertEqual(calls, [])

    def test_audit_report_separates_unsupported_from_inconclusive(self) -> None:
        rows = [
            _row("S1", "linkedin"),
            _row("S2", "naukri"),
            _row("U1", "google"),
            _row("U2", "foundit"),
            _row("U3", ""),
        ]
        probed: list[str] = []

        def linkedin(row):
            probed.append(row["job_id"])
            return ("confirmed_active", "fixture_open", "linkedin")

        def naukri(row):
            probed.append(row["job_id"])
            return ("inconclusive", "naukri_http_500", "naukri")

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            network = _Network(stack)
            stack.enter_context(mock.patch.object(audit_job_ready, "ROOT", Path(tmp)))
            stack.enter_context(mock.patch.object(audit_job_ready, "load_env"))
            stack.enter_context(mock.patch.object(audit_job_ready, "fetch_job_ready_rows", return_value=rows))
            stack.enter_context(
                mock.patch.object(audit_job_ready, "default_probes", return_value={"linkedin": linkedin, "naukri": naukri})
            )
            stack.enter_context(
                mock.patch.object(audit_job_ready.psycopg2, "connect", side_effect=AssertionError("no database write"))
            )
            stack.enter_context(redirect_stdout(stdout))
            code = audit_job_ready.main([])
            report = json.loads(next((Path(tmp) / "output").glob("job_ready_audit_*.json")).read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(probed), ["S1", "S2"])
        self.assertEqual(network.attempts, [])
        self.assertEqual(
            {key: report[key] for key in ("checked", "confirmed_active", "confirmed_expired", "inconclusive", "unsupported")},
            {"checked": 2, "confirmed_active": 1, "confirmed_expired": 0, "inconclusive": 1, "unsupported": 3},
        )
        self.assertEqual(report["unsupported_by_source"], {"foundit": 1, "google": 1, "unknown": 1})
        self.assertEqual(report["validation_reasons"]["unsupported:unsupported_source"], 3)
        self.assertEqual(report["queued"], report["checked"] + report["unsupported"])


class _CatalogFixture(unittest.TestCase):
    STATE = (
        "link_status",
        "link_checked_at",
        "last_link_observation",
        "last_confirmed_open_at",
        "last_confirmed_closed_at",
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
        self._serial = 0

    def _seed(self, source: str, count: int, *, checked: bool = True, status: str = "active") -> list[str]:
        ids: list[str] = []
        with self.conn.cursor() as cur:
            for _ in range(count):
                self._serial += 1
                job_id = f"CQJ-U-{self._serial:04d}"
                cur.execute(
                    """
                    INSERT INTO validated_jobs
                        (job_id, source, title, job_url, link_status, link_checked_at, last_link_observation,
                         last_confirmed_open_at, last_confirmed_closed_at)
                    VALUES (%s, %s, 'Fixture', %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id,
                        source,
                        f"https://jobs.example.test/{self._serial}",
                        status,
                        T0 + timedelta(hours=self._serial) if checked else None,
                        "inconclusive:fixture" if checked else None,
                        T0 if checked else None,
                        T0 - timedelta(days=1) if checked else None,
                    ),
                )
                ids.append(job_id)
        self.conn.commit()
        return ids

    def _fetch(self, sql: str, params=None) -> list[tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        self.conn.commit()
        return rows

    def _states(self, job_ids: list[str]) -> dict[str, tuple]:
        rows = self._fetch(
            f"SELECT job_id, {', '.join(self.STATE)} FROM validated_jobs WHERE job_id = ANY(%s)", (job_ids,)
        )
        return {row[0]: row[1:] for row in rows}

    def _select(self, **kwargs) -> list[str]:
        rows = catalog.select_validation_candidates(self.conn, **kwargs)
        self.conn.rollback()
        return [row["job_id"] for row in rows]


@unittest.skipUnless(CATALOG_URL, "disposable daily catalog is not configured")
class CatalogSelectionTests(_CatalogFixture):
    def test_unsupported_rows_do_not_consume_the_normal_budget(self) -> None:
        self._seed("google", 4)
        self._seed("foundit", 4)
        linkedin = self._seed("linkedin", 4)
        naukri = self._seed("naukri", 4)
        self.assertEqual(self._select(limit=8, priority_ids=[]), linkedin + naukri)
        self.assertEqual(self._select(limit=3, priority_ids=[]), linkedin[:3])
        self.assertEqual(self._select(limit=200, priority_ids=[]), linkedin + naukri)

    def test_supported_sources_are_matched_case_insensitively(self) -> None:
        self._seed("google", 2)
        mixed = self._seed(" LinkedIn ", 1) + self._seed("NAUKRI", 1) + self._seed("Indeed", 1)
        self.assertEqual(self._select(limit=5, priority_ids=[]), mixed)

    def test_unsupported_priority_ids_do_not_consume_the_priority_budget(self) -> None:
        google = self._seed("google", 3)
        foundit = self._seed("foundit", 3)
        linkedin = self._seed("linkedin", 4)
        fresh = self._seed("naukri", 1, checked=False)
        priority = google + foundit + fresh
        selected = self._select(limit=4, priority_ids=priority)
        self.assertEqual(selected, fresh + linkedin[:3])
        selected = self._select(limit=2, priority_ids=google[:2] + fresh)
        self.assertEqual(selected, fresh + linkedin[:1])

    def test_supported_ordering_is_deterministic(self) -> None:
        self._seed("google", 2, checked=False)
        unchecked = self._seed("naukri", 2, checked=False) + self._seed("linkedin", 2, checked=False)
        checked = self._seed("indeed", 2)
        first = self._select(limit=6, priority_ids=[])
        self.assertEqual(first, sorted(unchecked) + checked)
        self.assertEqual(self._select(limit=6, priority_ids=[]), first)

    def test_only_unsupported_rows_select_nothing(self) -> None:
        unsupported = self._seed("google", 3) + self._seed("foundit", 3) + self._seed("", 1) + self._seed("future", 1)
        self.assertEqual(self._select(limit=200, priority_ids=unsupported), [])
        self.assertEqual(self._select(limit=200, full_audit=True), [])

    def test_full_audit_selects_every_supported_row_and_no_unsupported_row(self) -> None:
        self._seed("google", 2)
        supported = self._seed("linkedin", 2) + self._seed("naukri", 2)
        self._seed("foundit", 2)
        self.assertEqual(self._select(limit=1, full_audit=True), supported)

    def test_capability_counts_reconcile(self) -> None:
        self._seed("google", 2)
        self._seed("foundit", 3)
        self._seed("", 1)
        self._seed("linkedin", 2)
        self._seed("Naukri", 1, status="expired")
        counts = catalog.catalog_counts(self.conn)
        self.conn.rollback()
        self.assertEqual((counts["total"], counts["active"], counts["expired"]), (9, 8, 1))
        self.assertEqual((counts["supported"], counts["unsupported"]), (3, 6))
        self.assertEqual(counts["supported"] + counts["unsupported"], counts["total"])
        self.assertEqual(counts["unsupported_by_source"], {"foundit": 3, "google": 2, "unknown": 1})
        self.assertEqual(sum(counts["unsupported_by_source"].values()), counts["unsupported"])


@unittest.skipUnless(CATALOG_URL, "disposable daily catalog is not configured")
class DailyUnsupportedTests(_CatalogFixture):
    def _run(self, *, probes, **kwargs) -> tuple[dict, list[str]]:
        probed: list[str] = []

        def recording(probe):
            def run(row):
                probed.append(row["job_id"])
                return probe(row)

            return run

        chosen = None if probes is None else {source: recording(probe) for source, probe in probes.items()}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            network = _Network(stack)
            stack.enter_context(mock.patch.object(daily, "OUTPUT_DIR", Path(tmp)))
            stack.enter_context(redirect_stdout(io.StringIO()))
            report = daily.run_daily(catalog_conn=self.conn, jobs=[], validate=True, probes=chosen, **kwargs)
        self.assertEqual(network.attempts, [])
        return report, probed

    def _assert_untouched(self, before: dict[str, tuple], unsupported: list[str]) -> None:
        self.assertEqual(self._states(unsupported), before)
        self.assertEqual(
            self._fetch("SELECT COUNT(*) FROM jobready_link_observations WHERE job_id = ANY(%s)", (unsupported,)),
            [(0,)],
        )

    def test_normal_run_validates_supported_rows_and_reports_unsupported_separately(self) -> None:
        unsupported = self._seed("google", 4) + self._seed("foundit", 4)
        linkedin = self._seed("linkedin", 4)
        naukri = self._seed("naukri", 4)
        before = self._states(unsupported)
        report, probed = self._run(
            probes={
                "linkedin": lambda row: ("confirmed_active", "fixture_open", "linkedin"),
                "naukri": lambda row: ("inconclusive", "naukri_http_500", "naukri"),
            },
            validation_limit=8,
        )
        self.assertEqual(sorted(probed), sorted(linkedin + naukri))
        self.assertEqual(report["validation_selected"], 8)
        self.assertEqual(report["validated_checked"], 8)
        self.assertEqual(report["confirmed_active"], 4)
        self.assertEqual(report["confirmed_expired"], 0)
        self.assertEqual(report["unknown_or_inconclusive"], 4)
        self.assertEqual(report["lifecycle_observations_recorded"], 8)
        self.assertEqual(
            report["validation_reasons"], {"confirmed_active:fixture_open": 4, "inconclusive:naukri_http_500": 4}
        )
        self.assertEqual(report["validation_supported_sources"], ["indeed", "linkedin", "naukri"])
        self.assertEqual(report["catalog_supported_rows"], 8)
        self.assertEqual(report["catalog_unsupported_rows"], 8)
        self.assertEqual(report["catalog_unsupported_by_source"], {"foundit": 4, "google": 4})
        self.assertEqual(report["catalog_total"], report["catalog_supported_rows"] + report["catalog_unsupported_rows"])
        self._assert_untouched(before, unsupported)
        ledger_ids = {row[0] for row in self._fetch("SELECT job_id FROM jobready_link_observations")}
        self.assertEqual(ledger_ids, set(linkedin + naukri))

    def test_full_audit_never_records_unsupported_rows(self) -> None:
        unsupported = self._seed("google", 3) + self._seed("", 1) + self._seed("future-board", 1)
        supported = self._seed("indeed", 2) + self._seed("linkedin", 2)
        before = self._states(unsupported)
        report, probed = self._run(
            probes={
                "indeed": lambda row: ("confirmed_expired", "fixture_closed", "indeed"),
                "linkedin": lambda row: ("inconclusive", "linkedin_http_429", "linkedin"),
            },
            full_audit=True,
        )
        self.assertEqual(sorted(probed), sorted(supported))
        self.assertEqual(report["validation_selected"], 4)
        self.assertEqual(report["validated_checked"], 4)
        self.assertEqual((report["confirmed_expired"], report["unknown_or_inconclusive"]), (2, 2))
        self.assertEqual(report["catalog_unsupported_rows"], 5)
        self.assertEqual(report["catalog_unsupported_by_source"], {"future-board": 1, "google": 3, "unknown": 1})
        self._assert_untouched(before, unsupported)

    def test_catalog_with_only_unsupported_rows_validates_nothing(self) -> None:
        unsupported = self._seed("google", 3) + self._seed("foundit", 2)
        before = self._states(unsupported)
        for probes in (None, {"linkedin": lambda row: ("confirmed_active", "fixture", "linkedin")}):
            report, probed = self._run(probes=probes, validation_limit=200)
            self.assertEqual(probed, [])
            self.assertEqual(report["validation_selected"], 0)
            self.assertEqual(report["validated_checked"], 0)
            self.assertEqual(report["unknown_or_inconclusive"], 0)
            self.assertEqual(report["lifecycle_observations_recorded"], 0)
            self.assertEqual(report["validation_reasons"], {})
            self.assertEqual(report["catalog_unsupported_rows"], 5)
            self._assert_untouched(before, unsupported)

    def test_unsupported_rows_never_gain_confirmed_evidence(self) -> None:
        unchecked = self._seed("google", 2, checked=False) + self._seed("foundit", 1, checked=False)
        self._seed("naukri", 1)
        before = self._states(unchecked)
        self._run(probes={"naukri": lambda row: ("confirmed_expired", "fixture", "naukri")}, validation_limit=10)
        after = self._states(unchecked)
        self.assertEqual(after, before)
        for state in after.values():
            self.assertEqual(state[0], "active")
            self.assertIsNone(state[1])
            self.assertIsNone(state[2])
            self.assertIsNone(state[3])
            self.assertIsNone(state[4])


if __name__ == "__main__":
    unittest.main()
