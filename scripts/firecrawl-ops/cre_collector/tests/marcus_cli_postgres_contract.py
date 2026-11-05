"""Actual guarded Marcus CLI against the caller's disposable PostgreSQL."""
import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import cre_checkpoint_refresh as refresh
import cre_ingest as ingest
from cre_marcus_inventory_checkpoint import admit_inventory, ingest_subset_context, run_replay_worker
from cre_marcus_inventory import exact_json
from cre_marcus_inventory_db import identity_probe_sql, partial_readback_sql
from tests.test_marcus_inventory import listing


def run_cli_contract(sql, container_name):
    """Bind synthetic source/gate evidence, run the shipped CLI and read back."""
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
            raise RuntimeError("CLI fixture requires a clean exact-head checkout")
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
        lock_path = root / "lock"
        with patch.object(refresh, "canonical_shared_lock_dir", return_value=lock_path), refresh.SharedLock(lock_path) as owner:
            candidate = run_replay_worker(run_dir, manifest, owner, lambda *_: None)
            stats = refresh._validate_source_artifact_local(candidate, "marcus-millichap", child_start,
                expected_generation_id=run_dir.name, expected_generation_started_at=generation_start)
            checkpoint["artifact"] = {**stats, "path": str(candidate.relative_to(run_dir))}
            from cre_marcus_inventory_checkpoint import verify_candidate_artifact
            verified = verify_candidate_artifact(candidate, manifest, current_code_sha=code_sha, current_git_dirty=False)
            source, staged = verified["source"], verified["staged"]
            probe_data = sql(identity_probe_sql(staged, source["generation"], source["started_at"], source["finished_at"])).split("|", 3)
            assert probe_data[:2] == ["1", "1"]
            held, preimage = json.loads(probe_data[2]), json.loads(probe_data[3], parse_float=Decimal)
            assert held == []
            encoded = exact_json(preimage).encode("ascii")
            preimage_path = run_dir / "marcus-millichap-partial" / "preimage-1-0123456789abcdef.json"
            preimage_path.write_bytes(encoded)
            preimage_path.chmod(0o600)
            staged_bytes = json.dumps(staged, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
            checkpoint["marcus_inventory_probe"] = {"artifact_sha256": verified["sha256"], "generation_id": run_dir.name,
                "producer_sha": code_sha, "database_target_sha256": target, "staged_sha256": hashlib.sha256(staged_bytes).hexdigest(),
                "held": held, "preimage_path": str(preimage_path.relative_to(run_dir)), "preimage_bytes": len(encoded),
                "preimage_sha256": hashlib.sha256(encoded).hexdigest()}
            report = {"generated_for_artifact": str(candidate), "summary": {"mode": "live", "torow_errors": 0,
                "baseline_updated": False, "baseline_update_sources": [], "hold_sources": ["marcus-millichap"],
                "mark_missing_safe_sources": [], "mark_missing_safe_brokerages": [], "verdict_counts": {}},
                "per_source": {"marcus-millichap": {"verdict": "first_seen",
                "reason": "no baseline row; cannot gate (first sight); a source pass also errored this run",
                "mark_missing_safe": False, "current_active": 1}}}
            aggregate = copy.deepcopy(report)
            binding = admit_inventory(candidate, manifest, report, current_code_sha=code_sha, current_git_dirty=False, phase="source")
            checkpoint["gate"] = copy.deepcopy(report["per_source"]["marcus-millichap"])
            assert admit_inventory(candidate, manifest, aggregate, current_code_sha=code_sha,
                                   current_git_dirty=False, phase="aggregate") == binding
            attempt_id = uuid.uuid4().hex
            envelope = {"manifest": manifest, "source_report": report, "aggregate_report": aggregate,
                        "binding": binding, "attempt_id": attempt_id}
            context = ingest_subset_context(candidate, envelope, current_code_sha=code_sha, current_git_dirty=False,
                                            expected_database_target_sha256=target, attempt_id=attempt_id)
            claims = run_dir / "marcus-millichap-partial" / "claims.json"
            claims.write_text(json.dumps(envelope), encoding="utf8")
            claims.chmod(0o600)
            env_file = root / "synthetic.env"
            env_file.write_text("POSTGRES_URL_NON_POOLING=" + db_url + "\n", encoding="utf8")
            env_file.chmod(0o600)
            shim = root / "psql"
            shim.write_text("#!/bin/sh\nexec docker exec -i " + shlex.quote(container_name) +
                            " psql -U postgres \"$@\"\n", encoding="utf8")
            shim.chmod(0o700)
            env = {"PATH": os.environ["PATH"], "HOME": str(root), "PSQL_BIN": str(shim)}
            cli = subprocess.run([sys.executable, str(HERE / "cre_ingest.py"), "--in", str(candidate),
                "--marcus-inventory-claims", str(claims), "--require-strict-freshness", "--env-file", str(env_file),
                "--expected-db-target-sha256", target, "--checkpoint-ingest-attempt", attempt_id],
                cwd=HERE, env=env, text=True, capture_output=True, timeout=120)
            if cli.returncode:
                raise RuntimeError("synthetic Marcus guarded CLI failed: " + cli.stderr[-4000:])
            readback = partial_readback_sql(context["staged"], context["held"], context["preimage"], context["source"], context["job_id"])
            assert sql(readback) == "1|1|0"
            assert sql("SELECT count(*) FROM credeals.cre_listings WHERE external_id='301' AND status IS NULL AND scraped_at IS NULL") == "1"
            print("Marcus actual guarded CLI -> disposable PostgreSQL -> exact readback PASS")


HERE = Path(__file__).resolve().parents[1]
