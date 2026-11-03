"""Real local client/server boundary; no scheduler or production lock."""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import test_cre_code_handoff as fixtures

git_case = fixtures.git_case


def test_real_owner_process_serves_one_fresh_native_request(tmp_path):
    from cre_owned_handoff_boundary import OwnerBoundary

    binding = {"selector": "--maintenance-packet", "release": "/release", "output": "/output",
               "observation": "/observation", "authorization": "/authorization"}
    calls = []
    with OwnerBoundary(binding, lambda mode: calls.append(mode), lambda pid, uid, req: uid == os.getuid()) as owner:
        owner.activate("Repin")
        program = ("from cre_owned_handoff_boundary import request_boundary;import json,sys;"
                   "request_boundary(sys.argv[1],int(sys.argv[2]),json.loads(sys.argv[3]),'Repin',"
                   "lambda:None,lambda:None)")
        child = subprocess.Popen([sys.executable, "-c", program, str(owner.path), str(os.getpid()), json.dumps(binding)])
        deadline = time.monotonic() + 10
        while child.poll() is None and time.monotonic() < deadline:
            owner.poll()
            time.sleep(.01)
        assert child.wait(timeout=2) == 0
    assert calls == ["Repin"]


def test_owner_refusal_never_yields_success(tmp_path):
    from cre_owned_handoff_boundary import OwnerBoundary

    binding = {"selector": "--maintenance-packet", "release": "/release", "output": "/output",
               "observation": "/observation", "authorization": "/authorization"}
    def deny(_):
        raise ValueError("evidence changed")
    with OwnerBoundary(binding, deny, lambda pid, uid, req: uid == os.getuid()) as owner:
        owner.activate("Hold")
        program = ("from cre_owned_handoff_boundary import request_boundary;import json,sys;"
                   "request_boundary(sys.argv[1],int(sys.argv[2]),json.loads(sys.argv[3]),'Hold',"
                   "lambda:None,lambda:None)")
        child = subprocess.Popen([sys.executable, "-c", program, str(owner.path), str(os.getpid()), json.dumps(binding)],
                                 stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while child.poll() is None and time.monotonic() < deadline:
            owner.poll()
            time.sleep(.01)
        assert child.wait(timeout=2) != 0


def test_owner_refuses_changed_nested_evidence_and_checks_lease_last(tmp_path, monkeypatch):
    import cre_code_handoff as handoff

    files = {name: tmp_path / name for name in ("release", "observation", "authorization", "nested")}
    for name, path in files.items():
        path.write_bytes(name.encode())
    args = SimpleNamespace(maintenance_packet=str(files["release"]), restoration_packet=None,
                           release=str(files["release"]), observation=str(files["observation"]),
                           authorization=str(files["authorization"]), output=str(tmp_path / "output"))
    calls = []
    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={str(files["nested"]): b"nested"})
    def recheck():
        assert files["nested"].read_bytes() == evidence.original_payloads[str(files["nested"])]
        calls.append("evidence")
    evidence.recheck = recheck
    monkeypatch.setattr(handoff, "boundary_executables", lambda *_: ())
    monkeypatch.setattr(handoff, "runtime_modules", lambda *_: calls.append("tooling"))
    monkeypatch.setattr(handoff, "assert_owned", lambda *_args, **_kwargs: calls.append("lease"))
    monkeypatch.setattr(handoff, "check_boundary_times", lambda *_: calls.append("clock"))
    boundary = handoff.owned_boundary(args, b"release", {}, evidence, None, None, None,
                                       lambda: calls.append("admission"))
    boundary.check("Hold")
    assert calls == ["evidence", "tooling", "admission", "lease", "clock"]
    files["nested"].write_bytes(b"altered")
    with pytest.raises(AssertionError):
        boundary.check("Hold")


