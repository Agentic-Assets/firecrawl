"""A failure receipt preserves the original error and bounds native cleanup."""

import json
from types import SimpleNamespace

import pytest

import cre_code_handoff as operation
import cre_runtime_maintenance as maintenance


@pytest.mark.parametrize("classification,marker,release_started,expected", [
    ("precommit-failure", False, False, False),
    ("precommit-failure", True, False, True),
    ("precommit-failure", True, True, False),
    ("commit-uncertain", True, True, False),
    ("postcommit-confirmation-failure", True, True, False),
])
def test_failure_receipt_only_holds_mutated_fleet_before_release_start(tmp_path, monkeypatch, classification, marker, release_started, expected):
    from cre_runtime_maintenance import record_maintenance_failure

    if marker:
        (tmp_path / "mutation-start.json").write_text("{}")
    if release_started:
        (tmp_path / "release-start.json").write_text("{}")
    calls = []
    monkeypatch.setattr(operation.Foreground, "native", lambda self, mode, *args, **kwargs: calls.append(mode))
    args = SimpleNamespace(release="/synthetic/release", observation="/synthetic/observation",
                           authorization="/synthetic/authorization")
    original = RuntimeError("original controlled failure")
    record_maintenance_failure(args, b"release", {}, object(), tmp_path, classification, original)
    result = json.loads((tmp_path / "failure.json").read_text())
    assert result["schema"] == "cre-runtime-maintenance-failure-v1"
    assert result["classification"] == classification
    assert result["original_error_type"] == "RuntimeError" and result["original_error"] == str(original)
    assert result["failure_hold"]["attempted"] is expected
    assert result["failure_hold"]["invocation_completed"] is expected
    assert "succeeded" not in result["failure_hold"]
    assert calls == (["FailureHold"] if expected else [])
    assert not (tmp_path / "terminal.json").exists()


@pytest.mark.parametrize("fault", ["clear", "clear-report", "exit", "lock-clear", "terminal"])
def test_release_failures_preserve_original_and_never_rehold_tasks(tmp_path, monkeypatch, fault):
    observed, auth, output = tmp_path / "observation", tmp_path / "authorization", tmp_path / "operation"
    observed.write_bytes(b"observation")
    auth.write_bytes(b"{}")
    args = SimpleNamespace(release="/synthetic/release", observation=str(observed), authorization=str(auth), output=str(output))
    packet = {"new": {"sha": "a" * 40, "tree": "b" * 40}, "roots": {"runtime": str(tmp_path)}, "expected_images": {}}
    calls = []

    class Lease:
        def __enter__(self): return self
        def __exit__(self, kind, value, traceback):
            if kind is None and fault == "exit": raise RuntimeError("exit")
        def clear_recovery_requirement(self):
            if fault in {"clear", "clear-report"}: raise RuntimeError("clear")

    checkpoint = SimpleNamespace(canonical_shared_lock_dir=lambda _: tmp_path / "lock",
                                 SharedLock=lambda *args, **kwargs: Lease())
    for name in ("validate_maintenance_release", "validate_maintenance_authorization", "validate_prepared_images",
                 "maintenance_readiness", "validate_maintenance_ready", "validate_operation_paths",
                 "validate_current_projection"):
        monkeypatch.setattr(maintenance, name, lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(maintenance, "run_maintenance_steps", lambda *args, **kwargs: {}, raising=False)
    monkeypatch.setattr(operation, "owned_authority", lambda *args: {"synthetic": "owned"})
    monkeypatch.setattr(operation, "released_authority", lambda *args: ([1, "token", "generation", False], [1, 2]))
    monkeypatch.setattr(operation, "lock_clear", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("lock-clear"))
                        if fault == "lock-clear" else None)
    monkeypatch.setattr(operation.Foreground, "native", lambda self, mode, *args, **kwargs: calls.append(mode))
    original_write = operation.write_new
    def write(path, value):
        if path.name == "terminal.json" and fault == "terminal": raise RuntimeError("terminal")
        if path.name == "failure.json" and fault == "clear-report": raise RuntimeError("report failed")
        return original_write(path, value)
    monkeypatch.setattr(operation, "write_new", write)
    evidence = SimpleNamespace(recheck=lambda: None)
    git = SimpleNamespace(development=lambda _: None)
    with pytest.raises(RuntimeError, match="clear" if fault == "clear-report" else fault):
        maintenance.apply_maintenance(args, b"release", packet, evidence, git, (checkpoint, None, None, None))
    if fault == "clear-report":
        assert calls == [] and not (output / "terminal.json").exists()
        return
    receipt = json.loads((output / "failure.json").read_text())
    assert receipt["original_error"] == fault
    assert receipt["classification"] == ("commit-uncertain" if fault in {"clear", "exit", "lock-clear"}
                                          else "postcommit-confirmation-failure")
    assert calls == []
    assert not (output / "terminal.json").exists()
