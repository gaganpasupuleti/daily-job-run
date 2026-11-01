#!/usr/bin/env python3
"""Bounded real job collection benchmark. No database writes or publication."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
SCRAPER = REPO / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

from benchmark_isolation import check_public_collection_isolation, redact_urls  # noqa: E402
from run_all_sources import collect_jobs, dedupe_jobs, parse_sources  # noqa: E402

MAX_PAGES_CAP = 5
MAX_RESULTS_CAP = 30
MAX_FAMILIES_CAP = 3
DEFAULT_SOURCES = "naukri,indeed"
DEFAULT_MAX_PAGES = 1
DEFAULT_RESULTS_WANTED = 5
DEFAULT_FAMILY = "python-dev"


def quiet_public_logs() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s", force=True)
    for name in ("httpx", "httpcore", "urllib3", "run_all_sources", "naukri_api", "jobspy", "JobSpy"):
        logging.getLogger(name).setLevel(logging.WARNING)


def peak_memory_mb() -> float | None:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes; macOS reports bytes.
        if sys.platform == "darwin":
            return round(usage / (1024 * 1024), 2)
        return round(usage / 1024, 2)
    except Exception:
        if tracemalloc.is_tracing():
            _current, peak = tracemalloc.get_traced_memory()
            return round(peak / (1024 * 1024), 2)
        return None


def sanitize_errors(errors: list[str]) -> list[str]:
    return [redact_urls(item) for item in errors]


def build_report(
    *,
    sources: str,
    max_pages: int,
    results_wanted: int,
    family_ids: list[str],
    scrape_result,
    raw_count: int,
    unique_count: int,
    collection_seconds: float,
    dedupe_seconds: float,
    workflow_started: float,
) -> dict:
    want_naukri, jobspy_sites, want_foundit = parse_sources(sources)
    completed: list[str] = []
    if want_naukri:
        completed.append("naukri")
    if want_foundit:
        completed.append("foundit")
    completed.extend(jobspy_sites)
    per_source = dict(scrape_result.per_source)
    source_errors = sanitize_errors(list(scrape_result.errors))
    health = scrape_result.source_health or {}
    failure_counts = {
        name: len(row.get("errors") or []) if isinstance(row, dict) else 0
        for name, row in health.items()
    }
    total_runtime = round(time.monotonic() - workflow_started, 2)
    avg_per_job = round(collection_seconds / raw_count, 3) if raw_count else 0.0
    return {
        "mode": "live_collection_benchmark",
        "production_database_accessed": False,
        "publication_ran": False,
        "sources_requested": sources,
        "sources_attempted": list(scrape_result.sources_attempted),
        "sources_completed": completed,
        "jobs_collected_by_source": per_source,
        "jobs_collected_total_raw": raw_count,
        "jobs_unique_after_dedupe": unique_count,
        "source_error_messages": source_errors,
        "source_failure_counts": failure_counts,
        "source_error_count": len(source_errors),
        "collection_elapsed_seconds": round(collection_seconds, 2),
        "dedupe_elapsed_seconds": round(dedupe_seconds, 2),
        "workflow_runtime_seconds": total_runtime,
        "peak_memory_mb": peak_memory_mb(),
        "average_seconds_per_collected_job": avg_per_job,
        "families_used": family_ids,
        "max_pages": max_pages,
        "results_wanted": results_wanted,
    }


def format_public_log_line(report: dict) -> str:
    parts = [
        "BENCH_COLLECT",
        report.get("status", "ok"),
        f"sources={report['sources_requested']}",
        f"raw={report['jobs_collected_total_raw']}",
        f"unique={report['jobs_unique_after_dedupe']}",
        f"errors={report['source_error_count']}",
        f"collect_sec={report['collection_elapsed_seconds']}",
        f"dedupe_sec={report['dedupe_elapsed_seconds']}",
        f"total_sec={report['workflow_runtime_seconds']}",
    ]
    if report.get("peak_memory_mb") is not None:
        parts.append(f"peak_mb={report['peak_memory_mb']}")
    return " ".join(parts)


def write_local_report(report: dict) -> Path | None:
    directory = (os.environ.get("BENCHMARK_LOCAL_REPORT_DIR") or os.environ.get("RUNNER_TEMP") or "").strip()
    if not directory:
        return None
    path = Path(directory) / "jobready_live_collection_benchmark.json"
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path


def run_benchmark(
    *,
    sources: str,
    max_pages: int,
    results_wanted: int,
    family_ids: list[str],
    hours_old: int,
) -> dict:
    check_public_collection_isolation()
    quiet_public_logs()
    workflow_started = time.monotonic()
    tracemalloc.start()
    t0 = time.monotonic()
    result = collect_jobs(
        sources=sources,
        max_pages=max_pages,
        results_wanted=results_wanted,
        family_ids=family_ids or None,
        hours_old=hours_old,
        write_json=False,
    )
    collection_seconds = time.monotonic() - t0
    raw_count = sum(result.per_source.values())
    t1 = time.monotonic()
    unique = dedupe_jobs(result.jobs)
    dedupe_seconds = time.monotonic() - t1
    report = build_report(
        sources=sources,
        max_pages=max_pages,
        results_wanted=results_wanted,
        family_ids=family_ids,
        scrape_result=result,
        raw_count=raw_count,
        unique_count=len(unique),
        collection_seconds=collection_seconds,
        dedupe_seconds=dedupe_seconds,
        workflow_started=workflow_started,
    )
    report["status"] = "pass" if report["jobs_unique_after_dedupe"] >= 0 else "fail"
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded live job collection benchmark (no DB writes)")
    parser.add_argument("--sources", default=os.environ.get("BENCHMARK_SOURCES", DEFAULT_SOURCES))
    parser.add_argument("--max-pages", type=int, default=int(os.environ.get("BENCHMARK_MAX_PAGES", DEFAULT_MAX_PAGES)))
    parser.add_argument(
        "--results-wanted",
        type=int,
        default=int(os.environ.get("BENCHMARK_RESULTS_WANTED", DEFAULT_RESULTS_WANTED)),
    )
    parser.add_argument("--family", action="append", default=[])
    parser.add_argument("--hours-old", type=int, default=72)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    families = args.family or [DEFAULT_FAMILY]
    if args.max_pages < 1 or args.max_pages > MAX_PAGES_CAP:
        print("BENCH_COLLECT fail reason=max_pages_out_of_range")
        return 2
    if args.results_wanted < 1 or args.results_wanted > MAX_RESULTS_CAP:
        print("BENCH_COLLECT fail reason=results_wanted_out_of_range")
        return 2
    if len(families) > MAX_FAMILIES_CAP:
        print("BENCH_COLLECT fail reason=too_many_families")
        return 2
    try:
        report = run_benchmark(
            sources=args.sources,
            max_pages=args.max_pages,
            results_wanted=args.results_wanted,
            family_ids=families,
            hours_old=args.hours_old,
        )
    except ValueError:
        print("BENCH_COLLECT fail reason=isolation_guard")
        return 3
    except Exception as exc:
        print(f"BENCH_COLLECT fail reason=unexpected error={redact_urls(str(exc))[:120]}")
        return 1
    write_local_report(report)
    print(format_public_log_line(report))
    for source, count in sorted(report["jobs_collected_by_source"].items()):
        print(f"BENCH_SOURCE source={source} collected={count}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(
                "### Live collection benchmark (counts only)\n\n"
                f"- Raw collected: **{report['jobs_collected_total_raw']}**\n"
                f"- Unique after dedupe: **{report['jobs_unique_after_dedupe']}**\n"
                f"- Source errors: **{report['source_error_count']}**\n"
                f"- Collection time: **{report['collection_elapsed_seconds']}s**\n"
                f"- Workflow time: **{report['workflow_runtime_seconds']}s**\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
