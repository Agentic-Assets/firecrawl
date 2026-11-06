"""Synthetic source-bound checks for interrupted Colliers cursor recovery."""
import json
import os
from unittest.mock import MagicMock

import cre_checkpoint_refresh as refresh
import pytest
from recovery import colliers_cursor as recovery


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.fixture
def incident(tmp_path, monkeypatch):
    series_dir = tmp_path / "out" / "checkpoint-series" / "2026-11-06T030006Z"
    run_id = "2026-11-06T030006Z-308c9289b522"
    run = series_dir / "runs" / run_id
    lock = tmp_path / ".cre.lock"
    lock.mkdir()
    owner = MagicMock(spec=refresh.SharedLock)
    owner.path = lock
    owner._owned_directory_fd.side_effect = lambda: os.open(lock, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock)
    target, sha, artifact = "c" * 64, "a" * 40, "b" * 64
    attempt, old_attempt = "d" * 32, "e" * 32
    cursor = {"schema": "cre-partial-cursor/v1", "sourceKey": "colliers", "targetSha256": target,
        "position": 1565, "generation": run_id, "artifactSha256": "f" * 64,
        "attemptId": old_attempt, "outcome": "exact_commit"}
    monkeypatch.setattr(recovery, "read_cursor", lambda *args: (1565, cursor))
    receipt = {"local_settlement": "confirmed", "status": "failed", "last_checkpoint": {
        "run_id": run_id, "child_invocation_id": "1" * 32}}
    monkeypatch.setattr(recovery, "read_receipt", lambda *args: receipt)
    monkeypatch.setattr(recovery.series, "pipeline_source_qualified", lambda *args, **kwargs: True)
    monkeypatch.setattr(recovery.series, "_expected_child_config", lambda *args: {"pipeline": True})
    before = run / "pipeline" / "unit-0053" / "validation.json"
    _write(before, {"synthetic": "bound before"})
    import hashlib
    digest = hashlib.sha256(before.read_bytes()).hexdigest()
    parent = {"started_at": "2026-11-06T03:00:06Z", "finished_at": "2026-11-06T05:00:54Z",
        "collector_git_sha": sha, "database_target": {"algorithm": "sha256", "value": target},
        "config": {"sources": ["colliers"]},
        "latest_invocation_control": {"invocation_id": "2" * 32},
        "sources": {"colliers": {"checkpoint_run": f"runs/{run_id}",
            "attempts": [{"series_control_child_id": "1" * 32}]}}}
    binding = {"source_key": "colliers", "whole_source_coverage": False, "generation_id": run_id,
        "producer_sha": sha, "database_target_sha256": target, "artifact_sha256": artifact,
        "cursor": 1565, "next_cursor": 1670}
    completion = {"firstPosition": 1459, "rotationComplete": False, "sourceStopped": False,
                  "proposedCursor": 1565, "admitted": 100, "held": 0, "unattempted": 0}
    prior_worker = {"window_index": 1, "cursor": 1459, "receipt_path": "colliers-partial/old-receipt.json",
                    "receipt_sha256": "1" * 64, "artifact_path": "colliers-partial/old-artifact.json",
                    "completion": completion}
    prior_checkpoint = {"colliers_partial_worker": prior_worker,
        "gate": {"current_inventory_proof": {"artifact_sha256": "f" * 64, "cursor": 1459, "next_cursor": 1565}},
        "partial_cursor_settlement": {"attempt_id": old_attempt}}
    import cre_colliers_partial_checkpoint as partial
    worker_receipt = {**completion, "schema": "colliers-window-worker-receipt/v1",
        "generation": run_id, "attempt": 1, "producerSha": sha, "targetSha256": target,
        "artifact": {"sha256": "f" * 64, "bytes": 123}, "evidence": {"sha256": "2" * 64, "bytes": 456}}
    monkeypatch.setattr(partial, "verify_worker_artifact", lambda *args, **kwargs: {"receipt": worker_receipt})
    checkpoint = {"state": "ingested", "ingest": {"rc": 0, "finished_at": "done", "additive": True,
        "mark_missing": False, "status_activation": False, "attempt_id": attempt},
        "subset_ingest_attempt_id": attempt, "artifact": {"sha256": artifact},
        "gate": {"mark_missing_safe": False, "current_inventory_proof": binding},
        "colliers_partial_worker": {"state": "settled", "rc": 0, "cursor": 1565, "window_index": 2,
                                    "stop_before_position": 1459},
        "partial_windows": [{"index": 1, "first_position": 1459, "state": "verified", "cursor": 1459, "next_cursor": 1565,
            "receipt_path": prior_worker["receipt_path"], "receipt_sha256": prior_worker["receipt_sha256"],
            "artifact_sha256": "f" * 64, "attempt_id": old_attempt, "outcome": "exact_commit",
            "unit_id": "unit-0053", "checkpoint": prior_checkpoint, "result": {
                "validation_path": "pipeline/unit-0053/validation.json", "validation_sha256": digest}}]}
    child = {"run_id": run_id, "collector_git_sha": sha, "collector_git_dirty": False,
        "config": {"pipeline": True}, "preflight": {"database_target": parent["database_target"],
            "shared_lock": str(lock)}, "sources": {"colliers": checkpoint}, "pipeline": {
            "commit_outcome_unknown": False, "source_results": {}, "units": [
                {"unit_id": "unit-0053", "state": "verified", "sources": ["colliers"]},
                {"unit_id": "unit-0054", "state": "failed", "sources": ["colliers"],
                 "before_path": "pipeline/unit-0053/validation.json", "before_sha256": digest}]}}
    _write(series_dir / "manifest.json", parent)
    _write(run / "manifest.json", child)
    return series_dir, run, owner, receipt, cursor, child


