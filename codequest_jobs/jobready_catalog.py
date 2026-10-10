"""JobReady validated_jobs catalog.

This module owns catalog schema, identity, CQJ allocation, and upserts.
The daily pipeline must not write any other database.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
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
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    link_checked_at TIMESTAMPTZ,
    last_link_observation TEXT
);

ALTER TABLE validated_jobs ADD COLUMN IF NOT EXISTS link_checked_at TIMESTAMPTZ;
ALTER TABLE validated_jobs ADD COLUMN IF NOT EXISTS last_link_observation TEXT;

CREATE INDEX IF NOT EXISTS idx_validated_jobs_source ON validated_jobs (source);
CREATE INDEX IF NOT EXISTS idx_validated_jobs_role_family ON validated_jobs (role_family);
CREATE INDEX IF NOT EXISTS idx_validated_jobs_experience ON validated_jobs (experience_bucket);
CREATE INDEX IF NOT EXISTS idx_validated_jobs_link_status ON validated_jobs (link_status);
CREATE INDEX IF NOT EXISTS idx_validated_jobs_synced_at ON validated_jobs (synced_at DESC);
CREATE INDEX IF NOT EXISTS idx_validated_jobs_link_checked_at
    ON validated_jobs (link_checked_at ASC NULLS FIRST, job_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_validated_jobs_source_id_unique
    ON validated_jobs (source_id)
    WHERE source_id IS NOT NULL AND btrim(source_id) <> '';
"""

REQUIRED_COLUMNS = (
    "job_id",
    "source_id",
    "source",
    "title",
    "company",
    "location",
    "job_type",
    "job_url",
    "apply_url",
    "link_status",
    "ingest_profile",
    "date_posted",
    "salary_min",
    "salary_max",
    "currency",
    "scraped_created_at",
    "synced_at",
    "actual_role_id",
    "actual_role_name",
    "role_family",
    "role_level_id",
    "experience_level",
    "experience_bucket",
    "jd_summary",
    "jd_clean",
    "jd_requirements",
    "jd_responsibilities",
    "required_skills",
    "good_to_have_skills",
    "tools",
    "programming_languages",
    "databases",
    "frameworks",
    "student_preparation_topics",
    "mapping_confidence",
    "manual_review_needed",
    "approved_status",
    "link_checked_at",
    "last_link_observation",
    "updated_at",
)

REQUIRED_INDEXES = (
    "idx_validated_jobs_source_id_unique",
    "idx_validated_jobs_link_checked_at",
)

# A normal daily run checks this many catalog rows, oldest first after the priority share.
DEFAULT_VALIDATION_LIMIT = 200

PRESERVE_WHEN_BLANK = {
    "company",
    "location",
    "job_type",
    "apply_url",
    "date_posted",
    "salary_min",
    "salary_max",
    "currency",
    "jd_summary",
    "jd_clean",
    "jd_requirements",
    "jd_responsibilities",
    "required_skills",
    "good_to_have_skills",
    "tools",
    "programming_languages",
    "databases",
    "frameworks",
    "student_preparation_topics",
    "mapping_confidence",
}

