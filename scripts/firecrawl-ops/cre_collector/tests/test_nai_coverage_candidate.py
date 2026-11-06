"""Coverage-held candidate retains original and cannot use generic ingestion."""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import cre_checkpoint_refresh as refresh
from nai_partial.admission import candidate
from nai_partial.coverage_candidate import CANDIDATE, ORIGINAL, SCHEMA, prepare_owned
from tests.test_nai_post_admission import fixture


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("nai_partial.admission._utc_now", lambda: datetime(2026, 11, 5, 10, 2, tzinfo=timezone.utc))


def test_owned_marked_candidate_binds_original_and_refuses_unclaimed_cli(tmp_path, monkeypatch):
    path, manifest, sha = fixture(tmp_path)
    lock_path = tmp_path / ".cre.lock"
    original = path.read_bytes()
    monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock_path)
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: None)
    with refresh.SharedLock(lock_path, recovery_required=True) as owner:
        try:
            marked = prepare_owned(path.parent.parent, manifest, owner)
        finally:
            owner.clear_recovery_requirement()
    info = manifest["sources"]["nai-global"]["artifact"]
    assert (path.parent.parent / ORIGINAL).read_bytes() == original
    assert info["nai_coverage_original_sha256"] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original
    assert marked == path.parent.parent / CANDIDATE
    assert json.loads(marked.read_bytes())["schema"] == SCHEMA
    binding, staged = candidate(marked, manifest, code_sha=sha, dirty=False)
    assert binding["staged_count"] == 1 and len(staged["staged"]) == 1
    cli = Path(__file__).resolve().parents[1] / "cre_ingest.py"
    proc = subprocess.run([sys.executable, str(cli), "--in", str(marked), "--dry-run"],
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode != 0
    assert "refusing NAI partial artifact without guarded checkpoint admission" in proc.stderr
    (path.parent.parent / ORIGINAL).write_bytes(original + b" ")
    with pytest.raises(ValueError, match="original binding differs"):
        candidate(marked, manifest, code_sha=sha, dirty=False)


@pytest.mark.parametrize("failure", ["link", "unlink", "save"])
def test_interrupted_publication_retries_without_losing_original(tmp_path, monkeypatch, failure):
    path, manifest, sha = fixture(tmp_path)
    lock_path = tmp_path / ".cre.lock"
    original = path.read_bytes()
    monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock_path)
    import nai_partial.coverage_candidate as coverage
    saved = []
    monkeypatch.setattr(refresh, "save_manifest", lambda *_: saved.append(True))
    with refresh.SharedLock(lock_path, recovery_required=True) as owner:
        try:
            import copy
            prior = copy.deepcopy(manifest)
            with monkeypatch.context() as scoped:
                target = refresh if failure == "save" else coverage.os
                method = "save_manifest" if failure == "save" else failure
                scoped.setattr(target, method, lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic stop")))
                with pytest.raises(OSError, match="synthetic stop"):
                    prepare_owned(path.parent.parent, manifest, owner)
            assert path.read_bytes() == original
            # The persisted checkpoint is the old pointer when save fails.
            manifest = prior
            marked = prepare_owned(path.parent.parent, manifest, owner)
            assert path.read_bytes() == original and saved
            assert candidate(marked, manifest, code_sha=sha, dirty=False)[0]["staged_count"] == 1
            assert not (path.parent / ".nai-global-coverage-candidate.tmp").exists()
        finally:
            owner.clear_recovery_requirement()


@pytest.mark.parametrize("probe_hold", [False, True])
def test_real_preseal_preserves_original_transition_and_peer(tmp_path, monkeypatch, probe_hold):
    import copy
    from datetime import datetime, timezone
    import cre_refresh_pipeline as pipeline
    import nai_partial.admission as admission

    path, manifest, sha = fixture(tmp_path)
    root = path.parent.parent
    original = path.read_bytes()
    lock_path = tmp_path / ".cre.lock"
    manifest["scope"] = {}
    manifest["config"].update(pipeline=True, sources=["nai-global", "peer"])
    manifest["sources"]["peer"] = {"state": "ready", "artifact": {"path": "sources/peer.json"}}
    peer_before = copy.deepcopy(manifest["sources"]["peer"])
    manifest["preflight"]["shared_lock"] = str(lock_path)
    monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock_path)
    # Preserve a synthetic checkpoint so restart reads a fresh manifest object.
    monkeypatch.setattr(refresh, "save_manifest",
                        lambda directory, data: (directory / "manifest.json").write_text(json.dumps(data)))
    monkeypatch.setattr(refresh, "git_identity", lambda: (sha, False))
    monkeypatch.setattr(admission, "_utc_now", lambda: datetime(2026, 11, 5, 10, 2, tzinfo=timezone.utc))

    def gate_command(argv, *_args, **_kwargs):
        out = Path(argv[argv.index("--out") + 1])
        out.write_text(json.dumps({"summary": {"mode": "live", "torow_errors": 0,
            "baseline_updated": False, "baseline_update_sources": [], "hold_sources": ["nai-global"]},
            "generated_for_artifact": argv[argv.index("--in") + 1], "per_source": {"nai-global": {
                "current_active": 1, "verdict": "hold", "mark_missing_safe": False,
                "reason": "current_active 1 below floor 100"}}}))
        return 2

    def probe(artifact, owner_manifest, _env):
        if probe_hold:
            raise admission.NaiSourceProbeRefusal("synthetic source-local hold")
        binding, _ = candidate(artifact, owner_manifest, code_sha=sha, dirty=False)
        before = '[{"external_id":"infabode:1","listing":null,"source_index":null,"children":{}}]'
        return {**binding, "held": [], "preimage": before,
                "preimage_sha256": hashlib.sha256(before.encode()).hexdigest()}

    monkeypatch.setattr(refresh, "run_command", gate_command)
    monkeypatch.setattr(admission, "run_identity_probe", probe)
    # The compact synthetic artifact omits full-collector source entries;
    # actual structural validation is covered by the mixed PG fixture.
    monkeypatch.setattr(refresh, "validate_source_artifact", lambda *_args, **_kwargs: {})
    # Exercise the real admission calculation in-process; the separate
    # artifact-compute transport is covered by the mixed coordinator proof.
    original_admit = refresh._admit_current_inventory
    monkeypatch.setattr(refresh, "_admit_current_inventory",
        lambda *args, **kwargs: original_admit(*args, **kwargs, local=True))
    with refresh.SharedLock(lock_path, recovery_required=True) as owner:
        owner_token = pipeline._OWNER_CHECK.set(owner._require_authority)
        lease_token = pipeline._OWNER_LEASE.set(owner.lease_token)
        try:
            result = pipeline.preseal_source_gate(root, manifest, "nai-global", None)
            assert result is (not probe_hold)
            assert path.read_bytes() == original
            assert manifest["sources"]["peer"] == peer_before
            marked = root / CANDIDATE
            assert json.loads(marked.read_bytes())["schema"] == SCHEMA
            assert manifest["sources"]["nai-global"]["artifact"]["path"] == CANDIDATE
            if probe_hold:
                assert manifest["sources"]["nai-global"]["state"] == "gate_blocked"
            else:
                assert manifest["sources"]["nai-global"]["gate"]["verdict"] == "ok_additive_verified_subset"
            # Simulate a stop after the candidate pointer was saved: the next
            # preseal gate observes a freshly loaded marked candidate pointer.
            manifest = json.loads((root / "manifest.json").read_text())
            assert manifest["sources"]["nai-global"]["artifact"]["path"] == CANDIDATE
            assert pipeline.preseal_source_gate(root, manifest, "nai-global", None) is (not probe_hold)
            assert path.read_bytes() == original and manifest["sources"]["peer"] == peer_before
        finally:
            pipeline._OWNER_CHECK.reset(owner_token)
            pipeline._OWNER_LEASE.reset(lease_token)
            owner.clear_recovery_requirement()


