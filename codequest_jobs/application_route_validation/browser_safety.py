"""Navigation safety for shadow browser checks."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_BLOCKED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_ALLOWED_SUFFIXES = (
    ".greenhouse.io",
    "greenhouse.io",
    ".naukri.com",
    "naukri.com",
)


def is_url_allowed(url: str) -> tuple[bool, str]:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False, "malformed_url"
    if parsed.scheme not in {"http", "https"}:
        return False, "scheme_not_allowed"
    host = (parsed.hostname or "").lower()
    if not host or host in _BLOCKED_HOSTS:
        return False, "host_blocked"
    if host.endswith(".local") or host.endswith(".internal"):
        return False, "host_blocked"
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return False, "private_address"
    except ValueError:
        pass
    for suffix in _ALLOWED_SUFFIXES:
        if host == suffix.lstrip(".") or host.endswith(suffix):
            return True, "allowed"
    return False, "host_not_allowlisted"


def resolve_public_host(url: str) -> tuple[bool, str]:
    """shortcut: DNS rebinding guard, skip for allowlisted public suffixes only."""
    ok, reason = is_url_allowed(url)
    if not ok:
        return ok, reason
    host = (urlsplit(url).hostname or "").lower()
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False, "dns_failure"
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback:
            return False, "dns_private_address"
    return True, "allowed"
