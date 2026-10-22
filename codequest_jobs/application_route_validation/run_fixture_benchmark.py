"""Run fixture benchmark (local HTTP + optional Playwright). No catalog or DB access."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_route_validation.contracts import JobValidationTarget  # noqa: E402
from application_route_validation.engine import validate_application_route  # noqa: E402
from application_route_validation.fixture_catalog import FIXTURE_CASES, FIXTURES_DIR  # noqa: E402


def _serve_fixtures(port: int) -> ThreadingHTTPServer:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(FIXTURES_DIR), **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Application-route validation fixture benchmark")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--playwright", action="store_true", help="Use Playwright against local fixtures")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    server = _serve_fixtures(args.port)
    base = f"http://127.0.0.1:{args.port}"

    playwright = None
    browser = None
    page = None
    if args.playwright:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("playwright not installed; run: pip install playwright && playwright install chromium")
            server.shutdown()
            return 2
        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

    def navigate(p, url: str) -> None:
        p.goto(url, wait_until="domcontentloaded")

    rows: list[dict] = []
    correct = 0
    total_ms = 0
    for case in FIXTURE_CASES:
        url = f"{base}/{case.filename}"
        target = JobValidationTarget(
            expected_job_id=case.expected_job_id,
            source="fixture",
            listing_url=url,
            application_url=url,
        )
        result = validate_application_route(
            target,
            enable_playwright_fallback=args.playwright,
            playwright_page=page,
            navigate=navigate if args.playwright else None,
            playwright_timeout_ms=3000,
        )
        ok = result.decision == case.expected_decision
        correct += int(ok)
        total_ms += result.elapsed_ms
        rows.append(
            {
                "case": case.name,
                "expected": case.expected_decision,
                "actual": result.decision,
                "ok": ok,
                "reason": result.reason,
                "elapsed_ms": result.elapsed_ms,
                "notes": case.notes,
            }
        )

    if browser:
        browser.close()
    if playwright:
        playwright.stop()
    server.shutdown()

    n = len(FIXTURE_CASES)
    summary = {
        "fixture_cases": n,
        "playwright": args.playwright,
        "accuracy": correct / n if n else 0.0,
        "correct": correct,
        "mean_elapsed_ms": total_ms / n if n else 0,
        "false_open": sum(1 for r in rows if r["expected"] != "confirmed_open" and r["actual"] == "confirmed_open"),
        "false_closed": sum(1 for r in rows if r["expected"] != "confirmed_closed" and r["actual"] == "confirmed_closed"),
        "unresolved_rate": sum(1 for r in rows if r["actual"] == "unresolved") / n if n else 0,
        "rows": rows,
        "limitation": "Fixture-only benchmark; not representative of live board precision.",
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"cases={n} correct={correct} accuracy={summary['accuracy']:.2%} mean_ms={summary['mean_elapsed_ms']:.1f}")
        print(f"false_open={summary['false_open']} false_closed={summary['false_closed']} unresolved_rate={summary['unresolved_rate']:.2%}")
        for row in rows:
            mark = "OK" if row["ok"] else "MISS"
            print(f"  [{mark}] {row['case']}: expected={row['expected']} actual={row['actual']} ({row['reason']})")
    return 0 if correct == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
