#!/usr/bin/env python3
"""Availability audit for catalog links.

Each catalog row is checked with the canonical validator in
jobready_validator.py, the same rules run_jobready_daily.py uses. The report
keeps the stored lifecycle state (persisted_status) separate from what this
audit observed (observation: confirmed_active, confirmed_expired, or
inconclusive). An inconclusive check never counts as confirmed_active,
whatever the stored status was. A row on a source without a lifecycle probe is
reported as unsupported (reason unsupported_source) and is not probed.

This command is read-only. It reads the catalog in a read-only session,
checks URLs, and writes only its local JSON report. It never changes link_status, writes lifecycle evidence, applies
schema, or touches the JobReady application database. Catalog lifecycle changes
come from run_jobready_daily.py. The retired --prune-job-ready and
--confirm-target options are refused with JOBREADY_SYNC_RETIRED and exit code 2
before any environment or database access.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import load_env
from jobready_validator import Probe, default_probes, probe_for, validate_candidates
from link_policy import (
    CONFIRMED_ACTIVE,
    CONFIRMED_EXPIRED,
    INCONCLUSIVE,
    UNSUPPORTED,
    UNSUPPORTED_SOURCE,
    source_label,
)

ENV = ROOT.parent / "naukri_jobs_scraper" / ".env"
JOBREADY_SYNC_RETIRED = "JOBREADY_SYNC_RETIRED"
JOBREADY_SYNC_RETIRED_HELP = (
    "audit_job_ready.py is read-only and no longer writes the JobReady catalog or application database.\n"
    "Catalog lifecycle changes come from codequest_jobs/run_jobready_daily.py.\n"
    "Application membership reconciliation is codequest_jobs/sync_job_ready_ui.py."
)
EXIT_RETIRED = 2
SOURCE_ORDER = {"linkedin": 0, "indeed": 1, "foundit": 2, "naukri": 3}
CONCLUSIVE = (CONFIRMED_ACTIVE, CONFIRMED_EXPIRED)
ROW_COLUMNS = ("job_id", "source", "job_url", "apply_url", "link_status")


def fetch_job_ready_rows() -> list[dict[str, Any]]:
    """Read catalog rows with their stored link_status. The session is read-only."""
    dest = (os.getenv("JOB_READY_DATABASE_URL") or "").strip()
    if not dest:
        raise SystemExit("APPLY_REFUSED catalog target must be configured")
    try:
        conn = psycopg2.connect(dest, connect_timeout=20)
    except Exception:
        raise SystemExit("UI_SYNC_FAILED") from None
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, source, job_url, apply_url, link_status
                FROM validated_jobs
                WHERE job_id IS NOT NULL
                  AND COALESCE(job_url, '') <> ''
                """
            )
            rows = [dict(zip(ROW_COLUMNS, raw)) for raw in cur.fetchall()]
    finally:
        conn.close()
    rows.sort(key=lambda row: (SOURCE_ORDER.get((row["source"] or "").lower(), 9), row["job_id"]))
    return rows


