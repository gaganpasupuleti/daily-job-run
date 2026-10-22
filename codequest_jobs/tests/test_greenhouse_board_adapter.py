from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_route_validation.adapters.greenhouse_board import interpret_greenhouse_board_html  # noqa: E402
from application_route_validation.contracts import CONFIRMED_CLOSED, CONFIRMED_OPEN, UNRESOLVED  # noqa: E402

FIX = ROOT / "application_route_validation" / "fixtures" / "greenhouse"
APPLY = "https://job-boards.greenhouse.io/acme/jobs/5447368008"
FINAL = APPLY


class GreenhouseBoardAdapterTests(unittest.TestCase):
    def test_open_post(self) -> None:
        html = (FIX / "open_post.html").read_text(encoding="utf-8")
        obs = interpret_greenhouse_board_html(apply_url=APPLY, final_url=FINAL, html=html, http_status=200)
        self.assertEqual(obs.application_route.status, CONFIRMED_OPEN)

    def test_closed_post(self) -> None:
        html = (FIX / "closed_post.html").read_text(encoding="utf-8")
        obs = interpret_greenhouse_board_html(apply_url=APPLY, final_url=FINAL, html=html, http_status=200)
        self.assertEqual(obs.application_route.status, CONFIRMED_CLOSED)

    def test_recommended_noise_ignored(self) -> None:
        html = (FIX / "recommended_closed_noise.html").read_text(encoding="utf-8")
        obs = interpret_greenhouse_board_html(apply_url=APPLY, final_url=FINAL, html=html, http_status=200)
        self.assertEqual(obs.application_route.status, CONFIRMED_OPEN)

    def test_redirect_off_board(self) -> None:
        obs = interpret_greenhouse_board_html(
            apply_url=APPLY,
            final_url="https://example.com/careers",
            html="<html></html>",
            http_status=200,
        )
        self.assertEqual(obs.application_route.status, UNRESOLVED)
        self.assertEqual(obs.reason, "redirect_off_greenhouse")


if __name__ == "__main__":
    unittest.main()
