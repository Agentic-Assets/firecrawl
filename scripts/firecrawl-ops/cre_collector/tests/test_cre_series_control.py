"""Offline control contracts; no network, database, Docker or live runtime."""

import json
import os
import signal
import sys
import subprocess
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

import cre_checkpoint_series as series
import cre_series_control as control
from cre_resource_recovery import SeriesOwnershipLock

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Explicit Linux-only series controls"
)


@pytest.mark.parametrize("seconds", [0, 29, 86401, True, float("nan"), float("inf")])
def test_invalid_budget_refuses(seconds):
    with pytest.raises(control.SeriesControlError):
        control.validate_control_budget(seconds)


@pytest.mark.parametrize("seconds", [30, 600, 86400])
def test_valid_budget(seconds):
    control.validate_control_budget(seconds)


def test_unsupported_platform_refuses(monkeypatch):
    monkeypatch.setattr(control.sys, "platform", "darwin")
    with pytest.raises(control.SeriesControlError):
        control.validate_control_budget(600)


def test_cli_invalid_budget_precedes_credentials_and_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        series, "database_target_fingerprint", lambda *_a: pytest.fail("credentials")
    )
    with pytest.raises(SystemExit):
        series.main(
            ["--work-deadline-seconds", "29", "--out-root", str(tmp_path / "absent")]
        )
    assert not (tmp_path / "absent").exists()


def test_receipt_binds_existing_series_flock(tmp_path):
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        with owner.handlers():
            owner.publish_running()
            receipt = control.read_receipt(tmp_path, owner.invocation_id)
            control.validate_live_owner(tmp_path, receipt)
            assert receipt["stop_owner"]["series_lock_fd"] == lock.fileno()
            assert receipt["status"] == "running"
            assert oct((tmp_path / ".series-control").stat().st_mode & 0o777) == "0o700"


def test_receipt_publication_requires_directory_fsync(tmp_path, monkeypatch):
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        monkeypatch.setattr(
            control.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("denied"))
        )
        with owner.handlers(), pytest.raises(OSError):
            owner.publish_running()


def test_stop_latches_across_registration_without_child_signal_mask(tmp_path):
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        before = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        with owner.handlers():
            with owner.defer_stops():
                os.kill(os.getpid(), signal.SIGTERM)
                assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before
            with pytest.raises(control.SeriesWorkStopped):
                owner.check()


def test_existing_timer_refused_without_overwrite(tmp_path):
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        signal.setitimer(signal.ITIMER_REAL, 100)
        try:
            with pytest.raises(control.SeriesControlError), owner.handlers():
                pass
            assert signal.getitimer(signal.ITIMER_REAL)[0] > 90
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)


def args(seconds=600):
    return SimpleNamespace(
        work_deadline_seconds=seconds,
        env_file=None,
        retry_failed=False,
        expected_sha=None,
    )


def manifest_at(path):
    cfg = series.series_config(
        sources=("cbre", "jll"),
        page_cap=400,
        concurrency=1,
        attempts_per_source=1,
        max_resume_age_hours=24,
        max_host_cpu_percent=90,
        cpu_sustain_seconds=30,
        cpu_sample_seconds=2,
        nice=15,
    )
    return series.new_manifest(path, git_sha="a" * 40, config=cfg)


def test_library_main_cannot_publish_control_capability(tmp_path, monkeypatch):
    monkeypatch.setattr(
        series, "database_target_fingerprint", lambda *_a: pytest.fail("credentials")
    )
    with pytest.raises(series.SeriesError, match="one-shot CLI"):
        series.main(
            ["--work-deadline-seconds", "600", "--out-root", str(tmp_path / "absent")]
        )
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize(
    "cause,expected", [(signal.SIGALRM, 124), (signal.SIGTERM, 130)]
)
def test_control_between_sources_has_typed_terminal_receipt(
    tmp_path, monkeypatch, cause, expected, cli_role
):
    manifest = manifest_at(tmp_path)

    def work(_path, current, **kwargs):
        current["sources"]["cbre"]["state"] = "complete"
        os.kill(os.getpid(), cause)
        pytest.fail("no next dispatch")

    monkeypatch.setattr(series, "run_series", work)
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        rc = series.run_controlled_series(
            tmp_path, manifest, lock, args(), _cli_control_capability=cli_role
        )
    assert rc == expected
    assert manifest["sources"]["jll"]["state"] == "pending"
    receipt = control.read_receipt(
        tmp_path, manifest["latest_invocation_control"]["invocation_id"]
    )
    assert receipt["status"] == (
        "deadline_interrupted" if expected == 124 else "interrupted"
    )
    assert receipt["local_settlement"] == "not_started"
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)


def test_deadline_during_cooldown_preserves_typed_cause(
    tmp_path, monkeypatch, cli_role
):
    manifest = manifest_at(tmp_path)
    cfg = series.RecoveryConfig(max_recoveries_per_source=1)
    # Exercise the real cooldown catch; synthetic evidence only.
    monkeypatch.setattr(
        series, "validate_resource_recovery_admission", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(series, "_start_or_continue_cooldown", lambda *_a, **_k: {})

    def expire(*_a, **_k):
        os.kill(os.getpid(), signal.SIGALRM)

    monkeypatch.setattr(series, "wait_for_cpu_recovery", expire)
    manifest["config"]["resource_recovery"] = cfg.as_dict()

    def work(_path, current, **_kwargs):
        series._cool_down_for_child_resume(
            tmp_path,
            current,
            source="cbre",
            child_manifest_path=tmp_path / "runs" / "placeholder" / "manifest.json",
            child_manifest={},
            env_file=None,
        )

    monkeypatch.setattr(series, "run_series", work)
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        rc = series.run_controlled_series(
            tmp_path, manifest, lock, args(), _cli_control_capability=cli_role
        )
    assert rc == 124
    assert manifest["error"] == "series work deadline expired"


def test_terminal_manifest_failure_still_cleans_owned_child(
    tmp_path, monkeypatch, cli_role
):
    manifest = manifest_at(tmp_path)
    seen = []

    def work(_p, _m, **kwargs):
        owner = kwargs["control"]
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        owner.register_group(control.OwnedProcessGroup.from_spawn(process))
        seen.append(process)
        raise control.SeriesWorkStopped

    original = series.save_manifest
    writes = 0

    def fail_terminal(*a):
        nonlocal writes
        writes += 1
        if writes > 1:
            raise OSError("disk unavailable")
        original(*a)

    monkeypatch.setattr(series, "run_series", work)
    monkeypatch.setattr(series, "save_manifest", fail_terminal)
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock, pytest.raises(OSError):
        series.run_controlled_series(
            tmp_path, manifest, lock, args(), _cli_control_capability=cli_role
        )
    assert seen[0].returncode is not None
    receipt = control.read_receipt(
        tmp_path, manifest["latest_invocation_control"]["invocation_id"]
    )
    assert receipt["status"] == "running"


def test_checkpoint_hook_is_private_bound_and_strict(tmp_path):
    invocation = "a" * 32
    current = {"collector_git_sha": "b" * 40}
    control.publish_checkpoint_settlement(
        tmp_path,
        invocation,
        current,
        child_invocation_id=invocation,
        guards_stopped=True,
        nested_cleanup_confirmed=True,
        terminal_evidence_saved=True,
    )
    raw = json.loads(
        (tmp_path / ".series-control" / (invocation + ".json")).read_text()
    )
    assert raw["pid"] == os.getpid()
    assert raw["run_id"] == tmp_path.name
    assert raw["invocation_id"] == invocation
    assert all(
        raw[k] is True
        for k in (
            "guards_stopped",
            "nested_cleanup_confirmed",
            "terminal_evidence_saved",
        )
    )


@pytest.mark.parametrize(
    "field",
    [
        "pid",
        "run_id",
        "collector_git_sha",
        "invocation_id",
        "guards_stopped",
        "nested_cleanup_confirmed",
        "terminal_evidence_saved",
    ],
)
def test_checkpoint_settlement_rejects_drift_or_missing_proof(tmp_path, field):
    child = tmp_path / "run"
    child.mkdir()
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        owner.prepare_checkpoint(child, "a" * 40)
        owner.checkpoint_pid = os.getpid()
        owner.child_unverified = True
        control.publish_checkpoint_settlement(
            child,
            owner.invocation_id,
            {"collector_git_sha": "a" * 40},
            child_invocation_id=owner.child_invocation_id,
            guards_stopped=True,
            nested_cleanup_confirmed=True,
            terminal_evidence_saved=True,
        )
        path = child / ".series-control" / (owner.child_invocation_id + ".json")
        payload = json.loads(path.read_text())
        payload[field] = False
        path.write_text(json.dumps(payload))
        with pytest.raises(control.SeriesControlError):
            owner.verify_checkpoint()
        assert owner.child_unverified


def test_real_child_mask_and_registration_stop(tmp_path, monkeypatch):
    child_run = tmp_path / "child"
    child_run.mkdir()
    script = tmp_path / "child.py"
    script.write_text("""import json, os, signal, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from cre_series_control import publish_checkpoint_settlement
run, invocation, child_id = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
(run / 'mask.json').write_text(json.dumps([int(v) for v in blocked]))
def stopped(*_a):
    publish_checkpoint_settlement(run, invocation, {'collector_git_sha': 'a' * 40}, child_invocation_id=child_id, guards_stopped=True, nested_cleanup_confirmed=True, terminal_evidence_saved=True)
    raise SystemExit(130)
signal.signal(signal.SIGTERM, stopped)
(run / 'ready').write_text('ready')
while True: time.sleep(0.01)
""")
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        owner.prepare_checkpoint(child_run, "a" * 40)

        def spawn_then_stop(*a, **kw):
            process = subprocess.Popen(*a, **kw)
            # Termination lands after Popen returns but before registration.
            deadline = time.monotonic() + 5
            while not (child_run / "ready").exists():
                if time.monotonic() >= deadline:
                    raise AssertionError("synthetic child failed to start")
                time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGTERM)
            return process

        with owner.handlers():
            owner.publish_running()
            process = owner.spawn(
                spawn_then_stop,
                [
                    sys.executable,
                    str(script),
                    str(Path(series.__file__).parent),
                    str(child_run),
                    owner.invocation_id,
                    owner.child_invocation_id,
                ],
                start_new_session=True,
            )
            with pytest.raises(control.SeriesWorkStopped):
                owner.check()
            owner.settle_active()
            assert owner.finish("interrupted")
        assert process.returncode == 130
        blocked = json.loads((child_run / "mask.json").read_text())
        assert all(
            int(sig) not in blocked
            for sig in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)
        )
        assert owner.receipt["local_settlement"] == "confirmed"


def test_unknown_registration_never_signals_unverified_group(tmp_path, monkeypatch):
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        monkeypatch.setattr(
            control.OwnedProcessGroup,
            "from_spawn",
            lambda _p: (_ for _ in ()).throw(ValueError("unknown")),
        )
        monkeypatch.setattr(
            control,
            "settle_owned_groups",
            lambda *_a, **_kw: pytest.fail("no numeric fallback"),
        )
        with owner.handlers():
            owner.publish_running()
            with pytest.raises(ValueError):
                owner.spawn(lambda: SimpleNamespace(pid=1234))
            assert owner.finish("failed") is False
        assert owner.receipt["local_settlement"] == "unknown"


def test_reaped_leader_with_live_group_is_unknown_and_never_signalled(
    tmp_path, monkeypatch
):
    import cre_owned_processes as owned

    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        process = SimpleNamespace(pid=1234, returncode=0, wait=lambda timeout: 0)
        group = control.OwnedProcessGroup.from_spawn(process)
        process._cre_owned_group = group
        owner.register_group(group)
        monkeypatch.setattr(owned, "_group_alive", lambda _pg: True)
        monkeypatch.setattr(
            owned.os,
            "killpg",
            lambda *_a: pytest.fail("reaped group must not be signalled"),
        )
        with pytest.raises(control.SeriesControlError, match="cleanup_unknown"):
            owner.observe_exit(process)
        assert owner.cleanup_unknown


@pytest.fixture
def cli_role(monkeypatch):
    token = object()
    monkeypatch.setattr(series, "__name__", "__main__")
    monkeypatch.setattr(series, "_CLI_CONTROL_CAPABILITY", token)
    return token


@pytest.fixture(autouse=True)
def bounded_synthetic_cleanup(monkeypatch):
    original = control.settle_owned_groups

    def bounded(groups, **kwargs):
        return original(
            groups,
            **{
                **kwargs,
                "grace_seconds": min(kwargs.get("grace_seconds", 0), 0.2),
                "kill_seconds": 1,
            },
        )

    monkeypatch.setattr(control, "settle_owned_groups", bounded)


@pytest.mark.parametrize("fault", ["positive_directory_fsync", "pending_unlink"])
def test_post_rename_fault_leaves_reader_refusal(tmp_path, monkeypatch, fault):
    directory = tmp_path / ".series-control"
    directory.mkdir(mode=0o700)
    private = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    original_fsync, original_unlink = os.fsync, os.unlink
    count = 0

    def fsync(fd):
        nonlocal count
        count += 1
        if fault == "positive_directory_fsync" and count == 4:
            raise OSError("post-rename durability failed")
        original_fsync(fd)

    def unlink(name, **kwargs):
        if fault == "pending_unlink" and str(name).endswith(".pending"):
            raise OSError("pending witness cannot be removed")
        original_unlink(name, **kwargs)

    monkeypatch.setattr(control.os, "fsync", fsync)
    monkeypatch.setattr(control.os, "unlink", unlink)
    try:
        with pytest.raises(OSError):
            control._write_private_receipt(
                private,
                "receipt.json",
                {"status": "complete", "local_settlement": "confirmed"},
            )
        assert (
            json.loads((directory / "receipt.json").read_text())["status"] == "complete"
        )
        with pytest.raises(control.SeriesControlError, match="incomplete"):
            control._read_private_receipt(private, "receipt.json")
        with pytest.raises(FileExistsError):
            control._write_private_receipt(
                private, "receipt.json", {"status": "complete"}
            )
    finally:
        os.close(private)


def test_replaced_series_lock_and_process_birth_refuse(tmp_path):
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        with owner.handlers():
            owner.publish_running()
            payload = control.read_receipt(tmp_path, owner.invocation_id)
            payload["stop_owner"]["start_ticks"] += 1
            with pytest.raises(control.SeriesControlError, match="birth"):
                control.validate_live_owner(tmp_path, payload)
            payload = control.read_receipt(tmp_path, owner.invocation_id)
            (tmp_path / ".series.lock").rename(tmp_path / "old-lock")
            (tmp_path / ".series.lock").write_text(f"{os.getpid()}\n")
            (tmp_path / ".series.lock").chmod(0o600)
            with pytest.raises(control.SeriesControlError, match="lock"):
                control.validate_live_owner(tmp_path, payload)


