"""Exercise the disconnected fill on a synthetic collector artifact, never a live DB."""
import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cre_matthews_partial_checkpoint import derive
from cre_matthews_partial_fill import fill_sql, readback_sql


def run(*args, input_text=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, check=False, timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def main(path, started, finished):
    raw=Path(path).read_bytes()
    candidate=derive(raw,generation_id="fixture-generation",generation_started_at=started,
                     attempt_started_at=started,attempt_finished_at=finished)
    assert {item["external_id"] for item in candidate["rows"]}=={"good-sale","leasing-good"}
    binding={"artifact_sha256":hashlib.sha256(raw).hexdigest(),"database_target_sha256":"b"*64,
             "producer_sha":"c"*40,"generation_id":"fixture-generation","attempt_id":"d"*32,
             "started_at":candidate["started_at"],"finished_at":candidate["finished_at"]}
    name="matthews-e2e-"+uuid.uuid4().hex[:12]
    container_id=None
    def sql(query):
        return run("docker","exec","-i",name,"psql","-U","postgres","-X","-q","-tA","-v","ON_ERROR_STOP=1",input_text=query)
    try:
        container_id=run("docker","run","-d","--rm","--pull=never","--name",name,"--network=none","--cpus=0.5",
                         "--memory=512m","--pids-limit=128","--tmpfs","/var/lib/postgresql/data:rw,size=256m",
                         "-e","POSTGRES_PASSWORD=synthetic-only","postgres:17")
        for _ in range(100):
            ready=subprocess.run(["docker","exec",name,"pg_isready","-U","postgres"],capture_output=True,check=False)
            logs=subprocess.run(["docker","logs",name],capture_output=True,text=True,check=False)
            if ready.returncode==0 and (logs.stdout+logs.stderr).count("database system is ready to accept connections")>=2:
                break
            time.sleep(.2)
        else:
            raise RuntimeError("disposable database did not start")
        directory=Path(__file__).resolve().parents[2]/"sql"
        master=(directory/"000_run_all.sql").read_text()
        master=re.sub(r"^\\i (\S+)\s*$",lambda m:(directory/m[1]).read_text(),master,flags=re.MULTILINE)
        sql(master)
        lifecycle="\n".join(["\\set CRE_APPROVE_LISTING_LIFECYCLE 1","\\set CRE_LISTING_LIFECYCLE_OPERATOR cayman",
            "\\set CRE_LISTING_LIFECYCLE_APPROVAL_REF AGENTIC-4308","\\set CRE_LISTING_LIFECYCLE_CONFIRM 'APPLY 016_cre_listing_lifecycle'",
            (directory/"016_cre_listing_lifecycle.sql").read_text()])
        sql(lifecycle)
        sql("""INSERT INTO credeals.cre_listings(brokerage_id,external_id,source_url,canonical_url,status,raw_data,title,scraped_at)
SELECT id, x, 'https://www.matthews.com/properties/'||x,'https://www.matthews.com/properties/'||x,
'active','{\"prior\":true}',NULL,NULL FROM credeals.cre_brokerages CROSS JOIN unnest(ARRAY['good-sale','leasing-good']) x WHERE slug='matthews';""")
        sql(fill_sql(candidate["rows"],binding))
        proof=json.loads(sql(readback_sql(candidate["rows"],binding)))
        assert proof["verified_jobs"]==1 and proof["held"]==[] and proof["updated"]==["good-sale","leasing-good"], proof
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE title='Good peer' AND raw_data='{\"prior\":true}'::jsonb AND status='active';")=="2"
        assert len(candidate["held"])==1 and candidate["mark_missing_safe"] is False
        print("offline collector artifact to isolated PostgreSQL passed")
    finally:
        if container_id:
            subprocess.run(["docker","rm","-f",container_id],capture_output=True,check=False)

if __name__=="__main__":
    main(*sys.argv[1:])