def audit_row(row: dict[str, Any], probes: dict[str, Probe] | None = None) -> dict[str, Any]:
    """Return the stored status and this audit's canonical observation for one row.

    A row whose source has no lifecycle probe is reported as unsupported/unsupported_source
    with no provider; nothing is probed.
    """
    result = {
        "job_id": row["job_id"],
        "source": source_label(row.get("source")),
        "persisted_status": row.get("link_status"),
    }
    if probe_for(row.get("source"), probes) is None:
        return {**result, "observation": UNSUPPORTED, "reason": UNSUPPORTED_SOURCE, "provider": None}
    checked = validate_candidates([row], probes=probes)[0]
    observation = checked.observation if checked.observation in CONCLUSIVE else INCONCLUSIVE
    return {**result, "observation": observation, "reason": checked.reason, "provider": checked.provider}


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Count stored lifecycle state and audit observations separately.

    checked counts rows sent to a provider probe; unsupported rows are counted apart.
    """
    summary: dict[str, Any] = {
        "checked": 0,
        CONFIRMED_ACTIVE: 0,
        CONFIRMED_EXPIRED: 0,
        INCONCLUSIVE: 0,
        UNSUPPORTED: 0,
        "unsupported_by_source": {},
        "persisted_active": 0,
        "persisted_expired": 0,
        "persisted_other": 0,
    }
    reasons: dict[str, int] = {}
    crosstab: dict[str, dict[str, int]] = {}
    unsupported_by_source: dict[str, int] = {}
    for result in results:
        observation = result["observation"]
        summary[observation] += 1
        if observation == UNSUPPORTED:
            unsupported_by_source[result["source"]] = unsupported_by_source.get(result["source"], 0) + 1
        else:
            summary["checked"] += 1
        persisted = result["persisted_status"]
        bucket = f"persisted_{persisted}" if persisted in ("active", "expired") else "persisted_other"
        summary[bucket] += 1
        label = f"{observation}:{result['reason']}"
        reasons[label] = reasons.get(label, 0) + 1
        row = crosstab.setdefault(str(persisted), {})
        row[observation] = row.get(observation, 0) + 1
    summary["unsupported_by_source"] = dict(sorted(unsupported_by_source.items()))
    summary["validation_reasons"] = dict(sorted(reasons.items()))
    summary["persisted_by_observation"] = {key: dict(sorted(value.items())) for key, value in sorted(crosstab.items())}
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only catalog link audit")
    parser.add_argument(
        "--prune-job-ready",
        action="store_true",
        help="RETIRED. Refused with JOBREADY_SYNC_RETIRED; this audit never modifies JobReady.",
    )
    parser.add_argument(
        "--confirm-target",
        default=None,
        help="RETIRED. Refused with JOBREADY_SYNC_RETIRED; this audit never modifies JobReady.",
    )
    args = parser.parse_args(argv)
    if args.prune_job_ready or args.confirm_target is not None:
        print(JOBREADY_SYNC_RETIRED, flush=True)
        print(JOBREADY_SYNC_RETIRED_HELP, flush=True)
        return EXIT_RETIRED
    load_env(ENV)
    print("Read-only audit. The JobReady catalog and application database will not be modified.", flush=True)

    rows = fetch_job_ready_rows()
    by_source: dict[str, int] = {}
    for row in rows:
        key = (row["source"] or "unknown").lower()
        by_source[key] = by_source.get(key, 0) + 1
    print(f"Job-ready availability audit: {len(rows)} links", flush=True)
    print("by_source:", by_source, flush=True)
    eta_min = max(1, int(round(len(rows) / 1.4 / 60))) if rows else 0
    print(f"ETA audit: ~{eta_min} min", flush=True)

    probes = default_probes()
    results: list[dict[str, Any]] = []
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(audit_row, row, probes) for row in rows]
        for fut in as_completed(futures):
            result = fut.result()
            results.append(result)
            if len(results) % 100 == 0:
                counts = summarize(results)
                elapsed = time.time() - t0
                rate = len(results) / elapsed if elapsed else 0
                left = (len(rows) - len(results)) / rate if rate else 0
                print(
                    f"  progress {len(results)}/{len(rows)} "
                    f"confirmed_active={counts[CONFIRMED_ACTIVE]} "
                    f"confirmed_expired={counts[CONFIRMED_EXPIRED]} "
                    f"inconclusive={counts[INCONCLUSIVE]} "
                    f"unsupported={counts[UNSUPPORTED]} "
                    f"({rate:.1f}/s, ~{left/60:.0f}m left)",
                    flush=True,
                )

    elapsed = round(time.time() - t0, 1)
    results.sort(key=lambda result: result["job_id"])
    summary = {
        **summarize(results),
        "queued": len(rows),
        "elapsed_sec": elapsed,
        "by_source_queued": by_source,
    }
    report = {**summary, "results": results}
    out = ROOT / "output" / f"job_ready_audit_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    print(f"Report: {out}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, flush=True)
            raise SystemExit(2) from None
        raise
