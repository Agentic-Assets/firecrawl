"""Synthetic host boundaries exercise actual owned-maintenance failure ordering."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

import cre_code_handoff as operation
import cre_runtime_maintenance as maintenance


@pytest.mark.parametrize("failure_report_raises", [False, True])
def test_failed_dependency_install_keeps_recovery_and_original_error(tmp_path, monkeypatch, failure_report_raises):
    if not hasattr(maintenance, "apply_maintenance"):
        pytest.fail("maintenance apply producer is missing")
    observation = tmp_path / "observation.json"
    authorization = tmp_path / "authorization.json"
    observation.write_text("{}")
    authorization.write_text(json.dumps({"operation_output": str(tmp_path / "operation")}))
    release = tmp_path / "release.json"
    release.write_bytes(b"{}")
    args = SimpleNamespace(release=str(release), observation=str(observation), authorization=str(authorization),
                           output=str(tmp_path / "operation"))
    packet = {"roots": {"native": "C:/native", "runtime": "/runtime"}, "old": {"sha": "a" * 40, "tree": "b" * 40},
              "new": {"sha": "c" * 40, "tree": "d" * 40}, "native_before": {"sha": "c" * 40, "tree": "d" * 40},
              "expected_images": {}}
    state = {"recovery": False, "install_called": False}
    class Lease:
        def __init__(self, _path, *, recovery_required):
            assert recovery_required is True
        def __enter__(self):
            state["recovery"] = True
            return self
        def __exit__(self, *_args):
            pass
        def clear_recovery_requirement(self):
            state["recovery"] = False
    checkpoint = SimpleNamespace(SharedLock=Lease, canonical_shared_lock_dir=lambda _root: tmp_path / "lock")
    def fail_install(*_args, **_kwargs):
        state["install_called"] = True
        raise RuntimeError("synthetic npm failure")
    monkeypatch.setattr(maintenance, "validate_maintenance_release", lambda *_: None)
    monkeypatch.setattr(maintenance, "validate_maintenance_authorization", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(operation, "check_boundary_times", lambda *_args: None)
    monkeypatch.setattr(maintenance, "validate_prepared_images", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(maintenance, "validate_operation_paths", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(maintenance, "validate_current_projection", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(maintenance, "maintenance_readiness", lambda *_args, **_kwargs: {}, raising=False)
    monkeypatch.setattr(maintenance, "run_maintenance_steps", fail_install, raising=False)
    monkeypatch.setattr(operation, "owned_boundary", lambda *_args, **_kwargs: nullcontext(None))
    def report_failure(*_args, **_kwargs):
        if failure_report_raises:
            raise OSError("synthetic failure-report error")
    monkeypatch.setattr(maintenance, "record_maintenance_failure", report_failure, raising=False)
    git = SimpleNamespace(development=lambda _packet: None)
    evidence = SimpleNamespace(recheck=lambda: None)
    with pytest.raises(RuntimeError, match="synthetic npm failure"):
        maintenance.apply_maintenance(args, b"{}", packet, evidence, git, (checkpoint, None, None, None))
    assert state["install_called"] is True
    assert state["recovery"] is True
    assert not (tmp_path / "operation" / "terminal.json").exists()


def test_projection_refusal_happens_before_lease_or_runtime_steps(tmp_path, monkeypatch):
    if not hasattr(maintenance, "apply_maintenance"):
        pytest.fail("maintenance apply producer is missing")
    observation, authorization = tmp_path / "observation", tmp_path / "authorization"
    observation.write_text("{}")
    authorization.write_text("{}")
    release = tmp_path / "release.json"
    release.write_bytes(b"{}")
    args = SimpleNamespace(release=str(release), observation=str(observation), authorization=str(authorization),
                           output=str(tmp_path / "operation"))
    packet = {"roots": {"runtime": "/runtime"}, "new": {"sha": "c" * 40, "tree": "d" * 40},
              "expected_images": {"api": "sha256:" + "9" * 64}}
    monkeypatch.setattr(maintenance, "validate_maintenance_release", lambda *_: None)
    monkeypatch.setattr(maintenance, "validate_maintenance_authorization", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(operation, "check_boundary_times", lambda *_args: None)
    monkeypatch.setattr(maintenance, "validate_prepared_images", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(maintenance, "validate_operation_paths", lambda *_args, **_kwargs: None, raising=False)
    monkeypatch.setattr(maintenance, "maintenance_readiness", lambda *_args, **_kwargs: {})
    def refuse(*_args, **_kwargs):
        raise RuntimeError("projection differs")
    monkeypatch.setattr(maintenance, "validate_current_projection", refuse, raising=False)
    checkpoint = SimpleNamespace(canonical_shared_lock_dir=lambda _root: tmp_path / "lock",
                                 SharedLock=lambda *_args, **_kwargs: pytest.fail("lease acquired"))
    git = SimpleNamespace(development=lambda _: None)
    with pytest.raises(RuntimeError, match="projection differs"):
        maintenance.apply_maintenance(args, b"{}", packet, SimpleNamespace(), git, (checkpoint, None, None, None))


def test_operation_and_projection_outputs_must_be_distinct_windows_mapped_scratch_paths():
    from cre_code_handoff_evidence import HandoffError

    if not hasattr(maintenance, "validate_operation_paths"):
        pytest.fail("maintenance output path validation missing")
    packet = {"roots": {"development": "C:/dev", "native": "C:/runtime", "runtime": "/home/user/runtime"},
              "projection_output": "C:/dev/.scratch/projection"}
    maintenance.validate_operation_paths(packet, "C:/dev/.scratch/operation")
    for output in ("/home/user/operation", "C:/runtime/.scratch/operation", "C:/dev/.scratch/projection"):
        with pytest.raises(HandoffError):
            maintenance.validate_operation_paths(packet, output)
