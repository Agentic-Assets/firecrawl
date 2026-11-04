"""Disposable PG proof of the real generated raw_data CASE expression."""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cre_ingest


def run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"fixture command failed: {result.stderr}")
    return result.stdout.strip()


def lit(value):
    return "'" + json.dumps(value, separators=(",", ":")).replace("'", "''") + "'::jsonb"


def expression(enrichment):
    sql = cre_ingest.build_sql([], [], "2026-11-04T00:00:00Z", set(), svn_enrichment_ids=("synthetic",) if enrichment else ())
    return sql.split("raw_data          = ", 1)[1].split(",\n        source_lastmod", 1)[0]


def main():
    name = "svn-raw-proof-" + uuid.uuid4().hex[:10]
    def sql(statement):
        return run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                   "-v", "ON_ERROR_STOP=1", input=statement)
    try:
        run("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5",
            "--memory=256m", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                                   capture_output=True).returncode == 0
            if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable postgres not ready")
        sql("CREATE SCHEMA credeals; CREATE TABLE credeals.cre_brokerages(id int, slug text);"
            "INSERT INTO credeals.cre_brokerages VALUES (1,'svn'),(2,'newmark');")
        old = {"inventoryObservedAt": "earlier", "latestInventoryObservation": {"latitude": 1},
               "oldDetail": "not carried"}
        new = {"sourceKey": "svn", "markdown": "new detail"}
        def evaluate(enrichment, prior, incoming, brokerage=1):
            statement = (f"SELECT ({expression(enrichment)})::text FROM "
                         f"(SELECT {lit(prior)} AS raw_data, {brokerage} AS brokerage_id) t CROSS JOIN "
                         f"(SELECT {lit(incoming)} AS raw_data, {brokerage} AS brokerage_id, 'synthetic'::text AS external_id, "
                         "'2026-11-04T00:00:00Z'::timestamptz AS scraped_at) excluded;")
            return json.loads(sql(statement))
        actual = evaluate(True, old, new)
        assert actual == {**new, "inventoryObservedAt": "earlier",
                          "latestInventoryObservation": {"latitude": 1}}
        assert evaluate(False, old, new) == new
        assert evaluate(True, old, {"sourceKey": "newmark", "markdown": "new"}, 2) == {
            "sourceKey": "newmark", "markdown": "new"}
        assert evaluate(True, {}, new) == new
        incoming = {**new, "inventoryObservedAt": "newer", "latestInventoryObservation": {"latitude": 2}}
        assert evaluate(True, old, incoming) == actual
        asymmetric = {**new, "inventoryObservedAt": "unproven newer"}
        assert evaluate(True, old, asymmetric) == actual
        null_incoming = {**new, "inventoryObservedAt": None, "latestInventoryObservation": None}
        assert evaluate(True, old, null_incoming) == actual
        assert evaluate(True, ["inventoryObservedAt"], new) == new
        partial = evaluate(True, old, {"sourceKey": "svn", "detailError": "failed"})
        assert partial["oldDetail"] == "not carried"
        assert partial["latestInventoryObservation"]["detailError"] == "failed"
        print("PASS: real generated CASE preserves SVN inventory wrapper, detail, scoping, "
              "untrusted incoming inventory ignored, absent keys, and existing partial behavior")
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
