-- Part of AGENTIC-2895. Restricted source-native evidence, not a canonical
-- listing, lifecycle, normalized fact, or OM extraction writer. Excluded from
-- 000_run_all.sql. PostgreSQL 16+ required. A non-superuser CREATEROLE executor
-- creates missing roles inside this transaction, receiving automatic ADMIN-only
-- membership (INHERIT FALSE, SET FALSE). This migration temporarily grants SET
-- on the owner role, then revokes that extra grant before COMMIT. The
-- bootstrap-granted ADMIN-only memberships remain. The executor is a trusted
-- administrator and can use CREATEROLE/ADMIN to regain role privileges.
-- Source-specific admission, withheld-value handling, retention, consumers,
-- ownership-contract registration, live catalog/ACL review and rollback are
-- separate prerequisites to use; this migration grants no login membership.
\set ON_ERROR_STOP on
\if :{?CRE_SOURCE_EVIDENCE_APPROVE}
\else
  \set CRE_SOURCE_EVIDENCE_APPROVE 0
\endif
\if :{?CRE_SOURCE_EVIDENCE_OPERATOR}
\else
  \set CRE_SOURCE_EVIDENCE_OPERATOR ''
\endif
\if :{?CRE_SOURCE_EVIDENCE_APPROVAL_REF}
\else
  \set CRE_SOURCE_EVIDENCE_APPROVAL_REF ''
\endif
\if :{?CRE_SOURCE_EVIDENCE_CONFIRM}
\else
  \set CRE_SOURCE_EVIDENCE_CONFIRM ''
\endif
SELECT (:'CRE_SOURCE_EVIDENCE_APPROVE' = '1'
 AND :'CRE_SOURCE_EVIDENCE_OPERATOR' IN ('cayman','stace')
 AND :'CRE_SOURCE_EVIDENCE_APPROVAL_REF' ~ '^AGENTIC-[0-9]+$'
 AND :'CRE_SOURCE_EVIDENCE_CONFIRM' = 'APPLY 019_cre_source_evidence') AS authorized \gset
\if :authorized
\else
  DO $$ BEGIN RAISE EXCEPTION '019 requires operator, issue and exact confirmation'; END $$;
\endif

BEGIN;
SET LOCAL search_path = pg_catalog;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
DO $$
DECLARE role_name text;
BEGIN
  IF current_user <> session_user THEN
    RAISE EXCEPTION 'migration requires a direct executor session';
  END IF;
  FOREACH role_name IN ARRAY ARRAY['cre_source_evidence_owner','cre_source_evidence_writer','cre_source_evidence_reader'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', role_name);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=role_name AND NOT rolcanlogin
      AND NOT rolinherit AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls) THEN
      RAISE EXCEPTION 'missing or unsafe role: %', role_name;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=role_name) THEN
      RAISE EXCEPTION 'role must not inherit privileges: %', role_name;
    END IF;
    -- Only the executor's restricted automatic ADMIN membership is allowed.
    -- Any other inbound edge can confer privileges directly or transitively.
    IF EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid
      WHERE r.rolname=role_name AND NOT (m.member=(SELECT oid FROM pg_roles WHERE rolname=current_user)
        AND m.admin_option AND NOT m.inherit_option AND NOT m.set_option)) THEN
      RAISE EXCEPTION 'role has unexpected members at migration: %', role_name;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid
      WHERE r.rolname=role_name AND m.member=(SELECT oid FROM pg_roles WHERE rolname=current_user)
      AND m.admin_option AND NOT m.inherit_option AND NOT m.set_option) THEN
      RAISE EXCEPTION 'executor needs restricted admin membership: %', role_name;
    END IF;
  END LOOP;
END $$;
DO $$ BEGIN
  EXECUTE format('GRANT cre_source_evidence_owner TO %I WITH INHERIT FALSE, SET TRUE', current_user);
