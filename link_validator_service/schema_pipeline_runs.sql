-- Pipeline run logs (sister ops → CodeQuest Postgres). Safe to re-run.

CREATE TABLE IF NOT EXISTS job_link_validation_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    duration_ms INTEGER,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    triggered_by VARCHAR(255) NOT NULL DEFAULT 'validate_links',
    include_bad BOOLEAN NOT NULL DEFAULT FALSE,
    only_unchecked BOOLEAN NOT NULL DEFAULT FALSE,
    sources_json TEXT,
    limit_n INTEGER NOT NULL DEFAULT 0,
    workers INTEGER,
    queued INTEGER NOT NULL DEFAULT 0,
    checked INTEGER NOT NULL DEFAULT 0,
    active_count INTEGER NOT NULL DEFAULT 0,
    expired_count INTEGER NOT NULL DEFAULT 0,
    failed_link_count INTEGER NOT NULL DEFAULT 0,
    unknown_count INTEGER NOT NULL DEFAULT 0,
    by_source_json TEXT,
    db_after_json TEXT,
    report_path TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_link_validation_runs_started
    ON job_link_validation_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_link_validation_runs_status
    ON job_link_validation_runs (status);

CREATE TABLE IF NOT EXISTS job_push_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    duration_ms INTEGER,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    triggered_by VARCHAR(255) NOT NULL DEFAULT 'push_validated_to_job_ready',
    destination VARCHAR(64) NOT NULL DEFAULT 'job_ready',
    active_fetched INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    updated_count INTEGER NOT NULL DEFAULT 0,
    total_after INTEGER NOT NULL DEFAULT 0,
    by_source_json TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_push_runs_started
    ON job_push_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_push_runs_status
    ON job_push_runs (status);

-- Ensure scrape-run table has the counters used by ops tooling (idempotent).
CREATE TABLE IF NOT EXISTS job_scrape_runs (
    id SERIAL PRIMARY KEY,
    search_term VARCHAR(200),
    location VARCHAR(120),
    sources_json TEXT,
    results_wanted INTEGER,
    hours_old INTEGER,
    total_found INTEGER DEFAULT 0,
    saved_count INTEGER DEFAULT 0,
    skipped_duplicates INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    errors_json TEXT,
    source_breakdown_json TEXT,
    run_type VARCHAR(64),
    profile VARCHAR(64),
    expired_count INTEGER DEFAULT 0,
    failed_link_count INTEGER DEFAULT 0,
    started_at TIMESTAMP WITHOUT TIME ZONE,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    duration_ms INTEGER,
    triggered_by VARCHAR(255),
    status VARCHAR(32)
);

ALTER TABLE job_scrape_runs ADD COLUMN IF NOT EXISTS expired_count INTEGER DEFAULT 0;
ALTER TABLE job_scrape_runs ADD COLUMN IF NOT EXISTS failed_link_count INTEGER DEFAULT 0;
