"""JobReady validated_jobs catalog.

This module owns catalog schema, identity, CQJ allocation, and upserts.
The daily pipeline must not write any other database.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import psycopg2
from psycopg2.extras import Json, execute_values

# Session lock for the whole daily run. Released on disconnect.
RUN_LOCK_KEY = 87264011
# Transaction lock around new CQJ allocation.
ALLOC_LOCK_KEY = 87264021
CATALOG_APPLICATION_NAME = "daily-job-run"
LOCK_APPLICATION_NAME = "daily-job-run-lock"
# Transaction-local only. A timeout aborts the whole catalog batch.
LOCK_TIMEOUT = "10s"
STATEMENT_TIMEOUT = "120s"
IDLE_IN_TRANSACTION_TIMEOUT = "180s"

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

-- Lifecycle evidence (Phase 1). Starts NULL and empty: nothing is backfilled.
-- last_confirmed_open_at / last_confirmed_closed_at: latest observed_at of a trusted
-- confirmed_active / confirmed_expired observation. inconclusive never advances either.
ALTER TABLE validated_jobs ADD COLUMN IF NOT EXISTS last_confirmed_open_at TIMESTAMPTZ;
ALTER TABLE validated_jobs ADD COLUMN IF NOT EXISTS last_confirmed_closed_at TIMESTAMPTZ;

-- Append-only: one row per validation attempt. Repeated identical results are legitimate,
-- so there is no uniqueness beyond id. observed_at is when the provider was probed;
-- recorded_at is when the row was persisted. Metadata only: no bodies, headers, or credentials.
CREATE TABLE IF NOT EXISTS jobready_link_observations (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    observation TEXT NOT NULL,
    reason TEXT NOT NULL,
    provider TEXT NOT NULL,
    previous_link_status TEXT,
    resulting_link_status TEXT,
    run_id TEXT,
    requested_url TEXT,
    final_url TEXT,
    http_status INTEGER,
    evidence_version TEXT NOT NULL,
    CONSTRAINT jobready_link_observations_job_id_fkey
        FOREIGN KEY (job_id) REFERENCES validated_jobs (job_id) ON DELETE RESTRICT,
    CONSTRAINT jobready_link_observations_observation_check
        CHECK (observation IN ('confirmed_active', 'confirmed_expired', 'inconclusive'))
);

CREATE INDEX IF NOT EXISTS idx_jobready_link_observations_job_time
    ON jobready_link_observations (job_id, observed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_jobready_link_observations_observed_at
    ON jobready_link_observations (observed_at DESC);
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

# Lifecycle evidence objects are deliberately absent from REQUIRED_COLUMNS and REQUIRED_INDEXES:
# the daily run neither reads nor writes them, so a catalog without them must still pass.
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


def connect(url: str | None = None, *, application_name: str = CATALOG_APPLICATION_NAME):
    return psycopg2.connect(
        url or catalog_url(),
        connect_timeout=20,
        application_name=application_name,
    )


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
        self.conn = connect(self.url, application_name=LOCK_APPLICATION_NAME)
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


def allocate_job_ids(cur, count: int, when: datetime | None = None) -> list[str]:
    """Reserve a contiguous CQJ block. The advisory lock is held until commit or rollback."""
    if count <= 0:
        return []
    moment = when or _now()
    day = moment.strftime("%Y%m%d")
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
    return [f"CQJ-{day}-{current + offset:04d}" for offset in range(1, count + 1)]


def allocate_job_id(conn, when: datetime | None = None) -> str:
    """Next CQJ-YYYYMMDD-NNNN. The daily batch path allocates a block instead."""
    with conn.cursor() as cur:
        return allocate_job_ids(cur, 1, when)[0]


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


FETCH_COLUMNS = (
    "job_id",
    "source_id",
    "source",
    "job_url",
    "link_status",
    "scraped_created_at",
    *CONTENT_FIELDS,
)

JSON_FIELDS = {
    "jd_requirements",
    "jd_responsibilities",
    "required_skills",
    "good_to_have_skills",
    "tools",
    "programming_languages",
    "databases",
    "frameworks",
    "student_preparation_topics",
}

RESOLVED_FIELDS = (
    "job_id",
    "is_new",
    "content_changed",
    "source_id",
    "source",
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
    "manual_review_needed",
    "approved_status",
    "mapping_confidence",
    "has_jd_clean",
)

RESOLVED_TABLE_SQL = """
CREATE TEMP TABLE catalog_resolved (
    job_id TEXT,
    is_new BOOLEAN NOT NULL,
    content_changed BOOLEAN NOT NULL,
    source_id TEXT,
    source TEXT,
    title TEXT,
    company TEXT,
    location TEXT,
    job_type TEXT,
    job_url TEXT,
    apply_url TEXT,
    ingest_profile TEXT,
    date_posted TIMESTAMPTZ,
    salary_min NUMERIC,
    salary_max NUMERIC,
    currency TEXT,
    role_family TEXT,
    actual_role_id TEXT,
    actual_role_name TEXT,
    role_level_id TEXT,
    experience_level TEXT,
    experience_bucket TEXT,
    jd_summary TEXT,
    jd_clean TEXT,
    jd_requirements JSONB,
    jd_responsibilities JSONB,
    required_skills JSONB,
    good_to_have_skills JSONB,
    tools JSONB,
    programming_languages JSONB,
    databases JSONB,
    frameworks JSONB,
    student_preparation_topics JSONB,
    manual_review_needed BOOLEAN,
    approved_status TEXT,
    mapping_confidence NUMERIC,
    has_jd_clean BOOLEAN
) ON COMMIT DROP
"""


def release_read_transaction(conn) -> None:
    """End a read-only transaction before a long scrape.

    Leaving it open makes the catalog session idle in a transaction for the
    whole scrape and freezes NOW() at the start of that transaction.
    """
    conn.rollback()


def apply_write_timeouts(cur) -> None:
    """Bound this transaction only. Do not change database-wide settings."""
    for name, value in (
        ("lock_timeout", LOCK_TIMEOUT),
        ("statement_timeout", STATEMENT_TIMEOUT),
        ("idle_in_transaction_session_timeout", IDLE_IN_TRANSACTION_TIMEOUT),
    ):
        cur.execute("SELECT set_config(%s, %s, true)", (name, value))
        cur.fetchone()


def _empty_upsert_counts() -> dict[str, Any]:
    return {
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "reobserved": 0,
        "new_ids": [],
        "reobserved_ids": [],
        "catalog_plan_sec": 0.0,
        "catalog_write_sec": 0.0,
    }


def _record(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(FETCH_COLUMNS, row, strict=True))


def _prefetch_existing(cur, jobs: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    """Load matching catalog rows in a few statements, not one lookup per job."""
    columns = ", ".join(FETCH_COLUMNS)
    source_ids = [job["source_id"] for job in jobs if str(job.get("source_id") or "").strip()]
    by_source_id: dict[str, dict[str, Any]] = {}
    if source_ids:
        cur.execute(
            f"SELECT {columns} FROM validated_jobs WHERE source_id = ANY(%s)",
            (source_ids,),
        )
        for row in cur.fetchall():
            record = _record(row)
            if record["source_id"]:
                by_source_id[record["source_id"]] = record
    missing = [job for job in jobs if job.get("source_id") not in by_source_id]
    by_url: dict[tuple[str, str], dict[str, Any]] = {}
    if missing:
        cur.execute(
            """
            CREATE TEMP TABLE incoming_identity (
                source TEXT,
                job_url TEXT
            ) ON COMMIT DROP
            """
        )
        execute_values(
            cur,
            "INSERT INTO incoming_identity (source, job_url) VALUES %s",
            [(job.get("source") or "", job.get("job_url") or "") for job in missing],
            page_size=max(len(missing), 1),
        )
        qualified = ", ".join(f"v.{name}" for name in FETCH_COLUMNS)
        cur.execute(
            f"""
            SELECT {qualified}
            FROM incoming_identity AS i
            JOIN validated_jobs AS v
              ON lower(v.source) = lower(i.source)
             AND v.job_url = i.job_url
            ORDER BY v.scraped_created_at NULLS LAST, v.job_id
            """
        )
        for row in cur.fetchall():
            record = _record(row)
            key = ((record["source"] or "").lower(), record["job_url"] or "")
            by_url.setdefault(key, record)
    return by_source_id, by_url


def _match_existing(
    job: dict[str, Any],
    by_source_id: dict[str, dict[str, Any]],
    by_url: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any] | None:
    source_id = str(job.get("source_id") or "").strip()
    if source_id and source_id in by_source_id:
        return by_source_id[source_id]
    return by_url.get(((job.get("source") or "").lower(), job.get("job_url") or ""))


def _plan_rows(
    jobs: list[dict[str, Any]],
    by_source_id: dict[str, dict[str, Any]],
    by_url: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Last incoming row wins when one batch repeats an identity."""
    planned: dict[tuple[str, str], dict[str, Any]] = {}
    for job in jobs:
        existing = _match_existing(job, by_source_id, by_url)
        if existing:
            merged = _merged_content(existing, job)
            stored_source_id = str(existing.get("source_id") or "").strip()
            merged["source_id"] = stored_source_id or job.get("source_id")
            entry = {
                "is_new": False,
                "content_changed": _content_changed(existing, merged),
                "job_id": existing["job_id"],
                "payload": merged,
            }
            planned[("id", existing["job_id"])] = entry
            continue
        payload = dict(job)
        payload["source_id"] = job.get("source_id")
        entry = {
            "is_new": True,
            "content_changed": True,
            "job_id": None,
            "payload": payload,
        }
        planned[("new", str(job.get("source_id") or ""), str(job.get("job_url") or ""))] = entry
    return list(planned.values())


