# Application-route validation — completion candidate

## Baseline (Phase 1)

| Item | Value |
|------|--------|
| `origin/main` | `26998d4f4b4b792d8e50d60536facb54595e9757` |
| Local HEAD (pre-branch) | `bf76f6ea4681f863352945468bcbf5eb3ab91014` on `fix/jobready-json-acquisition-audit` |
| Prototype location | `codequest_jobs/application_route_validation/` (untracked on main until branch merge) |
| Shadow isolation | `tests/test_shadow_isolation.py` — no `apply_link_observations` / `jobready_catalog` in shadow package |

Reproduce main-only test failure (reported): checkout `origin/main` and run `python -m unittest codequest_jobs.tests.test_lifecycle_projection` — one conflict-ordering test may fail on main; not introduced by route package.

## Architecture

- **Lifecycle (existing):** `jobready_validator` → `apply_link_observations()` → catalog + `jobready_link_observations`.
- **Route (new):** `shadow_engine` / adapters → `RouteObservation` → `JsonlRouteEvidenceStore` or `jobready_route_observations` (SQL, not wired to daily run).
- **Projection:** `route_projection.project_route()` per subject; `route_freshness.evaluate_route_freshness()` for TTL hold.
- **Eligibility:** `eligibility_export_v2.py` composes D17 v1 + route readiness (opt-in `jobready-eligibility-v2`).

## Adapter matrix

| Adapter | Status | Notes |
|---------|--------|-------|
| HTTP lifecycle probes | Verified (existing) | Listing subject only |
| `greenhouse_board` v2 | Experimental | Public boards; destination self-match + apply_url link |
| `naukri_listing_page` | Experimental | No conclusive signal in 10-job sample |
| Workday / Cisco / LinkedIn browser | Unsupported | Allowlist / contract |
| Greenhouse Harvest OAuth | Verified (existing) | Requisition subject; separate credentials |

## Commands

```bash
cd codequest_jobs
python -m unittest tests.test_greenhouse_board_adapter tests.test_route_projection_store tests.test_shadow_isolation tests.test_route_policy tests.test_application_route_validation -v
python application_route_validation/run_shadow_ten.py --limit 10
python application_route_validation/run_benchmark.py --limit 10
```

## Activation

- **Default:** shadow JSONL only; no schema apply on production.
- **DB:** `application_route_validation/route_evidence_schema.sql` on disposable DB only.
- **Rollback:** do not apply SQL; stop running shadow scripts; v1 eligibility unchanged.

## Manual labels

Fill `application_route_validation/experiments/manual_labels.template.json` — live precision **unmeasured** until completed.

## Recommendation

**Continue shadow mode.** Greenhouse board adapter shows incremental route signal on 4/10 jobs; independent human labels and Workday/Cisco adapters required before activation review.
