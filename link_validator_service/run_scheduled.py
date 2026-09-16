#!/usr/bin/env python3
"""One scheduled pass for Railway cron. Exits when finished.

Every run:
  1. Recheck all job links (Naukri HTTP, Indeed GraphQL, LinkedIn closed page).
  2. Map active jobs that have no role tag, plus jobs scraped in the last 2 days.
  3. Push still-active jobs to job-ready validated_jobs.
  4. Remove anything that is no longer active so the live table has clean links only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("LINK_VALIDATE_SKIP_NAUKRI", "1")
os.environ.setdefault("PYTHONUNBUFFERED", "1")


def _require_env() -> None:
    source = (os.getenv("CODEQUEST_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    dest = (os.getenv("JOB_READY_DATABASE_URL") or "").strip()
    if not source:
        raise SystemExit("CODEQUEST_DATABASE_URL or DATABASE_URL is required")
    if not dest:
        raise SystemExit("JOB_READY_DATABASE_URL is required")


def _skip_if_already_running() -> bool:
    from db import connect, ensure_run_logs_schema

    ensure_run_logs_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, started_at
                FROM job_link_validation_runs
                WHERE status = 'running'
                  AND started_at > NOW() - INTERVAL '170 minutes'
                ORDER BY id DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
    if not row:
        return False
    print(f"Skip: validation run {row[0]} still marked running since {row[1]}", flush=True)
    return True


def _prune_stale_live_jobs() -> None:
    """Keep validated_jobs as active links only."""
    import psycopg2
    from psycopg2.extras import execute_values

    from db import connect

    dest = os.environ["JOB_READY_DATABASE_URL"].strip()
    with connect() as src:
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
            ids = [row[0] for row in cur.fetchall()]
    print(f"Active jobs after validation: {len(ids)}", flush=True)

    with psycopg2.connect(dest, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE active_job_ids (job_id TEXT PRIMARY KEY) ON COMMIT DROP")
            if ids:
                execute_values(cur, "INSERT INTO active_job_ids (job_id) VALUES %s", [(i,) for i in ids])
            cur.execute("SELECT COUNT(*) FROM validated_jobs WHERE COALESCE(link_status, '') = 'active'")
            live_before = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT COUNT(*)
                FROM validated_jobs v
                WHERE COALESCE(v.link_status, '') = 'active'
                  AND NOT EXISTS (
                    SELECT 1 FROM active_job_ids a WHERE a.job_id = v.job_id
                  )
                """
            )
            would_remove = int(cur.fetchone()[0])
            if live_before and would_remove > live_before * 0.25:
                raise SystemExit(
                    f"Refusing to remove {would_remove} of {live_before} live jobs. "
                    "That drop is too large for one run."
                )
            cur.execute(
                """
                DELETE FROM validated_jobs v
                WHERE COALESCE(v.link_status, '') <> 'active'
                   OR NOT EXISTS (
                    SELECT 1 FROM active_job_ids a WHERE a.job_id = v.job_id
                  )
                """
            )
            removed = cur.rowcount
            cur.execute("SELECT COUNT(*) FROM validated_jobs")
            remaining = cur.fetchone()[0]
        conn.commit()
    print(f"Removed stale live rows: {removed}. validated_jobs now {remaining}", flush=True)


def map_jobs() -> None:
    """Tag roles for unmapped active jobs and jobs scraped in the last 2 days."""
    from enrichment import classify_job_row, upsert_enrichments
    from practice_items import upsert_practice_for_enrichments
    from role_taxonomy import ensure_taxonomy

    from db import connect, ensure_jd_schema

    ensure_taxonomy()
    ensure_jd_schema()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.job_id, s.title, s.company, s.location, s.job_type, s.description,
                       s.link_status, s.ingest_profile, s.source, s.created_at
                FROM scraped_jobs s
                LEFT JOIN job_enrichments e ON e.job_id = s.job_id
                WHERE COALESCE(s.link_status, 'active') = 'active'
                  AND s.job_id IS NOT NULL
                  AND (
                    e.job_id IS NULL
                    OR s.created_at >= NOW() - INTERVAL '2 days'
                  )
                ORDER BY s.created_at DESC
                """
            )
            cols = [d[0] for d in cur.description]
            jobs = [dict(zip(cols, row)) for row in cur.fetchall()]
    print(f"Mapping {len(jobs)} jobs...", flush=True)
    if not jobs:
        return
    classified = [classify_job_row(job) for job in jobs if job.get("job_id")]
    inserted, updated = upsert_enrichments(classified)
    practice = upsert_practice_for_enrichments(classified)
    print(
        f"Mapped {len(classified)} | +{inserted} new / {updated} updated | practice={practice}",
        flush=True,
    )


def main() -> None:
    _require_env()
    if _skip_if_already_running():
        return

    workers = os.getenv("VALIDATE_WORKERS", "6")
    print("Starting link validation...", flush=True)
    subprocess.run(
        [
            sys.executable,
            "-u",
            str(ROOT / "validate_links.py"),
            "--include-bad",
            "--workers",
            workers,
            "--batch-size",
            "40",
        ],
        cwd=str(ROOT),
        check=True,
    )

    print("Starting job mapping...", flush=True)
    map_jobs()

    print("Pushing active jobs...", flush=True)
    subprocess.run(
        [sys.executable, "-u", str(ROOT / "push_validated_to_job_ready.py")],
        cwd=str(ROOT),
        check=True,
    )
    _prune_stale_live_jobs()
    print("Scheduled validation finished.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: step failed with exit {exc.returncode}", file=sys.stderr, flush=True)
        raise SystemExit(exc.returncode) from exc
