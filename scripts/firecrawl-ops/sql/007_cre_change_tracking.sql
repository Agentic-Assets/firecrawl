-- =============================================================================
-- 007_cre_change_tracking.sql
-- CRE Listing Intelligence for EQUIRE
-- Supabase project: supabase-agentic-assets-v2 (fhqycqubkkrdgzswccwd)
--
-- Change-tracking / monitor layer (design doc section 7). ADDITIVE ONLY: four
-- new credeals.cre_* tables that give the system a persisted enumeration
-- snapshot, an append-only change ledger, a durable enrichment queue, and a
-- per-source health baseline. Nothing here alters or drops an existing object.
-- The cre_listings column/CHECK additions live in 002, their indexes in 004,
-- and the v_cre_recent_changes view in 005 (which runs last).
--
-- Registered in 000_run_all.sql AFTER 004 and BEFORE 006, so these tables exist
-- before 006 and 005 (views) run.
--
-- Security posture mirrors the existing collector-owned tables (see
-- cre_collector/archive/SUPABASE_SECURITY_NOTE_2026-06-12.md): RLS enabled, no
-- public row policies. The service-role / direct postgres connection the
-- collector uses bypasses RLS. Do NOT grant anon/authenticated access.
--
-- Requires: 001_cre_brokerages.sql, 002_cre_listings.sql, 003_cre_scrape_tracking.sql.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- cre_listing_events -- append-only change ledger (the history sink the system
-- lacks today). One row per detected change; never updated in place.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_listing_events (
    id                  uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    listing_id          uuid        NOT NULL REFERENCES credeals.cre_listings(id) ON DELETE CASCADE,
    brokerage_id        uuid        REFERENCES credeals.cre_brokerages(id),
    scrape_job_id       uuid        REFERENCES credeals.cre_scrape_jobs(id),  -- per-run row, inserted first
    event_type          text        NOT NULL CHECK (event_type IN
                          ('new', 'status_change', 'price_change', 'disappeared', 'reappeared', 'possible_relist')),
    field               text,                       -- which field changed (status, sale_price_usd, ...)
    old_value           text,
    new_value           text,
    source_value        text,                       -- raw source signal (e.g. http_410, raw status string)
    source_status_value text,                       -- raw native status string as scraped (design section 12.6)
    sale_price_text     text,                       -- raw salePriceText evidence ("Negotiable", "Call for offers") (12.6)
    lease_rate_text     text,                       -- raw leaseRateText evidence (12.6)
    source_url          text,                       -- primary-source grounding for the event
    content_hash        text,                       -- price+status fingerprint at detection time
    detected_at         timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS cre_listing_events_listing_idx   ON credeals.cre_listing_events (listing_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS cre_listing_events_type_idx      ON credeals.cre_listing_events (event_type, detected_at DESC);
CREATE INDEX IF NOT EXISTS cre_listing_events_brokerage_idx ON credeals.cre_listing_events (brokerage_id, detected_at DESC);
-- Within-run idempotency guard: re-emitting the same event under the same run is a no-op.
-- (Cross-run idempotency comes from cre_source_index state: a re-run sees no delta.)
CREATE UNIQUE INDEX IF NOT EXISTS cre_listing_events_idem_uq
    ON credeals.cre_listing_events (listing_id, event_type, COALESCE(field, ''), COALESCE(new_value, ''), scrape_job_id);

ALTER TABLE credeals.cre_listing_events ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE  credeals.cre_listing_events IS 'Append-only change ledger for cre_listings: new/status_change/price_change/disappeared/reappeared/possible_relist. Derived from real before-vs-source deltas, never from updated_at. Stores raw source evidence (status/price text, content hash) per design section 12.6. Backs v_cre_recent_changes.';
COMMENT ON COLUMN credeals.cre_listing_events.scrape_job_id IS 'Per-run cre_scrape_jobs id, inserted FIRST so this FK holds; also the run scope for the idempotency unique index.';

-- ---------------------------------------------------------------------------
-- cre_source_index -- persisted enumeration snapshot for the monitor. Keyed
-- EXACTLY like cre_listings (brokerage_id + the same prefixed external_id the
-- ingestor derives), so the diff runner can match without re-scraping detail.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_source_index (
    id                 uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    brokerage_id       uuid        NOT NULL REFERENCES credeals.cre_brokerages(id),
    external_id        text        NOT NULL,    -- the SAME prefixed id cre_ingest derives
    source_key         text,                    -- non-key attribute (cbre-dealflow, colliers-main, ...)
    url                text,
    source_lastmod     timestamptz,
    fingerprint        text,                    -- price+status hash for Tier-A change diff
    soft_deleted       boolean     DEFAULT false,  -- mirrored from cre_listings.deleted_at
    observed_status    text,
    first_seen         timestamptz DEFAULT now(),
    last_seen          timestamptz DEFAULT now(),
    last_enumerated_at timestamptz DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS cre_source_index_uq             ON credeals.cre_source_index (brokerage_id, external_id);
CREATE INDEX        IF NOT EXISTS cre_source_index_first_seen_idx ON credeals.cre_source_index (first_seen DESC);
CREATE INDEX        IF NOT EXISTS cre_source_index_source_key_idx ON credeals.cre_source_index (source_key);

ALTER TABLE credeals.cre_source_index ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_source_index IS 'Per-source enumeration snapshot keyed (brokerage_id, external_id) like cre_listings. The monitor diffs the latest enumeration against this to detect new/disappeared listings without re-scraping detail.';

-- ---------------------------------------------------------------------------
-- cre_enrichment_queue -- durable work queue for Tier-B (sitemap-detail)
-- enrichment only. A new/changed listing that needs a detail fetch is enqueued
-- here and drained by the enrichment pass.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_enrichment_queue (
    id           uuid        DEFAULT gen_random_uuid() PRIMARY KEY,
    brokerage_id uuid        REFERENCES credeals.cre_brokerages(id),
    source_key   text,
    external_id  text,
    url          text,
    reason       text        CHECK (reason IN ('new', 'changed')),
    priority     int         DEFAULT 100,
    enqueued_at  timestamptz DEFAULT now(),
    claimed_at   timestamptz,
    done_at      timestamptz,
    attempts     int         DEFAULT 0,
    last_error   text,
    UNIQUE (brokerage_id, external_id, reason)
);

CREATE INDEX IF NOT EXISTS cre_enrichment_queue_drain_idx ON credeals.cre_enrichment_queue (priority, enqueued_at) WHERE done_at IS NULL;

ALTER TABLE credeals.cre_enrichment_queue ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_enrichment_queue IS 'Durable Tier-B enrichment work queue. New/changed listings needing a detail fetch are enqueued and drained by the enrichment pass; UNIQUE (brokerage_id, external_id, reason) dedups.';

-- ---------------------------------------------------------------------------
-- cre_source_baseline -- per-source health baseline. Updated ONLY after a clean
-- gated run (rolling median, not last) so the coverage-and-anomaly gate has a
-- stable reference and a single bad run cannot poison the baseline.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credeals.cre_source_baseline (
    source_key               text        PRIMARY KEY,
    brokerage_slug           text,
    median_active_rows       integer,
    last_active_rows         integer,
    last_accepted_scraped_at timestamptz,
    last_accepted_job_id     uuid        REFERENCES credeals.cre_scrape_jobs(id),
    challenge_rate           numeric,
    updated_at               timestamptz DEFAULT now()
);

ALTER TABLE credeals.cre_source_baseline ENABLE ROW LEVEL SECURITY;

COMMENT ON TABLE credeals.cre_source_baseline IS 'Per-source health baseline (rolling median active rows + last accepted run) for the coverage-and-anomaly gate. Updated only after a clean gated run.';

-- FK covering indexes (Splinter 0001_unindexed_foreign_keys). Also shipped in
-- 008_cre_fk_indexes.sql for live-project apply traceability; IF NOT EXISTS
-- in both places keeps 000_run_all idempotent.
CREATE INDEX IF NOT EXISTS cre_listing_events_scrape_job_idx
    ON credeals.cre_listing_events (scrape_job_id);

CREATE INDEX IF NOT EXISTS cre_source_baseline_last_accepted_job_idx
    ON credeals.cre_source_baseline (last_accepted_job_id);
