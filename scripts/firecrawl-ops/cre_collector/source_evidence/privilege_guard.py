"""Bounded catalog guard for automatic evidence, evaluated before SET ROLE.

This detects specified effective capabilities, including PUBLIC grants. It is
not a server-side restriction on the login and cannot establish an append-only
credential: system objects, invoker routine side effects, concurrent ACL
changes, and independent connections remain outside this check.
"""


def sql():
    """Return a fixed SQL guard to insert inside the append transaction."""
    return """DO $evidence_privileges$
BEGIN
 IF EXISTS (
   SELECT 1 FROM (VALUES (session_user::text), ('cre_source_evidence_writer')) AS role_name(name)
   JOIN pg_catalog.pg_namespace n ON n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
   WHERE pg_catalog.has_schema_privilege(role_name.name, n.oid, 'CREATE')
 ) OR EXISTS (
   SELECT 1 FROM (VALUES (session_user::text), ('cre_source_evidence_writer')) AS role_name(name)
   JOIN pg_catalog.pg_namespace n ON n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
   JOIN pg_catalog.pg_proc p ON p.pronamespace = n.oid
   WHERE p.prosecdef AND pg_catalog.has_schema_privilege(role_name.name, n.oid, 'USAGE')
     AND pg_catalog.has_function_privilege(role_name.name, p.oid, 'EXECUTE')
     AND NOT (n.nspname = 'cre_source_evidence' AND p.proname = 'append_event'
       AND pg_catalog.oidvectortypes(p.proargtypes) =
         'text, integer, text, text, timestamp with time zone, text, jsonb, jsonb, text, text, text, text')
 ) OR EXISTS (
   SELECT 1 FROM (VALUES (session_user::text), ('cre_source_evidence_writer')) AS role_name(name)
   JOIN pg_catalog.pg_namespace n ON n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
   JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r','p','v','m','f')
   WHERE pg_catalog.has_schema_privilege(role_name.name, n.oid, 'USAGE') AND (
     pg_catalog.has_table_privilege(role_name.name, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
     OR pg_catalog.has_any_column_privilege(role_name.name, c.oid, 'INSERT,UPDATE,REFERENCES'))
 ) OR EXISTS (
   SELECT 1 FROM (VALUES (session_user::text), ('cre_source_evidence_writer')) AS role_name(name)
   JOIN pg_catalog.pg_namespace n ON n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
   JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relkind = 'S'
   WHERE pg_catalog.has_schema_privilege(role_name.name, n.oid, 'USAGE')
     AND pg_catalog.has_sequence_privilege(role_name.name, c.oid, 'USAGE,UPDATE')
 ) THEN
   RAISE EXCEPTION 'automatic evidence refused: disallowed effective database capability';
 END IF;
END $evidence_privileges$;
"""
