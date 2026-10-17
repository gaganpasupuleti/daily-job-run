"""Offline tests for Greenhouse Harvest v3 lifecycle adapter (D13)."""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import greenhouse_checker as gh  # noqa: E402
import jobready_validator  # noqa: E402
import retry_policy  # noqa: E402
from jobready_validator import probe_greenhouse, validate_candidates  # noqa: E402
from link_policy import CONFIRMED_ACTIVE, CONFIRMED_EXPIRED, INCONCLUSIVE  # noqa: E402
from lifecycle_projection import (  # noqa: E402
    CONFIRMED_OPEN,
    REASON_CONFIRMED_OPEN,
    default_authority_policy,
    project_lifecycle,
)

FAKE_ENV = {
    gh.ENV_CLIENT_ID: "fixture-client-id",
    gh.ENV_CLIENT_SECRET: "fixture-client-secret",
}

GREENHOUSE_ROW = {
    "job_id": "gh-job-1",
    "source": "greenhouse",
    "job_url": "https://boards.greenhouse.io/acme/jobs/12345",
}


def _post(post_id: int, *, active: bool, live: bool, internal: bool) -> dict:
    return {"id": post_id, "active": active, "live": live, "internal": internal}


def _token_json(expires_in: int = 3600) -> dict:
    return {"access_token": "fixture-access-token", "token_type": "Bearer", "expires_in": expires_in}


class GreenhouseTestBase(unittest.TestCase):
    def setUp(self) -> None:
        gh._invalidate_token(gh._module_token_state)
        self.sleeps: list[float] = []
        self._sleep_patcher = patch.object(retry_policy.time, "sleep", self.sleeps.append)
        self._sleep_patcher.start()

    def tearDown(self) -> None:
        self._sleep_patcher.stop()


class GreenhouseUrlParsingTests(unittest.TestCase):
    def test_boards_path_id(self) -> None:
        url = "https://boards.greenhouse.io/acme/jobs/99"
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), ("99", None))

    def test_job_boards_path_id(self) -> None:
        url = "https://job-boards.greenhouse.io/co/jobs/42/"
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), ("42", None))

    def test_gh_jid_query(self) -> None:
        url = "https://boards.greenhouse.io/acme?gh_jid=7"
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), ("7", None))

    def test_path_and_gh_jid_agree(self) -> None:
        url = "https://boards.greenhouse.io/acme/jobs/5?gh_jid=5"
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), ("5", None))

    def test_path_and_gh_jid_disagree(self) -> None:
        url = "https://boards.greenhouse.io/acme/jobs/5?gh_jid=6"
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), (None, "greenhouse_identity_conflict"))

    def test_missing_id(self) -> None:
        self.assertEqual(gh.greenhouse_job_post_id_from_url("https://boards.greenhouse.io/acme"), (None, None))

    def test_malformed_id(self) -> None:
        self.assertEqual(
            gh.greenhouse_job_post_id_from_url("https://boards.greenhouse.io/acme/jobs/abc"),
            (None, None),
        )

    def test_zero_rejected(self) -> None:
        self.assertEqual(
            gh.greenhouse_job_post_id_from_url("https://boards.greenhouse.io/acme/jobs/0"),
            (None, None),
        )

    def test_unrelated_domain(self) -> None:
        self.assertEqual(gh.greenhouse_job_post_id_from_url("https://example.com/jobs/1?gh_jid=1"), (None, None))