def _resolved_tuple(entry: dict[str, Any]) -> tuple[Any, ...]:
    payload = entry["payload"]
    values: list[Any] = []
    for name in RESOLVED_FIELDS:
        if name == "job_id":
            values.append(entry["job_id"])
        elif name == "is_new":
            values.append(entry["is_new"])
        elif name == "content_changed":
            values.append(entry["content_changed"])
        elif name == "has_jd_clean":
            values.append(bool(payload.get("jd_clean")))
        elif name in JSON_FIELDS:
            values.append(_json(payload.get(name)))
        else:
            values.append(payload.get(name))
    return tuple(values)


def _write_resolved(cur, planned: list[dict[str, Any]]) -> None:
    cur.execute(RESOLVED_TABLE_SQL)
    execute_values(
        cur,
        f"INSERT INTO catalog_resolved ({', '.join(RESOLVED_FIELDS)}) VALUES %s",
        [_resolved_tuple(entry) for entry in planned],
        page_size=max(len(planned), 1),
    )
    cur.execute(
        """
        UPDATE validated_jobs AS v SET
            source_id = r.source_id,
            source = r.source,
            title = r.title,
            company = r.company,
            location = r.location,
            job_type = r.job_type,
            job_url = r.job_url,
            apply_url = r.apply_url,
            ingest_profile = r.ingest_profile,
            date_posted = r.date_posted,
            salary_min = r.salary_min,
            salary_max = r.salary_max,
            currency = r.currency,
            role_family = r.role_family,
            actual_role_id = r.actual_role_id,
            actual_role_name = r.actual_role_name,
            role_level_id = r.role_level_id,
            experience_level = r.experience_level,
            experience_bucket = r.experience_bucket,
            jd_summary = r.jd_summary,
            jd_clean = r.jd_clean,
            jd_requirements = r.jd_requirements,
            jd_responsibilities = r.jd_responsibilities,
            required_skills = r.required_skills,
            good_to_have_skills = r.good_to_have_skills,
            tools = r.tools,
            programming_languages = r.programming_languages,
            databases = r.databases,
            frameworks = r.frameworks,
            student_preparation_topics = r.student_preparation_topics,
            mapping_confidence = r.mapping_confidence,
            manual_review_needed = r.manual_review_needed,
            approved_status = r.approved_status,
            has_jd_clean = r.has_jd_clean,
            synced_at = NOW(),
            updated_at = CASE WHEN r.content_changed THEN NOW() ELSE v.updated_at END
        FROM catalog_resolved AS r
        WHERE v.job_id = r.job_id
          AND NOT r.is_new
        """
    )
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
        )
        SELECT
            job_id, source_id, source, title, company, location, job_type,
            job_url, apply_url, 'active', ingest_profile, date_posted,
            salary_min, salary_max, currency, role_family,
            actual_role_id, actual_role_name, role_level_id, experience_level,
            experience_bucket, jd_summary, jd_clean, jd_requirements, jd_responsibilities,
            required_skills, good_to_have_skills, tools, programming_languages,
            databases, frameworks, student_preparation_topics,
            manual_review_needed, approved_status, mapping_confidence, has_jd_clean,
            NOW(), NOW(), NOW()
        FROM catalog_resolved
        WHERE is_new
        """
    )


def _write_failure(exc: BaseException, stage: str) -> str:
    text = str(exc).lower()
    if "://" in text or "password" in text:
        reason = "catalog write failed"
    elif "lock timeout" in text:
        reason = "lock_timeout"
    elif "statement timeout" in text:
        reason = "statement_timeout"
    elif "idle" in text and "transaction" in text:
        reason = "idle_in_transaction_timeout"
    else:
        reason = "catalog write failed"
    return f"CATALOG_WRITE_FAILED stage={stage} {reason}"


def upsert_jobs(
    conn,
    jobs: list[dict[str, Any]],
    *,
    on_plan_complete: Callable[[], None] | None = None,
    on_commit: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Insert new source identities and refresh jobs seen again.

    The batch is one transaction: prefetch, one CQJ block, then set-based
    update and insert. A timeout or write error rolls the whole batch back.
    on_plan_complete runs after planning and before the write; if it raises,
    the batch rolls back as a plan failure.
    on_commit runs as soon as conn.commit() succeeds, before any post-commit
    logging, so a later failure cannot be mistaken for an uncommitted write.
    A search hit does not change link_status. New rows start active.
    Existing expired rows stay expired until a validator confirms them active.
    scraped_created_at is immutable. synced_at advances only for this scrape.
    Blank date, salary, JD, and skill values do not erase stored values.
    """
    counts = _empty_upsert_counts()
    if not jobs:
        return counts
    stage = "catalog_plan"
    started = time.perf_counter()
    try:
        with conn.cursor() as cur:
            apply_write_timeouts(cur)
            by_source_id, by_url = _prefetch_existing(cur, jobs)
            planned = _plan_rows(jobs, by_source_id, by_url)
            new_rows = [entry for entry in planned if entry["is_new"]]
            existing_rows = [entry for entry in planned if not entry["is_new"]]
            if new_rows:
                for entry, job_id in zip(new_rows, allocate_job_ids(cur, len(new_rows)), strict=True):
                    entry["job_id"] = job_id
            counts["catalog_plan_sec"] = round(time.perf_counter() - started, 3)
            print(
                f"STAGE catalog_plan existing={len(existing_rows)} new={len(new_rows)} "
                f"elapsed={counts['catalog_plan_sec']}",
                flush=True,
            )
            if on_plan_complete is not None:
                on_plan_complete()
            stage = "catalog_write"
            write_started = time.perf_counter()
            print(
                f"STAGE catalog_write updates={len(existing_rows)} inserts={len(new_rows)} start",
                flush=True,
            )
            if planned:
                _write_resolved(cur, planned)
        conn.commit()
    except Exception as exc:
        conn.rollback()
        raise SystemExit(_write_failure(exc, stage)) from None
    if on_commit is not None:
        on_commit()
    counts["catalog_write_sec"] = round(time.perf_counter() - write_started, 3)
    print(
        f"STAGE catalog_write committed elapsed={counts['catalog_write_sec']}",
        flush=True,
    )
    for entry in existing_rows:
        counts["reobserved"] += 1
        counts["reobserved_ids"].append(entry["job_id"])
        if entry["content_changed"]:
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
    for entry in new_rows:
        counts["new"] += 1
        counts["new_ids"].append(entry["job_id"])
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
