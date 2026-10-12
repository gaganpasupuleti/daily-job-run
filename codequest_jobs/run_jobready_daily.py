#!/usr/bin/env python3
"""Canonical daily JobReady catalog run.

JobReady validated_jobs is the only database this command writes.
CodeQuest and the legacy scraper database are not used.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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
    DEFAULT_VALIDATION_LIMIT,
    DailyRunLock,
    apply_link_observations,
    apply_schema,
    catalog_counts,
    catalog_experience_bucket,
    catalog_url,
    connect,
    identity_duplicates,
    release_read_transaction,
    schema_problems,
    select_validation_candidates,
    upsert_jobs,
)
from jobready_validator import validate_candidates  # noqa: E402
from normalize import source_job_id  # noqa: E402
from run_journal import RunJournal  # noqa: E402

OUTPUT_DIR = ROOT / "output"
RUNS_DIR = OUTPUT_DIR / "runs"


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _stage(message: str) -> None:
    print(f"STAGE {message}", flush=True)


def prepare_catalog_row(job) -> dict:
    """Classify in memory and shape one validated_jobs payload. No database I/O."""
    url = (job.job_url or "").strip()
    source = (job.source or "").strip().lower()
    identity = source_job_id(source, url)
    description = job.description or ""
    classified = classify_job_row(
        {
            "job_id": identity,
            "title": job.title,
            "description": description,
            "company": job.company,
            "ingest_profile": job.ingest_profile or job.role_family,
            "link_status": "active",
            "job_type": job.job_type,
        }
    )
    has_description = bool(description.strip())
    return {
        "source_id": identity,
        "source": source,
        "title": job.title or "Untitled role",
        "company": job.company,
        "location": job.location,
        "job_type": job.job_type,
        "job_url": url,
        "apply_url": job.apply_url or url,
        "ingest_profile": job.ingest_profile or job.role_family,
        "date_posted": job.date_posted,
        "salary_min": job.salary_min,
        "salary_max": job.salary_max,
        "currency": job.currency or None,
        "role_family": job.role_family or job.ingest_profile,
        "actual_role_id": classified["actual_role_id"],
        "actual_role_name": classified["actual_role_name"],
        "role_level_id": classified["role_level_id"],
        "experience_level": classified["experience_level"],
        "experience_bucket": catalog_experience_bucket(
            experience_level=classified["experience_level"],
            title=job.title,
            job_type=job.job_type,
        ),
        "jd_summary": classified["jd_summary"] if has_description else None,
        "jd_clean": classified["jd_clean"] if has_description else None,
        "jd_requirements": classified["jd_requirements"] if has_description else None,
        "jd_responsibilities": classified["jd_responsibilities"] if has_description else None,
        "required_skills": classified["required_skills"] if has_description else None,
        "good_to_have_skills": classified["good_to_have_skills"] if has_description else None,
        "tools": classified["tools"] if has_description else None,
        "programming_languages": classified["programming_languages"] if has_description else None,
        "databases": classified["databases"] if has_description else None,
        "frameworks": classified["frameworks"] if has_description else None,
        "student_preparation_topics": classified["student_preparation_topics"] if has_description else None,
        "manual_review_needed": classified["manual_review_needed"],
        "approved_status": classified["approved_status"],
        "mapping_confidence": classified["mapping_confidence"],
    }


def apply_observations(conn, observations) -> dict[str, int]:
    """Apply validator observations. Search presence is not an observation."""
    return apply_link_observations(conn, observations)


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


def suspicious_run(
    scraped: int,
    errors: list[str],
    sources_attempted: list[str],
    source_health: dict | None = None,
) -> str | None:
    failed = sorted(name for name, row in (source_health or {}).items() if row.get("failed"))
    if failed:
        return "source-wide failure: " + ", ".join(failed)
    if scraped == 0 and errors:
        return "empty scrape with source errors"
    if scraped == 0 and sources_attempted:
        return "empty scrape"
    return None


def _priority_ids(conn, upserted: dict) -> list[str]:
    """New rows, plus re-observed rows that still have no conclusive check."""
    priority = list(upserted.get("new_ids") or [])
    reobserved = list(upserted.get("reobserved_ids") or [])
    if not reobserved:
        return priority
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id FROM validated_jobs
            WHERE job_id = ANY(%s)
              AND (
                link_checked_at IS NULL
                OR last_link_observation IS NULL
                OR last_link_observation LIKE 'inconclusive%%'
              )
            """,
            (reobserved,),
        )
        priority.extend(row[0] for row in cur.fetchall() if row[0] not in priority)
    return priority


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
    observations=None,
    app_conn=None,
    apply_membership: bool = False,
    confirm_target: str = "",
    sources_attempted: list[str] | None = None,
    source_errors: list[str] | None = None,
    source_health: dict | None = None,
    hold_lock: DailyRunLock | None = None,
    validate: bool = False,
    validation_limit: int = DEFAULT_VALIDATION_LIMIT,
    full_audit: bool = False,
    probes=None,
    journal: RunJournal | None = None,
) -> dict:
    """Upsert jobs, validate catalog rows, and optionally sync application membership.

    validate=True is the canonical daily path. Injected observations are for tests.
    This function does not create schema. Each stage is journaled before it
    starts and after it ends; failures are recorded and re-raised.
    """
    journal = journal or RunJournal.disabled()
    started = datetime.now(timezone.utc)
    journal.begin("catalog_plan")

    write_committed = False

    def _plan_complete() -> None:
        journal.complete("catalog_plan", rows=len(prepared))
        journal.begin("catalog_write")

    def _write_committed() -> None:
        nonlocal write_committed
        write_committed = True
        journal.mark_committed("catalog_write")

    try:
        problems = schema_problems(catalog_conn)
        if problems:
            raise SystemExit("SCHEMA_REQUIRED " + "; ".join(problems))
        prepared = [prepare_catalog_row(job) for job in jobs]
        manual_review = sum(1 for row in prepared if row["manual_review_needed"])
        families: dict[str, int] = {}
        sources: dict[str, int] = {}
        for row in prepared:
            family = row.get("role_family") or "unspecified"
            families[family] = families.get(family, 0) + 1
            sources[row["source"]] = sources.get(row["source"], 0) + 1
        upserted = upsert_jobs(
            catalog_conn,
            prepared,
            on_plan_complete=_plan_complete,
            on_commit=_write_committed,
        )
    except BaseException as exc:
        failing = journal.running_stage() or "catalog_plan"
        journal.fail(failing, exc, committed=write_committed if failing == "catalog_write" else None)
        raise
    if journal.status("catalog_plan") == "running":
        journal.complete("catalog_plan", rows=len(prepared))
    if prepared:
        journal.complete(
            "catalog_write",
            committed=True,
            new=upserted["new"],
            updated=upserted["updated"],
            unchanged=upserted["unchanged"],
            reobserved=upserted["reobserved"],
        )
    else:
        journal.skip("catalog_write", "no_rows")
    validation = {"checked": 0, "confirmed_active": 0, "confirmed_expired": 0, "inconclusive": 0}
    validation_reasons: dict[str, int] = {}
    validation_started = time.perf_counter()
    if observations is not None:
        journal.skip("validation_select", "injected_observations")
        journal.skip("validation_probe", "injected_observations")
        with journal.stage("validation_write", committed_on_failure=False) as stage:
            validation = apply_observations(catalog_conn, observations)
            stage.committed = True
            stage.counters.update(validation)
        _stage(f"validation selected={validation['checked']}")
    elif validate:
        with journal.stage("validation_select") as stage:
            priority = _priority_ids(catalog_conn, upserted)
            candidates = select_validation_candidates(
                catalog_conn,
                limit=validation_limit,
                full_audit=full_audit,
                priority_ids=priority,
            )
            release_read_transaction(catalog_conn)
            stage.counters.update(priority=len(priority), selected=len(candidates))
        _stage(f"validation selected={len(candidates)}")
        with journal.stage("validation_probe") as stage:
            built = validate_candidates(candidates, probes=probes)
            stage.counters["observed"] = len(built)
        with journal.stage("validation_write", committed_on_failure=False) as stage:
            validation = apply_observations(catalog_conn, built)
            stage.committed = True
            stage.counters.update(validation)
        for item in built:
            label = f"{item.observation}:{item.reason}"
            validation_reasons[label] = validation_reasons.get(label, 0) + 1
    else:
        for name in ("validation_select", "validation_probe", "validation_write"):
            journal.skip(name, "validation_not_requested")
    validation_sec = round(time.perf_counter() - validation_started, 3) if observations is not None or validate else 0.0
    if observations is not None or validate:
        _stage(
            f"validation complete checked={validation['checked']} "
            f"confirmed_active={validation['confirmed_active']} "
            f"confirmed_expired={validation['confirmed_expired']} "
            f"inconclusive={validation['inconclusive']} elapsed={validation_sec}"
        )
    health = source_health or {}
    warning = suspicious_run(len(prepared), source_errors or [], sources_attempted or [], health)
    successful = sorted(name for name, row in health.items() if row.get("succeeded"))
    failed = sorted(name for name, row in health.items() if row.get("failed"))
    membership = None
    if app_conn is None:
        journal.skip("application_sync", "not_requested")
    else:
        with journal.stage("application_sync", committed_on_failure=False if apply_membership else None) as stage:
            membership = membership_would_change(catalog_conn, app_conn)
            print(
                f"would_archive={membership['would_archive']} would_reactivate={membership['would_reactivate']}",
                flush=True,
            )
            stage.counters.update(membership, apply_requested=apply_membership)
            if apply_membership:
                if warning:
                    raise SystemExit(f"membership sync refused: {warning}")
                from sync_job_ready_ui import archive_ui_jobs_not_in_catalog

                membership = archive_ui_jobs_not_in_catalog(confirm_target=confirm_target)
                stage.committed = True
                stage.counters.update({key: value for key, value in membership.items() if isinstance(value, int)})
    with journal.stage("report") as stage:
        totals = catalog_counts(catalog_conn)
        finished = datetime.now(timezone.utc)
        report = {
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "elapsed_sec": round((finished - started).total_seconds(), 3),
            "run_id": journal.run_id if journal.path is not None else None,
            "sources_attempted": sources_attempted or sorted(health) or sorted(sources),
            "sources_successful": successful,
            "sources_failed": failed,
            "source_health": health,
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
            "validation_reasons": dict(sorted(validation_reasons.items())),
            "per_source_counts": sources,
            "per_family_counts": families,
            "manual_review_count": manual_review,
            "catalog_total": totals["total"],
            "catalog_active": totals["active"],
            "catalog_expired": totals["expired"],
            "application_sync": membership,
            "warning": warning,
            "lock_held": hold_lock is not None,
            "validation_limit": None if full_audit else validation_limit,
            "full_audit": full_audit,
            "catalog_plan_sec": upserted.get("catalog_plan_sec", 0.0),
            "catalog_write_sec": upserted.get("catalog_write_sec", 0.0),
            "validation_sec": validation_sec,
        }
        report["report_path"] = str(write_report(report))
        stage.counters["report_path"] = report["report_path"]
    _stage(
        f"report new={report['new_catalog_jobs']} updated={report['updated_catalog_jobs']} "
        f"unchanged={report['unchanged_catalog_jobs']}"
    )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the JobReady-only daily catalog pipeline.")
    parser.add_argument("--sources", default="naukri,indeed,linkedin,google,foundit")
    parser.add_argument("--max-pages", type=int, default=2)
    parser.add_argument("--results-wanted", type=int, default=12)
    parser.add_argument("--hours-old", type=int, default=72)
    parser.add_argument("--sync-application", action="store_true")
    parser.add_argument("--confirm-target", default="")
    parser.add_argument("--preflight", action="store_true", help="Read-only catalog schema and identity check.")
    parser.add_argument("--apply-schema", action="store_true", help="Apply reviewed idempotent catalog DDL.")
    parser.add_argument(
        "--validation-limit",
        type=int,
        default=DEFAULT_VALIDATION_LIMIT,
        help=f"Catalog rows to validate on a normal run (default {DEFAULT_VALIDATION_LIMIT}).",
    )
    parser.add_argument("--full-audit", action="store_true", help="Validate every catalog row.")
    return parser.parse_args(argv)


