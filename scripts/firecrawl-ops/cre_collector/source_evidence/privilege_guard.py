"""Shared bounded effective-capability predicates, not credential isolation."""


def hazards_query(roles_sql):
    """Return guard hazards for existing role names supplied by a trusted SQL query."""
    return f"""WITH role_name AS ({roles_sql})
 SELECT 'schema_create' AS kind, r.name AS role, pg_catalog.format('%I', n.nspname) AS object_name
 FROM role_name r JOIN pg_catalog.pg_namespace n ON n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
 WHERE pg_catalog.has_schema_privilege(r.name, n.oid, 'CREATE')
 UNION ALL
 SELECT 'security_definer_execute', r.name, pg_catalog.format('%I.%I(%s)', n.nspname, p.proname, pg_catalog.oidvectortypes(p.proargtypes))
 FROM role_name r JOIN pg_catalog.pg_namespace n ON n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
 JOIN pg_catalog.pg_proc p ON p.pronamespace = n.oid
 WHERE p.prosecdef AND pg_catalog.has_schema_privilege(r.name, n.oid, 'USAGE')
   AND pg_catalog.has_function_privilege(r.name, p.oid, 'EXECUTE')
   AND NOT (n.nspname = 'cre_source_evidence' AND p.proname = 'append_event'
     AND pg_catalog.oidvectortypes(p.proargtypes) = 'text, integer, text, text, timestamp with time zone, text, jsonb, jsonb, text, text, text, text')
 UNION ALL
 SELECT 'relation_write', r.name, pg_catalog.format('%I.%I', n.nspname, c.relname)
 FROM role_name r JOIN pg_catalog.pg_namespace n ON n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
 JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relkind IN ('r','p','v','m','f')
 WHERE pg_catalog.has_schema_privilege(r.name, n.oid, 'USAGE') AND (
   pg_catalog.has_table_privilege(r.name, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER,MAINTAIN')
   OR pg_catalog.has_any_column_privilege(r.name, c.oid, 'INSERT,UPDATE,REFERENCES'))
 UNION ALL
 SELECT 'sequence_write', r.name, pg_catalog.format('%I.%I', n.nspname, c.relname)
 FROM role_name r JOIN pg_catalog.pg_namespace n ON n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
 JOIN pg_catalog.pg_class c ON c.relnamespace = n.oid AND c.relkind = 'S'
 WHERE pg_catalog.has_schema_privilege(r.name, n.oid, 'USAGE')
   AND pg_catalog.has_sequence_privilege(r.name, c.oid, 'USAGE,UPDATE')"""


def sql():
    """Return a fixed SQL guard to insert inside the append transaction."""
    roles = "SELECT DISTINCT rolname::text AS name FROM pg_catalog.pg_roles WHERE rolname IN (session_user, 'cre_source_evidence_writer')"
    return ("DO $evidence_privileges$ BEGIN IF NOT EXISTS "
            "(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='cre_source_evidence_writer') OR EXISTS (" +
            hazards_query(roles) + ") THEN RAISE EXCEPTION "
            "'automatic evidence refused: disallowed effective database capability'; END IF; END $evidence_privileges$;\n")
