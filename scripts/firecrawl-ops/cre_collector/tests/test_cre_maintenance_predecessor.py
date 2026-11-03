"""Synthetic retained failure/recovery files exercise actual predecessor reader."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cre_code_handoff_evidence import Evidence, HandoffError, sha
from cre_runtime_maintenance_continuation import validate_continuation_predecessor

OLD = {"sha": "a" * 40, "tree": "b" * 40}
BEFORE = {"sha": "c" * 40, "tree": "d" * 40}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def predecessor_case(tmp_path, monkeypatch):
    import cre_code_handoff_git
    import cre_runtime_maintenance
    import cre_runtime_maintenance_steps
    from cre_code_handoff_evidence import DEPENDENCY_CHECKS

    # Synthetic original admission is separately covered by the complete A
    # fixture. Preserve real predecessor command and archive validation here.
    monkeypatch.setattr(cre_runtime_maintenance, "validate_maintenance_release", lambda *_: None)
    monkeypatch.setattr(cre_runtime_maintenance, "validate_maintenance_authorization", lambda *_, **__: None)
    monkeypatch.setattr(cre_code_handoff_git, "windows_path", lambda value: str(value))
    monkeypatch.setattr(cre_runtime_maintenance_steps, "compose_argv", lambda *_: ["synthetic-compose"])
    operation = tmp_path / "operation"
    operation.mkdir()
    roots = {"native": str(tmp_path / "native"), "runtime": str(tmp_path / "runtime"),
             "tooling": str(tmp_path / "tooling")}
    packet = {"old": OLD, "runtime_before": BEFORE, "roots": roots,
              "failed_handoff_release": {"path": "/original-a", "sha256": "1" * 64, "bytes": 10}}
    prior = {"schema": "cre-runtime-maintenance-v1", "old": OLD, "new": BEFORE, "roots": roots,
             "failed_handoff_release": packet["failed_handoff_release"], "inputs": [],
             "native_change": {"deployed": OLD}, "runtime_change": {"deployed": OLD},
             "programs": {"powershell": str(tmp_path / "pwsh")}}
    prior_path = tmp_path / "failed-release.json"
    prior_path.write_bytes(encoded(prior))
    failure = {"schema": "cre-runtime-maintenance-failure-v1", "classification": "precommit-failure",
               "release_sha256": sha(prior_path.read_bytes()), "original_error": "container runtime configuration changed",
               "failure_hold": {"attempted": True, "invocation_completed": True, "error_type": None}}
    observed_path = tmp_path / "initial-observation.json"
    observed_path.write_bytes(encoded({"fixture": "initial observation"}))
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_bytes(encoded({"fixture": "authorization"}))
    now = datetime(2026, 11, 2, tzinfo=UTC)
    mutation = {"schema": "cre-runtime-maintenance-mutation-v1", "release_sha256": sha(prior_path.read_bytes()),
                "authorization_sha256": sha(authorization_path.read_bytes()),
                "entered_at": (now + timedelta(microseconds=500000)).isoformat(), "operation_output": str(operation)}
    git_rows = {"schema": "cre-runtime-maintenance-git-v1"}
    for role in ("native", "runtime"):
        git_rows[role] = {"before": {**OLD, "root": roots[role], "status": "", "tracking": OLD["sha"]},
                          "after": {**BEFORE, "root": roots[role], "status": "", "tracking": BEFORE["sha"]}}
    for name, value in (("failure.json", failure), ("mutation-start.json", mutation), ("git-observation.json", git_rows)):
        (operation / name).write_bytes(encoded(value))
    script = Path(roots["tooling"]) / "scripts/firecrawl-ops/cre_collector/windows/Invoke-CreCodeHandoff.ps1"
    def native(mode):
        return [prior["programs"]["powershell"], "-NoProfile", "-NonInteractive", "-File", str(script),
                "-Mode", mode, "-MaintenancePath", str(prior_path), "-MaintenanceSha", sha(prior_path.read_bytes()),
                "-Output", str(operation), "-OwnerPid", "91680", "-Observation", str(observed_path),
                "-Authorization", str(authorization_path)]
    collector = Path(roots["runtime"]) / "scripts/firecrawl-ops/cre_collector"
    commands = [("01-native-hold", native("Hold"), None),
                ("02-npm-ci", ["bash", "-c", 'cd -- "$1" && exec npm ci --ignore-scripts',
                               "cre-maintenance", str(collector)], BEFORE)]
    for index, (name, argv) in enumerate(DEPENDENCY_CHECKS.items(), start=3):
        commands.append((f"{index:02d}-{name}", ["bash", "-c", 'cd -- "$1" && shift && exec "$@"',
                                                       "cre-maintenance", str(collector), *argv], BEFORE))
    commands += [("06-compose-up", ["synthetic-compose"], None), ("01-native-failurehold", native("FailureHold"), None)]
    command_paths = []
    for index, (name, argv, selected) in enumerate(commands):
        log = operation / (name + ".log")
        log.write_bytes(b"synthetic settled foreground log\n")
        receipt = {"schema": "cre-owned-gate-v1" if selected else "cre-code-command-v1",
                   "producer": "checkpoint-run-command-v1", "argv": argv, "exit_code": 0,
                   "local_settlement": "confirmed", "log_closed": True, "command_log_sha256": sha(log.read_bytes()),
                   "started_at": (now + timedelta(seconds=index)).isoformat(),
                   "finished_at": (now + timedelta(seconds=index, microseconds=900000)).isoformat(),
                   "effective_cwd": str(collector) if selected else None}
        if selected:
            receipt.update(identity=selected, unchanged=True, cwd=str(collector))
        receipt_path = operation / (name + ".json")
        receipt_path.write_bytes(encoded(receipt))
        command_paths += [receipt_path, log]
    outer = Path(str(operation) + ".outer-failure.json")
    outer.write_bytes(encoded({"schema": "cre-runtime-maintenance-outer-failure-v1",
                               "release_sha256": sha(prior_path.read_bytes()), "operation_output": str(operation)}))
    streams = [Path(str(operation) + suffix) for suffix in (".stdout.raw", ".stderr.raw")]
    for stream in streams:
        stream.write_bytes(b"synthetic captured stream\n")
    authority = b"v1 synthetic recovery-required\n"
    pid = b"91680 1\n"
    lease = b"operator-recovery-required:synthetic\n"
    daily = tmp_path / "runtime/scripts/firecrawl-ops/cre_collector/out/daily"
    expected = {"schema": "cre-known-settled-owner-v1", "lock_path": str(daily / ".cre.lock"),
                "directory_identity": [2, 3], "owner_pid": 91680, "generation": "synthetic",
                "authority": {"identity": [2, 4], "sha256": sha(authority), "size": len(authority)},
                "pid": {"identity": [2, 5], "sha256": sha(pid), "size": len(pid)},
                "lease": {"identity": [2, 6], "sha256": sha(lease), "size": len(lease)}}
    expected["fingerprint"] = sha(encoded(expected))
    authority_observation = {"owner_pid": 91680, "proc_state": "absent", "recovery_required": True,
                             "lock_directory_present": True, "pid_file_present": True,
                             "lease_file_present": True, "authority_inode": [2, 4],
                             "directory_inode": [2, 3], "authority_sha256": sha(authority)}
    owner_path = tmp_path / "authority-observation.json"
    owner_path.write_bytes(encoded(authority_observation))
    paths = [prior_path, owner_path, observed_path, outer, *streams, *command_paths] + [operation / name for name in ("failure.json", "mutation-start.json", "git-observation.json")]
    manifest = {"schema": "independent-maintenance-failure-audit-manifest-v1", "files": [
        {"path": str(path), "bytes": len(path.read_bytes()), "sha256": sha(path.read_bytes())} for path in paths]}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(encoded(manifest))
    archive = daily / ".cre-owner-recovery-forensics" / expected["fingerprint"]
    (archive / "lock").mkdir(parents=True)
    for name, raw in (("authority-before", authority), ("lock/pid", pid), ("lock/lease", lease)):
        (archive / name).write_bytes(raw)
    proof_bytes = {"known_outcome": manifest_path.read_bytes(), "remote_settlement": b"synthetic remote",
                   "descendant_settlement": b"synthetic descendants"}
    proofs = {}
    for role, raw in proof_bytes.items():
        (archive / (role + ".evidence")).write_bytes(raw)
        proofs[role] = {"path": "/synthetic/" + role, "sha256": sha(raw), "size": len(raw), "identity": [9, 9]}
    prepared = {"status": "PREPARED_EXACT_KNOWN_SETTLED_RECOVERY", "expected": expected,
                "operator": "cayman", "reference": "AGENTIC-4607", "evidence": proofs}
    completed = {"status": "KNOWN_SETTLED_OWNER_RECOVERED", "executed": True, "archive": str(archive),
                 "expected_fingerprint": expected["fingerprint"], "authority_identity": [2, 4],
                 "operator": "cayman", "reference": "AGENTIC-4607", "evidence": proofs}
    (archive / "prepared.json").write_bytes(encoded(prepared))
    (archive / "completed.json").write_bytes(encoded(completed))
    state_path = tmp_path / "expected.json"
    state_path.write_bytes(encoded(expected))
    paths_by_role = {"failed_release": prior_path, "failure_manifest": manifest_path,
                     "recovery_prepared": archive / "prepared.json", "recovery_completed": archive / "completed.json",
                     "recovery_expected_state": state_path}
    continuation = {**{role: role for role in paths_by_role}, "failed_operation": str(operation)}
    packet["continuation"] = continuation
    inputs = [{"role": role, "path": str(path), "bytes": len(path.read_bytes()), "sha256": sha(path.read_bytes())}
              for role, path in paths_by_role.items()]
    evidence = Evidence({"inputs": inputs}, lambda path: __import__("pathlib").Path(path).read_bytes())
    return packet, evidence, continuation, archive, operation


def test_real_predecessor_reader_accepts_synthetic_bound_archive(predecessor_case):
    packet, evidence, continuation, _, _ = predecessor_case
    validate_continuation_predecessor(packet, evidence, continuation)


@pytest.mark.parametrize("item", ["owner", "lease", "known_outcome", "failure", "git",
                                  "command", "log", "outer", "authorization"])
def test_predecessor_reader_refuses_altered_or_partial_archive(predecessor_case, item):
    packet, evidence, continuation, archive, operation = predecessor_case
    targets = {"owner": archive / "authority-before", "lease": archive / "lock/lease",
               "known_outcome": archive / "known_outcome.evidence", "failure": operation / "failure.json",
               "git": operation / "git-observation.json", "command": operation / "06-compose-up.json",
               "log": operation / "02-npm-ci.log", "outer": Path(str(operation) + ".outer-failure.json"),
               "authorization": operation.parent / "authorization.json"}
    targets[item].write_bytes(b"foreign")
    with pytest.raises((HandoffError, ValueError)):
        validate_continuation_predecessor(packet, evidence, continuation)
