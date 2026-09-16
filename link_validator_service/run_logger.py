"""Persist scrape / link-validation / push run logs to CodeQuest Postgres."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from db import connect, ensure_run_logs_schema

ROOT = Path(__file__).resolve().parent


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _duration_ms(started: datetime | None, finished: datetime | None = None) -> int | None:
    if started is None:
        return None
    end = finished or _now()
    start = started.replace(tzinfo=None) if getattr(started, "tzinfo", None) else started
    return max(0, int((end - start).total_seconds() * 1000))


def start_validation_run(
    *,
    include_bad: bool,
    only_unchecked: bool,
    sources: list[str] | None,
    limit_n: int,
    workers: int,
    queued: int,
    triggered_by: str = "validate_links",
) -> tuple[int, datetime]:
    ensure_run_logs_schema()
    started = _now()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO job_link_validation_runs (
                    started_at, status, triggered_by, include_bad, only_unchecked,
                    sources_json, limit_n, workers, queued
                ) VALUES (%s, 'running', %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    started,
                    triggered_by[:255],
                    include_bad,
                    only_unchecked,
                    json.dumps(sources or []),
                    limit_n,
                    workers,
                    queued,
                ),
            )
            run_id = int(cur.fetchone()[0])
        conn.commit()
    return run_id, started


def finish_validation_run(
    run_id: int,
    *,
    started: datetime,
    status: str,
    checked: int,
    active_count: int,
    expired_count: int,
    failed_link_count: int,
    unknown_count: int = 0,
    by_source: dict[str, Any] | None = None,
    db_after: dict[str, Any] | None = None,
    report_path: str | None = None,
    error: str | None = None,
) -> None:
    ensure_run_logs_schema()
    finished = _now()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_link_validation_runs SET
                    finished_at = %s,
                    duration_ms = %s,
                    status = %s,
                    checked = %s,
                    active_count = %s,
                    expired_count = %s,
                    failed_link_count = %s,
                    unknown_count = %s,
                    by_source_json = %s,
                    db_after_json = %s,
                    report_path = %s,
                    error = %s
                WHERE id = %s
                """,
                (
                    finished,
                    _duration_ms(started, finished),
                    status,
                    checked,
                    active_count,
                    expired_count,
                    failed_link_count,
                    unknown_count,
                    json.dumps(by_source or {}),
                    json.dumps(db_after or {}, default=str),
                    (report_path or "")[:500] or None,
                    (error or "")[:2000] or None,
                    run_id,
                ),
            )
        conn.commit()


def start_push_run(
    *,
    destination: str = "job_ready",
    triggered_by: str = "push_validated_to_job_ready",
) -> tuple[int, datetime]:
    ensure_run_logs_schema()
    started = _now()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO job_push_runs (
                    started_at, status, triggered_by, destination
                ) VALUES (%s, 'running', %s, %s)
                RETURNING id
                """,
                (started, triggered_by[:255], destination[:64]),
            )
            run_id = int(cur.fetchone()[0])
        conn.commit()
    return run_id, started


def finish_push_run(
    run_id: int,
    *,
    started: datetime,
    status: str,
    active_fetched: int,
    inserted_count: int,
    updated_count: int,
    total_after: int,
    by_source: list[tuple[Any, Any]] | dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    ensure_run_logs_schema()
    finished = _now()
    if isinstance(by_source, list):
        by_source_payload: Any = {str(k): int(v) for k, v in by_source}
    else:
        by_source_payload = by_source or {}
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE job_push_runs SET
                    finished_at = %s,
                    duration_ms = %s,
                    status = %s,
                    active_fetched = %s,
                    inserted_count = %s,
                    updated_count = %s,
                    total_after = %s,
                    by_source_json = %s,
                    error = %s
                WHERE id = %s
                """,
                (
                    finished,
                    _duration_ms(started, finished),
                    status,
                    active_fetched,
                    inserted_count,
                    updated_count,
                    total_after,
                    json.dumps(by_source_payload),
                    (error or "")[:2000] or None,
                    run_id,
                ),
            )
        conn.commit()
