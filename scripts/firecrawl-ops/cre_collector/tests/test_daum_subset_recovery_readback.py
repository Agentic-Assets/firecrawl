"""Recovery rederives DAUM subset counts before using actual readback."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
import cre_checkpoint_refresh as refresh
import cre_daum_current_inventory as daum
from cre_validate import FULL_VALIDATION_PROFILE, QUERIES, VALIDATION_AUDIT_SCOPES
from test_cre_checkpoint_refresh import freshness_generation_row
from test_daum_identity_partition import qualified_conflict
from test_daum_verified_subset import complete, subset  # noqa: F401


@pytest.mark.parametrize(("recovery_kind", "fault"), [
    (kind, fault)
    for kind in ("interrupted", "settled")
    for fault in ("none", "claims_hash", "attempt", "gate", "generation", "wrong_count")
] + [("settled", "fence")])
def test_recovery_reproves_held_subset_and_real_readback(subset, monkeypatch, recovery_kind, fault):
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
    if recovery_kind == "settled":
        checkpoint["ingest_recovery"] = {"reason": "settled_ingest_failure", "subprocess_rc": 1}
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
    if recovery_kind == "settled":
        run_key, _ = refresh.artifact_run_identity_from_digests([info["sha256"]])
        probe = {"database_target_sha256": refresh.manifest_database_target_sha256(manifest),
                 "artifact_run_key_sha256": hashlib.sha256(run_key.encode()).hexdigest(),
                 "attempt_id": checkpoint["ingest"]["attempt_id"]}
        validation.update(
            ok=True, validation_profile=FULL_VALIDATION_PROFILE,
            audit_scope=VALIDATION_AUDIT_SCOPES[FULL_VALIDATION_PROFILE],
            historical_primary_child_conflicts=[], artifact_run_probe=probe,
            atomic_settlement_probe={**probe, "source": daum.SOURCE, "generation_id": manifest["run_id"]},
        )
        validation["queries"] = {
            **{name: [] for name in QUERIES}, **validation["queries"],
            "atomic_settlement": [{"matching_jobs": "1", "matching_generations": "0" if fault == "fence" else "1"}],
            "inventory_generation_fingerprints": [
                {"source_key": key, "row_count": "99" if key == daum.SOURCE else "0",
                 "active_row_count": "99", "classified_row_count": "99",
                 "unclassified_row_count": "0",
                 "max_row_updated_at": manifest["started_at"],
                 "max_observation_at": manifest["started_at"]}
                for key in refresh.SOURCE_KEYS
            ],
        }
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