CONTENT_FIELDS = (
    "title",
    "company",
    "location",
    "job_type",
    "job_url",
    "apply_url",
    "ingest_profile",
    "date_posted",
    "salary_min",
    "salary_max",
    "currency",
    "role_family",
    "actual_role_id",
    "actual_role_name",
    "role_level_id",
    "experience_level",
    "experience_bucket",
    "jd_summary",
    "jd_clean",
    "jd_requirements",
    "jd_responsibilities",
    "required_skills",
    "good_to_have_skills",
    "tools",
    "programming_languages",
    "databases",
    "frameworks",
    "student_preparation_topics",
    "mapping_confidence",
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
    """Test and explicit schema helper. The daily command does not call this."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def catalog_experience_bucket(*, experience_level: str | None, title: str | None, job_type: str | None) -> str:
    """JobReady catalog vocabulary. Do not store the classifier's raw band here."""
    blob = f"{title or ''} {job_type or ''}".lower()
    if "intern" in blob:
        return "Internship"
    level = (experience_level or "").strip().lower()
    if level == "fresher":
        return "Fresher"
    if level == "entry":
        return "Entry (1-2 yrs)"
    if level == "experienced":
        return "Experienced"
    if "fresher" in blob or "trainee" in blob or "graduate" in blob:
        return "Fresher"
    return "Unknown / Unclassified"


def _table_exists(cur) -> bool:
    cur.execute("SELECT to_regclass('public.validated_jobs')")
    return cur.fetchone()[0] is not None


def schema_problems(conn) -> list[str]:
    """Read-only. Empty when the catalog is ready for a daily run."""
    problems: list[str] = []
    with conn.cursor() as cur:
        if not _table_exists(cur):
            return ["validated_jobs table is missing"]
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'validated_jobs'
            """
        )
        present = {row[0] for row in cur.fetchall()}
        missing = [name for name in REQUIRED_COLUMNS if name not in present]
        if missing:
            problems.append("missing columns: " + ", ".join(missing))
        cur.execute(
            """
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'validated_jobs'
            """
        )
        indexes = {row[0] for row in cur.fetchall()}
        missing_indexes = [name for name in REQUIRED_INDEXES if name not in indexes]
        if missing_indexes:
            problems.append("missing indexes: " + ", ".join(missing_indexes))
    return problems


def identity_duplicates(conn) -> dict[str, int]:
    """Read-only duplicate counts. Zero means a unique source identity is safe."""
    with conn.cursor() as cur:
        if not _table_exists(cur):
            return {"source_id": 0, "source_url": 0}
        cur.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT source_id
                FROM validated_jobs
                WHERE source_id IS NOT NULL AND btrim(source_id) <> ''
                GROUP BY source_id
                HAVING COUNT(*) > 1
            ) dupes
            """
        )
        source_id = int(cur.fetchone()[0])
        cur.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT lower(source), job_url
                FROM validated_jobs
                GROUP BY lower(source), job_url
                HAVING COUNT(*) > 1
            ) dupes
            """
        )
        source_url = int(cur.fetchone()[0])
    return {"source_id": source_id, "source_url": source_url}


def apply_schema(conn) -> None:
    """Idempotent catalog DDL. Refuses when identity duplicates already exist."""
    with conn.cursor() as cur:
        exists = _table_exists(cur)
    if exists:
        dupes = identity_duplicates(conn)
        if dupes["source_id"] or dupes["source_url"]:
            raise SystemExit(
                "SCHEMA_REFUSED duplicate source identities exist; "
                f"source_id groups={dupes['source_id']} source_url groups={dupes['source_url']}"
            )
    ensure_schema(conn)


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


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _norm(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "[{":
            try:
                return json.loads(text)
            except Exception:
                return value
    return value


def _same(left: Any, right: Any) -> bool:
    left = _norm(left)
    right = _norm(right)
    if left is None and right is None:
        return True
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except Exception:
            return False
    return left == right


def _merged_content(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Keep stored date, salary, JD, and skill values when the new scrape omitted them."""
    merged = dict(current)
    for field in CONTENT_FIELDS:
        if field not in incoming:
            continue
        value = incoming.get(field)
        if field in PRESERVE_WHEN_BLANK and _blank(value):
            continue
        merged[field] = value
    return merged


def _content_changed(current: dict[str, Any], merged: dict[str, Any]) -> bool:
    return any(not _same(current.get(field), merged.get(field)) for field in CONTENT_FIELDS)


def _load_content(cur, job_id: str) -> dict[str, Any]:
    columns = ", ".join(CONTENT_FIELDS)
    cur.execute(f"SELECT {columns} FROM validated_jobs WHERE job_id = %s", (job_id,))
    row = cur.fetchone()
    return {field: row[index] for index, field in enumerate(CONTENT_FIELDS)}


