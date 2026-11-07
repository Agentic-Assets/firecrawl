"""Exercise the experimental fill against an isolated, networkless database."""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cre_matthews_partial_fill import fill_sql, stage_observations


def run(*args, input_text=None):
    return subprocess.run(args, input=input_text, text=True, capture_output=True, check=True, timeout=90).stdout.strip()


def observation(identity, **fields):
    url = "https://www.matthews.com/properties/" + identity
    return {"schema": "matthews-completed-parse-observation/v1", "sourceKey": "matthews", "observation": {
        "id": identity, "url": url, "canonicalUrl": url,
        "inventoryObservedAt": "2026-11-07T00:00:00Z", "detailObservedAt": "2026-11-07T00:01:00Z",
        "freshnessProvenance": {"generationId": "fixture", "method": "matthews_canonical_detail",
            "identityMethod": "canonical_property_url", "cacheDisposition": "live", "detailScope": "detail_page"}, **fields}}


def main():
    name = "matthews-partial-fill-" + uuid.uuid4().hex[:12]
    def sql(text):
        return run("docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1", input_text=text)
    try:
        run("docker", "run", "-d", "--rm", "--name", name, "--network=none", "--cpus=0.5", "--memory=512m", "--pids-limit=128", "--tmpfs", "/var/lib/postgresql/data:rw,size=256m", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
        for _ in range(100):
            probe = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True, check=False)
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=False)
            if probe.returncode == 0 and (logs.stdout + logs.stderr).count("database system is ready to accept connections") >= 2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("isolated postgres did not start")
        migration_dir = Path(__file__).resolve().parents[2] / "sql"
        master = (migration_dir / "000_run_all.sql").read_text()
        master = re.sub(r"^\\i (\S+)\s*$", lambda m: (migration_dir / m[1]).read_text(), master, flags=re.MULTILINE)
        sql(master)
        lifecycle = "\n".join(["\\set CRE_APPROVE_LISTING_LIFECYCLE 1", "\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4308", "\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (migration_dir / "016_cre_listing_lifecycle.sql").read_text()])
        sql(lifecycle)
        sql("""INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,canonical_url,status,raw_data,title)
SELECT id,'known','https://www.matthews.com/properties/known','https://www.matthews.com/properties/known','active','{\"old\":\"retained\"}','Existing title' FROM credeals.cre_brokerages WHERE slug='matthews';
INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,status,raw_data,title)
SELECT id,'other','https://www.matthews.com/properties/conflict','active','{}','Other' FROM credeals.cre_brokerages WHERE slug='matthews';
INSERT INTO credeals.cre_source_index(brokerage_id,external_id,source_key,url)
SELECT id,'known','matthews','https://www.matthews.com/properties/known' FROM credeals.cre_brokerages WHERE slug='matthews';
INSERT INTO credeals.cre_listing_images(listing_id,url)
SELECT id,'https://example.test/old.jpg' FROM credeals.cre_listings WHERE external_id='known';""")
        staged=stage_observations([observation("known",name="Incoming title",description="New description",buildingSizeSqft=4000,capRatePct=6.5),
                                   observation("conflict",name="Conflicted"),observation("new",name="New")],"fixture","2026-11-06T23:59:00Z","2026-11-07T00:02:00Z")
        result=sql(fill_sql(staged))
        assert "known|updated|" in result, result
        assert "conflict|held|" in result and "new|held|" in result, result
        reread=sql("""SELECT jsonb_build_object('title',title,'description',description,'status',status,'raw_data',raw_data,
'size_sf',size_sf,'cap_rate',cap_rate,'images',(SELECT jsonb_agg(url) FROM credeals.cre_listing_images WHERE listing_id=l.id)) FROM credeals.cre_listings l WHERE external_id='known';""")
        assert json.loads(reread)=={"title":"Existing title","description":"New description","status":"active","raw_data":{"old":"retained"},"size_sf":4000,"cap_rate":0.065,"images":["https://example.test/old.jpg"]}, reread
        assert sql("SELECT count(*) FROM credeals.cre_listings;")=="2"
        assert "known|unchanged|" in sql(fill_sql(staged))
        print("isolated postgres fill/readback passed")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)

if __name__ == "__main__":
    main()
