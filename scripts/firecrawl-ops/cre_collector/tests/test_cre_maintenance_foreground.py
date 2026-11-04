"""Maintenance commands use their selected identity at the collector cwd."""

from types import SimpleNamespace

import pytest

import cre_code_handoff as operation
from cre_code_handoff_evidence import HandoffError, sha


def test_owned_collector_command_uses_maintenance_new_identity(tmp_path, monkeypatch):
    selected = {"sha": "a" * 40, "tree": "b" * 40}
    runtime = tmp_path / "runtime"
    collector = runtime / "scripts/firecrawl-ops/cre_collector"
    collector.mkdir(parents=True)
    packet = {"schema": "cre-runtime-maintenance-v1", "new": selected, "roots": {"runtime": str(runtime)},
              "programs": {"native_git": "unused"}}
    checkpoint = SimpleNamespace(safe_process_env=dict,
                                 _run_logged_command=lambda argv, log, env: (log.write_bytes(b"complete\n") and 0))
    monkeypatch.setattr(operation, "Git", lambda _: SimpleNamespace(clean=lambda *args, **kwargs: None))
    runner = operation.Foreground(packet, checkpoint, tmp_path)
    receipt = runner.run("npm-ls", ["npm", "ls"], effective_cwd=collector)
    assert receipt["identity"] == selected and receipt["unchanged"] is True


def test_command_reader_uses_actual_foreground_receipt_and_rejects_changed_bytes(tmp_path, monkeypatch):
    from cre_runtime_maintenance_transition import validate_maintenance_command

    selected = {"sha": "a" * 40, "tree": "b" * 40}
    collector = tmp_path / "runtime/scripts/firecrawl-ops/cre_collector"
    collector.mkdir(parents=True)
    packet = {"schema": "cre-runtime-maintenance-v1", "new": selected,
              "roots": {"runtime": str(tmp_path / "runtime")}, "programs": {"native_git": "unused"}}
    checkpoint = SimpleNamespace(safe_process_env=dict,
                                 _run_logged_command=lambda argv, log, env: (log.write_bytes(b"complete\n") and 0))
    monkeypatch.setattr(operation, "Git", lambda _: SimpleNamespace(clean=lambda *args, **kwargs: None))
    operation.Foreground(packet, checkpoint, tmp_path).run("npm-ls", ["npm", "ls"], effective_cwd=collector)
    log, receipt = tmp_path / "01-npm-ls.log", tmp_path / "01-npm-ls.json"
    ref = lambda path: {"path": str(path), "bytes": path.stat().st_size, "sha256": sha(path.read_bytes())}
    row = {"name": "npm-ls", "receipt": ref(receipt), "log": ref(log)}
    read = lambda path: __import__("pathlib").Path(path).read_bytes()
    validate_maintenance_command(row, ["npm", "ls"], selected, str(collector), read)
    with pytest.raises(HandoffError):
        validate_maintenance_command(row, ["npm", "install"], selected, str(collector), read)
    log.write_bytes(b"altered\n")
    with pytest.raises(HandoffError):
        validate_maintenance_command(row, ["npm", "ls"], selected, str(collector), read)
