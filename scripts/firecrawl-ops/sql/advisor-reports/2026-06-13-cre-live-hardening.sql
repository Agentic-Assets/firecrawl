-- =====================================================================
-- CRE LIVE HARDENING + DATA-QUALITY CLEANUP (one-time, idempotent)
-- Project: fhqycqubkkrdgzswccwd   Schema: credeals
-- Authored: 2026-06-13. Every statement was read-verified against live by the
-- data+automation recon pass and is a no-op on re-run.
--
-- SCOPE: data/DB integrity ONLY. The status-display feature is EXCLUDED:
--   - NO 005 view widening (status IN active/under_contract/pending)
--   - NO search_cre_listings() body widening
--   - NO Phase-2 status activation
-- Those stay gated with the (out-of-scope) EQUIRE consumer deploy.
--
-- HOW TO APPLY (quiet window only; no concurrent cre_ingest.py / cre_monitor.py
-- --apply / cre_gate.py --update-baseline / cre_daily_update.sh):
--   psql "$POSTGRES_URL_NON_POOLING" -v ON_ERROR_STOP=1 -f 2026-06-13-cre-live-hardening.sql
-- (Sections A and B are wrapped in one transaction. Section C is board-visible
-- and opt-in: it is commented out by default.)
-- =====================================================================

-- =====================================================================
-- SECTION A + B: integrity hardening + board-INVISIBLE data fixes (atomic)
-- =====================================================================
BEGIN;
SET LOCAL statement_timeout = '600s';

-- (A1) 002: status CHECK idempotent guard (verified NO-OP on live; already 9-value).
DO $$
DECLARE cdef text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO cdef
    FROM pg_constraint
    WHERE conrelid = 'credeals.cre_listings'::regclass
      AND conname  = 'cre_listings_status_check';
    IF cdef IS NULL
       OR cdef NOT LIKE '%under_contract%'
       OR cdef NOT LIKE '%pending%'
       OR cdef NOT LIKE '%off_market%' THEN
        ALTER TABLE credeals.cre_listings DROP CONSTRAINT IF EXISTS cre_listings_status_check;
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_status_check
            CHECK (status IN ('active', 'inactive', 'under_contract', 'pending',
                              'sold', 'leased', 'off_market', 'expired', 'withdrawn'));
    END IF;
END $$;

-- (A2) 002: cap_rate (>0 AND <0.5) and occupancy_rate [0,1] range CHECKs.
-- NOT EXISTS-guarded; first apply takes ACCESS EXCLUSIVE + a validating scan
-- (live verified compliant: cap_rate 0.0103..0.42, occupancy_rate all NULL).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_cap_rate_range_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_cap_rate_range_check
            CHECK (cap_rate IS NULL OR (cap_rate > 0 AND cap_rate < 0.5));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid='credeals.cre_listings'::regclass
                     AND conname='cre_listings_occupancy_rate_range_check') THEN
        ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_occupancy_rate_range_check
            CHECK (occupancy_rate IS NULL OR (occupancy_rate >= 0 AND occupancy_rate <= 1));
    END IF;
END $$;

-- (A3) 003: cre_scrape_log.job_id and listing_id FKs -> ON DELETE SET NULL (guarded).
DO $$
DECLARE d text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO d FROM pg_constraint
     WHERE conrelid='credeals.cre_scrape_log'::regclass AND conname='cre_scrape_log_job_id_fkey';
    IF d IS NULL OR position('ON DELETE SET NULL' IN d) = 0 THEN
        ALTER TABLE credeals.cre_scrape_log DROP CONSTRAINT IF EXISTS cre_scrape_log_job_id_fkey;
        ALTER TABLE credeals.cre_scrape_log ADD CONSTRAINT cre_scrape_log_job_id_fkey
            FOREIGN KEY (job_id) REFERENCES credeals.cre_scrape_jobs(id) ON DELETE SET NULL;
    END IF;
    SELECT pg_get_constraintdef(oid) INTO d FROM pg_constraint
     WHERE conrelid='credeals.cre_scrape_log'::regclass AND conname='cre_scrape_log_listing_id_fkey';
    IF d IS NULL OR position('ON DELETE SET NULL' IN d) = 0 THEN
        ALTER TABLE credeals.cre_scrape_log DROP CONSTRAINT IF EXISTS cre_scrape_log_listing_id_fkey;
        ALTER TABLE credeals.cre_scrape_log ADD CONSTRAINT cre_scrape_log_listing_id_fkey
            FOREIGN KEY (listing_id) REFERENCES credeals.cre_listings(id) ON DELETE SET NULL;
    END IF;
