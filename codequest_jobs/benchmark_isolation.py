"""Fail-closed guards for public collection and private validation benchmarks."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

URL_PATTERN = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)

PRODUCTION_DB_KEYS = frozenset({
    "JOB_READY_DATABASE_URL",
    "JOB_READY_APP_DATABASE_URL",
    "JOBS_SOURCE_DATABASE_URL",
    "DATABASE_URL",
    "DATABASE_URL_SYNC",
    "JOBS_APPLY_DATABASE_URL",
    "JOBREADY_UI_SYNC_APP_URL",
    "JOBREADY_UI_SYNC_CATALOG_URL",
    "JOBREADY_DAILY_CATALOG_URL",
    "JOBREADY_DAILY_APP_URL",
    "JOBREADY_SENTINEL_CODEQUEST_URL",
    "JOBREADY_SENTINEL_SCRAPER_URL",
    "SQL_SANDBOX_ADMIN_DATABASE_URL",
    "SQL_SANDBOX_RUNNER_DATABASE_URL",
})

SECRET_KEYS = frozenset({
    "PGPASSWORD",
    "PGHOST",
    "PGHOSTADDR",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGPASSFILE",
    "GREENHOUSE_HARVEST_CLIENT_SECRET",
    "GREENHOUSE_HARVEST_CLIENT_ID",
    "APIFY_TOKEN",
    "RAILWAY_TOKEN",
    "RAILWAY_API_TOKEN",
})

SYNC_FORBIDDEN_FLAGS = ("--sync-application",)


def redact_urls(text: str) -> str:
    return URL_PATTERN.sub("[url-redacted]", text or "")


def _has_prefix_secret(key: str, value: str) -> bool:
    if not value:
        return False
    if key in SECRET_KEYS or key in PRODUCTION_DB_KEYS:
        return True
    if key.startswith(("JOB_READY_", "JOBS_SOURCE_", "JOBS_APPLY_", "GREENHOUSE_HARVEST_")):
        return True
    if key.startswith("JOBREADY_") and key.endswith("_URL"):
        return True
    return False


def check_public_collection_isolation(environ: dict[str, str] | None = None) -> None:
    """Public live collection must not load catalog or production database settings."""
    env = environ if environ is not None else os.environ
    for key, value in env.items():
        if _has_prefix_secret(key, (value or "").strip()):
            raise ValueError("Production or catalog database settings are forbidden for public collection benchmarks")
    if (env.get("GITHUB_ACTIONS") or "").lower() == "true" and (env.get("INDEED_API_KEY") or "").strip():
        # Allowed if operator configured; not a DB leak. No-op for indeed key.
        pass


def check_validation_benchmark_environment(environ: dict[str, str] | None = None) -> str:
    """Validation benchmarks run only in private, authorized environments."""
    env = environ if environ is not None else os.environ
    if (env.get("GITHUB_ACTIONS") or "").lower() == "true":
        raise ValueError("Live catalog validation benchmarks must not run on GitHub-hosted runners")
    url = (env.get("JOBREADY_AUDIT_READONLY_URL") or env.get("JOB_READY_DATABASE_URL") or "").strip()
    if not url:
        raise ValueError("Read-only catalog URL must be configured for validation benchmarks")
    parsed = urlsplit(url)
    if parsed.scheme not in ("postgresql", "postgres"):
        raise ValueError("Read-only catalog URL must use postgresql scheme")
    return url


def parse_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_disposable_local_catalog(url: str) -> bool:
    host = parse_host(url)
    return host in ("localhost", "127.0.0.1")