class GreenhouseInterpretTests(unittest.TestCase):
    def test_live_active(self) -> None:
        body = [_post(1, active=True, live=True, internal=False)]
        ev = gh.interpret_harvest_job_posts(
            status_code=200,
            body=body,
            expected_id="1",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=True,
        )
        self.assertEqual((ev.observation, ev.reason), (CONFIRMED_ACTIVE, "greenhouse_post_live"))

    def test_offline_active(self) -> None:
        body = [_post(2, active=True, live=False, internal=False)]
        ev = gh.interpret_harvest_job_posts(
            status_code=200,
            body=body,
            expected_id="2",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=True,
        )
        self.assertEqual((ev.observation, ev.reason), (CONFIRMED_EXPIRED, "greenhouse_post_offline"))

    def test_internal(self) -> None:
        body = [_post(3, active=True, live=True, internal=True)]
        ev = gh.interpret_harvest_job_posts(
            status_code=200,
            body=body,
            expected_id="3",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=True,
        )
        self.assertEqual((ev.observation, ev.reason), (INCONCLUSIVE, "greenhouse_internal_post"))

    def test_identity_mismatch(self) -> None:
        body = [_post(9, active=True, live=True, internal=False)]
        ev = gh.interpret_harvest_job_posts(
            status_code=200,
            body=body,
            expected_id="1",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=True,
        )
        self.assertEqual((ev.observation, ev.reason), (INCONCLUSIVE, "greenhouse_identity_mismatch"))
        self.assertEqual(ev.observed_identities, ("9",))

    def test_malformed_boolean(self) -> None:
        body = [{"id": 1, "active": "true", "live": True, "internal": False}]
        ev = gh.interpret_harvest_job_posts(
            status_code=200,
            body=body,
            expected_id="1",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=True,
        )
        self.assertEqual(ev.reason, "greenhouse_invalid_response")

    def test_malformed_json_shape(self) -> None:
        ev = gh.interpret_harvest_job_posts(
            status_code=200,
            body={"not": "a list"},
            expected_id="1",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=True,
        )
        self.assertEqual(ev.reason, "greenhouse_invalid_response")

    def test_http_404(self) -> None:
        ev = gh.interpret_harvest_job_posts(
            status_code=404,
            body=None,
            expected_id="1",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=True,
        )
        self.assertEqual((ev.observation, ev.reason), (INCONCLUSIVE, "greenhouse_http_404"))

    def test_active_query_with_deleted_row_invalid(self) -> None:
        body = [_post(1, active=False, live=False, internal=False)]
        ev = gh.interpret_harvest_job_posts(
            status_code=200,
            body=body,
            expected_id="1",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=True,
        )
        self.assertEqual((ev.observation, ev.reason), (INCONCLUSIVE, "greenhouse_invalid_response"))

    def test_inactive_query_deleted(self) -> None:
        body = [_post(1, active=False, live=False, internal=False)]
        ev = gh.interpret_harvest_job_posts(
            status_code=200,
            body=body,
            expected_id="1",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=False,
        )
        self.assertEqual((ev.observation, ev.reason), (CONFIRMED_EXPIRED, "greenhouse_post_deleted"))

    def test_inactive_query_live_true_invalid(self) -> None:
        body = [_post(1, active=True, live=True, internal=False)]
        ev = gh.interpret_harvest_job_posts(
            status_code=200,
            body=body,
            expected_id="1",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=False,
        )
        self.assertEqual((ev.observation, ev.reason), (INCONCLUSIVE, "greenhouse_invalid_response"))

    def test_inactive_query_offline_invalid(self) -> None:
        body = [_post(1, active=True, live=False, internal=False)]
        ev = gh.interpret_harvest_job_posts(
            status_code=200,
            body=body,
            expected_id="1",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=False,
        )
        self.assertEqual((ev.observation, ev.reason), (INCONCLUSIVE, "greenhouse_invalid_response"))


