"""Prototype application-route validation: pure + optional Playwright fixtures."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_route_validation.adapters.playwright_fixture import parse_fixture_dom  # noqa: E402
from application_route_validation.contracts import CONFIRMED_CLOSED, CONFIRMED_OPEN, UNRESOLVED  # noqa: E402
from application_route_validation.decision import decide_application_route  # noqa: E402
from application_route_validation.contracts import RouteDimension  # noqa: E402
from application_route_validation.engine import validate_application_route  # noqa: E402
from application_route_validation.contracts import JobValidationTarget  # noqa: E402
from application_route_validation.adapters.http_board import observe_listing_http  # noqa: E402
from jobready_validator import interpret_linkedin  # noqa: E402
from linkedin_checker import GUEST_URL  # noqa: E402

JOB_ID = "4475277813"
APPLY_BUTTON = (
    '<button class="sign-up-modal__outlet top-card-layout__cta top-card-layout__cta--primary btn-md btn-primary" '
    'data-modal="job-details-topcard-apply-modal"> Apply </button>'
)
CLOSED_FIGURE = (
    '<figure class="closed-job closed-job__flavor topcard__flavor-row">'
    '<figcaption class="closed-job__flavor--closed">No longer accepting applications</figcaption></figure>'
)


def _linkedin_body(apply: str = APPLY_BUTTON, closed: bool = False) -> str:
    return (
        '<section class="top-card-layout container-lined">'
        f"{CLOSED_FIGURE if closed else ''}"
        f'<div class="top-card-layout__cta-container">{apply}</div></section>'
        f'<code id="decoratedJobPostingId" style="display: none"><!--"{JOB_ID}"--></code>'
    )


class DecisionTests(unittest.TestCase):
    def test_listing_closed_route_open_is_confirmed_open(self) -> None:
        listing = RouteDimension(CONFIRMED_CLOSED, "board", True)
        requisition = RouteDimension(CONFIRMED_OPEN, "ats", True)
        application = RouteDimension(CONFIRMED_OPEN, "apply", True)
        decision, _ = decide_application_route(listing=listing, requisition=requisition, application_route=application)
        self.assertEqual(decision, CONFIRMED_OPEN)

    def test_identity_mismatch_unresolved(self) -> None:
        application = RouteDimension(CONFIRMED_OPEN, "apply", False)
        decision, reason = decide_application_route(
            listing=RouteDimension(UNRESOLVED, "x", False),
            requisition=RouteDimension(UNRESOLVED, "y", None),
            application_route=application,
        )
        self.assertEqual(decision, UNRESOLVED)
        self.assertEqual(reason, "identity_mismatch")


class HttpReuseTests(unittest.TestCase):
    def test_linkedin_http_adapter_matches_interpret(self) -> None:
        body = _linkedin_body()
        guest = GUEST_URL.format(job_id=JOB_ID)
        obs, reason = interpret_linkedin(status_code=200, body=body, expected_job_id=JOB_ID, final_url=guest)
        listing_obs = observe_listing_http(
            source="linkedin",
            expected_job_id=JOB_ID,
            status_code=200,
            body=body,
            payload=None,
            final_url=guest,
        )
        self.assertEqual(listing_obs.listing.status, CONFIRMED_OPEN if obs == "confirmed_active" else obs)
        self.assertEqual(listing_obs.reason, reason)


class FixtureDomTests(unittest.TestCase):
    def test_login_blocker(self) -> None:
        listing, req, app, reason = parse_fixture_dom({"data-blocker": "login"})
        self.assertEqual(listing.status, UNRESOLVED)
        self.assertIn("login", reason)

    def test_listing_closed_route_open_attrs(self) -> None:
        attrs = {
            "data-expected-job-id": "job-1011",
            "data-observed-job-id": "job-1011",
            "data-listing-status": "closed",
            "data-requisition-status": "open",
            "data-application-route": "open",
        }
        listing, requisition, application, _ = parse_fixture_dom(attrs)
        decision, _ = decide_application_route(listing=listing, requisition=requisition, application_route=application)
        self.assertEqual(decision, CONFIRMED_OPEN)
        self.assertEqual(application.status, CONFIRMED_OPEN)


class PlaywrightFixtureIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("playwright not installed")

    def test_delayed_render_fixture(self) -> None:
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from application_route_validation.fixture_catalog import FIXTURES_DIR

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(FIXTURES_DIR), **kwargs)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}/delayed_render.html"

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            target = JobValidationTarget("job-1003", "naukri", url, url)
            result = validate_application_route(
                target,
                http_listing={
                    "status_code": 200,
                    "payload": {},
                    "final_url": "https://www.naukri.com/jobapi/v1/job/job-1003",
                },
                enable_playwright_fallback=True,
                playwright_page=page,
                navigate=lambda pg, u: pg.goto(u, wait_until="domcontentloaded"),
                playwright_timeout_ms=3000,
            )
            browser.close()
        server.shutdown()
        self.assertEqual(result.decision, CONFIRMED_OPEN)


if __name__ == "__main__":
    unittest.main()
