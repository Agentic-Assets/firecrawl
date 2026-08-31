-- =============================================================================
-- 016_cre_listing_lifecycle.sql
-- Durable source-observation transitions and replay-safe lifecycle events.
--
-- Requires 003, 007, and 009. Additive and idempotent. This migration does not
-- reconcile existing disagreements between cre_listings and cre_source_index;
-- use cre_reconcile_listing_lifecycle.py with reviewed source evidence.
-- =============================================================================

ALTER TABLE credeals.cre_source_index
    ADD COLUMN IF NOT EXISTS observation_present boolean;
ALTER TABLE credeals.cre_source_index
    ADD COLUMN IF NOT EXISTS presence_generation bigint;
ALTER TABLE credeals.cre_source_index
    ADD COLUMN IF NOT EXISTS presence_changed_at timestamptz;

-- Seed the observation state once. soft_deleted remains the canonical lifecycle
-- mirror written by cre_ingest; it is not subsequently written by cre_monitor.
UPDATE credeals.cre_source_index
SET observation_present = NOT soft_deleted
WHERE observation_present IS NULL;
UPDATE credeals.cre_source_index
SET presence_generation = 0
WHERE presence_generation IS NULL;

ALTER TABLE credeals.cre_source_index
    ALTER COLUMN observation_present SET DEFAULT true;
ALTER TABLE credeals.cre_source_index
    ALTER COLUMN observation_present SET NOT NULL;
ALTER TABLE credeals.cre_source_index
    ALTER COLUMN presence_generation SET DEFAULT 0;
ALTER TABLE credeals.cre_source_index
    ALTER COLUMN presence_generation SET NOT NULL;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'cre_source_index_presence_generation_nonnegative'
      AND conrelid = 'credeals.cre_source_index'::regclass
  ) THEN
    ALTER TABLE credeals.cre_source_index
      ADD CONSTRAINT cre_source_index_presence_generation_nonnegative
      CHECK (presence_generation >= 0);
  END IF;
END $$;

ALTER TABLE credeals.cre_scrape_jobs
    ADD COLUMN IF NOT EXISTS artifact_run_key text;

CREATE UNIQUE INDEX IF NOT EXISTS cre_scrape_jobs_artifact_run_key_uidx
    ON credeals.cre_scrape_jobs (brokerage_id, artifact_run_key)
    WHERE artifact_run_key IS NOT NULL;

ALTER TABLE credeals.cre_listing_events
    ADD COLUMN IF NOT EXISTS presence_generation bigint;
ALTER TABLE credeals.cre_listing_events
    ADD COLUMN IF NOT EXISTS reconciliation_provenance text;
ALTER TABLE credeals.cre_listing_events
    ADD COLUMN IF NOT EXISTS evidence_observed_at timestamptz;
ALTER TABLE credeals.cre_listing_events
    ADD COLUMN IF NOT EXISTS evidence_time_semantics text;
ALTER TABLE credeals.cre_listing_events
    ADD COLUMN IF NOT EXISTS reconciliation_evidence_sha256 text;

-- Reconciliation-written history rows retain their source-observation semantics
-- and deterministic job identity. Normal ingestor history rows leave these null.
ALTER TABLE credeals.cre_listing_price_history
    ADD COLUMN IF NOT EXISTS reconciliation_job_id uuid;
ALTER TABLE credeals.cre_listing_price_history
    ADD COLUMN IF NOT EXISTS reconciliation_provenance text;
ALTER TABLE credeals.cre_listing_price_history
    ADD COLUMN IF NOT EXISTS observed_at_semantics text;
