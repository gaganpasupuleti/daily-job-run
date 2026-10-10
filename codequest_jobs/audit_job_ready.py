#!/usr/bin/env python3
"""Availability audit for catalog links.

By default this only reads the catalog and checks URLs. It does not modify
the JobReady application database. Pass --prune-job-ready and --confirm-target
to remove confirmed-closed catalog rows and align application jobs.
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

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import load_env
from link_checker import LINK_STATUS_EXPIRED, LINK_STATUS_LINK_FAILED
from validate_links import build_indeed_expired_map, sync_dead_to_job_ready, validate_one

ENV = ROOT.parent / "naukri_jobs_scraper" / ".env"
SOURCE_ORDER = {"linkedin": 0, "indeed": 1, "foundit": 2, "naukri": 3}


def fetch_job_ready_rows() -> list[tuple]:
    dest = (os.getenv("JOB_READY_DATABASE_URL") or "").strip()
    if not dest:
        raise SystemExit("APPLY_REFUSED catalog target must be configured")
    import psycopg2

    try:
        conn = psycopg2.connect(dest, connect_timeout=20)
    except Exception:
        raise SystemExit("UI_SYNC_FAILED") from None
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, source, job_url, apply_url
                FROM validated_jobs
                WHERE job_id IS NOT NULL
                  AND COALESCE(job_url, '') <> ''
                """
            )
            raw = cur.fetchall()
    rows = [
        (job_id, job_id, source, job_url, apply_url, "active")
        for job_id, source, job_url, apply_url in raw
    ]
    rows.sort(key=lambda row: (SOURCE_ORDER.get((row[2] or "").lower(), 9), row[1]))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only catalog link audit unless prune is explicit")
    parser.add_argument(
        "--prune-job-ready",
        action="store_true",
        help="Remove confirmed-closed catalog rows and align application jobs",
    )
    parser.add_argument(
        "--confirm-target",
        default="",
        help="Application database as host:port/database. Required with --prune-job-ready. Not a DSN.",
    )
    args = parser.parse_args(argv)
    load_env(ENV)
    if args.prune_job_ready:
        from sync_job_ready_ui import assert_application_write_target

        print(f"TARGET {assert_application_write_target(args.confirm_target)}", flush=True)
    else:
        print("Read-only audit. The application database will not be modified.", flush=True)

    rows = fetch_job_ready_rows()
    by_source: dict[str, int] = {}
    for _id, _job_id, source, *_rest in rows:
        key = (source or "unknown").lower()
        by_source[key] = by_source.get(key, 0) + 1
    print(f"Job-ready availability audit: {len(rows)} links", flush=True)
    print("by_source:", by_source, flush=True)
    eta_min = max(1, int(round(len(rows) / 1.4 / 60))) if rows else 0
    print(f"ETA audit: ~{eta_min} min", flush=True)

    indeed_map = build_indeed_expired_map(rows)
    counts = {"checked": 0, "active": 0, "expired": 0, "other": 0, "removed": 0}
    pending: list[tuple[str, str, str, str]] = []
    t0 = time.time()

    def flush(batch: list[tuple[str, str, str, str]]) -> None:
        if not batch or not args.prune_job_ready:
            return
        dead = [row for row in batch if row[3] in (LINK_STATUS_EXPIRED, LINK_STATUS_LINK_FAILED)]
        if not dead:
            return
        result = sync_dead_to_job_ready(dead, confirm_target=args.confirm_target)
        counts["removed"] += int(result.get("removed") or 0)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(validate_one, row, indeed_expired=indeed_map): row for row in rows}
        for fut in as_completed(futures):
            row_id, job_id, source, status = fut.result()
            pending.append((row_id, job_id, source, status))
            counts["checked"] += 1
            if status == "active":
                counts["active"] += 1
            elif status == "expired":
                counts["expired"] += 1
            else:
                counts["other"] += 1
            if len(pending) >= 100:
                flush(pending)
                pending.clear()
                elapsed = time.time() - t0
                rate = counts["checked"] / elapsed if elapsed else 0
                left = (len(rows) - counts["checked"]) / rate if rate else 0
                print(
                    f"  progress {counts['checked']}/{len(rows)} "
                    f"active={counts['active']} expired={counts['expired']} "
                    f"removed={counts['removed']} ({rate:.1f}/s, ~{left/60:.0f}m left)",
                    flush=True,
                )

    flush(pending)
    elapsed = round(time.time() - t0, 1)
    summary = {**counts, "queued": len(rows), "elapsed_sec": elapsed, "by_source_queued": by_source, "prune": args.prune_job_ready}
    out = ROOT / "output" / f"job_ready_audit_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
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
