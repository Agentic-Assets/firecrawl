"""Private synthetic owners only; never signal a live collector or database."""

import copy
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import cre_checkpoint_refresh as checkpoint
import cre_guarded_tier as tier
import cre_stop_guarded_tier as stop
import pytest
from cre_owned_processes import Settlement

RUN = "2026-10-13T230000Z-0123456789ab"
LINUX = pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd/proc contract")


@pytest.fixture
def fake_owner(monkeypatch, tmp_path):
    owner = {"schema_version": 1, "run_id": RUN, "pid": os.getpid() + 10000}
    running = {"stop_owner": owner, "status": "running"}
    terminal = {
        "stop_owner": owner,
        "status": "interrupted",
        "finished_at": "now",
        "guards_stopped": True,
        "local_settlement": {"state": "confirmed"},
    }
    state = {"waiting": False, "signals": [], "validations": 0, "timeout": False}
    monkeypatch.setattr(stop.sys, "platform", "linux")
    monkeypatch.setattr(
        stop.os,
        "pidfd_open",
        lambda *_: os.open(os.devnull, os.O_RDONLY),
        raising=False,
    )
    monkeypatch.setattr(
        stop, "_read_receipt", lambda *_: terminal if state["waiting"] else running
    )

    def validate(*_):
        state["validations"] += 1

    def exited(_fd, milliseconds=0):
        if milliseconds:
            state["waiting"] = True
            return not state["timeout"]
        return False

    monkeypatch.setattr(stop, "_validate_owner", validate)
    monkeypatch.setattr(stop, "_validate_process", lambda *_: None)
    monkeypatch.setattr(stop, "_exited", exited)
    monkeypatch.setattr(
        stop,
        "_authority",
        lambda _: {
            key: owner[key]
            for key in (
                "pid",
                "authority_identity",
                "authority_token",
                "authority_generation",
                "lease_token",
                "lock_directory_identity",
            )
        },
    )
    owner.update(
        authority_identity=[1, 2],
        authority_token="a" * 43,
        authority_generation="operator-recovery-required:" + "b" * 43,
        lease_token="operator-recovery-required:" + "b" * 43,
        lock_directory_identity=[1, 3],
    )
    monkeypatch.setattr(
        stop.signal,
        "pidfd_send_signal",
        lambda *args: state["signals"].append(args),
        raising=False,
    )
    return owner, running, terminal, state


def invoke_stop(tmp_path, **kwargs):
    return stop.stop_run(
        RUN, 1, _out_root=tmp_path / "runs", _lock_path=tmp_path / "lock", **kwargs
    )


def test_one_signal_after_two_owner_checks_then_verified_terminal(fake_owner, tmp_path):
    _owner, _running, _terminal, state = fake_owner
    rc, result = invoke_stop(tmp_path)
    assert rc == 0 and result["outcome"] == "terminal_local_settlement_confirmed"
    assert result["terminal_status"] == "interrupted"
    assert state["validations"] == 2
    assert len(state["signals"]) == 1 and state["signals"][0][1] == signal.SIGTERM
    assert result["recovery_fence"] == "retained"


@pytest.mark.parametrize(
    "fault",
    [
        "changed_binding",
        "running",
        "guards",
        "settlement",
        "missing_finished",
        "invalid_local",
        "unknown_status",
    ],
)
def test_exit_without_matching_terminal_proof_is_unknown(fake_owner, tmp_path, fault):
    _owner, _running, terminal, _state = fake_owner
    if fault == "changed_binding":
        terminal["stop_owner"] = {"pid": 99}
    elif fault == "running":
        terminal["status"] = "running"
    elif fault == "guards":
        terminal["guards_stopped"] = False
    elif fault == "settlement":
        terminal["local_settlement"] = {"state": "unknown"}
    elif fault == "invalid_local":
        terminal["local_settlement"] = ["confirmed"]
    elif fault == "unknown_status":
        terminal["status"] = "invented"
    else:
        del terminal["finished_at"]
    rc, result = invoke_stop(tmp_path)
    assert rc == 2 and result["outcome"] == "stop_outcome_unverified"


