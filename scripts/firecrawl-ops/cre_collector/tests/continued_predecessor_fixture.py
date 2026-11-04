"""Produce synthetic failed-maintenance evidence through the real Foreground writer."""

import json
import os
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import cre_code_handoff as handoff
import cre_code_handoff_git as git_module
import cre_refresh_pipeline
import cre_runtime_maintenance_steps as maintenance_steps
from cre_code_handoff_evidence import DEPENDENCY_CHECKS, sha
from cre_runtime_maintenance_steps import compose_argv


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def produce_prior_failure(tmp_path, prior, prior_path, observation, authorization, output, evidence, now, monkeypatch):
    """Produce real command receipts with injected process, clock, and Git boundaries."""
    output.mkdir()
    monkeypatch.setattr(handoff, "windows_path", lambda path: str(path))
    monkeypatch.setattr(git_module, "windows_path", lambda path: str(path))
    monkeypatch.setattr(maintenance_steps, "windows_path", lambda path: str(path))
    tooling = handoff.local_path(prior["roots"]["tooling"])
    monkeypatch.setattr(handoff, "__file__", str(tooling / "scripts/firecrawl-ops/cre_collector/cre_code_handoff.py"))
    monkeypatch.setattr(handoff, "Git", lambda _: SimpleNamespace(clean=lambda *args, **kwargs: None))
    monkeypatch.setattr(cre_refresh_pipeline, "command_poll_hook", lambda _: nullcontext())
    ticks = iter(now + timedelta(seconds=index) for index in range(30))
    monkeypatch.setattr(handoff, "now", lambda: next(ticks))
    checkpoint = SimpleNamespace(safe_process_env=dict,
                                 _run_logged_command=lambda argv, log, env: (log.write_bytes(b"synthetic closed log\n") and 0))
    runner = handoff.Foreground(prior, checkpoint, output)
    prior_raw = prior_path.read_bytes()
    runner.native("Hold", prior_path, sha(prior_raw), observation, authorization)
    collector = handoff.local_path(prior["roots"]["runtime"]) / "scripts/firecrawl-ops/cre_collector"
    runner.run("npm-ci", ["bash", "-c", 'cd -- "$1" && exec npm ci --ignore-scripts',
                           "cre-maintenance", str(collector)], effective_cwd=collector)
    for name, args in DEPENDENCY_CHECKS.items():
        runner.run(name, ["bash", "-c", 'cd -- "$1" && shift && exec "$@"',
                          "cre-maintenance", str(collector), *args], effective_cwd=collector)
    runner.run("compose-up", compose_argv(prior, evidence))
    handoff.Foreground(prior, checkpoint, output).native("FailureHold", prior_path, sha(prior_raw), observation, authorization)
    mutation = {"schema": "cre-runtime-maintenance-mutation-v1", "release_sha256": sha(prior_raw),
                "authorization_sha256": sha(authorization.read_bytes()), "operation_output": str(output),
                "entered_at": (now + timedelta(microseconds=500000)).isoformat(),
                "tooling": prior["tooling"], "fleet": prior["task_baseline"]}
    failure = {"schema": "cre-runtime-maintenance-failure-v1", "classification": "precommit-failure",
               "release_sha256": sha(prior_raw), "original_error": "container runtime configuration changed",
               "failure_hold": {"attempted": True, "invocation_completed": True, "error_type": None}}
    git = {"schema": "cre-runtime-maintenance-git-v1"}
    for role in ("native", "runtime"):
        before = prior[role + "_change"]["deployed"]
        git[role] = {"before": {**before, "root": prior["roots"][role], "status": "", "tracking": before["sha"]},
                     "after": {**prior["new"], "root": prior["roots"][role], "status": "", "tracking": prior["new"]["sha"]}}
    for name, value in (("mutation-start.json", mutation), ("failure.json", failure), ("git-observation.json", git)):
        (output / name).write_bytes(encoded(value))
    outer = Path(str(output) + ".outer-failure.json")
    outer.write_bytes(encoded({"schema": "cre-runtime-maintenance-outer-failure-v1", "release_sha256": sha(prior_raw),
                               "operation_output": str(output)}))
    streams = [Path(str(output) + suffix) for suffix in (".stdout.raw", ".stderr.raw")]
    for stream in streams:
        stream.write_bytes(b"")
    authority, pid, lease = b"synthetic authority", b"synthetic pid", b"synthetic lease"
    daily = Path(prior["roots"]["runtime"]) / "scripts/firecrawl-ops/cre_collector/out/daily"
    expected = {"schema": "cre-known-settled-owner-v1", "lock_path": str(daily / ".cre.lock"),
                "directory_identity": [2, 3], "owner_pid": os.getpid(), "generation": "synthetic",
                "authority": {"identity": [2, 4], "sha256": sha(authority), "size": len(authority)},
                "pid": {"identity": [2, 5], "sha256": sha(pid), "size": len(pid)},
                "lease": {"identity": [2, 6], "sha256": sha(lease), "size": len(lease)}}
    expected["fingerprint"] = sha(encoded(expected))
    owner = {"owner_pid": os.getpid(), "proc_state": "absent", "recovery_required": True,
             "lock_directory_present": True, "pid_file_present": True, "lease_file_present": True,
             "authority_inode": [2, 4], "directory_inode": [2, 3], "authority_sha256": sha(authority)}
    owner_path = tmp_path / "authority-observation.json"
    owner_path.write_bytes(encoded(owner))
    paths = [prior_path, observation, owner_path, outer, *streams, *sorted(output.iterdir())]
    manifest = {"schema": "independent-maintenance-failure-audit-manifest-v1", "files": [
        {"path": str(path), "bytes": path.stat().st_size, "sha256": sha(path.read_bytes())} for path in paths]}
    manifest_path = tmp_path / "failure-manifest.json"
    manifest_path.write_bytes(encoded(manifest))
    archive = daily / ".cre-owner-recovery-forensics" / expected["fingerprint"]
    (archive / "lock").mkdir(parents=True)
    for name, raw in (("authority-before", authority), ("lock/pid", pid), ("lock/lease", lease)):
        (archive / name).write_bytes(raw)
    proofs = {}
    for role, raw in {"known_outcome": manifest_path.read_bytes(), "remote_settlement": b"synthetic remote",
                      "descendant_settlement": b"synthetic descendants"}.items():
        (archive / (role + ".evidence")).write_bytes(raw)
        proofs[role] = {"path": "/synthetic/" + role, "sha256": sha(raw), "size": len(raw), "identity": [9, 9]}
    prepared = {"status": "PREPARED_EXACT_KNOWN_SETTLED_RECOVERY", "expected": expected,
                "operator": "cayman", "reference": "AGENTIC-4607", "evidence": proofs}
    completed = {"status": "KNOWN_SETTLED_OWNER_RECOVERED", "executed": True, "archive": str(archive),
                 "expected_fingerprint": expected["fingerprint"], "authority_identity": [2, 4],
                 "operator": "cayman", "reference": "AGENTIC-4607", "evidence": proofs}
    for name, value in (("prepared.json", prepared), ("completed.json", completed)):
        (archive / name).write_bytes(encoded(value))
    state_path = tmp_path / "recovery-expected.json"
    state_path.write_bytes(encoded(expected))
    paths_by_role = {"failed_release": prior_path, "failure_manifest": manifest_path,
                     "recovery_prepared": archive / "prepared.json", "recovery_completed": archive / "completed.json",
                     "recovery_expected_state": state_path}
    refs = [{"role": role, "path": str(path), "bytes": path.stat().st_size, "sha256": sha(path.read_bytes())}
            for role, path in paths_by_role.items()]
    continuation = {**{role: role for role in paths_by_role}, "failed_operation": str(output)}
    return SimpleNamespace(roles=refs, continuation=continuation, archive=archive, manifest=manifest_path)