END $$;

-- (A4) 006: ISOLATED ALTER VIEW only (security model, not visibility; verified
-- NO-OP on live, already security_invoker=true). The 006 CREATE OR REPLACE VIEW
-- is EXCLUDED (its l.* would expand the live view column set).
ALTER VIEW credeals.v_cre_listings_full SET (security_invoker = true);

-- (A5) 007: cre_listing_events_idem_uq -> NULLS NOT DISTINCT (plain unique index).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_index ix JOIN pg_class i ON i.oid=ix.indexrelid
        WHERE i.relname='cre_listing_events_idem_uq'
          AND i.relnamespace='credeals'::regnamespace
          AND ix.indnullsnotdistinct = false) THEN
        DROP INDEX credeals.cre_listing_events_idem_uq;
        CREATE UNIQUE INDEX cre_listing_events_idem_uq
            ON credeals.cre_listing_events
            (listing_id, event_type, COALESCE(field, ''), COALESCE(new_value, ''), scrape_job_id)
            NULLS NOT DISTINCT;
    END IF;
END $$;

-- (A6) 007: cre_enrichment_queue unique -> NULLS NOT DISTINCT (TABLE CONSTRAINT).
DO $$
DECLARE cname text; nnd boolean;
BEGIN
    SELECT con.conname, ix.indnullsnotdistinct INTO cname, nnd
    FROM pg_constraint con
    JOIN pg_index ix ON ix.indexrelid = con.conindid
    WHERE con.conrelid='credeals.cre_enrichment_queue'::regclass
      AND con.contype='u'
      AND pg_get_constraintdef(con.oid) = 'UNIQUE (brokerage_id, external_id, reason)';
    IF cname IS NOT NULL AND nnd = false THEN
        EXECUTE format('ALTER TABLE credeals.cre_enrichment_queue DROP CONSTRAINT %I', cname);
        ALTER TABLE credeals.cre_enrichment_queue
            ADD CONSTRAINT cre_enrichment_queue_brokerage_id_external_id_reason_key
            UNIQUE NULLS NOT DISTINCT (brokerage_id, external_id, reason);
    END IF;
END $$;

-- (A7) 007: cre_listing_events brokerage_id and scrape_job_id FKs -> ON DELETE SET NULL.
DO $$
DECLARE d text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO d FROM pg_constraint
     WHERE conrelid='credeals.cre_listing_events'::regclass AND conname='cre_listing_events_brokerage_id_fkey';
    IF d IS NULL OR position('ON DELETE SET NULL' IN d) = 0 THEN
        ALTER TABLE credeals.cre_listing_events DROP CONSTRAINT IF EXISTS cre_listing_events_brokerage_id_fkey;
        ALTER TABLE credeals.cre_listing_events ADD CONSTRAINT cre_listing_events_brokerage_id_fkey
            FOREIGN KEY (brokerage_id) REFERENCES credeals.cre_brokerages(id) ON DELETE SET NULL;
    END IF;
    SELECT pg_get_constraintdef(oid) INTO d FROM pg_constraint
     WHERE conrelid='credeals.cre_listing_events'::regclass AND conname='cre_listing_events_scrape_job_id_fkey';
    IF d IS NULL OR position('ON DELETE SET NULL' IN d) = 0 THEN
        ALTER TABLE credeals.cre_listing_events DROP CONSTRAINT IF EXISTS cre_listing_events_scrape_job_id_fkey;
        ALTER TABLE credeals.cre_listing_events ADD CONSTRAINT cre_listing_events_scrape_job_id_fkey
            FOREIGN KEY (scrape_job_id) REFERENCES credeals.cre_scrape_jobs(id) ON DELETE SET NULL;
    END IF;
