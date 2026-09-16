"""Shared CodeQuest Postgres connection helpers."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
SCRAPER_ENV = ROOT.parent / "naukri_jobs_scraper" / ".env"


def load_env(env_file: str | Path | None = None) -> None:
    path = Path(env_file) if env_file else SCRAPER_ENV
    if path.is_file():
        load_dotenv(path)
    else:
        load_dotenv()


def get_database_url() -> str:
    url = (
        os.getenv("CODEQUEST_DATABASE_URL", "").strip()
        or os.getenv("DATABASE_URL", "").strip()
    )
    if not url:
        raise RuntimeError(
            "Set CODEQUEST_DATABASE_URL (or DATABASE_URL) in "
            f"{SCRAPER_ENV} for CodeQuest Postgres access."
        )
    return url


def connect(*, connect_timeout: int = 20):
    return psycopg2.connect(get_database_url(), connect_timeout=connect_timeout)


_SCHEMA_READY = False
_RUN_LOGS_READY = False


def ensure_jd_schema(*, force: bool = False) -> None:
    """Apply clean-JD + practice-items schema once per process."""
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    sql_path = ROOT / "schema_jd_clean.sql"
    sql = sql_path.read_text(encoding="utf-8")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    _SCHEMA_READY = True


def ensure_run_logs_schema(*, force: bool = False) -> None:
    """Apply pipeline run-log tables (validation + push + scrape ensure)."""
    global _RUN_LOGS_READY
    if _RUN_LOGS_READY and not force:
        return
    sql_path = ROOT / "schema_pipeline_runs.sql"
    sql = sql_path.read_text(encoding="utf-8")
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    _RUN_LOGS_READY = True