def test_timeout_never_escalates(fake_owner, tmp_path):
    *_, state = fake_owner
    state["timeout"] = True
    rc, result = invoke_stop(tmp_path)
    assert rc == 2 and result["owner_exited"] is False
    assert len(state["signals"]) == 1


def test_completion_racing_stop_is_reported_as_completion(fake_owner, tmp_path):
    _owner, _running, terminal, _state = fake_owner
    terminal["status"] = "complete"
    rc, result = invoke_stop(tmp_path)
    assert rc == 0 and result["outcome"] == "completed"
    assert result["signal_sent"] is True


def test_changed_ownership_refuses_before_signal(fake_owner, tmp_path, monkeypatch):
    *_, state = fake_owner

    def changed(*_):
        raise stop.StopRefused("credential-canary")

    monkeypatch.setattr(stop, "_validate_owner", changed)
    rc, result = invoke_stop(tmp_path)
    assert rc == 1 and not state["signals"]
    assert "credential-canary" not in json.dumps(result)


@pytest.mark.parametrize(
    "failure", [ProcessLookupError, PermissionError, NotImplementedError, OverflowError]
)
def test_unavailable_pidfd_never_signals(fake_owner, tmp_path, monkeypatch, failure):
    *_, state = fake_owner

    def unavailable(*_):
        raise failure

    monkeypatch.setattr(stop.os, "pidfd_open", unavailable)
    rc, result = invoke_stop(tmp_path)
    assert rc == 1 and result["signal_sent"] is False and not state["signals"]


def test_complete_receipt_with_reused_pid_is_refused(fake_owner, tmp_path, monkeypatch):
    _owner, running, _terminal, state = fake_owner
    running["status"] = "complete"

    def reused(*_):
        raise stop.StopRefused("birth differs")

    monkeypatch.setattr(stop, "_validate_process", reused)
    rc, result = invoke_stop(tmp_path)
    assert rc == 1 and result["owner_exited"] is False and not state["signals"]


@pytest.mark.parametrize(
    "run_id,wait",
    [("../other", 1), ("credential-canary", 1), (RUN, 0), (RUN, 121), (RUN, True)],
)
def test_invalid_invocations_never_signal_or_echo_unvalidated_input(
    fake_owner, run_id, wait
):
    *_, state = fake_owner
    rc, result = stop.stop_run(run_id, wait)
    assert rc == 1 and not state["signals"]
    assert "credential-canary" not in json.dumps(result)


@pytest.mark.parametrize(
    "fault",
    ["symlink", "hardlink", "oversize", "public", "legacy", "wrong_run", "fifo"],
)
def test_unsafe_receipts_are_rejected(tmp_path, fault):
    root = tmp_path / "runs"
    directory = root / RUN
    directory.mkdir(parents=True, mode=0o700)
    doc = {
        "tier": "enrich",
        "stop_owner": {
            "schema_version": 1,
            "owner_role": "guarded_tier_one_shot_cli",
            "run_id": RUN,
            "run_directory_identity": stop._identity(directory.stat()),
        },
    }
    path = directory / "receipt.json"
    checkpoint.atomic_write_text(path, json.dumps(doc))
    if fault == "symlink":
        path.rename(directory / "other")
        path.symlink_to(directory / "other")
    elif fault == "hardlink":
        os.link(path, directory / "other")
    elif fault == "oversize":
        path.write_text("x" * 17000)
    elif fault == "public":
        path.chmod(0o644)
    elif fault == "legacy":
        checkpoint.atomic_write_text(path, '{"tier":"enrich"}')
    elif fault == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    else:
        doc["stop_owner"]["run_id"] = "different"
        checkpoint.atomic_write_text(path, json.dumps(doc))
    with pytest.raises((stop.StopRefused, OSError, ValueError)):
        stop._read_receipt(root, RUN)


