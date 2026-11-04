"""Read-only, bounded catalog inspection; not an isolation or activation proof."""
import re

from source_evidence import privilege_guard

LIMIT = 100
MAX_RESULTS_BYTES = 1024 * 1024


def sql(login=None):
    if login is not None and (not isinstance(login, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', login)):
        raise ValueError('invalid login role')
    selected = "session_user::text" if login is None else "'" + login + "'"
    roles = ("SELECT DISTINCT rolname::text AS name FROM pg_catalog.pg_roles WHERE rolname IN (" +
             selected + ", 'cre_source_evidence_writer')")
    hazards = privilege_guard.hazards_query(roles)
    return f"""BEGIN READ ONLY;
SET LOCAL search_path = pg_catalog;
SET LOCAL statement_timeout = 30000;
WITH hazards AS MATERIALIZED ({hazards}),
 roles AS (SELECT r.rolname AS name, r.rolcanlogin AS login, r.rolinherit AS inherit,
 r.rolsuper AS superuser, r.rolcreatedb AS create_database, r.rolcreaterole AS create_role,
 r.rolreplication AS replication, r.rolbypassrls AS bypass_rls FROM pg_catalog.pg_roles r
 WHERE r.rolname IN (session_user, {selected}, 'cre_source_evidence_writer')),
 extensions AS (SELECT e.extname AS name, e.extversion AS version, n.nspname AS schema
 FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid=e.extnamespace),
 databases AS (SELECT d.datname AS name FROM pg_catalog.pg_database d
 JOIN pg_catalog.pg_roles r ON r.rolname={selected}
 WHERE NOT d.datistemplate AND d.datallowconn AND pg_catalog.has_database_privilege(r.oid,d.oid,'CONNECT'))
SELECT pg_catalog.json_build_object(
 'schema_version','cre-source-evidence-target-inspection/v1',
 'assessment','bounded_catalog_only_not_activation_or_isolation_proof',
 'current_database',current_database(), 'session_login',session_user, 'selected_login',{selected},
 'writer_role_exists',EXISTS(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname='cre_source_evidence_writer'),
 'selected_role_exists',EXISTS(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname={selected}),
 'roles',COALESCE((SELECT json_agg(x) FROM (SELECT * FROM roles ORDER BY name) x),'[]'::json),
 'hazard_count',(SELECT count(*) FROM hazards),
 'hazards',COALESCE((SELECT json_agg(x) FROM (SELECT * FROM hazards ORDER BY kind,role,object_name LIMIT {LIMIT}) x),'[]'::json),
 'hazards_truncated',(SELECT count(*) > {LIMIT} FROM hazards),
 'extension_count',(SELECT count(*) FROM extensions),
 'extensions',COALESCE((SELECT json_agg(x) FROM (SELECT * FROM extensions ORDER BY name LIMIT {LIMIT}) x),'[]'::json),
 'extensions_truncated',(SELECT count(*) > {LIMIT} FROM extensions),
 'connectable_database_count',(SELECT count(*) FROM databases),
 'connectable_databases',COALESCE((SELECT json_agg(x) FROM (SELECT * FROM databases ORDER BY name LIMIT {LIMIT}) x),'[]'::json),
 'databases_truncated',(SELECT count(*) > {LIMIT} FROM databases),
 'other_databases_inspected',false,
 'server_version_num',current_setting('server_version_num'),
 'evidence_schema_exists',EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname='cre_source_evidence'),
 'events_relation_exists',pg_catalog.to_regclass('cre_source_evidence.events') IS NOT NULL,
 'fact_index_name_exists',pg_catalog.to_regclass('cre_source_evidence.cre_source_evidence_facts_text_trgm_idx') IS NOT NULL,
 'pg_trgm_available_version',(SELECT default_version FROM pg_catalog.pg_available_extensions WHERE name='pg_trgm')
);
COMMIT;
"""
