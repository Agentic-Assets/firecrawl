"""Synthetic source-bound checks for interrupted Colliers cursor recovery."""
import json
import os
from unittest.mock import MagicMock

import cre_checkpoint_refresh as refresh
import pytest
from recovery import colliers_cursor as recovery
import cre_colliers_partial_checkpoint as partial

_REAL_VERIFY_WORKER_ARTIFACT = partial.verify_worker_artifact


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
    prior_worker = {"window_index": 1, "cursor": 1459, "attempt": 1,
                    "receipt_path": "colliers-partial/old-receipt.json",
                    "receipt_sha256": "1" * 64, "artifact_path": "colliers-partial/old-artifact.json",
                    "evidence_path": "colliers-partial/old-evidence.jsonl", "completion": completion}
    evidence_path = run / prior_worker["evidence_path"]
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.parent.chmod(0o700)
    evidence_raw = (json.dumps({"schema": "colliers-window-evidence/v1", "sourceKey": "colliers",
                                "generation": run_id, "attempt": 1}) + "\n"
                    + json.dumps({"kind": "detail", "source": {"start": 1401, "offset": 58}}) + "\n"
                    + json.dumps({"kind": "detail", "source": {"start": 1501, "offset": 63}}) + "\n").encode()
    evidence_path.write_bytes(evidence_raw)
    evidence_path.chmod(0o600)
    prior_checkpoint = {"colliers_partial_worker": prior_worker,
        "gate": {"current_inventory_proof": {"artifact_sha256": "f" * 64, "cursor": 1459, "next_cursor": 1565}},
        "partial_cursor_settlement": {"attempt_id": old_attempt}}
    import cre_colliers_partial_checkpoint as partial
    worker_receipt = {**completion, "schema": "colliers-window-worker-receipt/v1",
        "generation": run_id, "attempt": 1, "producerSha": sha, "targetSha256": target,
        "artifact": {"sha256": "f" * 64, "bytes": 123},
        "evidence": {"sha256": hashlib.sha256(evidence_raw).hexdigest(), "bytes": len(evidence_raw)}}
    monkeypatch.setattr(partial, "verify_worker_artifact", lambda *args, **kwargs: {
        "receipt": worker_receipt, "data": {"total": 2000, "cursor": 1459, "proposedCursor": 1565}})
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



@pytest.mark.parametrize("case", ["gap", "wrap", "shrink_wrap", "no_progress", "evidence_mismatch", "missing_tail", "wrong_tail", "outside_interval", "artifact_cursor"])
def test_prior_window_binds_rotated_first_evidence(incident, monkeypatch, case):
    import hashlib
    import cre_colliers_partial_checkpoint as partial

    series_dir, run, owner, _, _, child = incident
    checked = partial.verify_worker_artifact()
    monkeypatch.setattr(partial, "verify_worker_artifact", lambda *args, **kwargs: checked)
    previous = child["sources"]["colliers"]["partial_windows"][0]
    worker = previous["checkpoint"]["colliers_partial_worker"]
    start, first, end, total = (1459, 1460, 1565, 2000)
    if case == "wrap":
        start, first, end = 1999, 1, 3
    elif case == "shrink_wrap":
        start, first, end = 3000, 1999, 1
    elif case == "no_progress":
        first, end = start, start
    elif case == "outside_interval":
        first = 1600
    previous.update(cursor=start, first_position=first, next_cursor=end)
    worker["cursor"] = start
    worker["completion"].update(firstPosition=first, proposedCursor=end)
    prior = previous["checkpoint"]["gate"]["current_inventory_proof"]
    prior.update(cursor=start, next_cursor=end)
    child["sources"]["colliers"]["colliers_partial_worker"]["cursor"] = end
    child["sources"]["colliers"]["colliers_partial_worker"]["stop_before_position"] = first
    child["sources"]["colliers"]["gate"]["current_inventory_proof"]["cursor"] = end
    checked["receipt"].update(firstPosition=first, proposedCursor=end)
    checked["data"].update(cursor=(start + 1 if case == "artifact_cursor" else start),
                           proposedCursor=end, total=total)
    evidence_path = run / worker["evidence_path"]
    lines = evidence_path.read_text().splitlines()
    record = json.loads(lines[1])
    record["source"] = {"start": first + (1 if case == "evidence_mismatch" else 0), "offset": 0}
    tail = {"kind": "detail", "source": {"start": (end - 2) % total + 1, "offset": 0}}
    if case == "wrong_tail":
        tail["source"]["start"] = first
    records = [lines[0], json.dumps(record)]
    if case != "missing_tail":
        records.append(json.dumps(tail))
    raw = ("\n".join(records) + "\n").encode()
    evidence_path.write_bytes(raw)
    checked["receipt"]["evidence"] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    _write(run / "manifest.json", child)
    if case in {"gap", "wrap", "shrink_wrap"}:
        # Cursor proof and pending binding must follow the modified committed window.
        cursor = {"schema": "cre-partial-cursor/v1", "sourceKey": "colliers",
            "targetSha256": child["preflight"]["database_target"]["value"], "position": end,
            "generation": child["run_id"], "artifactSha256": previous["artifact_sha256"],
            "attemptId": previous["attempt_id"], "outcome": "exact_commit"}
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(recovery, "read_cursor", lambda *args: (end, cursor))
            assert recovery.qualify(series_dir, owner)["cursor"] == end
    else:
        with pytest.raises(recovery.SourceEvidenceRefusal):
            recovery.qualify(series_dir, owner)



