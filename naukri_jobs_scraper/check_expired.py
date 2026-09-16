#!/usr/bin/env python3
"""Check existing Naukri jobs in Postgres for expired / closed listings."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from dotenv import load_dotenv

from db import list_job_ids_not_in, status_summary, update_job_statuses
from naukri_api import NaukriSearchClient

ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check stored Naukri jobs for expiry.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--skip-ids-file",
        default="",
        help="Optional JSON file with list of job_ids seen in latest scrape (skip re-check).",
    )
    parser.add_argument("--limit", type=int, default=0, help="Max old jobs to check (0 = all).")
    parser.add_argument("--delay", type=float, default=0.8)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(ROOT / args.env_file)

    seen: set[str] = set()
    if args.skip_ids_file:
        import json

        payload = json.loads(Path(args.skip_ids_file).read_text(encoding="utf-8"))
        if isinstance(payload, list):
            seen = {str(x) for x in payload}
        elif isinstance(payload, dict) and "job_ids" in payload:
            seen = {str(x) for x in payload["job_ids"]}

    candidates = list_job_ids_not_in(seen, status="active")
    if args.limit > 0:
        candidates = candidates[: args.limit]

    print(f"Checking {len(candidates)} old active jobs not seen in latest scrape...")
    client = NaukriSearchClient(delay_seconds=args.delay)
    updates: list[tuple[str, str]] = []

    try:
        for idx, (job_id, title) in enumerate(candidates, start=1):
            status = client.check_job_status(job_id)
            updates.append((job_id, status))
            if idx % 25 == 0 or idx == len(candidates):
                print(f"  checked {idx}/{len(candidates)} (latest={status})")
            if idx < len(candidates):
                time.sleep(args.delay)
    finally:
        client.close()

    counts = update_job_statuses(updates)
    print("\nExpiry check results:")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")

    print("\nDB status summary:")
    for status, count in status_summary():
        print(f"  {status}: {count}")


if __name__ == "__main__":
    main()
