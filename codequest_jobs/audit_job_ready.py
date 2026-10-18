#!/usr/bin/env python3
"""Availability audit for catalog links and acquisition JSON artifacts.

Each row is checked with the canonical validator in jobready_validator.py.
Catalog mode reads validated_jobs; JSON mode reads run_all_sources.py output only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg2

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
SCRAPER = REPO / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

from db import load_env  # noqa: E402
from jobready_validator import Probe, default_probes, probe_for, validate_candidates  # noqa: E402
from link_policy import (  # noqa: E402
    CONFIRMED_ACTIVE,
    CONFIRMED_EXPIRED,
    INCONCLUSIVE,
    UNSUPPORTED,
    UNSUPPORTED_SOURCE,
    normalize_source,
    source_label,
)
from normalize import source_job_id  # noqa: E402

ENV = SCRAPER / ".env"
JOBREADY_SYNC_RETIRED = "JOBREADY_SYNC_RETIRED"
JOBREADY_SYNC_RETIRED_HELP = (
    "audit_job_ready.py is read-only and no longer writes the JobReady catalog or application database.\n"
    "Catalog lifecycle changes come from codequest_jobs/run_jobready_daily.py.\n"
    "Application membership reconciliation is codequest_jobs/sync_job_ready_ui.py."
)
EXIT_RETIRED = 2
SOURCE_ORDER = {"linkedin": 0, "indeed": 1, "foundit": 2, "naukri": 3}
CONCLUSIVE = (CONFIRMED_ACTIVE, CONFIRMED_EXPIRED)
ROW_COLUMNS = ("job_id", "source", "job_url", "apply_url", "link_status")
ACQUISITION_META_KEYS = ("title", "company", "role_family", "role_family_name", "search_keyword")
UNKNOWN_LABEL = "unknown"
DEFAULT_MAX_WORKERS = 4


def fetch_job_ready_rows() -> list[dict[str, Any]]:
    """Read catalog rows with their stored link_status. The session is read-only."""
    dest = (os.getenv("JOB_READY_DATABASE_URL") or "").strip()
    if not dest:
        raise SystemExit("APPLY_REFUSED catalog target must be configured")
    try:
        conn = psycopg2.connect(dest, connect_timeout=20)
    except Exception:
        raise SystemExit("UI_SYNC_FAILED") from None
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id, source, job_url, apply_url, link_status
                FROM validated_jobs
                WHERE job_id IS NOT NULL
                  AND COALESCE(job_url, '') <> ''
                """
            )
            rows = [dict(zip(ROW_COLUMNS, raw)) for raw in cur.fetchall()]
    finally:
        conn.close()
    rows.sort(key=lambda row: (SOURCE_ORDER.get(normalize_source(row["source"]), 9), row["job_id"]))
    return rows


