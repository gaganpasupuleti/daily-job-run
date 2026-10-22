"""Expected outcomes for local fixture pages (prototype benchmark)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True, slots=True)
class FixtureCase:
    name: str
    filename: str
    expected_job_id: str
    expected_decision: str
    notes: str = ""


FIXTURE_CASES: tuple[FixtureCase, ...] = (
    FixtureCase("open_matching", "open_matching.html", "job-1001", "confirmed_open"),
    FixtureCase("closed_matching", "closed_matching.html", "job-1002", "confirmed_closed"),
    FixtureCase("delayed_render", "delayed_render.html", "job-1003", "confirmed_open", "JS delay"),
    FixtureCase("generic_apply_noise", "generic_apply_noise.html", "job-1004", "unresolved"),
    FixtureCase("redirect_same_req", "redirect_same_req.html", "job-1005", "confirmed_open"),
    FixtureCase("redirect_different_req", "redirect_different_req.html", "job-1006", "unresolved"),
    FixtureCase("login_wall", "login_wall.html", "job-1007", "unresolved"),
    FixtureCase("captcha_wall", "captcha_wall.html", "job-1008", "unresolved"),
    FixtureCase("empty_page", "empty_page.html", "job-1009", "unresolved"),
    FixtureCase("contradictory", "contradictory.html", "job-1010", "unresolved"),
    FixtureCase(
        "listing_closed_route_open",
        "listing_closed_route_open.html",
        "job-1011",
        "confirmed_open",
        "Board closed; employer route open",
    ),
)
