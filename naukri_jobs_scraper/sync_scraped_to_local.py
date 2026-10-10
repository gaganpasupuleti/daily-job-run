#!/usr/bin/env python3
"""LEGACY. Backfill naukri_scraper from CodeQuest scraped_jobs (+ enrichment families)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)

BATCH = 100


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> None:
    url = (os.getenv("CODEQUEST_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("DATABASE_URL / CODEQUEST_DATABASE_URL missing")

    # Ensure source column exists (same schema.sql as scrape path)
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")

    conn = psycopg2.connect(url, connect_timeout=20)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute(schema)
    conn.commit()
    cur.execute("SET statement_timeout = '180s'")

    cur.execute(
        """
        SELECT
            s.id AS job_id,
            COALESCE(NULLIF(s.source, ''), 'unknown') AS source,
            COALESCE(NULLIF(s.title, ''), 'Untitled') AS title,
            COALESCE(s.company, '') AS company,
            COALESCE(s.location, '') AS location,
            COALESCE(s.job_type, '') AS experience,
            CASE
                WHEN s.salary_min IS NOT NULL OR s.salary_max IS NOT NULL
                THEN CONCAT(
                    COALESCE(s.currency, ''), ' ',
                    COALESCE(s.salary_min::text, ''),
                    CASE WHEN s.salary_min IS NOT NULL AND s.salary_max IS NOT NULL THEN '-' ELSE '' END,
                    COALESCE(s.salary_max::text, '')
                )
                ELSE ''
            END AS salary,
            '' AS skills,
            COALESCE(s.description, '') AS description,
            COALESCE(s.job_url, '') AS job_url,
            COALESCE(to_char(s.date_posted, 'YYYY-MM-DD'), '') AS posted_label,
            COALESCE(s.ingest_profile, '') AS search_keyword,
            0 AS experience_filter,
            COALESCE(NULLIF(r.role_family, ''), NULLIF(s.ingest_profile, ''), '') AS role_family,
            COALESCE(NULLIF(e.actual_role_name, ''), '') AS role_family_name,
            COALESCE(NULLIF(s.link_status, ''), 'active') AS status
        FROM scraped_jobs s
        LEFT JOIN job_enrichments e ON e.job_id = s.job_id
        LEFT JOIN job_roles r ON r.role_id = e.actual_role_id
        WHERE s.id IS NOT NULL
          AND COALESCE(s.job_url, '') <> ''
          AND NOT EXISTS (
                SELECT 1 FROM naukri_scraper n
                WHERE n.job_id = s.id OR n.job_url = s.job_url
          )
        ORDER BY s.created_at DESC NULLS LAST
        """
    )
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    log(f"Missing in naukri_scraper: {len(rows)}")

    insert_sql = """
        INSERT INTO naukri_scraper (
            job_id, source, title, company, location, experience, salary,
            skills, description, job_url, posted_label, search_keyword,
            experience_filter, role_family, role_family_name,
            status, scraped_at, last_seen_at, last_checked_at
        ) VALUES %s
        ON CONFLICT (job_id) DO UPDATE SET
            last_seen_at = NOW(),
            last_checked_at = NOW(),
            source = COALESCE(NULLIF(EXCLUDED.source, ''), naukri_scraper.source),
            role_family = COALESCE(NULLIF(EXCLUDED.role_family, ''), naukri_scraper.role_family),
            role_family_name = COALESCE(NULLIF(EXCLUDED.role_family_name, ''), naukri_scraper.role_family_name),
            status = COALESCE(NULLIF(EXCLUDED.status, ''), naukri_scraper.status)
        RETURNING (xmax = 0) AS inserted
    """
    template = (
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),NOW())"
    )

    inserted = 0
    updated = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        results = execute_values(cur, insert_sql, batch, template=template, fetch=True)
        conn.commit()
        batch_ins = sum(1 for r in results if r[0])
        inserted += batch_ins
        updated += len(results) - batch_ins
        log(f"  batch {i // BATCH + 1}: +{batch_ins} new, {len(results) - batch_ins} refreshed")

    cur.execute(
        """
        SELECT COALESCE(source, 'unknown'), COUNT(*)
        FROM naukri_scraper
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    local = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM naukri_scraper")
    local_total = cur.fetchone()[0]
    cur.execute(
        """
        SELECT COALESCE(source, 'unknown'), COUNT(*)
        FROM scraped_jobs
        GROUP BY 1 ORDER BY 2 DESC
        """
    )
    cq = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM scraped_jobs")
    cq_total = cur.fetchone()[0]
    cur.close()
    conn.close()

    log(f"Done. Inserted={inserted}, refreshed={updated}")
    log(f"naukri_scraper total={local_total} by_source={local}")
    log(f"scraped_jobs total={cq_total} by_source={cq}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise
