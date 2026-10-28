"""Test bounded privilege guard in an owned, disposable PostgreSQL cluster."""
import argparse
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parent / "observation_store"))
from owned_observation_fixture_cluster import OwnedCluster, tool
from source_evidence import automatic, collector, events, privilege_guard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-disposable-fixture", action="store_true", required=True)
    parser.parse_args()
    initdb, pg_ctl, psql = (tool(name) for name in ("initdb", "pg_ctl", "psql"))
    database = "cre_priv_" + uuid.uuid4().hex[:12]
    with OwnedCluster(initdb, pg_ctl, port=55447) as cluster:
        def sql(statement, *, target=database, user=None, check=True):
            result = subprocess.run(
                [psql, "-X", "-qAt", "-h", str(cluster.root), "-p", str(cluster.port),
                 "-d", target, "-v", "ON_ERROR_STOP=1"] + (["-U", user] if user else []),
                input=statement, capture_output=True, text=True, timeout=45,
                env=cluster.env, check=False,
            )
            if check and result.returncode:
                raise AssertionError("disposable SQL failed: " + result.stderr)
            return result

        sql("CREATE ROLE evidence_executor LOGIN CREATEROLE CREATEDB BYPASSRLS;", target="postgres")
        sql(f"CREATE DATABASE {database} OWNER evidence_executor;", target="postgres")
        sql("CREATE ROLE cre_source_evidence_owner NOLOGIN NOINHERIT; "
            "CREATE ROLE cre_source_evidence_writer NOLOGIN NOINHERIT; "
            "CREATE ROLE cre_source_evidence_reader NOLOGIN NOINHERIT;", user="evidence_executor")
        migration = (HERE.parents[2] / "sql/019_cre_source_evidence.sql").read_text()
        sql("\\set CRE_SOURCE_EVIDENCE_APPROVE 1\n\\set CRE_SOURCE_EVIDENCE_OPERATOR cayman\n"
            "\\set CRE_SOURCE_EVIDENCE_APPROVAL_REF AGENTIC-2895\n"
            "\\set CRE_SOURCE_EVIDENCE_CONFIRM 'APPLY 019_cre_source_evidence'\n" + migration,
            user="evidence_executor")
        sql("CREATE ROLE evidence_auto LOGIN NOINHERIT; "
            "GRANT cre_source_evidence_writer TO evidence_auto WITH INHERIT FALSE, SET TRUE; "
            "CREATE SCHEMA fixture; GRANT USAGE ON SCHEMA fixture TO PUBLIC; "
            "CREATE TABLE fixture.items(a integer,b integer); CREATE SEQUENCE fixture.seq;")
        artifact = {"runMeta": {"finishedAt": "2026-10-28T10:00:00Z"},
                    "sources": [{"sourceKey": "faris-lee"}],
                    "listings": [{"sourceKey": "faris-lee", "value": i} for i in range(78)]}
        plan = events.plan(collector.convert(artifact, source_key="faris-lee"), source_key="faris-lee")
        guarded = automatic.append_sql(plan).replace("BEGIN;\n", "BEGIN;\n" + privilege_guard.sql(), 1)

        hazards = [
            ("CREATE FUNCTION fixture.danger() RETURNS integer LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
             "DROP FUNCTION fixture.danger()"),
            ("GRANT INSERT ON fixture.items TO PUBLIC", "REVOKE INSERT ON fixture.items FROM PUBLIC"),
            ("GRANT UPDATE(a) ON fixture.items TO PUBLIC", "REVOKE UPDATE(a) ON fixture.items FROM PUBLIC"),
            ("GRANT USAGE ON SEQUENCE fixture.seq TO PUBLIC", "REVOKE USAGE ON SEQUENCE fixture.seq FROM PUBLIC"),
            ("GRANT CREATE ON SCHEMA fixture TO PUBLIC", "REVOKE CREATE ON SCHEMA fixture FROM PUBLIC"),
            ("GRANT TRIGGER ON fixture.items TO cre_source_evidence_writer",
             "REVOKE TRIGGER ON fixture.items FROM cre_source_evidence_writer"),
            ("GRANT UPDATE ON SEQUENCE fixture.seq TO evidence_auto",
             "REVOKE UPDATE ON SEQUENCE fixture.seq FROM evidence_auto"),
        ]
        for add, remove in hazards:
            sql(add)
            refused = sql(guarded, user="evidence_auto", check=False)
            assert refused.returncode != 0 and "disallowed effective database capability" in refused.stderr, (add, refused.stderr)
            assert sql("SELECT count(*) FROM cre_source_evidence.events").stdout.strip() == "0"
            sql(remove)
        sql(guarded, user="evidence_auto")
        assert sql("SELECT count(*) FROM cre_source_evidence.events").stdout.strip() == "79"
        sql(guarded, user="evidence_auto")
        assert sql("SELECT count(*) FROM cre_source_evidence.events").stdout.strip() == "79"
        print("Disposable PG17 privilege guard: 7 negative cases, 78 records, exact replay passed")


if __name__ == "__main__":
    main()
