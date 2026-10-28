-- 018_cre_building_source_facts.sql: review candidate only, no live apply.
-- Psql-only; excluded from 000_run_all.sql. No writer, backfill or promotion.
-- Required: named owner issue, reviewed branch, fresh catalog/ledger/ACL and
-- dependency readback, delta, lock and rollback review, separate DDL approval.
-- Free text may contain sensitive or withheld values: source-specific visibility
-- and provenance proof are required before any separate writer or read exposure.
-- Table-level grants extend to added columns. Record preexisting object/comment
-- ownership; do not blindly drop or overwrite a preexisting field in rollback.
\set ON_ERROR_STOP on
\if :{?CRE_APPROVE_BUILDING_SOURCE_FACTS}
\else
  \set CRE_APPROVE_BUILDING_SOURCE_FACTS 0
\endif
\if :{?CRE_BUILDING_SOURCE_FACTS_OPERATOR}
\else
  \set CRE_BUILDING_SOURCE_FACTS_OPERATOR ''
\endif
\if :{?CRE_BUILDING_SOURCE_FACTS_APPROVAL_REF}
\else
  \set CRE_BUILDING_SOURCE_FACTS_APPROVAL_REF ''
\endif
\if :{?CRE_BUILDING_SOURCE_FACTS_CONFIRM}
\else
  \set CRE_BUILDING_SOURCE_FACTS_CONFIRM ''
\endif
SELECT (
  :'CRE_APPROVE_BUILDING_SOURCE_FACTS' = '1'
  AND :'CRE_BUILDING_SOURCE_FACTS_OPERATOR' IN ('cayman','stace')
  AND :'CRE_BUILDING_SOURCE_FACTS_APPROVAL_REF' ~ '^AGENTIC-[0-9]+$'
  AND :'CRE_BUILDING_SOURCE_FACTS_CONFIRM' = 'APPLY 018_cre_building_source_facts'
) AS cre_building_facts_authorized \gset
\if :cre_building_facts_authorized
  \echo 'Approved migration 018 building-source-facts requested'
\else
  \echo 'REFUSED: migration 018 requires operator, issue and exact confirmation'
  DO $$ BEGIN RAISE EXCEPTION 'migration 018 requires the approval-gated psql contract'; END $$;
\endif

BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='30s';
LOCK TABLE credeals.cre_listings IN ACCESS EXCLUSIVE MODE;
DO $$
DECLARE
  field text;
  expected_type regtype;
  existing_column record;
  existing_constraint record;
  field_attnum smallint;
BEGIN
  FOREACH field IN ARRAY ARRAY['elevator_count','typical_floor_size_text'] LOOP
    expected_type := CASE WHEN field='elevator_count' THEN 'integer'::regtype ELSE 'text'::regtype END;
    SELECT a.atttypid,a.attnotnull,a.attidentity,a.attgenerated,a.attnum,
      a.attcollation, t.typcollation,
      EXISTS (SELECT 1 FROM pg_attrdef d WHERE d.adrelid=a.attrelid AND d.adnum=a.attnum) AS has_default
    INTO existing_column FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid
    WHERE a.attrelid='credeals.cre_listings'::regclass AND a.attname=field AND NOT a.attisdropped;
    -- Existing columns require an owner decision and separate verification. A
    -- shape/comment match cannot prove legacy rows satisfy the new constraint,
    -- and scanning them under this exclusive lock is not an acceptable gate.
    IF FOUND THEN
      RAISE EXCEPTION 'preexisting column requires separate review: %',field;
    END IF;
    EXECUTE format('ALTER TABLE credeals.cre_listings ADD COLUMN %I %s',field,expected_type);
    SELECT attnum INTO field_attnum FROM pg_attribute WHERE attrelid='credeals.cre_listings'::regclass AND attname=field AND NOT attisdropped;
    IF EXISTS (
      SELECT 1 FROM pg_constraint c WHERE c.conrelid='credeals.cre_listings'::regclass
      AND NOT (field='elevator_count' AND c.conname='cre_listings_elevator_count_nonnegative')
      AND (c.conkey @> ARRAY[field_attnum]::smallint[] OR (c.contype='c' AND (c.conkey IS NULL OR 0=ANY(c.conkey))))
    ) THEN RAISE EXCEPTION 'additional constraint references column: %',field; END IF;
    IF EXISTS (
      SELECT 1 FROM pg_index i WHERE i.indrelid='credeals.cre_listings'::regclass AND i.indisunique
      AND (field_attnum=ANY(i.indkey) OR EXISTS (
        SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=i.indexrelid
        AND d.refclassid='pg_class'::regclass AND d.refobjid=i.indrelid AND d.refobjsubid=field_attnum
      ))
    ) THEN RAISE EXCEPTION 'unique index references column: %',field; END IF;
  END LOOP;
  SELECT c.contype,pg_get_expr(c.conbin,c.conrelid) AS expression,c.connoinherit,c.conkey
    INTO existing_constraint FROM pg_constraint c WHERE c.conrelid='credeals.cre_listings'::regclass
    AND c.conname='cre_listings_elevator_count_nonnegative';
  IF FOUND THEN
    IF existing_constraint.contype<>'c' OR existing_constraint.expression IS DISTINCT FROM '(elevator_count >= 0)'
      OR existing_constraint.connoinherit OR existing_constraint.conkey IS DISTINCT FROM ARRAY[
        (SELECT attnum FROM pg_attribute WHERE attrelid='credeals.cre_listings'::regclass AND attname='elevator_count' AND NOT attisdropped)
      ]::smallint[] THEN RAISE EXCEPTION 'incompatible existing elevator count constraint'; END IF;
  ELSE
    ALTER TABLE credeals.cre_listings ADD CONSTRAINT cre_listings_elevator_count_nonnegative CHECK(elevator_count >= 0) NOT VALID;
  END IF;
END $$;
COMMENT ON COLUMN credeals.cre_listings.elevator_count IS
  'Nullable source-reported elevator count when the source unambiguously states a count. Zero is a reported zero; NULL is unknown, missing, invalid or ambiguous. Not building floors. Retain original evidence and source observation provenance.';
COMMENT ON COLUMN credeals.cre_listings.typical_floor_size_text IS
  'Nullable source-stated typical floor size literal. Preserve its stated unit and context; no square-foot unit, numeric interpretation, building size or canonical measurement is inferred. NULL means unknown, missing, invalid or ambiguous. Retain original evidence and source observation provenance.';
COMMIT;
-- Rollback is a separate reviewed decision. Stop new writers/readers, assess
-- dependencies and preserve/export populated values. Retain preexisting columns
-- and restore their prior comments; dropping added columns destroys data and
-- requires separate explicit approval. No automatic DROP is authorized.
