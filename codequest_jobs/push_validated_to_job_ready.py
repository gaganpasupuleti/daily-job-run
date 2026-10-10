#!/usr/bin/env python3
"""LEGACY. Push CodeQuest scraped_jobs into JobReady.

The daily pipeline does not use this module. Schema lives in jobready_catalog.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values, Json

ROOT = Path(__file__).resolve().parent
SCRAPER_ENV = ROOT.parent / "naukri_jobs_scraper" / ".env"
load_dotenv(SCRAPER_ENV)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobready_catalog import SCHEMA_SQL  # noqa: E402
from run_logger import finish_push_run, start_push_run  # noqa: E402


FETCH_SQL = """
SELECT
    s.job_id,
    s.id AS source_id,
    s.source,
    s.title,
    s.company,
    s.location,
    s.job_type,
    s.job_url,
    s.apply_url,
    s.link_status,
    s.ingest_profile,
    s.date_posted,
    s.salary_min,
    s.salary_max,
    s.currency,
    s.created_at AS scraped_created_at,
    e.actual_role_id,
    e.actual_role_name,
    r.role_family,
    e.role_level_id,
    e.experience_level,
    e.jd_summary,
    e.jd_clean,
    e.jd_responsibilities,
    e.jd_requirements,
    e.keywords_must,
    e.keywords_nice,
    e.practice_focus,
    e.required_skills,
    e.good_to_have_skills,
    e.tools,
    e.programming_languages,
    e.databases,
    e.frameworks,
    e.student_preparation_topics,
    e.quiz_pack_id,
    e.mapping_confidence,
    e.manual_review_needed,
    e.approved_status
FROM scraped_jobs s
LEFT JOIN job_enrichments e ON e.job_id = s.job_id
LEFT JOIN job_roles r ON r.role_id = e.actual_role_id
WHERE COALESCE(s.link_status, '') = 'active'
  AND s.job_id IS NOT NULL
  AND COALESCE(s.job_url, '') <> ''
ORDER BY s.created_at DESC NULLS LAST
"""

UPSERT_SQL = """
INSERT INTO validated_jobs (
    job_id, source_id, source, title, company, location, job_type,
    job_url, apply_url, link_status, ingest_profile, date_posted,
    salary_min, salary_max, currency, scraped_created_at,
    actual_role_id, actual_role_name, role_family, role_level_id,
    experience_level, experience_bucket, jd_summary, jd_clean,
    jd_responsibilities, jd_requirements, keywords_must, keywords_nice,
    practice_focus, required_skills, good_to_have_skills, tools,
    programming_languages, databases, frameworks, student_preparation_topics,
    quiz_pack_id, mapping_confidence, manual_review_needed, approved_status,
    has_jd_clean
) VALUES %s
ON CONFLICT (job_id) DO UPDATE SET
    source = EXCLUDED.source,
    title = EXCLUDED.title,
    company = EXCLUDED.company,
    location = EXCLUDED.location,
    job_type = EXCLUDED.job_type,
    job_url = EXCLUDED.job_url,
    apply_url = EXCLUDED.apply_url,
    link_status = EXCLUDED.link_status,
    ingest_profile = EXCLUDED.ingest_profile,
    date_posted = EXCLUDED.date_posted,
    salary_min = EXCLUDED.salary_min,
    salary_max = EXCLUDED.salary_max,
    currency = EXCLUDED.currency,
    actual_role_id = EXCLUDED.actual_role_id,
    actual_role_name = EXCLUDED.actual_role_name,
    role_family = EXCLUDED.role_family,
    role_level_id = EXCLUDED.role_level_id,
    experience_level = EXCLUDED.experience_level,
    experience_bucket = EXCLUDED.experience_bucket,
    jd_summary = EXCLUDED.jd_summary,
    jd_clean = EXCLUDED.jd_clean,
    jd_responsibilities = EXCLUDED.jd_responsibilities,
    jd_requirements = EXCLUDED.jd_requirements,
    keywords_must = EXCLUDED.keywords_must,
    keywords_nice = EXCLUDED.keywords_nice,
    practice_focus = EXCLUDED.practice_focus,
    required_skills = EXCLUDED.required_skills,
    good_to_have_skills = EXCLUDED.good_to_have_skills,
    tools = EXCLUDED.tools,
    programming_languages = EXCLUDED.programming_languages,
    databases = EXCLUDED.databases,
    frameworks = EXCLUDED.frameworks,
    student_preparation_topics = EXCLUDED.student_preparation_topics,
    quiz_pack_id = EXCLUDED.quiz_pack_id,
    mapping_confidence = EXCLUDED.mapping_confidence,
    manual_review_needed = EXCLUDED.manual_review_needed,
    approved_status = EXCLUDED.approved_status,
    has_jd_clean = EXCLUDED.has_jd_clean,
    synced_at = NOW(),
    updated_at = NOW()