END $$;
-- Deliberately refuse an existing namespace; never take over preexisting ACLs.
CREATE SCHEMA cre_source_evidence AUTHORIZATION cre_source_evidence_owner;
SET LOCAL ROLE cre_source_evidence_owner;
REVOKE ALL ON SCHEMA cre_source_evidence FROM PUBLIC;
CREATE TABLE cre_source_evidence.events (
  capture_id text NOT NULL CHECK (capture_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
  item_no integer NOT NULL CHECK (item_no BETWEEN 0 AND 1000000),
  source_key text NOT NULL CHECK (source_key ~ '^[a-z0-9][a-z0-9_-]{0,127}$'),
  external_id text CHECK (external_id IS NULL OR octet_length(external_id) BETWEEN 1 AND 1024),
  observed_at timestamptz NOT NULL,
  source_url text CHECK (source_url IS NULL OR octet_length(source_url) BETWEEN 1 AND 4096),
  provenance jsonb NOT NULL CHECK (jsonb_typeof(provenance) = 'object' AND octet_length(provenance::text) <= 16384),
  facts jsonb NOT NULL CHECK (jsonb_typeof(facts) = 'array' AND jsonb_array_length(facts) <= 10000 AND octet_length(facts::text) <= 4194304),
  document_url text CHECK (document_url IS NULL OR octet_length(document_url) BETWEEN 1 AND 4096),
  extractor_version text CHECK (extractor_version IS NULL OR octet_length(extractor_version) BETWEEN 1 AND 1024),
  extracted_text text CHECK (extracted_text IS NULL OR octet_length(extracted_text) <= 65536),
  text_sha256 text CHECK (text_sha256 IS NULL OR text_sha256 ~ '^[0-9a-f]{64}'),
  text_search tsvector GENERATED ALWAYS AS (to_tsvector('pg_catalog.simple'::regconfig, coalesce(extracted_text, ''))) STORED,
  received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (capture_id,item_no),
  CHECK ((extracted_text IS NULL AND text_sha256 IS NULL AND extractor_version IS NULL AND document_url IS NULL)
      OR (extracted_text IS NOT NULL AND text_sha256 IS NOT NULL AND extractor_version IS NOT NULL AND document_url IS NOT NULL
          AND text_sha256 = encode(sha256(convert_to(extracted_text,'UTF8')),'hex')))
);
ALTER TABLE cre_source_evidence.events OWNER TO cre_source_evidence_owner;
REVOKE ALL ON cre_source_evidence.events FROM PUBLIC, cre_source_evidence_writer;
GRANT SELECT ON cre_source_evidence.events TO cre_source_evidence_reader;
CREATE INDEX cre_source_evidence_text_search_idx ON cre_source_evidence.events USING gin (text_search);
CREATE INDEX cre_source_evidence_facts_idx ON cre_source_evidence.events USING gin (facts jsonb_path_ops);
ALTER TABLE cre_source_evidence.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE cre_source_evidence.events FORCE ROW LEVEL SECURITY;
-- The non-login function owner may insert and read solely to validate retries.
-- There is no update or delete policy. Database superusers, BYPASSRLS roles,
-- and administrators able to change ownership or policy remain trusted.
CREATE POLICY cre_source_evidence_owner_select ON cre_source_evidence.events
  FOR SELECT TO cre_source_evidence_owner USING (true);
CREATE POLICY cre_source_evidence_owner_insert ON cre_source_evidence.events
  FOR INSERT TO cre_source_evidence_owner WITH CHECK (true);
CREATE POLICY cre_source_evidence_reader_select ON cre_source_evidence.events
  FOR SELECT TO cre_source_evidence_reader USING (true);
CREATE FUNCTION cre_source_evidence.append_event(
  p_capture_id text, p_item_no integer, p_source_key text, p_external_id text,
  p_observed_at timestamptz, p_source_url text, p_provenance jsonb, p_facts jsonb,
  p_document_url text, p_extractor_version text, p_extracted_text text, p_text_sha256 text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
DECLARE existing cre_source_evidence.events%ROWTYPE;
BEGIN
  -- Reject oversized inputs before insertion and before expensive conversions.
  IF p_provenance IS NULL OR pg_column_size(p_provenance)>16384 OR p_facts IS NULL
     OR pg_column_size(p_facts)>4194304 OR octet_length(p_extracted_text)>65536 THEN
    RAISE EXCEPTION 'evidence exceeds bounds or required value is null';
  END IF;
  INSERT INTO cre_source_evidence.events (capture_id,item_no,source_key,external_id,observed_at,source_url,
    provenance,facts,document_url,extractor_version,extracted_text,text_sha256)
  VALUES (p_capture_id,p_item_no,p_source_key,p_external_id,p_observed_at,p_source_url,
    p_provenance,p_facts,p_document_url,p_extractor_version,p_extracted_text,p_text_sha256)
  ON CONFLICT (capture_id,item_no) DO NOTHING;
  IF FOUND THEN RETURN 'inserted'; END IF;
  SELECT * INTO STRICT existing FROM cre_source_evidence.events WHERE capture_id=p_capture_id AND item_no=p_item_no;
  IF ROW(existing.source_key,existing.external_id,existing.observed_at,existing.source_url,existing.provenance,
         existing.facts,existing.document_url,existing.extractor_version,existing.extracted_text,existing.text_sha256)
     IS DISTINCT FROM ROW(p_source_key,p_external_id,p_observed_at,p_source_url,p_provenance,
         p_facts,p_document_url,p_extractor_version,p_extracted_text,p_text_sha256) THEN
    RAISE EXCEPTION 'divergent evidence replay';
  END IF;
  RETURN 'unchanged';
END $$;
ALTER FUNCTION cre_source_evidence.append_event(text,integer,text,text,timestamptz,text,jsonb,jsonb,text,text,text,text) OWNER TO cre_source_evidence_owner;
REVOKE ALL ON FUNCTION cre_source_evidence.append_event(text,integer,text,text,timestamptz,text,jsonb,jsonb,text,text,text,text) FROM PUBLIC;
GRANT USAGE ON SCHEMA cre_source_evidence TO cre_source_evidence_writer;
GRANT USAGE ON SCHEMA cre_source_evidence TO cre_source_evidence_reader;
GRANT EXECUTE ON FUNCTION cre_source_evidence.append_event(text,integer,text,text,timestamptz,text,jsonb,jsonb,text,text,text,text) TO cre_source_evidence_writer;
-- Default ACLs of the migration executor can add grants at object creation.
-- Fail the transaction rather than silently expose evidence through them.
DO $$
DECLARE owner_oid oid := 'cre_source_evidence_owner'::regrole;
DECLARE writer_oid oid := 'cre_source_evidence_writer'::regrole;
DECLARE reader_oid oid := 'cre_source_evidence_reader'::regrole;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_namespace n, LATERAL aclexplode(n.nspacl) a
      WHERE n.nspname='cre_source_evidence'
      AND (a.grantee NOT IN (owner_oid,writer_oid,reader_oid) OR (a.grantee IN (writer_oid,reader_oid) AND (a.privilege_type <> 'USAGE' OR a.is_grantable))))
    OR EXISTS (SELECT 1 FROM pg_class c, LATERAL aclexplode(c.relacl) a
      WHERE c.oid='cre_source_evidence.events'::regclass
      AND (a.grantee NOT IN (owner_oid,reader_oid) OR (a.grantee=reader_oid AND (a.privilege_type <> 'SELECT' OR a.is_grantable))))
    OR EXISTS (SELECT 1 FROM pg_proc p, LATERAL aclexplode(p.proacl) a
      WHERE p.oid='cre_source_evidence.append_event(text,integer,text,text,timestamptz,text,jsonb,jsonb,text,text,text,text)'::regprocedure
      AND (a.grantee NOT IN (owner_oid,writer_oid) OR (a.grantee=writer_oid AND (a.privilege_type <> 'EXECUTE' OR a.is_grantable)))) THEN
    RAISE EXCEPTION 'unexpected default object privileges';
  END IF;
END $$;
-- Remove the executor-issued SET grant, leaving only the bootstrap-issued
-- ADMIN-only memberships. These retain administrative power, not SET/INHERIT.
RESET ROLE;
DO $$ DECLARE role_name text; BEGIN
  EXECUTE format('REVOKE cre_source_evidence_owner FROM %I', current_user);
  FOREACH role_name IN ARRAY ARRAY['cre_source_evidence_owner','cre_source_evidence_writer','cre_source_evidence_reader'] LOOP
    IF (SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid
      WHERE r.rolname=role_name AND m.member=(SELECT oid FROM pg_roles WHERE rolname=current_user)
      AND m.admin_option AND NOT m.inherit_option AND NOT m.set_option) <> 1
      OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid
      WHERE r.rolname=role_name AND (m.member<>(SELECT oid FROM pg_roles WHERE rolname=current_user)
        OR NOT m.admin_option OR m.inherit_option OR m.set_option)) THEN
      RAISE EXCEPTION 'unexpected membership remains: %', role_name;
    END IF;
  END LOOP;
END $$;
COMMIT;
-- Never drop populated evidence as an automatic rollback. Stop access and
-- preserve evidence; any export, retention or destruction requires review.
