"""Mixed-source pipeline contract against the disposable PostgreSQL fixture.

All network access from the real Marcus producer is denied by the injected
transport. Savills and Matthews are synthetic owned child processes. The
database unit, gates, serial ingest and validation/readback are real.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import cre_checkpoint_refresh as refresh
import cre_ingest as ingest
import cre_refresh_pipeline as pipeline
from cre_owned_processes import OwnedProcessGroup
from tests.test_cre_checkpoint_refresh import strict_artifact


def run_mixed_contract(sql, container_name):
    with tempfile.TemporaryDirectory(prefix="marcus-mixed-contract-", delete=False) as temporary:
        root = Path(temporary)
        print(f"Marcus mixed private evidence: {root}", flush=True)
        run_dir = root / "generation-mixed"
        run_dir.mkdir()
        db_url = "postgresql://postgres:synthetic-only@localhost/postgres"
        target = ingest.database_target_fingerprint_from_url(db_url)["value"]
        env_file = root / "synthetic.env"
        env_file.write_text("POSTGRES_URL_NON_POOLING=" + db_url + "\n", encoding="utf8")
        env_file.chmod(0o600)
        # Seed ordinary, unrelated on-market rows so the production search
        # smoke checks and Savills' previously established baseline are real.
        sql("""
INSERT INTO credeals.cre_source_baseline
    (source_key, brokerage_slug, median_active_rows, last_active_rows)
VALUES ('savills', 'savills', 2, 2);
INSERT INTO credeals.cre_listings
    (brokerage_id, external_id, source_url, canonical_url, status, title,
     address, city, state, property_type, transaction_type, raw_data)
SELECT id, 'synthetic-industrial', 'https://example.test/seed/industrial',
       'https://example.test/seed/industrial', 'active', 'Industrial Lee',
       '100 National Avenue', 'Dallas', 'TX', 'industrial', 'sale', '{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='lee-associates';
INSERT INTO credeals.cre_listings
    (brokerage_id, external_id, source_url, canonical_url, status, title,
     city, state, property_type, transaction_type, raw_data)
SELECT id, 'synthetic-office', 'https://example.test/seed/office',
       'https://example.test/seed/office', 'active', 'Office',
       'Dallas', 'TX', 'office', 'sale', '{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='lee-associates';
