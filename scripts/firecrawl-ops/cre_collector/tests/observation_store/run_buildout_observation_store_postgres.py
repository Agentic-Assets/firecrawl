"""Exercise the unapplied store draft only on an ephemeral synthetic cluster.

Run in a no-hardlinks standalone clone with --execute-disposable-fixture.
No database URL, live data, or external connection is accepted.
"""
import argparse
import hashlib
import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from owned_observation_fixture_cluster import OwnedCluster, tool

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-disposable-fixture", action="store_true")
    if not parser.parse_args().execute_disposable_fixture:
        parser.error("explicit --execute-disposable-fixture is required")
    initdb, pg_ctl, psql = (tool(name) for name in ("initdb", "pg_ctl", "psql"))
    migration = (HERE / "fixtures/migration.sql").read_text()
    database = "cre_store_fixture_" + uuid.uuid4().hex[:12]
    with OwnedCluster(initdb, pg_ctl, port=55441) as cluster:
        temp = str(cluster.root)

        def sql(statement, target=database, check=True):
            result = subprocess.run([psql, "-X", "-qAt", "-h", temp, "-p", "55441", "-d", target, "-v", "ON_ERROR_STOP=1"], input=statement, capture_output=True, text=True, timeout=45, check=False, env=cluster.env)
            if check and result.returncode:
                raise AssertionError(result.stderr[-2000:])
            return result

        def append(capture, page, count, variant=0):
            # Synthetic literals only, with duplicate labels and unknown IDs.
            raw = json.dumps({"records": [{"id": None, "facts": [["unknown", variant], ["unknown", False]]} for _ in range(count)]}, separators=(",", ":")).encode()
            assertions = '{"source":null,"generation":null,"page":null,"transaction":"unknown"}'
            provenance = '{"sourceAuthorization":"not_evaluated","sourceCompleteness":"unverified"}'
            return ("SET ROLE cre_buildout_observation_writer; SELECT cre_buildout_observation.append_page("
                    f"'{capture}',{page},'{assertions}'::jsonb,'{provenance}'::jsonb,"
                    f"'{hashlib.sha256(raw).hexdigest()}',{len(raw)},decode('{raw.hex()}','hex'),'{hashlib.sha256(raw).hexdigest()}',"
                    f"'{json.dumps(json.loads(raw)['records'], separators=(',', ':'))}'::jsonb);")
        sql(f"CREATE DATABASE {database};", target="postgres")
        sql("CREATE ROLE cre_buildout_observation_writer NOLOGIN; CREATE ROLE cre_buildout_observation_owner NOLOGIN;", target="postgres")
        assert sql(migration, check=False).returncode != 0
        sql("CREATE SCHEMA cre_buildout_observation;")
        gated = "SET cre_observation_store.approved_draft='fixture-only';\n" + migration
        assert sql(gated, check=False).returncode != 0
        sql("DROP SCHEMA cre_buildout_observation;")
        sql(gated)
        sql("CREATE SCHEMA credeals; CREATE TABLE credeals.listings(id int PRIMARY KEY, status text); INSERT INTO credeals.listings VALUES (1,'active'); CREATE TABLE credeals.source_index(id int PRIMARY KEY, status text); INSERT INTO credeals.source_index VALUES (1,'present');")
        before = sql("SELECT (SELECT row_to_json(x)::text FROM credeals.listings x),(SELECT row_to_json(x)::text FROM credeals.source_index x);").stdout
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [task.result().stdout.strip() for task in [pool.submit(sql, append("capture-a", 0, 45)) for _ in range(2)]]
        assert sorted(outcomes) == ["inserted", "unchanged"]
        assert sql(append("capture-a", 1, 45)).stdout.strip() == "inserted"
        assert sql(append("capture-a", 0, 45, 1), check=False).returncode != 0
        oversized = ("SET ROLE cre_buildout_observation_writer; SELECT cre_buildout_observation.append_page("
                     "'oversized',0,'{}'::jsonb,'{}'::jsonb,repeat('0',64),1,"
                     "convert_to(repeat('x',16777217),'UTF8'),repeat('0',64),'[]'::jsonb);")
        failure = sql(oversized, check=False)
        assert failure.returncode != 0 and "observation input exceeds bounds" in failure.stderr
        too_many_assertions = ("SET ROLE cre_buildout_observation_writer; SELECT cre_buildout_observation.append_page("
                               "'oversized-meta',0,jsonb_build_object('x',repeat('x',20000)),'{}'::jsonb,"
                               "repeat('0',64),1,convert_to('{}','UTF8'),repeat('0',64),'[]'::jsonb);")
        failure = sql(too_many_assertions, check=False)
        assert failure.returncode != 0 and "observation input exceeds bounds" in failure.stderr
        forged_observations = append("mismatch", 0, 1).replace('["unknown",0]', '["unknown",99]')
        assert sql(forged_observations, check=False).returncode != 0
        owner = sql("SELECT r.rolname, r.rolsuper, r.rolbypassrls, r.rolcanlogin FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner WHERE p.oid='cre_buildout_observation.append_page(text,integer,jsonb,jsonb,text,bigint,bytea,text,jsonb)'::regprocedure;").stdout.strip()
        assert owner == "cre_buildout_observation_owner|f|f|f"
        assert sql("SELECT has_table_privilege('cre_buildout_observation_writer','cre_buildout_observation.capture_pages','SELECT,INSERT,UPDATE,DELETE');").stdout.strip() == "f"
        assert sql("SET ROLE cre_buildout_observation_writer; SELECT * FROM cre_buildout_observation.capture_pages;", check=False).returncode != 0
        assert sql("CREATE ROLE unprivileged_fixture NOLOGIN;").returncode == 0
        assert sql(append("forbidden", 0, 1).replace("SET ROLE cre_buildout_observation_writer", "SET ROLE unprivileged_fixture"), check=False).returncode != 0
        forged = append("forged", 0, 1).replace("not_evaluated", "authenticated")
        assert sql(forged, check=False).returncode != 0
        assert sql("SET ROLE cre_buildout_observation_writer; DELETE FROM cre_buildout_observation.capture_pages;", check=False).returncode != 0
        sql("BEGIN; " + append("rolled-back", 0, 1) + " ROLLBACK;")
        assert sql("SELECT count(*), sum(jsonb_array_length(observations)) FROM cre_buildout_observation.capture_pages;").stdout.strip() == "2|90"
        assert before == sql("SELECT (SELECT row_to_json(x)::text FROM credeals.listings x),(SELECT row_to_json(x)::text FROM credeals.source_index x);").stdout
        assert sql("SELECT assertions->>'source' IS NULL AND observations->0->'facts' = '[[\"unknown\",0],[\"unknown\",false]]'::jsonb FROM cre_buildout_observation.capture_pages WHERE capture_page=0;").stdout.strip() == "t"
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(sql, append("race", 0, 1, v), check=False) for v in (0, 1)]
            results = [task.result() for task in pending]
        assert sorted(result.returncode == 0 for result in results) == [False, True]
        print("PASS: 90 observations across two pages; exact/divergent races, ACL, rollback, collision gate and unchanged canonical fixtures")


if __name__ == "__main__":
    main()
