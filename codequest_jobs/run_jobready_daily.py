#!/usr/bin/env python3
"""Canonical daily JobReady catalog run.

JobReady validated_jobs is the only database this command writes.
CodeQuest and the legacy scraper database are not used.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
SCRAPER = REPO / "naukri_jobs_scraper"
for _entry in (str(SCRAPER), str(ROOT)):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

from enrichment import classify_job_row  # noqa: E402
from jobready_catalog import (  # noqa: E402
    DailyRunLock,
    catalog_counts,
    ensure_schema,
    mark_confirmed_expired,
    upsert_jobs,
)
from link_policy import may_mark_expired, next_link_status  # noqa: E402
from normalize import source_job_id  # noqa: E402

OUTPUT_DIR = ROOT / "output"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")


def prepare_catalog_row(job) -> dict:
    """Classify in memory and shape one validated_jobs payload. No database I/O."""
    url = (job.job_url or "").strip()
    source = (job.source or "").strip().lower()
    identity = source_job_id(source, url)
    classified = classify_job_row(
        {
            "job_id": identity,
            "title": job.title,
            "description": job.description,
            "company": job.company,
            "ingest_profile": job.ingest_profile or job.role_family,
            "link_status": "active",
            "job_type": job.job_type,
        }
    )
    return {
        "source_id": identity,
        "source": source,
        "title": job.title or "Untitled role",
        "company": job.company,
        "location": job.location,
        "job_type": job.job_type,
        "job_url": url,
        "apply_url": job.apply_url or url,
        "link_status": "active",
        "ingest_profile": job.ingest_profile or job.role_family,
        "role_family": job.role_family or job.ingest_profile,
        "actual_role_id": classified["actual_role_id"],
        "actual_role_name": classified["actual_role_name"],
        "role_level_id": classified["role_level_id"],
        "experience_level": classified["experience_level"],
        "experience_bucket": classified["experience_level"],
        "jd_summary": classified["jd_summary"],
        "jd_clean": classified["jd_clean"],
        "manual_review_needed": classified["manual_review_needed"],
        "approved_status": classified["approved_status"],
        "mapping_confidence": classified["mapping_confidence"],
        "required_skills": classified["required_skills"],
    }


def apply_observations(conn, observations: list[tuple[str, str, str]]) -> dict[str, int]:
    """Apply link observations. Only confirmed_expired changes a row to expired."""
    expired_ids: list[str] = []
    counts = {"checked": 0, "confirmed_active": 0, "confirmed_expired": 0, "inconclusive": 0}
    with conn.cursor() as cur:
        for job_id, previous, observation in observations:
            counts["checked"] += 1
            if may_mark_expired(observation):
                counts["confirmed_expired"] += 1
                expired_ids.append(job_id)
            elif observation == "confirmed_active":
                counts["confirmed_active"] += 1
                if previous != "active":
                    cur.execute(
                        """
                        UPDATE validated_jobs
                        SET link_status = 'active', updated_at = NOW(), synced_at = NOW()
                        WHERE job_id = %s
                        """,
                        (job_id,),
                    )
            else:
                counts["inconclusive"] += 1
                if next_link_status(previous, observation) != previous:
                    raise RuntimeError("inconclusive observation changed status")
    conn.commit()
    mark_confirmed_expired(conn, expired_ids)
    return counts


def membership_would_change(catalog_conn, app_conn) -> dict[str, int]:
    """Dry-run counts. Active catalog rows are the only membership."""
    from sync_job_ready_ui import _count_membership

    with catalog_conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id FROM validated_jobs
            WHERE job_id IS NOT NULL AND link_status = 'active'
            """
        )
        keep = [row[0] for row in cur.fetchall()]
    with app_conn.cursor() as cur:
        would_archive, would_reactivate = _count_membership(cur, keep)
    return {"would_archive": would_archive, "would_reactivate": would_reactivate}


def suspicious_run(scraped: int, errors: list[str], sources_attempted: list[str]) -> str | None:
    if scraped == 0 and errors:
        return "empty scrape with source errors"
    if scraped == 0 and sources_attempted:
        return "empty scrape"
    return None


def write_report(payload: dict) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started = payload.get("started_at") or datetime.now(timezone.utc).isoformat()
    stamp = started.replace("-", "").replace(":", "").replace("T", "_")[:15]
    path = OUTPUT_DIR / f"jobready_daily_{stamp}.json"
    text = json.dumps(payload, indent=2, default=str)
    lowered = text.lower()
    if "postgresql://" in lowered or "password=" in lowered:
        raise SystemExit("report refused because it contained a credential")
    path.write_text(text, encoding="utf-8")
    return path


