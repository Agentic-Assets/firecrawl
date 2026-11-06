"""Mixed NAI pipeline contract against the disposable PostgreSQL fixture.

NAI, Savills and Matthews are synthetic owned child processes. The database
unit, gates, serial ingest and validation/readback are real. No provider is
called, and this does not test the NAI source adapter.
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
from tests.test_nai_post_partial import post


def run_mixed_contract(sql, container_name, shared_tmp, *, inventory_only=False):
    with tempfile.TemporaryDirectory(prefix="nai-mixed-contract-", delete=False) as temporary:
        root = Path(temporary)
        print(f"NAI mixed private evidence: {root}", flush=True)
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
VALUES ('savills', 'savills', 2, 2), ('nai-global', 'nai-global', 1080, 1080);
INSERT INTO credeals.cre_listings
    (brokerage_id,external_id,source_url,canonical_url,status,title,scraped_at,raw_data)
SELECT id,'infabode:old','https://example.test/old',
       'https://www.naiglobal.com/listings/?propertyId=post-2','active','Conflict',
       '2020-01-01T00:00:00Z','{}'::jsonb
FROM credeals.cre_brokerages WHERE slug='nai-global';
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
        code_sha, dirty = refresh.git_identity()
        if dirty or not code_sha:
            raise RuntimeError("mixed coordinator fixture requires a clean exact-head checkout")
        sources = ("nai-global", "savills", "matthews")
        manifest = refresh.new_manifest(run_dir, git_sha=code_sha, git_dirty=False, sources=sources,
            page_cap=3, concurrency=1, source_workers=2, pipeline=True,
            database_target={"algorithm": "sha256", "value": target})
        env = {"PATH": os.environ["PATH"], "HOME": str(root), "PSQL_BIN": str(shim), "TMPDIR": str(shared_tmp)}

        def start(directory, owner, source, **kwargs):
            attempt, temporary_artifact, log_path, started = refresh._cohort_attempt(
                directory, owner, source, collector_concurrency=1)
            stamp = refresh.utc_now()
            scope = "source_native_public_record" if source == "nai-global" else "detail_page"
            payload = strict_artifact(source, detail_scope=scope, generation=owner["run_id"],
                                      generation_started_at=owner["started_at"])
            if source == "nai-global":
                payload["listings"] = [post(1), post(2, tx="lease")]
                for row in payload["listings"]:
                    row["freshnessProvenance"]["generationId"] = owner["run_id"]
                if inventory_only:
                    script = """
const [input, generation, observed] = process.argv.slice(1);
process.argv = [process.argv[0], 'nai-fixture'];
globalThis.fetch = async () => { throw new Error('NETWORK_DENIED'); };
const { naiListingFromFeed } = await import('./sources/nai-global.ts');
const row = JSON.parse(input);
const mapped = naiListingFromFeed(row, 'sale', null, 'synthetic unavailable detail', true,
  {generationId:generation, observedAt:observed, method:'infabode_public_posts_graphql', cacheDisposition:'live'});
console.log(JSON.stringify({...mapped, sourceKey:'nai-global', transactionMode:'sale'}));
"""
                    mapped = subprocess.run(["node", "--import", "tsx", "--input-type=module", "-e", script,
                        json.dumps(payload["listings"][0]["feedRow"]), owner["run_id"], stamp],
                        cwd=Path(__file__).resolve().parents[1], env=env, check=True,
                        capture_output=True, text=True, timeout=30)
                    payload["listings"][0] = json.loads(mapped.stdout)
                    payload["sources"][0]["freshness"].update(detailErrors=1, detailObserved=0, childPreservationRows=1)
            payload["runMeta"].update(startedAt=stamp, finishedAt=stamp)
            for entry in payload["sources"]:
                entry.update(totalAvailableOnSource=entry["listingsCollected"],
                    sourceUrl="https://www.savills.us/properties/", method="synthetic-owned-worker")
            for row in payload["listings"]:
                row["inventoryObservedAt"] = stamp
                if (row.get("freshnessProvenance") or {}).get("detailScope") != "inventory_only":
                    row["detailObservedAt"] = stamp
                if source == "savills":
                    row["canonicalUrl"] = row["url"]
            child = ("import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])"
                     if source in {"nai-global", "savills"} else f"import sys; sys.exit({refresh.COHORT_SETTLED_FAILURE_EXIT})")
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
                assert results["nai-global"]["partial"] is True
                assert results["nai-global"]["whole_source_coverage"] is False
                gate = manifest["sources"]["nai-global"]["gate"]
                assert gate["raw_verdict"] == "hold" and gate["mark_missing_safe"] is False
                assert gate["current_inventory_proof"]["emitted_count"] == 1
                assert gate["current_inventory_proof"]["held_count"] == 1
                assert results["savills"]["state"] == "verified"
                assert results["matthews"]["state"] == "failed"
                assert manifest["sources"]["nai-global"]["readback"]["kind"] == "nai_public_post_partial"
                assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='infabode:1' AND status IS NULL") == "1"
                assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='infabode:2'") == "0"
                assert sql("SELECT count(*) FROM credeals.cre_source_index si JOIN credeals.cre_brokerages b ON b.id=si.brokerage_id WHERE b.slug='nai-global'") == "0"
                assert sql("SELECT count(*) FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id WHERE b.slug='savills' AND l.external_id IN ('id-1','id-2')") == "2"
                assert sql("SELECT count(*) FROM credeals.cre_listings l JOIN credeals.cre_brokerages b ON b.id=l.brokerage_id WHERE b.slug='matthews'") == "0"
                print("NAI mixed actual pipeline, disposable PG and readback PASS")
            finally:
                owner.clear_recovery_requirement()
