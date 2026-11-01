#!/usr/bin/env python3
"""Full-scale collection benchmark: all families, five sources, no database writes."""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
SCRAPER = REPO / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

from benchmark_isolation import check_public_collection_isolation, redact_urls  # noqa: E402
from benchmark_reporting import is_rate_limit_error, merge_scrape_health  # noqa: E402
from live_collection_benchmark import (  # noqa: E402
    build_report,
    format_public_log_line,
    quiet_public_logs,
    sanitize_errors,
    write_local_report,
)
from role_keywords import load_families  # noqa: E402
from run_all_sources import collect_jobs, dedupe_jobs  # noqa: E402

FULL_SOURCES = "naukri,indeed,linkedin,google,foundit"
DEFAULT_MAX_PAGES = 1
DEFAULT_RESULTS_WANTED = 5
DEFAULT_HOURS_OLD = 72
FAMILY_PAUSE_SECONDS = 2.0
RATE_LIMIT_BACKOFF_SECONDS = 90.0


def local_report_dir() -> Path:
    configured = (os.environ.get("BENCHMARK_LOCAL_REPORT_DIR") or "").strip()
    if configured:
        path = Path(configured)
    else:
        path = REPO / "benchmark_local_reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def should_stop_for_rate_limit(errors: list[str]) -> tuple[bool, str]:
    for message in errors:
        if is_rate_limit_error(message):
            return True, redact_urls(message)[:160]
    return False, ""


def recompute_health(jobs, source_label: list[str], merged_health: dict) -> dict:
    returned = Counter((job.source or "").lower() for job in jobs)
    health: dict = {}
    for name in source_label:
        row = merged_health.get(name) or {}
        calls = int(row.get("calls") or 0)
        failures = int(row.get("call_failures") or 0)
        health[name] = {
            "attempted": True,
            "calls": calls,
            "call_failures": failures,
            "succeeded": calls > 0 and failures < calls,
            "failed": calls == 0 or failures == calls,
            "jobs_returned": int(returned.get(name, 0)),
            "errors": list(dict.fromkeys(row.get("errors") or [])),
        }
    return health


def run_full_scale(
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    results_wanted: int = DEFAULT_RESULTS_WANTED,
    hours_old: int = DEFAULT_HOURS_OLD,
) -> dict:
    check_public_collection_isolation()
    quiet_public_logs()
    families = load_families()
    family_ids = [family["id"] for family in families]
    workflow_started = time.monotonic()
    all_jobs: list = []
    merged_health: dict = {}
    all_errors: list[str] = []
    sources_attempted: list[str] = []
    stopped_early = False
    stop_reason = ""
    collection_seconds = 0.0
    families_completed = 0

    for index, family in enumerate(families):
        t0 = time.monotonic()
        partial = collect_jobs(
            sources=FULL_SOURCES,
            max_pages=max_pages,
            results_wanted=results_wanted,
            hours_old=hours_old,
            family_ids=[family["id"]],
            write_json=False,
        )
        collection_seconds += time.monotonic() - t0
        families_completed = index + 1
        if not sources_attempted:
            sources_attempted = list(partial.sources_attempted)
        merged_health = merge_scrape_health(merged_health, partial.source_health or {})
        all_errors.extend(partial.errors)
        all_jobs.extend(partial.jobs)

        hit, reason = should_stop_for_rate_limit(partial.errors)
        if not hit:
            for row in (partial.source_health or {}).values():
                hit, reason = should_stop_for_rate_limit(list(row.get("errors") or []))
                if hit:
                    break
        if hit:
            stopped_early = True
            stop_reason = reason or "rate_limit"
            break

        if index + 1 < len(families):
            time.sleep(FAMILY_PAUSE_SECONDS)

    t_dedupe = time.monotonic()
    unique_jobs = dedupe_jobs(all_jobs)
    dedupe_seconds = time.monotonic() - t_dedupe
    per_source = Counter((job.source or "").lower() for job in all_jobs)
    raw_count = sum(per_source.values())
    per_source_unique = Counter((job.source or "").lower() for job in unique_jobs)
    health = recompute_health(unique_jobs, sources_attempted, merged_health)

    from types import SimpleNamespace

    scrape_result = SimpleNamespace(
        source_health=health,
        sources_attempted=sources_attempted,
        per_source=dict(per_source_unique),
        errors=all_errors,
    )

    report = build_report(
        sources=FULL_SOURCES,
        max_pages=max_pages,
        results_wanted=results_wanted,
        family_ids=family_ids[:families_completed],
        scrape_result=scrape_result,
        raw_count=raw_count,
        unique_count=len(unique_jobs),
        collection_seconds=collection_seconds,
        dedupe_seconds=dedupe_seconds,
        workflow_started=workflow_started,
        stopped_early=stopped_early,
        stop_reason=stop_reason,
    )
    report["status"] = "pass"
    report["families_planned"] = len(family_ids)
    report["families_completed"] = families_completed
    report["hours_old"] = hours_old

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    detail_path = local_report_dir() / f"full_scale_jobs_{stamp}.json"
    detail_path.write_text(
        json.dumps(
            {
                "meta": {
                    "sources": FULL_SOURCES,
                    "max_pages": max_pages,
                    "results_wanted": results_wanted,
                    "hours_old": hours_old,
                    "families_completed": families_completed,
                    "stopped_early": stopped_early,
                },
                "jobs": [job.to_dict() for job in unique_jobs],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    report["local_jobs_path"] = str(detail_path)
    summary_path = local_report_dir() / f"full_scale_summary_{stamp}.json"
    public_report = dict(report)
    public_report["source_error_messages"] = sanitize_errors(report["source_error_messages"])[:50]
    summary_path.write_text(json.dumps(public_report, indent=2) + "\n", encoding="utf-8")
    report["local_summary_path"] = str(summary_path)
    return report


def print_sanitized_summary(report: dict) -> None:
    print(format_public_log_line(report))
    print(
        "BENCH_FULL families_completed="
        f"{report.get('families_completed')}/{report.get('families_planned')} "
        f"stopped_early={report.get('stopped_early')} "
        f"stop_reason={report.get('stop_reason') or 'none'}"
    )
    for name in report.get("sources_requested") or []:
        detail = (report.get("source_details") or {}).get(name) or {}
        print(
            f"BENCH_SOURCE source={name} status={detail.get('status')} "
            f"searches={detail.get('searches_attempted')} failures={detail.get('search_failures')} "
            f"jobs={detail.get('jobs_returned')}"
        )
    runtime = report.get("github_actions_runtime") or {}
    print(
        "BENCH_GITHUB daily_sec="
        f"{runtime.get('estimated_daily_runtime_seconds')} "
        f"monthly_min={runtime.get('estimated_monthly_runtime_minutes')} "
        f"public_usd_extra={report.get('github_actions_cost_assumption_public_usd')} "
        f"private_usd_extra={report.get('github_actions_cost_assumption_private_usd')}"
    )


def main() -> int:
    try:
        report = run_full_scale()
    except ValueError:
        print("BENCH_FULL fail reason=isolation_guard")
        return 3
    except Exception as exc:
        print(f"BENCH_FULL fail reason=unexpected error={redact_urls(str(exc))[:120]}")
        return 1
    os.environ.setdefault("BENCHMARK_LOCAL_REPORT_DIR", str(local_report_dir()))
    write_local_report(report)
    print_sanitized_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