def test_boundary_clock_is_read_at_each_call(monkeypatch):
    import cre_code_handoff as handoff
    from cre_code_handoff_evidence import HandoffError

    start = datetime(2026, 11, 3, tzinfo=UTC)
    observation = json.dumps({"observed_at": start.isoformat(), "qualified_at": start.isoformat()}).encode()
    authorization = json.dumps({"authorized_at": start.isoformat(),
                                "expires_at": (start + timedelta(seconds=300)).isoformat()}).encode()
    monkeypatch.setattr(handoff, "now", lambda: start + timedelta(seconds=299))
    handoff.check_boundary_times(observation, authorization)
    monkeypatch.setattr(handoff, "now", lambda: start + timedelta(seconds=300))
    with pytest.raises(HandoffError, match="expired"):
        handoff.check_boundary_times(observation, authorization)


def test_conflicting_windows_case_aliases_refuse_before_socket_creation(tmp_path, monkeypatch):
    import cre_code_handoff as handoff
    from cre_code_handoff_evidence import HandoffError

    observation, authorization = tmp_path / "observation", tmp_path / "authorization"
    observation.write_bytes(b"observation")
    authorization.write_bytes(b"authorization")
    args = SimpleNamespace(maintenance_packet="C:/release", restoration_packet=None,
                           release="C:/release", output="C:/output", observation=str(observation),
                           authorization=str(authorization))
    evidence = SimpleNamespace(inputs={"first": {"path": "C:/Dir/File"}}, original={"first": b"one"},
                               original_payloads={"c:/dir/file": b"two"})
    monkeypatch.setattr(handoff, "boundary_executables", lambda *_: ())
    with pytest.raises(HandoffError, match="aliases"):
        handoff.owned_boundary(args, b"release", {}, evidence, None, None, None, lambda: None)


@pytest.mark.parametrize("failing_check", ["tooling", "git"])
def test_owner_refuses_changed_tooling_or_git_before_authorizing(tmp_path, monkeypatch, failing_check):
    import cre_code_handoff as handoff

    paths = {name: tmp_path / name for name in ("release", "observation", "authorization")}
    for name, path in paths.items():
        path.write_bytes(name.encode())
    args = SimpleNamespace(maintenance_packet=str(paths["release"]), restoration_packet=None,
                           release=str(paths["release"]), observation=str(paths["observation"]),
                           authorization=str(paths["authorization"]), output=str(tmp_path / "output"))
    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={}, recheck=lambda: None)
    def tooling(*_args):
        if failing_check == "tooling":
            raise RuntimeError("tooling changed")
    def git(_mode):
        if failing_check == "git":
            raise RuntimeError("git objects changed")
    monkeypatch.setattr(handoff, "runtime_modules", tooling)
    monkeypatch.setattr(handoff, "boundary_executables", lambda *_: ())
    boundary = handoff.owned_boundary(args, b"release", {}, evidence, None, None, None,
                                       lambda: pytest.fail("admission after changed source"), extra_check=git)
    with pytest.raises(RuntimeError, match="changed"):
        boundary.check("Hold")


def test_executable_fingerprints_change_when_selected_binary_changes(tmp_path, monkeypatch):
    import cre_code_handoff as handoff

    native_git, powershell, linux_git, python = (tmp_path / name for name in ("native-git", "pwsh", "git", "python"))
    for path in (native_git, powershell, linux_git, python):
        path.write_bytes(path.name.encode())
    monkeypatch.setattr(handoff.shutil, "which", lambda _name: str(linux_git))
    monkeypatch.setattr(handoff.sys, "executable", str(python))
    packet = {"programs": {"native_git": str(native_git), "powershell": str(powershell)}}
    original = handoff.boundary_executables(packet)
    native_git.write_bytes(b"replaced")
    assert handoff.boundary_executables(packet) != original