class GreenhouseTransportTests(GreenhouseTestBase):
    def _client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)

    def test_deleted_second_query(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            active = request.url.params.get("active")
            if active == "true":
                return httpx.Response(200, json=[], request=request)
            return httpx.Response(200, json=[_post(12345, active=False, live=False, internal=False)], request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with self._client(handler) as client:
                ev = gh.lookup_greenhouse_lifecycle("12345", client, token_state=gh._TokenState())
        self.assertEqual((ev.observation, ev.reason), (CONFIRMED_EXPIRED, "greenhouse_post_deleted"))

    def test_both_empty_not_closure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            return httpx.Response(200, json=[], request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with self._client(handler) as client:
                ev = gh.lookup_greenhouse_lifecycle("12345", client, token_state=gh._TokenState())
        self.assertEqual((ev.observation, ev.reason), (INCONCLUSIVE, "greenhouse_post_not_found"))

    def test_exhaustion_never_confirmed_expired(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            attempts += 1
            return httpx.Response(503, request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with self._client(handler) as client:
                ev = gh.lookup_greenhouse_lifecycle("12345", client, token_state=gh._TokenState())
        self.assertEqual(ev.observation, INCONCLUSIVE)
        self.assertNotEqual(ev.observation, CONFIRMED_EXPIRED)
        self.assertGreaterEqual(attempts, retry_policy.GREENHOUSE_LIFECYCLE_RETRY.max_attempts)


class GreenhouseAuthTests(GreenhouseTestBase):
    def test_missing_client_id_no_http(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(500, request=request)

        with patch.dict(os.environ, {gh.ENV_CLIENT_SECRET: "x"}, clear=True):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token, reason = gh.fetch_access_token(client)
        self.assertEqual((token, reason), (None, "greenhouse_auth_missing"))
        self.assertFalse(called)

    def test_missing_secret_no_http(self) -> None:
        with patch.dict(os.environ, {gh.ENV_CLIENT_ID: "x"}, clear=True):
            with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
                token, reason = gh.fetch_access_token(client)
        self.assertEqual((token, reason), (None, "greenhouse_auth_missing"))

    def test_successful_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.host, "auth.greenhouse.io")
            return httpx.Response(200, json=_token_json(), request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token, reason = gh.fetch_access_token(client, token_state=gh._TokenState())
        self.assertEqual(reason, None)
        self.assertEqual(token, "fixture-access-token")

    def test_token_401_inconclusive(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token, reason = gh.fetch_access_token(client, token_state=gh._TokenState())
        self.assertEqual((token, reason), (None, "greenhouse_auth_failure"))

    def test_optional_sub(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.content.decode())
            return httpx.Response(200, json=_token_json(), request=request)

        env = {**FAKE_ENV, gh.ENV_USER_ID: "42"}
        with patch.dict(os.environ, env, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                gh.fetch_access_token(client, token_state=gh._TokenState())
        self.assertTrue(any("sub=42" in body for body in seen))

    def test_invalid_user_id_skips_request(self) -> None:
        called = False
        env = {**FAKE_ENV, gh.ENV_USER_ID: "not-numeric"}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json=_token_json(), request=request)

        with patch.dict(os.environ, env, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token, reason = gh.fetch_access_token(client, token_state=gh._TokenState())
        self.assertEqual((token, reason), (None, "greenhouse_auth_failure"))
        self.assertFalse(called)

    def test_token_not_in_provenance(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            return httpx.Response(
                200,
                json=[_post(12345, active=True, live=True, internal=False)],
                request=request,
            )

        real_client = httpx.Client

        def client_factory(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), timeout=15.0)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with patch.object(jobready_validator.httpx, "Client", client_factory):
                result = probe_greenhouse(GREENHOUSE_ROW)
        joined = " ".join(filter(None, [result.requested_url, result.final_url, result.reason]))
        self.assertNotIn("fixture-access-token", joined)
        self.assertNotIn("fixture-client-secret", joined)


class GreenhouseGhJidTests(unittest.TestCase):
    def test_duplicate_conflicting_gh_jid(self) -> None:
        url = "https://boards.greenhouse.io/acme?gh_jid=5&gh_jid=6"
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), (None, "greenhouse_identity_conflict"))

    def test_duplicate_identical_gh_jid(self) -> None:
        url = "https://boards.greenhouse.io/acme?gh_jid=5&gh_jid=5"
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), ("5", None))

    def test_path_with_conflicting_duplicate_gh_jid(self) -> None:
        url = "https://boards.greenhouse.io/acme/jobs/5?gh_jid=5&gh_jid=6"
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), (None, "greenhouse_identity_conflict"))

    def test_valid_plus_blank_gh_jid_conflict(self) -> None:
        url = "https://boards.greenhouse.io/acme?gh_jid=5&gh_jid="
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), (None, "greenhouse_identity_conflict"))

    def test_valid_plus_malformed_gh_jid_conflict(self) -> None:
        url = "https://boards.greenhouse.io/acme?gh_jid=5&gh_jid=abc"
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), (None, "greenhouse_identity_conflict"))

    def test_path_with_blank_duplicate_gh_jid_conflict(self) -> None:
        url = "https://boards.greenhouse.io/acme/jobs/5?gh_jid=5&gh_jid="
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), (None, "greenhouse_identity_conflict"))

    def test_single_blank_gh_jid_no_identity(self) -> None:
        url = "https://boards.greenhouse.io/acme?gh_jid="
        self.assertEqual(gh.greenhouse_job_post_id_from_url(url), (None, None))

    def test_mixed_malformed_conflict_no_http(self) -> None:
        row = {
            **GREENHOUSE_ROW,
            "job_url": "https://boards.greenhouse.io/acme?gh_jid=5&gh_jid=",
        }
        with patch.object(jobready_validator.httpx, "Client") as mock_client:
            result = probe_greenhouse(row)
        mock_client.assert_not_called()
        self.assertEqual(result.reason, "greenhouse_identity_conflict")


