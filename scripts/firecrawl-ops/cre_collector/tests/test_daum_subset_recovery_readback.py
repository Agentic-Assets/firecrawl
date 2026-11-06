"""Recovery rederives DAUM subset counts before using actual readback."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
import cre_checkpoint_refresh as refresh
import cre_daum_current_inventory as daum
from test_cre_checkpoint_refresh import freshness_generation_row
from test_daum_identity_partition import qualified_conflict
from test_daum_verified_subset import complete, subset  # noqa: F401


@pytest.mark.parametrize("fault", ["none", "claims_hash", "attempt", "gate", "generation", "wrong_count"])
def test_recovery_reproves_held_subset_and_real_readback(subset, monkeypatch, fault):
    root, artifact, _data, manifest, report = subset
    binding, _ = qualified_conflict(subset)
    assert binding["emitted_count"] == 99
    source_report = copy.deepcopy(report)
    checkpoint = manifest["sources"][daum.SOURCE]
    checkpoint["gate"] = copy.deepcopy(source_report["per_source"][daum.SOURCE])
    assert daum.admit_subset(artifact, manifest, report, current_code_sha=refresh.git_identity()[0],
                             current_git_dirty=False, phase="aggregate") == binding
    attempt = "e" * 32
    envelope = {"manifest": copy.deepcopy(manifest), "source_report": source_report,
                "aggregate_report": copy.deepcopy(report), "binding": binding, "attempt_id": attempt}
    claims = root / "claims.json"
    claims.write_text(json.dumps(envelope))
    checkpoint["subset_ingest_attempt_id"] = attempt
    checkpoint["daum_verified_subset_claims"] = {"path": str(claims), "sha256": refresh.sha256_file(claims), "attempt_id": attempt}
    checkpoint["ingest"] = {"attempt_id": attempt}
    info = checkpoint["artifact"]
    info.update(staged_unique=100, inventory_only=0, strict_freshness=True,
                finished_at=manifest["started_at"], freshness_generation_id=manifest["run_id"],
                freshness_generation_started_at=manifest["started_at"])
    if fault == "claims_hash": checkpoint["daum_verified_subset_claims"]["sha256"] = "a" * 64
    if fault == "attempt": checkpoint["ingest"]["attempt_id"] = "f" * 32
    if fault == "gate": checkpoint["gate"]["current_inventory_proof"]["emitted_count"] = 98
    if fault == "generation": manifest["run_id"] = "wrong-generation"
    active = 100 if fault == "wrong_count" else 99
    row = freshness_generation_row(source=daum.SOURCE, active=active, generation_id=manifest["run_id"],
        earliest_inventory_observed_at=manifest["started_at"], latest_inventory_observed_at=manifest["started_at"],
        earliest_detail_observed_at=manifest["started_at"], latest_detail_observed_at=manifest["started_at"])
    validation = {"queries": {"source_counts": [{"source_key": daum.SOURCE}],
                              "freshness_generations": [row], "inventory_only_index": [],
                              "artifact_run_jobs": [{"matching_jobs": "1"}]}}
    def command(argv, _log, **_kwargs):
        Path(argv[argv.index("--out") + 1]).write_text(json.dumps(validation))
        return 0
    monkeypatch.setattr(refresh, "run_command", command)
    monkeypatch.setattr(refresh, "save_manifest", lambda *_args: None)
    if fault == "none":
        refresh.recover_interrupted_ingest(root, manifest, daum.SOURCE, None)
        assert checkpoint["state"] == "ingested"
        assert checkpoint["readback"]["ok"] is True
    else:
        with pytest.raises(refresh.GlobalStageError):
            refresh.recover_interrupted_ingest(root, manifest, daum.SOURCE, None)
        assert checkpoint["state"] == "ingest_recovery_required"
        assert checkpoint["ingest_recovery"]["readback_ok"] is False
