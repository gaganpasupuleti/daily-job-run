-- Naukri scraper table (separate from other app tables in production Postgres)

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'naukri_jobs'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'naukri_scraper'
    ) THEN
        ALTER TABLE naukri_jobs RENAME TO naukri_scraper;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS naukri_scraper (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(32) NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'naukri',
    title TEXT NOT NULL,
    company TEXT,
    location TEXT,
    experience TEXT,
    salary TEXT,
    skills TEXT,
    description TEXT,
    job_url TEXT,
    posted_label TEXT,
    search_keyword TEXT,
    experience_filter INTEGER,
    role_family TEXT,
    role_family_name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_checked_at TIMESTAMPTZ
);

ALTER TABLE naukri_scraper ADD COLUMN IF NOT EXISTS role_family TEXT;
ALTER TABLE naukri_scraper ADD COLUMN IF NOT EXISTS role_family_name TEXT;
ALTER TABLE naukri_scraper ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE naukri_scraper ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
ALTER TABLE naukri_scraper ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ;
ALTER TABLE naukri_scraper ADD COLUMN IF NOT EXISTS source TEXT;

UPDATE naukri_scraper SET status = 'active' WHERE status IS NULL OR status = '';
UPDATE naukri_scraper SET last_seen_at = scraped_at WHERE last_seen_at IS NULL;
UPDATE naukri_scraper SET source = 'naukri' WHERE source IS NULL OR source = '';
ALTER TABLE naukri_scraper ALTER COLUMN source SET DEFAULT 'naukri';
ALTER TABLE naukri_scraper ALTER COLUMN source SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_naukri_scraper_scraped_at ON naukri_scraper (scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_naukri_scraper_keyword ON naukri_scraper (search_keyword);
CREATE INDEX IF NOT EXISTS idx_naukri_scraper_role_family ON naukri_scraper (role_family);
CREATE INDEX IF NOT EXISTS idx_naukri_scraper_status ON naukri_scraper (status);
CREATE INDEX IF NOT EXISTS idx_naukri_scraper_source ON naukri_scraper (source);
