"""Cursor publication follows exact readback and records uncertainty."""
import copy

import cre_checkpoint_refresh as refresh
import cre_partial_cursor as cursor
import cre_refresh_pipeline as pipeline
import pytest


@pytest.mark.parametrize("fail", [False, True])
def test_commit_cursor_intent_is_durable_before_publication(tmp_path, monkeypatch, fail):
    target, artifact, attempt = "a" * 64, "b" * 64, "c" * 32
    proof = {"ok": True, "outcome": "exact_commit", "artifact_sha256": artifact,
             "generation_id": "gen", "attempt_id": attempt, "database_target_sha256": target}
    binding = {"cursor": 1, "next_cursor": 3, "artifact_sha256": artifact,
               "generation_id": "gen", "database_target_sha256": target}
    checkpoint = {"gate": {"current_inventory_proof": binding},
                  "readback": {"ok": True, "observation": proof}}
    manifest = {"preflight": {"shared_lock": str(tmp_path / ".cre.lock")},
                "sources": {"colliers": checkpoint}}
    snapshots = []
    monkeypatch.setattr(refresh, "save_manifest", lambda _path, value: snapshots.append(copy.deepcopy(value)))
    monkeypatch.setattr(pipeline, "require_pipeline_owner", lambda *_: None)

    def publish(*args, **kwargs):
        assert snapshots[-1]["sources"]["colliers"]["partial_cursor_settlement"]["state"] == "publishing"
        assert kwargs["proof"] == proof
        if fail:
            raise OSError("synthetic failure after publication")

    monkeypatch.setattr(cursor, "advance_cursor", publish)
    if fail:
        with pytest.raises(OSError):
            pipeline.settle_colliers_partial_cursor(tmp_path, manifest)
        assert checkpoint["partial_cursor_settlement"]["state"] == "recovery_required"
    else:
        pipeline.settle_colliers_partial_cursor(tmp_path, manifest)
        assert checkpoint["partial_cursor_settlement"]["state"] == "settled"
    assert snapshots[-1]["sources"]["colliers"]["partial_cursor_settlement"] == checkpoint["partial_cursor_settlement"]