def run_daily(
    *,
    catalog_conn,
    jobs,
    observations: list[tuple[str, str, str]] | None = None,
    app_conn=None,
    apply_membership: bool = False,
    confirm_target: str = "",
    sources_attempted: list[str] | None = None,
    source_errors: list[str] | None = None,
    hold_lock: DailyRunLock | None = None,
) -> dict:
    """Upsert jobs, validate, and optionally sync application membership."""
    started = datetime.now(timezone.utc)
    ensure_schema(catalog_conn)
    prepared = [prepare_catalog_row(job) for job in jobs]
    manual_review = sum(1 for row in prepared if row["manual_review_needed"])
    families: dict[str, int] = {}
    sources: dict[str, int] = {}
    for row in prepared:
        families[row.get("role_family") or "unspecified"] = families.get(row.get("role_family") or "unspecified", 0) + 1
        sources[row["source"]] = sources.get(row["source"], 0) + 1
    upserted = upsert_jobs(catalog_conn, prepared)
    validation = {"checked": 0, "confirmed_active": 0, "confirmed_expired": 0, "inconclusive": 0}
    if observations:
        validation = apply_observations(catalog_conn, observations)
    totals = catalog_counts(catalog_conn)
    warning = suspicious_run(len(prepared), source_errors or [], sources_attempted or [])
    membership = None
    if app_conn is not None:
        membership = membership_would_change(catalog_conn, app_conn)
        if apply_membership:
            if warning:
                raise SystemExit(f"membership sync refused: {warning}")
            from sync_job_ready_ui import archive_ui_jobs_not_in_catalog

            membership = archive_ui_jobs_not_in_catalog(confirm_target=confirm_target)
    finished = datetime.now(timezone.utc)
    report = {
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_sec": round((finished - started).total_seconds(), 3),
        "sources_attempted": sources_attempted or sorted(sources),
        "sources_successful": sorted(sources),
        "source_errors": source_errors or [],
        "jobs_scraped": len(jobs),
        "unique_after_dedupe": len(prepared),
        "new_catalog_jobs": upserted["new"],
        "updated_catalog_jobs": upserted["updated"],
        "unchanged_catalog_jobs": upserted["unchanged"],
        "reobserved_jobs": upserted["reobserved"],
        "validated_checked": validation["checked"],
        "confirmed_active": validation["confirmed_active"],
        "confirmed_expired": validation["confirmed_expired"],
        "unknown_or_inconclusive": validation["inconclusive"],
        "per_source_counts": sources,
        "per_family_counts": families,
        "manual_review_count": manual_review,
        "catalog_total": totals["total"],
        "catalog_active": totals["active"],
        "catalog_expired": totals["expired"],
        "application_sync": membership,
        "warning": warning,
        "lock_held": hold_lock is not None,
    }
    report["report_path"] = str(write_report(report))
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the JobReady-only daily catalog pipeline.")
    parser.add_argument("--sources", default="naukri,indeed,linkedin,google,foundit")
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--results-wanted", type=int, default=12)
    parser.add_argument("--hours-old", type=int, default=72)
    parser.add_argument("--sync-application", action="store_true")
    parser.add_argument("--confirm-target", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    from run_all_sources import collect_jobs

    lock = DailyRunLock()
    try:
        lock.acquire()
    except SystemExit:
        print("another JobReady daily run is active", flush=True)
        return 2
    try:
        import jobready_catalog

        conn = jobready_catalog.connect()
        try:
            scraped = collect_jobs(
                sources=args.sources,
                max_pages=args.max_pages,
                results_wanted=args.results_wanted,
                hours_old=args.hours_old,
                write_json=True,
            )
            app_conn = None
            if args.sync_application:
                from sync_job_ready_ui import _app_url, _connect

                app_conn = _connect(_app_url())
            report = run_daily(
                catalog_conn=conn,
                jobs=scraped.jobs,
                app_conn=app_conn,
                apply_membership=args.sync_application,
                confirm_target=args.confirm_target,
                sources_attempted=scraped.sources_attempted,
                source_errors=scraped.errors,
                hold_lock=lock,
            )
            print(json.dumps({k: report[k] for k in ("new_catalog_jobs", "catalog_active", "report_path")}))
        finally:
            conn.close()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
