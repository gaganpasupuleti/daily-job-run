#!/usr/bin/env python3
"""Manual Naukri job scraper — HTTP only, saves to Railway Postgres + local JSON."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from db import save_jobs
from naukri_api import NaukriSearchClient

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
DEFAULT_KEYWORDS = [
    "cyber security fresher",
    "cybersecurity intern",
    "soc analyst fresher",
    "information security intern",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape Naukri fresher/intern jobs (HTTP only).")
    parser.add_argument(
        "--keyword",
        action="append",
        help="Search keyword (repeatable). Defaults to cybersecurity fresher queries.",
    )
    parser.add_argument("--location", default="india", help="Location slug for Naukri search.")
    parser.add_argument("--max-pages", type=int, default=2, help="Pages per keyword.")
    parser.add_argument(
        "--experience",
        type=int,
        default=0,
        help="Naukri experience filter. 0 = fresher.",
    )
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="Disable fresher/intern title filter.",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Skip Railway Postgres; only write local JSON.",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="Alias for --no-db.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Dotenv file for DATABASE_URL (.env = production Postgres, .env.testing = test Postgres).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(ROOT / args.env_file)
    keywords = args.keyword or DEFAULT_KEYWORDS
    fresher_only = not args.include_all
    save_to_db = not (args.no_db or args.json_only)

    all_jobs = []
    client = NaukriSearchClient()

    try:
        for keyword in keywords:
            print(f"Searching: {keyword!r} (experience={args.experience})")
            jobs = client.search_jobs(
                keyword,
                location=args.location,
                max_pages=args.max_pages,
                experience=args.experience,
                fresher_only=fresher_only,
            )
            print(f"  -> {len(jobs)} jobs")
            all_jobs.extend(jobs)
    finally:
        client.close()

    # dedupe across keywords
    deduped = []
    seen = set()
    for job in all_jobs:
        if job.job_id in seen:
            continue
        seen.add(job.job_id)
        deduped.append(job)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / f"naukri_jobs_{stamp}.json"
    payload = {
        "meta": {
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "source": "naukri.com/jobapi/v3/search",
            "keywords": keywords,
            "location": args.location,
            "experience_filter": args.experience,
            "fresher_only": fresher_only,
            "total_jobs": len(deduped),
        },
        "jobs": [j.to_dict() for j in deduped],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved JSON: {json_path}")

    if save_to_db:
        inserted, updated = save_jobs(deduped)
        print(f"Railway Postgres: {inserted} new, {updated} updated")
    else:
        print("Skipped database (--no-db). Set DATABASE_URL in .env or .env.testing to enable.")

    print(f"Done. Total unique jobs: {len(deduped)}")


if __name__ == "__main__":
    main()
