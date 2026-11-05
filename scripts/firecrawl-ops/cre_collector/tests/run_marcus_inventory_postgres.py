"""Real-schema Marcus inventory proof in disposable, networkless PostgreSQL."""
import json
from decimal import Decimal
import os
import re
import signal
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
    name = os.environ.get("MARCUS_TEST_CONTAINER") or "marcus-inventory-" + uuid.uuid4().hex[:12]
    if not re.fullmatch(r"marcus-inventory-[a-f0-9]{12}", name):
        raise ValueError("invalid disposable PostgreSQL name")

    def terminate(_signum, _frame):
        raise KeyboardInterrupt("disposable PostgreSQL proof interrupted")

    signal.signal(signal.SIGTERM, terminate)
    cpu = os.environ.get("MARCUS_TEST_CPU")
    if cpu is not None and not re.fullmatch(r"[0-9]+", cpu):
        raise ValueError("invalid disposable PostgreSQL CPU")
    cpu_args = ["--cpuset-cpus=" + cpu] if cpu is not None else []

    def sql(query, check=True):
        result = run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                     "-v", "ON_ERROR_STOP=1", text=query, check=False)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip() if check else result

    owned = False
    try:
        run("docker", "run", "-d", "--rm", "--pull=never", "--name", name, "--label", "codex.marcus-proof=" + name, "--network=none", "--cpus=0.5",
            *cpu_args, "--memory=512m", "--pids-limit=128", "--tmpfs", "/var/lib/postgresql/data:rw,size=256m",
            "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        owned = True
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
        from tests.marcus_cli_postgres_contract import run_cli_contract
        run_cli_contract(sql, name)
        from tests.marcus_mixed_postgres_contract import run_mixed_contract
        run_mixed_contract(sql, name)
        sql("""
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,canonical_url,status,title,sale_price_usd,raw_data)
SELECT id,'101','https://www.marcusmillichap.com/properties/101/source-name',
 'https://www.marcusmillichap.com/properties/101/source-name','active','Existing title',12345678901234567890.12345678901234567890,
 '{"sourceKey":"marcus-millichap","highPrecision":0.12345678901234567890123456789}'::jsonb FROM credeals.cre_brokerages WHERE slug='marcus-millichap';
INSERT INTO credeals.cre_source_index (brokerage_id,external_id,source_key,url)
SELECT id,'101',NULL,'https://www.marcusmillichap.com/properties/101/older-slug'
FROM credeals.cre_brokerages WHERE slug='marcus-millichap';
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
        held, prior = json.loads(probe[2]), json.loads(probe[3], parse_float=Decimal)
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
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='101' AND title='Existing title' AND status='active' AND lat=34.1 AND lng=-118.2") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listing_images") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_source_index WHERE external_id='101' AND source_key IS NULL") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='103'") == "0"
        assert sql(write, check=False).returncode != 0
        assert sql("BEGIN; UPDATE credeals.cre_listings SET status='active' WHERE external_id='102'; " + readback + "; ROLLBACK;") == "1|3|1"
        assert sql("BEGIN; UPDATE credeals.cre_scrape_jobs SET errors_count=0 WHERE id='" + job + "'; " + readback + "; ROLLBACK;") == "0|3|0"
        sql("""
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,canonical_url,status,lat,lng,title,raw_data)
SELECT id,'105','https://www.marcusmillichap.com/properties/105/source-name',
 'https://www.marcusmillichap.com/properties/202/other','active',NULL,NULL,NULL,'{"sourceKey":"marcus-millichap"}'::jsonb
FROM credeals.cre_brokerages WHERE slug='marcus-millichap';
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,status,lat,lng,title,raw_data)
SELECT id,'107','https://www.marcusmillichap.com/properties/107/source-name',
 'active',40.123456789012345,NULL,NULL,'{"sourceKey":"marcus-millichap"}'::jsonb
FROM credeals.cre_brokerages WHERE slug='marcus-millichap';
""")
        conflict_rows = [stage_inventory(listing(str(i), "activity-" + str(i)), GENERATION, START, FINISH)
                         for i in (105, 106, 107)]
        conflict_probe = sql(identity_probe_sql(conflict_rows, GENERATION, START, FINISH)).split("|", 3)
        conflict_held, conflict_prior = json.loads(conflict_probe[2]), json.loads(conflict_probe[3], parse_float=Decimal)
        assert conflict_held == ["105"]
        conflict_receipt = dict(receipt, partition={"staged": conflict_rows, "held": []})
        conflict_job = str(uuid.uuid4())
        sql(build_partial_sql(conflict_rows, conflict_held, conflict_prior, conflict_receipt, conflict_job))
        assert sql(partial_readback_sql(conflict_rows, conflict_held, conflict_prior, conflict_receipt, conflict_job)) == "1|3|0"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='105' AND NOT (raw_data ? 'marcusInventoryObservations')") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='106' AND status IS NULL") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='107' AND title='Source title' AND lat=40.123456789012345::double precision AND lng IS NULL") == "1"
        history = json.loads(sql("SELECT raw_data->'marcusInventoryObservations' FROM credeals.cre_listings WHERE external_id='101'"))
        full = {"sourceKey": "marcus-millichap", "transactionMode": "sale", "id": "101",
                "url": "https://www.marcusmillichap.com/properties/101/source-name", "name": "Full title"}
        jobs = [{"slug": "marcus-millichap", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
        sql(ingest.build_sql([ingest.to_row(full, {}, FINISH)], jobs, FINISH, set()))
        assert json.loads(sql("SELECT raw_data->'marcusInventoryObservations' FROM credeals.cre_listings WHERE external_id='101'")) == history
        race_staged = [stage_inventory(listing(str(i), "activity-" + str(i)), GENERATION, START, FINISH)
                       for i in (101, 104)]
        race_probe = sql(identity_probe_sql(race_staged, GENERATION, START, FINISH)).split("|", 3)
        race_held, race_prior = json.loads(race_probe[2]), json.loads(race_probe[3], parse_float=Decimal)
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
        # Approximate the observed 3,261-record candidate and add unrelated
        # parents so the source-URL collision query cannot pass by testing an
        # empty target. The result measures only this disposable target.
        sql("""
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,status,raw_data)
SELECT b.id,'unrelated-' || n,'https://unrelated.example/properties/' || n,'active','{}'::jsonb
FROM credeals.cre_brokerages b CROSS JOIN generate_series(1,10000) n WHERE b.slug='marcus-millichap';
""")
        scale = [stage_inventory(listing(str(i), "activity-" + str(i)), GENERATION, START, FINISH)
                 for i in range(200000, 203261)]
        started = time.monotonic()
        scale_probe = sql(identity_probe_sql(scale, GENERATION, START, FINISH)).split("|", 3)
        elapsed = time.monotonic() - started
        assert scale_probe[:2] == ["3261", "3261"] and json.loads(scale_probe[2]) == []
        print(f"Marcus disposable 3261-row/10000-peer read-only probe: {elapsed:.3f}s")
        print("Marcus real-schema new/existing/held/children/readback/full-refresh/cooperating-race PASS")
    finally:
        if owned:
            run("docker", "rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
