-- =============================================================================
-- 003_cre_scrape_tracking.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- Run-level and URL-level observability for scrape jobs. One cre_scrape_jobs
-- row per broker run; one cre_scrape_log row per URL attempt. Mirrors the
-- pattern of EQUIRE prospect_agent_runs / prospect_agent_run_events so the
-- ListingHunterAgent can report progress and operators can audit failures.
--
-- Requires: 001_cre_brokerages.sql, 002_cre_listings.sql (FK targets).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- cre_scrape_jobs -- one row per scrape run (per broker, per invocation).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_scrape_jobs (
    id                  uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    brokerage_id        uuid        REFERENCES credeals.cre_brokerages(id),
    status              text        DEFAULT 'running'
                                    CHECK (status IN ('running', 'completed', 'failed', 'partial')),
    started_at          timestamptz DEFAULT now(),
    completed_at        timestamptz,
    listings_discovered integer     DEFAULT 0,  -- URLs found in the discovery/seed phase
    listings_scraped    integer     DEFAULT 0,  -- detail pages successfully rendered
    listings_saved      integer     DEFAULT 0,  -- rows upserted into cre_listings
    errors_count        integer     DEFAULT 0,
    notes               text,
    created_at          timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cre_scrape_jobs_brokerage_idx ON credeals.cre_scrape_jobs (brokerage_id);
CREATE INDEX IF NOT EXISTS cre_scrape_jobs_status_idx     ON credeals.cre_scrape_jobs (status);
CREATE INDEX IF NOT EXISTS cre_scrape_jobs_started_idx    ON credeals.cre_scrape_jobs (started_at DESC);

COMMENT ON TABLE  credeals.cre_scrape_jobs        IS 'One row per CRE scrape run. Tracks discovery/scrape/save counts and final status for operator audit.';
COMMENT ON COLUMN credeals.cre_scrape_jobs.status IS 'running | completed (clean) | partial (some errors) | failed (run aborted).';

-- -----------------------------------------------------------------------------
-- cre_scrape_log -- one row per URL attempt within a job.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_scrape_log (
    id            uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    job_id        uuid        REFERENCES credeals.cre_scrape_jobs(id),
    listing_id    uuid        REFERENCES credeals.cre_listings(id),  -- null on error/skip before a row exists
    url           text,
    status        text        CHECK (status IN ('success', 'error', 'skipped', 'duplicate')),
    http_status   integer,
    error_message text,
    scraped_at    timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cre_scrape_log_job_idx     ON credeals.cre_scrape_log (job_id);
CREATE INDEX IF NOT EXISTS cre_scrape_log_status_idx  ON credeals.cre_scrape_log (status);
CREATE INDEX IF NOT EXISTS cre_scrape_log_listing_idx ON credeals.cre_scrape_log (listing_id);

COMMENT ON TABLE  credeals.cre_scrape_log        IS 'Per-URL scrape attempt log. status duplicate = matched an existing cre_listings row; skipped = filtered before fetch.';
COMMENT ON COLUMN credeals.cre_scrape_log.status IS 'success | error | skipped | duplicate.';
