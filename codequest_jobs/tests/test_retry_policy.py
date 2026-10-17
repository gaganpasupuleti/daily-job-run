"""Pure retry-policy and HTTP retry executor tests (no wall-clock sleeps)."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

import retry_policy as rp  # noqa: E402


class LifecycleRetryPolicyTests(unittest.TestCase):
    def test_max_attempts_validated(self) -> None:
        policy = rp.LifecycleRetryPolicy("test", max_attempts=1, base_delay_seconds=0.0, max_delay_seconds=1.0)
        self.assertEqual(policy.max_attempts, 1)

    def test_zero_max_attempts_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rp.LifecycleRetryPolicy("test", max_attempts=0, base_delay_seconds=0.0, max_delay_seconds=1.0)

    def test_negative_delays_rejected(self) -> None:
        with self.assertRaises(ValueError):
            rp.LifecycleRetryPolicy("test", max_attempts=1, base_delay_seconds=-0.1, max_delay_seconds=1.0)

    def test_deterministic_backoff(self) -> None:
        policy = rp.LifecycleRetryPolicy("test", max_attempts=5, base_delay_seconds=1.0, max_delay_seconds=10.0)
        self.assertEqual(rp.policy_backoff_seconds(policy, 1), 1.0)
        self.assertEqual(rp.policy_backoff_seconds(policy, 2), 2.0)
        self.assertEqual(rp.policy_backoff_seconds(policy, 3), 4.0)

    def test_max_delay_clamp(self) -> None:
        policy = rp.LifecycleRetryPolicy("test", max_attempts=5, base_delay_seconds=1.0, max_delay_seconds=3.0)
        self.assertEqual(rp.policy_backoff_seconds(policy, 5), 3.0)

    def test_retry_after_honored(self) -> None:
        policy = rp.LifecycleRetryPolicy("test", max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=5.0)
        self.assertEqual(rp.parse_retry_after_seconds("2", now=lambda: 0.0), 2.0)
        self.assertEqual(rp.delay_before_retry(policy, 1, "2", now=lambda: 0.0), 2.0)

    def test_retry_after_clamped(self) -> None:
        policy = rp.LifecycleRetryPolicy("test", max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=2.0)
        self.assertEqual(rp.delay_before_retry(policy, 1, "99", now=lambda: 0.0), 2.0)

    def test_malformed_retry_after_safe(self) -> None:
        policy = rp.LifecycleRetryPolicy("test", max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=4.0)
        self.assertIsNone(rp.parse_retry_after_seconds("not-a-date", now=lambda: 0.0))
        self.assertEqual(rp.delay_before_retry(policy, 1, "not-a-date", now=lambda: 0.0), 0.5)

    def test_zero_retry_after(self) -> None:
        self.assertEqual(rp.parse_retry_after_seconds("0", now=lambda: 0.0), 0.0)

    def test_negative_retry_after_invalid(self) -> None:
        self.assertIsNone(rp.parse_retry_after_seconds("-1", now=lambda: 0.0))

    def test_absent_retry_after_uses_backoff(self) -> None:
        policy = rp.LifecycleRetryPolicy("test", max_attempts=3, base_delay_seconds=0.25, max_delay_seconds=4.0)
        self.assertEqual(rp.delay_before_retry(policy, 1, None, now=lambda: 0.0), 0.25)

    def test_http_date_retry_after(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        header = format_datetime(datetime(2026, 1, 1, 12, 0, 3, tzinfo=timezone.utc))
        parsed = rp.parse_retry_after_seconds(header, now=lambda: now.timestamp())
        self.assertEqual(parsed, 3.0)

    def test_stable_policy_ids(self) -> None:
        self.assertEqual(rp.LINKEDIN_LIFECYCLE_RETRY.policy_id, "linkedin-lifecycle-retry-v1")
        self.assertEqual(rp.NAUKRI_LIFECYCLE_RETRY.policy_id, "naukri-lifecycle-retry-v1")
        self.assertEqual(rp.INDEED_LIFECYCLE_RETRY.policy_id, "indeed-lifecycle-retry-v1")


class ExecuteWithRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps: list[float] = []
        self.policy = rp.LifecycleRetryPolicy("test", max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=5.0)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def test_success_stops_immediately(self) -> None:
        calls = 0

        def op() -> int:
            nonlocal calls
            calls += 1
            return 200

        result = rp.execute_with_retry(
            op,
            self.policy,
            should_retry_result=lambda code: code >= 500,
            sleep_fn=self._sleep,
        )
        self.assertEqual((result, calls, self.sleeps), (200, 1, []))

    def test_terminal_result_stops_immediately(self) -> None:
        calls = 0

        def op() -> int:
            nonlocal calls
            calls += 1
            return 404

        result = rp.execute_with_retry(
            op,
            self.policy,
            should_retry_result=lambda code: code >= 500,
            sleep_fn=self._sleep,
        )
        self.assertEqual((result, calls, self.sleeps), (404, 1, []))

    def test_retryable_result_retries(self) -> None:
        sequence = [503, 503, 200]
        calls = 0

        def op() -> int:
            nonlocal calls
            value = sequence[calls]
            calls += 1
            return value

        result = rp.execute_with_retry(
            op,
            self.policy,
            should_retry_result=lambda code: code >= 500,
            sleep_fn=self._sleep,
        )
        self.assertEqual(result, 200)
        self.assertEqual(calls, 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_no_sleep_after_final_attempt(self) -> None:
        sequence = [503, 503, 503]
        calls = 0

        def op() -> int:
            nonlocal calls
            value = sequence[calls]
            calls += 1
            return value

        result = rp.execute_with_retry(
            op,
            self.policy,
            should_retry_result=lambda code: code >= 500,
            sleep_fn=self._sleep,
        )
        self.assertEqual(result, 503)
        self.assertEqual(calls, 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_exhaustion_exact_attempt_count(self) -> None:
        calls = 0

        def op() -> int:
            nonlocal calls
            calls += 1
            return 500

        rp.execute_with_retry(
            op,
            self.policy,
            should_retry_result=lambda code: code >= 500,
            sleep_fn=self._sleep,
        )
        self.assertEqual(calls, 3)

    def test_programming_exception_not_swallowed(self) -> None:
        def op() -> int:
            raise TypeError("programming")

        with self.assertRaises(TypeError):
            rp.execute_with_retry(op, self.policy, should_retry_result=lambda _c: False, sleep_fn=self._sleep)


class ExecuteHttpWithRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps: list[float] = []
        self.policy = rp.LifecycleRetryPolicy("test", max_attempts=3, base_delay_seconds=0.5, max_delay_seconds=4.0)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def _response(self, status: int, *, retry_after: str | None = None) -> httpx.Response:
        headers: dict[str, str] = {}
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return httpx.Response(status, headers=headers, request=httpx.Request("GET", "https://example.test/job"))

    def test_timeout_retries(self) -> None:
        attempts = 0

        def request() -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise httpx.TimeoutException("timeout")
            return self._response(200)

        resp, err = rp.execute_http_with_retry(request, self.policy, sleep_fn=self._sleep)
        self.assertIsNone(err)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(attempts, 2)
        self.assertEqual(len(self.sleeps), 1)

    def test_connection_error_retries(self) -> None:
        attempts = 0

        def request() -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ConnectError("refused", request=httpx.Request("GET", "https://example.test"))
            return self._response(200)

        resp, err = rp.execute_http_with_retry(request, self.policy, sleep_fn=self._sleep)
        self.assertIsNone(err)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(attempts, 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_429_retries(self) -> None:
        attempts = 0

        def request() -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return self._response(429, retry_after="0")
            return self._response(200)

        resp, err = rp.execute_http_with_retry(request, self.policy, sleep_fn=self._sleep)
        self.assertIsNone(err)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(attempts, 2)

    def test_5xx_retries(self) -> None:
        attempts = 0

        def request() -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return self._response(502)
            return self._response(200)

        resp, err = rp.execute_http_with_retry(request, self.policy, sleep_fn=self._sleep)
        self.assertIsNone(err)
        self.assertEqual(resp.status_code, 200)

    def test_terminal_404_does_not_retry(self) -> None:
        attempts = 0

        def request() -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return self._response(404)

        resp, err = rp.execute_http_with_retry(request, self.policy, sleep_fn=self._sleep)
        self.assertIsNone(err)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(attempts, 1)
        self.assertEqual(self.sleeps, [])

    def test_transport_exhaustion_returns_error(self) -> None:
        def request() -> httpx.Response:
            raise httpx.ConnectError("refused", request=httpx.Request("GET", "https://example.test"))

        resp, err = rp.execute_http_with_retry(request, self.policy, sleep_fn=self._sleep)
        self.assertIsNone(resp)
        self.assertIsInstance(err, httpx.ConnectError)
        self.assertEqual(len(self.sleeps), 2)

    def test_retry_after_precedence_over_backoff(self) -> None:
        attempts = 0

        def request() -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return self._response(503, retry_after="2")
            return self._response(200)

        rp.execute_http_with_retry(request, self.policy, sleep_fn=self._sleep)
        self.assertEqual(self.sleeps, [2.0])
