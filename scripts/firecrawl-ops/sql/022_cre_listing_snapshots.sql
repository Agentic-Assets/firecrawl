-- Draft only: private canonical listing snapshots. Not part of 000_run_all.sql.
-- Apply only after separate owner review and an authorized migration procedure.
\set ON_ERROR_STOP on
\if :{?CRE_APPROVE_LISTING_SNAPSHOTS}
\else
  \set CRE_APPROVE_LISTING_SNAPSHOTS 0
\endif
\if :{?CRE_LISTING_SNAPSHOTS_OPERATOR}
\else
  \set CRE_LISTING_SNAPSHOTS_OPERATOR ''
\endif
\if :{?CRE_LISTING_SNAPSHOTS_APPROVAL_REF}
\else
  \set CRE_LISTING_SNAPSHOTS_APPROVAL_REF ''
\endif
\if :{?CRE_LISTING_SNAPSHOTS_CONFIRM}
\else
  \set CRE_LISTING_SNAPSHOTS_CONFIRM ''
\endif
SELECT (:'CRE_APPROVE_LISTING_SNAPSHOTS' = '1'
  AND :'CRE_LISTING_SNAPSHOTS_OPERATOR' IN ('cayman','stace')
  AND :'CRE_LISTING_SNAPSHOTS_APPROVAL_REF' ~ '^AGENTIC-[0-9]+$'
  AND :'CRE_LISTING_SNAPSHOTS_CONFIRM' = 'APPLY 022_cre_listing_snapshots') AS snapshot_approved \gset
\if :snapshot_approved
\else
  DO $$ BEGIN RAISE EXCEPTION 'listing snapshot migration requires owner approval'; END $$;
\endif
BEGIN;
CREATE TABLE IF NOT EXISTS credeals.cre_listing_snapshots (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  brokerage_id uuid NOT NULL,
  external_id text NOT NULL,
  listing_id uuid,
  artifact_run_key text NOT NULL,
  phase text NOT NULL CHECK (phase IN ('preimage', 'observation', 'postimage')),
  payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  payload jsonb NOT NULL,
  captured_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (brokerage_id, external_id, artifact_run_key, phase, payload_sha256)
);
CREATE INDEX IF NOT EXISTS cre_listing_snapshots_lookup_idx
  ON credeals.cre_listing_snapshots (brokerage_id, external_id, captured_at DESC);
ALTER TABLE credeals.cre_listing_snapshots ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON credeals.cre_listing_snapshots FROM PUBLIC;
REVOKE ALL ON SEQUENCE credeals.cre_listing_snapshots_id_seq FROM PUBLIC;
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
    REVOKE ALL ON credeals.cre_listing_snapshots FROM anon;
    REVOKE ALL ON SEQUENCE credeals.cre_listing_snapshots_id_seq FROM anon;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
    REVOKE ALL ON credeals.cre_listing_snapshots FROM authenticated;
    REVOKE ALL ON SEQUENCE credeals.cre_listing_snapshots_id_seq FROM authenticated;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='service_role') THEN
    REVOKE ALL ON credeals.cre_listing_snapshots FROM service_role;
    REVOKE ALL ON SEQUENCE credeals.cre_listing_snapshots_id_seq FROM service_role;
    GRANT SELECT, INSERT ON credeals.cre_listing_snapshots TO service_role;
    GRANT USAGE, SELECT ON SEQUENCE credeals.cre_listing_snapshots_id_seq TO service_role;
  END IF;
END $$;
COMMENT ON TABLE credeals.cre_listing_snapshots IS
  'Draft private collector-owned parent/child and staged observation snapshots; never provider wire bytes. Retention is indefinite until separately reviewed policy; no automatic deletion.';
COMMIT;