def _open_catalog():
    try:
        return connect()
    except SystemExit:
        raise
    except Exception:
        raise SystemExit("catalog target is unreachable") from None


def run_preflight() -> int:
    """Read-only. Prints a safe target identity and does not change the catalog."""
    from sync_job_ready_ui import format_target_identity

    conn = _open_catalog()
    try:
        conn.set_session(readonly=True, autocommit=True)
        try:
            identity = format_target_identity(catalog_url())
        except Exception:
            raise SystemExit("PREFLIGHT_FAILED catalog target identity is incomplete") from None
        problems = schema_problems(conn)
        dupes = identity_duplicates(conn) if "table is missing" not in " ".join(problems) else {"source_id": 0, "source_url": 0}
        if dupes["source_id"] or dupes["source_url"]:
            problems.append(
                f"duplicate identities source_id={dupes['source_id']} source_url={dupes['source_url']}"
            )
        print(f"catalog_target {identity}", flush=True)
        print(
            f"duplicate_source_id={dupes['source_id']} duplicate_source_url={dupes['source_url']}",
            flush=True,
        )
        if problems:
            print("PREFLIGHT_FAILED " + "; ".join(problems), flush=True)
            return 1
        print("PREFLIGHT_OK", flush=True)
        return 0
    finally:
        conn.close()


