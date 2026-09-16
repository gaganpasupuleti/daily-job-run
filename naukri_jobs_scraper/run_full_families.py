#!/usr/bin/env python3
"""Full Naukri scrape for all 18 CodeQuest role families (fresher + intern, India)."""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from db import list_job_ids_not_in, save_jobs, status_summary, update_job_statuses
from naukri_api import NaukriJob, NaukriSearchClient
from role_keywords import load_families

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape fresher/intern jobs for 18 role families.")
    parser.add_argument("--location", default="india")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--experience", type=int, default=0)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument(
        "--check-expired",
        action="store_true",
        help="After scrape, check old DB jobs not seen this run for expiry.",
    )
    parser.add_argument(
        "--expiry-limit",
        type=int,
        default=0,
        help="Max old jobs to expiry-check (0 = all).",
    )
    parser.add_argument("--expiry-delay", type=float, default=0.8)
    return parser.parse_args()


def check_old_jobs_for_expiry(
    client: NaukriSearchClient,
    seen_job_ids: set[str],
    *,
    limit: int,
    delay: float,
) -> dict[str, int]:
    candidates = list_job_ids_not_in(seen_job_ids, status="active")
    if limit > 0:
        candidates = candidates[:limit]

    print(f"\nExpiry check: {len(candidates)} old active jobs not seen in this scrape")
    updates: list[tuple[str, str]] = []
    for idx, (job_id, _title) in enumerate(candidates, start=1):
        status = client.check_job_status(job_id)
        updates.append((job_id, status))
        if idx % 25 == 0 or idx == len(candidates):
            print(f"  checked {idx}/{len(candidates)} (latest={status})")
        if idx < len(candidates):
            time.sleep(delay)
    return update_job_statuses(updates)


def main() -> None:
    args = parse_args()
    load_dotenv(ROOT / args.env_file)
    families = load_families()

    print(f"Scraping {len(families)} role families (fresher/intern, {args.location})")
    client = NaukriSearchClient(delay_seconds=1.2)
    all_jobs: list[NaukriJob] = []
    per_family_counts: dict[str, int] = {}

    try:
        for idx, family in enumerate(families, start=1):
            fam_jobs: list[NaukriJob] = []
            print(f"\n[{idx}/{len(families)}] {family['name']} ({family['id']})")
            for keyword in family["keywords"]:
                print(f"  keyword: {keyword!r}")
                jobs = client.search_jobs(
                    keyword,
                    location=args.location,
                    max_pages=args.max_pages,
                    experience=args.experience,
                    fresher_only=True,
                )
                for job in jobs:
                    job.role_family = family["id"]
                    job.role_family_name = family["name"]
                print(f"    -> {len(jobs)} jobs")
                fam_jobs.extend(jobs)

            seen: set[str] = set()
            unique_fam_jobs = []
            for job in fam_jobs:
                if job.job_id in seen:
                    continue
                seen.add(job.job_id)
                unique_fam_jobs.append(job)

            per_family_counts[family["id"]] = len(unique_fam_jobs)
            all_jobs.extend(unique_fam_jobs)
            print(f"  family total: {len(unique_fam_jobs)} unique jobs")

        deduped: list[NaukriJob] = []
        global_seen: set[str] = set()
        for job in all_jobs:
            if job.job_id in global_seen:
                continue
            global_seen.add(job.job_id)
            deduped.append(job)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        summary = {
            "meta": {
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "families_scraped": len(families),
                "keywords_total": sum(len(f["keywords"]) for f in families),
                "location": args.location,
                "experience_filter": args.experience,
                "max_pages": args.max_pages,
                "total_unique_jobs": len(deduped),
                "per_family_counts": per_family_counts,
            },
            "jobs": [j.to_dict() for j in deduped],
        }
        json_path = OUTPUT_DIR / f"naukri_18_families_{stamp}.json"
        json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSaved JSON: {json_path}")

        if not args.no_db:
            inserted, existed = save_jobs(deduped)
            print(f"Database: {inserted} new, {existed} already existed (no duplicates added)")

            if args.check_expired:
                counts = check_old_jobs_for_expiry(
                    client,
                    {j.job_id for j in deduped},
                    limit=args.expiry_limit,
                    delay=args.expiry_delay,
                )
                print("Expiry results:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none")
                print("DB status summary:")
                for status, count in status_summary():
                    print(f"  {status}: {count}")

        print("\nPer-family breakdown:")
        for fam in families:
            print(f"  {fam['id']:20} {per_family_counts.get(fam['id'], 0):3} jobs")

        top = Counter(j.role_family for j in deduped).most_common(5)
        print(f"\nDone. {len(deduped)} unique jobs across {len(families)} families.")
        print("Top families:", ", ".join(f"{k}={v}" for k, v in top))
    finally:
        client.close()


if __name__ == "__main__":
    main()