def test_real_worker_artifact_verifier_with_source_gap(incident, monkeypatch, tmp_path):
    """Exercise real synthetic Node selection and Python worker verification."""
    import hashlib
    import subprocess
    from pathlib import Path

    series_dir, run, owner, _, cursor, child = incident
    collector = Path(__file__).resolve().parents[1]
    fixture_dir = tmp_path / "node-fixture"
    fixture_dir.mkdir(mode=0o700)
    env = {**os.environ, "COLLIERS_FIXTURE_DIRECTORY": str(fixture_dir),
           "COLLIERS_FIXTURE_GENERATION": child["run_id"], "COLLIERS_FIXTURE_GAP": "1",
           "COLLIERS_FIXTURE_TARGET": "c"}
    subprocess.run(["node", "--import", "tsx", "tests/ts/fixtures/colliers-window-staging.ts"],
                   cwd=collector, env=env, capture_output=True, text=True, timeout=30, check=True)
    artifact_raw = (fixture_dir / "artifact").read_bytes()
    artifact_data = json.loads(artifact_raw)
    evidence_raw = (fixture_dir / "evidence").read_bytes()
    assert (artifact_data["cursor"], artifact_data["proposedCursor"], artifact_data["total"]) == (1, 3, 3)
    private = run / "colliers-partial"
    artifact_path = private / "window-1-artifact.json"
    evidence_path = private / "window-1-evidence.jsonl"
    artifact_path.write_bytes(artifact_raw)
    artifact_path.chmod(0o600)
    evidence_path.write_bytes(evidence_raw)
    evidence_path.chmod(0o600)
    artifact_sha = hashlib.sha256(artifact_raw).hexdigest()
    worker_receipt = {"schema": "colliers-window-worker-receipt/v1", "generation": child["run_id"],
        "attempt": 1, "producerSha": "a" * 40, "targetSha256": "c" * 64,
        "artifact": {"bytes": len(artifact_raw), "sha256": artifact_sha},
        "evidence": artifact_data["evidence"], "detailFinishedAt": artifact_data["detailFinishedAt"],
        "proposedCursor": 3, "admitted": 1, "held": artifact_data["held"], "heldOther": artifact_data["heldOther"],
        "unattempted": artifact_data["unattempted"], "sourceStopped": False, "rotationComplete": False,
        "firstPosition": 2}
    worker_path = private / "window-1-receipt.json"
    _write(worker_path, worker_receipt)
    receipt_sha = hashlib.sha256(worker_path.read_bytes()).hexdigest()
    source = artifact_data["source"]
    source_receipt = {"generation": child["run_id"], "generation_started_at": "2026-11-04T14:00:00Z",
        "attempt": 1, "started_at": "2026-11-04T15:00:00Z", "finished_at": "2026-11-04T16:00:00Z",
        "bytes": source["bytes"], "sha256": source["sha256"], "main_sha256": "c" * 64,
        "producer_sha": "a" * 40, "database_target_sha256": "c" * 64}
    monkeypatch.setattr(partial, "revalidate_source_receipt", lambda *_args: source_receipt)
    monkeypatch.setattr(partial, "verify_worker_artifact", _REAL_VERIFY_WORKER_ARTIFACT)
    prior = child["sources"]["colliers"]["partial_windows"][0]
    worker = prior["checkpoint"]["colliers_partial_worker"]
    worker.update(state="settled", rc=0, attempt=1, cursor=1,
        artifact_path="colliers-partial/window-1-artifact.json",
        evidence_path="colliers-partial/window-1-evidence.jsonl",
        receipt_path="colliers-partial/window-1-receipt.json", receipt_sha256=receipt_sha,
        completion={key: worker_receipt[key] for key in
            ("firstPosition", "rotationComplete", "sourceStopped", "proposedCursor", "admitted", "held", "unattempted")})
    prior.update(cursor=1, first_position=2, next_cursor=3, artifact_sha256=artifact_sha,
                 receipt_path=worker["receipt_path"], receipt_sha256=receipt_sha)
    prior["checkpoint"]["artifact"] = {"path": str(artifact_path.relative_to(run)),
                                        "bytes": len(artifact_raw), "sha256": artifact_sha}
    prior["checkpoint"]["gate"]["current_inventory_proof"].update(
        cursor=1, next_cursor=3, artifact_sha256=artifact_sha)
    child["sources"]["colliers"]["colliers_partial_worker"].update(cursor=3, stop_before_position=2)
    child["sources"]["colliers"]["gate"]["current_inventory_proof"]["cursor"] = 3
    cursor.update(position=3, artifactSha256=artifact_sha)
    _write(run / "manifest.json", child)
    monkeypatch.setattr(recovery, "read_cursor", lambda *_args: (3, cursor))
    assert recovery.qualify(series_dir, owner)["cursor"] == 3
    evidence_path.write_bytes(evidence_raw + b"tampered")
    with pytest.raises(recovery.SourceEvidenceRefusal):
        recovery.qualify(series_dir, owner)


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


