# CodeQuest sister jobs + quizzes ops

Ops control plane for [learn-coding-through](https://github.com/gaganpasupuleti/learn-coding-through).  
Writes the **same Postgres tables** as CodeQuest (`scraped_jobs`, `job_enrichments`, `quiz_packs`, …).

## Cursor runbook

**Ask Cursor:** `run jobs + quizzes`

```bash
python codequest_jobs/run_jobs_pipeline.py --env-file naukri_jobs_scraper/.env
```

Capped smoke (one family):

```bash
python codequest_jobs/run_jobs_pipeline.py ^
  --env-file naukri_jobs_scraper/.env ^
  --family python-dev ^
  --steps scrape,links,enrich,quizzes ^
  --max-pages 1 --results-wanted 5 --link-limit 40
```

Skip scrape (enrich + quizzes only):

```bash
python codequest_jobs/run_jobs_pipeline.py --steps links,enrich,quizzes
```

## Pipeline steps

| Step | What it does | Tables |
|------|----------------|--------|
| `scrape` | Naukri + Indeed + LinkedIn | `scraped_jobs`, local `naukri_scraper` |
| `links` | Recheck URLs (HEAD + Naukri API) | `scraped_jobs.link_status` |
| `enrich` | ROLE_* tagging + **clean JD** + keywords + job practice | `job_enrichments`, `job_practice_items` |
| `quizzes` | Role-level MCQ + coding workbench packs | `quiz_packs`, `quiz_pack_questions` |

Clean JD columns on `job_enrichments`: `jd_clean`, `jd_responsibilities`, `jd_requirements`, `keywords_must`, `keywords_nice`, `practice_focus`.

## Env

See [`naukri_jobs_scraper/.env.example`](../naukri_jobs_scraper/.env.example):

- `DATABASE_URL` — local scraper table
- `CODEQUEST_DATABASE_URL` — CodeQuest / shared app DB

## Sync contract

See [sync_contract.md](sync_contract.md). Taxonomy must stay aligned with CodeQuest `job_role_seed.py`.
