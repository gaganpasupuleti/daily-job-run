# Legacy modules

The daily command is `python codequest_jobs/run_jobready_daily.py`. These files remain so older audits can still be read. They are not part of that command.

## CodeQuest and local scraper writes

- `naukri_jobs_scraper/codequest_sync.py`
- `naukri_jobs_scraper/db.py`
- `naukri_jobs_scraper/push_to_production.py`
- `naukri_jobs_scraper/sync_scraped_to_local.py`
- `naukri_jobs_scraper/verify_db.py`
- `naukri_jobs_scraper/verify_production.py`
- `naukri_jobs_scraper/push_json_to_db.py`
- `codequest_jobs/push_validated_to_job_ready.py` (`--prune` is refused)
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
