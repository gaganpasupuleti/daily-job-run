"""Project monthly runtime and cost from measured benchmark seconds.

Pricing values are configurable assumptions; billing rates are not verified here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PricingAssumptions:
    github_public_minutes_included: int = 2000
    github_private_minutes_included: int = 3000
    github_extra_usd_per_minute: float = 0.008
    railway_vcpu_usd_per_hour: float = 0.000463
    railway_memory_gb_usd_per_hour: float = 0.000231
    railway_vcpu_count: float = 1.0
    railway_memory_gb: float = 1.0


def monthly_minutes(runs_per_day: int, seconds_per_run: float) -> float:
    return runs_per_day * 30 * (seconds_per_run / 60.0)


def github_cost_usd(minutes: float, *, private: bool, pricing: PricingAssumptions) -> dict:
    included = pricing.github_private_minutes_included if private else pricing.github_public_minutes_included
    billable = max(0.0, minutes - included)
    return {
        "minutes_used": round(minutes, 1),
        "included_minutes_assumption": included,
        "billable_minutes_assumption": round(billable, 1),
        "estimated_extra_usd_assumption": round(billable * pricing.github_extra_usd_per_minute, 2),
        "rates_verified": False,
    }


def railway_cost_usd(minutes: float, pricing: PricingAssumptions) -> dict:
    """`minutes` is already total monthly runner minutes (not per-day)."""
    hours = minutes / 60.0
    compute = pricing.railway_vcpu_count * pricing.railway_vcpu_usd_per_hour
    memory = pricing.railway_memory_gb * pricing.railway_memory_gb_usd_per_hour
    hourly = compute + memory
    return {
        "minutes_used": round(minutes, 1),
        "estimated_usd_assumption": round(hours * hourly, 2) if minutes else 0.0,
        "hourly_rate_usd_assumption": round(hourly, 6),
        "rates_verified": False,
    }


def github_actions_runtime_estimate(seconds_per_run: float, *, runs_per_day: int = 1) -> dict:
    daily_seconds = round(seconds_per_run * runs_per_day, 1)
    monthly_minutes_total = monthly_minutes(runs_per_day, seconds_per_run)
    return {
        "seconds_per_run_measured": round(seconds_per_run, 2),
        "runs_per_day": runs_per_day,
        "estimated_daily_runtime_seconds": daily_seconds,
        "estimated_monthly_runtime_minutes": round(monthly_minutes_total, 1),
        "estimated_monthly_runtime_hours": round(monthly_minutes_total / 60.0, 2),
    }


def project_costs(
    *,
    collection_seconds: float,
    validation_seconds_by_limit: dict[int, float],
    pricing: PricingAssumptions | None = None,
) -> dict:
    pricing = pricing or PricingAssumptions()
    collection_runs = {1: collection_seconds, 2: collection_seconds, 4: collection_seconds}
    out: dict = {"measured": {"collection_seconds": collection_seconds, "validation_seconds_by_limit": validation_seconds_by_limit}, "assumptions": pricing.__dict__, "collection": {}, "validation": {}}
    for runs_per_day, seconds in ((1, collection_seconds), (2, collection_seconds), (4, collection_seconds)):
        minutes = monthly_minutes(runs_per_day, seconds)
        out["collection"][f"{runs_per_day}x_daily"] = {
            "github_public": github_cost_usd(minutes, private=False, pricing=pricing),
            "github_private": github_cost_usd(minutes, private=True, pricing=pricing),
            "railway_cron": railway_cost_usd(minutes, pricing),
        }
    for limit, seconds in sorted(validation_seconds_by_limit.items()):
        minutes = monthly_minutes(1, seconds)
        out["validation"][f"limit_{limit}"] = {
            "github_public": github_cost_usd(minutes, private=False, pricing=pricing),
            "github_private": github_cost_usd(minutes, private=True, pricing=pricing),
            "railway_cron": railway_cost_usd(minutes, pricing),
        }
    return out


def estimate_queue_runtime(checked: int, elapsed_seconds: float, targets: tuple[int, ...] = (1000, 5000)) -> dict[int, float]:
    if checked <= 0 or elapsed_seconds <= 0:
        return {target: 0.0 for target in targets}
    per_job = elapsed_seconds / checked
    return {target: round(per_job * target, 1) for target in targets}
