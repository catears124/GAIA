CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS sync_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'partial', 'cancelled')),
    sources INTEGER NOT NULL DEFAULT 0 CHECK (sources >= 0),
    postings INTEGER NOT NULL DEFAULT 0 CHECK (postings >= 0),
    failed INTEGER NOT NULL DEFAULT 0 CHECK (failed >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_runs_single_running
    ON sync_runs ((status)) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS idx_sync_runs_finished_at
    ON sync_runs (finished_at DESC) WHERE finished_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS postings (
    posting_key TEXT PRIMARY KEY,
    family_key TEXT NOT NULL,
    company TEXT NOT NULL CHECK (btrim(company) <> ''),
    title TEXT NOT NULL CHECK (btrim(title) <> ''),
    normalized_title TEXT NOT NULL,
    locations TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    apply_url TEXT NOT NULL CHECK (btrim(apply_url) <> ''),
    canonical_apply_url TEXT NOT NULL CHECK (btrim(canonical_apply_url) <> ''),
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_mode TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    employment_type TEXT NOT NULL DEFAULT '',
    posted_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ,
    posted_raw TEXT,
    posted_precision TEXT NOT NULL DEFAULT 'unknown',
    posted_confidence TEXT NOT NULL DEFAULT 'unknown',
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    category TEXT NOT NULL,
    season TEXT,
    year SMALLINT,
    target_match TEXT NOT NULL,
    link_checked_at TIMESTAMPTZ,
    link_http_status INTEGER,
    link_final_url TEXT,
    link_status TEXT NOT NULL DEFAULT 'unchecked'
);

CREATE INDEX IF NOT EXISTS idx_postings_family_active
    ON postings (family_key) WHERE active;
CREATE INDEX IF NOT EXISTS idx_postings_target_active
    ON postings (target_match, category) WHERE active;
CREATE INDEX IF NOT EXISTS idx_postings_source_active
    ON postings (source) WHERE active;
CREATE INDEX IF NOT EXISTS idx_postings_canonical_url_active
    ON postings (canonical_apply_url) WHERE active;
CREATE INDEX IF NOT EXISTS idx_postings_company_trgm
    ON postings USING GIN (lower(company) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_postings_locations_gin
    ON postings USING GIN (locations);

CREATE TABLE IF NOT EXISTS families (
    family_key TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    season TEXT,
    year SMALLINT,
    target_match TEXT NOT NULL,
    opening_count INTEGER NOT NULL CHECK (opening_count >= 0),
    location_count INTEGER NOT NULL CHECK (location_count >= 0),
    locations TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    openings JSONB NOT NULL DEFAULT '[]'::JSONB,
    first_posted_at TIMESTAMPTZ,
    latest_posted_at TIMESTAMPTZ,
    posted_precision TEXT NOT NULL,
    first_detected_at TIMESTAMPTZ NOT NULL,
    last_verified_at TIMESTAMPTZ NOT NULL,
    direct_openings INTEGER NOT NULL CHECK (direct_openings >= 0),
    backstop_openings INTEGER NOT NULL CHECK (backstop_openings >= 0),
    CHECK (opening_count = direct_openings + backstop_openings)
);

CREATE INDEX IF NOT EXISTS idx_families_feed
    ON families (target_match, category, direct_openings, latest_posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_families_company_lower
    ON families (lower(company));
CREATE INDEX IF NOT EXISTS idx_families_company_trgm
    ON families USING GIN (lower(company) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_families_title_trgm
    ON families USING GIN (lower(title) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_families_locations_gin
    ON families USING GIN (locations);
CREATE INDEX IF NOT EXISTS idx_families_openings_gin
    ON families USING GIN (openings jsonb_path_ops);

CREATE TABLE IF NOT EXISTS benchmark_cases (
    version TEXT NOT NULL,
    posting_key TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    employment_type TEXT NOT NULL,
    source_mode TEXT NOT NULL,
    expected_category TEXT NOT NULL,
    expected_target_match TEXT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (version, posting_key)
);

CREATE TABLE IF NOT EXISTS source_catalog (
    source TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('current', 'historical')),
    spec JSONB NOT NULL,
    first_discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_discovered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    complete BOOLEAN NOT NULL,
    rows_scanned INTEGER NOT NULL CHECK (rows_scanned >= 0),
    expected_rows INTEGER CHECK (expected_rows IS NULL OR expected_rows >= 0),
    target_rows INTEGER NOT NULL CHECK (target_rows >= 0),
    last_attempt_at TIMESTAMPTZ NOT NULL,
    last_success_at TIMESTAMPTZ,
    last_error TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    scope TEXT NOT NULL DEFAULT 'current' CHECK (scope IN ('current', 'historical')),
    note TEXT,
    last_run_id BIGINT REFERENCES sync_runs(id) ON DELETE SET NULL,
    lifecycle TEXT NOT NULL DEFAULT 'candidate'
        CHECK (lifecycle IN ('candidate', 'productive', 'quarantined')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0)
);

CREATE INDEX IF NOT EXISTS idx_source_health_run
    ON source_health (last_run_id, scope, status);
CREATE INDEX IF NOT EXISTS idx_source_health_lifecycle
    ON source_health (lifecycle, scope);

CREATE OR REPLACE FUNCTION promote_source_catalog_scope()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.lifecycle = 'productive' THEN
        UPDATE source_catalog
        SET scope = 'current', last_discovered_at = now()
        WHERE source = NEW.source;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS source_catalog_promote_current ON source_health;
CREATE TRIGGER source_catalog_promote_current
AFTER INSERT OR UPDATE OF lifecycle ON source_health
FOR EACH ROW
EXECUTE FUNCTION promote_source_catalog_scope();