def upsert_jobs(conn, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Insert new source identities and refresh jobs seen again.

    A search hit does not change link_status. New rows start active.
    Existing expired rows stay expired until a validator confirms them active.
    scraped_created_at is immutable. synced_at advances only for this scrape.
    Blank date, salary, JD, and skill values do not erase stored values.
    """
    counts: dict[str, Any] = {
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "reobserved": 0,
        "new_ids": [],
        "reobserved_ids": [],
    }
    if not jobs:
        return counts
    with conn.cursor() as cur:
        for job in jobs:
            source_id = job["source_id"]
            existing = _find(cur, source_id, job["source"], job["job_url"])
            if existing:
                job_id, _created_at, _updated_at, _previous_status, _stored_source_id = existing
                current = _load_content(cur, job_id)
                merged = _merged_content(current, job)
                changed = _content_changed(current, merged)
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
                        ingest_profile = %s,
                        date_posted = %s,
                        salary_min = %s,
                        salary_max = %s,
                        currency = %s,
                        role_family = %s,
                        actual_role_id = %s,
                        actual_role_name = %s,
                        role_level_id = %s,
                        experience_level = %s,
                        experience_bucket = %s,
                        jd_summary = %s,
                        jd_clean = %s,
                        jd_requirements = %s,
                        jd_responsibilities = %s,
                        required_skills = %s,
                        good_to_have_skills = %s,
                        tools = %s,
                        programming_languages = %s,
                        databases = %s,
                        frameworks = %s,
                        student_preparation_topics = %s,
                        mapping_confidence = %s,
                        manual_review_needed = %s,
                        approved_status = %s,
                        has_jd_clean = %s,
                        synced_at = NOW(),
                        updated_at = CASE WHEN %s THEN NOW() ELSE updated_at END
                    WHERE job_id = %s
                    """,
                    (
                        source_id,
                        job["source"],
                        merged.get("title"),
                        merged.get("company"),
                        merged.get("location"),
                        merged.get("job_type"),
                        merged.get("job_url"),
                        merged.get("apply_url"),
                        merged.get("ingest_profile"),
                        merged.get("date_posted"),
                        merged.get("salary_min"),
                        merged.get("salary_max"),
                        merged.get("currency"),
                        merged.get("role_family"),
                        merged.get("actual_role_id"),
                        merged.get("actual_role_name"),
                        merged.get("role_level_id"),
                        merged.get("experience_level"),
                        merged.get("experience_bucket"),
                        merged.get("jd_summary"),
                        merged.get("jd_clean"),
                        _json(merged.get("jd_requirements")),
                        _json(merged.get("jd_responsibilities")),
                        _json(merged.get("required_skills")),
                        _json(merged.get("good_to_have_skills")),
                        _json(merged.get("tools")),
                        _json(merged.get("programming_languages")),
                        _json(merged.get("databases")),
                        _json(merged.get("frameworks")),
                        _json(merged.get("student_preparation_topics")),
                        merged.get("mapping_confidence"),
                        merged.get("manual_review_needed"),
                        merged.get("approved_status"),
                        bool(merged.get("jd_clean")),
                        changed,
                        job_id,
                    ),
                )
                counts["reobserved"] += 1
                counts["reobserved_ids"].append(job_id)
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
                    job_url, apply_url, link_status, ingest_profile, date_posted,
                    salary_min, salary_max, currency, role_family,
                    actual_role_id, actual_role_name, role_level_id, experience_level,
                    experience_bucket, jd_summary, jd_clean, jd_requirements, jd_responsibilities,
                    required_skills, good_to_have_skills, tools, programming_languages,
                    databases, frameworks, student_preparation_topics,
                    manual_review_needed, approved_status, mapping_confidence, has_jd_clean,
                    scraped_created_at, synced_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, 'active', %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
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
                    job.get("ingest_profile"),
                    job.get("date_posted"),
                    job.get("salary_min"),
                    job.get("salary_max"),
                    job.get("currency"),
                    job.get("role_family"),
                    job.get("actual_role_id"),
                    job.get("actual_role_name"),
                    job.get("role_level_id"),
                    job.get("experience_level"),
                    job.get("experience_bucket"),
                    job.get("jd_summary"),
                    job.get("jd_clean"),
                    _json(job.get("jd_requirements")),
                    _json(job.get("jd_responsibilities")),
                    _json(job.get("required_skills")),
                    _json(job.get("good_to_have_skills")),
                    _json(job.get("tools")),
                    _json(job.get("programming_languages")),
                    _json(job.get("databases")),
                    _json(job.get("frameworks")),
                    _json(job.get("student_preparation_topics")),
                    job.get("manual_review_needed"),
                    job.get("approved_status"),
                    job.get("mapping_confidence"),
                    bool(job.get("jd_clean")),
                ),
            )
            counts["new"] += 1
            counts["new_ids"].append(job_id)
    conn.commit()
    return counts


