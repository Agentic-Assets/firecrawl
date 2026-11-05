"""Exercise Lyon partial transaction against an isolated PostgreSQL schema."""
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
from cre_lyon_partial import (  # noqa: E402
    build_partial_sql,
    identity_probe_sql,
    partial_readback_sql,
    stage_record,
)
from tests.test_lyon_partial_stage import record  # noqa: E402


def run(*args, text=None, check=True):
    return subprocess.run(args, input=text, text=True, capture_output=True, check=check, timeout=120)


def main():
    name = "lyon-partial-" + uuid.uuid4().hex[:12]

    def sql(query, check=True):
        result = run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                     "-v", "ON_ERROR_STOP=1", text=query, check=False)
        if check and result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip() if check else result

    try:
        run("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5",
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
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4308",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        sql(schema)
        # Exercise source custody, gate claims, and the actual ingest CLI SQL on
        # this disposable schema rather than only calling the SQL builder.
        from tests.test_lyon_partial_checkpoint import LyonCheckpointTests
        fixture = LyonCheckpointTests(methodName="test_owned_replay_worker_produces_bound_private_candidate")
        fixture.capture_sql = []
        fixture.test_owned_replay_worker_produces_bound_private_candidate()
        generated_sql, context = fixture.capture_sql[0]
        sql(generated_sql)
        source = context["source"]
        actual_readback = partial_readback_sql(context["staged"], context["held"], context["preimage"],
            context["job_id"], source["started_at"], source["finished_at"])
        assert sql(actual_readback) == "1|1|0"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='example' AND status IS NULL AND transaction_type IS NULL") == "1"
        sql("""
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,status,address,raw_data)
SELECT id,'legacy','https://lyonstahl.com/properties/legacy/','active','Same address',
 '{"sourceKey":"lyon-stahl","lyonStahlPostId":"777"}'::jsonb FROM credeals.cre_brokerages WHERE slug='lyon-stahl';
INSERT INTO credeals.cre_listing_images (listing_id,url)
SELECT id,'https://example.test/image.jpg' FROM credeals.cre_listings WHERE external_id='legacy';
""")
        legacy = record("https://lyonstahl.com/properties/legacy/", "property_path", "legacy")
        legacy["occurrences"][0]["row"].update(title="New title", address="Same address", zip="12345", latitude=34.1, longitude=-118.2)
        query = record("https://lyonstahl.com/?post_type=properties&p=40201", "wordpress_post", "40201")
        query["occurrences"][0]["row"].update(title="Separate", address="Same address")
        conflict = record("https://lyonstahl.com/?post_type=properties&p=777", "wordpress_post", "777")
        staged = [stage_record(item) for item in (legacy, query, conflict)]
        result = sql(identity_probe_sql(staged)).split("|", 3)
        assert result[:2] == ["3", "3"]
        held, before = json.loads(result[2]), json.loads(result[3])
        assert held == ["wordpress:777"]
        job = str(uuid.uuid4())
        start, finish = "2026-11-05T06:00:00Z", "2026-11-05T06:01:00Z"
        write = build_partial_sql(staged, held, before, job, start, finish)
        readback = partial_readback_sql(staged, held, before, job, start, finish)
        sql(write)
        assert sql(readback) == "1|3|0"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='wordpress:40201' AND status IS NULL AND transaction_type IS NULL") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='legacy' AND status='active' AND address='Same address'") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='legacy' AND lat=34.1 AND lng=-118.2") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listing_images") == "1"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='wordpress:777'") == "0"
        assert sql(write, check=False).returncode != 0
        bad = sql("BEGIN; UPDATE credeals.cre_listings SET status='active' WHERE external_id='wordpress:40201'; " + readback + "; ROLLBACK;")
        assert bad == "1|3|1", bad
        probe_again = sql(identity_probe_sql(staged)).split("|", 3)
        held_again, before_again = json.loads(probe_again[2]), json.loads(probe_again[3])
        assert held_again == ["wordpress:777"]
        next_job = str(uuid.uuid4())
        sql(build_partial_sql(staged, held_again, before_again, next_job, start, finish))
        assert sql(partial_readback_sql(staged, held_again, before_again, next_job, start, finish)) == "1|3|0"
        assert sql("SELECT jsonb_array_length(raw_data#>'{lyonSearchObservations,entries}') FROM credeals.cre_listings WHERE external_id='legacy'") == "1"
        # The ordinary full path must preserve the exact partial search history.
        retained = json.loads(sql("SELECT raw_data->'lyonSearchObservations' FROM credeals.cre_listings WHERE external_id='legacy'"))
        full = {"sourceKey": "lyon-stahl", "transactionMode": "sale", "id": "legacy",
                "url": "https://lyonstahl.com/properties/legacy/", "name": "", "transactionType": "Investment Sale"}
        jobs = [{"slug": "lyon-stahl", "discovered": 1, "saved": 1, "errors": 0, "notes": "synthetic"}]
        def full_sql(value):
            return ingest.build_sql([ingest.to_row(value, {}, "2026-11-05T06:05:00Z")],
                                    jobs, "2026-11-05T06:05:00Z", set())
        sql(full_sql(full))
        after = json.loads(sql("SELECT raw_data FROM credeals.cre_listings WHERE external_id='legacy'"))
        assert after["lyonSearchObservations"] == retained
        collision = {**full, "lyonSearchObservations": {"unknown": "refuse"}}
        result = sql(full_sql(collision), check=False)
        assert result.returncode != 0 and "Lyon search evidence key collision" in result.stderr
        assert json.loads(sql("SELECT raw_data FROM credeals.cre_listings WHERE external_id='legacy'")) == after
        sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{lyonSearchObservations}', '[]'::jsonb) WHERE external_id='legacy'")
        malformed = sql(full_sql(full), check=False)
        assert malformed.returncode != 0 and "Lyon search evidence malformed" in malformed.stderr
        sql("UPDATE credeals.cre_listings SET raw_data=" + ingest.sql_lit(json.dumps(after)) + "::jsonb WHERE external_id='legacy'")
        sql("UPDATE credeals.cre_listings SET canonical_url='https://lyonstahl.com/properties/collision/' WHERE external_id='legacy'")
        collision = stage_record(record("https://lyonstahl.com/properties/collision/", "property_path", "collision"))
        assert json.loads(sql(identity_probe_sql([collision])).split("|", 3)[2]) == ["collision"]
        sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data,'{lyonSearchObservations}', '\"broken\"'::jsonb) WHERE external_id='legacy'")
        assert json.loads(sql(identity_probe_sql([staged[0]])).split("|", 3)[2]) == ["legacy"]
        sql("UPDATE credeals.cre_listings SET raw_data=raw_data || '{\"lyonStahlPostId\":\"999\"}'::jsonb WHERE external_id='wordpress:40201'")
        assert json.loads(sql(identity_probe_sql([staged[1]])).split("|", 3)[2]) == ["wordpress:40201"]
        # Partial coordinate components remain in source history without
        # fabricating a pair; a same-observation pair may later fill the gap.
        split = record("https://lyonstahl.com/properties/split/", "property_path", "split")
        split["occurrences"][0]["row"]["latitude"] = "34.1"
        split["occurrences"].append({**split["occurrences"][0], "index": 1, "row": {
            "permalink": split["url"], "longitude": "-118.2", "title": "Other"}})
        split_stage = [stage_record(split)]
        assert split_stage[0]["lat"] is None and split_stage[0]["lng"] is None
        split_probe = sql(identity_probe_sql(split_stage)).split("|", 3)
        split_held, split_prior = json.loads(split_probe[2]), json.loads(split_probe[3])
        split_job = str(uuid.uuid4())
        sql(build_partial_sql(split_stage, split_held, split_prior, split_job, start, finish))
        assert sql(partial_readback_sql(split_stage, split_held, split_prior, split_job, start, finish)) == "1|1|0"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='split' AND lat IS NULL AND lng IS NULL") == "1"
        split["occurrences"].append({**split["occurrences"][0], "index": 2, "row": {
            "permalink": split["url"], "latitude": "34.1", "longitude": "-118.2"}})
        complete_stage = [stage_record(split)]
        complete_probe = sql(identity_probe_sql(complete_stage)).split("|", 3)
        complete_held, complete_prior = json.loads(complete_probe[2]), json.loads(complete_probe[3])
        complete_job = str(uuid.uuid4())
        sql(build_partial_sql(complete_stage, complete_held, complete_prior, complete_job, start, finish))
        assert sql(partial_readback_sql(complete_stage, complete_held, complete_prior, complete_job, start, finish)) == "1|1|0"
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='split' AND lat=34.1 AND lng=-118.2") == "1"
        # A concurrent noncooperating transaction holds the source-index row.
        # Lyon must wait there before locking its listing. A separate actor can
        # update that listing while Lyon waits; the stale preimage then aborts.
        sql("""
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,status,raw_data)
SELECT id,'lock-target','https://lyonstahl.com/properties/lock-target/','active','{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='lyon-stahl';
INSERT INTO credeals.cre_source_index (brokerage_id,external_id,source_key,url,observation_present)
SELECT id,'lock-target','lyon-stahl','https://lyonstahl.com/properties/lock-target/',true
FROM credeals.cre_brokerages WHERE slug='lyon-stahl';
""")
        lock_row = stage_record(record("https://lyonstahl.com/properties/lock-target/", "property_path", "lock-target"))
        lock_probe = sql(identity_probe_sql([lock_row])).split("|", 3)
        lock_held, lock_prior = json.loads(lock_probe[2]), json.loads(lock_probe[3])
        lock_job = str(uuid.uuid4())
        lock_sql = build_partial_sql([lock_row], lock_held, lock_prior, lock_job, start, finish)
        locker = subprocess.Popen(["docker", "exec", "-i", "-e", "PGAPPNAME=lyon-fixture-locker", name,
            "psql", "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c",
            "BEGIN; SELECT id FROM credeals.cre_source_index WHERE external_id='lock-target' FOR UPDATE; SELECT pg_sleep(5); COMMIT;"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        writer = None
        try:
            for _ in range(100):
                if sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='lyon-fixture-locker' AND wait_event='PgSleep'") == "1":
                    break
                time.sleep(.05)
            else:
                raise RuntimeError("fixture source-index lock not acquired")
            writer = subprocess.Popen(["docker", "exec", "-i", "-e", "PGAPPNAME=lyon-fixture-writer", name,
                "psql", "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            writer.stdin.write(lock_sql)
            writer.stdin.close()
            writer.stdin = None
            for _ in range(100):
                if sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='lyon-fixture-writer' AND wait_event_type='Lock'") == "1":
                    break
                time.sleep(.05)
            else:
                raise RuntimeError("Lyon fixture did not wait on the held index lock")
            sql("SET lock_timeout='1s'; UPDATE credeals.cre_listings SET title='concurrent' WHERE external_id='lock-target'")
            locker.communicate(timeout=10)
            assert locker.returncode == 0
            _, error = writer.communicate(timeout=10)
            assert writer.returncode != 0 and "Lyon identity partition changed" in error
            assert sql("SELECT count(*) FROM credeals.cre_scrape_jobs WHERE id='" + lock_job + "'") == "0"
            assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='lock-target'") == "concurrent"
        finally:
            if locker.poll() is None:
                locker.terminate()
                locker.communicate(timeout=10)
            if writer is not None and writer.poll() is None:
                writer.terminate()
                writer.communicate(timeout=10)
        # While the source transaction holds its selected rows, a distinct
        # Lyon listing is not row-locked by this partial ingestion.
        sql("""
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,status,raw_data)
SELECT id,'unrelated','https://lyonstahl.com/properties/unrelated/','active','{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='lyon-stahl';
""")
        fresh_probe = sql(identity_probe_sql([lock_row])).split("|", 3)
        fresh_job = str(uuid.uuid4())
        fresh_sql = build_partial_sql([lock_row], json.loads(fresh_probe[2]), json.loads(fresh_probe[3]),
                                      fresh_job, start, finish)
        assert fresh_sql.count("COMMIT;") == 1
        fresh_sql = fresh_sql.replace("COMMIT;", "SELECT pg_sleep(4); COMMIT;")
        writer = subprocess.Popen(["docker", "exec", "-i", "-e", "PGAPPNAME=lyon-fixture-scoped", name,
            "psql", "-U", "postgres", "-X", "-q", "-v", "ON_ERROR_STOP=1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            writer.stdin.write(fresh_sql)
            writer.stdin.close()
            writer.stdin = None
            for _ in range(100):
                if sql("SELECT count(*) FROM pg_stat_activity WHERE application_name='lyon-fixture-scoped' AND wait_event='PgSleep'") == "1":
                    break
                time.sleep(.05)
            else:
                raise RuntimeError("Lyon fixture did not hold scoped locks")
            sql("SET lock_timeout='1s'; UPDATE credeals.cre_listings SET title='not blocked' WHERE external_id='unrelated'")
            _, error = writer.communicate(timeout=10)
            assert writer.returncode == 0, error
            assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='unrelated'") == "not blocked"
        finally:
            if writer.poll() is None:
                writer.terminate()
                writer.communicate(timeout=10)
        print("Lyon disposable PostgreSQL: identity hold, null status, additive history, children, stale preimage and readback passed")
    finally:
        run("docker", "rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
