"""Exercise the experimental fill against an isolated, networkless database."""
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from matthews_partial.fill import fill_sql, readback_sql, stage_observations


def run(*args, input_text=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, check=False, timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


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
    container_id = None
    try:
        container_id = run("docker", "run", "-d", "--rm", "--pull=never", "--name", name, "--network=none", "--cpus=0.5", "--memory=512m", "--pids-limit=128", "--tmpfs", "/var/lib/postgresql/data:rw,size=256m", "-e", "POSTGRES_PASSWORD=synthetic-only", "postgres:17")
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
        sql("""INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,canonical_url,status,raw_data,title,scraped_at)
SELECT id,'known','https://www.matthews.com/properties/known','https://www.matthews.com/properties/known','active','{\"old\":\"retained\"}','Existing title',NULL FROM credeals.cre_brokerages WHERE slug='matthews';
INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,status,raw_data,title)
SELECT id,'other','https://www.matthews.com/properties/conflict','active','{}','Other' FROM credeals.cre_brokerages WHERE slug='matthews';
INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,canonical_url,status,raw_data,scraped_at)
SELECT id,'bad','https://www.matthews.com/properties/bad','https://www.matthews.com/properties/bad','active','{}',NULL FROM credeals.cre_brokerages WHERE slug='matthews';
INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,canonical_url,status,raw_data,scraped_at)
SELECT id,'stale','https://www.matthews.com/properties/stale','https://www.matthews.com/properties/stale','active','{}','2026-11-08T00:00:00Z' FROM credeals.cre_brokerages WHERE slug='matthews';
CREATE FUNCTION credeals.fixture_bad_row() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.external_id='bad' THEN RAISE check_violation USING MESSAGE='synthetic record-local failure'; END IF;
 RETURN NEW; END $$;
CREATE TRIGGER fixture_bad_row BEFORE UPDATE ON credeals.cre_listings FOR EACH ROW EXECUTE FUNCTION credeals.fixture_bad_row();
INSERT INTO credeals.cre_source_index(brokerage_id,external_id,source_key,url,last_seen,last_enumerated_at)
SELECT id,'known','matthews','https://www.matthews.com/properties/known',NULL,NULL FROM credeals.cre_brokerages WHERE slug='matthews';
INSERT INTO credeals.cre_listing_images(listing_id,url)
SELECT id,'https://example.test/old.jpg' FROM credeals.cre_listings WHERE external_id='known';""")
        staged=stage_observations([observation("known",name="Incoming title",description="New description",buildingSizeSqft=4000,capRatePct=6.5),
                                   observation("bad",name="Fails"),observation("conflict",name="Conflicted"),observation("new",name="New"),observation("stale",name="Stale")],"fixture","2026-11-06T23:59:00Z","2026-11-07T00:02:00Z")
        binding = {"artifact_sha256": "a" * 64, "database_target_sha256": "b" * 64,
                   "producer_sha": "c" * 40, "generation_id": "fixture", "attempt_id": "d" * 32,
                   "started_at": "2026-11-06T23:59:00Z", "finished_at": "2026-11-07T00:02:00Z"}
        before = json.loads(sql(readback_sql(staged, binding)))
        assert before["occupied_jobs"] == 0 and before["matching_jobs"] == 0 and before["verified_jobs"] == 0, before
        sql(fill_sql(staged, binding))
        receipt = json.loads(sql(readback_sql(staged, binding)))
        assert receipt == {"occupied_jobs": 1, "matching_jobs": 1, "verified_jobs": 1, "held": ["bad", "conflict", "new", "stale"], "held_reasons": {"bad": "record_write_rejected", "conflict": "identity_conflict_or_unknown", "new": "identity_conflict_or_unknown", "stale": "identity_conflict_or_unknown"}, "updated": ["known"]}, receipt
        reread=sql("""SELECT jsonb_build_object('title',title,'description',description,'status',status,'raw_data',raw_data,
'size_sf',size_sf,'cap_rate',cap_rate,'images',(SELECT jsonb_agg(url) FROM credeals.cre_listing_images WHERE listing_id=l.id)) FROM credeals.cre_listings l WHERE external_id='known';""")
        assert json.loads(reread)=={"title":"Existing title","description":"New description","status":"active","raw_data":{"old":"retained"},"size_sf":4000,"cap_rate":0.065,"images":["https://example.test/old.jpg"]}, reread
        assert sql("SELECT count(*) FROM credeals.cre_listings;")=="4"
        wrong_binding = {**binding, "attempt_id": "1" * 32}
        occupied = json.loads(sql(readback_sql(staged, wrong_binding)))
        assert occupied["occupied_jobs"] == 1 and occupied["matching_jobs"] == 0 and occupied["verified_jobs"] == 0
        duplicate = subprocess.run(["docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1"],
            input=fill_sql(staged, binding), text=True, capture_output=True, check=False, timeout=90)
        assert duplicate.returncode != 0 and "reconcile before replay" in duplicate.stderr
        sql("UPDATE credeals.cre_listing_images SET alt_text='changed' WHERE url='https://example.test/old.jpg';")
        changed = json.loads(sql(readback_sql(staged, binding)))
        assert changed["matching_jobs"] == 1 and changed["verified_jobs"] == 0, changed
        sql("""CREATE FUNCTION credeals.fixture_clobber() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
 IF NEW.external_id='known' THEN NEW.title='corrupted'; END IF; RETURN NEW; END $$;
 CREATE TRIGGER fixture_clobber BEFORE UPDATE ON credeals.cre_listings FOR EACH ROW EXECUTE FUNCTION credeals.fixture_clobber();""")
        second_binding = {**binding, "artifact_sha256": "e" * 64, "attempt_id": "f" * 32}
        second_staged = stage_observations([observation("known",street="New address")], "fixture",
            "2026-11-06T23:59:00Z", "2026-11-07T00:02:00Z")
        rejected = subprocess.run(["docker", "exec", "-i", name, "psql", "-U", "postgres", "-X", "-q", "-tA", "-v", "ON_ERROR_STOP=1"],
            input=fill_sql(second_staged, second_binding), text=True, capture_output=True, check=False, timeout=90)
        assert rejected.returncode != 0 and "preserved parent or children changed" in rejected.stderr, rejected.stderr
        assert json.loads(sql(readback_sql(second_staged, second_binding)))["matching_jobs"] == 0
        assert sql("SELECT title FROM credeals.cre_listings WHERE external_id='known';") == "Existing title"
        print("isolated postgres fill/readback passed")
    finally:
        if container_id:
            subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, check=False)

if __name__ == "__main__":
    main()
