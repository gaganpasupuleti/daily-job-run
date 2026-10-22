"""Shadow-run application-route validation on a small job sample. JSONL only; no catalog writes."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application_route_validation.shadow_engine import validate_job_shadow  # noqa: E402

DEFAULT_SAMPLE = (
    "CQJ-20261020-1020",
    "CQJ-20261020-1021",
    "CQJ-20261020-1022",
    "CQJ-20261020-14022",
    "CQJ-20261020-14032",
    "CQJ-20261020-16824",
    "CQJ-20261020-2488",
    "CQJ-20261020-10215",
    "CQJ-20261020-10205",
    "CQJ-20261020-10208",
)

OBSERVATIONS = ROOT / "output" / "runs" / "20261021T053813Z_bf76f6e_27551f_observations.jsonl"


def load_rows(path: Path, job_ids: tuple[str, ...]) -> list[dict]:
    wanted = set(job_ids)
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            if item["job_id"] in wanted:
                rows.append(
                    {
                        "job_id": item["job_id"],
                        "source": item["source"],
                        "job_url": item["job_url"],
                        "apply_url": item.get("apply_url") or item["job_url"],
                        "link_status": item.get("previous_link_status", "unverified"),
                    }
                )
    missing = wanted - {r["job_id"] for r in rows}
    if missing:
        raise SystemExit(f"Missing job_ids in {path}: {sorted(missing)}")
    order = {jid: i for i, jid in enumerate(job_ids)}
    rows.sort(key=lambda r: order[r["job_id"]])
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observations", type=Path, default=OBSERVATIONS)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent / "experiments")
    args = parser.parse_args()

    job_ids = DEFAULT_SAMPLE[: args.limit]
    rows = load_rows(args.observations, job_ids)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jsonl_path = args.out_dir / f"shadow_route_{stamp}.jsonl"
    summary_path = args.out_dir / f"shadow_route_{stamp}_summary.json"

    playwright = None
    browser = None
    page = None
    if not args.no_browser:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
    summaries = []
    with jsonl_path.open("w", encoding="utf-8") as out:
        for row in rows:
            page = None
            if browser is not None:
                page = browser.new_context().new_page()
            result = validate_job_shadow(
                row,
                browser_page=page,
                enable_browser=not args.no_browser,
                browser_timeout_ms=25000,
            )
            if page is not None:
                page.context.close()
            for rec in result.records:
                out.write(rec.to_json() + "\n")
            summaries.append(
                {
                    "job_id": result.job_id,
                    "source": result.source,
                    "listing_http_reason": result.listing_http_reason,
                    "listing_outcome": result.listing_outcome,
                    "application_route_outcome": result.application_route_outcome,
                    "route_ready": result.route_ready,
                    "route_ready_reason": result.route_ready_reason,
                    "browser_attempted": result.browser_attempted,
                    "browser_skip_reason": result.browser_skip_reason,
                    "elapsed_ms": result.elapsed_ms,
                    "apply_url": row["apply_url"],
                }
            )

    if browser:
        browser.close()
    if playwright:
        playwright.stop()

    conclusive = sum(1 for s in summaries if s["route_ready"] != "unresolved")
    browser_ran = sum(1 for s in summaries if s["browser_attempted"])
    browser_yield = sum(
        1
        for s in summaries
        if s["browser_attempted"] and s["application_route_outcome"] != "unresolved"
    )
    report = {
        "sample_size": len(summaries),
        "conclusive_route_ready": conclusive,
        "unresolved_rate": 1 - (conclusive / len(summaries)) if summaries else 0,
        "browser_attempts": browser_ran,
        "browser_incremental_route_signal": browser_yield,
        "jsonl": str(jsonl_path),
        "jobs": summaries,
        "note": "No manual labels in this run; metrics describe coverage only.",
    }
    summary_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
