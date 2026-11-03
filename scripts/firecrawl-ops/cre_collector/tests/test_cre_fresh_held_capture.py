"""A fresh held snapshot requires its actual source-bound native capture."""
import copy
import json
from types import SimpleNamespace

import pytest

import cre_runtime_maintenance_continuation as continuation
from cre_code_handoff_evidence import Evidence, HandoffError, sha


def test_fresh_capture_binds_source_command_streams_and_observation():
    root = "/fixture/native"
    identity = {"sha": "a" * 40, "tree": "b" * 40}
    names = ("CreCodeHandoff.ps1", "CreTaskContract.ps1", "CreRestorationExecution.ps1",
             "CreRuntimeMaintenance.ps1", "CreNativeCapture.cs")
    data = {}
    def ref(path, raw):
        data[path] = raw
        return {"path": path, "sha256": sha(raw), "bytes": len(raw)}
    files = [ref(root + "/scripts/firecrawl-ops/cre_collector/windows/" + name, name.encode()) for name in names]
    script = ref("/fixture/script.ps1", b"reviewed script")
    program = ref("/fixture/pwsh.exe", b"program")
    stdout, stderr = ref("/fixture/capture/stdout.raw", b"done"), ref("/fixture/capture/stderr.raw", b"")
    observation = {"schema": "cre-held-partial-maintenance-observation-v1", "observed_at": "2026-11-03T04:00:01Z"}
    obs = ref("/fixture/capture/observation/observation.json", json.dumps(observation).encode())
    stream = lambda size: {"ObservedBytes": size, "CapturedBytes": size, "Truncated": False, "Eof": True,
                           "ReadFailed": False, "WriteFailed": False, "FlushFailed": False, "CloseFailed": False,
                           "DrainTimedOut": False, "ReaderDidNotStop": False, "RunReferenceConflict": False, "Failed": False}
    capture = {"schema": "cre-held-partial-maintenance-native-capture-v1", "started_at": "2026-11-03T04:00:00Z",
               "finished_at": "2026-11-03T04:00:02Z", "source_root": root, "source_identity": identity,
               "source_files": files, "capture_script": script, "program": program,
               "argv": ["-NoProfile", "-NonInteractive", "-File", script["path"], "-Output", "/fixture/capture/observation"],
               "capture": {"StartAttempted": True, "Started": True, "StartFailed": False,
                           "ReadOnlyDeadlineExpired": False, "NativeExitCode": 0, "Failed": False,
                           "Stdout": stream(4), "Stderr": stream(0)},
               "stdout": stdout, "stderr": stderr, "observation": obs}
    packet = {"roots": {"native": root}, "programs": {"powershell": program["path"]},
              "inputs": [{"role": "fresh", **obs}, {"role": "program_archive", **ref("/fixture/archive-pwsh.exe", b"program")}], "continuation": {"fresh_held_observation": "fresh", "fresh_held_program": "program_archive"}}
    prior = {"new": identity}
    evidence = Evidence(packet, data.__getitem__)
    git = SimpleNamespace(command=lambda _repo, _show, path: path.rsplit("/", 1)[-1].encode(), text=lambda *_args: identity["tree"])
    continuation.validate_fresh_held_capture(packet, prior, evidence, observation, capture, git, "/fixture/repo", "/fixture/capture/receipt.json")
    for fault in ("source", "stream", "exit", "eof", "output"):
        bad = copy.deepcopy(capture)
        if fault == "source": bad["source_files"][0]["sha256"] = "0" * 64
        if fault == "stream": bad["stdout"]["sha256"] = "0" * 64
        if fault == "exit": bad["capture"]["NativeExitCode"] = 1
        if fault == "eof": bad["capture"]["Stdout"]["Eof"] = False
        if fault == "output": bad["argv"][-1] = "/foreign"
        with pytest.raises(HandoffError):
            continuation.validate_fresh_held_capture(packet, prior, evidence, observation, bad, git, "/fixture/repo", "/fixture/capture/receipt.json")
