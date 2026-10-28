-- 017_cre_available_space_count.sql
-- Psql only. Excluded from 000_run_all.sql. This is a review candidate, not
-- live deployment or authorization to backfill or switch writers.
-- Required approval: schema owner, current catalog/ACL/dependency review,
-- named issue and exact attestation by Cayman or Stace.
\set ON_ERROR_STOP on
\if :{?CRE_APPROVE_AVAILABLE_SPACE_COUNT}
\else
  \set CRE_APPROVE_AVAILABLE_SPACE_COUNT 0
\endif
\if :{?CRE_AVAILABLE_SPACE_COUNT_OPERATOR}
\else
  \set CRE_AVAILABLE_SPACE_COUNT_OPERATOR ''
\endif
\if :{?CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF}
\else
  \set CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF ''
\endif
\if :{?CRE_AVAILABLE_SPACE_COUNT_CONFIRM}
\else
  \set CRE_AVAILABLE_SPACE_COUNT_CONFIRM ''
\endif
SELECT (
  :'CRE_APPROVE_AVAILABLE_SPACE_COUNT' = '1'
  AND :'CRE_AVAILABLE_SPACE_COUNT_OPERATOR' IN ('cayman', 'stace')
  AND :'CRE_AVAILABLE_SPACE_COUNT_APPROVAL_REF' ~ '^AGENTIC-[0-9]+$'
  AND :'CRE_AVAILABLE_SPACE_COUNT_CONFIRM' = 'APPLY 017_cre_available_space_count'
) AS cre_available_space_count_authorized \gset
\if :cre_available_space_count_authorized
  \echo 'Approved migration 017 available-space contract requested'
\else
  \echo 'REFUSED: migration 017 requires the documented operator, issue and exact confirmation'
  DO $$ BEGIN RAISE EXCEPTION 'migration 017 requires the approval-gated psql contract'; END $$;
\endif

-- DRAFT FOR REVIEW ONLY. Not applied. Owner: Firecrawl listing lifecycle.
-- Gate: named owner issue, reviewed branch, current catalog/dependency/ACL and
-- migration-ledger readback, expected delta/timeouts and forward/rollback plan,
-- plus separate explicit approval for production DDL.
-- Review effective table- and column-level privileges, roles, views and RLS:
-- table-level SELECT grants can expose newly added columns automatically.
-- This change does not authorize a backfill, status mutation, or product rollout.

BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
-- Acquire the DDL lock before inspecting catalog state. A concurrent schema
-- change cannot slip between the preflight and the subsequent ALTER TABLE.
LOCK TABLE credeals.cre_listings IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE
  existing_column record;
  existing_constraint record;
BEGIN
  SELECT a.atttypid, a.attnotnull, a.attidentity, a.attgenerated,
         EXISTS (SELECT 1 FROM pg_attrdef d WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum) AS has_default
    INTO existing_column
    FROM pg_attribute a
   WHERE a.attrelid = 'credeals.cre_listings'::regclass
     AND a.attname = 'available_space_count' AND NOT a.attisdropped;
  IF FOUND THEN
    IF existing_column.atttypid <> 'integer'::regtype
       OR existing_column.attnotnull OR existing_column.has_default
       OR existing_column.attidentity <> '' OR existing_column.attgenerated <> '' THEN
      RAISE EXCEPTION 'incompatible existing available_space_count column';
    END IF;
  ELSE
    ALTER TABLE credeals.cre_listings ADD COLUMN available_space_count integer;
  END IF;

  SELECT c.contype, pg_get_expr(c.conbin, c.conrelid) AS expression,
         c.connoinherit, c.conkey
    INTO existing_constraint
    FROM pg_constraint c
   WHERE c.conrelid = 'credeals.cre_listings'::regclass
     AND c.conname = 'cre_listings_available_space_count_nonnegative';
  IF FOUND THEN
    IF existing_constraint.contype <> 'c'
       OR existing_constraint.expression <> '(available_space_count >= 0)'
       OR existing_constraint.connoinherit
       OR existing_constraint.conkey IS DISTINCT FROM ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='credeals.cre_listings'::regclass AND attname='available_space_count' AND NOT attisdropped)]::smallint[] THEN
      RAISE EXCEPTION 'incompatible existing available_space_count constraint';
    END IF;
  ELSE
    ALTER TABLE credeals.cre_listings
      ADD CONSTRAINT cre_listings_available_space_count_nonnegative
      CHECK (available_space_count >= 0) NOT VALID;
  END IF;
  -- Any other table constraint referencing this column requires explicit
  -- review: even an apparently stricter check could reject the valid zero.
  IF EXISTS (
    SELECT 1 FROM pg_constraint c
    WHERE c.conrelid = 'credeals.cre_listings'::regclass
      AND c.conname <> 'cre_listings_available_space_count_nonnegative'
      AND (c.conkey @> ARRAY[(SELECT attnum FROM pg_attribute
        WHERE attrelid = 'credeals.cre_listings'::regclass
          AND attname = 'available_space_count' AND NOT attisdropped)]::smallint[]
        OR (c.contype = 'c' AND (c.conkey IS NULL OR 0 = ANY(c.conkey))))
  ) THEN
    RAISE EXCEPTION 'additional constraint references available_space_count';
  END IF;
  -- A unique index can constrain data without a pg_constraint entry. Include
  -- direct index keys and expression/predicate dependencies on this column.
  IF EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = 'credeals.cre_listings'::regclass AND i.indisunique
      AND (
        (SELECT attnum FROM pg_attribute
          WHERE attrelid = i.indrelid AND attname = 'available_space_count'
            AND NOT attisdropped) = ANY(i.indkey)
        OR EXISTS (
          SELECT 1 FROM pg_depend d
          WHERE d.classid = 'pg_class'::regclass AND d.objid = i.indexrelid
            AND d.refclassid = 'pg_class'::regclass AND d.refobjid = i.indrelid
            AND d.refobjsubid = (SELECT attnum FROM pg_attribute
              WHERE attrelid = i.indrelid AND attname = 'available_space_count'
                AND NOT attisdropped)
        )
      )
  ) THEN
    RAISE EXCEPTION 'unique index references available_space_count';
  END IF;
END $$;

COMMENT ON COLUMN credeals.cre_listings.available_space_count IS
  'Nullable source-reported count of available spaces, when unambiguously provided by a supported listing source. Not building units, parking spaces, status, or total building occupancy. Zero is a reported zero; NULL means absent, unsupported, invalid, or ambiguous. Preserve original evidence in extra_facts/raw_data; use source-specific parsing and retain observation provenance.';
COMMIT;

-- ROLLBACK is a separate reviewed decision. Record pre-apply column/constraint
-- existence and original comment in the deployment ledger. If the column or
-- comment predated this migration, preserve that column and restore its prior
-- comment; never blindly DROP it. Stop new readers and writers first; assess all
-- dependencies and export populated values. Dropping a column created by this
-- migration still needs separate approval and destroys its values. Preserve the
-- original JSON evidence. Do not blindly run rollback as part of this script.
