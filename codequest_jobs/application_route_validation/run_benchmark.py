"""HTTP-only vs browser-assisted benchmark on the approved 10-job sample."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_route_validation.run_shadow_ten import DEFAULT_SAMPLE, load_rows  # noqa: E402
from application_route_validation.shadow_engine import validate_job_shadow  # noqa: E402

OBSERVATIONS = ROOT / "output" / "runs" / "20261021T053813Z_bf76f6e_27551f_observations.jsonl"


def _run(rows: list[dict], browser: bool) -> list[dict]:
    playwright = None
    browser_obj = None
    out: list[dict] = []
    if browser:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        browser_obj = playwright.chromium.launch(headless=True)
    for row in rows:
        page = browser_obj.new_context().new_page() if browser_obj else None
        result = validate_job_shadow(row, browser_page=page, enable_browser=browser, browser_timeout_ms=25000)
        if page is not None:
            page.context.close()
        out.append(
            {
                "job_id": result.job_id,
                "route_ready": result.route_ready,
                "application_route_outcome": result.application_route_outcome,
                "browser_attempted": result.browser_attempted,
                "elapsed_ms": result.elapsed_ms,
            }
        )
    if browser_obj:
        browser_obj.close()
    if playwright:
        playwright.stop()
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observations", type=Path, default=OBSERVATIONS)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "experiments")
    args = parser.parse_args()
    rows = load_rows(args.observations, DEFAULT_SAMPLE[: args.limit])
    run_id = str(uuid.uuid4())
    http_rows = _run(rows, browser=False)
    browser_rows = _run(rows, browser=True)
    durations = [r["elapsed_ms"] for r in browser_rows]
    incremental = sum(
        1
        for h, b in zip(http_rows, browser_rows)
        if h["route_ready"] == "unresolved" and b["route_ready"] != "unresolved"
    )
    report = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_size": len(rows),
        "label_coverage": 0,
        "open_precision": None,
        "closed_precision": None,
        "note": "Precision requires manual_labels.json; unresolved is not counted as correct.",
        "unresolved_rate_http": sum(1 for r in http_rows if r["route_ready"] == "unresolved") / len(http_rows),
        "unresolved_rate_browser": sum(1 for r in browser_rows if r["route_ready"] == "unresolved") / len(browser_rows),
        "browser_incremental_resolved": incremental,
        "duration_ms_p50": statistics.median(durations) if durations else 0,
        "duration_ms_p95": sorted(durations)[int(0.95 * (len(durations) - 1))] if durations else 0,
        "http": http_rows,
        "browser": browser_rows,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = args.out_dir / f"benchmark_{stamp}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
