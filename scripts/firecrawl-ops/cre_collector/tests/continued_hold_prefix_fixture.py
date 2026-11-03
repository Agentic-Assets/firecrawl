"""Synthetic Hold-only failure, independently sampled and recovery-sealed."""
import copy
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from continued_partial_fixture import encoded, produce_partial_failure

from cre_code_handoff_evidence import sha


def produce_hold_failure(tmp_path, prior, prior_path, observation, authorization, output, evidence, now, monkeypatch, source):
    # Reuse the synthetic command/owner generator, then rebuild the entirely
    # synthetic artifacts as the distinct two-command Hold-only attempt.
    generated = produce_partial_failure(tmp_path, prior, prior_path, observation, authorization, output, evidence, now, monkeypatch, owner_salt=b" held-prefix")
    audit = tmp_path / "partial-audit"
    def write(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else encoded(value))
        return path
    def ref(path, role=None):
        path = Path(path); raw = path.read_bytes()
        row = {"path": str(path), "bytes": len(raw), "sha256": sha(raw)}
        if role is not None:
            row["role"] = role
        return row
    prior_raw = prior_path.read_bytes()
    for path in list(output.iterdir()):
        if path.name.startswith(("02-", "03-", "04-", "05-")) or path.name in {
            "git-observation.json", "containers-after.json", "interra-final.xml"}:
            path.unlink()
    hold = json.loads((output / "01-native-hold.json").read_bytes())
    finished = now + timedelta(seconds=20)
    write(output / "failure.json", {"schema": "cre-runtime-maintenance-failure-v1", "classification": "precommit-failure",
          "release_sha256": sha(prior_raw), "original_error_type": "HandoffError",
          "original_error": "maintenance observation expired/future",
          "failure_hold": {"attempted": True, "invocation_completed": True, "error_type": None}})
    for name in ("native-hold.json", "hold-windows-admission.capture.json"):
        write(output / name, {})
    for name in ("hold-windows-admission.stderr.raw", "hold-windows-admission.stdout.raw"):
        write(output / name, b"")
    write(output / "failure-hold.json", {"schema": "cre-runtime-maintenance-failure-hold-v1", "attempted": True,
          "concurrent_native_frontends": [], "visibility_error": None,
          "tasks": [{"key": row["key"], "task_path": row["task_path"], "task_name": row["task_name"],
                     "disable_attempted": True, "disabled": True, "state": "Disabled", "error": None}
                    for row in prior["task_baseline"]]})
    tasks = []
    for row in prior["task_baseline"]:
        raw = evidence.raw(row["xml"])
        write(output / (row["key"] + "-before.xml"), raw)
        path = write(audit / "native" / (row["key"] + "-observed.xml"), raw)
        sample = {k: v for k, v in row.items() if k not in {"xml", "pin_before", "desired_enabled"}}
        sample.update(state="Disabled", xml_sha256=sha(raw), xml_reference=ref(path))
        tasks.append(sample)
    old_execution = json.loads((audit / "scheduler-observation.json").read_bytes())
    old_execution.update(started_at=(finished + timedelta(seconds=1)).isoformat(),
                         finished_at=(finished + timedelta(seconds=2)).isoformat(),
                         tasks=[{k: v for k, v in row.items() if k != "xml_reference"} for row in tasks])
    execution = write(audit / "native" / "scheduler-observation.json", old_execution)
    native = {"schema": "cre-held-partial-maintenance-observation-v1", "observed_at": (finished + timedelta(seconds=3)).isoformat(),
              "tasks": tasks, "frontends": [], "instances": [], "execution": ref(execution)}
    write(audit / "native" / "observation.json", native)
    roots = {"native": prior["new"], "runtime": prior["runtime_before"]}
    write(audit / "git-readback.json", {"schema": "independent-post-failure-git-readback-v1",
          "observed_at": finished.isoformat(), "roots": {key: {"head": ident["sha"], "tree": ident["tree"],
          "tracking": ident["sha"], "branch": "main", "status": ""} for key, ident in roots.items()}})
    containers = evidence.obj("container_config_before")
    write(audit / "host-observation-01.json", {"observed_at": finished.isoformat(),
          "queues": dict.fromkeys(("jobs_in_queue", "active_jobs_in_queue", "waiting_jobs_in_queue", "active_crawl_count"), 0),
          "browser": {"status": "healthy", "activePages": 0}, "normalized_containers": containers})
    expected_path = tmp_path / "partial-recovery-expected.json"
    expected = json.loads(expected_path.read_bytes())
    write(audit / "linux-observation-01.json", {"schema": "cre-known-owner-linux-settlement-observation-v1",
          "observed_at": finished.isoformat(), "fingerprint": expected["fingerprint"], "owner_pid": expected["owner_pid"],
          "expected_state_matches": True, "owner_exists": False, "owner_group_members": [],
          "unreadable_same_uid_pids": [], "visible_competing_roles": []})
    launcher = tmp_path / "synthetic-launcher"
    stdout, stderr = write(launcher / "03-apply.stdout.raw", b""), write(launcher / "03-apply.stderr.raw", b"")
    def arg(flag):
        return hold["argv"][hold["argv"].index(flag) + 1]
    from cre_code_handoff_git import local_path

    script = str(local_path(prior["roots"]["tooling"]) / "scripts/firecrawl-ops/cre_collector/windows/Invoke-CreCodeHandoff.ps1")
    argv = ["-NoProfile", "-NonInteractive", "-File", script, "-Mode", "Apply", "-MaintenancePath", str(prior_path),
            "-MaintenanceSha", sha(prior_raw), "-Observation", arg("-Observation"), "-Authorization", arg("-Authorization"),
            "-Output", str(output)]
    stream = {"Eof": True, "Truncated": False, "Failed": False, "ObservedBytes": 0, "CapturedBytes": 0}
    write(launcher / "03-apply.json", {"schema": "reviewed-maintenance-launch-stage-v1", "stage": "03-apply",
          "release_sha256": sha(prior_raw), "program": prior["programs"]["powershell"], "argv": argv,
          "stdout_sha256": sha(b""), "stderr_sha256": sha(b""), "started_at": hold["started_at"],
          "finished_at": finished.isoformat(), "capture": {"Started": True, "Failed": False, "NativeExitCode": 1,
          "Stdout": stream, "Stderr": stream}})
    # Fresh source-bound observation of exactly the same held task XML.
    capture_dir = tmp_path / "hold-fresh-capture"
    fresh_execution = copy.deepcopy(old_execution)
    fresh_time = finished + timedelta(seconds=5)
    fresh_execution.update(started_at=(finished + timedelta(seconds=3)).isoformat(),
                           finished_at=(finished + timedelta(seconds=4)).isoformat())
    fresh_exec_path = write(capture_dir / "fresh-execution.json", fresh_execution)
    fresh = dict(native, observed_at=fresh_time.isoformat(), execution=ref(fresh_exec_path))
    fresh_ref = ref(write(capture_dir / "observation" / "observation.json", fresh))
    program = ref(write(capture_dir / "program-copy.bin", b"synthetic native PowerShell binary"))
    capture_script = ref(write(capture_dir / "capture.ps1", b"synthetic read-only capture script\n"))
    out, err = ref(write(capture_dir / "stdout.raw", b"")), ref(write(capture_dir / "stderr.raw", b""))
    names = ("CreCodeHandoff.ps1", "CreTaskContract.ps1", "CreRestorationExecution.ps1", "CreRuntimeMaintenance.ps1", "CreNativeCapture.cs")
    files = []
    for name in names:
        relative = "scripts/firecrawl-ops/cre_collector/windows/" + name
        raw = (source.root / relative).read_bytes()
        files.append({"path": prior["roots"]["native"] + "/" + relative, "bytes": len(raw), "sha256": sha(raw)})
    capture_stream = {"ObservedBytes": 0, "CapturedBytes": 0, "Eof": True, **dict.fromkeys(("Truncated", "ReadFailed", "WriteFailed", "FlushFailed", "CloseFailed", "DrainTimedOut", "ReaderDidNotStop", "RunReferenceConflict", "Failed"), False)}
    capture = {"schema": "cre-held-partial-maintenance-native-capture-v1", "source_identity": prior["new"],
               "source_root": prior["roots"]["native"], "source_files": files, "capture_script": capture_script,
               "program": {**program, "path": prior["programs"]["powershell"]},
               "argv": ["-NoProfile", "-NonInteractive", "-File", capture_script["path"], "-Output", str(capture_dir / "observation")],
               "observation": fresh_ref, "stdout": out, "stderr": err,
               "started_at": (fresh_time - timedelta(seconds=1)).isoformat(), "finished_at": (fresh_time + timedelta(seconds=1)).isoformat(),
               "capture": {"StartAttempted": True, "Started": True, "StartFailed": False, "ReadOnlyDeadlineExpired": False,
                           "NativeExitCode": 0, "Failed": False, "Stdout": capture_stream, "Stderr": capture_stream}}
    capture_ref = ref(write(capture_dir / "receipt.json", capture))
    outer = [Path(str(output) + suffix) for suffix in (".outer-failure.json", ".stdout.raw", ".stderr.raw")]
    audit_paths = [prior_path, observation, expected_path, *outer, *sorted(output.iterdir()),
                   audit / "native" / "observation.json", execution, *sorted((audit / "native").glob("*-observed.xml")),
                   audit / "git-readback.json", audit / "host-observation-01.json", audit / "linux-observation-01.json",
                   launcher / "03-apply.json", stdout, stderr]
    entries = [ref(path, "operation" if path.parent == output else "synthetic") for path in audit_paths]
    manifest = {"schema": "independent-maintenance-failure-audit-manifest-v1", "release_input_count": len(prior["inputs"]),
                "release_inputs": copy.deepcopy(prior["inputs"]), "files": entries}
    manifest_path = write(generated.manifest, manifest)
    archive = generated.archive
    prepared = json.loads((archive / "prepared.json").read_bytes())
    completed = json.loads((archive / "completed.json").read_bytes())
    proof = manifest_path.read_bytes()
    write(archive / "known_outcome.evidence", proof)
    for row in (prepared, completed):
        row["evidence"]["known_outcome"].update(sha256=sha(proof), size=len(proof))
    write(archive / "prepared.json", prepared); write(archive / "completed.json", completed)
    paths = {"failed_release": prior_path, "failure_manifest": manifest_path, "recovery_prepared": archive / "prepared.json",
             "recovery_completed": archive / "completed.json", "recovery_expected_state": expected_path,
             "fresh_held_observation": Path(fresh_ref["path"]), "fresh_held_capture": Path(capture_ref["path"]),
             "fresh_held_program": Path(program["path"])}
    return SimpleNamespace(roles=[ref(path, role) for role, path in paths.items()],
                           continuation={**{role: role for role in paths}, "failed_operation": str(output),
                                         "failure_kind": "native-advanced-hold-only-v1"},
                           task_baseline=copy.deepcopy(prior["task_baseline"]), fresh_time=fresh_time)
