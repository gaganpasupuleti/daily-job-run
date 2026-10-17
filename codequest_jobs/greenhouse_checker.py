"""Greenhouse Harvest v3 job-post lifecycle evidence (OAuth client credentials)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from link_policy import CONFIRMED_ACTIVE, CONFIRMED_EXPIRED, INCONCLUSIVE
from retry_policy import GREENHOUSE_LIFECYCLE_RETRY, execute_http_with_retry

GREENHOUSE_TOKEN_URL = "https://auth.greenhouse.io/token"
HARVEST_JOB_POSTS_URL = "https://harvest.greenhouse.io/v3/job_posts"

ENV_CLIENT_ID = "GREENHOUSE_HARVEST_CLIENT_ID"
ENV_CLIENT_SECRET = "GREENHOUSE_HARVEST_CLIENT_SECRET"
ENV_USER_ID = "GREENHOUSE_HARVEST_USER_ID"

_BOARD_HOSTS = frozenset({"boards.greenhouse.io", "job-boards.greenhouse.io"})
_JOBS_PATH_RE = re.compile(r"/jobs/(\d+)(?:/)?$", re.IGNORECASE)
_USER_ID_RE = re.compile(r"^\d+$")


@dataclass
class GreenhouseEvidence:
    observation: str
    reason: str
    requested_url: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    observed_identities: tuple[str, ...] | None = None


def _positive_post_id(raw: str | None) -> str | None:
    if not raw or not raw.isdigit():
        return None
    value = int(raw)
    if value <= 0:
        return None
    return str(value)


def _post_id_field(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        return str(value)
    return None


def _strict_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def greenhouse_job_post_id_from_url(url: str) -> tuple[str | None, str | None]:
    """Parse a catalog URL to a Harvest job-post id, or (None, conflict_reason)."""
    if not url:
        return None, None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None, None
    host = (parsed.netloc or "").lower()
    path_id = None
    if host in _BOARD_HOSTS:
        match = _JOBS_PATH_RE.search(parsed.path or "")
        if match:
            path_id = _positive_post_id(match.group(1))
    qs = parse_qs(parsed.query)
    gh_vals = qs.get("gh_jid") or []
    query_id = _positive_post_id(gh_vals[0].strip()) if gh_vals and gh_vals[0] else None
    if host.endswith(".greenhouse.io") or host == "greenhouse.io":
        if path_id and query_id and path_id != query_id:
            return None, "greenhouse_identity_conflict"
        if path_id:
            return path_id, None
        if query_id:
            return query_id, None
        return None, None
    return None, None


def _credentials_present() -> bool:
    return bool((os.getenv(ENV_CLIENT_ID) or "").strip() and (os.getenv(ENV_CLIENT_SECRET) or "").strip())


def _validated_user_id() -> str | None:
    raw = (os.getenv(ENV_USER_ID) or "").strip()
    if not raw:
        return None
    if not _USER_ID_RE.fullmatch(raw):
        return None
    return raw


@dataclass
class _TokenState:
    access_token: str | None = None
    expires_at: float = 0.0


_module_token_state = _TokenState()


def _parse_token_payload(payload: object) -> tuple[str | None, float | None]:
    if not isinstance(payload, dict):
        return None, None
    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        return None, None
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
        return None, None
    if expires_in <= 0:
        return None, None
    return token.strip(), float(expires_in)


def fetch_access_token(
    client: httpx.Client,
    *,
    token_state: _TokenState | None = None,
    now: Callable[[], float] = lambda: 0.0,
    sleep_fn: Callable[[float], None] | None = None,
) -> tuple[str | None, str | None]:
    """Return (bearer token, failure reason). Uses in-memory cache when token_state is provided."""
    if not _credentials_present():
        return None, "greenhouse_auth_missing"
    state = token_state if token_state is not None else _module_token_state
    if state.access_token and now() < state.expires_at:
        return state.access_token, None

    client_id = (os.getenv(ENV_CLIENT_ID) or "").strip()
    client_secret = (os.getenv(ENV_CLIENT_SECRET) or "").strip()
    body: dict[str, str] = {"grant_type": "client_credentials"}
    sub = _validated_user_id()
    if sub is not None:
        body["sub"] = sub
    elif (os.getenv(ENV_USER_ID) or "").strip():
        return None, "greenhouse_auth_failure"

    def _post_token() -> httpx.Response:
        return client.post(
            GREENHOUSE_TOKEN_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(client_id, client_secret),
        )

    try:
        response, transport_error = execute_http_with_retry(_post_token, GREENHOUSE_LIFECYCLE_RETRY, sleep_fn=sleep_fn)
    except httpx.HTTPError:
        return None, "greenhouse_auth_failure"
    if transport_error is not None or response is None:
        return None, "greenhouse_auth_failure"
    if response.status_code in (401, 403):
        return None, "greenhouse_auth_failure"
    if response.status_code == 404:
        return None, "greenhouse_auth_failure"
    if response.status_code != 200:
        return None, "greenhouse_auth_failure"
    try:
        payload = response.json()
    except ValueError:
        return None, "greenhouse_auth_failure"
    token, expires_in = _parse_token_payload(payload)
    if token is None or expires_in is None:
        return None, "greenhouse_auth_failure"
    state.access_token = token
    state.expires_at = now() + expires_in
    return token, None


def _job_posts_request_url(post_id: str, *, active: bool) -> str:
    flag = "true" if active else "false"
    return f"{HARVEST_JOB_POSTS_URL}?ids={post_id}&active={flag}"


def _observed_ids(posts: list[dict[str, Any]]) -> tuple[str, ...]:
    ids: list[str] = []
    for post in posts:
        parsed = _post_id_field(post.get("id"))
        if parsed is not None:
            ids.append(parsed)
    return tuple(sorted(set(ids)))


def _interpret_matched_post(post: dict[str, Any], expected_id: str) -> tuple[str, str] | None:
    active = _strict_bool(post.get("active"))
    live = _strict_bool(post.get("live"))
    internal = _strict_bool(post.get("internal"))
    if active is None or live is None or internal is None:
        return INCONCLUSIVE, "greenhouse_invalid_response"
    if internal:
        return INCONCLUSIVE, "greenhouse_internal_post"
    if active and live:
        return CONFIRMED_ACTIVE, "greenhouse_post_live"
    if active and not live:
        return CONFIRMED_EXPIRED, "greenhouse_post_offline"
    if not active:
        return CONFIRMED_EXPIRED, "greenhouse_post_deleted"
    return INCONCLUSIVE, "greenhouse_invalid_response"


def _select_expected_post(posts: list[dict[str, Any]], expected_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """One matching post, or (None, reason) when ambiguous or contradictory."""
    matches: list[dict[str, Any]] = []
    for post in posts:
        if _post_id_field(post.get("id")) == expected_id:
            matches.append(post)
    if not matches:
        return None, None
    if len(matches) == 1:
        return matches[0], None
    signatures = {
        (
            _strict_bool(post.get("active")),
            _strict_bool(post.get("live")),
            _strict_bool(post.get("internal")),
        )
        for post in matches
    }
    if len(signatures) > 1:
        return None, "greenhouse_invalid_response"
    return matches[0], None


def interpret_harvest_job_posts(
    *,
    status_code: int | None,
    body: object,
    expected_id: str,
    requested_url: str,
    final_url: str | None,
    error: str | None = None,
) -> GreenhouseEvidence:
    if error or status_code is None:
        return GreenhouseEvidence(INCONCLUSIVE, "greenhouse_connection_failure", requested_url=requested_url)
    if status_code == 404:
        return GreenhouseEvidence(
            INCONCLUSIVE,
            "greenhouse_http_404",
            requested_url=requested_url,
            final_url=final_url,
            http_status=status_code,
            observed_identities=None,
        )
    if status_code in (401, 403):
        return GreenhouseEvidence(
            INCONCLUSIVE,
            "greenhouse_auth_failure",
            requested_url=requested_url,
            final_url=final_url,
            http_status=status_code,
            observed_identities=None,
        )
    if status_code != 200:
        return GreenhouseEvidence(
            INCONCLUSIVE,
            f"greenhouse_http_{status_code}",
            requested_url=requested_url,
            final_url=final_url,
            http_status=status_code,
            observed_identities=None,
        )
    if not isinstance(body, list):
        return GreenhouseEvidence(
            INCONCLUSIVE,
            "greenhouse_invalid_response",
            requested_url=requested_url,
            final_url=final_url,
            http_status=status_code,
            observed_identities=None,
        )
    posts: list[dict[str, Any]] = []
    for item in body:
        if not isinstance(item, dict):
            return GreenhouseEvidence(
                INCONCLUSIVE,
                "greenhouse_invalid_response",
                requested_url=requested_url,
                final_url=final_url,
                http_status=status_code,
                observed_identities=None,
            )
        posts.append(item)
    observed = _observed_ids(posts)
    if posts and expected_id not in observed:
        return GreenhouseEvidence(
            INCONCLUSIVE,
            "greenhouse_identity_mismatch",
            requested_url=requested_url,
            final_url=final_url,
            http_status=status_code,
            observed_identities=observed,
        )
    matched, ambiguity = _select_expected_post(posts, expected_id)
    if ambiguity:
        return GreenhouseEvidence(
            INCONCLUSIVE,
            ambiguity,
            requested_url=requested_url,
            final_url=final_url,
            http_status=status_code,
            observed_identities=observed,
        )
    if matched is None:
        return GreenhouseEvidence(
            INCONCLUSIVE,
            "greenhouse_post_not_found",
            requested_url=requested_url,
            final_url=final_url,
            http_status=status_code,
            observed_identities=(),
        )
    verdict = _interpret_matched_post(matched, expected_id)
    if verdict is None:
        return GreenhouseEvidence(
            INCONCLUSIVE,
            "greenhouse_invalid_response",
            requested_url=requested_url,
            final_url=final_url,
            http_status=status_code,
            observed_identities=observed,
        )
    observation, reason = verdict
    return GreenhouseEvidence(
        observation,
        reason,
        requested_url=requested_url,
        final_url=final_url,
        http_status=status_code,
        observed_identities=observed,
    )


def lookup_greenhouse_lifecycle(
    post_id: str,
    client: httpx.Client,
    *,
    token_state: _TokenState | None = None,
    now: Callable[[], float] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> GreenhouseEvidence:
    token, auth_reason = fetch_access_token(client, token_state=token_state, now=now or (lambda: 0.0), sleep_fn=sleep_fn)
    if token is None:
        return GreenhouseEvidence(INCONCLUSIVE, auth_reason or "greenhouse_auth_failure")

    headers = {"Authorization": f"Bearer {token}"}

    def _get_active() -> httpx.Response:
        return client.get(HARVEST_JOB_POSTS_URL, params={"ids": post_id, "active": "true"}, headers=headers)

    active_url = _job_posts_request_url(post_id, active=True)
    try:
        active_resp, active_transport = execute_http_with_retry(_get_active, GREENHOUSE_LIFECYCLE_RETRY, sleep_fn=sleep_fn)
    except httpx.HTTPError:
        return GreenhouseEvidence(INCONCLUSIVE, "greenhouse_connection_failure", requested_url=active_url)
    if active_transport is not None or active_resp is None:
        return GreenhouseEvidence(INCONCLUSIVE, "greenhouse_connection_failure", requested_url=active_url)

    try:
        active_body = active_resp.json()
    except ValueError:
        active_body = None
    active_evidence = interpret_harvest_job_posts(
        status_code=active_resp.status_code,
        body=active_body,
        expected_id=post_id,
        requested_url=active_url,
        final_url=str(active_resp.url),
    )
    if active_evidence.reason != "greenhouse_post_not_found":
        return active_evidence

    def _get_inactive() -> httpx.Response:
        return client.get(HARVEST_JOB_POSTS_URL, params={"ids": post_id, "active": "false"}, headers=headers)

    inactive_url = _job_posts_request_url(post_id, active=False)
    try:
        inactive_resp, inactive_transport = execute_http_with_retry(_get_inactive, GREENHOUSE_LIFECYCLE_RETRY, sleep_fn=sleep_fn)
    except httpx.HTTPError:
        return GreenhouseEvidence(INCONCLUSIVE, "greenhouse_connection_failure", requested_url=inactive_url)
    if inactive_transport is not None or inactive_resp is None:
        return GreenhouseEvidence(INCONCLUSIVE, "greenhouse_connection_failure", requested_url=inactive_url)
    try:
        inactive_body = inactive_resp.json()
    except ValueError:
        inactive_body = None
    return interpret_harvest_job_posts(
        status_code=inactive_resp.status_code,
        body=inactive_body,
        expected_id=post_id,
        requested_url=inactive_url,
        final_url=str(inactive_resp.url),
    )
