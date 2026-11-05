"""Coordinator, guarded Marcus CLI and readback on disposable PostgreSQL."""
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import cre_checkpoint_refresh as refresh
import cre_ingest as ingest
from cre_marcus_inventory_checkpoint import run_replay_worker
from tests.test_marcus_inventory import listing


def run_cli_contract(sql, container_name):
    """Exercise real gates, coordinator ingest and readback from synthetic source."""
    with tempfile.TemporaryDirectory(prefix="marcus-cli-contract-") as temporary:
        root = Path(temporary)
        run_dir = root / "generation"
        rejected = run_dir / "rejected"
        rejected.mkdir(parents=True, mode=0o700)
        rejected.chmod(0o755)
        now = datetime.now(UTC)
        generation_start = (now - timedelta(minutes=2)).isoformat()
        parent_start = (now - timedelta(minutes=1)).isoformat()
        child_start = (now - timedelta(seconds=50)).isoformat()
        observed = (now - timedelta(seconds=40)).isoformat()
        parent_finish = (now - timedelta(seconds=2)).isoformat()
        child_finish = (now - timedelta(seconds=1)).isoformat()
        code_sha, dirty = refresh.git_identity()
        if dirty or not code_sha:
            raise RuntimeError("coordinator fixture requires a clean exact-head checkout")
        db_url = "postgresql://postgres:synthetic-only@localhost/postgres"
        target = ingest.database_target_fingerprint_from_url(db_url)["value"]
        manifest = refresh.new_manifest(run_dir, git_sha=code_sha, git_dirty=False,
            sources=("marcus-millichap",), page_cap=400, concurrency=1, pipeline=True,
            database_target={"algorithm": "sha256", "value": target})
        manifest["started_at"] = generation_start
        row = listing("301", "activity-301")
        row["inventoryObservedAt"] = observed
        row["freshnessProvenance"]["generationId"] = run_dir.name
        artifact = {"runMeta": {"startedAt": child_start, "finishedAt": child_finish,
                    "freshness": {"generationId": run_dir.name, "generationStartedAt": generation_start}},
                    "sources": [{"sourceKey": "marcus-millichap", "transaction": "sale", "listingsCollected": 1,
                                 "freshness": {"detailErrors": 1}}], "listings": [row], "brokers": [], "totalListings": 1}
        original = rejected / "marcus-millichap-attempt-1.json"
        original.write_text(json.dumps(artifact), encoding="utf8")
        original.chmod(0o600)
        attempt = {"number": 1, "rc": 0, "started_at": parent_start, "finished_at": parent_finish,
                   "error": "marcus-millichap/sale freshness metric detailErrors must be zero",
                   "rejected_artifact": str(original.relative_to(run_dir)),
                   "rejected_artifact_sha256": hashlib.sha256(original.read_bytes()).hexdigest()}
        checkpoint = manifest["sources"]["marcus-millichap"]
        checkpoint["attempts"] = [attempt]
        env_file = root / "synthetic.env"
        env_file.write_text("POSTGRES_URL_NON_POOLING=" + db_url + "\n", encoding="utf8")
        env_file.chmod(0o600)
        shim = root / "psql"
        shim.write_text("#!/usr/bin/env python3\n" +
            "import os,sys\n" +
            "args=sys.argv[1:]\n" +
            "if '-f' in args and args[args.index('-f')+1] != '-':\n" +
            " i=args.index('-f')+1; fd=os.open(args[i],os.O_RDONLY); os.dup2(fd,0); os.close(fd); args[i]='-'\n" +
            "os.execvp('docker',['docker','exec','-i'," + repr(container_name) + ",'psql','-U','postgres',*args])\n",
            encoding="utf8")
        shim.chmod(0o700)
        env = {"PATH": os.environ["PATH"], "HOME": str(root), "PSQL_BIN": str(shim)}
        lock_path = root / "lock"
        with patch.object(refresh, "canonical_shared_lock_dir", return_value=lock_path), \
             patch.object(ingest, "find_psql", return_value=str(shim)), \
             patch.object(refresh, "safe_process_env", return_value=env), refresh.SharedLock(lock_path) as owner:
            candidate = run_replay_worker(run_dir, manifest, owner, refresh.save_manifest)
            stats = refresh._validate_source_artifact_local(candidate, "marcus-millichap", child_start,
                expected_generation_id=run_dir.name, expected_generation_started_at=generation_start)
            checkpoint["artifact"] = {**stats, "path": str(candidate.relative_to(run_dir)), "attempt_started_at": child_start}
            checkpoint["state"] = "validated"
            refresh.gate_source(run_dir, manifest, "marcus-millichap", candidate, str(env_file))
            assert checkpoint["gate"]["verdict"] == "ok_additive_verified_subset"
            assert checkpoint["gate"]["mark_missing_safe"] is False
            refresh.run_aggregate_gate(run_dir, manifest, str(env_file), sources=("marcus-millichap",))
            refresh.ingest_source(run_dir, manifest, "marcus-millichap", candidate, str(env_file))
            assert checkpoint["state"] == "ingested" and checkpoint["ingest"]["rc"] == 0
            observed_result = refresh.marcus_inventory_readback(run_dir, manifest, str(env_file))
            assert observed_result["ok"] is True and observed_result["outcome"] == "exact_commit"
            assert observed_result["detail_qualified"] == 0 and observed_result["whole_source_coverage"] is False
            readback = refresh.verify_validation_readback(run_dir, manifest,
                {"queries": {}, "marcus_inventory_readback": observed_result}, sources=("marcus-millichap",))
            assert readback["ok"] is True and checkpoint["readback"]["kind"] == "marcus_inventory_partial"
            assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='301' AND status IS NULL AND scraped_at IS NULL") == "1"
            print("Marcus real source/aggregate gates -> owned coordinator CLI -> bound readback PASS")