def test_bound_local_evidence_qualifies_without_writing(incident):
    series_dir, _, owner, _, _, _ = incident
    result = recovery.qualify(series_dir, owner)
    assert (result["cursor"], result["next_cursor"], result["unit_id"]) == (1565, 1670, "unit-0054")


@pytest.mark.parametrize("tamper", ["unsettled", "wrong_cursor", "unknown_write", "mark_missing", "before"])
def test_unsafe_or_changed_evidence_is_refused(incident, tamper):
    series_dir, run, owner, receipt, cursor, child = incident
    if tamper == "unsettled":
        receipt["local_settlement"] = "unknown"
    elif tamper == "wrong_cursor":
        cursor["artifactSha256"] = "0" * 64
    elif tamper == "unknown_write":
        child["pipeline"]["commit_outcome_unknown"] = True
        _write(run / "manifest.json", child)
    elif tamper == "mark_missing":
        child["sources"]["colliers"]["ingest"]["mark_missing"] = True
        _write(run / "manifest.json", child)
    else:
        _write(run / "pipeline" / "unit-0053" / "validation.json", {"changed": True})
    with pytest.raises(ValueError):
        recovery.qualify(series_dir, owner)

@pytest.mark.parametrize("quality", [True, False])
def test_recovery_reuses_readonly_validation_and_never_replays(incident, monkeypatch, quality):
    from pathlib import Path

    import cre_partial_cursor

    series_dir, run, owner, _, _, child = incident
    before = (run / "manifest.json").read_bytes()
    target = child["preflight"]["database_target"]["value"]
    producer = child["collector_git_sha"]
    verifier = "9" * 40
    attempt = child["sources"]["colliers"]["ingest"]["attempt_id"]
    artifact = child["sources"]["colliers"]["artifact"]["sha256"]
    proof = {"ok": True, "outcome": "exact_commit", "artifact_sha256": artifact,
        "generation_id": child["run_id"], "attempt_id": attempt,
        "database_target_sha256": target}
    monkeypatch.setattr(refresh, "git_identity", lambda: (verifier, False))
    monkeypatch.setattr(recovery.series, "local_git_output", lambda args: producer)
    calls = []
    def validate(run_dir, current, env_file, **kwargs):
        assert kwargs["sources"] == ["colliers"] and kwargs["persist_manifest"] is False
        assert kwargs["historical_colliers_producer_sha"] == producer
        _write(Path(kwargs["evidence_dir"]) / "validation.json", {"synthetic": "validated"})
        current["sources"]["colliers"]["readback"] = {"observation": proof}
        return {"query_execution_ok": True, "quality_no_regression": quality, "readback_ok": True}
    monkeypatch.setattr(refresh, "run_final_validation", validate)
    def advance(*args, **kwargs):
        calls.append((args, kwargs))
        desired = {"schema": "cre-partial-cursor/v1", "sourceKey": "colliers", "targetSha256": target,
            "position": 1670, "generation": child["run_id"], "artifactSha256": artifact,
            "attemptId": attempt, "outcome": "exact_commit"}
        monkeypatch.setattr(recovery, "read_cursor", lambda *unused: (1670, desired))
        return desired
    monkeypatch.setattr(cre_partial_cursor, "advance_cursor", advance)
    publication = []
    options = {"before_publish": lambda: publication.append("armed"), "after_publish": lambda: publication.append("settled")}
    if quality:
        result = recovery.recover(series_dir, owner, expected_target=target, env_file=None, **options)
        assert result["verifier_sha"] == verifier and result["producer_sha"] == producer
        assert result["whole_source_coverage"] is False
        assert len(calls) == 1 and calls[0][0][3:5] == (1565, 1670)
        assert publication == ["armed", "settled"]
    else:
        with pytest.raises(ValueError, match="validation did not qualify"):
            recovery.recover(series_dir, owner, expected_target=target, env_file=None, **options)
        assert calls == [] and publication == []
    assert (run / "manifest.json").read_bytes() == before

