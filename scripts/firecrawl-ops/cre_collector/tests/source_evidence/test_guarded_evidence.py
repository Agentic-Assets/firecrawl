"""Offline contracts for targeted evidence and its single recovery fence."""
import json
from pathlib import Path

import pytest

import cre_checkpoint_refresh as checkpoint
from source_evidence import automatic, enrichment_custody, guarded_evidence, guarded_fence
from test_automatic import SHA, artifact, setup_policy


@pytest.fixture(autouse=True)
def clean_identity(monkeypatch):
    monkeypatch.setattr(checkpoint, "git_identity", lambda: (SHA, False))


class Lock:
    def __init__(self, root):
        self.path = root / "lock"
    def _require_authority(self):
        pass


def setup(tmp_path):
    config, _, _ = setup_policy(tmp_path)
    run = tmp_path / "run-1"
    run.mkdir(mode=0o700)
    marker = enrichment_custody.mark(run, "faris-lee")
    raw = json.dumps(artifact(count=1)).encode()
    (run / "enriched.json").write_bytes(raw)
    (run / "enriched.json").chmod(0o600)
    (run / "claims.json").write_text('{"claims": []}')
    (run / "claims.json").chmod(0o600)
    receipt = {"tier": "enrich", "source": "faris-lee", "apply": True, "expected_sha": SHA,
               "local_settlement": {"state": "confirmed"},
               "enrichment_artifact": enrichment_custody.settle(run, marker),
               "source_evidence": {"config": config, "status": "awaiting_worker"}}
    lock = Lock(tmp_path)
    def save():
        checkpoint.atomic_write_text(run / "receipt.json", json.dumps(receipt) + "\n")
    save()
    return run, receipt, lock, save


def test_settled_artifact_seals_and_appends(tmp_path, monkeypatch):
    run, receipt, lock, save = setup(tmp_path)
    seen = []
    def write(*args, **kwargs):
        assert receipt["source_evidence"]["job"]["state"] == "appending"
        assert guarded_fence.read(lock.path) == guarded_evidence.binding(run, receipt)
        seen.append(args)
    monkeypatch.setattr(automatic.cli, "run_psql", write)
    guarded_evidence.seal_and_append(run, receipt, lock, save)
    assert len(seen) == 1
    assert receipt["source_evidence"]["job"]["state"] == "appended"
    assert guarded_fence.read(lock.path) is None


@pytest.mark.parametrize("state", ["unknown", "not_started", None])
def test_unknown_worker_never_seals_or_writes(tmp_path, monkeypatch, state):
    run, receipt, lock, save = setup(tmp_path)
    receipt["local_settlement"]["state"] = state
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("DB write"))
    with pytest.raises(ValueError):
        guarded_evidence.seal_and_append(run, receipt, lock, save)
    assert guarded_fence.read(lock.path) is None
    assert not (run / "source-evidence").exists()


@pytest.mark.parametrize("field,value", [("bytes", 0), ("sha256", "a" * 64), ("state", "absent")])
def test_changed_artifact_refuses_before_write(tmp_path, monkeypatch, field, value):
    run, receipt, lock, save = setup(tmp_path)
    receipt["enrichment_artifact"]["artifact"][field] = value
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("DB write"))
    with pytest.raises(ValueError):
        guarded_evidence.seal_and_append(run, receipt, lock, save)
    assert guarded_fence.read(lock.path) is None


def test_database_unknown_fences_later_writes(tmp_path, monkeypatch):
    run, receipt, lock, save = setup(tmp_path)
    def fail(*args, **kwargs):
        raise RuntimeError("remote unknown")
    monkeypatch.setattr(automatic.cli, "run_psql", fail)
    guarded_evidence.seal_and_append(run, receipt, lock, save)
    assert receipt["source_evidence"]["job"]["state"] == "outcome_unknown"
    assert guarded_fence.read(lock.path) == guarded_evidence.binding(run, receipt)
    with pytest.raises(ValueError):
        guarded_fence.require_clear(lock.path)
    assert receipt["local_settlement"]["state"] == "confirmed"


