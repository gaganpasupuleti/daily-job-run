"""Indeed job status via official GraphQL API (expired flag)."""

from __future__ import annotations

import json
import os
import logging
import re
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger(__name__)

INDEED_GRAPHQL = "https://apis.indeed.com/graphql"


def indeed_headers() -> dict[str, str]:
    """Build Indeed request headers. The API key stays in the environment."""
    headers = {
        "Host": "apis.indeed.com",
        "content-type": "application/json",
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
    key = (os.getenv("INDEED_API_KEY") or "").strip()
    if key:
        headers["indeed-api-key"] = key
    return headers


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


def _job_data_results(payload: object) -> list | None:
    """The jobData results list of a GraphQL response, or None when the response has none."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    job_data = data.get("jobData") if isinstance(data, dict) else None
    results = job_data.get("results") if isinstance(job_data, dict) else None
    return results if isinstance(results, list) else None


def indeed_returned_keys(payload: object) -> list[object] | None:
    """Every job key the response returned, requested or not, as returned.

    None when the response has no results list. Keys are evidence of what Indeed
    answered, not lifecycle evidence; parse_indeed_results decides the flag.
    """
    results = _job_data_results(payload)
    if results is None:
        return None
    keys: list[object] = []
    for item in results:
        job = item.get("job") if isinstance(item, dict) else None
        if isinstance(job, dict) and job.get("key") is not None:
            keys.append(job["key"])
    return keys


def parse_indeed_results(payload: object, requested: list[str]) -> dict[str, bool | None]:
    """Map each requested jk to the API's boolean expired flag, else None.

    A missing, null, or non-boolean flag is unknown. Keys that were not
    requested are ignored. A key returned twice with different flags is unknown.
    """
    out: dict[str, bool | None] = {jk: None for jk in requested}
    results = _job_data_results(payload)
    if results is None:
        return out
    seen: dict[str, bool | None] = {}
    for item in results:
        job = item.get("job") if isinstance(item, dict) else None
        if not isinstance(job, dict):
            continue
        key = job.get("key")
        if not isinstance(key, str) or key not in out:
            continue
        flag = job.get("expired")
        value = flag if isinstance(flag, bool) else None
        if key in seen and seen[key] != value:
            value = None
        seen[key] = value
    out.update(seen)
    return out


def batch_indeed_expired(jks: list[str]) -> dict[str, bool | None]:
    """Return jk -> expired (True/False) or None if lookup failed or was ambiguous."""
    out: dict[str, bool | None] = {}
    unique = [jk for jk in dict.fromkeys(jks) if jk]
    if not unique:
        return out

    if not (os.getenv("INDEED_API_KEY") or "").strip():
        logger.warning("INDEED_API_KEY is not configured")
        return {jk: None for jk in unique}

    with httpx.Client(timeout=15.0) as client:
        for i in range(0, len(unique), BATCH_SIZE):
            chunk = unique[i : i + BATCH_SIZE]
            payload = _post_job_data(client, chunk)
            if payload is None:
                for jk in chunk:
                    out.setdefault(jk, None)
                continue
            out.update(parse_indeed_results(payload, chunk))
    return out


def _post_job_data(client: httpx.Client, chunk: list[str]) -> object | None:
    """POST one jobData query. The decoded payload, or None on an HTTP or transport failure."""
    query = (
        "query { jobData(jobKeys: "
        + json.dumps(chunk)
        + ") { results { job { key expired } } } }"
    )
    try:
        resp = client.post(
            INDEED_GRAPHQL,
            headers=indeed_headers(),
            json={"query": query},
        )
        if resp.status_code != 200:
            logger.warning("Indeed GraphQL HTTP %s", resp.status_code)
            return None
        return resp.json()
    except Exception as exc:
        logger.warning("Indeed GraphQL batch failed: %s", exc)
        return None


def lookup_indeed_job(jk: str) -> tuple[bool | None, list[object] | None]:
    """One job key: (the strict expired flag, every key the response returned).

    The flag follows parse_indeed_results. The keys are None when no API key is
    configured or no usable response came back; otherwise indeed_returned_keys.
    """
    if not (os.getenv("INDEED_API_KEY") or "").strip():
        logger.warning("INDEED_API_KEY is not configured")
        return None, None
    with httpx.Client(timeout=15.0) as client:
        payload = _post_job_data(client, [jk])
    if payload is None:
        return None, None
    return parse_indeed_results(payload, [jk])[jk], indeed_returned_keys(payload)


def check_indeed_via_api(jk: str) -> str | None:
    """Return 'active', 'expired', or None (unknown)."""
    result = batch_indeed_expired([jk]).get(jk)
    if result is True:
        return "expired"
    if result is False:
        return "active"
    return None