def _stable_label(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return UNKNOWN_LABEL


def load_acquisition_jobs(path: Path) -> list[dict[str, Any]]:
    """Parse an acquisition artifact. Raises SystemExit on malformed structure."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"INPUT_JSON_UNREADABLE {path}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("INPUT_JSON_INVALID malformed JSON") from exc
    if not isinstance(payload, dict):
        raise SystemExit("INPUT_JSON_INVALID top-level value must be an object")
    jobs = payload.get("jobs")
    if not isinstance(jobs, list):
        raise SystemExit("INPUT_JSON_INVALID jobs must be a list")
    parsed: list[dict[str, Any]] = []
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise SystemExit(f"INPUT_JSON_INVALID jobs[{index}] must be an object")
        source = job.get("source")
        job_url = job.get("job_url")
        if not isinstance(source, str) or not source.strip():
            raise SystemExit(f"INPUT_JSON_INVALID jobs[{index}].source is required")
        if not isinstance(job_url, str) or not job_url.strip():
            raise SystemExit(f"INPUT_JSON_INVALID jobs[{index}].job_url is required")
        source_norm = normalize_source(source)
        url = job_url.strip()
        identity = source_job_id(source_norm, url)
        row = {
            "job_id": identity,
            "source": source_norm,
            "job_url": url,
            "apply_url": job.get("apply_url"),
            "link_status": None,
        }
        meta = {key: job.get(key) for key in ACQUISITION_META_KEYS}
        parsed.append({"row": row, "meta": meta})
    return parsed


def audit_row(row: dict[str, Any], probes: dict[str, Probe] | None = None) -> dict[str, Any]:
    """Return persisted status and canonical observation for one catalog-shaped row."""
    result = {
        "job_id": row["job_id"],
        "source": source_label(row.get("source")),
        "persisted_status": row.get("link_status"),
    }
    if probe_for(row.get("source"), probes) is None:
        return {**result, "observation": UNSUPPORTED, "reason": UNSUPPORTED_SOURCE, "provider": None}
    checked = validate_candidates([row], probes=probes)[0]
    observation = checked.observation if checked.observation in CONCLUSIVE else INCONCLUSIVE
    return {**result, "observation": observation, "reason": checked.reason, "provider": checked.provider}


def audit_acquisition_row(entry: dict[str, Any], probes: dict[str, Probe] | None = None) -> dict[str, Any]:
    """Audit one acquisition JSON row; persisted_status is always null."""
    base = audit_row(entry["row"], probes=probes)
    meta = entry["meta"]
    return {
        "job_id": base["job_id"],
        "source": base["source"],
        "title": meta.get("title"),
        "company": meta.get("company"),
        "role_family": meta.get("role_family"),
        "role_family_name": meta.get("role_family_name"),
        "search_keyword": meta.get("search_keyword"),
        "persisted_status": None,
        "observation": base["observation"],
        "reason": base["reason"],
        "provider": base["provider"],
    }


def _count_labels(items: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in items:
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def _observation_by_group(results: list[dict[str, Any]], label_fn: Callable[[dict[str, Any]], str]) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    for result in results:
        key = label_fn(result)
        bucket = grouped.setdefault(key, {CONFIRMED_ACTIVE: 0, CONFIRMED_EXPIRED: 0, INCONCLUSIVE: 0, UNSUPPORTED: 0})
        bucket[result["observation"]] += 1
    return {key: dict(sorted(value.items())) for key, value in sorted(grouped.items())}


def _reasons_by_source(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    grouped: dict[str, dict[str, int]] = {}
    for result in results:
        if result["observation"] == UNSUPPORTED:
            continue
        source = result["source"]
        reason = result.get("reason") or ""
        bucket = grouped.setdefault(source, {})
        bucket[reason] = bucket.get(reason, 0) + 1
    return {key: dict(sorted(value.items())) for key, value in sorted(grouped.items())}


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Count stored lifecycle state and audit observations separately."""
    summary: dict[str, Any] = {
        "checked": 0,
        CONFIRMED_ACTIVE: 0,
        CONFIRMED_EXPIRED: 0,
        INCONCLUSIVE: 0,
        UNSUPPORTED: 0,
        "unsupported_by_source": {},
        "persisted_active": 0,
        "persisted_expired": 0,
        "persisted_other": 0,
    }
    reasons: dict[str, int] = {}
    crosstab: dict[str, dict[str, int]] = {}
    unsupported_by_source: dict[str, int] = {}
    for result in results:
        observation = result["observation"]
        summary[observation] += 1
        if observation == UNSUPPORTED:
            unsupported_by_source[result["source"]] = unsupported_by_source.get(result["source"], 0) + 1
        else:
            summary["checked"] += 1
        persisted = result["persisted_status"]
        bucket = f"persisted_{persisted}" if persisted in ("active", "expired") else "persisted_other"
        summary[bucket] += 1
        label = f"{observation}:{result['reason']}"
        reasons[label] = reasons.get(label, 0) + 1
        row = crosstab.setdefault(str(persisted), {})
        row[observation] = row.get(observation, 0) + 1
    summary["unsupported_by_source"] = dict(sorted(unsupported_by_source.items()))
    summary["validation_reasons"] = dict(sorted(reasons.items()))
    summary["persisted_by_observation"] = {key: dict(sorted(value.items())) for key, value in sorted(crosstab.items())}
    return summary


def _by_source_observation(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    return _observation_by_group(results, lambda item: item["source"])


def _acquisition_queued_summaries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    sources = [source_label(entry["row"]["source"]) for entry in entries]
    families = [_stable_label(entry["meta"].get("role_family")) for entry in entries]
    keywords = [_stable_label(entry["meta"].get("search_keyword")) for entry in entries]
    return {
        "by_source_queued": _count_labels(sources),
        "by_role_family_queued": _count_labels(families),
        "by_search_keyword_queued": _count_labels(keywords),
    }


def _acquisition_result_summaries(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "by_source_observation": _by_source_observation(results),
        "by_role_family_observation": _observation_by_group(results, lambda item: _stable_label(item.get("role_family"))),
        "by_search_keyword_observation": _observation_by_group(
            results, lambda item: _stable_label(item.get("search_keyword"))
        ),
        "validation_reasons_by_source": _reasons_by_source(results),
    }


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SystemExit(f"{name} must be a positive integer")
    return value


def _run_audit_pool(
    tasks: list[Any],
    worker_fn: Callable[[Any], dict[str, Any]],
    *,
    max_workers: int,
    total: int,
    progress_label: str,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(worker_fn, task) for task in tasks]
        for fut in as_completed(futures):
            results.append(fut.result())
            if total and len(results) % 100 == 0:
                counts = summarize(results)
                elapsed = time.time() - t0
                rate = len(results) / elapsed if elapsed else 0
                left = (total - len(results)) / rate if rate else 0
                print(
                    f"  progress {len(results)}/{total} "
                    f"confirmed_active={counts[CONFIRMED_ACTIVE]} "
                    f"confirmed_expired={counts[CONFIRMED_EXPIRED]} "
                    f"inconclusive={counts[INCONCLUSIVE]} "
                    f"unsupported={counts[UNSUPPORTED]} "
                    f"({rate:.1f}/s, ~{left/60:.0f}m left)",
                    flush=True,
                )
    return results


def _safe_input_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO.resolve()))
    except ValueError:
        return str(path.resolve())