def test_exact_replay_revalidates_and_clears(tmp_path, monkeypatch):
    run, receipt, lock, save = setup(tmp_path)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    guarded_evidence.seal_and_append(run, receipt, lock, save)
    state = receipt["source_evidence"]["job"]
    state["state"] = "pending"
    save()
    writes = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: writes.append(a))
    guarded_evidence.append(run, receipt, lock, save, guarded_evidence.binding(run, receipt))
    assert len(writes) == 1
    assert state["state"] == "appended"
    assert guarded_fence.read(lock.path) is None


@pytest.mark.parametrize("tamper", ["plan", "input", "claims", "marker", "policy"])
def test_tamper_never_writes(tmp_path, monkeypatch, tamper):
    run, receipt, lock, save = setup(tmp_path)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    guarded_evidence.seal_and_append(run, receipt, lock, save)
    state = receipt["source_evidence"]["job"]
    marker = guarded_evidence.binding(run, receipt)
    if tamper in {"plan", "input"}:
        path = run / state["path" if tamper == "plan" else "input_path"]
        path.write_bytes(path.read_bytes() + b" ")
    elif tamper == "claims":
        (run / "claims.json").write_text('{"claims": ["changed"]}')
    elif tamper == "marker":
        marker["source"] = "svn"
    else:
        Path(receipt["source_evidence"]["config"]["policy_path"]).write_text("{}")
    state["state"] = "pending"
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("tampered DB write"))
    if tamper == "marker":
        with pytest.raises(ValueError):
            guarded_evidence.append(run, receipt, lock, save, marker)
    else:
        guarded_evidence.append(run, receipt, lock, save, marker)
        assert state["state"] == "refused"
    assert guarded_fence.read(lock.path) is not None


def test_explicit_receipt_replay_owns_lock_and_skips_enrichment(tmp_path, monkeypatch):
    run, receipt, lock, save = setup(tmp_path)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    guarded_evidence.seal_and_append(run, receipt, lock, save)
    monkeypatch.setattr(checkpoint, "git_identity", lambda: (SHA, False))
    calls = []
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: calls.append(a))
    guarded_evidence.reconcile(run / "receipt.json", lock.path)
    assert len(calls) == 1
    assert guarded_fence.read(lock.path) is None
    assert json.loads((run / "receipt.json").read_text())["source_evidence"]["job"]["state"] == "appended"
    with checkpoint.SharedLock(lock.path):
        pass


def test_replay_unknown_releases_local_owner_but_keeps_marker(tmp_path, monkeypatch):
    run, receipt, lock, save = setup(tmp_path)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    guarded_evidence.seal_and_append(run, receipt, lock, save)
    monkeypatch.setattr(checkpoint, "git_identity", lambda: (SHA, False))
    with pytest.raises(ValueError):
        guarded_evidence.reconcile(run / "receipt.json", lock.path)
    assert guarded_fence.read(lock.path) is not None
    with checkpoint.SharedLock(lock.path):
        pass


def test_unknown_psql_local_cleanup_keeps_owner_fenced(tmp_path, monkeypatch):
    from source_evidence.document_process import DocumentProcessCleanupUnknown
    run, receipt, lock, save = setup(tmp_path)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    guarded_evidence.seal_and_append(run, receipt, lock, save)
    monkeypatch.setattr(checkpoint, "git_identity", lambda: (SHA, False))
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: (_ for _ in ()).throw(DocumentProcessCleanupUnknown("unknown")))
    with pytest.raises(DocumentProcessCleanupUnknown):
        guarded_evidence.reconcile(run / "receipt.json", lock.path)
    with pytest.raises(checkpoint.LockHeldError):
        checkpoint.SharedLock(lock.path).acquire()


