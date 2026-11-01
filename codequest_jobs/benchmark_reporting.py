"""Shared reporting helpers for live collection benchmarks."""
from __future__ import annotations

from benchmark_isolation import redact_urls
from run_all_sources import parse_sources


RATE_LIMIT_MARKERS = (
    "403",
    "429",
    "406",
    "captcha",
    "blocked",
    "rate limit",
    "too many requests",
    "use a proxy",
)


def is_rate_limit_error(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def analyze_source_outcomes(sources: str, source_health: dict) -> dict:
    """Classify each requested source: ok, zero_results, partial_errors, or failed."""
    _want_naukri, jobspy_sites, want_foundit = parse_sources(sources)
    requested: list[str] = []
    if _want_naukri:
        requested.append("naukri")
    if want_foundit:
        requested.append("foundit")
    requested.extend(jobspy_sites)

    per_source: dict[str, dict] = {}
    completed: list[str] = []
    failed_sources: list[str] = []
    zero_result_sources: list[str] = []

    for name in requested:
        row = source_health.get(name) or {}
        calls = int(row.get("calls") or 0)
        call_failures = int(row.get("call_failures") or 0)
        jobs_returned = int(row.get("jobs_returned") or 0)
        succeeded = bool(row.get("succeeded"))
        failed = bool(row.get("failed"))
        errors = [redact_urls(item) for item in (row.get("errors") or [])]

        if failed:
            status = "failed"
            failed_sources.append(name)
        elif jobs_returned == 0:
            status = "zero_results"
            zero_result_sources.append(name)
        elif call_failures > 0:
            status = "partial_errors"
            completed.append(name)
        else:
            status = "ok"
            completed.append(name)

        per_source[name] = {
            "status": status,
            "searches_attempted": calls,
            "search_failures": call_failures,
            "jobs_returned": jobs_returned,
            "error_count": len(errors),
        }

    return {
        "sources_requested": requested,
        "sources_completed": completed,
        "sources_failed": failed_sources,
        "sources_zero_results": zero_result_sources,
        "source_details": per_source,
    }


def merge_scrape_health(left: dict, right: dict) -> dict:
    merged: dict = {}
    for name in set(left) | set(right):
        a = left.get(name) or {}
        b = right.get(name) or {}
        errors = list(dict.fromkeys((a.get("errors") or []) + (b.get("errors") or [])))
        calls = int(a.get("calls") or 0) + int(b.get("calls") or 0)
        call_failures = int(a.get("call_failures") or 0) + int(b.get("call_failures") or 0)
        jobs_returned = int(a.get("jobs_returned") or 0) + int(b.get("jobs_returned") or 0)
        merged[name] = {
            "attempted": True,
            "calls": calls,
            "call_failures": call_failures,
            "succeeded": calls > 0 and call_failures < calls,
            "failed": calls == 0 or call_failures == calls,
            "jobs_returned": jobs_returned,
            "errors": errors,
        }
    return merged