def run_catalog_audit(rows: list[dict[str, Any]], *, max_workers: int, limit: int | None) -> tuple[dict[str, Any], Path]:
    if limit is not None:
        rows = rows[:limit]
    by_source = _count_labels(source_label(row["source"]) for row in rows)
    print(f"Job-ready availability audit: {len(rows)} links", flush=True)
    print("by_source:", by_source, flush=True)
    eta_min = max(1, int(round(len(rows) / 1.4 / 60))) if rows else 0
    print(f"ETA audit: ~{eta_min} min", flush=True)

    probes = default_probes()
    t0 = time.time()
    results = _run_audit_pool(rows, lambda row: audit_row(row, probes), max_workers=max_workers, total=len(rows), progress_label="catalog")
    elapsed = round(time.time() - t0, 1)
    results.sort(key=lambda result: result["job_id"])
    summary = {
        **summarize(results),
        "queued": len(rows),
        "elapsed_sec": elapsed,
        "by_source_queued": by_source,
    }
    if limit is not None:
        summary["limit_requested"] = limit
        summary["limit_effective"] = len(rows)
    report = {**summary, "results": results}
    out = ROOT / "output" / f"job_ready_audit_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return summary, out


def run_acquisition_audit(
    path: Path,
    *,
    max_workers: int,
    limit: int | None,
) -> tuple[dict[str, Any], Path]:
    entries = load_acquisition_jobs(path)
    if limit is not None:
        entries = entries[:limit]
    queued_meta = _acquisition_queued_summaries(entries)
    print(f"Acquisition JSON audit: {len(entries)} jobs from {_safe_input_path(path)}", flush=True)
    print("by_source:", queued_meta["by_source_queued"], flush=True)

    probes = default_probes()
    t0 = time.time()
    results = _run_audit_pool(
        entries,
        lambda entry: audit_acquisition_row(entry, probes=probes),
        max_workers=max_workers,
        total=len(entries),
        progress_label="json",
    )
    elapsed = round(time.time() - t0, 1)
    results.sort(key=lambda result: result["job_id"])
    summary = {
        "mode": "input_json",
        "input_json": _safe_input_path(path),
        **summarize(results),
        **queued_meta,
        **_acquisition_result_summaries(results),
        "queued": len(entries),
        "elapsed_sec": elapsed,
    }
    if limit is not None:
        summary["limit_requested"] = limit
        summary["limit_effective"] = len(entries)
    report = {**summary, "results": results}
    out = ROOT / "output" / f"acquisition_audit_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return summary, out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only catalog or acquisition JSON link audit")
    parser.add_argument(
        "--prune-job-ready",
        action="store_true",
        help="RETIRED. Refused with JOBREADY_SYNC_RETIRED; this audit never modifies JobReady.",
    )
    parser.add_argument(
        "--confirm-target",
        default=None,
        help="RETIRED. Refused with JOBREADY_SYNC_RETIRED; this audit never modifies JobReady.",
    )
    parser.add_argument(
        "--input-json",
        default=None,
        help="Audit a run_all_sources.py JSON artifact without connecting to PostgreSQL.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Audit only the first N rows after deterministic ordering.")
    parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help=f"Concurrent provider probes (default {DEFAULT_MAX_WORKERS}).",
    )
    args = parser.parse_args(argv)
    if args.prune_job_ready or args.confirm_target is not None:
        print(JOBREADY_SYNC_RETIRED, flush=True)
        print(JOBREADY_SYNC_RETIRED_HELP, flush=True)
        return EXIT_RETIRED

    limit = _positive_int("--limit", args.limit) if args.limit is not None else None
    max_workers = _positive_int("--max-workers", args.max_workers)

    if args.input_json:
        if ENV.is_file():
            load_env(ENV)
        path = Path(args.input_json)
        if not path.is_file():
            raise SystemExit(f"INPUT_JSON_NOT_FOUND {path}")
        print("Read-only acquisition audit. No database will be contacted.", flush=True)
        summary, out = run_acquisition_audit(path, max_workers=max_workers, limit=limit)
    else:
        load_env(ENV)
        print("Read-only audit. The JobReady catalog and application database will not be modified.", flush=True)
        rows = fetch_job_ready_rows()
        summary, out = run_catalog_audit(rows, max_workers=max_workers, limit=limit)

    print(json.dumps(summary, indent=2), flush=True)
    print(f"Report: {out}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, flush=True)
            raise SystemExit(2) from None
        raise
