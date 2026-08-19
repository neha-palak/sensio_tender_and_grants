-- Run once against the Supabase database: psql $DATABASE_URL -f schema.sql
-- (or paste into Supabase's SQL editor). Safe to re-run (IF NOT EXISTS throughout).
--
-- tenders/grants replace all_tenders_pipeline.xlsx / all_grants_pipeline.xlsx
-- (written by the two scrapers). saved_items replaces the per-founder
-- saved_<name>.xlsx files -- Postgres gives real concurrent writes, so there's
-- no more need to shard saves per founder to dodge Google Drive's lack of
-- file locking.

CREATE TABLE IF NOT EXISTS tenders (
    primary_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    sector TEXT,
    country TEXT,
    opening_date TEXT,
    closing_date TEXT,
    relevancy_score NUMERIC,
    inr_budget_maximum BIGINT,
    description TEXT,
    organisation_name TEXT,
    url TEXT,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS grants (
    primary_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    sector TEXT,
    country TEXT,
    opening_date TEXT,
    closing_date TEXT,
    relevancy_score NUMERIC,
    inr_budget_maximum BIGINT,
    description TEXT,
    organisation_name TEXT,
    url TEXT,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- domain is 'tenders' or 'grants'; item_id references tenders.primary_key or
-- grants.primary_key depending on domain (no FK -- a save is a snapshot that
-- should survive the source row being dropped by the next scrape, same as
-- the old per-founder Excel snapshots did).
CREATE TABLE IF NOT EXISTS saved_items (
    id SERIAL PRIMARY KEY,
    domain TEXT NOT NULL CHECK (domain IN ('tenders', 'grants')),
    item_id TEXT NOT NULL,
    founder TEXT NOT NULL,
    saved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (domain, item_id, founder)
);

CREATE INDEX IF NOT EXISTS saved_items_domain_item_idx ON saved_items (domain, item_id);

-- The app connects as the `postgres` role (via DATABASE_URL), which bypasses
-- RLS -- so this doesn't affect db.py. It blocks Supabase's auto-generated
-- PostgREST API (the anon/authenticated keys), which this project never uses.
-- No policies needed: RLS-enabled + zero policies = deny by default to those
-- keys.
ALTER TABLE tenders ENABLE ROW LEVEL SECURITY;
ALTER TABLE grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE saved_items ENABLE ROW LEVEL SECURITY;
