#!/usr/bin/env python3
"""LEGACY. Copy scraped jobs from testing DB to production naukri_scraper table."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

from db import TABLE_NAME, init_schema

ROOT = Path(__file__).resolve().parent

SOURCE_TABLE = "naukri_jobs"  # testing DB still uses old table name


def load_url(env_file: str) -> str:
    load_dotenv(ROOT / env_file, override=True)
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(f"DATABASE_URL missing in {env_file}")
    return url


def main() -> None:
    source_url = load_url(".env.testing")
    prod_url = load_url(".env")

    with psycopg2.connect(source_url) as src_conn:
        with src_conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT job_id, title, company, location, experience, salary,
                       skills, description, job_url, posted_label, search_keyword,
                       experience_filter, role_family, role_family_name, scraped_at
                FROM {SOURCE_TABLE}
                ORDER BY scraped_at
                """
            )
            rows = cur.fetchall()

    print(f"Read {len(rows)} rows from testing.{SOURCE_TABLE}")

    with psycopg2.connect(prod_url) as prod_conn:
        init_schema(prod_conn)
        insert_sql = f"""
            INSERT INTO {TABLE_NAME} (
                job_id, title, company, location, experience, salary,
                skills, description, job_url, posted_label, search_keyword,
                experience_filter, role_family, role_family_name, scraped_at
            ) VALUES %s
            ON CONFLICT (job_id) DO UPDATE SET
                title = EXCLUDED.title,
                company = EXCLUDED.company,
                location = EXCLUDED.location,
                experience = EXCLUDED.experience,
                salary = EXCLUDED.salary,
                skills = EXCLUDED.skills,
                description = EXCLUDED.description,
                job_url = EXCLUDED.job_url,
                posted_label = EXCLUDED.posted_label,
                search_keyword = EXCLUDED.search_keyword,
                experience_filter = EXCLUDED.experience_filter,
                role_family = COALESCE(NULLIF(EXCLUDED.role_family, ''), {TABLE_NAME}.role_family),
                role_family_name = COALESCE(NULLIF(EXCLUDED.role_family_name, ''), {TABLE_NAME}.role_family_name),
                scraped_at = EXCLUDED.scraped_at
            RETURNING (xmax = 0) AS inserted
        """
        with prod_conn.cursor() as cur:
            results = execute_values(cur, insert_sql, rows, fetch=True)
            cur.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
            total = cur.fetchone()[0]
            cur.execute(
                f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE role_family IS NOT NULL AND role_family <> ''"
            )
            tagged = cur.fetchone()[0]
        prod_conn.commit()

    inserted = sum(1 for row in results if row[0])
    updated = len(results) - inserted
    print(f"Pushed to production.{TABLE_NAME}: {inserted} new, {updated} updated")
    print(f"Production total rows: {total}")
    print(f"Production tagged with family: {tagged}")


if __name__ == "__main__":
    main()