def test_symlinked_control_directory_refuses(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (tmp_path / ".series-control").symlink_to(target, target_is_directory=True)
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        with owner.handlers(), pytest.raises(OSError):
            owner.publish_running()


def test_local_git_registration_latches_stop_and_cleans(tmp_path, monkeypatch):
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        original = series.subprocess.Popen
        processes = []

        def start_then_stop(*a, **kwargs):
            process = original(
                [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
            )
            processes.append(process)
            os.kill(os.getpid(), signal.SIGTERM)
            return process

        monkeypatch.setattr(series.subprocess, "Popen", start_then_stop)
        with owner.handlers():
            owner.publish_running()
            with pytest.raises(control.SeriesWorkStopped):
                series.local_git_output(["rev-parse", "HEAD"], control=owner)
            assert processes[0].returncode is not None
            assert owner.group is None
            assert owner.finish("interrupted")


def test_one_shot_pidfd_stop_real_private_owner(tmp_path):
    script = tmp_path / "owner.py"
    script.write_text("""import signal, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from cre_resource_recovery import SeriesOwnershipLock
from cre_series_control import SeriesControl, SeriesWorkStopped
root=Path(sys.argv[2])
with SeriesOwnershipLock(root/'.series.lock') as lock:
    owner=SeriesControl(root, lock, 600)
    with owner.handlers():
        try:
            owner.publish_running()
            # The parent may stop immediately after the flushed readiness hint.
            # Keep that post-publication interval inside the real signal handler.
            time.sleep(0.1)
            while True: time.sleep(0.01)
        except SeriesWorkStopped:
            owner.finish('interrupted')
""")
    process = subprocess.Popen(
        [sys.executable, str(script), str(Path(series.__file__).parent), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        import selectors

        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(5), "synthetic owner did not publish"
        hint = json.loads(process.stdout.readline())
        rc, outcome = control.stop_series(tmp_path, hint["invocation_id"], 5)
        assert rc == 0
        assert outcome["signal_sent"] is True
        assert outcome["owner_exited"] is True
        assert outcome["local_settlement"] == "not_started"
        assert outcome["terminal_status"] == "interrupted"
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


@pytest.fixture
def fake_stop(monkeypatch):
    owner = {"pid": 424242, "invocation_id": "a" * 32}
    running = {"stop_owner": owner, "status": "running"}
    terminal = {
        "stop_owner": owner,
        "status": "interrupted",
        "local_settlement": "confirmed",
        "finished_unix_ms": 1,
    }
    reads = [running, running, running, terminal]

    def read(*_a):
        return reads.pop(0) if len(reads) > 1 else reads[0]

    monkeypatch.setattr(control, "read_receipt", read)
    monkeypatch.setattr(control, "validate_live_owner", lambda *_a: None)
    monkeypatch.setattr(control.os, "pidfd_open", lambda *_a: 987654)
    original_close = os.close
    monkeypatch.setattr(
        control.os, "close", lambda fd: None if fd == 987654 else original_close(fd)
    )
    states = iter([False, True])
    monkeypatch.setattr(control._evidence(), "_exited", lambda *_a: next(states))
    signals = []
    monkeypatch.setattr(
        control.signal, "pidfd_send_signal", lambda *a: signals.append(a)
    )
    return reads, terminal, signals


@pytest.mark.parametrize(
    "status",
    [
        "complete",
        "complete_with_source_failures",
        "failed",
        "deadline_interrupted",
        "interrupted",
    ],
)
def test_pidfd_stop_requires_matching_terminal_proof(fake_stop, status):
    _reads, terminal, signals = fake_stop
    terminal["status"] = status
    rc, result = control.stop_series("/private/series", "a" * 32, 1)
    assert rc == 0
    assert signals == [(987654, signal.SIGTERM, None, 0)]
    assert result["outcome"] == (
        "completed"
        if status.startswith("complete")
        else "terminal_local_settlement_confirmed"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("local_settlement", "unknown"),
        ("finished_unix_ms", None),
        ("finished_unix_ms", True),
        ("status", "cleanup_unknown"),
        ("stop_owner", {"pid": 424243}),
    ],
)
def test_pidfd_stop_never_certifies_missing_or_drifted_proof(fake_stop, field, value):
    _reads, terminal, signals = fake_stop
    terminal[field] = value
    rc, result = control.stop_series("/private/series", "a" * 32, 1)
    assert rc == 2
    assert result["outcome"] == "stop_outcome_unverified"
    assert len(signals) == 1


def test_natural_completion_race_does_not_claim_signal(fake_stop, monkeypatch):
    reads, terminal, signals = fake_stop
    terminal["status"] = "complete"
    reads[:] = [terminal, terminal, terminal]
    monkeypatch.setattr(control._evidence(), "_exited", lambda *_a: True)
    rc, result = control.stop_series("/private/series", "a" * 32, 1)
    assert rc == 0 and result["outcome"] == "completed"
    assert result["signal_sent"] is False and not signals


def test_pidfd_timeout_is_unverified(fake_stop, monkeypatch):
    monkeypatch.setattr(control._evidence(), "_exited", lambda *_a: False)
    rc, result = control.stop_series("/private/series", "a" * 32, 1)
    assert rc == 2 and result["outcome"] == "stop_outcome_unverified"
    assert result["owner_exited"] is False


def test_report_failure_cannot_leave_complete_receipt(tmp_path, monkeypatch, cli_role):
    manifest = manifest_at(tmp_path)

    def work(_p, m, **_k):
        m["status"] = "complete"
        return 0

    monkeypatch.setattr(series, "run_series", work)
    monkeypatch.setattr(
        series, "save_report", lambda *_a: (_ for _ in ()).throw(OSError("failed"))
    )
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock, pytest.raises(OSError):
        series.run_controlled_series(
            tmp_path, manifest, lock, args(), _cli_control_capability=cli_role
        )
    receipt = control.read_receipt(
        tmp_path, manifest["latest_invocation_control"]["invocation_id"]
    )
    assert receipt["status"] == "running"


@pytest.mark.parametrize("unknown", [False, True])
def test_real_checkpoint_final_hook_follows_nested_cleanup(
    tmp_path, monkeypatch, unknown, checkpoint_main
):
    refresh = checkpoint_main
    parent_id, child_id = "a" * 32, "b" * 32

    def interrupt(*_a, **_k):
        if unknown:
            raise KeyboardInterrupt from refresh.ProcessCleanupUnknown(
                "synthetic unknown"
            )
        raise KeyboardInterrupt

    monkeypatch.setattr(refresh, "run_command", interrupt)
    rc = refresh.main(
        [
            "--sources",
            "svn",
            "--out-root",
            str(tmp_path / "runs"),
            "--series-control-invocation-id",
            parent_id,
            "--series-control-child-id",
            child_id,
        ]
    )
    assert rc == 130
    payload = json.loads(
        next(
            (tmp_path / "runs").glob("*/.series-control/" + child_id + ".json")
        ).read_text()
    )
    assert payload["guards_stopped"] is True
    assert payload["terminal_evidence_saved"] is True
    assert payload["nested_cleanup_confirmed"] is (not unknown)


def test_existing_checkpoint_default_creates_no_control_output(
    tmp_path, monkeypatch, checkpoint_main
):
    refresh = checkpoint_main
    monkeypatch.setattr(
        refresh,
        "run_command",
        lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    assert (
        refresh.main(["--sources", "svn", "--out-root", str(tmp_path / "runs")]) == 130
    )
    assert not list((tmp_path / "runs").glob("*/.series-control"))


def test_checkpoint_late_unknown_chain_cannot_publish_positive(
    tmp_path, monkeypatch, checkpoint_main
):
    refresh = checkpoint_main
    monkeypatch.setattr(
        refresh,
        "run_command",
        lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    def late(*_a):
        raise RuntimeError("late reporting error") from refresh.ProcessCleanupUnknown(
            "nested unknown"
        )

    monkeypatch.setattr(refresh, "save_report", late)
    with pytest.raises(RuntimeError, match="late"):
        refresh.main(
            [
                "--sources",
                "svn",
                "--out-root",
                str(tmp_path / "runs"),
                "--series-control-invocation-id",
                "a" * 32,
                "--series-control-child-id",
                "b" * 32,
            ]
        )
    receipts = list((tmp_path / "runs").glob("*/.series-control/" + "b" * 32 + ".json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["nested_cleanup_confirmed"] is False


def test_actual_short_timer_restores_handlers_and_discards_pending_alarm(tmp_path):
    before = {
        s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)
    }
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        with owner.handlers():
            signal.setitimer(signal.ITIMER_REAL, 0.01)
            with pytest.raises(control.SeriesWorkStopped):
                time.sleep(1)
            assert owner.stop_reason == "deadline"
            assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)
    assert {s: signal.getsignal(s) for s in before} == before
    assert signal.SIGALRM not in signal.sigpending()


def test_real_series_child_wait_deadline_stops_before_next_dispatch(
    tmp_path, monkeypatch, cli_role
):
    manifest = manifest_at(tmp_path)
    script = tmp_path / "checkpoint-fixture.py"
    script.write_text("""import argparse, signal, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from cre_series_control import publish_checkpoint_settlement
p=argparse.ArgumentParser()
p.add_argument('--run-dir');p.add_argument('--series-control-invocation-id');p.add_argument('--series-control-child-id')
a=p.parse_args(sys.argv[2:]);root=Path(a.run_dir);root.mkdir(parents=True)
def stopped(*_a):
    publish_checkpoint_settlement(root,a.series_control_invocation_id,{'collector_git_sha':'a'*40},child_invocation_id=a.series_control_child_id,guards_stopped=True,nested_cleanup_confirmed=True,terminal_evidence_saved=True)
    raise SystemExit(130)
signal.signal(signal.SIGTERM,stopped)
(root/'ready').write_text('ready')
while True:time.sleep(0.01)
""")
    dispatched = []

    def argv(source, **kwargs):
        dispatched.append(source)
        run = kwargs["child_out_root"] / kwargs["fresh_run_id"]
        return [
            sys.executable,
            str(script),
            str(Path(series.__file__).parent),
            "--run-dir",
            str(run),
        ]

    monkeypatch.setattr(series, "build_checkpoint_argv", argv)
    original_spawn = control.SeriesControl.spawn

    def spawn_then_deadline(owner, factory, *a, **kw):
        def start(*child_a, **child_kw):
            process = factory(*child_a, **child_kw)
            deadline = time.monotonic() + 5
            while not (owner.checkpoint_run / "ready").exists():
                if time.monotonic() > deadline:
                    raise AssertionError("synthetic checkpoint not ready")
                time.sleep(0.01)
            os.kill(os.getpid(), signal.SIGALRM)
            return process

        return original_spawn(owner, start, *a, **kw)

    monkeypatch.setattr(control.SeriesControl, "spawn", spawn_then_deadline)
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        rc = series.run_controlled_series(
            tmp_path, manifest, lock, args(), _cli_control_capability=cli_role
        )
    assert rc == 124
    assert dispatched == ["cbre"]
    assert manifest["sources"]["jll"]["state"] == "pending"
    receipt = control.read_receipt(
        tmp_path, manifest["latest_invocation_control"]["invocation_id"]
    )
    assert receipt["status"] == "deadline_interrupted"
    assert receipt["local_settlement"] == "confirmed"


def test_recovery_child_attempt_nonce_rejects_old_positive_receipt(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        owner.prepare_checkpoint(child, "a" * 40)
        old_id = owner.child_invocation_id
        owner.checkpoint_pid = 12345
        control.publish_checkpoint_settlement(
            child,
            owner.invocation_id,
            {"collector_git_sha": "a" * 40},
            child_invocation_id=old_id,
            guards_stopped=True,
            nested_cleanup_confirmed=True,
            terminal_evidence_saved=True,
        )
        owner.prepare_checkpoint(child, "a" * 40)
        assert owner.child_invocation_id != old_id
        assert owner.checkpoint_pid is None
        owner.checkpoint_pid = os.getpid()
        owner.child_unverified = True
        with pytest.raises(FileNotFoundError):
            owner.verify_checkpoint()
        assert owner.child_unverified


@pytest.fixture
def checkpoint_main(tmp_path, monkeypatch):
    import cre_checkpoint_refresh as refresh

    monkeypatch.setattr(
        refresh,
        "database_target_fingerprint",
        lambda _a: {"algorithm": "sha256", "value": "c" * 64},
    )
    monkeypatch.setattr(refresh, "git_identity", lambda: ("d" * 40, False))
    monkeypatch.setattr(
        refresh, "checkpoint_lock_dir", lambda _a: tmp_path / ".cre.lock"
    )
    monkeypatch.setattr(refresh, "run_cpu_guard_preflight", lambda _a: 1)
    monkeypatch.setattr(refresh.HostCpuGuard, "start", lambda _a: None)
    monkeypatch.setattr(refresh.HostCpuGuard, "stop", lambda _a: None)
    monkeypatch.setattr(refresh.HostMemoryGuard, "start", lambda _a: None)
    monkeypatch.setattr(refresh.HostMemoryGuard, "stop", lambda _a: None)
    return refresh


def test_pending_created_during_read_is_refused(tmp_path, monkeypatch):
    directory = tmp_path / ".series-control"
    directory.mkdir(mode=0o700)
    (directory / "receipt.json").write_text("{}")
    (directory / "receipt.json").chmod(0o600)
    private = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    original = control._evidence()._read_at

    def read_then_mark(*a, **k):
        result = original(*a, **k)
        (directory / "receipt.json.pending").touch(mode=0o600)
        return result

    monkeypatch.setattr(control._evidence(), "_read_at", read_then_mark)
    try:
        with pytest.raises(control.SeriesControlError, match="incomplete"):
            control._read_private_receipt(private, "receipt.json")
    finally:
        os.close(private)


def test_direct_controlled_runner_requires_exact_cli_capability(tmp_path):
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        with pytest.raises(series.SeriesError, match="one-shot CLI"):
            series.run_controlled_series(
                tmp_path,
                manifest_at(tmp_path),
                lock,
                args(),
                _cli_control_capability=object(),
            )
    assert not (tmp_path / ".series-control").exists()


def test_checkpoint_ownership_exit_fault_never_publishes_positive(
    tmp_path, monkeypatch, checkpoint_main
):
    refresh = checkpoint_main
    monkeypatch.setattr(
        refresh,
        "run_command",
        lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    original = refresh.SharedLock.__exit__

    def failed_release(self, *a):
        original(self, *a)
        raise refresh.ProcessCleanupUnknown("release unknown")

    monkeypatch.setattr(refresh.SharedLock, "__exit__", failed_release)
    with pytest.raises(refresh.ProcessCleanupUnknown):
        refresh.main(
            [
                "--sources",
                "svn",
                "--out-root",
                str(tmp_path / "runs"),
                "--series-control-invocation-id",
                "a" * 32,
                "--series-control-child-id",
                "b" * 32,
            ]
        )
    assert not list((tmp_path / "runs").glob("*/.series-control/" + "b" * 32 + ".json"))


@pytest.mark.parametrize("wait", [0, 121, True, "60"])
def test_stop_invalid_wait_never_opens_owner(tmp_path, monkeypatch, wait):
    monkeypatch.setattr(
        control.os, "pidfd_open", lambda *_a: pytest.fail("no owner open")
    )
    rc, result = control.stop_series(tmp_path, "a" * 32, wait)
    assert rc == 1 and not result["signal_sent"]


def test_blocked_control_signal_refuses_without_changing_mask(tmp_path):
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            with pytest.raises(control.SeriesControlError), owner.handlers():
                pass
            assert signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, set())
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def test_first_control_cause_survives_repeated_stops(tmp_path):
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        owner = control.SeriesControl(tmp_path, lock, 600)
        with owner.handlers():
            owner.publish_running()
            with owner.defer_stops():
                os.kill(os.getpid(), signal.SIGALRM)
                os.kill(os.getpid(), signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGINT)
            assert owner.stop_reason == "deadline"
            assert owner.finish("deadline_interrupted")
        assert owner.receipt["status"] == "deadline_interrupted"


def test_controlled_resume_replaces_budget_without_changing_config(
    tmp_path, monkeypatch, cli_role
):
    manifest = manifest_at(tmp_path)
    before = json.dumps(manifest["config"], sort_keys=True)

    def work(_p, current, **_k):
        current["status"] = "complete"
        return 0

    monkeypatch.setattr(series, "run_series", work)
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        assert (
            series.run_controlled_series(
                tmp_path, manifest, lock, args(600), _cli_control_capability=cli_role
            )
            == 0
        )
        first_id = manifest["latest_invocation_control"]["invocation_id"]
        assert (
            series.run_controlled_series(
                tmp_path, manifest, lock, args(1800), _cli_control_capability=cli_role
            )
            == 0
        )
        second = manifest["latest_invocation_control"]
    assert (
        second["invocation_id"] != first_id and second["work_deadline_seconds"] == 1800
    )
    assert json.dumps(manifest["config"], sort_keys=True) == before


def test_typed_stop_retains_unknown_causal_chain(tmp_path, monkeypatch, cli_role):
    manifest = manifest_at(tmp_path)

    def work(_p, _m, **kwargs):
        kwargs["control"].stop_reason = "deadline"
        raise control.SeriesWorkStopped from series.ProcessCleanupUnknown(
            "nested unknown"
        )

    monkeypatch.setattr(series, "run_series", work)
    with SeriesOwnershipLock(tmp_path / ".series.lock") as lock:
        rc = series.run_controlled_series(
            tmp_path, manifest, lock, args(), _cli_control_capability=cli_role
        )
    assert rc == 1
    receipt = control.read_receipt(
        tmp_path, manifest["latest_invocation_control"]["invocation_id"]
    )
    assert receipt["status"] == "cleanup_unknown"
    assert receipt["local_settlement"] == "unknown"