def test_discovery_finds_parent_with_different_timestamp(incident, monkeypatch, tmp_path):
    series_dir, _, owner, _, _, child = incident
    monkeypatch.setattr(refresh, "__file__", str(tmp_path / "collector.py"))
    moved = series_dir.rename(series_dir.with_name("2026-11-06T030005Z"))
    seen = []
    monkeypatch.setattr(recovery, "recover", lambda *args, **kwargs: seen.append(args[0]) or {"done": True})
    result = recovery.recover_prior_generation(owner, expected_target=child["preflight"]["database_target"]["value"],
        env_file=None, before_publish=lambda: None, after_publish=lambda: None)
    assert result == {"done": True}
    assert seen == [moved]


@pytest.mark.parametrize("damage", ["duplicate", "unbound", "wrong_child"])
def test_discovery_holds_ambiguous_or_unbound_child(incident, monkeypatch, tmp_path, damage):
    import shutil

    series_dir, run, owner, _, _, child = incident
    monkeypatch.setattr(refresh, "__file__", str(tmp_path / "collector.py"))
    if damage == "duplicate":
        shutil.copytree(series_dir, series_dir.with_name("2026-11-06T030005Z"))
    elif damage == "unbound":
        parent = json.loads((series_dir / "manifest.json").read_text())
        parent["sources"]["colliers"]["checkpoint_run"] = "runs/other"
        _write(series_dir / "manifest.json", parent)
    else:
        child["run_id"] = "other"
        _write(run / "manifest.json", child)
    monkeypatch.setattr(recovery, "recover", lambda *args, **kwargs: pytest.fail("must not recover"))
    with pytest.raises(recovery.SourceEvidenceRefusal):
        recovery.recover_prior_generation(owner, expected_target=child["preflight"]["database_target"]["value"],
            env_file=None, before_publish=lambda: None, after_publish=lambda: None)



