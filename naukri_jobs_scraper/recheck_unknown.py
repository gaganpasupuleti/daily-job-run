#!/usr/bin/env python3
"""Re-check jobs currently marked unknown/active for expiry."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

from db import TABLE_NAME, get_database_url, init_schema, status_summary, update_job_statuses
from naukri_api import NaukriSearchClient

ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--status", default="unknown", help="Which status bucket to re-check")
    parser.add_argument("--delay", type=float, default=0.8)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(ROOT / args.env_file)

    with psycopg2.connect(get_database_url()) as conn:
        init_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT job_id, title FROM {TABLE_NAME} WHERE status = %s ORDER BY scraped_at",
                (args.status,),
            )
            rows = cur.fetchall()

    print(f"Re-checking {len(rows)} jobs with status={args.status!r}")
    client = NaukriSearchClient(delay_seconds=args.delay)
    updates: list[tuple[str, str]] = []
    try:
        for idx, (job_id, title) in enumerate(rows, start=1):
            status = client.check_job_status(job_id)
            updates.append((job_id, status))
            print(f"  [{idx}/{len(rows)}] {status:8} {title[:60]}")
            if idx < len(rows):
                time.sleep(args.delay)
    finally:
        client.close()

    counts = update_job_statuses(updates)
    print("\nResults:", counts)
    print("DB summary:")
    for status, count in status_summary():
        print(f"  {status}: {count}")


if __name__ == "__main__":
    main()