ALTER TABLE credeals.cre_listing_price_history
    ADD COLUMN IF NOT EXISTS reconciliation_evidence_sha256 text;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'cre_listing_price_history_reconciliation_job_fk'
      AND conrelid = 'credeals.cre_listing_price_history'::regclass
  ) THEN
    ALTER TABLE credeals.cre_listing_price_history
      ADD CONSTRAINT cre_listing_price_history_reconciliation_job_fk
      FOREIGN KEY (reconciliation_job_id)
      REFERENCES credeals.cre_scrape_jobs(id) ON DELETE SET NULL;
  END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS cre_listing_price_history_reconciliation_job_uidx
    ON credeals.cre_listing_price_history (listing_id, reconciliation_job_id)
    WHERE reconciliation_job_id IS NOT NULL;

-- NOT VALID preserves legacy rows, while PostgreSQL still enforces the check
-- for every new/updated lifecycle event after this migration is applied.
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'cre_listing_events_lifecycle_identity_required'
      AND conrelid = 'credeals.cre_listing_events'::regclass
  ) THEN
    ALTER TABLE credeals.cre_listing_events
      ADD CONSTRAINT cre_listing_events_lifecycle_identity_required
      CHECK (
        event_type NOT IN ('disappeared', 'reappeared')
        OR (presence_generation IS NOT NULL AND scrape_job_id IS NOT NULL)
      ) NOT VALID;
  END IF;
END $$;

-- NULL generations on legacy lifecycle rows remain valid history. Every new
-- writer in this change supplies a generation. The partial unique index makes
-- one transition replay-safe without blocking a later cycle.
CREATE UNIQUE INDEX IF NOT EXISTS cre_listing_events_presence_transition_uidx
    ON credeals.cre_listing_events
       (listing_id, event_type, presence_generation)
    WHERE event_type IN ('disappeared', 'reappeared')
      AND presence_generation IS NOT NULL;

COMMENT ON COLUMN credeals.cre_source_index.observation_present IS
    'Latest complete-enumeration observation. Maintained by monitor and full ingest; never canonical listing lifecycle.';
COMMENT ON COLUMN credeals.cre_source_index.presence_generation IS
    'Monotonic generation incremented once per present/absent observation transition.';
COMMENT ON COLUMN credeals.cre_source_index.presence_changed_at IS
    'Collector observation timestamp at which presence_generation last advanced.';
COMMENT ON COLUMN credeals.cre_source_index.soft_deleted IS
    'Canonical listing lifecycle mirror. Written only by cre_ingest/reconciliation, never cre_monitor.';
COMMENT ON COLUMN credeals.cre_scrape_jobs.artifact_run_key IS
    'Stable sha256 identity of immutable collector artifact content; unique per brokerage.';
COMMENT ON COLUMN credeals.cre_listing_events.presence_generation IS
    'Source-index transition generation for disappeared/reappeared event idempotency.';
COMMENT ON COLUMN credeals.cre_listing_events.reconciliation_provenance IS
    'Explicit repair provenance; null for ordinary collector events.';
COMMENT ON COLUMN credeals.cre_listing_events.evidence_observed_at IS
    'Timestamp of the source observation supporting a repair; not inferred transition time.';
COMMENT ON COLUMN credeals.cre_listing_events.evidence_time_semantics IS
    'Machine-readable interpretation of evidence_observed_at and detected_at for a repair.';
COMMENT ON COLUMN credeals.cre_listing_events.reconciliation_evidence_sha256 IS
    'SHA-256 of the reviewed source-evidence artifact supporting a repair.';
COMMENT ON COLUMN credeals.cre_listing_price_history.reconciliation_job_id IS
    'Deterministic approved reconciliation job; null for ordinary ingest history.';
COMMENT ON COLUMN credeals.cre_listing_price_history.reconciliation_provenance IS
    'Explicit source/authority provenance for a reconciliation-written snapshot.';
COMMENT ON COLUMN credeals.cre_listing_price_history.observed_at_semantics IS
    'Machine-readable interpretation of observed_at; reconciliation uses source_evidence_observed_at.';
COMMENT ON COLUMN credeals.cre_listing_price_history.reconciliation_evidence_sha256 IS
    'SHA-256 of the reviewed source-evidence artifact supporting a reconciliation snapshot.';
