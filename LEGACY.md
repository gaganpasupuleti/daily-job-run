# Legacy modules

The daily command is `python codequest_jobs/run_jobready_daily.py`. These files remain so older audits can still be read. They are not part of that command.

## Retired commands

- `codequest_jobs/prune_job_ready_now.py` is RETIRED. It used to delete every JobReady catalog row that was not active in CodeQuest `scraped_jobs`, then reconcile application membership. Every invocation now prints `RETIRED_COMMAND prune_job_ready_now` and exits with code 2. It reads no database variable and opens no connection. No flag re-enables it. Do not use it for JobReady catalog maintenance.
- `codequest_jobs/push_validated_to_job_ready.py` is RETIRED. It used to copy active rows from CodeQuest `scraped_jobs` into `validated_jobs` and overwrite `link_status` with CodeQuest's value, so it could reactivate an expired row without a validator observation, a `jobready_link_observations` row, or a newer `last_confirmed_open_at`. Every invocation now prints `RETIRED_COMMAND push_validated_to_job_ready` and exits with code 2. It reads no database variable, opens no connection, writes no run log, and runs no schema SQL. No flag re-enables it, including the old `--max-age-days`, `--prune`, and `--confirm-target`. The frozen copy `link_validator_service/push_validated_to_job_ready.py` delegates to it and is retired the same way.

## Retired options

- `codequest_jobs/validate_links.py` `--sync-job-ready-every` and `--confirm-target` are RETIRED. They used to set `validated_jobs.link_status` to `expired` directly for every `scraped_jobs` result classified expired, then archive application jobs. That write skipped `jobready_link_observations`, `last_confirmed_closed_at`, provider evidence, and the observation ordering in `apply_link_observations`. Either option, with any value, now prints `JOBREADY_SYNC_RETIRED` and exits with code 2 before loading the environment or opening a connection. The value is never echoed. Without them the command still validates CodeQuest `scraped_jobs` and never touches the JobReady catalog or application database.
- `codequest_jobs/audit_job_ready.py` `--prune-job-ready` and `--confirm-target` are RETIRED the same way. The audit is read-only: it reads the catalog in a read-only session, checks URLs, and writes only its local JSON report.

CodeQuest `scraped_jobs` is not authoritative for JobReady lifecycle state. CodeQuest is not authoritative for JobReady catalog membership. Catalog lifecycle changes come from the JobReady collector and validator in `run_jobready_daily.py`. Application membership reconciliation is the separate guarded `codequest_jobs/sync_job_ready_ui.py`.

## CodeQuest and local scraper writes

- `naukri_jobs_scraper/codequest_sync.py`
- `naukri_jobs_scraper/db.py`
- `naukri_jobs_scraper/push_to_production.py`
- `naukri_jobs_scraper/sync_scraped_to_local.py`
- `naukri_jobs_scraper/verify_db.py`
- `naukri_jobs_scraper/verify_production.py`
- `naukri_jobs_scraper/push_json_to_db.py`
- `codequest_jobs/db.py`
- `codequest_jobs/run_jobs_pipeline.py`

## Quizzes and practice items

- `codequest_jobs/enrichment.py` database writers (`classify_job_row` itself is still used)
- `codequest_jobs/practice_items.py`
- `codequest_jobs/quiz_packs.py`
- `codequest_jobs/quiz_generators.py`

## Duplicated Railway validator

- `link_validator_service/` except the thin `run_scheduled.py` wrapper and the Indeed checker re-export

Delete these only in a later cleanup commit, after the JobReady-only path has been the production command.