@pytest.mark.parametrize("remote_unknown", [False, True])
def test_guarded_tier_keeps_canonical_outcome_separate(tmp_path, monkeypatch, remote_unknown):
    import cre_guarded_tier as tier
    import argparse
    config, _, _ = setup_policy(tmp_path)
    args = argparse.Namespace(tier="enrich", source="faris-lee", batch=10, deadline_seconds=30,
                              expected_sha=SHA, apply=True, env_file="canonical.env",
                              source_evidence_policy=config["policy_path"],
                              source_evidence_policy_sha256=config["policy_sha256"],
                              source_evidence_env_file=config["env_file"],
                              source_evidence_target_sha256=config["target_sha256"])
    monkeypatch.setattr(checkpoint, "database_target_fingerprint", lambda _: {"value": "b" * 64})
    def worker(argv, log, env):
        path = Path(argv[argv.index("--artifact-out") + 1])
        path.write_text(json.dumps(artifact(count=1)))
        path.chmod(0o600)
        return 0
    def db(*args, **kwargs):
        if remote_unknown:
            raise RuntimeError("unknown")
    monkeypatch.setattr(automatic.cli, "run_psql", db)
    result = tier.run_guarded_tier(args, _lock_path=tmp_path / "tier.lock", _out_root=tmp_path / "runs",
                                   _cpu_sampler=lambda: 10.0, _memory_sampler=lambda: 1,
                                   _verify_checkout=lambda *_: None, _runner=worker)
    receipt = json.loads(next((tmp_path / "runs").glob("*/receipt.json")).read_text())
    assert result == 0 and receipt["status"] == "complete"
    assert receipt["canonical_enrichment"] == {"state": "completed", "returncode": 0}
    assert receipt["source_evidence"]["job"]["state"] == ("outcome_unknown" if remote_unknown else "appended")
    assert (guarded_fence.read(tmp_path / "tier.lock") is not None) == remote_unknown
    with checkpoint.SharedLock(tmp_path / "tier.lock"):
        pass


def test_checkpoint_automatic_writes_pause_behind_targeted_fence(tmp_path, monkeypatch):
    from test_automatic import state
    run, receipt, lock, save = setup(tmp_path)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    guarded_evidence.seal_and_append(run, receipt, lock, save)
    refresh_run, path, manifest, attempt = state(tmp_path, receipt["source_evidence"]["config"], artifact(count=1))
    automatic.seal(refresh_run, manifest, "faris-lee", attempt, path)
    monkeypatch.setattr(checkpoint, "canonical_shared_lock_dir", lambda: lock.path)
    monkeypatch.setattr(checkpoint, "save_manifest", lambda *_: None)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("DB write crossed fence"))
    checkpoint.pump_source_evidence(refresh_run, manifest)
    assert manifest["automatic_source_evidence_paused"] is True
    assert attempt["source_evidence"]["state"] == "pending"


@pytest.mark.parametrize("tamper", ["invalid_json", "symlink", "permissions"])
def test_invalid_recovery_marker_fails_closed(tmp_path, tamper):
    lock = Lock(tmp_path)
    marker = guarded_fence.path(lock.path)
    if tamper == "symlink":
        marker.symlink_to(tmp_path / "missing")
    else:
        marker.write_text("{}")
        marker.chmod(0o644 if tamper == "permissions" else 0o600)
    with pytest.raises((ValueError, OSError)):
        guarded_fence.require_clear(lock.path)


def test_interrupted_fence_publication_never_dispatches_database(tmp_path, monkeypatch):
    run, receipt, lock, save = setup(tmp_path)
    publish = guarded_fence.publish
    def interrupted(lock_path, marker):
        publish(lock_path, marker)
        raise OSError("publication acknowledgement lost")
    monkeypatch.setattr(guarded_fence, "publish", interrupted)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("unexpected DB write"))
    with pytest.raises(OSError):
        guarded_evidence.seal_and_append(run, receipt, lock, save)
    assert guarded_fence.read(lock.path) == guarded_evidence.binding(run, receipt)
    assert json.loads((run / "receipt.json").read_text())["source_evidence"]["job"]["state"] == "pending"


def test_interrupted_appending_save_retains_fence_before_database(tmp_path, monkeypatch):
    run, receipt, lock, save = setup(tmp_path)
    def fail_appending_save():
        if receipt["source_evidence"].get("job", {}).get("state") in {"appending", "outcome_unknown"}:
            raise OSError("receipt save failed")
        save()
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("unexpected DB write"))
    with pytest.raises(OSError):
        guarded_evidence.seal_and_append(run, receipt, lock, fail_appending_save)
    assert guarded_fence.read(lock.path) == guarded_evidence.binding(run, receipt)
    assert json.loads((run / "receipt.json").read_text())["source_evidence"]["job"]["state"] == "pending"


