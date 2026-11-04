"""Run the actual 019 migration against an owned, disposable PostgreSQL cluster."""
import argparse
import hashlib
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from owned_observation_fixture_cluster import OwnedCluster, tool

HERE = Path(__file__).resolve().parent
MIGRATION = HERE.parents[2] / "sql/019_cre_source_evidence.sql"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-disposable-fixture", action="store_true", required=True)
    parser.parse_args()
    initdb, pg_ctl, psql = (tool(name) for name in ("initdb", "pg_ctl", "psql"))
    database = "cre_evidence_fixture_" + uuid.uuid4().hex[:12]
    with OwnedCluster(initdb, pg_ctl, port=55443) as cluster:
        def sql(statement, target=database, check=True, user=None):
            result = subprocess.run(
                [psql, "-X", "-qAt", "-h", str(cluster.root), "-p", "55443", "-d", target,
                 "-v", "ON_ERROR_STOP=1"] + (["-U", user] if user else []), input=statement, capture_output=True, text=True,
                timeout=45, check=False, env=cluster.env,
            )
            if check and result.returncode:
                raise AssertionError(result.stderr[-2000:])
            return result

        sql("CREATE ROLE evidence_executor LOGIN CREATEROLE CREATEDB BYPASSRLS;", target="postgres")
        sql(f"CREATE DATABASE {database} OWNER evidence_executor;", target="postgres")
        sql("CREATE ROLE cre_source_evidence_owner NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_writer NOLOGIN NOINHERIT; CREATE ROLE cre_source_evidence_reader NOLOGIN NOINHERIT;", target="postgres", user="evidence_executor")
        migration = MIGRATION.read_text()
        assert sql(migration, check=False).returncode != 0
        sql("CREATE FUNCTION public.sha256(bytea) RETURNS bytea LANGUAGE sql IMMUTABLE AS $$ SELECT pg_catalog.decode(pg_catalog.repeat('00',32),'hex') $$; CREATE FUNCTION public.to_tsvector(pg_catalog.regconfig,text) RETURNS pg_catalog.tsvector LANGUAGE sql IMMUTABLE AS $$ SELECT 'hijacked'::pg_catalog.tsvector $$;")
        assert sql("SET search_path=public,pg_catalog; SELECT encode(sha256(convert_to('Synthetic text','UTF8')),'hex') = repeat('0',64), to_tsvector('pg_catalog.simple'::regconfig,'Synthetic text') = 'hijacked'::tsvector;").stdout.strip() == "t|t"
        approved = ("SET search_path=public,pg_catalog;\n\\set CRE_SOURCE_EVIDENCE_APPROVE 1\n\\set CRE_SOURCE_EVIDENCE_OPERATOR cayman\n"
                    "\\set CRE_SOURCE_EVIDENCE_APPROVAL_REF AGENTIC-2895\n"
                    "\\set CRE_SOURCE_EVIDENCE_CONFIRM 'APPLY 019_cre_source_evidence'\n") + migration
        sql("CREATE ROLE evidence_unprivileged NOLOGIN; ALTER DEFAULT PRIVILEGES FOR ROLE cre_source_evidence_owner GRANT SELECT ON TABLES TO evidence_unprivileged;")
        assert sql(approved, check=False, user="evidence_executor").returncode != 0
        assert sql("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid WHERE r.rolname='cre_source_evidence_owner' AND m.member='evidence_executor'::regrole AND (m.inherit_option OR m.set_option);").stdout.strip() == "0"
        sql("ALTER DEFAULT PRIVILEGES FOR ROLE cre_source_evidence_owner REVOKE SELECT ON TABLES FROM evidence_unprivileged;")
        sql("CREATE ROLE evidence_bridge NOLOGIN; GRANT cre_source_evidence_owner TO evidence_bridge; GRANT evidence_bridge TO evidence_unprivileged;")
        failure = sql(approved, check=False, user="evidence_executor")
        assert failure.returncode != 0 and "role has unexpected members at migration" in failure.stderr
        sql("REVOKE cre_source_evidence_owner FROM evidence_bridge;")
        sql("GRANT cre_source_evidence_writer TO evidence_unprivileged;")
        assert sql(approved, check=False, user="evidence_executor").returncode != 0
        sql("REVOKE cre_source_evidence_writer FROM evidence_unprivileged;")
        sql("GRANT cre_source_evidence_reader TO evidence_unprivileged;")
        assert sql(approved, check=False, user="evidence_executor").returncode != 0
        sql("REVOKE cre_source_evidence_reader FROM evidence_unprivileged;")
        sql("DROP ROLE cre_source_evidence_owner, cre_source_evidence_writer, cre_source_evidence_reader;")
        assert sql("SELECT count(*) FROM pg_roles WHERE rolname IN ('cre_source_evidence_owner','cre_source_evidence_writer','cre_source_evidence_reader');").stdout.strip() == "0"
        # A collision after role creation rolls back all new roles and grants.
        sql("CREATE SCHEMA cre_source_evidence;")
        assert sql(approved, check=False, user="evidence_executor").returncode != 0
        assert sql("SELECT count(*) FROM pg_roles WHERE rolname IN ('cre_source_evidence_owner','cre_source_evidence_writer','cre_source_evidence_reader');").stdout.strip() == "0"
        assert sql("SELECT to_regnamespace('cre_source_evidence') IS NOT NULL;").stdout.strip() == "t"
        sql("DROP SCHEMA cre_source_evidence;")
        sql("CREATE FUNCTION public.reject_fixture_function() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN IF EXISTS (SELECT 1 FROM pg_event_trigger_ddl_commands() WHERE schema_name='cre_source_evidence') THEN RAISE EXCEPTION 'synthetic DDL failure'; END IF; END $$; CREATE EVENT TRIGGER reject_fixture_function ON ddl_command_end WHEN TAG IN ('CREATE FUNCTION') EXECUTE FUNCTION public.reject_fixture_function();")
        failure = sql(approved, check=False, user="evidence_executor")
        assert failure.returncode != 0 and "synthetic DDL failure" in failure.stderr
        assert sql("SELECT to_regnamespace('cre_source_evidence') IS NULL;").stdout.strip() == "t"
        assert sql("SELECT count(*) FROM pg_roles WHERE rolname IN ('cre_source_evidence_owner','cre_source_evidence_writer','cre_source_evidence_reader');").stdout.strip() == "0"
        sql("DROP EVENT TRIGGER reject_fixture_function; DROP FUNCTION public.reject_fixture_function();")
        sql("CREATE SCHEMA credeals; CREATE TABLE credeals.cre_listings(id integer, status text, deleted_at timestamptz); INSERT INTO credeals.cre_listings VALUES (1,'active',NULL);")
        # The actual DDL runs as a non-superuser with production-like powers.
        sql(approved, user="evidence_executor")
        assert sql("SELECT count(*) FROM pg_depend d JOIN pg_proc p ON p.oid=d.refobjid JOIN pg_namespace n ON n.oid=p.pronamespace WHERE d.refclassid='pg_proc'::regclass AND n.nspname='public' AND ((d.classid='pg_attrdef'::regclass AND d.objid IN (SELECT oid FROM pg_attrdef WHERE adrelid='cre_source_evidence.events'::regclass)) OR (d.classid='pg_constraint'::regclass AND d.objid IN (SELECT oid FROM pg_constraint WHERE conrelid='cre_source_evidence.events'::regclass)));").stdout.strip() == "0"
        assert sql("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid WHERE r.rolname LIKE 'cre_source_evidence_%' AND m.member='evidence_executor'::regrole AND m.admin_option AND NOT m.inherit_option AND NOT m.set_option;").stdout.strip() == "3"
        assert sql("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.roleid WHERE r.rolname LIKE 'cre_source_evidence_%' AND m.member='evidence_executor'::regrole AND (m.inherit_option OR m.set_option);").stdout.strip() == "0"
        assert sql("SET ROLE cre_source_evidence_owner;", user="evidence_executor", check=False).returncode != 0
        assert sql(approved, check=False, user="evidence_executor").returncode != 0
        digest = hashlib.sha256(b"Synthetic text").hexdigest()

        def append(capture, value="1", digest_value=digest):
            return ("SET ROLE cre_source_evidence_writer; SELECT cre_source_evidence.append_event("
                    f"'{capture}',0,'example',NULL,'2026-01-01T00:00:00Z',NULL,"
                    "'{\"method\":\"synthetic\"}'::jsonb,"
                    f"'[[\"area\",{value}],[\"area\",2]]'::jsonb,"
                    f"'https://example.test/doc','extractor-v1','Synthetic text','{digest_value}');")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: sql(append("capture-a")).stdout.strip(), range(2)))
        assert sorted(results) == ["inserted", "unchanged"]
        assert sql(append("capture-a", "3"), check=False).returncode != 0
        assert sql(append("bad-hash", digest_value="0" * 64), check=False).returncode != 0
        sql(append("capture-b", "3"))
        assert sql("SELECT count(*), bool_and(external_id IS NULL), count(DISTINCT facts) FROM cre_source_evidence.events;").stdout.strip() == "2|t|2"
        assert sql("SELECT has_table_privilege('cre_source_evidence_writer','cre_source_evidence.events','SELECT,INSERT,UPDATE,DELETE');").stdout.strip() == "f"
        assert sql("SET ROLE cre_source_evidence_writer; SELECT * FROM cre_source_evidence.events;", check=False).returncode != 0
        assert sql(append("forbidden").replace("SET ROLE cre_source_evidence_writer", "SET ROLE evidence_unprivileged"), check=False).returncode != 0
        sql("CREATE ROLE evidence_admitted LOGIN; GRANT cre_source_evidence_writer TO evidence_admitted;")
        assert sql(append("admitted").replace("SET ROLE cre_source_evidence_writer", "SET ROLE evidence_admitted")).stdout.strip() == "inserted"
        assert sql("SET ROLE evidence_admitted; SELECT * FROM cre_source_evidence.events;", check=False).returncode != 0
        sql("CREATE ROLE evidence_reader LOGIN; GRANT cre_source_evidence_reader TO evidence_reader;")
        assert sql("SET ROLE evidence_reader; SELECT count(*) FROM cre_source_evidence.events WHERE text_search @@ phraseto_tsquery('pg_catalog.simple', 'Synthetic text');").stdout.strip() == "3"
        assert sql("SET ROLE evidence_reader; SELECT count(*) FROM cre_source_evidence.events WHERE text_search @@ phraseto_tsquery('pg_catalog.simple', 'text synthetic');").stdout.strip() == "0"
        assert sql("SET ROLE evidence_reader; SELECT count(*) FROM cre_source_evidence.events WHERE facts @> '[[\"area\",1]]'::jsonb;").stdout.strip() == "2"
        assert sql("SET ROLE evidence_reader; SELECT count(*) FROM cre_source_evidence.events WHERE facts = '[[\"area\",1],[\"area\",2]]'::jsonb;").stdout.strip() == "2"
        assert sql("SET ROLE evidence_reader; DELETE FROM cre_source_evidence.events;", check=False).returncode != 0
        assert sql(append("reader-write").replace("SET ROLE cre_source_evidence_writer", "SET ROLE evidence_reader"), check=False).returncode != 0
        assert sql("SELECT am.amname FROM pg_class c JOIN pg_am am ON am.oid=c.relam WHERE c.oid='cre_source_evidence.cre_source_evidence_text_search_idx'::regclass;").stdout.strip() == "gin"
        assert "USING gin (facts jsonb_path_ops)" in sql("SELECT pg_get_indexdef('cre_source_evidence.cre_source_evidence_facts_idx'::regclass);").stdout
        assert sql("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid='cre_source_evidence.events'::regclass;").stdout.strip() == "t"
        assert sql("SET ROLE cre_source_evidence_owner; WITH changed AS (UPDATE cre_source_evidence.events SET source_key='changed' RETURNING 1) SELECT count(*) FROM changed;").stdout.strip() == "0"
        assert sql("SET ROLE cre_source_evidence_owner; WITH changed AS (DELETE FROM cre_source_evidence.events RETURNING 1) SELECT count(*) FROM changed;").stdout.strip() == "0"
        oversized = append("oversized").replace("'Synthetic text'", "repeat('x',65537)")
        assert sql(oversized, check=False).returncode != 0
        sql("BEGIN; " + append("rollback") + " ROLLBACK;")
        assert sql("SELECT count(*) FROM cre_source_evidence.events;").stdout.strip() == "3"
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(sql, append("race", value), check=False) for value in ("1", "3")]
            assert sorted(job.result().returncode == 0 for job in pending) == [False, True]
        assert sql("SELECT id,status,deleted_at IS NULL FROM credeals.cre_listings;").stdout.strip() == "1|active|t"
        print("PASS: non-superuser apply, restricted executor memberships, search, ACL, replay, size and rollback")


if __name__ == "__main__":
    main()