class GreenhouseTokenCacheTests(GreenhouseTestBase):
    def test_production_clock_is_monotonic(self) -> None:
        self.assertIs(gh.PRODUCTION_CLOCK, time.monotonic)

    def test_cache_reuse_before_expiry(self) -> None:
        state = gh._TokenState()
        clock = {"t": 100.0}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_token_json(60), request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token1, _ = gh.fetch_access_token(client, token_state=state, now=lambda: clock["t"])
                clock["t"] = 120.0
                token2, _ = gh.fetch_access_token(client, token_state=state, now=lambda: clock["t"])
        self.assertEqual(token1, token2)

    def test_refresh_at_and_after_expiry(self) -> None:
        state = gh._TokenState()
        clock = {"t": 0.0}
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=_token_json(10), request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                gh.fetch_access_token(client, token_state=state, now=lambda: clock["t"])
                clock["t"] = 10.0
                gh.fetch_access_token(client, token_state=state, now=lambda: clock["t"])
                clock["t"] = 10.1
                gh.fetch_access_token(client, token_state=state, now=lambda: clock["t"])
        self.assertEqual(calls, 2)

    def test_malformed_expires_in_values(self) -> None:
        cases = [
            {"expires_in": 0},
            {"expires_in": -1},
            {"expires_in": float("nan")},
            {"expires_in": float("inf")},
        ]
        for extra in cases:
            payload = {"access_token": "t", "token_type": "Bearer", **extra}
            token, expires = gh._parse_token_payload(payload)
            self.assertIsNone(token)
            self.assertIsNone(expires)

    def test_cache_invalidated_on_client_id_change(self) -> None:
        state = gh._TokenState()
        clock = {"t": 0.0}
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=_token_json(3600), request=request)

        env = dict(FAKE_ENV)
        with patch.dict(os.environ, env, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                gh.fetch_access_token(client, token_state=state, now=lambda: clock["t"])
                with patch.dict(os.environ, {gh.ENV_CLIENT_ID: "other-client"}, clear=False):
                    gh.fetch_access_token(client, token_state=state, now=lambda: clock["t"])
        self.assertEqual(calls, 2)

    def test_valid_sub_cache_reuse(self) -> None:
        state = gh._TokenState()
        calls = 0
        env = {**FAKE_ENV, gh.ENV_USER_ID: "99"}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=_token_json(3600), request=request)

        with patch.dict(os.environ, env, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                gh.fetch_access_token(client, token_state=state, now=lambda: 0.0)
                gh.fetch_access_token(client, token_state=state, now=lambda: 1.0)
        self.assertEqual(calls, 1)

    def test_changed_valid_sub_refetches(self) -> None:
        state = gh._TokenState()
        calls = 0
        env = {**FAKE_ENV, gh.ENV_USER_ID: "99"}

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=_token_json(3600), request=request)

        with patch.dict(os.environ, env, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                gh.fetch_access_token(client, token_state=state, now=lambda: 0.0)
                with patch.dict(os.environ, {gh.ENV_USER_ID: "100"}, clear=False):
                    gh.fetch_access_token(client, token_state=state, now=lambda: 1.0)
        self.assertEqual(calls, 2)

    def test_invalid_sub_after_cached_no_sub_token(self) -> None:
        state = gh._TokenState()
        state.access_token = "stale-token"
        state.expires_at = 99999.0
        state.cache_key = (FAKE_ENV[gh.ENV_CLIENT_ID], "")
        http_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal http_calls
            http_calls += 1
            return httpx.Response(200, json=_token_json(), request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token, reason = gh.fetch_access_token(client, token_state=state, now=lambda: 0.0)
                with patch.dict(os.environ, {gh.ENV_USER_ID: "not-numeric"}, clear=False):
                    token2, reason2 = gh.fetch_access_token(client, token_state=state, now=lambda: 0.0)
        self.assertEqual((token, reason), ("stale-token", None))
        self.assertEqual((token2, reason2), (None, "greenhouse_auth_failure"))
        self.assertEqual(http_calls, 0)

    def test_invalid_sub_empty_cache_no_http(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=_token_json(), request=request)

        env = {**FAKE_ENV, gh.ENV_USER_ID: "bad-sub"}
        with patch.dict(os.environ, env, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token, reason = gh.fetch_access_token(client, token_state=gh._TokenState(), now=lambda: 0.0)
        self.assertEqual((token, reason), (None, "greenhouse_auth_failure"))
        self.assertEqual(calls, 0)

    def test_harvest_401_clears_cache_for_retry(self) -> None:
        state = gh._TokenState()
        clock = {"t": 0.0}
        token_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            if request.method == "POST":
                token_calls += 1
                return httpx.Response(200, json=_token_json(3600), request=request)
            if token_calls == 1:
                return httpx.Response(401, request=request)
            return httpx.Response(200, json=[_post(12345, active=True, live=True, internal=False)], request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                ev1 = gh.lookup_greenhouse_lifecycle("12345", client, token_state=state, now=lambda: clock["t"])
                ev2 = gh.lookup_greenhouse_lifecycle("12345", client, token_state=state, now=lambda: clock["t"])
        self.assertEqual(ev1.reason, "greenhouse_auth_failure")
        self.assertEqual(ev2.observation, CONFIRMED_ACTIVE)
        self.assertEqual(token_calls, 2)


class GreenhouseTokenRetryTests(GreenhouseTestBase):
    def test_malformed_token_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not-json", request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token, reason = gh.fetch_access_token(client, token_state=gh._TokenState(), now=lambda: 0.0)
        self.assertEqual((token, reason), (None, "greenhouse_auth_failure"))

    def test_missing_access_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"expires_in": 60}, request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token, reason = gh.fetch_access_token(client, token_state=gh._TokenState(), now=lambda: 0.0)
        self.assertEqual((token, reason), (None, "greenhouse_auth_failure"))

    def test_token_403(self) -> None:
        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, request=r))) as client:
                token, reason = gh.fetch_access_token(client, token_state=gh._TokenState(), now=lambda: 0.0)
        self.assertEqual((token, reason), (None, "greenhouse_auth_failure"))

    def test_token_429_then_success(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, request=request)
            return httpx.Response(200, json=_token_json(), request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token, reason = gh.fetch_access_token(client, token_state=gh._TokenState(), now=lambda: 0.0)
        self.assertEqual(reason, None)
        self.assertEqual(token, "fixture-access-token")
        self.assertEqual(attempts, 2)

    def test_token_5xx_then_success(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, request=request)
            return httpx.Response(200, json=_token_json(), request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token, reason = gh.fetch_access_token(client, token_state=gh._TokenState(), now=lambda: 0.0)
        self.assertEqual(reason, None)
        self.assertEqual(attempts, 2)

    def test_token_transport_exhaustion(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler)) as client:
                token, reason = gh.fetch_access_token(client, token_state=gh._TokenState(), now=lambda: 0.0)
        self.assertEqual((token, reason), (None, "greenhouse_auth_failure"))


class GreenhouseHarvestRetryTests(GreenhouseTestBase):
    def _lookup(self, handler):
        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0) as client:
                return gh.lookup_greenhouse_lifecycle("12345", client, token_state=gh._TokenState(), now=lambda: 0.0)

    def test_429_then_success(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, request=request)
            return httpx.Response(200, json=[_post(12345, active=True, live=True, internal=False)], request=request)

        ev = self._lookup(handler)
        self.assertEqual(ev.observation, CONFIRMED_ACTIVE)
        self.assertEqual(attempts, 2)

    def test_503_then_success(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, request=request)
            return httpx.Response(200, json=[_post(12345, active=True, live=True, internal=False)], request=request)

        ev = self._lookup(handler)
        self.assertEqual(ev.observation, CONFIRMED_ACTIVE)

    def test_timeout_then_success(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            attempts += 1
            if attempts == 1:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json=[_post(12345, active=True, live=True, internal=False)], request=request)

        ev = self._lookup(handler)
        self.assertEqual(ev.observation, CONFIRMED_ACTIVE)

    def test_harvest_401_not_closure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            return httpx.Response(401, request=request)

        ev = self._lookup(handler)
        self.assertEqual((ev.observation, ev.reason), (INCONCLUSIVE, "greenhouse_auth_failure"))

    def test_harvest_403_not_closure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            return httpx.Response(403, request=request)

        ev = self._lookup(handler)
        self.assertEqual((ev.observation, ev.reason), (INCONCLUSIVE, "greenhouse_auth_failure"))

    def test_harvest_404_not_closure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            return httpx.Response(404, request=request)

        ev = self._lookup(handler)
        self.assertEqual((ev.observation, ev.reason), (INCONCLUSIVE, "greenhouse_http_404"))