""")
        assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id IN ('synthetic-industrial','synthetic-office')") == "2"
        for query in ("'industrial',NULL,'TX',NULL,'sale'", "'office',NULL,NULL,NULL,'sale'",
                      "'Lee',NULL,NULL,NULL,NULL", "'National Avenue',NULL,NULL,NULL,NULL"):
            assert int(sql(f"SELECT count(*) FROM credeals.search_cre_listings({query})")) > 0
        shim = root / "psql"
        shim.write_text("#!/usr/bin/env python3\nimport os,sys\na=sys.argv[1:]\n"
            "if '-f' in a and a[a.index('-f')+1] != '-':\n"
            " i=a.index('-f')+1; fd=os.open(a[i],os.O_RDONLY); os.dup2(fd,0); os.close(fd); a[i]='-'\n"
            "os.execvp('docker',['docker','exec','-i'," + repr(container_name) + ",'psql','-U','postgres',*a])\n",
            encoding="utf8")
        shim.chmod(0o700)
        preload = root / "fake-fetch.mjs"
        preload.write_text('''
import net from "node:net"; import tls from "node:tls"; import http from "node:http"; import https from "node:https";
const deny=()=>{throw Error("NETWORK_DENIED")}; net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
globalThis.fetch=async (input,init={})=>{ const u=String(input);
 if(u.endsWith('/api/contentsearch/properties')) return Response.json({Results:{Properties:[{ActivityId:'a'}],TotalCount:2}});
 if(u.endsWith('/api/contentsearch/mapproperties')) return Response.json({Results:{Properties:[{ActivityId:'a',Latitude:34,Longitude:-118},{ActivityId:'b',Latitude:35,Longitude:-119}]}});
 if(u.endsWith('/api/contentsearch/mappropertydetail')) {const a=JSON.parse(init.body).activityId,id=a==='a'?'501':'502',p='/properties/'+id+'/fixture';
 return Response.json({Results:{ActivityId:a,PropertyUrl:p,PropertyDetail:'<div class="mm-tile" data-dealid="'+id+'" data-activityid="'+a+'"><a href="'+p+'"><h2>Fixture</h2></a></div>'}});}
 if(u.startsWith('https://www.marcusmillichap.com/properties/')) return new Response('<html>unverified</html>',{status:200});
 throw Error('UNEXPECTED_REQUEST');};
''', encoding="utf8")
        code_sha, dirty = refresh.git_identity()
        if dirty or not code_sha:
            raise RuntimeError("mixed coordinator fixture requires a clean exact-head checkout")
        sources = ("marcus-millichap", "savills", "matthews")
        manifest = refresh.new_manifest(run_dir, git_sha=code_sha, git_dirty=False, sources=sources,
            page_cap=3, concurrency=1, source_workers=2, pipeline=True,
            database_target={"algorithm": "sha256", "value": target})
        env = {"PATH": os.environ["PATH"], "HOME": str(root), "PSQL_BIN": str(shim),
               "NODE_OPTIONS": "--import=" + preload.as_uri(), "TMPDIR": os.environ["MARCUS_TEST_TMPDIR"]}
        original_start = refresh._start_cohort_collection

        def start(directory, owner, source, **kwargs):
            if source == "marcus-millichap":
                return original_start(directory, owner, source, **kwargs)
            attempt, temporary_artifact, log_path, started = refresh._cohort_attempt(
                directory, owner, source, collector_concurrency=1)
            stamp = refresh.utc_now()
            payload = strict_artifact(source, detail_scope="detail_page", generation=owner["run_id"],
                generation_started_at=owner["started_at"])
            payload["runMeta"].update(startedAt=stamp, finishedAt=stamp)
            for entry in payload["sources"]:
                entry.update(totalAvailableOnSource=entry["listingsCollected"],
                    sourceUrl="https://www.savills.us/properties/", method="synthetic-owned-worker")
            for row in payload["listings"]:
                row.update(inventoryObservedAt=stamp, detailObservedAt=stamp)
            child = ("import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])"
                     if source == "savills" else f"import sys; sys.exit({refresh.COHORT_SETTLED_FAILURE_EXIT})")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("a")
            process = subprocess.Popen([sys.executable, "-c", child, str(temporary_artifact), json.dumps(payload)],
                stdout=log, stderr=log, start_new_session=True)
            return refresh.CohortCollectionProcess(source, process, log, temporary_artifact,
                attempt, started, OwnedProcessGroup.from_spawn(process))

        lock_path = root / "lock"
        with patch.object(refresh, "canonical_shared_lock_dir", return_value=lock_path), \
             patch.object(ingest, "find_psql", return_value=str(shim)), \
             patch.object(refresh, "safe_process_env", return_value=env), \
             patch.object(refresh, "_start_cohort_collection", start), \
             refresh.SharedLock(lock_path, recovery_required=True) as owner:
            try:
                manifest["preflight"]["shared_lock"] = str(lock_path)
                failures = pipeline.run_bounded_pipeline(run_dir, manifest, sources, page_cap=3, concurrency=1,
                    attempts_this_run=1, env_file=str(env_file), source_workers=2, owner_check=owner._require_authority)
                results = manifest["pipeline"]["source_results"]
                assert failures == ["matthews"]
                assert results["marcus-millichap"]["partial"] is True
                assert results["marcus-millichap"]["whole_source_coverage"] is False
                assert results["savills"]["state"] == "verified"
                assert results["matthews"]["state"] == "failed"
                assert manifest["sources"]["marcus-millichap"]["readback"]["kind"] == "marcus_inventory_partial"
                assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id IN ('501','502') AND status IS NULL") == "2"
                assert sql("SELECT count(*) FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id WHERE b.slug='savills' AND l.external_id IN ('id-1','id-2')") == "2"
                assert sql("SELECT count(*) FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id WHERE b.slug='matthews'") == "0"
                print("Marcus mixed actual pipeline, disposable PG and readback PASS")
            finally:
                owner.clear_recovery_requirement()
