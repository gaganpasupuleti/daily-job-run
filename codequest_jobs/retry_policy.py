"""Bounded, source-aware HTTP retry helpers for lifecycle evidence transport.

Retry logic is transport-only: it does not interpret open/closed semantics.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Callable, TypeVar

import httpx

T = TypeVar("T")

RETRYABLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.NetworkError,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)

DEFAULT_RETRYABLE_STATUS_CODES = frozenset({408, 429}) | frozenset(range(500, 600))


def _validate_policy_id(policy_id: str) -> None:
    if not isinstance(policy_id, str) or not policy_id.strip():
        raise ValueError("policy_id must be a non-empty non-whitespace string")


def _validate_max_attempts(max_attempts: int) -> None:
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise ValueError("max_attempts must be an integer >= 1")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")


def _validate_delay(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number >= 0")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0")


@dataclass(frozen=True)
class LifecycleRetryPolicy:
    """Immutable retry policy. max_attempts counts the initial request plus retries."""

    policy_id: str
    max_attempts: int
    base_delay_seconds: float
    max_delay_seconds: float
    retryable_status_codes: frozenset[int] = DEFAULT_RETRYABLE_STATUS_CODES

    def __post_init__(self) -> None:
        _validate_policy_id(self.policy_id)
        _validate_max_attempts(self.max_attempts)
        _validate_delay("base_delay_seconds", self.base_delay_seconds)
        _validate_delay("max_delay_seconds", self.max_delay_seconds)
        object.__setattr__(self, "retryable_status_codes", frozenset(self.retryable_status_codes))


LINKEDIN_LIFECYCLE_RETRY = LifecycleRetryPolicy(
    policy_id="linkedin-lifecycle-retry-v1",
    max_attempts=3,
    base_delay_seconds=0.5,
    max_delay_seconds=4.0,
)

NAUKRI_LIFECYCLE_RETRY = LifecycleRetryPolicy(
    policy_id="naukri-lifecycle-retry-v1",
    max_attempts=3,
    base_delay_seconds=0.5,
    max_delay_seconds=4.0,
)

INDEED_LIFECYCLE_RETRY = LifecycleRetryPolicy(
    policy_id="indeed-lifecycle-retry-v1",
    max_attempts=3,
    base_delay_seconds=0.5,
    max_delay_seconds=4.0,
)

GREENHOUSE_LIFECYCLE_RETRY = LifecycleRetryPolicy(
    policy_id="greenhouse-lifecycle-retry-v1",
    max_attempts=3,
    base_delay_seconds=0.5,
    max_delay_seconds=4.0,
)


def parse_retry_after_seconds(
    value: str | None,
    *,
    now: Callable[[], float],
) -> float | None:
    """Parse Retry-After (delta-seconds or HTTP-date). Malformed values return None."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = int(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
            if when.tzinfo is None:
                from datetime import timezone

                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, when.timestamp() - now())
        except (TypeError, ValueError, OverflowError):
            return None
    if seconds < 0:
        return None
    return float(seconds)


def policy_backoff_seconds(policy: LifecycleRetryPolicy, retry_index: int) -> float:
    """Deterministic exponential backoff for retry_index 1, 2, ... (after each failure)."""
    if retry_index < 1:
        return 0.0
    exponent = retry_index - 1
    delay = policy.base_delay_seconds * (2**exponent)
    return min(delay, policy.max_delay_seconds)


def delay_before_retry(
    policy: LifecycleRetryPolicy,
    retry_index: int,
    retry_after_header: str | None,
    *,
    now: Callable[[], float],
) -> float:
    """Valid bounded Retry-After wins; otherwise policy backoff (both clamped to max_delay)."""
    parsed = parse_retry_after_seconds(retry_after_header, now=now)
    if parsed is not None:
        return min(parsed, policy.max_delay_seconds)
    return policy_backoff_seconds(policy, retry_index)


def is_retryable_http_status(status_code: int, policy: LifecycleRetryPolicy) -> bool:
    return status_code in policy.retryable_status_codes


def execute_with_retry(
    operation: Callable[[], T],
    policy: LifecycleRetryPolicy,
    *,
    should_retry_result: Callable[[T], bool],
    retry_after_for: Callable[[T], str | None] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> T:
    """Run operation up to policy.max_attempts. Programming errors propagate."""
    sleeper = sleep_fn or time.sleep
    clock = now or time.time
    retry_after_for = retry_after_for or (lambda _value: None)
    attempt = 0
    while True:
        attempt += 1
        value = operation()
        if not should_retry_result(value) or attempt >= policy.max_attempts:
            return value
        delay = delay_before_retry(policy, attempt, retry_after_for(value), now=clock)
        sleeper(delay)


def execute_http_with_retry(
    request: Callable[[], httpx.Response],
    policy: LifecycleRetryPolicy,
    *,
    sleep_fn: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> tuple[httpx.Response | None, BaseException | None]:
    """HTTP GET/POST with bounded retries on transport errors and retryable statuses."""
    sleeper = sleep_fn or time.sleep
    clock = now or time.time
    attempt = 0
    last_error: BaseException | None = None
    last_response: httpx.Response | None = None

    while attempt < policy.max_attempts:
        attempt += 1
        retry_after: str | None = None
        try:
            response = request()
            last_error = None
            last_response = response
            if not is_retryable_http_status(response.status_code, policy):
                return response, None
            retry_after = response.headers.get("Retry-After")
        except RETRYABLE_TRANSPORT_ERRORS as exc:
            last_error = exc
            last_response = None
            if attempt >= policy.max_attempts:
                return None, exc
        else:
            if attempt >= policy.max_attempts:
                return response, None

        delay = delay_before_retry(policy, attempt, retry_after, now=clock)
        sleeper(delay)

    return last_response, last_error
