# LEGACY. Sister repo ↔ CodeQuest sync contract

The daily pipeline no longer writes CodeQuest. See the repository README.

This package (`_tools/codequest_jobs`) is the **ops control plane** for
[learn-coding-through](https://github.com/gaganpasupuleti/learn-coding-through).
It writes the **same Postgres tables** the CodeQuest Job Board reads.

## Shared tables (app-visible)

| Table | Written by sister | Notes |
|-------|-------------------|--------|
| `scraped_jobs` | scrape + link check | `CQJ-*` job_id, `link_status`, `ingest_profile` |
| `job_scrape_runs` | scrape audit | optional |
| `job_roles` | taxonomy seed | `ROLE_*` IDs — do not invent new ones |
| `job_role_levels` | taxonomy seed | `{ROLE_*}_{FRESHER\|ENTRY\|EXPERIENCED}` |
| `job_enrichments` | enrich step | PK = `scraped_jobs.job_id` (CQJ-*) |
| `job_enrichments.jd_clean` | enrich step | Plain-text cleaned JD (HTML stripped) |
| `job_enrichments.jd_responsibilities` / `jd_requirements` | enrich | Structured bullet JSON |
| `job_enrichments.keywords_must` / `keywords_nice` / `practice_focus` | enrich | Clear keyword lists |
| `job_practice_items` | enrich step | Job-specific MCQ / CODING / SQL practice |
| `quiz_packs` | quizzes step | `QP_<FAMILY>_<LEVEL>_W01` |
| `quiz_pack_questions` | quizzes step | Role-level MCQ + workbench types |

Schema SQL: [`schema_jd_clean.sql`](schema_jd_clean.sql) (idempotent `ALTER` / `CREATE`).

## Source of truth in CodeQuest main

- Role seed: `backend/app/data/job_role_seed.py`
- Enrichment rules: `scripts/run_job_skill_enrichment.py`
- Link checker: `backend/app/services/job_link_checker.py`
- Docs: `docs/jobs/JOB_ROLE_ID_MODEL.md`, `QUIZ_PACK_IMPORT_SCHEMA.md`

When CodeQuest taxonomy changes, update `role_taxonomy.py` here to match.

## Env

- `CODEQUEST_DATABASE_URL` — CodeQuest / shared Railway Postgres
- `DATABASE_URL` — local scraper table (`naukri_scraper`); scrape dual-write uses both

## Cursor runbook

```bash
python _tools/codequest_jobs/run_jobs_pipeline.py --env-file _tools/naukri_jobs_scraper/.env
```

Ask Cursor: **run jobs + quizzes** → that script (steps: scrape, links, enrich, quizzes).