@LINUX
@pytest.mark.parametrize(
    "field",
    [
        "pid",
        "start_ticks",
        "boot_id",
        "uid",
        "pid_namespace_identity",
        "authority_token",
        "authority_generation",
        "lease_token",
        "authority_identity",
        "lock_directory_identity",
    ],
)
def test_stale_identity_or_authority_never_validates(tmp_path, monkeypatch, field):
    with checkpoint.SharedLock(tmp_path / "lock", recovery_required=True) as lock:
        run = tmp_path / RUN
        run.mkdir(mode=0o700)
        owner = stop.publish_stop_identity(lock, run)
        assert owner is not None
        # External validation never admits self; use a synthetic different PID
        # while retaining actual immutable identity fields for the contract.
        owner["pid"] += 10000
        actual = copy.deepcopy(owner)
        monkeypatch.setattr(
            stop,
            "_process_identity",
            lambda _: {
                key: actual[key]
                for key in (
                    "pid",
                    "start_ticks",
                    "boot_id",
                    "uid",
                    "pid_namespace_identity",
                )
            },
        )
        monkeypatch.setattr(
            stop,
            "_authority",
            lambda _: {
                key: actual[key]
                for key in (
                    "pid",
                    "authority_token",
                    "authority_generation",
                    "lease_token",
                    "authority_identity",
                    "lock_directory_identity",
                )
            },
        )
        monkeypatch.setattr(stop, "_held_flock", lambda _: None)
        if isinstance(owner[field], list):
            owner[field] = [0, 0]
        elif isinstance(owner[field], int):
            owner[field] += 1
        else:
            owner[field] = "different"
        with pytest.raises(stop.StopRefused):
            stop._validate_owner(owner, {"stop_owner": owner}, lock.path)


@LINUX
def test_actual_held_flock_is_required(tmp_path):
    with checkpoint.SharedLock(tmp_path / "lock", recovery_required=True) as lock:
        directory = tmp_path / RUN
        directory.mkdir(mode=0o700)
        owner = stop.publish_stop_identity(lock, directory)
        stop._held_flock(owner)
        other = os.open(lock.authority_path, os.O_RDONLY)
        try:
            owner["authority_fd"] = other
            with pytest.raises(stop.StopRefused):
                stop._held_flock(owner)
        finally:
            os.close(other)


@LINUX
def test_empty_pid_evidence_is_refused(tmp_path):
    with checkpoint.SharedLock(tmp_path / "lock", recovery_required=True) as lock:
        checkpoint.atomic_write_text(lock.path / "pid", "")
        with pytest.raises(stop.StopRefused):
            stop._authority(lock.path)


@LINUX
@pytest.mark.parametrize(
    "fault",
    [
        "missing_delimiter",
        "short",
        "zombie",
        "bad_ticks",
        "zero_ticks",
        "bad_uid",
        "bad_boot",
    ],
)
def test_malformed_process_birth_evidence_is_refused(monkeypatch, fault):
    pid = os.getpid()
    start = (
        "invalid" if fault == "bad_ticks" else "0" if fault == "zero_ticks" else "12345"
    )
    status = "Z" if fault == "zombie" else "S"
    raw_stat = f"{pid} (worker ) name) {status} " + "0 " * 18 + start
    if fault == "missing_delimiter":
        raw_stat = "no command delimiter"
    if fault == "short":
        raw_stat = f"{pid} (worker) S 0"
    uid = os.geteuid() + (fault == "bad_uid")
    boot = "invalid" if fault == "bad_boot" else "12345678-1234-1234-1234-123456789abc"

    def text(path, *_):
        if path.endswith("/stat"):
            return raw_stat
        if path.endswith("/status"):
            return "Uid:\t" + "\t".join([str(uid)] * 4)
        return boot

    monkeypatch.setattr(stop, "_proc_text", text)
    with pytest.raises((stop.StopRefused, ValueError)):
        stop._process_identity(pid)


