#!/usr/bin/env python3
"""Push latest scrape JSON into production naukri_scraper in small batches."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parent
TABLE = "naukri_scraper"
BATCH = 50


def log(msg: str) -> None:
    print(msg, flush=True)


def latest_json() -> Path:
    files = sorted((ROOT / "output").glob("naukri_18_families_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise FileNotFoundError("No scrape JSON found")
    return files[-1]


def main() -> None:
    load_dotenv(ROOT / ".env", override=True)
    import os

    url = os.environ["DATABASE_URL"]
    path = latest_json()
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_jobs = data.get("jobs", [])
    log(f"Source: {path.name} ({len(raw_jobs)} jobs)")

    rows = []
    for item in raw_jobs:
        job_id = str(item.get("job_id") or "")
        if not job_id:
            continue
        rows.append(
            (
                job_id,
                item.get("title") or "",
                item.get("company") or "",
                item.get("location") or "",
                item.get("experience") or "",
                item.get("salary") or "",
                item.get("skills") or "",
                item.get("description") or "",
                item.get("job_url") or "",
                item.get("posted_label") or "",
                item.get("search_keyword") or "",
                int(item.get("experience_filter") or 0),
                item.get("role_family") or "",
                item.get("role_family_name") or "",
                "active",
            )
        )

    log(f"Connecting...")
    conn = psycopg2.connect(url, connect_timeout=20)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '120s'")
    log("Connected. Inserting batches...")

    insert_sql = f"""
        INSERT INTO {TABLE} (
            job_id, title, company, location, experience, salary,
            skills, description, job_url, posted_label, search_keyword,
            experience_filter, role_family, role_family_name,
            status, scraped_at, last_seen_at, last_checked_at
        ) VALUES %s
        ON CONFLICT (job_id) DO NOTHING
        RETURNING job_id
    """
    template = "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),NOW())"

    inserted = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        results = execute_values(cur, insert_sql, batch, template=template, fetch=True)
        conn.commit()
        batch_new = len(results)
        inserted += batch_new
        log(f"  batch {i // BATCH + 1}: +{batch_new} new (skipped existing)")

    existed = len(rows) - inserted
    # Keep refresh optional — inserts with DO NOTHING already skip duplicates.
    cur.execute(f"SELECT status, COUNT(*) FROM {TABLE} GROUP BY 1 ORDER BY 2 DESC")
    summary = cur.fetchall()
    cur.execute(f"SELECT COUNT(*), COUNT(DISTINCT job_id) FROM {TABLE}")
    total, unique = cur.fetchone()
    cur.close()
    conn.close()

    log(f"Done. Pushed: {inserted} new, {existed} already existed (no duplicates)")
    log(f"DB total={total}, unique={unique}")
    for status, count in summary:
        log(f"  {status}: {count}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise
