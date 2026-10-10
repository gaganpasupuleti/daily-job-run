#!/usr/bin/env python3
"""Prune catalog rows that are no longer active in CodeQuest.

This does not write unless --prune-job-ready and --confirm-target are both set.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / "naukri_jobs_scraper" / ".env")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune the JobReady catalog only when explicitly confirmed")
    parser.add_argument("--prune-job-ready", action="store_true")
    parser.add_argument("--confirm-target", default="", help="Application database as host:port/database. Not a DSN.")
    args = parser.parse_args(argv)
    if not args.prune_job_ready:
        print("Read-only. Pass --prune-job-ready and --confirm-target to modify JobReady.", flush=True)
        return 0

    from sync_job_ready_ui import archive_ui_jobs_not_in_catalog, assert_application_write_target

    print(f"TARGET {assert_application_write_target(args.confirm_target)}", flush=True)
    codequest = (os.getenv("CODEQUEST_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    catalog = (os.getenv("JOB_READY_DATABASE_URL") or "").strip()
    if not codequest or not catalog:
        raise SystemExit("APPLY_REFUSED catalog targets must be configured")

    try:
        with psycopg2.connect(codequest, connect_timeout=20) as src:
            with src.cursor() as cur:
                cur.execute(
                    """
                    SELECT job_id
                    FROM scraped_jobs
                    WHERE COALESCE(link_status, '') = 'active'
                      AND job_id IS NOT NULL
                      AND COALESCE(job_url, '') <> ''
                    """
                )
                keep = [row[0] for row in cur.fetchall()]
        print(f"CodeQuest active keep-set: {len(keep)}", flush=True)
        with psycopg2.connect(catalog, connect_timeout=20) as dest:
            with dest.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM validated_jobs")
                before = cur.fetchone()[0]
                cur.execute("CREATE TEMP TABLE keep_ids (job_id TEXT PRIMARY KEY) ON COMMIT DROP")
                if keep:
                    execute_values(cur, "INSERT INTO keep_ids (job_id) VALUES %s", [(job_id,) for job_id in keep])
                cur.execute(
                    """
                    DELETE FROM validated_jobs v
                    WHERE NOT EXISTS (SELECT 1 FROM keep_ids k WHERE k.job_id = v.job_id)
                    """
                )
                removed = cur.rowcount
                cur.execute("SELECT COUNT(*) FROM validated_jobs")
                after = cur.fetchone()[0]
            dest.commit()
    except SystemExit:
        raise
    except Exception:
        raise SystemExit("UI_SYNC_FAILED") from None

    print(f"catalog validated_jobs: {before} -> {after} (removed {removed})", flush=True)
    result = archive_ui_jobs_not_in_catalog(confirm_target=args.confirm_target)
    print(
        f"application archived={result['archived']} reactivated={result['reactivated']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, flush=True)
            raise SystemExit(2) from None
        raise
