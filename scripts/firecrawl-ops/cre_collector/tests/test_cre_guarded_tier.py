"""Synthetic/private-lock contracts; never invoke a collector or database."""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import cre_checkpoint_refresh as checkpoint
import cre_guarded_tier as tier
import pytest


def options(**changes):
    values = dict(
        tier="enrich",
        source="interra-realty",
        batch=10,
        deadline_seconds=30,
        expected_sha="a" * 40,
        apply=False,
        env_file=None,
    )
    values.update(changes)
    return argparse.Namespace(**values)


def execute(args, tmp_path, **changes):
    seams = dict(
        _lock_path=tmp_path / "private.lock",
        _out_root=tmp_path / "runs",
        _cpu_sampler=lambda: 10.0,
        _memory_sampler=lambda: 1,
        _verify_checkout=lambda *_args: None,
    )
    seams.update(changes)
    return tier.run_guarded_tier(args, **seams)


def receipt(tmp_path):
    files = list((tmp_path / "runs").glob("*/receipt.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text())


def assert_fenced(tmp_path):
    with pytest.raises(checkpoint.LockHeldError):
        checkpoint.SharedLock(tmp_path / "private.lock").acquire()


def assert_released(tmp_path):
    with checkpoint.SharedLock(tmp_path / "private.lock"):
        pass


def monitor_artifact():
    return {
        "runMeta": {"mode": "monitor", "transactions": ["sale", "lease"]},
        "sources": [
            {
                "sourceKey": "interra-realty",
                "transaction": tx,
                "supported": True,
                "truncated": False,
                "listingsCollected": 1,
            }
            for tx in ("sale", "lease")
        ],
        "listings": [
            {
                "sourceKey": "interra-realty",
                "transactionMode": tx,
                "url": "https://example.test/" + tx,
            }
            for tx in ("sale", "lease")
        ],
        "totalListings": 2,
    }


@pytest.mark.parametrize(
    "fault",
    [
        "error",
        "truncated",
        "wrong_source",
        "missing_pass",
        "duplicate_pass",
        "unsupported",
        "missing_count",
        "count_mismatch",
        "wrong_mode",
        "wrong_listing_source",
        "wrong_listing_transaction",
        "total_mismatch",
    ],
)
@pytest.mark.parametrize("apply", [False, True])
def test_monitor_partial_artifact_never_reaches_transform_or_completion(
    tmp_path, monkeypatch, fault, apply
):
    monkeypatch.setattr(
        checkpoint, "database_target_fingerprint", lambda _: {"value": "b" * 64}
    )
    data = monitor_artifact()
    if fault == "error":
        data["sources"][1]["error"] = "secret timeout"
    elif fault == "truncated":
        data["sources"][1]["truncated"] = True
    elif fault == "wrong_source":
        data["sources"][1]["sourceKey"] = "hanley"
    elif fault == "missing_pass":
        data["sources"].pop()
    elif fault == "duplicate_pass":
        data["sources"][1]["transaction"] = "sale"
    elif fault == "unsupported":
        data["sources"][1]["supported"] = False
    elif fault == "missing_count":
        del data["sources"][1]["listingsCollected"]
    elif fault == "count_mismatch":
        data["sources"][1]["listingsCollected"] = 2
    elif fault == "wrong_mode":
        data["runMeta"]["mode"] = "full"
    elif fault == "wrong_listing_source":
        data["listings"][0]["sourceKey"] = "hanley"
    elif fault == "wrong_listing_transaction":
        data["listings"][0]["transactionMode"] = "both"
    elif fault == "total_mismatch":
        data["totalListings"] = 1
    calls = []

    def runner(_argv, log, *, env):
        calls.append(log.stem)
        (log.parent.parent / "monitor.json").write_text(json.dumps(data))
        return 0

    assert execute(options(tier="monitor", apply=apply), tmp_path, _runner=runner) == 1
    assert calls == ["collect"]
    assert receipt(tmp_path)["status"] == "failed_recovery_required"
    assert_fenced(tmp_path)


@pytest.mark.parametrize("empty", [False, True])
def test_complete_monitor_artifact_reaches_database_free_transform(tmp_path, empty):
    data = monitor_artifact()
    if empty:
        data["listings"] = []
        data["totalListings"] = 0
        for entry in data["sources"]:
            entry["listingsCollected"] = 0
    calls = []

    def runner(argv, log, *, env):
        calls.append(argv)
        if log.stem == "collect":
            (log.parent.parent / "monitor.json").write_text(json.dumps(data))
        return 0

    assert execute(options(tier="monitor"), tmp_path, _runner=runner) == 0
    assert len(calls) == 2 and "--dry-run" in calls[1]
    assert_released(tmp_path)


@pytest.mark.parametrize(
    "changes",
    [
        {"tier": "weekly"},
        {"tier": "daily"},
        {"source": "all"},
        {"source": "interra-realty,cbre"},
        {"batch": 0},
        {"batch": 51},
        {"deadline_seconds": 29},
        {"deadline_seconds": 14401},
        {"expected_sha": "abc"},
        {"env_file": "secret\npath"},
        *[
            {"tier": "monitor", "source": source, "apply": apply}
            for source in tier.MONITOR_EXCLUDED
            for apply in (False, True)
        ],
    ],
)
def test_invalid_policy_never_acquires_or_dispatches(tmp_path, changes):
    with pytest.raises(tier.GuardedTierError):
        execute(
            options(**changes),
            tmp_path,
            _runner=lambda *_a, **_kw: pytest.fail("unexpected dispatch"),
        )
    assert not list(tmp_path.iterdir())


def test_monitor_commands_remain_inventory_and_database_free(tmp_path):
    commands = tier.build_commands(options(tier="monitor"), tmp_path, None)
    collect, monitor = [argv for _phase, argv in commands]
    assert collect[:4] == ["npx", "--no-install", "tsx", "collect.ts"]
    assert "--source=interra-realty" in collect
    assert "--monitor" in collect and "--concurrency=1" in collect
    assert "--dry-run" in monitor and "--apply" not in monitor
    assert "cre_monitor.py" in monitor[1]
    assert not any(
        "cre_ingest.py" in arg for argv in (collect, monitor) for arg in argv
    )


@pytest.mark.parametrize(
    "source", sorted(set(tier.SOURCE_TO_BROKERAGE) - tier.MONITOR_EXCLUDED)
)
def test_monitor_apply_argv_is_exact_source_bound_and_preserves_transaction(
    tmp_path, source
):
    target = "b" * 64
    commands = tier.build_commands(
        options(tier="monitor", source=source, apply=True, env_file="/private/cre.env"),
        tmp_path,
        target,
    )
    assert [phase for phase, _ in commands] == ["collect", "monitor_apply"]
    collect, monitor = [argv for _, argv in commands]
    assert f"--source={source}" in collect and "--monitor" in collect
    assert "--apply" in monitor and "--dry-run" not in monitor
    assert monitor[monitor.index("--expected-db-target-sha256") + 1] == target
    assert monitor[monitor.index("--env-file") + 1] == "/private/cre.env"
    assert not set(monitor).intersection(
        {
            "--force-disappear",
            "--mark-missing",
            "--activate-status",
            "--update-baseline",
        }
    )


@pytest.mark.parametrize("target", [None, "invalid", "A" * 64])
def test_monitor_apply_refuses_unbound_commands(tmp_path, target):
    with pytest.raises(tier.GuardedTierError):
        tier.build_commands(options(tier="monitor", apply=True), tmp_path, target)


@pytest.mark.parametrize("outcome", ["success", "nonzero", "interrupt", "unknown"])
def test_monitor_apply_owner_keeps_failure_and_cancellation_fences(
    tmp_path, monkeypatch, outcome
):
    calls = []
    monkeypatch.setattr(
        checkpoint, "database_target_fingerprint", lambda _: {"value": "b" * 64}
    )

    def runner(argv, log, *, env):
        calls.append(log.stem)
        if log.stem == "collect":
            (log.parent.parent / "monitor.json").write_text(
                json.dumps(monitor_artifact())
            )
            return 0
        assert "--apply" in argv and "--expected-db-target-sha256" in argv
        if outcome == "interrupt":
            raise KeyboardInterrupt
        if outcome == "unknown":
            raise checkpoint.ProcessCleanupUnknown("secret-canary")
        return int(outcome == "nonzero")

    rc = execute(options(tier="monitor", apply=True), tmp_path, _runner=runner)
    assert calls == ["collect", "monitor_apply"]
    assert receipt(tmp_path)["database_target_sha256"] == "b" * 64
    if outcome == "success":
        assert rc == 0
        assert_released(tmp_path)
    else:
        assert rc != 0
        assert "secret-canary" not in json.dumps(receipt(tmp_path))
        assert_fenced(tmp_path)


def test_enrichment_apply_is_exactly_bound_and_never_lifecycle_escalated(tmp_path):
    target = "b" * 64
    argv = tier.build_commands(
        options(apply=True, env_file="/private/cre.env"), tmp_path, target
    )[0][1]
    assert argv[2:6] == ["--source", "interra-realty", "--batch", "10"]
    assert argv[6:] == [
        "--expected-db-target-sha256",
        target,
        "--env-file",
        "/private/cre.env",
    ]
    assert not set(argv).intersection(
        {"--mark-missing", "--activate-status", "--update-baseline"}
    )
    with pytest.raises(tier.GuardedTierError):
        tier.build_commands(options(apply=True), tmp_path, None)


def test_success_fences_every_dispatch_and_clears_only_after_durable_terminal(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setenv("CRE_ACTIVATE_STATUS", "1")

    def runner(argv, log, *, env):
        assert "CRE_ACTIVATE_STATUS" not in env
        fields = real_factory._authority_fields(lock.authority_fd)
        assert fields[-1] is True
        assert receipt(tmp_path)["status"] == "running"
        calls.append(argv)
        return 0

    original = checkpoint.SharedLock.clear_recovery_requirement

    def clear(self):
        doc = receipt(tmp_path)
        assert doc["status"] == "complete" and doc["guards_stopped"] is True
        assert doc["recovery_clear_eligible"] is True
        calls.append("clear")
        return original(self)

    monkeypatch.setattr(checkpoint.SharedLock, "clear_recovery_requirement", clear)
    # Capture the real held lock without changing its acquisition/fence logic.
    real_factory = checkpoint.SharedLock
    lock = None

    class CapturingLock(real_factory):
        def __init__(self, *args, **kwargs):
            nonlocal lock
            super().__init__(*args, **kwargs)
            lock = self

    monkeypatch.setattr(checkpoint, "SharedLock", CapturingLock)
    assert execute(options(), tmp_path, _runner=runner) == 0
    assert "--dry-run" in calls[0]
    assert calls[-1] == "clear"
    with real_factory(tmp_path / "private.lock"):
        pass


@pytest.mark.parametrize("outcome", ["nonzero", "interrupt", "unknown", "exception"])
def test_unsuccessful_work_remains_fenced_without_retry_or_secret_errors(
    tmp_path, outcome
):
    calls = []

    def runner(*_args, **_kwargs):
        calls.append(1)
        if outcome == "nonzero":
            return 1
        if outcome == "interrupt":
            raise KeyboardInterrupt
        if outcome == "unknown":
            raise checkpoint.ProcessCleanupUnknown("password=do-not-retain")
        raise RuntimeError("password=do-not-retain")

    assert execute(options(), tmp_path, _runner=runner) != 0
    assert len(calls) == 1
    doc = receipt(tmp_path)
    assert doc["recovery_clear_eligible"] is False
    assert "do-not-retain" not in json.dumps(doc)
    assert_fenced(tmp_path)


@pytest.mark.parametrize("sampler", [lambda: 95.0, lambda: float("nan")])
def test_cpu_admission_blocks_before_worker(tmp_path, sampler):
    assert (
        execute(
            options(),
            tmp_path,
            _cpu_sampler=sampler,
            _runner=lambda *_a, **_kw: pytest.fail("unexpected work"),
        )
        == 75
    )
    assert receipt(tmp_path)["status"] == "resource_guard_interrupted"
    assert_released(tmp_path)


def test_memory_unknown_blocks_before_worker(tmp_path):
    def unknown():
        raise ValueError("secret invalid telemetry")

    assert (
        execute(
            options(),
            tmp_path,
            _memory_sampler=unknown,
            _runner=lambda *_a, **_kw: pytest.fail("unexpected work"),
        )
        == 75
    )
    assert_released(tmp_path)


def test_database_env_systemexit_after_settled_git_publishes_fixed_preflight_receipt(
    tmp_path, monkeypatch
):
    def failed_env(_path):
        raise SystemExit("password=do-not-retain")

    monkeypatch.setattr(checkpoint, "database_target_fingerprint", failed_env)

    def verifier(_sha, command, _run_dir):
        command("git_head", ["git", "rev-parse", "HEAD"])

    def runner(argv, _log, *, env):
        assert argv[0] == "git", "provider worker must not run"
        return 0

    assert (
        execute(
            options(apply=True),
            tmp_path,
            _runner=runner,
            _verify_checkout=verifier,
        )
        == 1
    )
    doc = receipt(tmp_path)
    assert doc["status"] == "preflight_refused"
    assert doc["native_command_active"] is False
    assert doc["native_work_started"] is True
    assert "do-not-retain" not in json.dumps(doc)
    assert_released(tmp_path)


@pytest.mark.parametrize("fault", ["wrong_sha", "dirty", "stale_tip", "git_nonzero"])
def test_known_settled_git_refusal_releases_before_tier_work(tmp_path, fault):
    calls = []

    def runner(argv, log, *, env):
        assert argv[0] == "git", "provider worker must not run"
        calls.append(log.stem)
        output = {
            "git_head": ["b" * 40 if fault == "wrong_sha" else "a" * 40],
            "git_clean": [" M dirty.py"] if fault == "dirty" else [],
            "git_pushed": ["b" * 40 if fault == "stale_tip" else "a" * 40],
        }[log.stem]
        log.parent.mkdir(exist_ok=True)
        log.write_text(
            "[time] command: git\n"
            + "".join(line + "\n" for line in output)
            + "[time] rc=0\n"
        )
        return 1 if fault == "git_nonzero" else 0

    assert (
        execute(
            options(), tmp_path, _verify_checkout=tier.verify_checkout, _runner=runner
        )
        == 1
    )
    doc = receipt(tmp_path)
    assert doc["status"] == "preflight_refused"
    assert doc["native_work_started"] is True
    assert doc["native_command_active"] is False
    assert doc["tier_worker_started"] is False
    assert doc["recovery_clear_eligible"] is True
    assert calls
    assert_released(tmp_path)


@pytest.mark.parametrize(
    "failure", ["interrupted", "unknown", "preflight_exception", "exception"]
)
def test_active_git_command_exception_never_clears_fence(tmp_path, failure):
    def runner(*_a, **_kw):
        if failure == "interrupted":
            raise KeyboardInterrupt
        if failure == "unknown":
            raise checkpoint.ProcessCleanupUnknown("synthetic unknown settlement")
        if failure == "preflight_exception":
            raise tier.PreflightRefused("synthetic refusal during active command")
        raise RuntimeError("synthetic unexpected command failure")

    assert (
        execute(
            options(), tmp_path, _verify_checkout=tier.verify_checkout, _runner=runner
        )
        != 0
    )
    doc = receipt(tmp_path)
    assert doc["native_command_active"] is True
    assert doc["tier_worker_started"] is False
    assert doc["recovery_clear_eligible"] is False
    assert_fenced(tmp_path)


def test_unknown_settlement_chain_cannot_be_hidden_by_later_preflight_refusal(tmp_path):
    def verifier(*_args):
        raise tier.PreflightRefused(
            "synthetic later refusal"
        ) from checkpoint.ProcessCleanupUnknown("synthetic earlier unknown")

    assert (
        execute(
            options(),
            tmp_path,
            _verify_checkout=verifier,
            _runner=lambda *_a, **_kw: pytest.fail("unexpected work"),
        )
        == 1
    )
    assert receipt(tmp_path)["status"] == "failed_recovery_required"
    assert_fenced(tmp_path)


def test_real_command_has_valid_performance_journal_without_degradation(
    tmp_path, capsys
):
    def runner(_argv, log, *, env):
        return checkpoint.run_command(
            [sys.executable, "-c", "print('synthetic completion')"], log, env=env
        )

    assert execute(options(), tmp_path, _runner=runner) == 0
    assert "performance telemetry degraded" not in capsys.readouterr().err
    (journal,) = (tmp_path / "runs").glob("*/logs/enrich_dry_run.performance.jsonl")
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [record["event"] for record in records] == ["started", "finished"]
    assert records[-1]["returncode"] == 0
    assert_released(tmp_path)


def test_resource_trip_after_native_dispatch_never_clears_fence(tmp_path):
    def runner(*_a, **_kw):
        checkpoint._set_cpu_guard_trip("synthetic CPU stop")
        raise KeyboardInterrupt

    assert execute(options(), tmp_path, _runner=runner) == 75
    assert_fenced(tmp_path)


@pytest.mark.parametrize("age", [0, 14999, 15000])
def test_fresh_windows_admission_is_accepted(monkeypatch, age):
    monkeypatch.setattr(tier.time, "time", lambda: 1000)
    tier.validate_windows_admission(1000000 - age)


@pytest.mark.parametrize("stamp", [984999, 1000001, True, "1000000"])
def test_stale_future_or_malformed_windows_admission_is_refused(monkeypatch, stamp):
    monkeypatch.setattr(tier.time, "time", lambda: 1000)
    with pytest.raises(tier.GuardedTierError):
        tier.validate_windows_admission(stamp)


def test_windows_evidence_is_rechecked_before_first_tier_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(tier.time, "time", lambda: 1000)

    def verifier(_sha, _execute, _run_dir):
        monkeypatch.setattr(tier.time, "time", lambda: 1016)

    assert (
        execute(
            options(windows_admission_unix_ms=1000000),
            tmp_path,
            _verify_checkout=verifier,
            _runner=lambda *_a, **_kw: pytest.fail("stale worker dispatch"),
        )
        == 1
    )
    assert receipt(tmp_path)["tier_worker_started"] is False
    assert_fenced(tmp_path)


def test_existing_overlap_never_creates_successor_evidence(tmp_path):
    with checkpoint.SharedLock(tmp_path / "private.lock"):
        with pytest.raises(checkpoint.LockHeldError):
            execute(
                options(),
                tmp_path,
                _runner=lambda *_a, **_kw: pytest.fail("unexpected work"),
            )
        assert not (tmp_path / "runs").exists()


def test_terminal_publication_failure_preserves_fence(tmp_path, monkeypatch):
    original = checkpoint.atomic_write_text

    def fail_terminal(path, text):
        if path.name == "receipt.json" and json.loads(text)["status"] != "running":
            raise OSError("synthetic publication failure")
        original(path, text)

    monkeypatch.setattr(checkpoint, "atomic_write_text", fail_terminal)
    with pytest.raises(OSError):
        execute(options(), tmp_path, _runner=lambda *_a, **_kw: 0)
    assert_fenced(tmp_path)


def test_checkout_requires_clean_exact_pushed_tracking_tip(tmp_path):
    responses = {"git_head": ["a" * 40], "git_clean": [], "git_pushed": ["a" * 40]}

    def command(phase, _argv):
        path = tmp_path / "logs" / f"{phase}.log"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            "[time] command: git\n"
            + "".join(line + "\n" for line in responses[phase])
            + "[time] rc=0\n"
        )

    tier.verify_checkout("a" * 40, command, tmp_path)
    for phase, value in [
        ("git_head", ["b" * 40]),
        ("git_clean", [" M file.py"]),
        ("git_pushed", ["b" * 40]),
    ]:
        prior = responses[phase]
        responses[phase] = value
        with pytest.raises(tier.GuardedTierError):
            tier.verify_checkout("a" * 40, command, tmp_path)
        responses[phase] = prior


def test_real_deadline_settles_owned_worker_and_restores_timer_handlers(tmp_path):
    before = {
        s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)
    }
    started = tmp_path / "started"

    def runner(_argv, log, *, env):
        return checkpoint.run_command(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; import time; Path({str(started)!r}).write_text('ready'); time.sleep(30)",
            ],
            log,
            env=env,
        )

    began = time.monotonic()
    assert execute(options(), tmp_path, _runner=runner, _deadline_seconds=1) == 124
    assert started.read_text() == "ready"
    # The one-second work deadline is followed by checkpoint's separate owned
    # group settlement grace and bounded guard joins, not a one-second hard exit.
    assert time.monotonic() - began < 30
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert before == {s: signal.getsignal(s) for s in before}
    assert receipt(tmp_path)["status"] == "deadline_interrupted"
    assert_fenced(tmp_path)