def test_interrupted_but_settled_worker_seals_without_db_write(tmp_path, monkeypatch):
    import argparse
    import cre_guarded_tier as tier
    config, _, _ = setup_policy(tmp_path)
    args = argparse.Namespace(tier="enrich", source="faris-lee", batch=10, deadline_seconds=30,
                              expected_sha=SHA, apply=True, env_file="canonical.env",
                              source_evidence_policy=config["policy_path"],
                              source_evidence_policy_sha256=config["policy_sha256"],
                              source_evidence_env_file=config["env_file"],
                              source_evidence_target_sha256=config["target_sha256"])
    monkeypatch.setattr(checkpoint, "database_target_fingerprint", lambda _: {"value": "b" * 64})
    def worker(argv, log, env):
        path = Path(argv[argv.index("--artifact-out") + 1])
        path.write_text(json.dumps(artifact(count=1)))
        path.chmod(0o600)
        error = RuntimeError("worker interrupted after settlement")
        error.cre_local_settlement = {"state": "confirmed"}
        raise error
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("DB write after interruption"))
    rc = tier.run_guarded_tier(args, _lock_path=tmp_path / "tier.lock", _out_root=tmp_path / "runs",
                               _cpu_sampler=lambda: 10.0, _memory_sampler=lambda: 1,
                               _verify_checkout=lambda *_: None, _runner=worker)
    receipt = json.loads(next((tmp_path / "runs").glob("*/receipt.json")).read_text())
    assert rc == 1 and receipt["local_settlement"]["state"] == "confirmed"
    assert receipt["source_evidence"]["job"]["state"] == "pending"
    assert guarded_fence.read(tmp_path / "tier.lock") is not None
    with pytest.raises(checkpoint.LockHeldError):
        checkpoint.SharedLock(tmp_path / "tier.lock").acquire()


def test_missing_predispatch_fence_recovers_exact_pending_receipt(tmp_path, monkeypatch):
    run, receipt, lock, save = setup(tmp_path)
    publish = guarded_fence.publish
    monkeypatch.setattr(guarded_fence, "publish", lambda *_: (_ for _ in ()).throw(OSError("publish failed")))
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("DB write before marker"))
    with checkpoint.SharedLock(lock.path, recovery_required=True) as owner:
        with pytest.raises(OSError):
            guarded_evidence.seal_and_append(run, receipt, owner, save)
        owner.clear_recovery_requirement()
    assert guarded_fence.read(lock.path) is None
    assert json.loads((run / "receipt.json").read_text())["source_evidence"]["job"]["state"] == "pending"
    monkeypatch.setattr(guarded_fence, "publish", publish)
    writes = []
    def append_sql(*args, **kwargs):
        persisted = json.loads((run / "receipt.json").read_text())
        assert persisted["source_evidence"]["job"]["state"] == "appending"
        assert guarded_fence.read(lock.path) is not None
        writes.append(1)
    monkeypatch.setattr(automatic.cli, "run_psql", append_sql)
    guarded_evidence.reconcile(run / "receipt.json", lock.path, source="faris-lee", expected_sha=SHA)
    assert writes == [1]
    assert guarded_fence.read(lock.path) is None
    assert json.loads((run / "receipt.json").read_text())["source_evidence"]["status"] == "settled"


@pytest.mark.parametrize("state", ["appending", "outcome_unknown", "appended"])
def test_missing_fence_for_dispatched_job_is_not_recreated(tmp_path, monkeypatch, state):
    run, receipt, lock, save = setup(tmp_path)
    monkeypatch.setattr(guarded_fence, "publish", lambda *_: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        guarded_evidence.seal_only(run, receipt, lock, save)
    receipt["source_evidence"]["job"]["state"] = state
    save()
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("unsafe DB write"))
    with pytest.raises(ValueError):
        guarded_evidence.reconcile(run / "receipt.json", lock.path)
    assert guarded_fence.read(lock.path) is None


def test_appended_receipt_with_fence_is_marked_settled(tmp_path, monkeypatch):
    run, receipt, lock, save = setup(tmp_path)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: None)
    clear = guarded_fence.clear
    monkeypatch.setattr(guarded_fence, "clear", lambda *_: (_ for _ in ()).throw(OSError("clear failed")))
    with pytest.raises(OSError):
        guarded_evidence.seal_and_append(run, receipt, lock, save)
    assert json.loads((run / "receipt.json").read_text())["source_evidence"]["job"]["state"] == "appended"
    monkeypatch.setattr(guarded_fence, "clear", clear)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("unneeded DB replay"))
    guarded_evidence.reconcile(run / "receipt.json", lock.path)
    assert json.loads((run / "receipt.json").read_text())["source_evidence"]["status"] == "settled"
    assert guarded_fence.read(lock.path) is None


