"""Synthetic, network-none PG17 proof of the complete generated ingest transaction."""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import cre_ingest as ingest  # noqa: E402


def run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"fixture command failed: {result.stderr}")
    return result.stdout.strip()


def main():
    name = "svn-full-proof-" + uuid.uuid4().hex[:10]
    def sql(statement):
        return run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                   "-v", "ON_ERROR_STOP=1", input=statement)
    try:
        run("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5",
            "--memory=512m", "--pids-limit=128", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(50):
            if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                              capture_output=True).returncode == 0:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable postgres not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.M)
        schema = master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1",
            "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5257",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        sql(schema)
        item = {"sourceKey": "svn", "id": "synthetic", "url": "https://example.test/synthetic",
                "name": "Synthetic", "transactionType": "sale", "markdown": "new detail"}
        row = ingest.to_row(item, {}, "2026-11-04T00:00:00Z")
        job = [{"slug": "svn", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
        sql(ingest.build_sql([row], job, "2026-11-04T00:00:00Z", set()))
        sql("""UPDATE credeals.cre_listings SET raw_data=
              '{"inventoryObservedAt":"old","latestInventoryObservation":{"latitude":1.0000000000000001}}'::jsonb;""")
        sql(ingest.build_sql([row], job, "2026-11-04T00:01:00Z", set(),
                             svn_enrichment_ids=(row["external_id"],)))
        value = json.loads(sql("SELECT raw_data::text FROM credeals.cre_listings;"))
        assert value["inventoryObservedAt"] == "old"
        assert value["latestInventoryObservation"]["latitude"] == 1.0000000000000001
        assert value["markdown"] == "new detail"
        exact = sql("SELECT raw_data->'latestInventoryObservation'->>'latitude' FROM credeals.cre_listings;")
        assert exact == "1.0000000000000001"
        print("PASS: complete generated SVN ingest transaction preserves exact prior inventory and new detail")
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