@pytest.mark.parametrize("case", ["pending", "already_published", "unknown", "no_cursor"])
def test_normal_discovery_uses_only_current_cursor_generation(incident, monkeypatch, tmp_path, case):
    series_dir, run, owner, _, cursor, child = incident
    monkeypatch.setattr(refresh, "__file__", str(tmp_path / "collector.py"))
    target = child["preflight"]["database_target"]["value"]
    seen = []
    monkeypatch.setattr(recovery, "recover", lambda *args, **kwargs: seen.append((args, kwargs)) or {"done": True})
    if case == "already_published":
        cursor.update(position=1670, artifactSha256="b" * 64, attemptId="d" * 32)
        monkeypatch.setattr(recovery, "read_cursor", lambda *args: (1670, cursor))
    elif case == "unknown":
        child["pipeline"]["commit_outcome_unknown"] = True
        _write(run / "manifest.json", child)
    elif case == "no_cursor":
        monkeypatch.setattr(recovery, "read_cursor", lambda *args: (1, None))
    options = {"before_publish": lambda: None, "after_publish": lambda: None}
    if case == "unknown":
        with pytest.raises(refresh.GlobalStageError, match="unknown"):
            recovery.recover_prior_generation(owner, expected_target=target, env_file=None, **options)
    else:
        result = recovery.recover_prior_generation(owner, expected_target=target, env_file=None, **options)
        assert result == ({"done": True} if case == "pending" else None)
        assert len(seen) == (1 if case == "pending" else 0)
        if seen:
            assert seen[0][0][0] == series_dir

@pytest.mark.parametrize("damage", ["missing_parent", "missing_child", "bad_child", "cursor_mismatch"])
def test_discovery_holds_unusable_existing_cursor_history(incident, monkeypatch, tmp_path, damage):
    series_dir, run, owner, _, _, child = incident
    monkeypatch.setattr(refresh, "__file__", str(tmp_path / "collector.py"))
    target = child["preflight"]["database_target"]["value"]
    if damage == "missing_parent":
        (series_dir / "manifest.json").unlink()
    elif damage == "missing_child":
        (run / "manifest.json").unlink()
    elif damage == "bad_child":
        _write(run / "manifest.json", ["not a manifest"])
    else:
        child["sources"]["colliers"]["gate"]["current_inventory_proof"]["cursor"] = 999
        _write(run / "manifest.json", child)
    with pytest.raises(recovery.SourceEvidenceRefusal):
        recovery.recover_prior_generation(owner, expected_target=target, env_file=None,
            before_publish=lambda: None, after_publish=lambda: None)


@pytest.mark.parametrize("failure", [KeyError("missing manifest field"), FileNotFoundError("missing file")])
def test_qualification_turns_settled_missing_evidence_into_source_hold(incident, monkeypatch, failure):
    series_dir, _, owner, _, _, _ = incident
    def missing(*args):
        raise failure
    monkeypatch.setattr(recovery, "_qualify", missing)
    with pytest.raises(recovery.SourceEvidenceRefusal):
        recovery.qualify(series_dir, owner)


def test_cursor_publication_error_does_not_clear_armed_marker(incident, monkeypatch):
    from pathlib import Path

    import cre_partial_cursor

    series_dir, _run, owner, _, _, child = incident
    target = child["preflight"]["database_target"]["value"]
    producer, verifier = child["collector_git_sha"], "9" * 40
    proof = {"ok": True, "outcome": "exact_commit", "artifact_sha256": "b" * 64,
        "generation_id": child["run_id"], "attempt_id": "d" * 32, "database_target_sha256": target}
    monkeypatch.setattr(refresh, "git_identity", lambda: (verifier, False))
    monkeypatch.setattr(recovery.series, "local_git_output", lambda args: producer)
    def validate(run_dir, current, env_file, **kwargs):
        _write(Path(kwargs["evidence_dir"]) / "validation.json", {"synthetic": "validated"})
        current["sources"]["colliers"]["readback"] = {"observation": proof}
        return {"query_execution_ok": True, "quality_no_regression": True, "readback_ok": True}
    monkeypatch.setattr(refresh, "run_final_validation", validate)
    def failed(*args, **kwargs):
        raise OSError("synthetic post-replacement failure")
    monkeypatch.setattr(cre_partial_cursor, "advance_cursor", failed)
    marks = []
    with pytest.raises(OSError, match="post-replacement"):
        recovery.recover(series_dir, owner, expected_target=target, env_file=None,
            before_publish=lambda: marks.append("armed"), after_publish=lambda: marks.append("settled"))
    assert marks == ["armed"]

@pytest.mark.parametrize("verifier,dirty,allowed", [(None, False, False), ("9" * 40, False, True),
                                                     ("8" * 40, False, False), ("9" * 40, True, False)])
def test_historical_readback_keeps_original_producer_and_binds_clean_verifier(monkeypatch, verifier, dirty, allowed):
    import cre_colliers_partial_checkpoint as partial

    producer = "a" * 40
    monkeypatch.setattr(refresh, "git_identity", lambda: ("9" * 40, dirty))
    calls = []
    def inspect(*args, **kwargs):
        calls.append(kwargs)
        raise ValueError("stopped after context binding")
    monkeypatch.setattr(partial, "ingest_subset_context", inspect)
    with pytest.raises(ValueError, match="historical Colliers source evidence refused" if allowed else "code changed"):
        partial.run_partial_readback("artifact", {}, code_sha=producer, verifier_sha=verifier,
            target="c" * 64, attempt_id="d" * 32, env_file=None)
    assert bool(calls) is allowed
    if calls:
        assert calls[0]["current_code_sha"] == producer
