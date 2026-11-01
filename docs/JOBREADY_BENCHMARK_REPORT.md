# JobReady live benchmark report

Generated during implementation of `feat/jobready-live-benchmark`. Measured metrics are from **bounded local runs** on a developer workstation (Windows, Python 3.12). GitHub Actions runs should be triggered manually from `.github/workflows/jobready-live-benchmark.yml` after merge; billing numbers below use **assumptions** unless noted.

## Scope and safety

| Check | Result |
|-------|--------|
| Production catalog/database writes | **Not performed** |
| `--sync-application` / publication | **Not invoked** |
| Job URLs in benchmark stdout | **Redacted / suppressed** (HTTP request logging raised to WARNING in benchmark runner) |
| Live validation against production catalog | **Not run** (no verified read-only `JOBREADY_AUDIT_READONLY_URL` in this environment) |
| Public workflow connects to Postgres/Railway | **No** (see workflow static tests) |

## Real collection benchmark (Naukri + Indeed)

Defaults: `sources=naukri,indeed`, `max_pages=1`, `results_wanted=5`, single family `python-dev`.

| Run | Raw collected | Unique after dedupe | Naukri | Indeed | Errors | Collection (s) | Total workflow (s) | Peak memory (MB) |
|-----|---------------|---------------------|--------|--------|--------|------------------|--------------------|------------------|
| 1 | 41 | 41 | 31 | 10 | 0 | 13.66 | 13.66 | 45.35 |
| 2 | 41 | 41 | 31 | 10 | 0 | 9.81 | 9.81 | 45.28 |

**Average time per collected job (raw):** ~0.27s (run 1) and ~0.24s (run 2).

Detailed JSON reports were written under the local temp directory (`BENCHMARK_LOCAL_REPORT_DIR` / `RUNNER_TEMP`), which is gitignored—not committed.

## Validation benchmark (existing catalog rows)

**Status: untested (live).**

Requirements for a safe run (private environment only):

1. Configure `JOBREADY_AUDIT_READONLY_URL` (or existing `JOB_READY_DATABASE_URL`) with a **read-only** Postgres role.
2. Do **not** set `GITHUB_ACTIONS=true` (the wrapper refuses hosted runners).
3. Run, for example:

```shell
python codequest_jobs/validation_benchmark.py --limit 25
python codequest_jobs/validation_benchmark.py --limit 100
python codequest_jobs/validation_benchmark.py --limit 200
```

Expected metrics: selected/checked/active/expired/inconclusive/unsupported counts, per-source breakdown, elapsed time, sec/job, and projected seconds for 1,000 and 5,000 jobs (linear extrapolation from measured checked count).

## Cost projections (assumptions — rates not verified)

Measured collection duration used for math: **12s per run** (midpoint of the two local runs). Validation seconds use **placeholders** until live read-only runs exist.

| Scenario | Monthly minutes (approx.) | GitHub public extra USD* | GitHub private extra USD* | Railway cron USD* |
|----------|---------------------------|--------------------------|---------------------------|-------------------|
| Collection 1×/day | 6 | $0 (within 2,000 min allowance) | $0 (within 3,000 min allowance) | ~$0.01 |
| Collection 2×/day | 12 | $0 | $0 | ~$0.02 |
| Collection 4×/day | 24 | $0 | $0 | ~$0.03 |
| Validation limit 25/day | TBD | TBD | TBD | TBD |
| Validation limit 100/day | TBD | TBD | TBD | TBD |
| Validation limit 200/day | TBD | TBD | TBD | TBD |

\*Assumes GitHub-hosted Linux minutes at **$0.008/min** beyond included free minutes (2,000 public / 3,000 private per assumptions in `cost_calculator.py`). Railway row assumes **1 vCPU + 1 GB** at placeholder rates in `PricingAssumptions` — **not verified** against current Railway pricing.

Use `python -c` with `cost_calculator.project_costs(...)` after you have measured validation seconds to refresh this table.

## Recommendation

| Workload | Suggested platform | Rationale |
|----------|-------------------|-----------|
| **Daily fresh collection** | **Railway Cron** (private network to catalog writer) | Keeps scraper secrets and job payloads off public GitHub logs; long-running fetches fit a container better than burning Actions minutes at scale. |
| **Repeated link validation** | **Railway Cron or private runner** with read-only DB URL | Needs catalog access GitHub public runners should not have; validation benchmark explicitly blocks `GITHUB_ACTIONS`. |
| **Regression / synthetic proof** | **GitHub Actions public pilot** (`jobready-public-pilot.yml`) | Already proves 696 tests on disposable Postgres without production credentials. |
| **Bounded live scrape metrics** | **Manual `jobready-live-benchmark.yml`** | Useful for yield tuning with tiny caps; not for full catalog ingestion. |

**Cheapest reliable production pattern:** private Railway cron for collection + validation against private Postgres, public repo limited to manual synthetic pilot and optional tiny live collection benchmarks—**never** store production DB URLs in GitHub Secrets on a public repo.

## Security notes

- Collection isolation rejects catalog/production DB env vars before scraping.
- Validation wrapper refuses GitHub-hosted runners.
- Audit extensions cap `--limit` at **200** and support `--counts-only` summaries without per-row stdout.
- Inconclusive probe results remain inconclusive (never promoted to confirmed expired) via existing `audit_row` semantics.

## Outstanding risks

- Live scrapers can still fail or rate-limit unpredictably; benchmark defaults are small but not zero network risk.
- GitHub Actions logs depend on dependency log levels; benchmark runner elevates common HTTP loggers to WARNING—review each new dependency.
- Cost table is illustrative until validation timings and current vendor pricing are measured.
