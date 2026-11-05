"""Synthetic multi-window pipeline against an isolated real PostgreSQL schema."""
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import cre_ingest as ingest
from tests.test_colliers_partial_failed_collector_pipeline import (
    test_evidence_only_collector_failure_reaches_partial_pipeline as exercise_pipeline,
)


def command(*args, text=None, check=True):
    return subprocess.run(args, input=text, text=True, capture_output=True, check=check, timeout=120)


def main():
    name = "colliers-multi-" + uuid.uuid4().hex[:12]
    # Only this synthetic directory is shared with the networkless container.
    root = Path(tempfile.mkdtemp(prefix="colliers-multi-"))
    os.environ["TMPDIR"] = str(root)
    tempfile.tempdir = str(root)
    print(f"synthetic fixture evidence: {root}", flush=True)

    def sql(query):
        result = command("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                         "-v", "ON_ERROR_STOP=1", text=query, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    try:
        command("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5",
                "--memory=512m", "--pids-limit=128", "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
                "--mount", f"type=bind,src={root},dst={root}",
                "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = command("docker", "logs", name, check=False)
            ready = command("docker", "exec", name, "pg_isready", "-U", "postgres", check=False).returncode == 0
            if ready and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable database not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        schema = master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1", "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5261",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        # This external market view is counted by full validation but is not
        # created by the collector-owned schema migrations.
        schema += "\nCREATE VIEW credeals.v_cre_market_summary AS SELECT 1 WHERE false;\n"
        sql(schema)
        # Match the real search smoke prerequisites with unrelated active rows.
        sql("""
INSERT INTO credeals.cre_listings
    (brokerage_id, external_id, source_url, canonical_url, status, title,
     address, city, state, property_type, transaction_type, raw_data)
SELECT id, 'synthetic-industrial', 'https://example.test/seed/industrial',
       'https://example.test/seed/industrial', 'active', 'Industrial Lee',
       '100 National Avenue', 'Dallas', 'TX', 'industrial', 'sale', '{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='lee-associates';
INSERT INTO credeals.cre_listings
    (brokerage_id, external_id, source_url, canonical_url, status, title,
     city, state, property_type, transaction_type, raw_data)
SELECT id, 'synthetic-office', 'https://example.test/seed/office',
       'https://example.test/seed/office', 'active', 'Office',
       'Dallas', 'TX', 'office', 'sale', '{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='lee-associates';
""")
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id IN ('synthetic-industrial','synthetic-office')") == "2"
        for query in ("'industrial',NULL,'TX',NULL,'sale'", "'office',NULL,NULL,NULL,'sale'",
                      "'Lee',NULL,NULL,NULL,NULL", "'National Avenue',NULL,NULL,NULL,NULL"):
            assert int(sql(f"SELECT count(*) FROM credeals.search_cre_listings({query})")) > 0
        # Retain synthetic artifacts and child logs on either success or failure.
        db_url = "postgresql://postgres:synthetic-only@localhost/postgres"
        env_file = root / "synthetic.env"
        env_file.write_text("POSTGRES_URL_NON_POOLING=" + db_url + "\n", encoding="utf8")
        env_file.chmod(0o600)
        shim = root / "psql"
        shim.write_text("#!/usr/bin/env python3\nimport os,sys\nargs=sys.argv[1:]\n"
            "if '-f' in args and args[args.index('-f')+1] != '-':\n"
            " i=args.index('-f')+1; fd=os.open(args[i],os.O_RDONLY); os.dup2(fd,0); os.close(fd); args[i]='-'\n"
            "os.execvp('docker',['docker','exec','-i','--user',str(os.getuid())+':'+str(os.getgid())," + repr(name) + ",'psql','-U','postgres',*args])\n",
            encoding="utf8")
        shim.chmod(0o700)
        database = {"target": ingest.database_target_fingerprint_from_url(db_url), "psql": shim, "env_file": env_file}
        (root / "fixture").mkdir()
        with pytest.MonkeyPatch.context() as patch:
            exercise_pipeline(root / "fixture", patch, False, journal=True, database=database)
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id LIKE 'id%' AND status IS NULL") == "200"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id LIKE 'id%' AND status IS NOT NULL") == "0"
        windows = database["manifest"]["sources"]["colliers"]["partial_windows"]
        assert [window["state"] for window in windows] == ["verified", "verified", "settled_no_write"]
        print("Two real database windows and settled rotation: PASS")
    finally:
        command("docker", "rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
