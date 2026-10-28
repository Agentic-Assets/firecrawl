-- Part of AGENTIC-2895. Full facts-text trigram search for private evidence.
-- Excluded from 000_run_all.sql. This migration never installs pg_trgm,
-- changes evidence data, grants application access or changes RLS policies.
-- Reversal is a separately reviewed DROP INDEX of this index only; never drop
-- the evidence table. A non-concurrent build takes a table lock and may block
-- writes; use the bounded maintenance window and read back before/after.
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
 AND :'CRE_SOURCE_EVIDENCE_CONFIRM' = 'APPLY 020_cre_source_evidence_fact_text_index') AS authorized \gset
\if :authorized
\else
  DO $$ BEGIN RAISE EXCEPTION '020 requires operator, issue and exact confirmation'; END $$;
\endif

BEGIN;
SET LOCAL search_path = pg_catalog;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
DO $$
DECLARE role_name text;
DECLARE owner_oid oid := (SELECT oid FROM pg_roles WHERE rolname='cre_source_evidence_owner');
DECLARE writer_oid oid := (SELECT oid FROM pg_roles WHERE rolname='cre_source_evidence_writer');
DECLARE reader_oid oid := (SELECT oid FROM pg_roles WHERE rolname='cre_source_evidence_reader');
DECLARE events_oid oid := (SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='cre_source_evidence' AND c.relname='events');
DECLARE function_oid oid := (SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='cre_source_evidence' AND p.proname='append_event' AND oidvectortypes(p.proargtypes)='text, integer, text, text, timestamp with time zone, text, jsonb, jsonb, text, text, text, text');
BEGIN
  IF current_user <> session_user THEN
    RAISE EXCEPTION 'migration requires a direct executor session';
  END IF;
  FOREACH role_name IN ARRAY ARRAY['cre_source_evidence_owner','cre_source_evidence_writer','cre_source_evidence_reader'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=role_name AND NOT rolcanlogin AND NOT rolinherit
       AND NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication AND NOT rolbypassrls)
      OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=role_name)
      OR (SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid
          WHERE r.rolname=role_name AND m.member=(SELECT oid FROM pg_roles WHERE rolname=current_user)
            AND m.admin_option AND NOT m.inherit_option AND NOT m.set_option) <> 1
      OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid
          WHERE r.rolname=role_name AND (m.member<>(SELECT oid FROM pg_roles WHERE rolname=current_user)
            OR NOT m.admin_option OR m.inherit_option OR m.set_option)) THEN
      RAISE EXCEPTION 'missing or unsafe evidence role/membership: %', role_name;
    END IF;
  END LOOP;
  IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname='cre_source_evidence' AND nspowner=owner_oid)
    OR NOT EXISTS (SELECT 1 FROM pg_class WHERE oid=events_oid AND relkind='r' AND relowner=owner_oid
                   AND relrowsecurity AND relforcerowsecurity)
    OR NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid=events_oid AND attname='facts'
                   AND atttypid='jsonb'::regtype AND attnotnull AND NOT attisdropped)
    OR NOT EXISTS (SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace
                   WHERE e.extname='pg_trgm' AND e.extversion='1.6' AND n.nspname='public')
    OR NOT EXISTS (SELECT 1 FROM pg_opclass o JOIN pg_namespace n ON n.oid=o.opcnamespace
                   JOIN pg_am a ON a.oid=o.opcmethod
                   JOIN pg_depend d ON d.classid='pg_opclass'::regclass AND d.objid=o.oid AND d.deptype='e'
                   JOIN pg_extension e ON e.oid=d.refobjid AND d.refclassid='pg_extension'::regclass
                   WHERE n.nspname='public' AND o.opcname='gin_trgm_ops'
                   AND a.amname='gin' AND o.opcintype='text'::regtype
                   AND e.extname='pg_trgm' AND e.extversion='1.6'
                   AND has_schema_privilege(owner_oid,n.oid,'USAGE')) THEN
    RAISE EXCEPTION 'evidence table, RLS, owner or pg_trgm contract mismatch';
  END IF;
  IF (SELECT count(*) FROM pg_policy WHERE polrelid=events_oid) <> 3
    OR NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid=events_oid AND polname='cre_source_evidence_owner_select'
                   AND polcmd='r' AND polpermissive AND polroles=ARRAY[owner_oid] AND pg_get_expr(polqual,polrelid)='true' AND polwithcheck IS NULL)
    OR NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid=events_oid AND polname='cre_source_evidence_owner_insert'
                   AND polcmd='a' AND polpermissive AND polroles=ARRAY[owner_oid] AND polqual IS NULL AND pg_get_expr(polwithcheck,polrelid)='true')
    OR NOT EXISTS (SELECT 1 FROM pg_policy WHERE polrelid=events_oid AND polname='cre_source_evidence_reader_select'
                   AND polcmd='r' AND polpermissive AND polroles=ARRAY[reader_oid] AND pg_get_expr(polqual,polrelid)='true' AND polwithcheck IS NULL) THEN
    RAISE EXCEPTION 'evidence policy contract mismatch';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_namespace n, LATERAL aclexplode(n.nspacl) a WHERE n.nspname='cre_source_evidence'
      AND (a.grantee NOT IN (owner_oid,writer_oid,reader_oid) OR (a.grantee IN (writer_oid,reader_oid) AND (a.privilege_type <> 'USAGE' OR a.is_grantable))))
    OR EXISTS (SELECT 1 FROM pg_class c, LATERAL aclexplode(c.relacl) a WHERE c.oid=events_oid
      AND (a.grantee NOT IN (owner_oid,reader_oid) OR (a.grantee=reader_oid AND (a.privilege_type <> 'SELECT' OR a.is_grantable))))
    OR EXISTS (SELECT 1 FROM pg_proc p, LATERAL aclexplode(p.proacl) a
      WHERE p.oid=function_oid
      AND (a.grantee NOT IN (owner_oid,writer_oid) OR (a.grantee=writer_oid AND (a.privilege_type <> 'EXECUTE' OR a.is_grantable))))
    OR NOT EXISTS (SELECT 1 FROM pg_proc p WHERE p.oid=function_oid
        AND p.proowner=owner_oid AND p.prosecdef AND p.prokind='f' AND p.prorettype='text'::regtype
        AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')
        AND p.proconfig=ARRAY['search_path=pg_catalog'])
    OR EXISTS (SELECT 1 FROM pg_attribute a, LATERAL aclexplode(a.attacl) x WHERE a.attrelid=events_oid
        AND (x.grantee NOT IN (owner_oid,reader_oid) OR (x.grantee=reader_oid AND (x.privilege_type <> 'SELECT' OR x.is_grantable))))
    OR (SELECT count(*) FROM pg_namespace n, LATERAL aclexplode(n.nspacl) a
        WHERE n.nspname='cre_source_evidence' AND a.grantee IN (writer_oid,reader_oid)
          AND a.privilege_type='USAGE' AND NOT a.is_grantable) <> 2
    OR NOT EXISTS (SELECT 1 FROM pg_class c, LATERAL aclexplode(c.relacl) a
        WHERE c.oid=events_oid AND a.grantee=reader_oid AND a.privilege_type='SELECT' AND NOT a.is_grantable)
    OR NOT EXISTS (SELECT 1 FROM pg_proc p, LATERAL aclexplode(p.proacl) a
        WHERE p.oid=function_oid AND a.grantee=writer_oid AND a.privilege_type='EXECUTE' AND NOT a.is_grantable) THEN
    RAISE EXCEPTION 'evidence ACL or append function contract mismatch';
  END IF;