def test_ambiguous_history_does_not_hide_unknown_write(incident, monkeypatch, tmp_path):
    import shutil

    series_dir, run, owner, _, _, child = incident
    monkeypatch.setattr(refresh, "__file__", str(tmp_path / "collector.py"))
    shutil.copytree(series_dir, series_dir.with_name("2026-11-06T030005Z"))
    child["pipeline"]["commit_outcome_unknown"] = True
    _write(run / "manifest.json", child)
    with pytest.raises(refresh.GlobalStageError, match="unknown"):
        recovery.recover_prior_generation(owner, expected_target=child["preflight"]["database_target"]["value"],
            env_file=None, before_publish=lambda: None, after_publish=lambda: None)




def test_unrelated_series_symlink_does_not_hold_bound_parent(incident, monkeypatch, tmp_path):
    series_dir, _, owner, _, _, child = incident
    monkeypatch.setattr(refresh, "__file__", str(tmp_path / "collector.py"))
    series_dir.with_name("2026-11-06T030005Z").symlink_to(
        tmp_path / "unrelated", target_is_directory=True)
    seen = []
    monkeypatch.setattr(recovery, "recover", lambda *args, **kwargs: seen.append(args[0]) or {"done": True})
    result = recovery.recover_prior_generation(owner, expected_target=child["preflight"]["database_target"]["value"],
        env_file=None, before_publish=lambda: None, after_publish=lambda: None)
    assert result == {"done": True}
    assert seen == [series_dir]


def test_unrelated_link_without_real_parent_reports_history_unavailable(incident, monkeypatch, tmp_path):
    series_dir, _, owner, _, cursor, child = incident
    monkeypatch.setattr(refresh, "__file__", str(tmp_path / "collector.py"))
    series_dir.rename(series_dir.with_name("hidden-fixture"))
    series_dir.with_name("2026-11-06T030005Z").symlink_to(
        tmp_path / "unrelated", target_is_directory=True)
    monkeypatch.setattr(recovery, "recover", lambda *args, **kwargs: pytest.fail("must not recover"))
    result = recovery.recover_prior_generation(owner, expected_target=child["preflight"]["database_target"]["value"],
        env_file=None, before_publish=lambda: None, after_publish=lambda: None)
    assert result == {"history_unavailable": True, "generation": cursor["generation"],
                      "position": cursor["position"]}


def test_missing_entire_legacy_series_reports_unavailable_without_recovery(incident, monkeypatch, tmp_path):
    series_dir, _, owner, _, cursor, child = incident
    monkeypatch.setattr(refresh, "__file__", str(tmp_path / "collector.py"))
    target = child["preflight"]["database_target"]["value"]
    series_dir.rename(series_dir.with_name("retained-fixture"))
    monkeypatch.setattr(recovery, "recover", lambda *args, **kwargs: pytest.fail("must not recover"))
    result = recovery.recover_prior_generation(owner, expected_target=target, env_file=None,
        before_publish=lambda: pytest.fail("must not publish"), after_publish=lambda: pytest.fail("must not publish"))
    assert result == {"history_unavailable": True, "generation": cursor["generation"],
                      "position": cursor["position"]}


def test_missing_legacy_series_never_bypasses_lost_owner(incident, monkeypatch, tmp_path):
    series_dir, _, owner, _, _, child = incident
    monkeypatch.setattr(refresh, "__file__", str(tmp_path / "collector.py"))
    series_dir.rename(series_dir.with_name("retained-fixture"))
    owner._require_authority.side_effect = refresh.LockHeldError("owner lost")
    with pytest.raises(refresh.LockHeldError):
        recovery.recover_prior_generation(owner, expected_target=child["preflight"]["database_target"]["value"],
            env_file=None, before_publish=lambda: None, after_publish=lambda: None)


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


