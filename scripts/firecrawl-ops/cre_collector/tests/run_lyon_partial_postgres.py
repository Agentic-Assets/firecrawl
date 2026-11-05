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
    new_missing_canonical_count,
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
        # Reproduce the live quality boundary with source-backed old and new
        # parents. The sole exception is the exact number inserted as new by
        # this partial transaction, never an allowance for older-row damage.
        import cre_checkpoint_refresh as refresh
        from tests.test_cre_checkpoint_refresh import absolute_quality_report

        sql("""
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,canonical_url,raw_data)
SELECT id,'quality-old','https://lyonstahl.com/properties/quality-old/',
 'https://lyonstahl.com/properties/quality-old/', '{"sourceKey":"lyon-stahl"}'::jsonb
FROM credeals.cre_brokerages WHERE slug='lyon-stahl';
""")
        missing_sql = """
SELECT count(*) FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id
WHERE b.slug='lyon-stahl' AND l.deleted_at IS NULL AND (l.canonical_url IS NULL OR btrim(l.canonical_url)='')"""
        quality_before_count = int(sql(missing_sql))
        quality_rows = [stage_record(record(f"https://lyonstahl.com/properties/quality-{number}/",
                                          "property_path", f"quality-{number}")) for number in range(159)]
        quality_rows.append(stage_record(record("https://lyonstahl.com/properties/quality-old/",
                                                "property_path", "quality-old")))
        quality_probe = sql(identity_probe_sql(quality_rows)).split("|", 3)
        assert quality_probe[:2] == ["160", "160"]
        quality_held, quality_prior = json.loads(quality_probe[2]), json.loads(quality_probe[3])
        assert quality_held == []
        quality_job = str(uuid.uuid4())
        quality_start, quality_finish = "2026-11-05T06:00:00Z", "2026-11-05T06:01:00Z"
        sql(build_partial_sql(quality_rows, quality_held, quality_prior, quality_job, quality_start, quality_finish))
        quality_readback = partial_readback_sql(quality_rows, quality_held, quality_prior, quality_job,
                                                quality_start, quality_finish)
        assert sql(quality_readback) == "1|160|0"
        allowance = new_missing_canonical_count(quality_rows, quality_held, quality_prior)
        assert allowance == 159
        quality_after_count = int(sql(missing_sql))
        assert quality_after_count - quality_before_count == 159
        quality_before = absolute_quality_report(source="lyon-stahl", missing_canonical_url=str(quality_before_count))
        quality_after = absolute_quality_report(source="lyon-stahl", missing_canonical_url=str(quality_after_count))
        assert not refresh.compare_validation_quality(quality_before, quality_after)["ok"]
        assert refresh.compare_validation_quality(quality_before, quality_after,
                                                  lyon_new_missing_canonical=allowance)["ok"]
        sql("UPDATE credeals.cre_listings SET canonical_url=NULL WHERE external_id='quality-old'")
        assert sql(quality_readback) == "1|160|1"
        deteriorated = absolute_quality_report(source="lyon-stahl", missing_canonical_url=sql(missing_sql))
        assert not refresh.compare_validation_quality(quality_before, deteriorated,
                                                       lyon_new_missing_canonical=allowance)["ok"]
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
        # Preserve malformed prior history opaquely on an ordinary refresh.
        # The source-specific partial probe still holds that identity.
        peer = {**full, "id": "peer-full", "url": "https://lyonstahl.com/properties/peer-full/", "name": "Peer listing"}
        rows = [ingest.to_row(item, {}, "2026-11-05T06:05:00Z") for item in (full, peer)]
        sql(ingest.build_sql(rows, jobs, "2026-11-05T06:05:00Z", set()))
        assert sql("SELECT raw_data->'lyonSearchObservations' FROM credeals.cre_listings WHERE external_id='legacy'") == "[]"
        assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='peer-full'") == "Peer listing"
        assert json.loads(sql(identity_probe_sql([staged[0]])).split("|", 3)[2]) == ["legacy"]
        sql("UPDATE credeals.cre_listings SET raw_data=" + ingest.sql_lit(json.dumps(after)) + "::jsonb WHERE external_id='legacy'")
        # Unfamiliar but identity-bound history arrays are opaque. Preserve
        # every existing value and metadata and append our versioned entry.
        before_opaque = json.loads(sql("SELECT raw_data FROM credeals.cre_listings WHERE external_id='legacy'"))
        opaque = {"schema": "future-history/v9", "sourceKey": "lyon-stahl", "externalId": "legacy",
                  "extra": {"keep": True}, "entries": ["unknown", {"recordBase64": "not valid!", "extra": 7}, None]}
        sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{lyonSearchObservations}', "
            + ingest.sql_lit(json.dumps(opaque)) + "::jsonb) WHERE external_id='legacy'")
        opaque_peer = stage_record(record("https://lyonstahl.com/properties/opaque-peer/", "property_path", "opaque-peer"))
        mixed_stage = [staged[0], opaque_peer]
        mixed_probe = sql(identity_probe_sql(mixed_stage)).split("|", 3)
        mixed_held, mixed_prior = json.loads(mixed_probe[2]), json.loads(mixed_probe[3])
        assert mixed_held == []
        mixed_job = str(uuid.uuid4())
        sql(build_partial_sql(mixed_stage, mixed_held, mixed_prior, mixed_job, start, finish))
        assert sql(partial_readback_sql(mixed_stage, mixed_held, mixed_prior, mixed_job, start, finish)) == "1|2|0"
        after_opaque = json.loads(sql("SELECT raw_data->'lyonSearchObservations' FROM credeals.cre_listings WHERE external_id='legacy'"))
        assert {key: value for key, value in after_opaque.items() if key != "entries"} == {key: value for key, value in opaque.items() if key != "entries"}
        assert after_opaque["entries"][:len(opaque["entries"])] == opaque["entries"]
        assert after_opaque["entries"][-1] == {"schema": "lyon-search-entry/v1", "recordBase64": staged[0]["observation_base64"]}
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='opaque-peer'") == "1"
        unsafe = {**opaque, "sourceKey": "different-source"}
        sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data, '{lyonSearchObservations}', "
            + ingest.sql_lit(json.dumps(unsafe)) + "::jsonb) WHERE external_id='legacy'")
        safe_peer = stage_record(record("https://lyonstahl.com/properties/safe-peer/", "property_path", "safe-peer"))
        partition = [staged[0], safe_peer]
        unsafe_probe = sql(identity_probe_sql(partition)).split("|", 3)
        unsafe_held, unsafe_prior = json.loads(unsafe_probe[2]), json.loads(unsafe_probe[3])
        assert unsafe_held == ["legacy"]
        unsafe_job = str(uuid.uuid4())
        sql(build_partial_sql(partition, unsafe_held, unsafe_prior, unsafe_job, start, finish))
        assert sql(partial_readback_sql(partition, unsafe_held, unsafe_prior, unsafe_job, start, finish)) == "1|2|0"
        assert json.loads(sql("SELECT raw_data->'lyonSearchObservations' FROM credeals.cre_listings WHERE external_id='legacy'")) == unsafe
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='safe-peer'") == "1"
        sql("UPDATE credeals.cre_listings SET raw_data=" + ingest.sql_lit(json.dumps(before_opaque)) + "::jsonb WHERE external_id='legacy'")
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
