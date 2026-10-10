"""JobReady validated_jobs catalog.

This module owns catalog schema, identity, CQJ allocation, and upserts.
The daily pipeline must not write any other database.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import psycopg2
from psycopg2.extras import Json

# Session lock for the whole daily run. Released on disconnect.
RUN_LOCK_KEY = 87264011
# Transaction lock around new CQJ allocation.
ALLOC_LOCK_KEY = 87264021

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS validated_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    source_id TEXT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    job_type TEXT,
    job_url TEXT NOT NULL,
    apply_url TEXT,
    link_status TEXT NOT NULL DEFAULT 'active',
    ingest_profile TEXT,
    date_posted TIMESTAMPTZ,
    salary_min NUMERIC,
    salary_max NUMERIC,
    currency TEXT,
    scraped_created_at TIMESTAMPTZ,
    actual_role_id TEXT,
    actual_role_name TEXT,
    role_family TEXT,
    role_level_id TEXT,
    experience_level TEXT,
    experience_bucket TEXT,
    jd_summary TEXT,
    jd_clean TEXT,
    jd_responsibilities JSONB,
    jd_requirements JSONB,
    keywords_must JSONB,
    keywords_nice JSONB,
    practice_focus JSONB,
    required_skills JSONB,
    good_to_have_skills JSONB,
    tools JSONB,
    programming_languages JSONB,
    databases JSONB,
    frameworks JSONB,
    student_preparation_topics JSONB,
    quiz_pack_id TEXT,
    mapping_confidence NUMERIC,
    manual_review_needed BOOLEAN,
    approved_status TEXT,
    has_jd_clean BOOLEAN DEFAULT FALSE,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_validated_jobs_source ON validated_jobs (source);
CREATE INDEX IF NOT EXISTS idx_validated_jobs_role_family ON validated_jobs (role_family);
CREATE INDEX IF NOT EXISTS idx_validated_jobs_experience ON validated_jobs (experience_bucket);
CREATE INDEX IF NOT EXISTS idx_validated_jobs_link_status ON validated_jobs (link_status);
CREATE INDEX IF NOT EXISTS idx_validated_jobs_synced_at ON validated_jobs (synced_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_validated_jobs_source_id_unique
    ON validated_jobs (source_id)
    WHERE source_id IS NOT NULL AND btrim(source_id) <> '';
"""

CONTENT_FIELDS = (
    "title",
    "company",
    "location",
    "job_type",
    "job_url",
    "apply_url",
    "ingest_profile",
    "role_family",
    "actual_role_id",
    "actual_role_name",
    "role_level_id",
    "experience_level",
    "experience_bucket",
    "jd_summary",
    "manual_review_needed",
    "approved_status",
)


