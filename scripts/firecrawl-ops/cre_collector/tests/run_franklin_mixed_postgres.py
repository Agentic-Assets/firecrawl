"""Offline mixed Franklin Buildout and site-card transaction contract."""
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from run_franklin_site_lifecycle_postgres import run
from test_franklin_site_mixed import fixture, make_binding

import cre_franklin_site_mixed as mixed
from cre_franklin_site_projection import project
from cre_franklin_site_sql import readback_sql


def main():
    name = "franklin-mixed-" + uuid.uuid4().hex[:10]

    def sql(statement):
        return run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                   "-v", "ON_ERROR_STOP=1", input=statement)

    try:
        run("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5",
            "--memory=512m", "--pids-limit=128", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = subprocess.run(["docker", "logs", name], check=False, capture_output=True, text=True)
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                                   check=False, capture_output=True).returncode == 0
            if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable postgres not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        sql(master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5257",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()]))
        sql("""INSERT INTO credeals.cre_listings(brokerage_id, external_id, source_url, status)
               SELECT id, 'old-buildout', 'https://old.example/property', 'sold'
               FROM credeals.cre_brokerages WHERE slug='franklin-street';""")
        data = fixture()
        raw, binding = make_binding(data)
        env = {"PATH": os.environ["PATH"]}
        planned = mixed.plan(raw, binding, [], env)
        assert planned["buildout_count"] == 1 and planned["site_count"] == 1
        sql(planned["buildout_sql"])
        result = json.loads(sql(planned["site_sql"]).splitlines()[-1])
        assert result == {"held": [], "total": 1}
        rows = sql("""SELECT external_id || '|' || status || '|' || coalesce(deleted_at::text,'present')
                      FROM credeals.cre_listings ORDER BY external_id;""").splitlines()
        assert rows == ["20|active|present", "franklin-site:sale:12345678-1234-5123-8123-123456789abc|active|present",
                        "old-buildout|sold|present"], rows
        assert sql("SELECT count(*) FROM credeals.cre_scrape_jobs WHERE status='partial';") == "2"
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id LIKE 'franklin-site:%';") == "0"
        site_rows = project(raw, "mixed-run", env)["projection"]["listings"]
        assert json.loads(sql(readback_sql(site_rows, [], binding))) == {"expected": 1, "invalid": [], "matching_jobs": 1}
        print("Disposable mixed Franklin Buildout and site-card proof passed")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


if __name__ == "__main__":
    main()