RETURNING (xmax = 0) AS inserted
"""


def experience_bucket(row: dict) -> str:
    exp = (row.get("experience_level") or "").lower()
    title = f"{row.get('title') or ''} {row.get('job_type') or ''}".lower()
    if "intern" in title:
        return "Internship"
    if exp == "fresher":
        return "Fresher"
    if exp == "entry":
        return "Entry (1-2 yrs)"
    if exp == "experienced":
        return "Experienced"
    if "fresher" in title or "trainee" in title or "graduate" in title:
        return "Fresher"
    return "Unknown / Unclassified"


def as_json(val):
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        return Json(val)
    if isinstance(val, str):
        text = val.strip()
        if not text:
            return None
        if text.startswith("[") or text.startswith("{"):
            try:
                return Json(json.loads(text))
            except Exception:
                return Json(text)
        return Json(text)
    return Json(val)


def dedupe_rows(rows: list[dict]) -> list[dict]:
    """Keep newest row per job_url and per job_id. Input must be newest-first."""
    out: list[dict] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for row in rows:
        job_id = (row.get("job_id") or "").strip()
        url = (row.get("job_url") or "").strip().lower()
        if not job_id:
            continue
        if job_id in seen_ids:
            continue
        if url and url in seen_urls:
            continue
        seen_ids.add(job_id)
        if url:
            seen_urls.add(url)
        out.append(row)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Push active jobs to job-ready validated_jobs")
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=0,
        help="Only push jobs scraped in the last N days (0 = all active)",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="After push, delete catalog rows not in this push set",
    )
    parser.add_argument(
        "--confirm-target",
        default="",
        help="Application database as host:port/database. Required before a prune also updates application jobs. Not a DSN.",
    )
    args = parser.parse_args()
    if args.prune:
        raise SystemExit(
            "PRUNE_REFUSED a filtered or partial snapshot cannot delete catalog rows. "
            "Expiration comes from confirmed link validation, not scrape absence."
        )

    source_url = (
        os.getenv("CODEQUEST_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
    ).strip()
    dest_url = (os.getenv("JOB_READY_DATABASE_URL") or "").strip()
    if not source_url:
        raise SystemExit("CODEQUEST_DATABASE_URL / DATABASE_URL missing")
    if not dest_url:
        raise SystemExit("JOB_READY_DATABASE_URL missing")
    app_url = (os.getenv("JOB_READY_APP_DATABASE_URL") or "").strip()
    if args.prune and app_url:
        from sync_job_ready_ui import assert_application_write_target

        print(f"TARGET {assert_application_write_target(args.confirm_target)}", flush=True)

    run_id, run_started = start_push_run()
    print(f"Push run log id={run_id}", flush=True)
    inserted = updated = total = 0
    by_source: list = []
    rows: list = []
    status = "success"
    err: str | None = None

    try:
        print("Connecting source (CodeQuest)...", flush=True)
        with psycopg2.connect(source_url, connect_timeout=20) as src:
            with src.cursor() as cur:
                sql = FETCH_SQL
                params: list = []
                if args.max_age_days > 0:
                    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
                        days=args.max_age_days
                    )
                    sql = FETCH_SQL.replace(
                        "ORDER BY s.created_at DESC NULLS LAST",
                        "AND s.created_at >= %s\nORDER BY s.created_at DESC NULLS LAST",
                    )
                    params.append(cutoff)
                    print(f"Filter: scraped within last {args.max_age_days} days (since {cutoff})", flush=True)
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        before_dedupe = len(rows)
        rows = dedupe_rows(rows)
        print(
            f"Active jobs fetched: {before_dedupe} | after URL/id dedupe: {len(rows)}",
            flush=True,
        )

        print("Connecting destination (job-ready)...", flush=True)
        with psycopg2.connect(dest_url, connect_timeout=20) as dest:
            with dest.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                dest.commit()
                print("Table validated_jobs ready", flush=True)

                values = []
                for row in rows:
                    clean = (row.get("jd_clean") or "").strip()
                    values.append(
                        (
                            row["job_id"],
                            row.get("source_id"),
                            row.get("source") or "unknown",
                            row.get("title") or "Untitled",
                            row.get("company"),
                            row.get("location"),
                            row.get("job_type"),
                            row["job_url"],
                            row.get("apply_url"),
                            row.get("link_status") or "active",
                            row.get("ingest_profile"),
                            row.get("date_posted"),
                            row.get("salary_min"),
                            row.get("salary_max"),
                            row.get("currency"),
                            row.get("scraped_created_at"),
                            row.get("actual_role_id"),
                            row.get("actual_role_name"),
                            row.get("role_family") or row.get("ingest_profile"),
                            row.get("role_level_id"),
                            row.get("experience_level"),
                            experience_bucket(row),
                            row.get("jd_summary"),
                            row.get("jd_clean"),
                            as_json(row.get("jd_responsibilities")),
                            as_json(row.get("jd_requirements")),
                            as_json(row.get("keywords_must")),
                            as_json(row.get("keywords_nice")),
                            as_json(row.get("practice_focus")),
                            as_json(row.get("required_skills")),
                            as_json(row.get("good_to_have_skills")),
                            as_json(row.get("tools")),
                            as_json(row.get("programming_languages")),
                            as_json(row.get("databases")),
                            as_json(row.get("frameworks")),
                            as_json(row.get("student_preparation_topics")),
                            row.get("quiz_pack_id"),
                            row.get("mapping_confidence"),
                            row.get("manual_review_needed"),
                            row.get("approved_status"),
                            len(clean) >= 80,
                        )
                    )

                batch = 100
                for i in range(0, len(values), batch):
                    chunk = values[i : i + batch]
                    results = execute_values(cur, UPSERT_SQL, chunk, fetch=True)
                    dest.commit()
                    ins = sum(1 for r in results if r[0])
                    inserted += ins
                    updated += len(results) - ins
                    print(
                        f"  batch {i // batch + 1}: +{ins} new / {len(results) - ins} updated",
                        flush=True,
                    )

                if args.prune:
                    raise SystemExit(
                        "PRUNE_REFUSED a filtered or partial snapshot cannot delete catalog rows. "
                        "Expiration comes from confirmed link validation, not scrape absence."
                    )

                cur.execute(
                    """
                    SELECT source, COUNT(*)
                    FROM validated_jobs
                    GROUP BY 1 ORDER BY 2 DESC
                    """
                )
                by_source = cur.fetchall()
                cur.execute("SELECT COUNT(*) FROM validated_jobs")
                total = cur.fetchone()[0]
    except Exception as exc:
        status = "failed"
        err = type(exc).__name__
        raise
    finally:
        try:
            finish_push_run(
                run_id,
                started=run_started,
                status=status,
                active_fetched=len(rows),
                inserted_count=inserted,
                updated_count=updated,
                total_after=total,
                by_source=by_source,
                error=err,
            )
        except Exception as log_exc:
            print(f"WARNING: failed to write push run log: {log_exc}", flush=True)

    print(f"Done. inserted={inserted} updated={updated} total={total}", flush=True)
    print("by_source:", by_source, flush=True)
    if args.prune and app_url:
        from sync_job_ready_ui import archive_ui_jobs_not_in_catalog

        print("Syncing job-ready application jobs...", flush=True)
        archive_ui_jobs_not_in_catalog(confirm_target=args.confirm_target)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR: push failed", file=sys.stderr, flush=True)
        raise