def catalog_url() -> str:
    url = (os.getenv("JOB_READY_DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("JOB_READY_DATABASE_URL is required")
    return url


def connect(url: str | None = None):
    return psycopg2.connect(url or catalog_url(), connect_timeout=20)


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


class DailyRunLock:
    """One production daily run at a time. The lock dies with this connection."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url
        self.conn = None

    def acquire(self) -> None:
        self.conn = connect(self.url)
        self.conn.autocommit = True
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK_KEY,))
            locked = bool(cur.fetchone()[0])
        if not locked:
            self.conn.close()
            self.conn = None
            raise SystemExit("another JobReady daily run is active")

    def release(self) -> None:
        if self.conn is None:
            return
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (RUN_LOCK_KEY,))
        finally:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "DailyRunLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def active_job_ids(conn) -> list[str]:
    """Catalog rows that count as current JobReady membership."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id FROM validated_jobs
            WHERE job_id IS NOT NULL
              AND link_status = 'active'
            """
        )
        return [row[0] for row in cur.fetchall()]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def allocate_job_id(conn, when: datetime | None = None) -> str:
    """Next CQJ-YYYYMMDD-NNNN. Safe across concurrent transactions."""
    moment = when or _now()
    day = moment.strftime("%Y%m%d")
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ALLOC_LOCK_KEY,))
        cur.execute(
            """
            SELECT COALESCE(MAX(CAST(substring(job_id from 14) AS INTEGER)), 0)
            FROM validated_jobs
            WHERE job_id ~ %s
            """,
            (rf"^CQJ-{day}-[0-9]+$",),
        )
        current = int(cur.fetchone()[0])
    return f"CQJ-{day}-{current + 1:04d}"


def _json(value: Any):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return Json(value)
    return value


def _find(cur, source_id: str, source: str, job_url: str):
    cur.execute(
        """
        SELECT job_id, scraped_created_at, updated_at, link_status, source_id
        FROM validated_jobs
        WHERE source_id = %s
        """,
        (source_id,),
    )
    row = cur.fetchone()
    if row:
        return row
    cur.execute(
        """
        SELECT job_id, scraped_created_at, updated_at, link_status, source_id
        FROM validated_jobs
        WHERE lower(source) = lower(%s) AND job_url = %s
        ORDER BY scraped_created_at NULLS LAST
        LIMIT 1
        """,
        (source, job_url),
    )
    return cur.fetchone()


def _content_changed(cur, job_id: str, job: dict[str, Any], link_status: str) -> bool:
    columns = ", ".join(CONTENT_FIELDS)
    cur.execute(f"SELECT {columns}, link_status FROM validated_jobs WHERE job_id = %s", (job_id,))
    current = cur.fetchone()
    if current is None:
        return True
    for index, field in enumerate(CONTENT_FIELDS):
        if current[index] != job.get(field):
            return True
    return current[-1] != link_status


def upsert_jobs(conn, jobs: list[dict[str, Any]]) -> dict[str, int]:
    """Insert new source identities and refresh jobs seen again.

    Existing job_id and scraped_created_at are preserved.
    synced_at advances on every observation.
    updated_at advances only when content or link_status changes.
    This never deletes a row that was absent from the batch.
    """
    counts = {"new": 0, "updated": 0, "unchanged": 0, "reobserved": 0}
    if not jobs:
        return counts
    with conn.cursor() as cur:
        for job in jobs:
            source_id = job["source_id"]
            existing = _find(cur, source_id, job["source"], job["job_url"])
            observed_status = job.get("link_status") or "active"
            if existing:
                job_id, created_at, _updated_at, previous_status, stored_source_id = existing
                link_status = observed_status or previous_status
                changed = _content_changed(cur, job_id, job, link_status)
                cur.execute(
                    """
                    UPDATE validated_jobs SET
                        source_id = COALESCE(NULLIF(source_id, ''), %s),
                        source = %s,
                        title = %s,
                        company = %s,
                        location = %s,
                        job_type = %s,
                        job_url = %s,
                        apply_url = %s,
                        link_status = %s,
                        ingest_profile = %s,
                        role_family = %s,
                        actual_role_id = %s,
                        actual_role_name = %s,
                        role_level_id = %s,
                        experience_level = %s,
                        experience_bucket = %s,
                        jd_summary = %s,
                        jd_clean = %s,
                        manual_review_needed = %s,
                        approved_status = %s,
                        mapping_confidence = %s,
                        has_jd_clean = %s,
                        required_skills = %s,
                        synced_at = NOW(),
                        updated_at = CASE WHEN %s THEN NOW() ELSE updated_at END,
                        scraped_created_at = scraped_created_at
                    WHERE job_id = %s
                    """,
                    (
                        source_id,
                        job["source"],
                        job["title"],
                        job.get("company"),
                        job.get("location"),
                        job.get("job_type"),
                        job["job_url"],
                        job.get("apply_url"),
                        link_status,
                        job.get("ingest_profile"),
                        job.get("role_family"),
                        job.get("actual_role_id"),
                        job.get("actual_role_name"),
                        job.get("role_level_id"),
                        job.get("experience_level"),
                        job.get("experience_bucket"),
                        job.get("jd_summary"),
                        job.get("jd_clean"),
                        job.get("manual_review_needed"),
                        job.get("approved_status"),
                        job.get("mapping_confidence"),
                        bool(job.get("jd_clean")),
                        _json(job.get("required_skills")),
                        changed,
                        job_id,
                    ),
                )
                counts["reobserved"] += 1
                if changed:
                    counts["updated"] += 1
                else:
                    counts["unchanged"] += 1
                continue
            job_id = allocate_job_id(conn)
            cur.execute(
                """
                INSERT INTO validated_jobs (
                    job_id, source_id, source, title, company, location, job_type,
                    job_url, apply_url, link_status, ingest_profile, role_family,
                    actual_role_id, actual_role_name, role_level_id, experience_level,
                    experience_bucket, jd_summary, jd_clean, manual_review_needed,
                    approved_status, mapping_confidence, has_jd_clean, required_skills,
                    scraped_created_at, synced_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    NOW(), NOW(), NOW()
                )
                """,
                (
                    job_id,
                    source_id,
                    job["source"],
                    job["title"],
                    job.get("company"),
                    job.get("location"),
                    job.get("job_type"),
                    job["job_url"],
                    job.get("apply_url"),
                    observed_status,
                    job.get("ingest_profile"),
                    job.get("role_family"),
                    job.get("actual_role_id"),
                    job.get("actual_role_name"),
                    job.get("role_level_id"),
                    job.get("experience_level"),
                    job.get("experience_bucket"),
                    job.get("jd_summary"),
                    job.get("jd_clean"),
                    job.get("manual_review_needed"),
                    job.get("approved_status"),
                    job.get("mapping_confidence"),
                    bool(job.get("jd_clean")),
                    _json(job.get("required_skills")),
                ),
            )
            counts["new"] += 1
    conn.commit()
    return counts


def mark_confirmed_expired(conn, job_ids: list[str]) -> int:
    """Retain the row and set link_status = expired. Does not delete."""
    if not job_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE validated_jobs
            SET link_status = 'expired', updated_at = NOW(), synced_at = NOW()
            WHERE job_id = ANY(%s) AND link_status IS DISTINCT FROM 'expired'
            """,
            (job_ids,),
        )
        changed = int(cur.rowcount)
    conn.commit()
    return changed


def catalog_counts(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              COUNT(*),
              COUNT(*) FILTER (WHERE link_status = 'active'),
              COUNT(*) FILTER (WHERE link_status = 'expired')
            FROM validated_jobs
            """
        )
        total, active, expired = cur.fetchone()
    return {"total": int(total), "active": int(active), "expired": int(expired)}
