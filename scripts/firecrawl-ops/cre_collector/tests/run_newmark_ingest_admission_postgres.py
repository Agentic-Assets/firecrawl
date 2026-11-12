"""Exercise mixed source admission only against a disposable isolated database."""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT.parent / "sql"
IMAGE = "sha256:2d2b8998d31037bf721cfdf764d76ba74171b4fab3431b7f72c27c56ddbdf9e3"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-disposable-fixture", action="store_true")
    if not parser.parse_args().execute_disposable_fixture:
        parser.error("explicit fixture opt-in required")
    name = "cre-newmark-admission-" + uuid.uuid4().hex
    created = False
    try:
        subprocess.run(["docker", "create", "--pull=never", "--name", name,
                        "--network", "none", "--memory", "512m", "--cpus", "0.25",
                        "--pids-limit", "80", "--tmpfs", "/var/lib/postgresql/data",
                        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", IMAGE],
                       check=True, stdout=subprocess.DEVNULL, timeout=30)
        created = True
        subprocess.run(["docker", "start", name], check=True, stdout=subprocess.DEVNULL, timeout=30)
        for _ in range(60):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("fixture database did not start")
        psql = ["docker", "exec", "-i", name, "psql", "-h", "127.0.0.1", "-X", "-qAt", "-U", "postgres",
                "-v", "ON_ERROR_STOP=1"]

        def run(sql):
            result = subprocess.run(psql, input=sql, text=True, capture_output=True, timeout=60)
            if result.returncode:
                raise RuntimeError(result.stderr[-4000:])
            return result.stdout.strip()

        schema = re.sub(r"^\\i (\S+)\s*$", lambda match: (MIGRATIONS / match[1]).read_text(),
                        (MIGRATIONS / "000_run_all.sql").read_text(), flags=re.MULTILINE)
        run(schema)
        for migration, prefix in [("016_cre_listing_lifecycle.sql", "CRE_LISTING_LIFECYCLE"),
                                  ("017_cre_available_space_count.sql", "CRE_AVAILABLE_SPACE_COUNT"),
                                  ("018_cre_building_source_facts.sql", "CRE_BUILDING_SOURCE_FACTS")]:
            approve = {"CRE_LISTING_LIFECYCLE": "CRE_APPROVE_LISTING_LIFECYCLE",
                       "CRE_AVAILABLE_SPACE_COUNT": "CRE_APPROVE_AVAILABLE_SPACE_COUNT",
                       "CRE_BUILDING_SOURCE_FACTS": "CRE_APPROVE_BUILDING_SOURCE_FACTS"}[prefix]
            command = migration.removesuffix(".sql")
            # These values satisfy the migration harness only in this isolated
            # throwaway database; they do not authorize a production migration.
            run(f"\\set {approve} 1\n\\set {prefix}_OPERATOR cayman\n"
                f"\\set {prefix}_APPROVAL_REF AGENTIC-5337\n\\set {prefix}_CONFIRM 'APPLY {command}'\n"
                + (MIGRATIONS / migration).read_text())
        source = {"sourceKey": "newmark", "transaction": "sale", "listingsCollected": 1,
                  "retainedObservations": [{"newmarkRcmObservation": True}]}
        peer = {"sourceKey": "cbre", "transaction": "sale", "listingsCollected": 1}
        artifact = {"runMeta": {"mode": "enrich", "startedAt": "2026-11-11T12:00:00Z",
                                "finishedAt": "2026-11-11T12:00:00Z"},
                    "sources": [source, peer], "listings": [
                        {"sourceKey": "newmark", "id": "held", "url": "https://example.test/held",
                         "name": "Unbound detail", "salePriceUsd": 1234567},
                        {"sourceKey": "cbre", "id": "peer", "url": "https://example.test/peer",
                         "name": "Eligible peer"}], "brokers": []}
        with tempfile.TemporaryDirectory(prefix="newmark-admission-") as tmp:
            path, out = Path(tmp) / "artifact.json", Path(tmp) / "sql"
            original = json.dumps(artifact).encode()
            path.write_bytes(original)
            result = subprocess.run([sys.executable, str(ROOT / "cre_ingest.py"), "--in", str(path), "--dry-run",
                                     "--keep-artifacts", str(out)], cwd=ROOT, capture_output=True, text=True)
            assert result.returncode == 20 and "holding unbound Newmark" in result.stderr, result.stderr
            run((out / "ingest.sql").read_text())
            assert run("SELECT count(*) FROM credeals.cre_listings WHERE external_id='held';") == "0"
            assert run("SELECT title FROM credeals.cre_listings WHERE external_id='peer';") == "Eligible peer"
            assert run("SELECT count(*) FROM credeals.cre_scrape_jobs j JOIN credeals.cre_brokerages b "
                       "ON b.id=j.brokerage_id WHERE b.slug='newmark';") == "0"
            assert run("SELECT count(*) FROM credeals.cre_scrape_jobs j JOIN credeals.cre_brokerages b "
                       "ON b.id=j.brokerage_id WHERE b.slug='cbre';") == "1"
            assert path.read_bytes() == original
        print("PASS: disposable mixed admission retained peer and original evidence; no Newmark row or job")
    finally:
        if created:
            subprocess.run(["docker", "rm", "-f", name], check=True, stdout=subprocess.DEVNULL, timeout=30)


if __name__ == "__main__":
    main()
