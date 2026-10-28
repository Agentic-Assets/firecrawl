"""Opt-in synthetic-only PostgreSQL contract; never accepts a database URL.

Run from a standalone git clone --no-hardlinks:
  python3 tests/run_buildout_observation_prototype_postgres.py --execute-disposable-fixture
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE)]
from test_cre_buildout_observation_prototype import fixture, raw

import cre_buildout_observation_prototype as prototype


def tool(name):
    for directory in ("/opt/homebrew/opt/postgresql@17/bin", "/usr/lib/postgresql/17/bin"):
        path = Path(directory) / name
        if path.is_file():
            return str(path)
    candidate = shutil.which(name)
    if candidate and (name != "initdb" or (Path(candidate).resolve().parent / "postgres").is_file()):
        return candidate
    raise RuntimeError(f"missing PostgreSQL server tool: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-disposable-fixture", action="store_true")
    if not parser.parse_args().execute_disposable_fixture:
        parser.error("explicit --execute-disposable-fixture is required")
    initdb, pg_ctl, psql = (tool(name) for name in ("initdb", "pg_ctl", "psql"))
    migration = (HERE / "fixtures/cre_buildout_observation_prototype.sql").read_text()
    data = fixture()
    data["listings"][0]["buildoutInventoryObservation"] = json.loads(json.dumps(data["listings"][0]["buildoutInventoryObservation"]))
    data["listings"][0]["buildoutInventoryObservation"]["attributes"].append(["adversarial", "$$ $cre_observation_0$ O\\'Reilly \\ path"])
    records, summary = prototype.build_plan(raw(data))
    plan = prototype.sql_plan(records)
    database = "cre_observation_fixture_" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="cre-observation-pg-", dir="/tmp") as temp:
        root, db = Path(temp), Path(temp) / "db"
        subprocess.run([initdb, "-D", str(db), "-A", "trust", "--no-locale"], check=True, stdout=subprocess.DEVNULL, timeout=30)
        subprocess.run([pg_ctl, "-D", str(db), "-o", f"-h '' -k {temp} -p 55439", "-l", str(root / "server.log"), "start"], check=True, stdout=subprocess.DEVNULL, timeout=30)
        def command(target):
            return [psql, "-X", "-qAt", "-h", temp, "-p", "55439", "-d", target, "-v", "ON_ERROR_STOP=1"]
        def sql(text, target=database, check=True):
            result = subprocess.run(command(target), input="SET cre.observation_fixture='isolated-synthetic-only';\n" + text, capture_output=True, text=True, timeout=45, check=False)
            if check and result.returncode:
                raise AssertionError(result.stderr[-3000:])
            return result
        try:
            assert sql(migration, target="postgres", check=False).returncode != 0
            sql(f"CREATE DATABASE {database};", target="postgres")
            no_gate = subprocess.run(command(database), input=migration, capture_output=True, text=True, timeout=45, check=False)
            assert no_gate.returncode != 0, "migration ran without fixture session marker"
            sql(migration)
            sql("CREATE SCHEMA credeals; CREATE TABLE credeals.cre_listings (id int primary key, status text, updated_at text); INSERT INTO credeals.cre_listings VALUES (1,'active','before'); CREATE TABLE credeals.cre_source_index (id int primary key, state text); INSERT INTO credeals.cre_source_index VALUES (1,'present'); CREATE TABLE credeals.cre_listing_images (id int primary key); INSERT INTO credeals.cre_listing_images VALUES (1);")
            before = sql("SELECT (SELECT row_to_json(x)::text FROM credeals.cre_listings x), (SELECT row_to_json(x)::text FROM credeals.cre_source_index x), (SELECT count(*) FROM credeals.cre_listing_images);").stdout
            # Two concurrent exact attempts are safe and idempotent.
            with ThreadPoolExecutor(max_workers=2) as pool:
                attempts = [pool.submit(sql, "SET standard_conforming_strings=off;\n" + plan) for _ in range(2)]
                for attempt in attempts:
                    attempt.result()
            assert sql("SELECT count(*) FROM cre_observation_fixture.buildout_observations;").stdout.strip() == "65"
            after = sql("SELECT (SELECT row_to_json(x)::text FROM credeals.cre_listings x), (SELECT row_to_json(x)::text FROM credeals.cre_source_index x), (SELECT count(*) FROM credeals.cre_listing_images);").stdout
            assert before == after
            observed = json.loads(sql("SELECT observation::text FROM cre_observation_fixture.buildout_observations WHERE ordinal=0;").stdout)
            assert observed["payload"]["attributes"] == data["listings"][0]["buildoutInventoryObservation"]["attributes"]
            changed = json.loads(raw(data))
            changed["listings"][0]["buildoutInventoryObservation"]["attributes"][0][1] = True
            conflicting = prototype.sql_plan(prototype.build_plan(raw(changed))[0])
            assert sql(conflicting, check=False).returncode != 0
            assert sql("SELECT count(*) FROM cre_observation_fixture.buildout_observations;").stdout.strip() == "65"
            changed["runMeta"]["freshness"]["generationId"] = "run-2"
            for item in changed["listings"]:
                item["buildoutInventoryObservation"]["generationId"] = "run-2"
            sql(prototype.sql_plan(prototype.build_plan(raw(changed))[0]))
            assert sql("SELECT count(*) FROM cre_observation_fixture.buildout_observations;").stdout.strip() == "130"
            changed_value = json.loads(sql("SELECT observation::text FROM cre_observation_fixture.buildout_observations WHERE generation_id='run-2' AND ordinal=0;").stdout)
            assert changed_value["payload"]["attributes"][0] == ["literal", True]
            assert before == sql("SELECT (SELECT row_to_json(x)::text FROM credeals.cre_listings x), (SELECT row_to_json(x)::text FROM credeals.cre_source_index x), (SELECT count(*) FROM credeals.cre_listing_images);").stdout
            print(f"PASS: {summary['observations']} initial observations, concurrent retries, divergent replay refusal, 65 later conflicting observations, unchanged canonical fixtures")
        finally:
            subprocess.run([pg_ctl, "-D", str(db), "-m", "fast", "stop"], check=True, stdout=subprocess.DEVNULL, timeout=30)


if __name__ == "__main__":
    main()