def test_operator_stop_disarms_deadline_before_settlement():
    with tier.work_deadline(30):
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_existing_timer_is_preserved():
    signal.setitimer(signal.ITIMER_REAL, 60)
    try:
        with pytest.raises(tier.GuardedTierError):
            with tier.work_deadline(1):
                pytest.fail("existing timer was overwritten")
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 50
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


def test_blocked_caller_alarm_cannot_disable_work_deadline():
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        with pytest.raises(tier.GuardedTierError):
            with tier.work_deadline(1):
                pytest.fail("a blocked timer was admitted")
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def test_real_abrupt_parent_death_leaves_successor_fenced(tmp_path):
    ready = tmp_path / "ready"
    finished = tmp_path / "finished"
    collector = str(Path(tier.__file__).parent)
    script = f"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, {collector!r})
import cre_guarded_tier as tier
import cre_checkpoint_refresh as checkpoint
args = argparse.Namespace(**{vars(options())!r})
def runner(argv, log, *, env):
    return checkpoint.run_command([sys.executable, '-c', {("from pathlib import Path; import time; Path(" + repr(str(ready)) + ").write_text('ready'); time.sleep(1); Path(" + repr(str(finished)) + ").write_text('settled')")!r}], log, env=env)
tier.run_guarded_tier(args, _lock_path=Path({str(tmp_path / "private.lock")!r}),
    _out_root=Path({str(tmp_path / "runs")!r}), _runner=runner,
    _cpu_sampler=lambda: 10.0, _memory_sampler=lambda: 1,
    _verify_checkout=lambda *_args: None)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        end = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < end and parent.poll() is None:
            time.sleep(0.01)
        assert ready.exists(), (
            parent.stderr.read().decode()
            if parent.poll() is not None
            else "worker not ready"
        )
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)
        assert_fenced(tmp_path)
        end = time.monotonic() + 5
        while not finished.exists() and time.monotonic() < end:
            time.sleep(0.01)
        assert finished.exists()
        assert_fenced(tmp_path)
        assert receipt(tmp_path)["status"] == "running"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        parent.stderr.close()
