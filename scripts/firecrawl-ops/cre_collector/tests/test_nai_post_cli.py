"""Exercise the actual source-bound NAI ingest CLI without a database."""
import copy
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cre_nai_post_admission import admit, candidate
from tests.test_nai_post_admission import fixture


def test_real_cli_refuses_fabricated_claims_without_live_owner(tmp_path):
    path, manifest, _ = fixture(tmp_path)
    collector = Path(__file__).resolve().parent.parent
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=collector, text=True,
                         capture_output=True, check=True).stdout.strip()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    def clock(offset):
        return (now + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z")
    data = json.loads(path.read_bytes())
    data["runMeta"].update(startedAt=clock(-20), finishedAt=clock(-3))
    data["runMeta"]["freshness"]["generationStartedAt"] = clock(-30)
    data["listings"][0].update(inventoryObservedAt=clock(-18), detailObservedAt=clock(-5))
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    manifest.update(collector_git_sha=sha, started_at=clock(-30), scope={})
    info = manifest["sources"]["nai-global"]["artifact"]
    info.update(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), started_at=clock(-20),
                finished_at=clock(-3), attempt_started_at=clock(-25), freshness_generation_started_at=clock(-30))
    manifest["sources"]["nai-global"]["attempts"][-1].update(started_at=clock(-25), finished_at=clock(-1))
    binding, _ = candidate(path, manifest, code_sha=sha, dirty=False)
    before = ('[{"external_id":"infabode:1","listing":null,"source_index":null,'
              '"children":{"contacts":[],"documents":[],"images":[],"media":[],"links":[]}}]')
    manifest["sources"]["nai-global"]["nai_post_identity_probe"] = {
        **binding, "held": [], "preimage": before, "preimage_sha256": hashlib.sha256(before.encode()).hexdigest()}
    report = {"summary": {"mode": "live", "torow_errors": 0, "baseline_updated": False,
              "baseline_update_sources": [], "hold_sources": ["nai-global"]}, "generated_for_artifact": str(path),
              "per_source": {"nai-global": {"current_active": 1, "verdict": "hold", "mark_missing_safe": False,
                                            "reason": "current_active 1 below floor 100"}}}
    proof = admit(path, manifest, report, code_sha=sha, dirty=False, phase="source")
    manifest["sources"]["nai-global"]["gate"] = {"current_inventory_proof": proof}
    aggregate = copy.deepcopy(report)
    assert admit(path, manifest, aggregate, code_sha=sha, dirty=False, phase="aggregate") == proof
    attempt = "d" * 32
    envelope = {"manifest": manifest, "source_report": report, "aggregate_report": aggregate,
                "binding": proof, "attempt_id": attempt}
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    claims = private / "claims.json"
    claims.write_text(json.dumps(envelope))
    claims.chmod(0o600)
    base = [sys.executable, str(collector / "cre_ingest.py"), "--in", str(path), "--nai-post-claims", str(claims),
            "--checkpoint-ingest-attempt", attempt, "--expected-db-target-sha256", "b" * 64, "--dry-run"]
    env = {**os.environ, "CRE_ACTIVATE_STATUS": "0"}
    accepted = subprocess.run(base + ["--keep-artifacts", str(private / "accepted")], cwd=collector,
                              env=env, capture_output=True, text=True, timeout=30)
    assert accepted.returncode != 0
    assert not (private / "accepted" / "ingest.sql").exists()
    refused = subprocess.run(base + ["--mark-missing", "--keep-artifacts", str(private / "refused")], cwd=collector,
                             env=env, capture_output=True, text=True, timeout=30)
    assert refused.returncode != 0 and not (private / "refused" / "ingest.sql").exists()
