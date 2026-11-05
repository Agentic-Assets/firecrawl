"""Disposable PostgreSQL proof of all-parent Franklin rollback snapshot."""
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(ROOT), str(HERE)]
from test_franklin_rollback_evidence import case
from run_franklin_partial_e2e_postgres import disposable_cluster
from cre_franklin_rollback_evidence import snapshot_sql
from cre_ingest import psql_connection_env


def main():
    # The binding uses synthetic identifiers only; this cluster has no TCP.
    binding = case.__wrapped__()["binding"]
    with disposable_cluster() as root:
        uri = f"postgresql://postgres@{quote(str(root), safe='')}:5432/postgres"
        env = psql_connection_env(uri)

        def execute(sql):
            result = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1"],
                input=sql, env=env, text=True, capture_output=True, check=True, timeout=30)
            return result.stdout.strip()

        execute("""
CREATE SCHEMA credeals;
CREATE TABLE credeals.cre_brokerages (id int PRIMARY KEY, slug text);
CREATE TABLE credeals.cre_listings (brokerage_id int, deleted_at timestamptz, raw_data jsonb);
CREATE TABLE credeals.cre_scrape_jobs (id uuid, artifact_run_key text);
INSERT INTO credeals.cre_brokerages VALUES (1, 'franklin-street');
""")
        def counts():
            return json.loads(execute(snapshot_sql(binding)))

        assert counts() == {"job_uuid_or_key": 0, "last_generation": 0,
                            "history_generation": 0, "malformed_history": 0}
        execute("""INSERT INTO credeals.cre_listings VALUES
 (1, now(), '{"franklinSiteLastGenerationId":"run-1","franklinSiteObservations":[{"generationId":"run-1"}]}');""")
        found = counts()
        assert found["last_generation"] == found["history_generation"] == 1
        execute("""INSERT INTO credeals.cre_listings VALUES
 (1, NULL, '{"franklinSiteObservations":{}}');""")
        assert counts()["malformed_history"] == 1
        execute("INSERT INTO credeals.cre_scrape_jobs VALUES ('"
                + binding["job_id"] + "'::uuid, 'other');")
        assert counts()["job_uuid_or_key"] == 1
        print("Franklin rollback target snapshot: passed")


if __name__ == "__main__":
    main()