#!/usr/bin/env python3
"""Private-environment validation benchmark wrapper around audit_job_ready."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import audit_job_ready as audit  # noqa: E402
from benchmark_isolation import check_validation_benchmark_environment  # noqa: E402
from cost_calculator import estimate_queue_runtime, project_costs  # noqa: E402

ALLOWED_LIMITS = (25, 100, 200)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bounded read-only validation benchmark")
    parser.add_argument("--limit", type=int, required=True, choices=ALLOWED_LIMITS)
    parser.add_argument("--source", default=None, help="Optional source filter (naukri, indeed, ...)")
    parser.add_argument("--counts-only", action="store_true", help="Counts-only logging; omit per-row output")
    parser.add_argument("--with-cost-projection", action="store_true")
    parser.add_argument("--collection-seconds", type=float, default=0.0, help="Measured collection seconds for cost table")
    return parser.parse_args(argv)


def format_counts_line(summary: dict) -> str:
    return (
        "BENCH_VALIDATE pass "
        f"selected={summary['queued']} "
        f"checked={summary['checked']} "
        f"active={summary[audit.CONFIRMED_ACTIVE]} "
        f"expired={summary[audit.CONFIRMED_EXPIRED]} "
        f"inconclusive={summary[audit.INCONCLUSIVE]} "
        f"unsupported={summary[audit.UNSUPPORTED]} "
        f"elapsed_sec={summary['elapsed_sec']} "
        f"sec_per_job={summary.get('seconds_per_checked_job', 0)}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        check_validation_benchmark_environment()
    except ValueError:
        print("BENCH_VALIDATE fail reason=isolation_guard")
        return 3
    audit.load_env(audit.ENV)
    report_dir = (os.environ.get("BENCHMARK_LOCAL_REPORT_DIR") or os.environ.get("RUNNER_TEMP") or "").strip()
    summary, _report_path = audit.run_readonly_audit(
        limit=args.limit,
        source_filter=args.source,
        counts_only=True,
        report_dir=report_dir or None,
        write_repo_output=False,
    )
    print(format_counts_line(summary))
    for source, bucket in sorted((summary.get("results_by_source") or {}).items()):
        print(
            f"BENCH_VALIDATE_SOURCE source={source} "
            f"active={bucket.get(audit.CONFIRMED_ACTIVE, 0)} "
            f"expired={bucket.get(audit.CONFIRMED_EXPIRED, 0)} "
            f"inconclusive={bucket.get(audit.INCONCLUSIVE, 0)} "
            f"unsupported={bucket.get(audit.UNSUPPORTED, 0)}"
        )
    estimates = estimate_queue_runtime(summary["checked"], float(summary["elapsed_sec"]))
    print(f"BENCH_VALIDATE_ESTIMATE jobs=1000 seconds={estimates[1000]} jobs=5000 seconds={estimates[5000]}")
    if args.with_cost_projection and args.collection_seconds > 0:
        costs = project_costs(
            collection_seconds=args.collection_seconds,
            validation_seconds_by_limit={args.limit: float(summary["elapsed_sec"])},
        )
        out = Path(report_dir or ROOT / "output") / "jobready_cost_projection.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(costs, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