def apply_link_observations(conn, observations: list[Any]) -> dict[str, int]:
    """Apply validator results. Does not move synced_at.

    confirmed_expired and confirmed_active are the only status changes.
    An inconclusive result records the check and leaves link_status alone.
    """
    counts = {"checked": 0, "confirmed_active": 0, "confirmed_expired": 0, "inconclusive": 0}
    with conn.cursor() as cur:
        for item in observations:
            if isinstance(item, tuple):
                job_id, _previous, observation = item
                reason = observation
            else:
                job_id = item.job_id
                observation = item.observation
                reason = getattr(item, "reason", observation)
            label = observation if not reason or reason == observation else f"{observation}:{reason}"
            label = label[:200]
            counts["checked"] += 1
            if observation == "confirmed_expired":
                counts["confirmed_expired"] += 1
                cur.execute(
                    """
                    UPDATE validated_jobs SET
                        link_status = 'expired',
                        updated_at = CASE
                            WHEN link_status IS DISTINCT FROM 'expired' THEN NOW()
                            ELSE updated_at
                        END,
                        link_checked_at = NOW(),
                        last_link_observation = %s
                    WHERE job_id = %s
                    """,
                    (label, job_id),
                )
            elif observation == "confirmed_active":
                counts["confirmed_active"] += 1
                cur.execute(
                    """
                    UPDATE validated_jobs SET
                        link_status = 'active',
                        updated_at = CASE
                            WHEN link_status IS DISTINCT FROM 'active' THEN NOW()
                            ELSE updated_at
                        END,
                        link_checked_at = NOW(),
                        last_link_observation = %s
                    WHERE job_id = %s
                    """,
                    (label, job_id),
                )
            else:
                counts["inconclusive"] += 1
                cur.execute(
                    """
                    UPDATE validated_jobs SET
                        link_checked_at = NOW(),
                        last_link_observation = %s
                    WHERE job_id = %s
                    """,
                    (label, job_id),
                )
    conn.commit()
    return counts


def select_validation_candidates(
    conn,
    *,
    limit: int = DEFAULT_VALIDATION_LIMIT,
    full_audit: bool = False,
    priority_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Rotate through the catalog. Half of a normal budget is reserved for unconfirmed rows."""
    columns = (
        "job_id, source, job_url, apply_url, link_status, link_checked_at, last_link_observation"
    )
    names = ["job_id", "source", "job_url", "apply_url", "link_status", "link_checked_at", "last_link_observation"]
    priority_ids = [job_id for job_id in (priority_ids or []) if job_id]
    with conn.cursor() as cur:
        if full_audit:
            cur.execute(
                f"SELECT {columns} FROM validated_jobs ORDER BY link_checked_at NULLS FIRST, job_id"
            )
            return [_row_dict(names, row) for row in cur.fetchall()]
        selected: list[dict[str, Any]] = []
        seen: list[str] = []
        priority_budget = min(len(priority_ids), max(limit // 2, 1 if priority_ids else 0))
        if priority_ids and priority_budget:
            cur.execute(
                f"""
                SELECT {columns} FROM validated_jobs
                WHERE job_id = ANY(%s)
                ORDER BY link_checked_at NULLS FIRST, job_id
                LIMIT %s
                """,
                (priority_ids, priority_budget),
            )
            for row in cur.fetchall():
                item = _row_dict(names, row)
                selected.append(item)
                seen.append(item["job_id"])
        remaining = max(limit - len(selected), 0)
        if remaining:
            if seen:
                cur.execute(
                    f"""
                    SELECT {columns} FROM validated_jobs
                    WHERE NOT (job_id = ANY(%s))
                    ORDER BY link_checked_at NULLS FIRST, job_id
                    LIMIT %s
                    """,
                    (seen, remaining),
                )
            else:
                cur.execute(
                    f"""
                    SELECT {columns} FROM validated_jobs
                    ORDER BY link_checked_at NULLS FIRST, job_id
                    LIMIT %s
                    """,
                    (remaining,),
                )
            selected.extend(_row_dict(names, row) for row in cur.fetchall())
    return selected


def _row_dict(names: list[str], row: tuple) -> dict[str, Any]:
    return {name: row[index] for index, name in enumerate(names)}


def mark_confirmed_expired(conn, job_ids: list[str]) -> int:
    """Retain the row and set link_status = expired. Does not delete or move synced_at."""
    if not job_ids:
        return 0
    observations = [(job_id, "active", "confirmed_expired") for job_id in job_ids]
    return apply_link_observations(conn, observations)["confirmed_expired"]


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
