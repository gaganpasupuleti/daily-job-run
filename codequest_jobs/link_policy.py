"""Decide which link observations may change a catalog row.

Only an explicit confirmed-closed signal may mark a job expired.
Unknown, timeouts, blocks, and generic link failures keep the previous state.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

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


EVIDENCE_URL_MAX_LENGTH = 2048
_EVIDENCE_DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_EVIDENCE_HOST_RE = re.compile(rf"(?=.{{1,253}}\Z){_HOST_LABEL}(?:\.{_HOST_LABEL})*")


def sanitize_evidence_url(value: Any) -> str | None:
    """The only form in which a URL may be stored as lifecycle evidence.

    Keeps the http(s) scheme, hostname, a non-default port, and the path.
    Drops userinfo, query, and fragment, which can carry credentials or tokens.
    Returns None when the URL cannot be represented safely.
    """
    if not isinstance(value, str) or not value or len(value) > EVIDENCE_URL_MAX_LENGTH:
        return None
    if any(ord(char) <= 0x20 or ord(char) == 0x7F or char == "\\" for char in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in _EVIDENCE_DEFAULT_PORTS or not host or not _EVIDENCE_HOST_RE.fullmatch(host) or port == 0:
        return None
    authority = host if port in (None, _EVIDENCE_DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    return f"{scheme}://{authority}{parts.path}"


def valid_http_status(value: Any) -> bool:
    """A real three-digit status code. LinkedIn answers bot checks with 999, so 600-999 stay valid."""
    return type(value) is int and 100 <= value <= 999