class GreenhouseIdentityProvenanceTests(GreenhouseTestBase):
    def test_empty_usable_body_observed_empty(self) -> None:
        ev = gh.interpret_harvest_job_posts(
            status_code=200,
            body=[],
            expected_id="1",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=True,
        )
        self.assertEqual(ev.observed_identities, ())

    def test_non_200_observed_none(self) -> None:
        ev = gh.interpret_harvest_job_posts(
            status_code=500,
            body=None,
            expected_id="1",
            requested_url=gh.HARVEST_JOB_POSTS_URL,
            final_url=gh.HARVEST_JOB_POSTS_URL,
            query_active=True,
        )
        self.assertIsNone(ev.observed_identities)

    def test_second_query_provenance(self) -> None:
        inactive_url = gh._job_posts_request_url("12345", active=False)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            if request.url.params.get("active") == "true":
                return httpx.Response(200, json=[], request=request)
            return httpx.Response(200, json=[_post(12345, active=False, live=False, internal=False)], request=request)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0) as client:
                ev = gh.lookup_greenhouse_lifecycle("12345", client, token_state=gh._TokenState(), now=lambda: 0.0)
        self.assertIn("active=false", ev.requested_url or "")
        self.assertEqual(ev.requested_url, inactive_url)


class GreenhouseProbeTests(GreenhouseTestBase):
    def test_identity_conflict_no_request(self) -> None:
        row = {**GREENHOUSE_ROW, "job_url": "https://boards.greenhouse.io/a/jobs/1?gh_jid=2"}
        with patch.object(jobready_validator.httpx, "Client") as mock_client:
            result = probe_greenhouse(row)
        mock_client.assert_not_called()
        self.assertEqual(result.reason, "greenhouse_identity_conflict")

    def test_duplicate_gh_jid_conflict_no_request(self) -> None:
        row = {**GREENHOUSE_ROW, "job_url": "https://boards.greenhouse.io/a?gh_jid=5&gh_jid=6"}
        with patch.object(jobready_validator.httpx, "Client") as mock_client:
            result = probe_greenhouse(row)
        mock_client.assert_not_called()
        self.assertEqual(result.reason, "greenhouse_identity_conflict")

    def test_one_observation_per_validate(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(200, json=_token_json(), request=request)
            return httpx.Response(200, json=[_post(12345, active=True, live=True, internal=False)], request=request)

        real_client = httpx.Client

        def client_factory(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(handler), timeout=15.0)

        with patch.dict(os.environ, FAKE_ENV, clear=False):
            with patch.object(jobready_validator.httpx, "Client", client_factory):
                observations = validate_candidates([GREENHOUSE_ROW])
        self.assertEqual(len(observations), 1)


class GreenhouseAuthorityTests(unittest.TestCase):
    def test_greenhouse_default_authority(self) -> None:
        policy = default_authority_policy()
        self.assertTrue(policy.is_authoritative("greenhouse"))
        self.assertEqual(policy.authority_rank("greenhouse"), policy.authority_rank("linkedin"))

    def test_greenhouse_open_projection(self) -> None:
        from datetime import datetime, timezone

        from lifecycle_projection import LifecycleProjectionObservation

        items = [
            LifecycleProjectionObservation(
                evidence_ref="g1",
                provider="greenhouse",
                observation="confirmed_active",
                observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                reason="greenhouse_post_live",
                evidence_version="jobready-validator-v2",
                expected_identity="1",
                observed_identities=("1",),
            )
        ]
        result = project_lifecycle(items)
        self.assertEqual((result.decision, result.reason), (CONFIRMED_OPEN, REASON_CONFIRMED_OPEN))
