"""Indeed job status via official GraphQL API (expired flag)."""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)

INDEED_GRAPHQL = "https://apis.indeed.com/graphql"
INDEED_HEADERS = {
    "Host": "apis.indeed.com",
    "content-type": "application/json",
    "indeed-api-key": "161092c2017b5bbab13edb12461a62d5a833871e7cad6d9d475304573de67ac8",
    "accept": "application/json",
    "indeed-locale": "en-IN",
    "indeed-co": "IN",
    "accept-language": "en-IN,en;q=0.9",
    "user-agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6_1 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Indeed App 193.1"
    ),
    "indeed-app-info": "appv=193.1; appid=com.indeed.jobsearch; osv=16.6.1; os=ios; dtype=phone",
}
BATCH_SIZE = 50


def indeed_jk_from_url(url: str) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        if "indeed." not in host:
            return None
        qs = parse_qs(parsed.query)
        jk_vals = qs.get("jk") or []
        if jk_vals and jk_vals[0]:
            return jk_vals[0].strip()
        m = re.search(r"[?&]jk=([a-f0-9]{16})", url, re.I)
        if m:
            return m.group(1)
    except Exception:
        return None
    return None


def batch_indeed_expired(jks: list[str]) -> dict[str, bool | None]:
    """Return jk -> expired (True/False) or None if lookup failed."""
    out: dict[str, bool | None] = {}
    unique = [jk for jk in dict.fromkeys(jks) if jk]
    if not unique:
        return out

    with httpx.Client(timeout=15.0, verify=False) as client:
        for i in range(0, len(unique), BATCH_SIZE):
            chunk = unique[i : i + BATCH_SIZE]
            query = (
                "query { jobData(jobKeys: "
                + json.dumps(chunk)
                + ") { results { job { key expired } } } }"
            )
            try:
                resp = client.post(
                    INDEED_GRAPHQL,
                    headers=INDEED_HEADERS,
                    json={"query": query},
                )
                if resp.status_code != 200:
                    logger.warning("Indeed GraphQL HTTP %s", resp.status_code)
                    for jk in chunk:
                        out.setdefault(jk, None)
                    continue
                data = resp.json()
                results = (
                    data.get("data", {}).get("jobData", {}).get("results") or []
                )
                seen: set[str] = set()
                for item in results:
                    job = (item or {}).get("job") or {}
                    key = job.get("key")
                    if not key:
                        continue
                    seen.add(key)
                    out[key] = bool(job.get("expired"))
                for jk in chunk:
                    if jk not in seen:
                        out[jk] = None
            except Exception as exc:
                logger.warning("Indeed GraphQL batch failed: %s", exc)
                for jk in chunk:
                    out.setdefault(jk, None)
    return out


def check_indeed_via_api(jk: str) -> str | None:
    """Return 'active', 'expired', or None (unknown)."""
    result = batch_indeed_expired([jk]).get(jk)
    if result is True:
        return "expired"
    if result is False:
        return "active"
    return None
