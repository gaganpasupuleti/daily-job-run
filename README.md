# Daily job run

daily-job-run collects and maintains the source job catalog used by JobReady. JobReady is its only production data destination.

The pipeline scrapes configured boards, normalizes and dedupes the rows, classifies them in memory, and upserts `validated_jobs` in the JobReady catalog. A later observation updates `synced_at`. `scraped_created_at` stays at the first time that source identity was stored. `updated_at` moves only when the catalog content or link status changes. Confirmed closed jobs stay in the catalog with `link_status = expired`. A miss in today's scrape is not expiration.

## Daily command

```bash
python codequest_jobs/run_jobready_daily.py
```

That command scrapes, upserts the catalog, then validates a rotating slice of `validated_jobs`. The default validation budget is 200 rows. Up to half of that budget is reserved for new jobs and re-observed jobs that have never received a conclusive check. The rest of the budget walks the oldest `link_checked_at`, so a normal day keeps moving through the catalog. `--full-audit` checks every row. `--validation-limit` changes the budget.

A job found by search starts active. Seeing it in search again does not change `link_status`. An expired row stays expired until the validator returns `confirmed_active`. Only `confirmed_expired` marks a row expired. Link checks update `link_checked_at` and do not move `synced_at`.

A conclusive check needs evidence for the same job. The response must come from that job's provider endpoint over HTTPS on the provider's own host and default port, without a redirect elsewhere. It must carry the same job id and state its status:

- LinkedIn reads only the visible content of the page's single top-card element. Hidden, template, and script content does not count. A job is `confirmed_active` only when the top card has a working Apply action: LinkedIn's apply-modal button, or an Apply link to a real http or https destination. It is `confirmed_expired` when the top card says the job is closed or the guest endpoint returns 404 or 410. These are inconclusive:
  - a disabled, missing, or inert Apply action, such as a missing href, `#`, or `javascript:`;
  - a captcha or sign-in wall;
  - a "not found" page;
  - closed text outside the top card;
  - a page where the top card cannot be identified.
- Naukri needs an explicit status field for the same job id. A title alone is inconclusive.
- Indeed needs a true or false `expired` flag for the requested job key. A missing, null, or text value is inconclusive.
- Google, Foundit, and other sources are inconclusive.

Inconclusive keeps the previous `link_status`. The daily report counts each observation and reason under `validation_reasons`.

Before the first run, and after a schema review:

```bash
python codequest_jobs/run_jobready_daily.py --preflight
python codequest_jobs/run_jobready_daily.py --apply-schema
```

`--preflight` is read-only. It checks that the catalog is reachable, `validated_jobs` exists, required columns and indexes are present, and source identities are not duplicated. It prints `host`, `port`, and database name, not a connection string. `--apply-schema` applies idempotent catalog DDL and refuses when duplicate identities exist. A normal daily run refuses if that schema is missing. It does not create the table itself.

Experience buckets stored for JobReady are `Internship`, `Fresher`, `Entry (1-2 yrs)`, `Experienced`, and `Unknown / Unclassified`.

Required configuration: `JOB_READY_DATABASE_URL`.

Set `INDEED_API_KEY` when Indeed expiry checks should run. Leave it empty to skip those checks.

Application membership is separate. The command prints `would_archive` and `would_reactivate` before any application write. Pass `--sync-application --confirm-target host:port/database` only when `JOB_READY_APP_DATABASE_URL` should be updated. The confirmation is the host, port, and database name. It is not a connection string. Membership includes catalog rows whose `link_status` is `active`. A write is refused when an archive or reactivation is at least 100 jobs and at least 10 percent of the integration-owned active application jobs. There is no bypass flag. A source-wide scrape failure also refuses the membership write and does not expire unrelated catalog rows.

One run holds a PostgreSQL advisory lock on the catalog. A second run exits while that lock is held.

Each run writes `codequest_jobs/output/jobready_daily_YYYYMMDD_HHMMSS.json`.

## Sources

Naukri, Indeed, LinkedIn, Google, and Foundit, for the role families in `naukri_jobs_scraper/families.json`.

## Legacy commands

The modules in [LEGACY.md](LEGACY.md) still talk to CodeQuest or the old `naukri_scraper` database. Do not use them for the daily catalog. `link_validator_service/run_scheduled.py` only delegates to the command above.

`codequest_jobs/prune_job_ready_now.py` is retired. It exits with `RETIRED_COMMAND prune_job_ready_now` and touches no database. CodeQuest does not decide which catalog jobs survive.