@pytest.mark.parametrize(
    "platform",
    ["linux", "darwin"],
)
def test_imported_main_never_publishes_capability(monkeypatch, platform):
    calls = []
    monkeypatch.setattr(tier.sys, "platform", platform)
    monkeypatch.setattr(
        tier, "run_guarded_tier", lambda _args, **kw: calls.append(kw) or 0
    )
    assert (
        tier.main(
            ["enrich", "--source", "interra-realty", "--expected-sha", "a" * 40],
        )
        == 0
    )
    assert "_cli_stop_capability" not in calls[0]


def test_positive_settlement_survives_later_error_but_unknown_overrides_it():
    interrupted = KeyboardInterrupt()
    interrupted.cre_local_settlement = {
        "state": "confirmed",
        "groups": [],
        "errors": [],
    }
    later = OSError("synthetic log close failure")
    later.__context__ = interrupted
    assert (
        checkpoint.command_local_settlement(later) == interrupted.cre_local_settlement
    )
    later.__cause__ = checkpoint.ProcessCleanupUnknown("synthetic earlier uncertainty")
    assert checkpoint.command_local_settlement(later) is None
    assert checkpoint.command_local_settlement(KeyboardInterrupt()) is None


def test_later_global_uncertainty_overrides_previous_command_receipt(tmp_path):
    args = SimpleNamespace(
        tier="enrich",
        source="interra-realty",
        batch=10,
        deadline_seconds=30,
        expected_sha="a" * 40,
        apply=False,
        env_file=None,
    )

    def verifier(_sha, execute, _run_dir):
        execute("git_head", ["synthetic"])
        raise tier.PreflightRefused(
            "later refusal"
        ) from checkpoint.ProcessCleanupUnknown("earlier uncertainty")

    assert (
        tier.run_guarded_tier(
            args,
            _lock_path=tmp_path / "lock",
            _out_root=tmp_path / "runs",
            _runner=lambda *_a, **_kw: 0,
            _cpu_sampler=lambda: 10.0,
            _memory_sampler=lambda: 1,
            _verify_checkout=verifier,
        )
        == 1
    )
    (receipt_path,) = (tmp_path / "runs").glob("*/receipt.json")
    receipt = json.loads(receipt_path.read_text())
    assert receipt["local_settlement"]["state"] == "unknown"
    assert receipt["recovery_clear_eligible"] is False


@pytest.mark.parametrize("fault", ["log_write", "close", "observer"])
def test_real_runner_attaches_positive_settlement_before_later_failures(
    tmp_path, monkeypatch, fault
):
    checkpoint._clear_cpu_guard_trip()

    class Log:
        def write(self, text):
            if fault == "log_write" and "process settlement:" in text:
                raise OSError("synthetic settlement log failure")

        def flush(self):
            pass

        def close(self):
            if fault == "close":
                raise OSError("synthetic close failure")

    def interrupted_wait():
        raise KeyboardInterrupt

    process = SimpleNamespace(
        pid=os.getpid() + 10000, returncode=None, wait=interrupted_wait
    )
    monkeypatch.setattr(checkpoint.subprocess, "Popen", lambda *_a, **_kw: process)
    monkeypatch.setattr(
        checkpoint,
        "settle_owned_groups",
        lambda *_a, **_kw: Settlement(
            True,
            (),
            ({"pgid": process.pid, "leader_reaped": True, "group_gone": True},),
        ),
    )
    path = SimpleNamespace(parent=tmp_path, open=lambda *_a, **_kw: Log())
    with pytest.raises((KeyboardInterrupt, checkpoint.RefreshError, OSError)) as caught:
        try:
            checkpoint._run_logged_command(["synthetic"], path)
        except BaseException:
            if fault == "observer":
                raise OSError("synthetic optional observer failure") from None
            raise
    evidence = checkpoint.command_local_settlement(caught.value)
    assert evidence["state"] == "confirmed"
    assert evidence["groups"] == [
        {"pgid": process.pid, "leader_reaped": True, "group_gone": True}
    ]