def run_apply_schema() -> int:
    lock = DailyRunLock()
    try:
        lock.acquire()
    except SystemExit:
        print("another JobReady daily run is active", flush=True)
        return 2
    conn = None
    try:
        conn = _open_catalog()
        apply_schema(conn)
        print("SCHEMA_APPLIED", flush=True)
        return 0
    finally:
        if conn is not None:
            conn.close()
        lock.release()


def journal_run_fields(args: argparse.Namespace, *, injected_collector: bool) -> dict:
    """Run-level journal metadata. Flags and counts only; never targets or credentials."""
    return {
        "command": "run_jobready_daily",
        "effective_mode": "full_audit" if args.full_audit else "daily",
        "validation_limit": None if args.full_audit else args.validation_limit,
        "full_audit": bool(args.full_audit),
        "apply_schema": bool(args.apply_schema),
        "membership_sync_requested": bool(args.sync_application),
        "confirm_target_provided": bool(args.confirm_target),
        "sources": args.sources,
        "max_pages": args.max_pages,
        "results_wanted": args.results_wanted,
        "hours_old": args.hours_old,
        "injected_collector": injected_collector,
    }


def execute_daily(args: argparse.Namespace, *, collect=None, probes=None, journal_dir: Path | None = None) -> dict:
    """Orchestration used by the daily command: scrape, upsert, validate, report.

    A run journal is written before any work and updated at each stage. A
    failure is journaled, then re-raised so the command still fails.
    """
    if args.preflight or args.apply_schema:
        raise SystemExit("execute_daily does not change schema; use the command flags")
    from run_all_sources import collect_jobs

    collector = collect or collect_jobs
    journal = RunJournal.start(
        journal_dir if journal_dir is not None else RUNS_DIR,
        run=journal_run_fields(args, injected_collector=collect is not None),
    )
    _stage(f"journal run_id={journal.run_id} path={journal.path}")
    lock = DailyRunLock()
    conn = None
    app_conn = None
    try:
        with journal.stage("preflight") as stage:
            try:
                lock.acquire()
            except SystemExit:
                print("another JobReady daily run is active", flush=True)
                raise
            journal.record_lock_acquired()
            stage.counters["abandoned_previous"] = len(journal.mark_abandoned_previous())
            conn = _open_catalog()
            problems = schema_problems(conn)
            if problems:
                raise SystemExit("SCHEMA_REQUIRED " + "; ".join(problems))
            release_read_transaction(conn)
        scrape_started = time.perf_counter()
        with journal.stage("scrape") as stage:
            scraped = collector(
                sources=args.sources,
                max_pages=args.max_pages,
                results_wanted=args.results_wanted,
                hours_old=args.hours_old,
                write_json=collect is None,
            )
            stage.counters.update(
                jobs=len(scraped.jobs),
                sources_attempted=len(scraped.sources_attempted or []),
                source_errors=len(scraped.errors or []),
            )
        _stage(
            f"scrape complete jobs={len(scraped.jobs)} "
            f"elapsed={round(time.perf_counter() - scrape_started, 3)}"
        )
        if args.sync_application:
            from sync_job_ready_ui import _app_url, _connect

            try:
                app_conn = _connect(_app_url())
            except BaseException as exc:
                journal.fail("application_sync", exc, committed=False, code="APPLICATION_CONNECT_FAILED")
                raise
        report = run_daily(
            catalog_conn=conn,
            jobs=scraped.jobs,
            app_conn=app_conn,
            apply_membership=args.sync_application,
            confirm_target=args.confirm_target,
            sources_attempted=scraped.sources_attempted,
            source_errors=scraped.errors,
            source_health=getattr(scraped, "source_health", None),
            hold_lock=lock,
            validate=True,
            validation_limit=args.validation_limit,
            full_audit=args.full_audit,
            probes=probes,
            journal=journal,
        )
        journal.finish_completed(report_path=report["report_path"])
        print(
            json.dumps(
                {
                    "new_catalog_jobs": report["new_catalog_jobs"],
                    "validated_checked": report["validated_checked"],
                    "catalog_active": report["catalog_active"],
                    "report_path": report["report_path"],
                    "run_journal": str(journal.path),
                }
            ),
            flush=True,
        )
        return report
    except BaseException as exc:
        try:
            journal.finish_failed(exc)
        except Exception:
            print("JOURNAL_WRITE_FAILED", flush=True)
        print(f"run_journal {journal.path} terminal_state={journal.data['terminal_state']}", flush=True)
        raise
    finally:
        if app_conn is not None:
            app_conn.close()
        if conn is not None:
            conn.close()
        lock.release()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.preflight and args.apply_schema:
        raise SystemExit("choose either --preflight or --apply-schema")
    if args.preflight:
        return run_preflight()
    if args.apply_schema:
        return run_apply_schema()
    try:
        execute_daily(args)
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        if isinstance(exc.code, int):
            print(str(exc), flush=True)
            return exc.code
        print(str(exc), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
