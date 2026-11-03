"""Produce an explicitly synthetic partial Repin with real Foreground receipts."""
import copy
import json
import os
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import cre_code_handoff as handoff
import cre_code_handoff_git as git_module
import cre_refresh_pipeline
import cre_runtime_maintenance_steps as maintenance_steps
from cre_code_handoff_evidence import DEPENDENCY_CHECKS, HandoffError, sha


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def produce_partial_failure(tmp_path, prior, prior_path, observation, authorization, output, evidence, now, monkeypatch, *, owner_salt=b""):
    """Generate retained failure bytes; process, Git and scheduler are test doubles."""
    output.mkdir()
    audit = tmp_path / "partial-audit"
    audit.mkdir()
    monkeypatch.setattr(handoff, "windows_path", lambda path: str(path))
    monkeypatch.setattr(git_module, "windows_path", lambda path: str(path))
    monkeypatch.setattr(maintenance_steps, "windows_path", lambda path: str(path))
    tooling = handoff.local_path(prior["roots"]["tooling"])
    monkeypatch.setattr(handoff, "__file__", str(tooling / "scripts/firecrawl-ops/cre_collector/cre_code_handoff.py"))
    monkeypatch.setattr(handoff, "Git", lambda _: SimpleNamespace(clean=lambda *args, **kwargs: None))
    monkeypatch.setattr(cre_refresh_pipeline, "command_poll_hook", lambda _: nullcontext())
    ticks = iter(now + timedelta(seconds=index) for index in range(50))
    monkeypatch.setattr(handoff, "now", lambda: next(ticks))
    def run_command(argv, log, env):
        failed = "-Mode" in argv and argv[argv.index("-Mode") + 1] == "Repin"
        log.write_bytes(b"maintenance observation expired/future\n" if failed else b"synthetic closed log\n")
        return 1 if failed else 0
    checkpoint = SimpleNamespace(safe_process_env=dict, run_command=run_command)
    runner = handoff.Foreground(prior, checkpoint, output)
    prior_raw = prior_path.read_bytes()
    runner.native("Hold", prior_path, sha(prior_raw), observation, authorization)
    collector = handoff.local_path(prior["roots"]["runtime"]) / "scripts/firecrawl-ops/cre_collector"
    for name, args in DEPENDENCY_CHECKS.items():
        runner.run(name, ["bash", "-c", 'cd -- "$1" && shift && exec "$@"',
                          "cre-maintenance", str(collector), *args], effective_cwd=collector)
    try:
        runner.native("Repin", prior_path, sha(prior_raw), observation, authorization)
    except HandoffError as exc:
        if str(exc) != "native-repin failed (1); preserve held tasks and recovery": raise
    else:
        raise AssertionError("synthetic Repin must fail")
    handoff.Foreground(prior, checkpoint, output).native("FailureHold", prior_path, sha(prior_raw), observation, authorization)

    def write(path, value):
        raw = value if isinstance(value, bytes) else encoded(value)
        path.write_bytes(raw)
        return path
    def ref(path, role=None):
        raw = path.read_bytes()
        value = {"path": str(path), "bytes": len(raw), "sha256": sha(raw)}
        if role is not None: value["role"] = role
        return value

    mutation = {"schema": "cre-runtime-maintenance-mutation-v1", "release_sha256": sha(prior_raw),
                "authorization_sha256": sha(authorization.read_bytes()), "operation_output": str(output),
                "entered_at": (now + timedelta(microseconds=500000)).isoformat(),
                "tooling": prior["tooling"], "fleet": prior["task_baseline"]}
    failure = {"schema": "cre-runtime-maintenance-failure-v1", "classification": "precommit-failure",
               "release_sha256": sha(prior_raw), "original_error_type": "HandoffError",
               "original_error": "native-repin failed (1); preserve held tasks and recovery",
               "failure_hold": {"attempted": True, "invocation_completed": True, "error_type": None}}
    git = {"schema": "cre-runtime-maintenance-git-v1"}
    for role in ("native", "runtime"):
        before = prior[role + "_change"]["deployed"]
        git[role] = {"before": {**before, "root": prior["roots"][role], "status": "", "tracking": before["sha"]},
                     "after": {**prior["new"], "root": prior["roots"][role], "status": "", "tracking": prior["new"]["sha"]}}
    for name, value in (("mutation-start.json", mutation), ("failure.json", failure), ("git-observation.json", git)):
        write(output / name, value)
    outer = write(Path(str(output) + ".outer-failure.json"),
                  {"schema": "cre-runtime-maintenance-outer-failure-v1", "release_sha256": sha(prior_raw),
                   "operation_output": str(output), "original_error": "Native foreground exit/drain uncertain",
                   "commit_and_recovery_state": "unknown; independent readback required"})
    streams = [write(Path(str(output) + suffix), b"") for suffix in (".stdout.raw", ".stderr.raw")]
    current, tasks, task_roles, principals = [], [], [], []
    for row in prior["task_baseline"]:
        key = row["key"]
        original = evidence.raw(row["xml"])
        selected = key == "interra"
        current_xml = original.replace(prior["old"]["sha"].encode(), prior["new"]["sha"].encode()) if selected else original
        write(output / (key + "-before.xml"), original)
        if selected: write(output / (key + "-final.xml"), current_xml)
        current_path = write(tmp_path / ("current-" + key + ".xml"), current_xml)
        current_role = "partial-current-" + key
        task_roles.append(ref(current_path, current_role))
        successor = copy.deepcopy(row)
        successor.update(xml=current_role, pin_before=prior["new"]["sha"] if selected else prior["old"]["sha"])
        if selected:
            successor["actions"] = [{name: value.replace(prior["old"]["sha"], prior["new"]["sha"])
                                     for name, value in action.items()} for action in row["actions"]]
        current.append(successor)
        observed = {key_: value for key_, value in successor.items() if key_ not in {"xml", "pin_before", "desired_enabled"}}
        observed_path = write(audit / (key + "-observed.xml"), current_xml)
        observed.update(state="Disabled", xml_sha256=sha(current_xml), xml_reference=ref(observed_path),
                        pin_state="selected" if selected else "original")
        tasks.append(observed)
        principal = ET.fromstring(original).findtext("{*}Principals/{*}Principal/{*}UserId")
        principals.append({"key": key, "user_id": principal, "sid": principal,
                           "aliases": [principal, principal, "HOST\\fixture"]})
    native = {"schema": "partial-maintenance-native-readback-v1", "release_sha256": sha(prior_raw),
              "observed_at": (now + timedelta(seconds=12)).isoformat(), "tasks": tasks,
              "scheduler_instances": [], "native_frontends": []}
    write(audit / "native-observation.json", native)
    watermark = {"schema": "cre-restoration-watermark-v1", "channel": "Microsoft-Windows-TaskScheduler/Operational",
                 "computer": "synthetic-host", "record_id": 100, "record_sha256": "0" * 64,
                 "captured_at": (now + timedelta(microseconds=100000)).isoformat()}
    watermark_path = write(output / "scheduler-watermark.json", watermark)
    execution = {"schema": "cre-restoration-execution-v1", "watermark": watermark,
                 "watermark_reference": ref(watermark_path), "channel": watermark["channel"],
                 "computer": watermark["computer"], "upper_record_id": 100,
                 "started_at": (now + timedelta(seconds=13)).isoformat(), "finished_at": (now + timedelta(seconds=14)).isoformat(),
                 "events": [], "instances": [], "principals": principals, "observer_sid": principals[0]["sid"],
                 "observer_is_admin": False, "tasks": copy.deepcopy(tasks)}
    execution_path = write(audit / "scheduler-observation.json", execution)
    write(audit / "scheduler-execution-reference.json", ref(execution_path))
    containers = evidence.obj("container_config_before")
    for path in (output / "containers-before.json", output / "containers-after.json", audit / "containers-after.json"):
        write(path, containers)

    authority, pid, lease = b"synthetic partial authority" + owner_salt, b"synthetic partial pid", b"synthetic partial lease"
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
    owner_path = write(audit / "authority-observation.json", owner)
    paths = [prior_path, observation, owner_path, outer, *streams, *sorted(output.iterdir()),
             *[path for path in sorted(audit.iterdir()) if path != owner_path]]
    manifest = {"schema": "independent-maintenance-failure-audit-manifest-v1", "release_input_count": len(prior["inputs"]),
                "release_inputs": copy.deepcopy(prior["inputs"]), "files": [ref(path, "synthetic") for path in paths]}
    manifest_path = write(tmp_path / "partial-failure-manifest.json", manifest)
    archive = daily / ".cre-owner-recovery-forensics" / expected["fingerprint"]
    (archive / "lock").mkdir(parents=True)
    for name, raw in (("authority-before", authority), ("lock/pid", pid), ("lock/lease", lease)):
        write(archive / name, raw)
    proofs = {}
    for role, raw in {"known_outcome": manifest_path.read_bytes(), "remote_settlement": b"synthetic remote",
                      "descendant_settlement": b"synthetic descendants"}.items():
        write(archive / (role + ".evidence"), raw)
        proofs[role] = {"path": "/synthetic/" + role, "sha256": sha(raw), "size": len(raw), "identity": [9, 9]}
    prepared = {"status": "PREPARED_EXACT_KNOWN_SETTLED_RECOVERY", "expected": expected,
                "operator": "cayman", "reference": "AGENTIC-4607", "evidence": proofs}
    completed = {"status": "KNOWN_SETTLED_OWNER_RECOVERED", "executed": True, "archive": str(archive),
                 "expected_fingerprint": expected["fingerprint"], "authority_identity": [2, 4],
                 "operator": "cayman", "reference": "AGENTIC-4607", "evidence": proofs}
    for name, value in (("prepared.json", prepared), ("completed.json", completed)):
        write(archive / name, value)
    state_path = write(tmp_path / "partial-recovery-expected.json", expected)
    paths_by_role = {"failed_release": prior_path, "failure_manifest": manifest_path,
                     "recovery_prepared": archive / "prepared.json", "recovery_completed": archive / "completed.json",
                     "recovery_expected_state": state_path}
    roles = [ref(path, role) for role, path in paths_by_role.items()]
    continuation = {**{role: role for role in paths_by_role}, "failed_operation": str(output)}
    return SimpleNamespace(roles=roles + task_roles, continuation=continuation, task_baseline=current,
                           archive=archive, manifest=manifest_path)
