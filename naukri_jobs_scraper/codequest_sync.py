"""LEGACY. Dual-write UnifiedJob rows into CodeQuest scraped_jobs.

Not used by the JobReady daily pipeline.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2

from normalize import UnifiedJob

IST = ZoneInfo("Asia/Kolkata")


def get_codequest_database_url() -> str:
    url = os.getenv("CODEQUEST_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "CODEQUEST_DATABASE_URL is not set. Add the CodeQuest Postgres URL to .env, "
            "or pass --no-codequest for local-only writes."
        )
    return url


def _ist_date_yyyymmdd(dt: datetime | None = None) -> str:
    when = dt or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(IST).strftime("%Y%m%d")


def _format_job_id(date_str: str, seq: int) -> str:
    return f"CQJ-{date_str}-{seq:04d}"


def _next_job_id_seq(cur, date_str: str) -> int:
    prefix = f"CQJ-{date_str}-"
    cur.execute(
        """
        SELECT job_id FROM scraped_jobs
        WHERE job_id LIKE %s
        """,
        (f"{prefix}%",),
    )
    max_seq = 0
    for (jid,) in cur.fetchall():
        if not jid:
            continue
        try:
            max_seq = max(max_seq, int(str(jid).rsplit("-", 1)[-1]))
        except ValueError:
            pass
    return max_seq + 1


def create_scrape_run(
    *,
    search_term: str,
    location: str,
    sources: list[str],
    results_wanted: int,
    hours_old: int,
    run_type: str = "manual",
    profile: str | None = None,
    triggered_by: str = "run_all_sources",
) -> int:
    started = datetime.now(timezone.utc).replace(tzinfo=None)
    with psycopg2.connect(get_codequest_database_url(), connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO job_scrape_runs (
                    search_term, location, sources_json, results_wanted, hours_old,
                    total_found, saved_count, skipped_duplicates, error_count,
                    expired_count, failed_link_count,
                    started_at, triggered_by, status, run_type, profile
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    0, 0, 0, 0,
                    0, 0,
                    %s, %s, 'failed', %s, %s
                )
                RETURNING id
                """,
                (
                    search_term[:200],
                    location[:120],
                    json.dumps(sources),
                    results_wanted,
                    hours_old,
                    started,
                    triggered_by[:255],
                    run_type,
                    (profile or "")[:64] or None,
                ),
            )
            run_id = int(cur.fetchone()[0])
        conn.commit()
    return run_id


def update_scrape_run(
    run_id: int,
    *,
    total_found: int,
    saved_count: int,
    skipped_duplicates: int,
    error_count: int,
    errors: list[str] | None = None,
    source_breakdown: dict[str, int] | None = None,
    status: str = "success",
    started_at: datetime | None = None,
) -> None:
    finished = datetime.now(timezone.utc).replace(tzinfo=None)
    duration_ms = None
    if started_at is not None:
        started_naive = started_at.replace(tzinfo=None) if started_at.tzinfo else started_at
        duration_ms = int((finished - started_naive).total_seconds() * 1000)
    with psycopg2.connect(get_codequest_database_url(), connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_scrape_runs SET
                    total_found = %s,
                    saved_count = %s,
                    skipped_duplicates = %s,
                    error_count = %s,
                    errors_json = %s,
                    source_breakdown_json = %s,
                    finished_at = %s,
                    duration_ms = %s,
                    status = %s
                WHERE id = %s
                """,
                (
                    total_found,
                    saved_count,
                    skipped_duplicates,
                    error_count,
                    json.dumps(errors or []) if errors else None,
                    json.dumps(source_breakdown or {}),
                    finished,
                    duration_ms,
                    status,
                    run_id,
                ),
            )
        conn.commit()


def save_to_scraped_jobs(jobs: list[UnifiedJob]) -> tuple[int, int]:
    """Insert new CodeQuest scraped_jobs rows; skip duplicates by id.

    Returns (saved_count, skipped_duplicates).
    """
    if not jobs:
        return 0, 0

    saved = 0
    skipped = 0
    next_seq_by_date: dict[str, int] = {}
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            return _save_to_scraped_jobs_once(jobs, saved, skipped, next_seq_by_date)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            last_err = exc
            print(f"CodeQuest write retry {attempt}/3 after DB drop: {exc}", flush=True)
            time.sleep(3 * attempt)
    if last_err:
        raise last_err
    return saved, skipped


def _save_to_scraped_jobs_once(
    jobs: list[UnifiedJob],
    saved: int,
    skipped: int,
    next_seq_by_date: dict[str, int],
) -> tuple[int, int]:
    with psycopg2.connect(get_codequest_database_url(), connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for job in jobs:
                cq_id = (job.codequest_id or job.job_id).strip()
                job_url = (job.job_url or "").strip()
                if not cq_id or not job_url:
                    skipped += 1
                    continue

                cur.execute("SELECT 1 FROM scraped_jobs WHERE id = %s", (cq_id,))
                if cur.fetchone():
                    skipped += 1
                    continue

                created = datetime.now(timezone.utc).replace(tzinfo=None)
                date_str = _ist_date_yyyymmdd(created.replace(tzinfo=timezone.utc))
                if date_str not in next_seq_by_date:
                    next_seq_by_date[date_str] = _next_job_id_seq(cur, date_str)
                else:
                    next_seq_by_date[date_str] += 1
                cqj = _format_job_id(date_str, next_seq_by_date[date_str])

                cur.execute(
                    """
                    INSERT INTO scraped_jobs (
                        id, source, title, company, location, job_type,
                        date_posted, salary_min, salary_max, currency,
                        description, job_url, apply_url, created_at,
                        link_status, ingest_profile, job_id
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        'active', %s, %s
                    )
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        cq_id,
                        (job.source or "unknown")[:40],
                        (job.title or "Untitled role")[:300],
                        (job.company or None),
                        (job.location or None),
                        (job.job_type or job.experience or None),
                        job.date_posted,
                        job.salary_min,
                        job.salary_max,
                        (job.currency or None),
                        job.description or None,
                        job_url[:2048],
                        (job.apply_url or job_url)[:2048],
                        created,
                        (job.ingest_profile or job.role_family or "fresher_india")[:64],
                        cqj,
                    ),
                )
                if cur.rowcount:
                    saved += 1
                else:
                    skipped += 1
                if (saved + skipped) % 25 == 0:
                    conn.commit()
                    print(f"  codequest write {saved + skipped}/{len(jobs)}", flush=True)
        conn.commit()

    return saved, skipped


def codequest_source_summary() -> list[tuple[str, int]]:
    with psycopg2.connect(get_codequest_database_url(), connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(source, 'unknown'), COUNT(*)
                FROM scraped_jobs
                GROUP BY 1
                ORDER BY 2 DESC
                """
            )
            return [(row[0], row[1]) for row in cur.fetchall()]