@pytest.mark.parametrize("tamper", ["plan", "claims", "policy"])
def test_missing_fence_recovery_revalidates_before_publication(tmp_path, monkeypatch, tamper):
    run, receipt, lock, save = setup(tmp_path)
    publish = guarded_fence.publish
    monkeypatch.setattr(guarded_fence, "publish", lambda *_: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        guarded_evidence.seal_only(run, receipt, lock, save)
    monkeypatch.setattr(guarded_fence, "publish", publish)
    if tamper == "plan":
        path = run / receipt["source_evidence"]["job"]["path"]
        path.write_bytes(path.read_bytes() + b" ")
    elif tamper == "claims":
        (run / "claims.json").write_text('{"claims": ["changed"]}')
    else:
        Path(receipt["source_evidence"]["config"]["policy_path"]).write_text("{}")
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("unsafe DB write"))
    with pytest.raises(ValueError):
        guarded_evidence.reconcile(run / "receipt.json", lock.path)
    assert guarded_fence.read(lock.path) is None


def test_settled_receipt_save_failure_retains_marker_for_recovery(tmp_path, monkeypatch):
    run, receipt, lock, save = setup(tmp_path)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: None)
    def fail_final_save():
        if receipt["source_evidence"].get("status") == "settled":
            raise OSError("settled receipt publication failed")
        save()
    with pytest.raises(OSError):
        guarded_evidence.seal_and_append(run, receipt, lock, fail_final_save)
    persisted = json.loads((run / "receipt.json").read_text())
    assert persisted["source_evidence"]["job"]["state"] == "appended"
    assert persisted["source_evidence"]["status"] != "settled"
    assert guarded_fence.read(lock.path) is not None
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: pytest.fail("unneeded DB replay"))
    guarded_evidence.reconcile(run / "receipt.json", lock.path)
    assert json.loads((run / "receipt.json").read_text())["source_evidence"]["status"] == "settled"
    assert guarded_fence.read(lock.path) is None


def test_lost_clear_acknowledgement_keeps_durable_settled_receipt(tmp_path, monkeypatch):
    run, receipt, lock, save = setup(tmp_path)
    monkeypatch.setattr(automatic.cli, "run_psql", lambda *a, **k: None)
    clear = guarded_fence.clear
    def clear_then_fail(*args):
        clear(*args)
        raise OSError("clear acknowledgement lost")
    monkeypatch.setattr(guarded_fence, "clear", clear_then_fail)
    with pytest.raises(OSError):
        guarded_evidence.seal_and_append(run, receipt, lock, save)
    persisted = json.loads((run / "receipt.json").read_text())
    assert persisted["source_evidence"]["job"]["state"] == "appended"
    assert persisted["source_evidence"]["status"] == "settled"
    assert guarded_fence.read(lock.path) is None


@pytest.mark.parametrize("stage", ["create", "partial_write", "file_fsync", "install"])
def test_abrupt_publication_never_leaves_partial_final_marker(tmp_path, stage):
    import os
    lock = Lock(tmp_path)
    marker = {"schema": "cre-targeted-evidence-fence/v1", "receipt": str(tmp_path / "receipt.json"),
              "plan_sha256": "a" * 64, "source": "faris-lee", "policy_sha256": "b" * 64,
              "target_sha256": "c" * 64}
    child = os.fork()
    if child == 0:
        if stage == "create":
            guarded_fence.tempfile.mkstemp = lambda **kwargs: os._exit(71)
        elif stage == "partial_write":
            original_write = os.write
            def die_during_write(fd, data):
                original_write(fd, data[:5])
                os._exit(71)
            guarded_fence.os.write = die_during_write
        elif stage == "file_fsync":
            original_fsync = os.fsync
            def die_after_fsync(fd):
                original_fsync(fd)
                os._exit(71)
            guarded_fence.os.fsync = die_after_fsync
        else:
            original_rename = os.rename
            def die_after_rename(source, target):
                original_rename(source, target)
                os._exit(71)
            guarded_fence.os.rename = die_after_rename
        guarded_fence.publish(lock.path, marker)
        os._exit(72)
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 71
    observed = guarded_fence.read(lock.path)
    assert observed == (marker if stage == "install" else None)