OWNER_SCRIPT = r"""
import ast, sys
from pathlib import Path
import cre_checkpoint_refresh as checkpoint
import cre_guarded_tier as tier
root=Path(sys.argv[1])
path=Path(tier.__file__)
tree=ast.parse(path.read_text())
namespace={'__name__':'__main__','__file__':str(path)}
exec(compile(ast.Module(body=tree.body[:-1],type_ignores=[]),str(path),'exec'),namespace)
original=namespace['run_guarded_tier']
def run(args, **kw):
    def runner(argv, log, *, env):
        child="from pathlib import Path; import os,time; Path("+repr(str(root/'child-ready'))+").write_text(str(os.getpid())); time.sleep(60)"
        return checkpoint.run_command([sys.executable, '-c', child], log, env=env)
    return original(args, _lock_path=root/'private.lock', _out_root=root/'runs',
        _runner=runner, _cpu_sampler=lambda:10.0, _memory_sampler=lambda:1,
        _verify_checkout=lambda *_:None, _deadline_seconds=20, **kw)
namespace['run_guarded_tier']=run
sys.argv=[str(path),'enrich','--source','interra-realty','--expected-sha','a'*40]
exec(compile(ast.Module(body=[tree.body[-1]],type_ignores=[]),str(path),'exec'),namespace)
"""


@LINUX
@pytest.mark.parametrize("requesters", [1, 2])
def test_real_private_owner_cooperative_stop_and_concurrent_requests(
    tmp_path, monkeypatch, requesters
):
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        pytest.skip("pidfd APIs unavailable")
    owner = subprocess.Popen(
        [sys.executable, "-c", OWNER_SCRIPT, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / "child-ready").exists():
            if owner.poll() is not None or time.monotonic() >= deadline:
                stdout, stderr = owner.communicate(timeout=2)
                pytest.fail(f"synthetic owner did not become ready: {stdout} {stderr}")
            time.sleep(0.01)
        (run_dir,) = (tmp_path / "runs").iterdir()
        if requesters == 2:
            original = signal.pidfd_send_signal
            barrier = threading.Barrier(2)

            def simultaneous(*args):
                barrier.wait(timeout=5)
                return original(*args)

            monkeypatch.setattr(stop.signal, "pidfd_send_signal", simultaneous)
        with ThreadPoolExecutor(max_workers=requesters) as executor:
            futures = [
                executor.submit(
                    stop.stop_run,
                    run_dir.name,
                    40,
                    _out_root=tmp_path / "runs",
                    _lock_path=tmp_path / "private.lock",
                )
                for _ in range(requesters)
            ]
            results = [future.result(timeout=45) for future in futures]
        assert owner.wait(timeout=3) == 130
        assert all(
            rc == 0 and result["signal_sent"] and result["recovery_fence"] == "retained"
            for rc, result in results
        )
        receipt = json.loads((run_dir / "receipt.json").read_text())
        assert receipt["guards_stopped"] is True
        assert receipt["local_settlement"]["state"] == "confirmed"
        assert all(
            g["group_gone"] and g["leader_reaped"]
            for g in receipt["local_settlement"]["groups"]
        )
        assert receipt["recovery_clear_eligible"] is False
        with pytest.raises(checkpoint.LockHeldError):
            checkpoint.SharedLock(tmp_path / "private.lock").acquire()
    finally:
        if owner.poll() is None:
            # This test is the actual parent of this unreaped synthetic owner.
            owner.kill()
        owner.communicate(timeout=3)
