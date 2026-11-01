#!/usr/bin/env python3
"""Collect Naukri, Indeed, LinkedIn, Google, and Foundit jobs.

The function returns normalized jobs. It does not write CodeQuest or the
legacy naukri_scraper database. JSON artifacts are optional.

Legacy database writes require explicit flags and are deprecated.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from foundit_client import scrape_foundit_safe
from jobspy_client import DEFAULT_SOURCES as JOBSPY_DEFAULTS
from jobspy_client import scrape_jobs_safe
from naukri_api import NaukriSearchClient
from normalize import UnifiedJob
from normalize import from_jobspy_row, from_naukri
from role_keywords import load_families

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("run_all_sources")


@dataclass
class ScrapeResult:
    jobs: list[UnifiedJob]
    per_source: dict[str, int]
    per_family: dict[str, int]
    errors: list[str]
    sources_attempted: list[str]
    started_at: str
    finished_at: str
    json_path: str | None = None
    families: list[dict] = field(default_factory=list)
    source_health: dict = field(default_factory=dict)


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
        key = ((job.source or "").lower(), (job.job_url or "").strip())
        if not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(job)
    return out


def collect_jobs(
    *,
    sources: str = "naukri,indeed,linkedin,google,foundit",
    location: str = "india",
    max_pages: int = 2,
    experience: int = 0,
    results_wanted: int = 12,
    hours_old: int = 72,
    family_ids: list[str] | None = None,
    write_json: bool = False,
) -> ScrapeResult:
    """Scrape configured sources and return normalized jobs. No database writes."""
    started = datetime.now(timezone.utc)
    want_naukri, jobspy_sites, want_foundit = parse_sources(sources)
    families = load_families()
    if family_ids:
        wanted = set(family_ids)
        families = [fam for fam in families if fam["id"] in wanted]
        if not families:
            raise SystemExit(f"No families matched {sorted(wanted)}")

    source_label: list[str] = []
    if want_naukri:
        source_label.append("naukri")
    if want_foundit:
        source_label.append("foundit")
    source_label.extend(jobspy_sites)

    all_jobs: list[UnifiedJob] = []
    per_family_counts: dict[str, int] = {}
    per_source_counts: Counter[str] = Counter()
    errors: list[str] = []
    health = {
        name: {"attempted": True, "calls": 0, "call_failures": 0, "errors": []}
        for name in source_label
    }
    naukri_client: NaukriSearchClient | None = None
    if want_naukri:
        naukri_client = NaukriSearchClient(delay_seconds=1.2)

    try:
        for family in families:
            fam_jobs: list[UnifiedJob] = []
            for keyword in family["keywords"]:
                if naukri_client is not None:
                    health["naukri"]["calls"] += 1
                    try:
                        n_jobs = naukri_client.search_jobs(
                            keyword,
                            location=location,
                            max_pages=max_pages,
                            experience=experience,
                            fresher_only=True,
                        )
                        for nj in n_jobs:
                            nj.role_family = family["id"]
                            nj.role_family_name = family["name"]
                            uj = from_naukri(nj)
                            uj.ingest_profile = family["id"]
                            fam_jobs.append(uj)
                    except Exception as exc:
                        logger.exception("Naukri failed for %r", keyword)
                        health["naukri"]["call_failures"] += 1
                        message = f"naukri:{keyword}: {exc}"
                        health["naukri"]["errors"].append(message)
                        errors.append(message)

                if jobspy_sites:
                    rows, _breakdown, site_errors = scrape_jobs_safe(
                        search_term=keyword,
                        results_wanted=results_wanted,
                        hours_old=hours_old,
                        sources=jobspy_sites,
                    )
                    errors.extend(site_errors)
                    import_failure = any("python-jobspy" in err for err in site_errors)
                    for site in jobspy_sites:
                        row = health[site]
                        row["calls"] += 1
                        site_errors_for = [err for err in site_errors if err.lower().startswith(f"{site}:")]
                        if import_failure or site_errors_for:
                            row["call_failures"] += 1
                            row["errors"].extend(site_errors_for or site_errors)
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

                if want_foundit:
                    f_jobs, f_errors = scrape_foundit_safe(
                        search_term=keyword,
                        results_wanted=max(4, results_wanted // 2),
                        hours_old=hours_old,
                    )
                    errors.extend(f_errors)
                    health["foundit"]["calls"] += 1
                    if f_errors and not f_jobs:
                        health["foundit"]["call_failures"] += 1
                        health["foundit"]["errors"].extend(f_errors)
                    for uj in f_jobs:
                        uj.role_family = family["id"]
                        uj.role_family_name = family["name"]
                        uj.ingest_profile = family["id"]
                        fam_jobs.append(uj)

            unique_fam = dedupe_jobs(fam_jobs)
            per_family_counts[family["id"]] = len(unique_fam)
            for job in unique_fam:
                per_source_counts[job.source] += 1
            all_jobs.extend(unique_fam)
    finally:
        if naukri_client is not None:
            naukri_client.close()

    deduped = dedupe_jobs(all_jobs)
    returned: Counter[str] = Counter(job.source for job in deduped)
    source_health = {}
    for name, row in health.items():
        calls = int(row["calls"])
        failures = int(row["call_failures"])
        source_health[name] = {
            "attempted": True,
            "calls": calls,
            "call_failures": failures,
            "succeeded": calls > 0 and failures < calls,
            "failed": calls == 0 or failures == calls,
            "jobs_returned": int(returned.get(name, 0)),
            "errors": list(dict.fromkeys(row["errors"])),
        }
    finished = datetime.now(timezone.utc)
    json_path = None
    if write_json:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = finished.strftime("%Y%m%d_%H%M%S")
        path = OUTPUT_DIR / f"all_sources_{stamp}.json"
        payload = {
            "meta": {
                "scraped_at": finished.isoformat(),
                "sources": source_label,
                "total_unique_jobs": len(deduped),
                "per_family_counts": per_family_counts,
                "per_source_counts": dict(per_source_counts),
                "errors": errors,
            },
            "jobs": [job.to_dict() for job in deduped],
        }
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        json_path = str(path)

    return ScrapeResult(
        jobs=deduped,
        per_source=dict(per_source_counts),
        per_family=per_family_counts,
        errors=errors,
        sources_attempted=source_label,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        json_path=json_path,
        families=families,
        source_health=source_health,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape job sources. Default writes a JSON artifact only."
    )
    parser.add_argument("--sources", default="naukri,indeed,linkedin,google,foundit")
    parser.add_argument("--location", default="india")
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--experience", type=int, default=0)
    parser.add_argument("--results-wanted", type=int, default=12)
    parser.add_argument("--hours-old", type=int, default=72)
    parser.add_argument("--family", action="append", default=[])
    parser.add_argument("--env-file", default=".env")
    parser.add_argument(
        "--legacy-write-local",
        action="store_true",
        help="DEPRECATED. Write the legacy naukri_scraper database.",
    )
    parser.add_argument(
        "--legacy-write-codequest",
        action="store_true",
        help="DEPRECATED. Write CodeQuest scraped_jobs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(ROOT / args.env_file)
    if args.legacy_write_local or args.legacy_write_codequest:
        print("DEPRECATED: legacy database writes are not part of the JobReady daily run.")
    result = collect_jobs(
        sources=args.sources,
        location=args.location,
        max_pages=args.max_pages,
        experience=args.experience,
        results_wanted=args.results_wanted,
        hours_old=args.hours_old,
        family_ids=args.family,
        write_json=True,
    )
    print(f"Saved JSON: {result.json_path}")
    print(f"Unique jobs: {len(result.jobs)}")
    if args.legacy_write_local:
        from db import save_jobs

        inserted, existed = save_jobs(result.jobs)
        print(f"LEGACY local DB: {inserted} new, {existed} already existed")
    if args.legacy_write_codequest:
        from codequest_sync import save_to_scraped_jobs

        saved, skipped = save_to_scraped_jobs(result.jobs)
        print(f"LEGACY CodeQuest: {saved} new, {skipped} skipped")
    if result.errors:
        print(f"Source errors: {len(result.errors)}")


if __name__ == "__main__":
    main()
