"""LEGACY. Railway PostgreSQL persistence for the old naukri_scraper table.

Not used by the JobReady daily pipeline.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import execute_values

from naukri_api import NaukriJob
from normalize import UnifiedJob, from_naukri

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
TABLE_NAME = "naukri_scraper"
_SCHEMA_READY = False


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and paste your Railway Postgres URL."
        )
    return url


def init_schema(conn, *, force: bool = False) -> None:
    """Run schema once per process. Avoids exclusive locks on every scrape."""
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    _SCHEMA_READY = True


def _as_unified(jobs: list[Any]) -> list[UnifiedJob]:
    out: list[UnifiedJob] = []
    for job in jobs:
        if isinstance(job, UnifiedJob):
            out.append(job)
        elif isinstance(job, NaukriJob):
            out.append(from_naukri(job))
        else:
            raise TypeError(f"Unsupported job type: {type(job)!r}")
    return out


def save_jobs(jobs: list[Any]) -> tuple[int, int]:
    """Upsert jobs by job_id. Returns (inserted_count, already_existed_count)."""
    unified = _as_unified(jobs)
    if not unified:
        return 0, 0

    rows = [
        (
            j.job_id,
            j.source or "naukri",
            j.title,
            j.company,
            j.location,
            j.experience,
            j.salary,
            j.skills,
            j.description,
            j.job_url,
            j.posted_label,
            j.search_keyword,
            j.experience_filter,
            j.role_family,
            j.role_family_name,
            "active",
        )
        for j in unified
    ]

    insert_sql = f"""
        INSERT INTO {TABLE_NAME} (
            job_id, source, title, company, location, experience, salary,
            skills, description, job_url, posted_label, search_keyword,
            experience_filter, role_family, role_family_name,
            status, scraped_at, last_seen_at, last_checked_at
        ) VALUES %s
        ON CONFLICT (job_id) DO UPDATE SET
            last_seen_at = NOW(),
            last_checked_at = NOW(),
            status = 'active',
            source = COALESCE(NULLIF(EXCLUDED.source, ''), {TABLE_NAME}.source),
            role_family = COALESCE(NULLIF(EXCLUDED.role_family, ''), {TABLE_NAME}.role_family),
            role_family_name = COALESCE(NULLIF(EXCLUDED.role_family_name, ''), {TABLE_NAME}.role_family_name)
        RETURNING (xmax = 0) AS inserted
    """
    template = (
        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), NOW())"
    )

    with psycopg2.connect(get_database_url(), connect_timeout=20) as conn:
        init_schema(conn)
        with conn.cursor() as cur:
            results = execute_values(cur, insert_sql, rows, template=template, fetch=True)
        conn.commit()

    inserted = sum(1 for row in results if row[0])
    existed = len(results) - inserted
    return inserted, existed


def source_summary() -> list[tuple[str, int]]:
    with psycopg2.connect(get_database_url(), connect_timeout=20) as conn:
        init_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE(source, 'unknown') AS source, COUNT(*)
                FROM {TABLE_NAME}
                GROUP BY 1
                ORDER BY 2 DESC
                """
            )
            return [(row[0], row[1]) for row in cur.fetchall()]


def list_job_ids_not_in(seen_job_ids: set[str], *, status: str = "active") -> list[tuple[str, str]]:
    with psycopg2.connect(get_database_url(), connect_timeout=20) as conn:
        with conn.cursor() as cur:
            if seen_job_ids:
                cur.execute(
                    f"""
                    SELECT job_id, title
                    FROM {TABLE_NAME}
                    WHERE status = %s
                      AND job_id <> ALL(%s)
                    ORDER BY last_seen_at ASC NULLS FIRST, scraped_at ASC
                    """,
                    (status, list(seen_job_ids)),
                )
            else:
                cur.execute(
                    f"""
                    SELECT job_id, title
                    FROM {TABLE_NAME}
                    WHERE status = %s
                    ORDER BY last_seen_at ASC NULLS FIRST, scraped_at ASC
                    """,
                    (status,),
                )
            return [(row[0], row[1]) for row in cur.fetchall()]


def update_job_statuses(updates: list[tuple[str, str]]) -> dict[str, int]:
    if not updates:
        return {}

    counts: dict[str, int] = {}
    with psycopg2.connect(get_database_url(), connect_timeout=20) as conn:
        with conn.cursor() as cur:
            for job_id, status in updates:
                cur.execute(
                    f"""
                    UPDATE {TABLE_NAME}
                    SET status = %s, last_checked_at = NOW()
                    WHERE job_id = %s
                    """,
                    (status, job_id),
                )
                counts[status] = counts.get(status, 0) + 1
        conn.commit()
    return counts


def status_summary() -> list[tuple[str, int]]:
    with psycopg2.connect(get_database_url(), connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COALESCE(status, 'unknown') AS status, COUNT(*)
                FROM {TABLE_NAME}
                GROUP BY 1
                ORDER BY 2 DESC
                """
            )
            return [(row[0], row[1]) for row in cur.fetchall()]
