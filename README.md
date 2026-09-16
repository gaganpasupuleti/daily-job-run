# Daily job run

Scrape India fresher/intern jobs, validate links, tag `ROLE_*` families, and push rows into every target Postgres DB.

This repo is **ops only** — no book PDFs, no scrape dumps, no `.env` secrets.

## What it does

| Step | Tool | Writes |
|------|------|--------|
| **Run jobs** | `naukri_jobs_scraper/run_all_sources.py` | Local `naukri_scraper` + CodeQuest `scraped_jobs` |
| **Validate** | `codequest_jobs/validate_links.py` | `scraped_jobs.link_status` |
| **Tag** | `codequest_jobs/enrichment.py` | `job_enrichments` (`ROLE_*`, clean JD, keywords) |
| **Push** | `codequest_jobs/push_validated_to_job_ready.py` | job-ready `validated_jobs` |

Scheduled Railway cron (validate → tag → push) lives in `link_validator_service/run_scheduled.py`.

## Layout

| Path | Role |
|------|------|
| [`naukri_jobs_scraper/`](naukri_jobs_scraper/) | Naukri + Indeed + LinkedIn + Foundit scrape and dual-write |
| [`codequest_jobs/`](codequest_jobs/) | Link check, ROLE tagging, quiz packs, push to job-ready |
| [`link_validator_service/`](link_validator_service/) | Daily cron: validate + tag + push + prune stale live rows |

## Setup

```bash
cd naukri_jobs_scraper
pip install -r requirements.txt
cp .env.example .env
```

Set:

- `DATABASE_URL` — local scraper Postgres (`naukri_scraper`)
- `CODEQUEST_DATABASE_URL` — CodeQuest app Postgres (`scraped_jobs`, enrichments)
- `JOB_READY_DATABASE_URL` — job-ready-platform Postgres (`validated_jobs`)

## Daily commands

Full pipeline (scrape → validate → tag → quizzes):

```bash
python codequest_jobs/run_jobs_pipeline.py --env-file naukri_jobs_scraper/.env
```

Scrape only, all sources, both DBs:

```bash
python naukri_jobs_scraper/run_all_sources.py --env-file naukri_jobs_scraper/.env
```

Validate links, then push active jobs to job-ready:

```bash
python codequest_jobs/validate_links.py
python codequest_jobs/push_validated_to_job_ready.py
```

Railway one-shot (already deployed as the daily cron):

```bash
python link_validator_service/run_scheduled.py
```

## Related

- App: https://github.com/gaganpasupuleti/learn-coding-through
- Job-ready UI: https://github.com/gaganpasupuleti/job-ready-platform
- Earlier ops copy: https://github.com/gaganpasupuleti/codequest-jobs-ops
