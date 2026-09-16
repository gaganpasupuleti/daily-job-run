#!/usr/bin/env python3
"""Keep job-ready UI `jobs` aligned with Jobs-server `validated_jobs`.

The UI reads the app Postgres `jobs` table, not `validated_jobs` directly.
After pushing/pruning the catalog, archive any jobs-server rows that left the
catalog so the product stops showing old postings.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / "naukri_jobs_scraper" / ".env")


def _catalog_url() -> str:
    url = (os.getenv("JOB_READY_DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("JOB_READY_DATABASE_URL missing")
    return url


def _app_url() -> str:
    url = (os.getenv("JOB_READY_APP_DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit(
            "JOB_READY_APP_DATABASE_URL missing "
            "(public proxy URL for job-ready app Postgres)"
        )
    return url


def archive_ui_jobs_not_in_catalog(*, dry_run: bool = False) -> dict[str, int]:
    catalog = _catalog_url()
    app = _app_url()
    with psycopg2.connect(catalog, connect_timeout=20) as src:
        with src.cursor() as cur:
            cur.execute("SELECT job_id FROM validated_jobs WHERE job_id IS NOT NULL")
            keep = [row[0] for row in cur.fetchall()]

    with psycopg2.connect(app, connect_timeout=30) as dest:
        with dest.cursor() as cur:
            cur.execute("CREATE TEMP TABLE keep_cqj (job_id TEXT PRIMARY KEY) ON COMMIT DROP")
            if keep:
                execute_values(cur, "INSERT INTO keep_cqj (job_id) VALUES %s", [(i,) for i in keep])
            cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'active'")
            before = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT COUNT(*)
                FROM jobs j
                WHERE j.status = 'active'
                  AND j.external_id LIKE 'jobs-server:%'
                  AND NOT EXISTS (
                    SELECT 1 FROM keep_cqj k
                    WHERE k.job_id = substr(j.external_id, length('jobs-server:') + 1)
                  )
                """
            )
            stale = int(cur.fetchone()[0])
            archived = 0
            reactivated = 0
            if not dry_run:
                if stale:
                    cur.execute(
                        """
                        UPDATE jobs j
                        SET status = 'archived', updated_at = NOW()
                        WHERE j.status = 'active'
                          AND j.external_id LIKE 'jobs-server:%'
                          AND NOT EXISTS (
                            SELECT 1 FROM keep_cqj k
                            WHERE k.job_id = substr(j.external_id, length('jobs-server:') + 1)
                          )
                        """
                    )
                    archived = int(cur.rowcount)
                cur.execute(
                    """
                    UPDATE jobs j
                    SET status = 'active', updated_at = NOW()
                    WHERE j.status = 'archived'
                      AND j.external_id LIKE 'jobs-server:%'
                      AND EXISTS (
                        SELECT 1 FROM keep_cqj k
                        WHERE k.job_id = substr(j.external_id, length('jobs-server:') + 1)
                      )
                    """
                )
                reactivated = int(cur.rowcount)
            cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'active'")
            after = int(cur.fetchone()[0])
            if not dry_run:
                dest.commit()
    host = urlparse(app).hostname
    print(
        f"UI sync host={host} catalog={len(keep)} active_before={before} "
        f"stale={stale} archived={archived} reactivated={reactivated} active_after={after}",
        flush=True,
    )
    return {
        "catalog": len(keep),
        "active_before": before,
        "stale": stale,
        "archived": archived,
        "reactivated": reactivated,
        "active_after": after,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Archive UI jobs removed from validated_jobs")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    archive_ui_jobs_not_in_catalog(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