END $$;
DO $$ BEGIN
  EXECUTE format('GRANT cre_source_evidence_owner TO %I WITH INHERIT FALSE, SET TRUE', current_user);
END $$;
SET LOCAL ROLE cre_source_evidence_owner;
CREATE INDEX IF NOT EXISTS cre_source_evidence_facts_text_trgm_idx
  ON cre_source_evidence.events USING gin ((facts::text) public.gin_trgm_ops);
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    JOIN pg_index i ON i.indexrelid=c.oid JOIN pg_am a ON a.oid=c.relam
    JOIN pg_opclass o ON o.oid=i.indclass[0] JOIN pg_namespace opn ON opn.oid=o.opcnamespace
    WHERE n.nspname='cre_source_evidence' AND c.relname='cre_source_evidence_facts_text_trgm_idx'
      AND c.relkind='i' AND c.relowner=(SELECT oid FROM pg_roles WHERE rolname='cre_source_evidence_owner')
      AND i.indrelid='cre_source_evidence.events'::regclass AND a.amname='gin'
      AND i.indisvalid AND i.indisready AND i.indislive AND NOT i.indisunique
      AND i.indnatts=1 AND i.indnkeyatts=1 AND i.indkey[0]=0 AND i.indpred IS NULL
      AND c.reloptions IS NULL AND c.reltablespace=0 AND i.indoption[0]=0
      AND i.indcollation[0]=(SELECT typcollation FROM pg_type WHERE oid='text'::regtype)
      AND opn.nspname='public' AND o.opcname='gin_trgm_ops'
      AND pg_get_expr(i.indexprs,i.indrelid)='(facts)::text'
  ) THEN RAISE EXCEPTION 'existing evidence trigram index differs from contract'; END IF;
END $$;
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
