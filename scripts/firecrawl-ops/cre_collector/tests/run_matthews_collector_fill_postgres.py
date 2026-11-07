"""Exercise the owned checkpoint with synthetic data and a disposable DB."""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from matthews_partial.checkpoint import derive, prepare_candidate


def run(*args, input_text=None):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, check=False, timeout=90)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def main(path, started, finished, mode="pipeline"):
    raw=Path(path).read_bytes()
    candidate=derive(raw,generation_id="fixture-generation",generation_started_at=started,
                     attempt_started_at=started,attempt_finished_at=finished)
    assert {item["external_id"] for item in candidate["rows"]}=={"good-sale","leasing-good"}
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
        import cre_checkpoint_refresh as refresh
        import cre_ingest
        import cre_refresh_pipeline as pipeline

        code, dirty=refresh.git_identity()
        assert not dirty, "test must run on a clean committed tree"
        with tempfile.TemporaryDirectory(prefix="matthews-owned-fixture-") as scratch:
            work=Path(scratch); root=work/"fixture-generation"; (root/"rejected").mkdir(parents=True)
            original=root/"rejected"/"matthews.json"; original.write_bytes(raw); os.chmod(original,0o600)
            attempt={"number":1,"rc":1,"started_at":started,"finished_at":finished,
                "rejected_artifact":"rejected/matthews.json","rejected_artifact_sha256":hashlib.sha256(raw).hexdigest()}
            synthetic_url="postgresql://postgres:synthetic-only@127.0.0.1/postgres"
            target=cre_ingest.database_target_fingerprint_from_url(synthetic_url)
            canonical_lock=refresh.canonical_shared_lock_dir()
            manifest={"schema_version":refresh.SCHEMA_VERSION,"run_id":"fixture-generation","started_at":started,"collector_git_sha":code,"collector_git_dirty":False,
                "preflight":{"database_target":target,"shared_lock":str(canonical_lock)},
                "scope":{"source_keys":["matthews"]},"sources":{"matthews":{"attempts":[attempt]}},
                "config":{"pipeline":mode == "pipeline","sources":["matthews"],"transactions":["sale","lease"],"max_items":0,
                    "additive":True,"mark_missing":False,"status_activation":False,"page_cap":0,"concurrency":1,
                    "source_workers":1,"host_cpu_guard":{},"admit_baseline_hold_additively":False}}
            info=prepare_candidate(root,manifest,attempt,git_identity=refresh.git_identity)
            attempt["matthews_partial_candidate"]={"state":"sealed_unadmitted",**info}
            gate_path=root/info["gate_path"]
            promoted=refresh.promote_matthews_partial_candidate(root,manifest,attempt,candidate_only=False)
            assert promoted is not None and promoted[0]==gate_path
            checkpoint=manifest["sources"]["matthews"]
            env_file=work/"synthetic.env"; env_file.write_text("POSTGRES_URL="+synthetic_url+"\n"); os.chmod(env_file,0o600)
            psql=work/"psql-fixture"
            psql.write_text("#!/bin/bash\nargs=(\"$@\")\nn=${#args[@]}\n"
                "if (( n > 1 )) && [[ \"${args[n-2]}\" == '-f' && \"${args[n-1]}\" != '-' ]]; then\n"
                "  file=\"${args[n-1]}\"; args[n-1]='-'; cat -- \"$file\" | docker exec -i "+container_id+
                " psql -U postgres \"${args[@]}\"\nelse\n  exec docker exec -i "+container_id+
                " psql -U postgres \"${args[@]}\"\nfi\n")
            os.chmod(psql,0o700)
            old_psql=os.environ.get("PSQL_BIN"); os.environ["PSQL_BIN"]=str(psql)
            cre_ingest.PSQL_CANDIDATES.insert(0,str(psql))
            try:
                canonical_lock.parent.mkdir(parents=True,exist_ok=True)
                with refresh.SharedLock(canonical_lock, recovery_required=True) as owner:
                    token=pipeline._OWNER_CHECK.set(owner._require_authority) if mode == "pipeline" else None
                    lease=pipeline._OWNER_LEASE.set(owner.lease_token) if mode == "pipeline" else None
                    try:
                        refresh.gate_source(root,manifest,"matthews",gate_path,str(env_file))
                        assert checkpoint["gate"]["verdict"]=="ok_additive_verified_subset"
                        warnings=checkpoint["gate"]["current_inventory_proof"]["receipt_warnings"]
                        if mode=="serial":
                            assert len(warnings)==1 and warnings[0]["reported"]==[0,0,0] and warnings[0]["verified"]==[1,0,0]
                        else:
                            assert warnings==[]
                        assert refresh.dry_run_source(root,manifest,"matthews",gate_path)
                        if mode == "serial":
                            refresh.run_serial_aggregate_gate(root,manifest,str(env_file),owner_check=owner._require_authority)
                        else:
                            refresh.run_aggregate_gate(root,manifest,str(env_file),sources=["matthews"])
                        if mode == "serial":
                            assert refresh.ingest_admitted_sources(root,manifest,str(env_file),owner_check=owner._require_authority) == []
                        else:
                            refresh.ingest_source(root,manifest,"matthews",gate_path,str(env_file))
                        assert checkpoint["state"]=="ingested" and checkpoint["ingest"]["rc"]==0
                        proof_read=refresh.matthews_partial_readback(root,manifest,str(env_file))
                        validation={"queries":{"freshness_generations":[],"inventory_only_index":[]},
                                    "matthews_partial_readback":proof_read}
                        assert refresh.verify_validation_readback(root,manifest,validation,persist_manifest=False)=={"ok":True,"failed_sources":[]}
                        assert checkpoint["readback"]["whole_source_coverage"] is False
                    finally:
                        if mode == "pipeline":
                            pipeline._OWNER_CHECK.reset(token); pipeline._OWNER_LEASE.reset(lease)
                    owner.clear_recovery_requirement()
            finally:
                cre_ingest.PSQL_CANDIDATES.remove(str(psql))
                if old_psql is None: os.environ.pop("PSQL_BIN",None)
                else: os.environ["PSQL_BIN"]=old_psql
            assert proof_read["verified_jobs"]==1 and proof_read["held"]==[] and proof_read["updated"]==["good-sale","leasing-good"], proof_read
            assert sql("SELECT count(*) FROM credeals.cre_listings WHERE title='Good peer' AND raw_data='{\"prior\":true}'::jsonb AND status='active';")=="2"
            assert len(candidate["held"])==1 and candidate["mark_missing_safe"] is False
        print("offline collector artifact to isolated PostgreSQL passed")
    finally:
        if container_id:
            subprocess.run(["docker","rm","-f",container_id],capture_output=True,check=False)

if __name__=="__main__":
    main(*sys.argv[1:])
