-- Additive route-evidence store (NOT wired to apply_link_observations).
-- Apply only on disposable development databases.

CREATE TABLE IF NOT EXISTS jobready_route_observations (
    evidence_ref UUID PRIMARY KEY,
    run_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('listing', 'employer_requisition', 'application_route')),
    subject_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('confirmed_open', 'confirmed_closed', 'unresolved')),
    reason TEXT NOT NULL,
    expected_identity TEXT,
    observed_identities TEXT[],
    identity_link_type TEXT,
    identity_link_detail TEXT,
    destination_self_match BOOLEAN,
    catalog_link_assured BOOLEAN,
    requested_url TEXT,
    final_url TEXT,
    http_status INTEGER,
    contract_version TEXT NOT NULL DEFAULT 'jobready-route-evidence-v1',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS jobready_route_observations_job_id_idx ON jobready_route_observations (job_id, observed_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS jobready_route_observations_dedupe_idx
    ON jobready_route_observations (job_id, subject_type, provider, adapter_version, observed_at, reason);
