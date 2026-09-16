#!/usr/bin/env python3
"""One-trigger scrape: Naukri + Indeed + LinkedIn → local DB + CodeQuest scraped_jobs."""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from codequest_sync import (
    create_scrape_run,
    get_codequest_database_url,
    save_to_scraped_jobs,
    update_scrape_run,
    codequest_source_summary,
)
from db import save_jobs, source_summary
from foundit_client import scrape_foundit_safe
from jobspy_client import DEFAULT_SOURCES as JOBSPY_DEFAULTS
from jobspy_client import scrape_jobs_safe
from naukri_api import NaukriSearchClient
from normalize import UnifiedJob, from_jobspy_row, from_naukri
from role_keywords import load_families

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("run_all_sources")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape Naukri + JobSpy (Indeed/LinkedIn) and dual-write to both DBs."
    )
    parser.add_argument(
        "--sources",
        default="naukri,indeed,linkedin,google,foundit",
        help="Comma-separated: naukri,indeed,linkedin,google,foundit",
    )
    parser.add_argument("--location", default="india", help="Naukri location slug")
    parser.add_argument("--max-pages", type=int, default=2, help="Naukri pages per keyword")
    parser.add_argument("--experience", type=int, default=0)
    parser.add_argument("--results-wanted", type=int, default=12, help="JobSpy results per site/keyword")
    parser.add_argument("--hours-old", type=int, default=72, help="JobSpy recency window")
    parser.add_argument(
        "--family",
        action="append",
        default=[],
        help="Limit to family id(s), e.g. --family python-dev (repeatable)",
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--no-local-db", action="store_true")
    parser.add_argument("--no-codequest", action="store_true")
    return parser.parse_args()


def parse_sources(raw: str) -> tuple[bool, list[str], bool]:
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    want_naukri = "naukri" in parts
    want_foundit = "foundit" in parts
    jobspy_sites = [p for p in parts if p in ("indeed", "linkedin", "google")]
    if not want_naukri and not jobspy_sites and not want_foundit:
        want_naukri = True
        jobspy_sites = list(JOBSPY_DEFAULTS) + ["google"]
    return want_naukri, jobspy_sites, want_foundit


def dedupe_jobs(jobs: list[UnifiedJob]) -> list[UnifiedJob]:
    seen: set[tuple[str, str]] = set()
    out: list[UnifiedJob] = []
    for job in jobs:
        key = (job.source, job.job_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


def main() -> None:
    args = parse_args()
    load_dotenv(ROOT / args.env_file)

    want_naukri, jobspy_sites, want_foundit = parse_sources(args.sources)
    families = load_families()
    if args.family:
        wanted = set(args.family)
        families = [f for f in families if f["id"] in wanted]
        if not families:
            raise SystemExit(f"No families matched --family {sorted(wanted)}")

    if not args.no_codequest:
        # Fail fast if CodeQuest URL missing
        get_codequest_database_url()

    source_label = []
    if want_naukri:
        source_label.append("naukri")
    if want_foundit:
        source_label.append("foundit")
    source_label.extend(jobspy_sites)
    print(
        f"Scraping {len(families)} families | sources={','.join(source_label)} "
        f"| naukri_pages={args.max_pages} | jobspy_results={args.results_wanted}"
    )

    all_jobs: list[UnifiedJob] = []
    per_family_counts: dict[str, int] = {}
    per_source_counts: Counter[str] = Counter()
    jobspy_errors: list[str] = []
    run_started = datetime.now(timezone.utc).replace(tzinfo=None)
    cq_run_id: int | None = None

    if not args.no_codequest:
        cq_run_id = create_scrape_run(
            search_term=f"all_families:{len(families)}",
            location="India",
            sources=source_label,
            results_wanted=args.results_wanted,
            hours_old=args.hours_old,
            run_type="manual",
            profile="multi_source_families",
            triggered_by="run_all_sources",
        )
        print(f"CodeQuest scrape run id: {cq_run_id}")

    naukri_client: NaukriSearchClient | None = None
    if want_naukri:
        naukri_client = NaukriSearchClient(delay_seconds=1.2)

    try:
        for idx, family in enumerate(families, start=1):
            fam_jobs: list[UnifiedJob] = []
            print(f"\n[{idx}/{len(families)}] {family['name']} ({family['id']})")

            for keyword in family["keywords"]:
                print(f"  keyword: {keyword!r}")

                if naukri_client is not None:
                    try:
                        n_jobs = naukri_client.search_jobs(
                            keyword,
                            location=args.location,
                            max_pages=args.max_pages,
                            experience=args.experience,
                            fresher_only=True,
                        )
                        for nj in n_jobs:
                            nj.role_family = family["id"]
                            nj.role_family_name = family["name"]
                            uj = from_naukri(nj)
                            uj.ingest_profile = family["id"]
                            fam_jobs.append(uj)
                        print(f"    naukri -> {len(n_jobs)} jobs")
                    except Exception as exc:
                        logger.exception("Naukri failed for %r", keyword)
                        jobspy_errors.append(f"naukri:{keyword}: {exc}")
                        print(f"    naukri -> ERROR: {exc}")

                if jobspy_sites:
                    rows, breakdown, errors = scrape_jobs_safe(
                        search_term=keyword,
                        results_wanted=args.results_wanted,
                        hours_old=args.hours_old,
                        sources=jobspy_sites,
                    )
                    jobspy_errors.extend(errors)
                    added = 0
                    for row in rows:
                        uj = from_jobspy_row(
                            row,
                            search_keyword=keyword,
                            role_family=family["id"],
                            role_family_name=family["name"],
                        )
                        if uj is None:
                            continue
                        uj.ingest_profile = family["id"]
                        fam_jobs.append(uj)
                        added += 1
                    br = ", ".join(f"{k}={v}" for k, v in sorted(breakdown.items()))
                    print(f"    jobspy -> {added} jobs ({br or 'none'})")
                    if errors:
                        print(f"    jobspy errors: {'; '.join(errors)}")

                if want_foundit:
                    f_jobs, f_errors = scrape_foundit_safe(
                        search_term=keyword,
                        results_wanted=max(4, args.results_wanted // 2),
                        hours_old=args.hours_old,
                    )
                    jobspy_errors.extend(f_errors)
                    for uj in f_jobs:
                        uj.role_family = family["id"]
                        uj.role_family_name = family["name"]
                        uj.ingest_profile = family["id"]
                        fam_jobs.append(uj)
                    print(f"    foundit -> {len(f_jobs)} jobs")

            unique_fam = dedupe_jobs(fam_jobs)
            per_family_counts[family["id"]] = len(unique_fam)
            for j in unique_fam:
                per_source_counts[j.source] += 1
            all_jobs.extend(unique_fam)
            print(f"  family total: {len(unique_fam)} unique jobs")

        deduped = dedupe_jobs(all_jobs)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        summary = {
            "meta": {
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "families_scraped": len(families),
                "sources": source_label,
                "location": args.location,
                "max_pages": args.max_pages,
                "results_wanted": args.results_wanted,
                "hours_old": args.hours_old,
                "total_unique_jobs": len(deduped),
                "per_family_counts": per_family_counts,
                "per_source_counts": dict(per_source_counts),
                "errors": jobspy_errors,
            },
            "jobs": [j.to_dict() for j in deduped],
        }
        json_path = OUTPUT_DIR / f"all_sources_{stamp}.json"
        json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nSaved JSON: {json_path}")

        local_inserted = local_existed = 0
        if not args.no_local_db:
            local_inserted, local_existed = save_jobs(deduped)
            print(f"Local DB: {local_inserted} new, {local_existed} already existed")
            print("Local source summary:")
            for src, count in source_summary():
                print(f"  {src}: {count}")

        cq_saved = cq_skipped = 0
        if not args.no_codequest:
            cq_saved, cq_skipped = save_to_scraped_jobs(deduped)
            print(f"CodeQuest scraped_jobs: {cq_saved} new, {cq_skipped} skipped (duplicates)")
            if cq_run_id is not None:
                status = "success"
                if jobspy_errors and deduped:
                    status = "partial_success"
                elif not deduped and jobspy_errors:
                    status = "failed"
                update_scrape_run(
                    cq_run_id,
                    total_found=len(deduped),
                    saved_count=cq_saved,
                    skipped_duplicates=cq_skipped,
                    error_count=len(jobspy_errors),
                    errors=jobspy_errors[:50],
                    source_breakdown=dict(per_source_counts),
                    status=status,
                    started_at=run_started,
                )
            print("CodeQuest source summary:")
            for src, count in codequest_source_summary():
                print(f"  {src}: {count}")

        print("\nPer-family breakdown:")
        for fam in families:
            print(f"  {fam['id']:20} {per_family_counts.get(fam['id'], 0):3} jobs")

        print("\nPer-source (this run):")
        for src, count in per_source_counts.most_common():
            print(f"  {src:12} {count:3}")

        print(
            f"\nDone. {len(deduped)} unique jobs | "
            f"local +{local_inserted} | codequest +{cq_saved}"
        )
    finally:
        if naukri_client is not None:
            naukri_client.close()


if __name__ == "__main__":
    main()