@pytest.mark.parametrize("point", ["before_link", "after_link", "before_save", "after_save"])
@pytest.mark.parametrize("recovery_required", [False, True])
def test_real_killed_publisher_restarts_from_private_evidence(tmp_path, monkeypatch, point, recovery_required):
    """Normal lock can resume; the production-style recovery fence stays closed."""
    import copy
    import time

    path, manifest, sha = fixture(tmp_path)
    root = path.parent.parent
    original = path.read_bytes()
    manifest["sources"]["peer"] = {"state": "ready", "artifact": {"path": "sources/peer.json"}}
    peer_before = copy.deepcopy(manifest["sources"]["peer"])
    checkpoint = root / "manifest.json"
    checkpoint.write_text(json.dumps(manifest))
    lock_path = tmp_path / ".cre.lock"
    ready = tmp_path / "ready"
    child_code = """
import json,os,signal,sys
from pathlib import Path
import cre_checkpoint_refresh as r
import nai_partial.coverage_candidate as coverage
root, lockpath, ready, point = map(Path, sys.argv[1:5])
phase = point.name
r.canonical_shared_lock_dir = lambda: lockpath
original_link = os.link
def stop():
    ready.write_text('ready')
    os.kill(os.getpid(), signal.SIGSTOP)
def link(*args, **kwargs):
    if phase == 'before_link': stop()
    result = original_link(*args, **kwargs)
    if phase == 'after_link': stop()
    return result
def save(directory, data):
    if phase == 'before_save': stop()
    r.atomic_write_json(directory / 'manifest.json', data)
    if phase == 'after_save': stop()
coverage.os.link = link
r.save_manifest = save
manifest = json.loads((root / 'manifest.json').read_text())
with r.SharedLock(lockpath, recovery_required=(sys.argv[5] == 'true')) as owner:
    coverage.prepare_owned(root, manifest, owner)
"""
    child = subprocess.Popen([sys.executable, "-c", child_code, str(root), str(lock_path), str(ready), point, str(recovery_required).lower()],
                             cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists():
            assert child.poll() is None, child.stderr.read().decode(errors="replace")
            assert time.monotonic() < deadline, "publisher did not reach crash point"
            time.sleep(.02)
        child.kill()
        child.wait(timeout=10)
        assert path.read_bytes() == original
        persisted = json.loads(checkpoint.read_text())
        assert persisted["sources"]["peer"] == peer_before
        monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock_path)
        if recovery_required:
            with pytest.raises(refresh.LockHeldError):
                refresh.SharedLock(lock_path, recovery_required=True).acquire()
            return
        monkeypatch.setattr(refresh, "save_manifest",
                            lambda directory, data: refresh.atomic_write_json(directory / "manifest.json", data))
        with refresh.SharedLock(lock_path, recovery_required=False) as owner:
            if persisted["sources"]["nai-global"]["artifact"].get("nai_coverage_partial"):
                marked = root / persisted["sources"]["nai-global"]["artifact"]["path"]
            else:
                marked = prepare_owned(root, persisted, owner)
            assert candidate(marked, persisted, code_sha=sha, dirty=False)[0]["staged_count"] == 1
            assert path.read_bytes() == original
            assert persisted["sources"]["peer"] == peer_before
            assert not (path.parent / ".nai-global-coverage-candidate.tmp").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
