"""Small real-schema proof for the NAI post-identity transaction."""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from cre_nai_post_partial import build_partial_sql, identity_probe_sql, partial_readback_sql, stage_post  # noqa: E402
from tests.test_nai_post_partial import post  # noqa: E402


def run(*args, text=None, check=True):
    return subprocess.run(args, input=text, text=True, capture_output=True, check=check, timeout=90)


def main():
    name = "nai-post-proof-" + uuid.uuid4().hex[:12]

    def sql(query):
        result = run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA",
                     "-v", "ON_ERROR_STOP=1", text=query, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

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
            raise RuntimeError("disposable postgres not ready")
        directory = HERE.parent.parent / "sql"
        master = (directory / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (directory / m[1]).read_text(), master, flags=re.MULTILINE)
        schema = master + "\n" + "\n".join([
            "\\set CRE_APPROVE_LISTING_LIFECYCLE 1", "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4284",
            "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory / "016_cre_listing_lifecycle.sql").read_text()])
        sql(schema)
        sql("""
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,canonical_url,status,title,scraped_at,raw_data)
SELECT id,'infabode:1','https://infabode.com/services/listings/1',NULL,'active','Existing title',
 '2026-11-05T09:00:00Z','{"sourceKey":"nai-global","id":"infabode:1","existing":true}'::jsonb
FROM credeals.cre_brokerages WHERE slug='nai-global';
INSERT INTO credeals.cre_listings (brokerage_id,external_id,source_url,canonical_url,status,title,raw_data)
SELECT id,'infabode:old','https://example.test/old',
 'https://www.naiglobal.com/listings/?propertyId=post-2','active','Conflict','{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='nai-global';
INSERT INTO credeals.cre_listing_contacts(listing_id,name)
SELECT id,'Preserve me' FROM credeals.cre_listings WHERE external_id='infabode:1';
""")
        staged = [stage_post(post(i), "gen-1") for i in (1, 2, 3)]
        result = sql(identity_probe_sql(staged)).split("|", 3)
        assert result[:2] == ["3", "3"]
        held, before = json.loads(result[2]), json.loads(result[3])
        assert held == ["infabode:2"], held
        job_id = str(uuid.uuid4())
        sql(build_partial_sql(staged, held, before, job_id,
                              "2026-11-05T09:59:00Z", "2026-11-05T10:01:00Z"))
        readback = partial_readback_sql(staged, held, before, job_id,
                                        "2026-11-05T09:59:00Z", "2026-11-05T10:01:00Z")
        assert sql(readback) == "1|3|0"
        result = json.loads(sql("""
SELECT json_build_object(
 'existing_title',(SELECT title FROM credeals.cre_listings WHERE external_id='infabode:1'),
 'existing_history',(SELECT jsonb_array_length(raw_data#>'{naiPublicPostHistory,entries}')
    FROM credeals.cre_listings WHERE external_id='infabode:1'),
 'preserved_contacts',(SELECT count(*) FROM credeals.cre_listing_contacts c
    JOIN credeals.cre_listings l ON l.id=c.listing_id WHERE l.external_id='infabode:1'),
 'held_untouched',(SELECT count(*) FROM credeals.cre_listings WHERE external_id='infabode:2'),
 'new_rows',(SELECT count(*) FROM credeals.cre_listings WHERE external_id='infabode:3'
    AND status IS NULL AND canonical_url IS NULL AND scraped_at IS NULL
    AND raw_data->>'identityScope'='infabode_public_post'),
 'index_rows',(SELECT count(*) FROM credeals.cre_source_index si
    JOIN credeals.cre_brokerages b ON b.id=si.brokerage_id WHERE b.slug='nai-global'))::text;
"""))
        assert result == {"existing_title": "Existing title", "existing_history": 1,
                          "preserved_contacts": 1, "held_untouched": 0,
                          "new_rows": 1, "index_rows": 0}, result
        print("NAI post partial real-schema proof passed")
    finally:
        run("docker", "rm", "-f", name, check=False)


if __name__ == "__main__":
    main()
