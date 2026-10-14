#!/usr/bin/env python3
"""Full link validation for scraped_jobs — concurrent HTTP + Naukri API for Naukri URLs.

This command never writes the JobReady catalog or the JobReady application
database. Catalog lifecycle changes come from run_jobready_daily.py; application
membership is sync_job_ready_ui.py. The retired --sync-job-ready-every and
--confirm-target options are refused before any environment or database access.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg2

from db import connect, load_env
from indeed_checker import batch_indeed_expired, indeed_jk_from_url
from linkedin_checker import check_linkedin_guest
from run_logger import finish_validation_run, start_validation_run
from link_checker import (
    LINK_STATUS_ACTIVE,
    LINK_STATUS_EXPIRED,
    LINK_STATUS_LINK_FAILED,
    LINK_STATUS_UNKNOWN,
    _naukri_job_id_from_url,
    check_naukri_via_api,
    check_url,
)

OUTPUT_DIR = ROOT / "output"
JOBREADY_SYNC_RETIRED = "JOBREADY_SYNC_RETIRED"
JOBREADY_SYNC_RETIRED_HELP = (
    "validate_links.py no longer writes the JobReady catalog or application database.\n"
    "Catalog lifecycle changes come from codequest_jobs/run_jobready_daily.py.\n"
    "Application membership reconciliation is codequest_jobs/sync_job_ready_ui.py."
)
EXIT_RETIRED = 2
SLOW_SOURCES = {"naukri", "linkedin", "foundit"}
_slow_lock = threading.Lock()
_slow_next = 0.0


def _pace_if_slow(source: str) -> None:
    """Naukri and LinkedIn block a burst. One request at a time, with a pause."""
    if source not in SLOW_SOURCES:
        return
    delay = float(os.getenv("LINK_VALIDATE_SLOW_DELAY", "0.75"))
    if delay <= 0:
        return
    global _slow_next
    with _slow_lock:
        now = time.time()
        wait = _slow_next - now
        if wait > 0:
            time.sleep(wait)
        _slow_next = time.time() + delay


def fetch_jobs(
    *,
    only_unchecked: bool,
    include_bad: bool,
    limit: int,
    sources: list[str] | None = None,
    max_age_days: int = 0,
) -> list[tuple]:
    sql = """
        SELECT id, job_id, source, job_url, apply_url, COALESCE(link_status, 'active')
        FROM scraped_jobs
        WHERE job_url IS NOT NULL AND trim(job_url) <> ''
    """
    params: list[Any] = []
    if sources:
        sql += " AND lower(source) = ANY(%s)"
        params.append([s.lower() for s in sources])
    if max_age_days > 0:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max_age_days)
        sql += " AND created_at >= %s"
        params.append(cutoff)
    if only_unchecked:
        sql += " AND link_checked_at IS NULL"
    elif not include_bad:
        sql += " AND COALESCE(link_status, 'active') IN ('active', 'unknown')"
    sql += " ORDER BY link_checked_at ASC NULLS FIRST, created_at DESC"
    if limit > 0:
        sql += " LIMIT %s"
        params.append(limit)

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def build_indeed_expired_map(rows: list[tuple]) -> dict[str, bool | None]:
    jks: list[str] = []
    for _row_id, _job_id, source, job_url, apply_url, _old in rows:
        if (source or "").lower() != "indeed":
            continue
        jk = indeed_jk_from_url(job_url or apply_url or "")
        if jk:
            jks.append(jk)
    if not jks:
        return {}
    print(f"Indeed GraphQL lookup for {len(set(jks))} job keys...", flush=True)
    return batch_indeed_expired(jks)


def validate_one(
    row: tuple,
    *,
    use_naukri_api: bool = True,
    indeed_expired: dict[str, bool | None] | None = None,
) -> tuple[str, str, str, str]:
    row_id, job_id, source, job_url, apply_url, _old = row
    src = (source or "").lower()
    _pace_if_slow(src)
    # Indeed status must use the viewjob URL (jk=), not the external apply URL.
    if src == "indeed":
        url = (job_url or apply_url or "").strip()
    else:
        url = (apply_url or job_url or "").strip()
    # LinkedIn's job page rate-limits a burst and a 429 is not a closed job.
    # The guest page is the closed-job signal. Skip the page HEAD.
    if src == "linkedin":
        status = check_linkedin_guest(job_url or url) or LINK_STATUS_UNKNOWN
    else:
        status = check_url(url)
    if src == "indeed":
        jk = indeed_jk_from_url(job_url or url)
        if jk and indeed_expired is not None and jk in indeed_expired:
            expired = indeed_expired[jk]
            if expired is True:
                status = LINK_STATUS_EXPIRED
            elif expired is False:
                status = LINK_STATUS_ACTIVE
        elif jk:
            from indeed_checker import check_indeed_via_api

            api_status = check_indeed_via_api(jk)
            if api_status == LINK_STATUS_EXPIRED:
                status = LINK_STATUS_EXPIRED
            elif api_status == LINK_STATUS_ACTIVE:
                status = LINK_STATUS_ACTIVE
    elif use_naukri_api and src == "naukri" and os.getenv("LINK_VALIDATE_SKIP_NAUKRI") != "1":
        nid = _naukri_job_id_from_url(url)
        if nid:
            api_status = check_naukri_via_api(nid)
            # Naukri detail API often false-positives expired; trust live HTTP when active.
            if api_status == LINK_STATUS_ACTIVE:
                status = LINK_STATUS_ACTIVE
            # Do not expire from the Naukri detail API. It false-expires live jobs.
    # A block or timeout is not proof the job closed. Keep the previous
    # status unless we saw an explicit expired or live signal.
    previous = (_old or "").strip()
    if status == LINK_STATUS_UNKNOWN and previous:
        status = previous
    elif status == LINK_STATUS_LINK_FAILED and previous in (LINK_STATUS_ACTIVE, LINK_STATUS_EXPIRED):
        status = previous
    return row_id, job_id or "", src, status


def apply_updates(results: list[tuple[str, str, str, str]]) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            with connect() as conn:
                with conn.cursor() as cur:
                    for row_id, _job_id, _source, status in results:
                        cur.execute(
                            """
                            UPDATE scraped_jobs
                            SET link_status = %s, link_checked_at = %s
                            WHERE id = %s
                            """,
                            (status, now, row_id),
                        )
                conn.commit()
            return
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            last_err = exc
            print(f"  apply_updates retry {attempt}/3 after DB drop", flush=True)
            time.sleep(2 * attempt)
    if last_err:
        raise last_err


def status_summary() -> dict[str, Any]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(link_status, '(null)'), COUNT(*)
                FROM scraped_jobs
                GROUP BY 1 ORDER BY 2 DESC
                """
            )
            by_status = {k: v for k, v in cur.fetchall()}
            cur.execute(
                """
                SELECT COALESCE(source, 'unknown'), COALESCE(link_status, '(null)'), COUNT(*)
                FROM scraped_jobs
                GROUP BY 1, 2
                ORDER BY 1, 3 DESC
                """
            )
            by_source = {}
            for source, status, n in cur.fetchall():
                by_source.setdefault(source, {})[status] = n
            cur.execute(
                """
                SELECT
                  COUNT(*) FILTER (WHERE link_checked_at IS NULL) AS never_checked,
                  COUNT(*) FILTER (WHERE link_checked_at IS NOT NULL) AS checked,
                  MAX(link_checked_at) AS last_checked
                FROM scraped_jobs
                """
            )
            never, checked, last = cur.fetchone()
    return {
        "by_status": by_status,
        "by_source": by_source,
        "never_checked": never,
        "checked": checked,
        "last_checked": last.isoformat(sep=" ", timespec="seconds") if last else None,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Validate scraped_jobs URLs")
    p.add_argument("--env-file", default=str(ROOT.parent / "naukri_jobs_scraper" / ".env"))
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--batch-size", type=int, default=100, help="DB commit every N results")
    p.add_argument("--limit", type=int, default=0, help="0 = all matching")
    p.add_argument(
        "--only-unchecked",
        action="store_true",
        help="Only rows with link_checked_at IS NULL",
    )
    p.add_argument(
        "--include-bad",
        action="store_true",
        help="Also recheck expired/link_failed (full revalidation)",
    )
    p.add_argument(
        "--sources",
        default="",
        help="Comma-separated sources to validate, e.g. indeed,naukri",
    )
    p.add_argument(
        "--max-age-days",
        type=int,
        default=0,
        help="Only validate jobs scraped in the last N days (0 = all)",
    )
    p.add_argument(
        "--sync-job-ready-every",
        default=None,
        help="RETIRED. Refused with JOBREADY_SYNC_RETIRED; JobReady is never modified by this command.",
    )
    p.add_argument(
        "--confirm-target",
        default=None,
        help="RETIRED. Refused with JOBREADY_SYNC_RETIRED; JobReady is never modified by this command.",
    )
    args = p.parse_args()
    if args.sync_job_ready_every is not None or args.confirm_target is not None:
        print(JOBREADY_SYNC_RETIRED, flush=True)
        print(JOBREADY_SYNC_RETIRED_HELP, flush=True)
        raise SystemExit(EXIT_RETIRED)
    load_env(args.env_file)
    print("JobReady catalog and application database will not be modified", flush=True)
    source_filter = [s.strip().lower() for s in args.sources.split(",") if s.strip()] or None

    rows = fetch_jobs(
        only_unchecked=args.only_unchecked,
        include_bad=args.include_bad,
        limit=args.limit,
        sources=source_filter,
        max_age_days=args.max_age_days,
    )
    if args.max_age_days > 0:
        print(f"Filter: scraped within last {args.max_age_days} days", flush=True)
    print(f"Queued for validation: {len(rows)}", flush=True)
    run_id, run_started = start_validation_run(
        include_bad=args.include_bad,
        only_unchecked=args.only_unchecked,
        sources=source_filter,
        limit_n=args.limit,
        workers=args.workers,
        queued=len(rows),
    )
    print(f"Validation run log id={run_id}", flush=True)
    if not rows:
        summary = status_summary()
        finish_validation_run(
            run_id,
            started=run_started,
            status="success",
            checked=0,
            active_count=0,
            expired_count=0,
            failed_link_count=0,
            db_after=summary,
        )
        print(json.dumps(summary, indent=2), flush=True)
        return

    indeed_map = build_indeed_expired_map(rows)
    counts = {
        "checked": 0,
        LINK_STATUS_ACTIVE: 0,
        LINK_STATUS_EXPIRED: 0,
        LINK_STATUS_LINK_FAILED: 0,
        LINK_STATUS_UNKNOWN: 0,
    }
    by_source_delta: dict[str, dict[str, int]] = {}
    t0 = time.time()
    pending: list[tuple[str, str, str, str]] = []
    run_error: str | None = None
    run_status = "success"

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(validate_one, row, indeed_expired=indeed_map): row for row in rows
            }
            for fut in as_completed(futures):
                row_id, job_id, source, status = fut.result()
                pending.append((row_id, job_id, source, status))
                counts["checked"] += 1
                counts[status] = counts.get(status, 0) + 1
                by_source_delta.setdefault(source or "unknown", {})
                by_source_delta[source or "unknown"][status] = (
                    by_source_delta[source or "unknown"].get(status, 0) + 1
                )
                if len(pending) >= args.batch_size:
                    apply_updates(pending)
                    pending.clear()
                    elapsed = time.time() - t0
                    rate = counts["checked"] / elapsed if elapsed else 0
                    print(
                        f"  progress {counts['checked']}/{len(rows)} "
                        f"active={counts[LINK_STATUS_ACTIVE]} "
                        f"expired={counts[LINK_STATUS_EXPIRED]} "
                        f"failed={counts[LINK_STATUS_LINK_FAILED]} "
                        f"unknown={counts[LINK_STATUS_UNKNOWN]} "
                        f"({rate:.1f}/s)",
                        flush=True,
                    )

        if pending:
            apply_updates(pending)
    except Exception as exc:
        run_status = "failed"
        run_error = type(exc).__name__
        if pending:
            try:
                apply_updates(pending)
            except Exception:
                pass
        raise
    finally:
        summary = status_summary()
        report = {
            "run_id": run_id,
            "validated_this_run": counts,
            "by_source_this_run": by_source_delta,
            "db_after": summary,
            "elapsed_sec": round(time.time() - t0, 1),
        }
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = OUTPUT_DIR / f"link_validation_{stamp}.json"
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        try:
            finish_validation_run(
                run_id,
                started=run_started,
                status=run_status,
                checked=counts["checked"],
                active_count=counts.get(LINK_STATUS_ACTIVE, 0),
                expired_count=counts.get(LINK_STATUS_EXPIRED, 0),
                failed_link_count=counts.get(LINK_STATUS_LINK_FAILED, 0),
                unknown_count=counts.get(LINK_STATUS_UNKNOWN, 0),
                by_source=by_source_delta,
                db_after=summary,
                report_path=str(out),
                error=run_error,
            )
        except Exception as log_exc:
            print(f"WARNING: failed to write validation run log: {log_exc}", flush=True)
        print(json.dumps(report, indent=2, default=str), flush=True)
        print(f"Report: {out}", flush=True)


if __name__ == "__main__":
    main()