END $$;

-- (B1) 50 board-INVISIBLE JLL rows: deleted_at set but status still 'active'.
-- They are already hidden (every v_cre_* view filters deleted_at IS NULL), so
-- this is pure consistency. Naturally idempotent (re-run matches 0 rows).
UPDATE credeals.cre_listings
   SET status='inactive'
 WHERE deleted_at IS NOT NULL AND status='active';

-- (B2) transwestern notes cosmetic fix: restore canonical "...empty or '-'."
-- (live corrupted to "...empty or '."). Guarded; no-op once corrected.
UPDATE credeals.cre_brokerages
   SET scrape_config = jsonb_set(scrape_config, '{notes}',
         to_jsonb('Collected by cre_collector source key transwestern. Use GET, not the browser POST body. Skip feed rows whose PageUrl is empty or ''-''.'::text)),
       updated_at = now()
 WHERE slug='transwestern'
   AND scrape_config->>'notes' = 'Collected by cre_collector source key transwestern. Use GET, not the browser POST body. Skip feed rows whose PageUrl is empty or ''.';

COMMIT;

-- =====================================================================
-- SECTION C (OPT-IN, BOARD-VISIBLE): Savills residential contamination cleanup
-- Removes 101 mis-categorized residential luxury "sale" rows + 1 non-US lease
-- ghost from the live board. Reversible (resurrect on re-collect). UNCOMMENT to
-- run; keep separate from A/B because it changes what users see.
-- =====================================================================
-- BEGIN;
-- UPDATE credeals.cre_listings
--    SET status='inactive', deleted_at=now(), updated_at=now()
--  WHERE brokerage_id=(SELECT id FROM credeals.cre_brokerages WHERE slug='savills')
--    AND transaction_type='sale' AND deleted_at IS NULL;        -- 101 residential rows
-- UPDATE credeals.cre_listings
--    SET status='inactive', deleted_at=now(), updated_at=now()
--  WHERE external_id='cyelit10899'
--    AND brokerage_id=(SELECT id FROM credeals.cre_brokerages WHERE slug='savills')
--    AND deleted_at IS NULL;                                     -- 1 Cyprus ghost lease row
-- COMMIT;

-- =====================================================================
-- POST-APPLY VERIFICATION (read-only)
-- =====================================================================
-- range checks present (expect 2 rows):
-- SELECT conname FROM pg_constraint WHERE conrelid='credeals.cre_listings'::regclass
--   AND conname IN ('cre_listings_cap_rate_range_check','cre_listings_occupancy_rate_range_check') ORDER BY 1;
-- all four target FKs ON DELETE SET NULL:
-- SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
--   WHERE conname IN ('cre_scrape_log_job_id_fkey','cre_scrape_log_listing_id_fkey',
--                     'cre_listing_events_brokerage_id_fkey','cre_listing_events_scrape_job_id_fkey') ORDER BY 1;
-- both unique objects NULLS NOT DISTINCT (expect indnullsnotdistinct=t):
-- SELECT i.relname, ix.indnullsnotdistinct FROM pg_index ix JOIN pg_class i ON i.oid=ix.indexrelid
--   WHERE i.relname IN ('cre_listing_events_idem_uq','cre_enrichment_queue_brokerage_id_external_id_reason_key');
-- 0 deleted-but-active rows remain:
-- SELECT count(*) FROM credeals.cre_listings WHERE deleted_at IS NOT NULL AND status='active';
-- transwestern notes corrected:
-- SELECT scrape_config->>'notes' FROM credeals.cre_brokerages WHERE slug='transwestern';
