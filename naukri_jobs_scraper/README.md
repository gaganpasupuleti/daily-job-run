# Naukri + Multi-Source Jobs Scraper

Scrapes India fresher/intern jobs from **Naukri** (HTTP API) plus **Indeed / LinkedIn** (`python-jobspy`). Tags the 18 CodeQuest role families and can dual-write to:

1. Local Railway table `naukri_scraper` (`DATABASE_URL`)
2. CodeQuest `scraped_jobs` (`CODEQUEST_DATABASE_URL`)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Paste DATABASE_URL and CODEQUEST_DATABASE_URL
```

## Full sister pipeline (scrape + links + enrich + quizzes)

From repo root (CodeQuest sync):

```bash
python codequest_jobs/run_jobs_pipeline.py --env-file naukri_jobs_scraper/.env
```

See [`../codequest_jobs/README.md`](../codequest_jobs/README.md).

## Commands

```bash
# Combined trigger — Naukri + Indeed + LinkedIn → both DBs
python run_all_sources.py --env-file .env

# Smoke (one family, capped)
python run_all_sources.py --family python-dev --max-pages 1 --results-wanted 5

# Local only (skip CodeQuest)
python run_all_sources.py --no-codequest

# Naukri-only full scrape (legacy)
python run_full_families.py --env-file .env

# Export to Excel
python export_excel.py --env-file .env
```

## Database

- Local: `naukri_scraper` (column `source`: naukri / indeed / linkedin)
- CodeQuest: `scraped_jobs` + `job_scrape_runs`
