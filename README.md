# Daily job run

daily-job-run collects and maintains the source job catalog used by JobReady. JobReady is its only production data destination.

The pipeline scrapes configured boards, normalizes and dedupes the rows, classifies them in memory, and upserts `validated_jobs` in the JobReady catalog. A later observation updates `synced_at`. `scraped_created_at` stays at the first time that source identity was stored. `updated_at` moves only when the catalog content or link status changes. Confirmed closed jobs stay in the catalog with `link_status = expired`. A miss in today's scrape is not expiration.

## Daily command

```bash
python codequest_jobs/run_jobready_daily.py
```

Required configuration: `JOB_READY_DATABASE_URL`.

Set `INDEED_API_KEY` when Indeed expiry checks should run. Leave it empty to skip those checks.

Application membership is separate. Pass `--sync-application --confirm-target host:port/database` only when `JOB_READY_APP_DATABASE_URL` should be updated. The confirmation is the host, port, and database name. It is not a connection string. Membership includes catalog rows whose `link_status` is `active`.

One run holds a PostgreSQL advisory lock on the catalog. A second run exits while that lock is held.

Each run writes `codequest_jobs/output/jobready_daily_YYYYMMDD_HHMMSS.json`.

## Sources

Naukri, Indeed, LinkedIn, Google, and Foundit, for the role families in `naukri_jobs_scraper/families.json`.

## Legacy commands

The modules in [LEGACY.md](LEGACY.md) still talk to CodeQuest or the old `naukri_scraper` database. Do not use them for the daily catalog. `link_validator_service/run_scheduled.py` only delegates to the command above.
