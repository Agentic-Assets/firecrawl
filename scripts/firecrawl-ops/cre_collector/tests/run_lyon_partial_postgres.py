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
from cre_lyon_partial import (
    build_partial_sql,
    identity_probe_sql,
    partial_readback_sql,
    stage_record,
)
from tests.test_lyon_partial_stage import record


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
        sql("UPDATE credeals.cre_listings SET canonical_url='https://lyonstahl.com/properties/collision/' WHERE external_id='legacy'")
        collision = stage_record(record("https://lyonstahl.com/properties/collision/", "property_path", "collision"))
        assert json.loads(sql(identity_probe_sql([collision])).split("|", 3)[2]) == ["collision"]
        sql("UPDATE credeals.cre_listings SET raw_data=jsonb_set(raw_data,'{lyonSearchObservations}', '\"broken\"'::jsonb) WHERE external_id='legacy'")
        assert json.loads(sql(identity_probe_sql([staged[0]])).split("|", 3)[2]) == ["legacy"]
        sql("UPDATE credeals.cre_listings SET raw_data=raw_data || '{\"lyonStahlPostId\":\"999\"}'::jsonb WHERE external_id='wordpress:40201'")
        assert json.loads(sql(identity_probe_sql([staged[1]])).split("|", 3)[2]) == ["wordpress:40201"]
        print("Lyon disposable PostgreSQL: identity hold, null status, additive history, children, stale preimage and readback passed")
    finally:
        run("docker", "rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
