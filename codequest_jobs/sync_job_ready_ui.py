#!/usr/bin/env python3
"""Keep JobReady application jobs aligned with the catalog.

Archive and reactivation update status and is_active in the same statement.
A write requires host:port/database confirmation and refuses a catalog/application swap.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT.parent / "naukri_jobs_scraper" / ".env")

ARCHIVE_SQL = """
UPDATE jobs j
SET status = 'archived',
    is_active = false,
    updated_at = NOW()
WHERE j.external_id LIKE 'jobs-server:%'
  AND NOT EXISTS (
    SELECT 1 FROM keep_cqj k
    WHERE k.job_id = substr(j.external_id, length('jobs-server:') + 1)
  )
  AND (
    j.status IS DISTINCT FROM 'archived'
    OR j.is_active IS DISTINCT FROM false
  )
"""

REACTIVATE_SQL = """
UPDATE jobs j
SET status = 'active',
    is_active = true,
    updated_at = NOW()
WHERE j.external_id LIKE 'jobs-server:%'
  AND EXISTS (
    SELECT 1 FROM keep_cqj k
    WHERE k.job_id = substr(j.external_id, length('jobs-server:') + 1)
  )
  AND (
    j.status IS DISTINCT FROM 'active'
    OR j.is_active IS DISTINCT FROM true
  )