def test_wrong_native_mode_is_denied_before_owner_check():
    from cre_owned_handoff_boundary import OwnerBoundary

    calls = []
    binding = {"release": "/release"}
    with OwnerBoundary(binding, lambda mode: calls.append(mode), lambda *_: True) as owner:
        owner.activate("Hold")
        program = ("from cre_owned_handoff_boundary import request_boundary;import json,sys;"
                   "request_boundary(sys.argv[1],int(sys.argv[2]),json.loads(sys.argv[3]),'Repin',"
                   "lambda:None,lambda:None)")
        child = subprocess.Popen([sys.executable, "-c", program, str(owner.path), str(os.getpid()), json.dumps(binding)],
                                 stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while child.poll() is None and time.monotonic() < deadline:
            owner.poll()
            time.sleep(.01)
        assert child.wait(timeout=2) != 0
    assert calls == []


def test_owner_refuses_when_deadline_crosses_during_recheck():
    from cre_owned_handoff_boundary import OwnerBoundary

    binding = {"release": "/release"}
    calls = []
    def slow_check(_mode):
        calls.append(True)
        time.sleep(.03)
    with OwnerBoundary(binding, slow_check, lambda *_: True) as owner:
        owner.activate("Hold")
        owner.command_deadline = time.monotonic() + .02
        program = ("from cre_owned_handoff_boundary import request_boundary;import json,sys;"
                   "request_boundary(sys.argv[1],int(sys.argv[2]),json.loads(sys.argv[3]),'Hold',"
                   "lambda:None,lambda:None)")
        child = subprocess.Popen([sys.executable, "-c", program, str(owner.path), str(os.getpid()), json.dumps(binding)],
                                 stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while child.poll() is None and time.monotonic() < deadline:
            owner.poll()
            time.sleep(.01)
        assert child.wait(timeout=2) != 0
    assert calls == [True]


def test_socket_cleanup_failure_does_not_mask_original_error():
    from cre_owned_handoff_boundary import OwnerBoundary

    directory = None
    try:
        with pytest.raises(RuntimeError, match="original operation"), OwnerBoundary(
            {}, lambda _mode: None, lambda *_: True
        ) as owner:
            directory = owner.directory
            (directory / "unexpected").write_text("fixture")
            raise RuntimeError("original operation")
    finally:
        if directory is not None:
            (directory / "unexpected").unlink(missing_ok=True)
            directory.rmdir()


@pytest.mark.parametrize("stage", ["run", "native"])
@pytest.mark.parametrize("original_failure", [False, True])
def test_foreground_deactivation_preserves_original_failure(tmp_path, monkeypatch, stage, original_failure):
    import cre_code_handoff as handoff

    class BadBoundary:
        def activate(self, _mode):
            pass
        def poll(self):
            pass
        def deactivate(self):
            raise OSError("synthetic close failure")

    def command(*_args, **_kwargs):
        if original_failure:
            raise RuntimeError("original native failure")
        return 0

    checkpoint = SimpleNamespace(safe_process_env=dict, run_command=command)
    runner = handoff.Foreground({"programs": {"powershell": "/tmp/pwsh"}}, checkpoint, tmp_path,
                                boundary=BadBoundary())
    if stage == "native":
        monkeypatch.setattr(handoff, "windows_path", str)
        monkeypatch.setattr(runner, "run", command)
    expected, message = (RuntimeError, "original native failure") if original_failure else (OSError, "synthetic close")
    with pytest.raises(expected, match=message) as result:
        if stage == "run":
            runner.run("synthetic", [])
        else:
            runner.native("Hold", "/tmp/release", "a" * 64)
    if original_failure and hasattr(result.value, "__notes__"):
        assert any("owner boundary deactivation failed" in note for note in result.value.__notes__)


def test_client_rechecks_owner_after_postreply_input_reads():
    from cre_owned_handoff_boundary import OwnerBoundary, request_boundary

    state = {"clock_calls": 0, "owned": True, "result": None}
    def clock():
        state["clock_calls"] += 1
        if state["clock_calls"] == 2:
            state["owned"] = False
    def owner_check():
        if not state["owned"]:
            raise RuntimeError("lease lost during input reread")
    with OwnerBoundary({}, lambda _mode: None, lambda *_: True) as owner:
        owner.activate("Hold")
        def client():
            try:
                request_boundary(owner.path, os.getpid(), {}, "Hold", owner_check, clock)
            except RuntimeError:
                state["result"] = "refused"
        thread = threading.Thread(target=client)
        thread.start()
        deadline = time.monotonic() + 10
        while thread.is_alive() and time.monotonic() < deadline:
            owner.poll()
            time.sleep(.01)
        thread.join(timeout=2)
    assert not thread.is_alive()
    assert state["result"] == "refused"


def test_foreground_real_process_wait_services_owner_boundary(tmp_path):
    import cre_checkpoint_refresh as checkpoint
    from cre_code_handoff import Foreground
    from cre_owned_handoff_boundary import OwnerBoundary

    binding = {"release": "/release"}
    calls = []
    program = ("from cre_owned_handoff_boundary import request_boundary;import json,sys;"
               "request_boundary(sys.argv[1],int(sys.argv[2]),json.loads(sys.argv[3]),'Hold',"
               "lambda:None,lambda:None)")
    with OwnerBoundary(binding, lambda mode: calls.append(mode), lambda *_: True) as owner:
        owner.activate("Hold")
        runner = Foreground({}, checkpoint, tmp_path, boundary=owner)
        receipt = runner.run("synthetic-native", [sys.executable, "-c", program, str(owner.path),
                                                  str(os.getpid()), json.dumps(binding)], seconds=10)
    assert calls == ["Hold"]
    assert receipt["exit_code"] == 0
    assert receipt["local_settlement"] == "confirmed" and receipt["log_closed"] is True


def test_real_peer_process_must_match_exact_native_boundary_command(tmp_path, monkeypatch):
    import cre_code_handoff as handoff
    from cre_owned_handoff_boundary import OwnerBoundary

    script = tmp_path / "cre_code_handoff.py"
    binding = {"selector": "--maintenance-packet", "release": "/release", "output": "/output",
               "observation": "/observation", "authorization": "/authorization"}
    script.write_text("from cre_owned_handoff_boundary import request_boundary\n"
                      "import json,sys\n"
                      "request_boundary(sys.argv[0] + '.socket', int(sys.argv[4]), "
                      + repr(binding) + ", sys.argv[6], lambda:None, lambda:None)\n")
    monkeypatch.setattr(handoff, "__file__", str(script))
    calls = []
    with OwnerBoundary(binding, lambda mode: calls.append(mode), handoff.verify_boundary_peer) as owner:
        owner.activate("Hold")
        # The fixture script gets its actual owner socket through a private symlink.
        (tmp_path / "cre_code_handoff.py.socket").symlink_to(owner.path)
        child = subprocess.Popen([sys.executable, "-B", str(script), "--maintenance-packet", "/release",
                                  "--assert-owner-boundary-pid", str(os.getpid()), "--boundary-mode", "Hold",
                                  "--observation", "/observation", "--authorization", "/authorization",
                                  "--output", "/output"], stderr=subprocess.DEVNULL,
                                 env={**os.environ, "PYTHONPATH": os.getcwd()})
        deadline = time.monotonic() + 10
        while child.poll() is None and time.monotonic() < deadline:
            owner.poll()
            time.sleep(.01)
        assert child.wait(timeout=2) == 0
    assert calls == ["Hold"]


def test_historical_git_recheck_rejects_replaced_commit(git_case):
    from cre_code_handoff_evidence import HandoffError
    from cre_runtime_maintenance_transition import validate_maintenance_git_history

    git, target, scope, _, path = git_case
    source = target.parent / "source"
    scope["changed_dependency_paths"] = []
    validate_maintenance_git_history(git, source, scope)
    (source / path).write_text("changed replacement object\n")
    fixtures.git_command(source, "commit", "-am", "replacement")
    replacement = git.text(source, "rev-parse", "HEAD")
    fixtures.git_command(source, "replace", scope["selected"]["sha"], replacement)
    with pytest.raises((HandoffError, subprocess.CalledProcessError)):
        validate_maintenance_git_history(git, source, scope)