@pytest.mark.parametrize("case", ["nai_only", "colliers", "duplicate", "child_collapse", "missing_metric",
                                  "missing_both_child", "missing_after_small_child", "unknown_child",
                                  "duplicate_quality", "duplicate_url_rows", "boolean_count",
                                  "negative_count", "nonnumeric_count"])
def test_historical_quality_preserves_global_diagnostic_and_scopes_admission(tmp_path, monkeypatch, case):
    from pathlib import Path
    import copy
    import cre_colliers_image_transition
    from tests.test_cre_preflight_validation_profile import declared_full
    from tests.test_cre_checkpoint_refresh import absolute_quality_report

    run = tmp_path / "run"
    (run / "logs").mkdir(parents=True)
    before = declared_full()
    after = copy.deepcopy(before)
    colliers = absolute_quality_report(source="colliers")["queries"]["quality_by_source"][0]
    changed = copy.deepcopy(colliers)
    if case == "colliers":
        changed["bad_source_url"] = "1"
    if case == "missing_metric":
        changed.pop("bad_source_url")
    before["queries"]["quality_by_source"] = [
        {"source_key": "nai-global", "missing_canonical_url": "0"}, colliers]
    after["queries"]["quality_by_source"] = [
        {"source_key": "nai-global", "missing_canonical_url": "92"},
        {"source_key": "colliers-main", "missing_canonical_url": "100"}, changed]
    for kind in ("contacts", "documents", "images", "media", "links"):
        if case == "missing_both_child" and kind == "links":
            continue
        count = "4" if case == "missing_after_small_child" and kind == "links" else "100"
        before["queries"]["child_counts"].append({"source_key": "colliers", "child_type": kind, "count": count})
        if case == "missing_after_small_child" and kind == "links":
            continue
        after["queries"]["child_counts"].append({"source_key": "colliers", "child_type": kind,
            "count": "50" if case == "child_collapse" and kind == "images" else count})
    if case == "unknown_child":
        after["queries"]["child_counts"][-1]["child_type"] = "unknown"
    if case in {"duplicate", "duplicate_url_rows"}:
        after["queries"]["duplicates"].append({"check_name": "duplicate_source_url_groups",
            "source_key": "colliers", "groups": "1", "rows": "2"})
    if case == "duplicate_url_rows":
        after["queries"]["duplicates"].append({"check_name": "duplicate_source_url_groups",
            "source_key": "colliers", "groups": "0", "rows": "0"})
    if case == "duplicate_quality":
        after["queries"]["quality_by_source"].append(copy.deepcopy(changed))
    if case in {"boolean_count", "negative_count", "nonnumeric_count"}:
        changed["bad_source_url"] = {"boolean_count": True, "negative_count": "-1",
                                     "nonnumeric_count": "unknown"}[case]
    assert refresh.compare_validation_quality(before, after)["ok"] is False
    colliers_regression = case != "nai_only"
    baseline = run / "before.json"
    _write(baseline, before)
    evidence = run / "private-evidence"
    manifest = refresh.new_manifest(run, git_sha="a" * 40, git_dirty=False,
        sources=("colliers",), page_cap=0, concurrency=1)
    manifest["preflight"]["database_target"] = {"algorithm": "sha256", "value": "c" * 64}
    def write_report(argv, _log, env):
        _write(Path(argv[argv.index("--out") + 1]), after)
        return 0
    monkeypatch.setattr(refresh, "run_command", write_report)
    monkeypatch.setattr(refresh, "colliers_partial_candidate", lambda *args: True)
    monkeypatch.setattr(refresh, "colliers_partial_readback", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(refresh, "verify_validation_readback",
        lambda *args, **kwargs: {"ok": True, "failed_sources": []})
    monkeypatch.setattr(cre_colliers_image_transition, "verified_checkpoint", lambda *args: None)
    options = {"sources": ["colliers"], "evidence_dir": evidence, "before_path": baseline,
               "historical_colliers_producer_sha": "a" * 40, "persist_manifest": False}
    if colliers_regression:
        with pytest.raises(refresh.GlobalStageError, match="final validation"):
            refresh.run_final_validation(run, manifest, None, **options)
    else:
        result = refresh.run_final_validation(run, manifest, None, **options)
        assert result["quality_no_regression"] is True
    saved = json.loads((evidence / "validation.json").read_text())
    global_quality = saved["colliers_recovery_global_quality_diagnostic"]
    assert global_quality["ok"] is False
    assert any("nai-global" in item and "0->92" in item for item in global_quality["failures"])
    source_quality = saved["colliers_recovery_source_quality"]
    assert source_quality["ok"] is not colliers_regression
    if case in {"colliers", "duplicate", "child_collapse"}:
        assert any("colliers" in item for item in source_quality["failures"])
    elif case in {"missing_metric", "missing_both_child", "missing_after_small_child", "unknown_child",
                  "duplicate_quality", "duplicate_url_rows", "boolean_count", "negative_count", "nonnumeric_count"}:
        assert source_quality["failures"] == ["Colliers recovery quality rows are incomplete or malformed"]
    assert manifest["validation"]["readback_ok"] is True
    if case == "nai_only":
        # An ordinary single-source run still uses the original global gate.
        with pytest.raises(refresh.GlobalStageError, match="final validation"):
            refresh.run_final_validation(run, copy.deepcopy(manifest), None, sources=["colliers"],
                evidence_dir=run / "ordinary", before_path=baseline)




@pytest.mark.parametrize("mismatch", ["postimage", "held_partition", "target"])
def test_actual_historical_readback_mismatch_is_source_local_except_target(incident, monkeypatch, mismatch):
    from types import SimpleNamespace
    import cre_colliers_partial_checkpoint as partial
    import cre_ingest as ingest
    import cre_validate as validate

    series_dir, _, owner, _, _, child = incident
    target = child["preflight"]["database_target"]["value"]
    producer, verifier = child["collector_git_sha"], "9" * 40
    monkeypatch.setattr(refresh, "git_identity", lambda: (verifier, False))
    monkeypatch.setattr(recovery.series, "local_git_output", lambda args: producer)
    monkeypatch.setattr(refresh, "database_target_fingerprint",
        lambda _: {"algorithm": "sha256", "value": target})
    context = {"source_receipt": {"generation": child["run_id"], "attempt": 1,
        "started_at": "start", "finished_at": "finish"}, "records": [], "staged": [], "held": [],
        "preimage": [], "artifact_finished_at": "finish", "job_id": "job", "history_holds": {},
        "binding": {"emitted_count": 100}}
    monkeypatch.setattr(partial, "ingest_subset_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(partial, "partial_readback_sql",
        lambda *args: ("SELECT 1", {"conflict": "held"} if mismatch == "held_partition" else {}))
    monkeypatch.setattr(ingest, "load_db_url", lambda _: ("synthetic", None))
    monkeypatch.setattr(ingest, "database_target_fingerprint_from_url",
        lambda _: {"value": "f" * 64 if mismatch == "target" else target})
    monkeypatch.setattr(ingest, "find_psql", lambda: "synthetic-psql")
    monkeypatch.setattr(validate, "_run_psql_script",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="jobs\tobserved\tmismatches\n1\t100\t1\n"))
    def actual_readback(*args, **kwargs):
        return partial.run_partial_readback("artifact", {}, code_sha=producer, verifier_sha=verifier,
            target=target, attempt_id="d" * 32, env_file=None)
    monkeypatch.setattr(refresh, "run_final_validation", actual_readback)
    publications = []
    options = {"before_publish": lambda: publications.append("armed"), "after_publish": lambda: publications.append("done")}
    error = ValueError if mismatch == "target" else recovery.SourceEvidenceRefusal
    with pytest.raises(error):
        recovery.recover(series_dir, owner, expected_target=target, env_file=None, **options)
    assert publications == []


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