"""


def _catalog_url() -> str:
    url = (os.getenv("JOB_READY_DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("APPLY_REFUSED catalog target must be configured")
    return url


def _app_url() -> str:
    url = (os.getenv("JOB_READY_APP_DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("APPLY_REFUSED application target must be configured")
    return url


def database_identity(database_url: str) -> tuple[str, int, str]:
    """Return host, port, and database name. Never include user, password, or query."""
    raw = (database_url or "").strip()
    raw = raw.replace("postgresql+asyncpg://", "postgresql://", 1)
    raw = raw.replace("postgres://", "postgresql://", 1)
    parsed = urlparse(raw)
    database = (parsed.path or "").lstrip("/").split("?")[0]
    host = (parsed.hostname or "").lower()
    if not host or not database or "@" in host:
        raise ValueError("target identity is incomplete")
    return (host, parsed.port or 5432, database)


def format_target_identity(database_url: str) -> str:
    host, port, database = database_identity(database_url)
    return f"host={host} port={port} database={database}"


def parse_target_confirmation(value: str) -> tuple[str, int, str]:
    """Parse host:port/database. A confirmation must not carry credentials."""
    text = (value or "").strip()
    if not text or "@" in text or "://" in text or " " in text:
        raise ValueError("target confirmation must be host:port/database")
    host_port, separator, database = text.partition("/")
    host, port_separator, port_text = host_port.partition(":")
    if not separator or not port_separator or not host or not database or "/" in database:
        raise ValueError("target confirmation must be host:port/database")
    if not port_text.isdigit():
        raise ValueError("target confirmation must be host:port/database")
    return (host.lower(), int(port_text), database)


def assert_application_write_target(confirm_target: str) -> str:
    """Confirm the application write target. Raises SystemExit before any write."""
    if not (confirm_target or "").strip():
        raise SystemExit("APPLY_REFUSED explicit target confirmation is required")
    try:
        app = _app_url()
        catalog = _catalog_url()
        confirmed = parse_target_confirmation(confirm_target)
        app_identity = database_identity(app)
        catalog_identity = database_identity(catalog)
    except ValueError as exc:
        raise SystemExit("APPLY_REFUSED target confirmation does not match") from exc
    if confirmed != app_identity:
        raise SystemExit("APPLY_REFUSED target confirmation does not match")
    if app_identity == catalog_identity:
        raise SystemExit("APPLY_REFUSED target is the catalog database")
    return format_target_identity(app)


def _stage_keep(cur, keep_ids: list[str]) -> None:
    cur.execute("CREATE TEMP TABLE keep_cqj (job_id TEXT PRIMARY KEY) ON COMMIT DROP")
    if keep_ids:
        execute_values(cur, "INSERT INTO keep_cqj (job_id) VALUES %s", [(job_id,) for job_id in keep_ids])


def _count_membership(cur, keep_ids: list[str]) -> tuple[int, int]:
    """Count rows a write would change. Uses bound parameters and no temp table."""
    cur.execute(
        """
        SELECT COUNT(*)
        FROM jobs j
        WHERE j.external_id LIKE 'jobs-server:%%'
          AND NOT (substr(j.external_id, length('jobs-server:') + 1) = ANY(%s))
          AND (
            j.status IS DISTINCT FROM 'archived'
            OR j.is_active IS DISTINCT FROM false
          )
        """,
        (keep_ids,),
    )
    would_archive = int(cur.fetchone()[0])
    cur.execute(
        """
        SELECT COUNT(*)
        FROM jobs j
        WHERE j.external_id LIKE 'jobs-server:%%'
          AND substr(j.external_id, length('jobs-server:') + 1) = ANY(%s)
          AND (
            j.status IS DISTINCT FROM 'active'
            OR j.is_active IS DISTINCT FROM true
          )
        """,
        (keep_ids,),
    )
    return would_archive, int(cur.fetchone()[0])


def apply_membership(cur, keep_ids: list[str]) -> dict[str, int]:
    """Archive missing catalog ids and reactivate ids that returned. Caller commits."""
    _stage_keep(cur, keep_ids)
    cur.execute(ARCHIVE_SQL)
    archived = int(cur.rowcount)
    cur.execute(REACTIVATE_SQL)
    reactivated = int(cur.rowcount)
    return {"archived": archived, "reactivated": reactivated}


def _connect(url: str):
    try:
        return psycopg2.connect(url, connect_timeout=20)
    except Exception:
        raise SystemExit("UI_SYNC_FAILED") from None


def _fetch_catalog_ids(catalog_url: str) -> list[str]:
    with _connect(catalog_url) as src:
        with src.cursor() as cur:
            cur.execute("SELECT job_id FROM validated_jobs WHERE job_id IS NOT NULL")
            return [row[0] for row in cur.fetchall()]


def archive_ui_jobs_not_in_catalog(*, dry_run: bool = False, confirm_target: str = "") -> dict[str, int]:
    """Align application jobs with the current catalog.

    dry_run counts changes and does not write. A real write updates status and
    is_active together and requires confirm_target.
    """
    catalog_url = _catalog_url()
    app_url = _app_url()
    if dry_run:
        try:
            print(f"TARGET {format_target_identity(app_url)}", flush=True)
        except ValueError:
            raise SystemExit("APPLY_REFUSED target identity is incomplete") from None
    else:
        print(f"TARGET {assert_application_write_target(confirm_target)}", flush=True)

    keep = _fetch_catalog_ids(catalog_url)
    with _connect(app_url) as dest:
        if dry_run:
            dest.set_session(readonly=True, autocommit=False)
        with dest.cursor() as cur:
            if dry_run:
                would_archive, would_reactivate = _count_membership(cur, keep)
                archived = 0
                reactivated = 0
            else:
                result = apply_membership(cur, keep)
                archived = result["archived"]
                reactivated = result["reactivated"]
                would_archive = archived
                would_reactivate = reactivated
            cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'active'")
            active_after = int(cur.fetchone()[0])
        if dry_run:
            dest.rollback()
        else:
            dest.commit()
    print(
        f"UI sync catalog={len(keep)} would_archive={would_archive} "
        f"would_reactivate={would_reactivate} archived={archived} "
        f"reactivated={reactivated} active_after={active_after} dry_run={dry_run}",
        flush=True,
    )
    return {
        "catalog": len(keep),
        "would_archive": would_archive,
        "would_reactivate": would_reactivate,
        "archived": archived,
        "reactivated": reactivated,
        "active_after": active_after,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Align JobReady application jobs with the catalog")
    parser.add_argument("--dry-run", action="store_true", help="Count changes without writing")
    parser.add_argument(
        "--confirm-target",
        default="",
        help="Application target as host:port/database. Required for a write. Not a DSN.",
    )
    args = parser.parse_args(argv)
    if not args.dry_run and not args.confirm_target.strip():
        print("APPLY_REFUSED explicit target confirmation is required", flush=True)
        return 2
    try:
        archive_ui_jobs_not_in_catalog(dry_run=args.dry_run, confirm_target=args.confirm_target)
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        if isinstance(exc.code, str):
            print(exc.code, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
