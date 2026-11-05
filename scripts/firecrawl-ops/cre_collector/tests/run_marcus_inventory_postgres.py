"""Real-schema Marcus inventory proof in disposable, networkless PostgreSQL."""
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
from cre_marcus_inventory import stage_inventory  # noqa: E402
from cre_marcus_inventory_db import build_partial_sql, identity_probe_sql, partial_readback_sql  # noqa: E402
from tests.test_marcus_inventory import listing, GENERATION, START, FINISH  # noqa: E402


def run(*args, text=None, check=True):
    return subprocess.run(args, input=text, text=True, capture_output=True, check=check, timeout=120)


def main():
    name = "marcus-inventory-" + uuid.uuid4().hex[:12]

    def sql(query, check=True):
        result = run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                     "-v", "ON_ERROR_STOP=1", text=query, check=False)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip() if check else result

    try:
        run("docker", "run", "-d", "--rm", "--pull=never", "--name", name, "--network=none", "--cpus=0.5",
            "--memory=512m", "--pids-limit=128", "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
            "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            logs = run("docker", "logs", name, check=False)
            if (run("docker", "exec", name, "pg_isready", "-U", "postgres", check=False).returncode == 0
                    and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2):
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable PostgreSQL did not start")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        schema = master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1", "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-5225",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        sql(schema)
        sql("""
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,canonical_url,status,title,raw_data)
SELECT id,'101','https://www.marcusmillichap.com/properties/101/source-name',
 'https://www.marcusmillichap.com/properties/101/source-name','active','Existing title',
 '{"sourceKey":"marcus-millichap"}'::jsonb FROM credeals.cre_brokerages WHERE slug='marcus-millichap';
INSERT INTO credeals.cre_listing_images (listing_id,url)
SELECT id,'https://example.test/image.jpg' FROM credeals.cre_listings WHERE external_id='101';
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,status,raw_data)
SELECT id,'other','HTTPS://MARCUSMILLICHAP.COM:443/properties/103/different-slug?view=1','active','{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='marcus-millichap';
""")
        staged = [stage_inventory(listing(str(i), "activity-" + str(i)), GENERATION, START, FINISH)
                  for i in (101, 102, 103)]
        probe = sql(identity_probe_sql(staged, GENERATION, START, FINISH)).split("|", 3)
        assert probe[:2] == ["3", "3"]
        held, prior = json.loads(probe[2]), json.loads(probe[3])
        assert held == ["103"]
        receipt = {"schema": "marcus-inventory-custody/v1", "source": "marcus-millichap", "coverage": "partial",
                   "generation": GENERATION, "attempt": 1, "source_sha256": "a" * 64,
                   "detail_qualified": 0, "detail_errors": 1, "started_at": START, "finished_at": FINISH,
                   "partition": {"staged": staged, "held": []}}
        job = str(uuid.uuid4())
        write = build_partial_sql(staged, held, prior, receipt, job)
        readback = partial_readback_sql(staged, held, prior, receipt, job)
        sql(write)
        assert sql(readback) == "1|3|0"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='102' AND status IS NULL AND scraped_at IS NULL AND country IS NULL") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='101' AND title='Existing title' AND status='active'") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listing_images") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='103'") == "0"
        assert sql(write, check=False).returncode != 0
        assert sql("BEGIN; UPDATE credeals.cre_listings SET status='active' WHERE external_id='102'; " + readback + "; ROLLBACK;") == "1|3|1"
        history = json.loads(sql("SELECT raw_data->'marcusInventoryObservations' FROM credeals.cre_listings WHERE external_id='101'"))
        full = {"sourceKey": "marcus-millichap", "transactionMode": "sale", "id": "101",
                "url": "https://www.marcusmillichap.com/properties/101/source-name", "name": "Full title"}
        jobs = [{"slug": "marcus-millichap", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
        sql(ingest.build_sql([ingest.to_row(full, {}, FINISH)], jobs, FINISH, set()))
        assert json.loads(sql("SELECT raw_data->'marcusInventoryObservations' FROM credeals.cre_listings WHERE external_id='101'")) == history
        race_staged = [stage_inventory(listing(str(i), "activity-" + str(i)), GENERATION, START, FINISH)
                       for i in (101, 104)]
        race_probe = sql(identity_probe_sql(race_staged, GENERATION, START, FINISH)).split("|", 3)
        race_held, race_prior = json.loads(race_probe[2]), json.loads(race_probe[3])
        race_receipt = dict(receipt, partition={"staged": race_staged, "held": []})
        race_write = build_partial_sql(race_staged, race_held, race_prior, race_receipt, str(uuid.uuid4()))
        holder = subprocess.Popen(["docker", "exec", "-i", "-e", "PGAPPNAME=marcus-race-holder", name,
                                   "psql", "-U", "postgres", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        holder.stdin.write("BEGIN; " + ingest.lifecycle_transaction_lock_sql() +
                           " UPDATE credeals.cre_listings SET status='inactive' WHERE external_id='101'; SELECT pg_sleep(3); COMMIT;\n")
        holder.stdin.close()
        holder.stdin = None
        for _ in range(100):
            active = sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='marcus-race-holder' AND state='active'")
            if active == "1":
                break
            time.sleep(.03)
        else:
            raise RuntimeError("synthetic lock holder did not start")
        writer = subprocess.Popen(["docker", "exec", "-i", "-e", "PGAPPNAME=marcus-race-writer", name,
                                   "psql", "-U", "postgres", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        writer.stdin.write(race_write)
        writer.stdin.close()
        writer.stdin = None
        for _ in range(100):
            waiting = sql("SELECT count(*) FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                          "WHERE a.application_name='marcus-race-writer' AND l.locktype='advisory' AND NOT l.granted")
            if waiting == "1":
                break
            time.sleep(.03)
        else:
            raise RuntimeError("synthetic writer did not wait on lifecycle lock")
        out, err = holder.communicate(timeout=15)
        assert holder.returncode == 0, err
        out, err = writer.communicate(timeout=15)
        assert writer.returncode != 0 and "Marcus identity partition changed" in err
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='104'") == "0"
        assert sql("SELECT status FROM credeals.cre_listings WHERE external_id='101'") == "inactive"
        print("Marcus real-schema new/existing/held/children/readback/full-refresh/cooperating-race PASS")
    finally:
        run("docker", "rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
