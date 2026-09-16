#!/usr/bin/env python3
"""
CodeQuest sister-repo master pipeline.

Ask Cursor: "run jobs + quizzes" → this script.

Steps (default all):
  scrape  — Naukri + Indeed + LinkedIn → scraped_jobs (+ local naukri_scraper)
  links   — recheck URLs / Naukri detail API → link_status
  enrich  — ROLE_* family tagging → job_enrichments
  quizzes — quiz packs + workbench coding questions → quiz_packs
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRAPER_DIR = ROOT.parent / "naukri_jobs_scraper"
DEFAULT_ENV = SCRAPER_DIR / ".env"

# Ensure local imports work when run as a script
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sister-repo jobs + quizzes pipeline for CodeQuest.")
    p.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV),
        help="Dotenv with DATABASE_URL + CODEQUEST_DATABASE_URL",
    )
    p.add_argument(
        "--steps",
        default="scrape,links,enrich,quizzes",
        help="Comma-separated: scrape,links,enrich,quizzes",
    )
    p.add_argument("--family", default="", help="Limit to sister family id, e.g. python-dev")
    p.add_argument("--sources", default="naukri,indeed,linkedin,google,foundit")
    p.add_argument("--max-pages", type=int, default=1, help="Naukri pages (scrape)")
    p.add_argument("--results-wanted", type=int, default=8, help="JobSpy results (scrape)")
    p.add_argument("--hours-old", type=int, default=72)
    p.add_argument("--link-limit", type=int, default=80, help="Max URLs to recheck")
    p.add_argument("--purge-dead", action="store_true", help="Delete non-active jobs after link check")
    p.add_argument("--enrich-days", type=int, default=14, help="Enrich jobs created in last N days (0=all)")
    p.add_argument("--enrich-limit", type=int, default=0, help="Max jobs to enrich (0=all matched)")
    p.add_argument("--no-codequest-scrape", action="store_true", help="Pass --no-codequest to scraper")
    return p.parse_args()


def step_scrape(args: argparse.Namespace) -> dict:
    cmd = [
        sys.executable,
        "-u",
        str(SCRAPER_DIR / "run_all_sources.py"),
        "--env-file",
        args.env_file,
        "--sources",
        args.sources,
        "--max-pages",
        str(args.max_pages),
        "--results-wanted",
        str(args.results_wanted),
        "--hours-old",
        str(args.hours_old),
    ]
    if args.family:
        cmd.extend(["--family", args.family])
    if args.no_codequest_scrape:
        cmd.append("--no-codequest")
    print("\n=== STEP scrape ===")
    print(" ", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(SCRAPER_DIR))
    if proc.returncode != 0:
        raise SystemExit(f"scrape failed with exit {proc.returncode}")
    return {"ok": True}


def step_links(args: argparse.Namespace) -> dict:
    from db import load_env
    from link_checker import cleanup_job_links, purge_non_active

    load_env(args.env_file)
    print("\n=== STEP links ===")
    summary = cleanup_job_links(limit=args.link_limit, delay=0.3)
    print("Link check:", summary)
    if args.purge_dead:
        purged = purge_non_active()
        print("Purged:", purged)
        summary["purge"] = purged
    return summary


def step_enrich(args: argparse.Namespace) -> dict:
    from db import load_env
    from enrichment import run_enrichment

    load_env(args.env_file)
    print("\n=== STEP enrich ===")
    days = None if args.enrich_days == 0 else args.enrich_days
    result = run_enrichment(
        days=days,
        limit=args.enrich_limit,
        family=args.family or None,
    )
    print(
        f"Classified {result['jobs_classified']} | "
        f"+{result['inserted']} new / {result['updated']} updated | "
        f"jd_clean={result.get('with_jd_clean', 0)}"
    )
    print("Practice items:", result.get("practice"))
    print("Role breakdown:", result.get("role_breakdown"))
    print("CSV:", result.get("csv"))
    return result


def step_quizzes(args: argparse.Namespace) -> dict:
    from db import load_env
    from quiz_packs import run_quiz_packs

    load_env(args.env_file)
    print("\n=== STEP quizzes ===")
    result = run_quiz_packs(family=args.family or None)
    print(f"Packs upserted: {result['packs_upserted']}")
    for pack in result.get("packs", [])[:12]:
        print(
            f"  {pack['quiz_pack_id']}: {pack['questions']} questions, "
            f"{pack['linked_jobs']} jobs linked"
        )
    return result


def main() -> None:
    args = parse_args()
    steps = [s.strip().lower() for s in args.steps.split(",") if s.strip()]
    allowed = {"scrape", "links", "enrich", "quizzes"}
    unknown = set(steps) - allowed
    if unknown:
        raise SystemExit(f"Unknown steps: {sorted(unknown)}. Allowed: {sorted(allowed)}")

    print("CodeQuest sister pipeline")
    print(f"  env: {args.env_file}")
    print(f"  steps: {', '.join(steps)}")
    if args.family:
        print(f"  family: {args.family}")

    results = {}
    if "scrape" in steps:
        results["scrape"] = step_scrape(args)
    if "links" in steps:
        results["links"] = step_links(args)
    if "enrich" in steps:
        results["enrich"] = step_enrich(args)
    if "quizzes" in steps:
        results["quizzes"] = step_quizzes(args)

    print("\n=== DONE ===")
    print("Steps completed:", ", ".join(results.keys()))


if __name__ == "__main__":
    main()
