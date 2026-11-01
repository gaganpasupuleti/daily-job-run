# JobReady: public-repository safety pilot

## What this pilot does

Adds a **manual-only** GitHub Actions workflow that runs the *existing* JobReady unit and integration suite against six temporary PostgreSQL databases in the GitHub runner. It does not run the daily collector, fetch fresh job listings, query Railway, read production jobs, publish or archive listings, or attach logs/artifacts. It intentionally cannot measure real scrape success or real database performance.

1. Add the three files in this bundle to the **same paths** in `gaganpasupuleti/daily-job-run`, preferably on a branch and pull request.
2. Go to **Actions → JobReady public pilot (disposable data only) → Run workflow**. It is manual only; there is no cron schedule.
3. Inspect the run's `PILOT_PASS`/`PILOT_FAIL` summary, total tests, passed, failed, errors, skipped, and duration. No credentials/URLs/job data should appear in logs. Do **not** upload the local test files or runtime reports as public artifacts.
4. Fix errors and rerun. Consider the public pilot technically passing only if the workflow passes twice on the default branch, produces nonzero passing tests, and its completed runs stay below your allocated runner-minute budget. Review skipped tests; skips due to unavailable DB indicate incomplete proof.
5. Switch the repository to **private** via repository Settings → General → Danger Zone → Change repository visibility, when ready. Note that turning private does not erase prior forks, exports, or cached copies.
6. Run a separate **read-only production preflight** with an authorized Railway-connected environment before scheduling actual collection. Do not set production database or scraper credentials in the public workflow, including as GitHub Secrets.
7. Recommended live architecture: deploy the collector as a short-lived Railway Cron service using **Railway private networking** and a narrowly privileged Postgres login. Review the DB schema/preflight, enable the writer deliberately, and keep `--sync-application` OFF until the publication gate is fixed. A GitHub-hosted runner cannot reach a Railway **private-only** database by default.
8. Confirm backups, limits, validation budgets, and staged changes before automated publication or archival. Preserve inactive job rows and application history.

## Local test

From the repository root (after installing its requirements), existing CI can run:

```shell
python -m unittest discover -s codequest_jobs/tests
```

The public pilot creates disposable Postgres automatically. It runs the same tests through `codequest_jobs/public_pilot.py` with a fail-closed connection guard and an allowlisted counts-only report.

## Critical architecture distinction

- **Public repo:** code and test-only pipeline may be public, with no production DB credentials or job-link exports.
- **Production data:** stay in Railway PostgreSQL, on a private network accessible only to appropriate Railway services.
- **Public web frontend:** published job URLs are inherently viewable by site visitors. Protect the unpublished catalog and collection evidence, not an impossible secrecy guarantee for public listings.
- **No auto-publish:** the current daily-run script's optional `--sync-application` uses `link_status='active'` rather than the application's explicit publication decisions, so do not turn it on.

## Expected results and limitations

`PILOT_PASS tests=<n> passed=<n> failed=0 errors=0 skipped=<n> seconds=<n>` confirms the synthetic test job passed. It **does not** confirm real job collection is working. Measure production scrape yield, supported-source coverage, fresh confirmed-open observations, and evidence-based withdrawal proposals in a later private read-only canary. No live run should auto-expire rows for 403, 429, CAPTCHA, or a missing scrape result.

## Access limitation encountered

The currently connected GitHub integration reports **pull=true, push=false** for `daily-job-run`; creating `chore/jobready-public-pilot-20261101` returned GitHub `403 Resource not accessible by integration`. Until the integration is granted repository write access, these files cannot be installed or Actions run on your behalf through that connection.
