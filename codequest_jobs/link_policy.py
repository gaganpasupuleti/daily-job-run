"""Decide which link observations may change a catalog row.

Only an explicit confirmed-closed signal may mark a job expired.
Unknown, timeouts, blocks, and generic link failures keep the previous state.
"""

from __future__ import annotations

import re
import string
from typing import Any
from urllib.parse import urlsplit

CONFIRMED_EXPIRED = "confirmed_expired"
CONFIRMED_ACTIVE = "confirmed_active"
INCONCLUSIVE = "inconclusive"

# Lifecycle validation capability of a catalog source. Unsupported means no provider
# lifecycle adapter exists: it is not closed, not open, and not a provider failure.
SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
UNSUPPORTED_SOURCE = "unsupported_source"
# Sources with a provider lifecycle probe in jobready_validator.default_probes().
SUPPORTED_LIFECYCLE_SOURCES = frozenset({"greenhouse", "indeed", "linkedin", "naukri"})
# Characters stripped from both ends of a source: ASCII space, \t, \n, \v, \f, \r.
SOURCE_TRIM_CHARS = " \t\n\x0b\x0c\r"
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)
_SOURCE_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,31}")


def normalize_source(source: Any) -> str:
    """Strip SOURCE_TRIM_CHARS and lowercase ASCII A-Z only.

    jobready_catalog.SUPPORTED_SOURCE_SQL applies the same rule in PostgreSQL; the two must agree.
    """
    return source.strip(SOURCE_TRIM_CHARS).translate(_ASCII_LOWER) if isinstance(source, str) else ""


def lifecycle_capability(source: Any) -> str:
    """SUPPORTED only for a source with a provider lifecycle probe. Blank and unknown are UNSUPPORTED."""
    return SUPPORTED if normalize_source(source) in SUPPORTED_LIFECYCLE_SOURCES else UNSUPPORTED


def source_label(source: Any) -> str:
    """A bounded report key for a source: 'unknown' when blank, 'other' when not a plain identifier."""
    normalized = normalize_source(source)
    if not normalized:
        return "unknown"
    return normalized if _SOURCE_LABEL_RE.fullmatch(normalized) else "other"

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


EVIDENCE_IDENTITY_MAX_COUNT = 16
# Plain provider requisition ids (LinkedIn and Naukri digits, Indeed hex job keys).
_EVIDENCE_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
# Stands in for a provider identity value that is not a plain id. It cannot match a real id.
REDACTED_IDENTITY = "[redacted]"


def evidence_identity(value: Any) -> str | None:
    """The stored form of one provider requisition identity.

    A plain id is kept as is. Any other value (URL, query, markup, oversized or non-string)
    is stored only as REDACTED_IDENTITY, so no raw response text is persisted. None stays None.
    """
    if value is None:
        return None
    return value if isinstance(value, str) and _EVIDENCE_IDENTITY_RE.fullmatch(value) else REDACTED_IDENTITY


def evidence_identities(values: Any) -> tuple[str, ...] | None:
    """The stored form of the identities a provider response exposed.

    None means no response identity was inspected; an empty tuple means a usable response
    was inspected and carried none. Values are deduplicated, sorted, and capped at
    EVIDENCE_IDENTITY_MAX_COUNT, so the same evidence always stores the same way.
    """
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise TypeError("observed identities must be a collection, not a single value")
    stored = sorted({evidence_identity(value) for value in values if value is not None})
    return tuple(stored[:EVIDENCE_IDENTITY_MAX_COUNT])


def valid_http_status(value: Any) -> bool:
    """A real three-digit status code. LinkedIn answers bot checks with 999, so 600-999 stay valid."""
    return type(value) is int and 100 <= value <= 999
