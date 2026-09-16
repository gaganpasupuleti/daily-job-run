-- Clean JD + job-specific practice (sister ops → shared CodeQuest Postgres)
-- Safe to re-run.

ALTER TABLE job_enrichments ADD COLUMN IF NOT EXISTS jd_clean TEXT;
ALTER TABLE job_enrichments ADD COLUMN IF NOT EXISTS jd_responsibilities JSONB;
ALTER TABLE job_enrichments ADD COLUMN IF NOT EXISTS jd_requirements JSONB;
ALTER TABLE job_enrichments ADD COLUMN IF NOT EXISTS keywords_must JSONB;
ALTER TABLE job_enrichments ADD COLUMN IF NOT EXISTS keywords_nice JSONB;
ALTER TABLE job_enrichments ADD COLUMN IF NOT EXISTS practice_focus JSONB;

CREATE TABLE IF NOT EXISTS job_practice_items (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(32) NOT NULL REFERENCES job_enrichments(job_id) ON DELETE CASCADE,
    skill_tag VARCHAR(64) NOT NULL,
    item_type VARCHAR(32) NOT NULL,
    prompt TEXT NOT NULL,
    starter_code_or_query TEXT,
    expected_output TEXT,
    option_a TEXT,
    option_b TEXT,
    option_c TEXT,
    option_d TEXT,
    correct_option VARCHAR(1),
    explanation TEXT NOT NULL DEFAULT '',
    difficulty VARCHAR(20) NOT NULL DEFAULT 'easy',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_job_practice_items_job_id ON job_practice_items (job_id);
CREATE INDEX IF NOT EXISTS ix_job_practice_items_type ON job_practice_items (item_type);
CREATE INDEX IF NOT EXISTS ix_job_enrichments_jd_clean_len ON job_enrichments ((length(COALESCE(jd_clean, ''))));
