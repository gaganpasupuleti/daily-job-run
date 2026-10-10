"""Decide which link observations may change a catalog row.

Only an explicit confirmed-closed signal may mark a job expired.
Unknown, timeouts, blocks, and generic link failures keep the previous state.
"""

from __future__ import annotations

CONFIRMED_EXPIRED = "confirmed_expired"
CONFIRMED_ACTIVE = "confirmed_active"
INCONCLUSIVE = "inconclusive"

# These observations must never expire a catalog row.
INCONCLUSIVE_OBSERVATIONS = {
    "unknown",
    "link_failed",
    "timeout",
    "http_401",
    "http_403",
    "http_408",
    "http_429",
    "http_5xx",
    "connection_failure",
    "bot_block",
    INCONCLUSIVE,
}


def observation_from_http_status(status_code: int) -> str:
    """Generic HTTP result. This is not a provider closed-job signal."""
    if status_code in (401, 403, 408, 429):
        return f"http_{status_code}"
    if status_code >= 500:
        return "http_5xx"
    return "link_failed" if status_code >= 400 else CONFIRMED_ACTIVE


def may_mark_expired(observation: str) -> bool:
    return observation == CONFIRMED_EXPIRED


def next_link_status(previous: str, observation: str) -> str:
    """Return the catalog link_status to store. Previous status is kept unless confirmed."""
    current = (previous or "active").strip() or "active"
    if observation == CONFIRMED_EXPIRED:
        return "expired"
    if observation == CONFIRMED_ACTIVE:
        return "active"
    return current
