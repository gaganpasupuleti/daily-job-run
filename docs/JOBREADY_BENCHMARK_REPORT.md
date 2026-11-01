# JobReady full-scale collection benchmark (measured)

**Date:** 2026-11-01 (UTC)  
**Branch:** `feat/jobready-live-benchmark` (PR #28 not merged)  
**Runner:** Local Windows workstation, Python 3.12 — **not** GitHub Actions  
**Isolation:** No production DB env vars loaded; no catalog writes, publication, or `--sync-application`

## Parameters (unchanged load)

| Parameter | Value |
|-----------|--------|
| Families | All **19** from `naukri_jobs_scraper/families.json` |
| Sources | `naukri,indeed,linkedin,google,foundit` |
| `max_pages` | 1 |
| `results_wanted` | 5 |
| `hours_old` | 72 |
| Inter-family pause | 2s |

Raw job payloads: `benchmark_local_reports/full_scale_jobs_*.json` (gitignored).  
Sanitized summary JSON: `benchmark_local_reports/full_scale_summary_20261101_141255.json`.

## Measured collection results (this run only)

| Metric | Value |
|--------|--------|
| Families completed | **19 / 19** |
| Stopped early | **No** |
| Raw jobs collected | **716** |
| Unique after global dedupe | **475** |
| Collection CPU time | **237.8 s** |
| Dedupe | **<0.1 s** |
| Total benchmark runtime | **273.8 s** (~4.6 min) |
| Peak memory (approx.) | See summary JSON |

### Per-source (status distinguishes zero yield vs failed searches)

| Source | Status | Searches attempted | Search failures | Unique jobs |
|--------|--------|-------------------:|----------------:|------------:|
| **naukri** | ok | 39 | 0 | **272** |
| **indeed** | ok | 39 | 0 | **91** |
| **linkedin** | ok | 39 | 0 | **112** |
| **google** | **zero_results** | 39 | 0 | **0** |
| **foundit** | **zero_results** | 39 | 0 | **0** |

- **Failed source** (all searches failed): none in this run.
- **Zero results:** Google and Foundit returned no normalized rows for every family keyword pass (distinct from hard failure).
- **Rate limiting:** JobSpy logged multiple `429 (blocked: use a proxy)` lines to stderr during the run; LinkedIn/Indeed still returned jobs. Early-stop did not trigger because those 429 messages were not always propagated into collector `errors` (limitation noted below).

## GitHub Actions runtime estimate (from this measured run)

Uses `github_actions_runtime_estimate()` on **273.8 s** total workflow time:

| Schedule | Est. daily runtime | Est. monthly minutes |
|----------|-------------------:|---------------------:|
| 1×/day | **273.8 s** | **~136.9 min** |
| 2×/day | **~547.6 s** | **~273.8 min** |

**Cost assumptions** (`cost_calculator.py`, rates **not verified**):

| Scenario | GitHub public extra USD/mo | GitHub private extra USD/mo |
|----------|---------------------------:|----------------------------:|
| 1× daily full-scale on Actions | **$0** (under assumed 2,000 min allowance) | **$0** (under assumed 3,000 min) |
| 2× daily | **$0** | **$0** |

Railway placeholder for 136.9 min/mo: **~$0.02** (see `PricingAssumptions`).

Do **not** compare to the earlier **41-job / 1-family** sample — that was a different scope.

## Validation benchmark (existing catalog) — **not executed**

No verified read-only account was available (`JOBREADY_AUDIT_READONLY_URL` / `JOB_READY_DATABASE_URL` unset in this environment).

When configured in a **private** environment only:

```shell
python codequest_jobs/validation_benchmark.py --limit 25
python codequest_jobs/validation_benchmark.py --limit 100
python codequest_jobs/validation_benchmark.py --limit 200
```

Expected outputs: `BENCH_VALIDATE` counts-only lines; no job URLs in stdout; no catalog writes.

## Fixes applied before trusting reports

1. **Source completion:** `sources_completed` now reflects per-source health (`ok` / `partial_errors` / `zero_results` / `failed`), not merely the requested source list.
2. **Railway cost:** Removed erroneous extra `× 30` on monthly minutes in `railway_cost_usd()`.
3. **GitHub cost lines:** `github_cost_usd(..., pricing=PricingAssumptions())` wired in `build_report()`.

## Limitations and errors

- **429 / proxy blocks** appear in dependency logs; benchmark `source_error_count` may be **0** while stderr shows JobSpy errors — improve by routing JobSpy failures into `errors` in a follow-up.
- **Foundit / Google** zero yield may reflect source availability, query mismatch, or blocking — not distinguished beyond `zero_results` status.
- **Local IP** vs **GitHub Actions IP** will change yield and block rates; re-measure on the target runner before scheduling.
- **Validation timings** and production catalog counts remain **untested** here.

## Security

- No Railway/production credentials loaded for collection.
- Detailed job JSON stays under `benchmark_local_reports/` (gitignored).
- Public logs should use `quiet_public_logs()` + `BENCH_*` lines only (stderr may still show dependency ERROR lines until JobSpy logging is fully suppressed).
