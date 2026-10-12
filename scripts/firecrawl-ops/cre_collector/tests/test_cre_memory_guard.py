"""Required memory safety, pure telemetry and owned-process signal contracts."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import cre_memory_guard as memory
import pytest

REAL_READ_PRESSURE = memory.read_memory_pressure


@pytest.fixture(autouse=True)
def clear_trip():
    refresh._clear_cpu_guard_trip()
    yield
    refresh._clear_cpu_guard_trip()


def guard(tmp_path, **kwargs):
    trips = []
    instance = memory.HostMemoryGuard(
        log_path=tmp_path / "memory.jsonl",
        on_trip=lambda **value: trips.append(value),
        **kwargs,
    )
    return instance, trips


def test_warning_requires_continuous_window_and_normal_resets_it(tmp_path):
    instance, _ = guard(tmp_path)
    for clock in range(0, 20, 2):
        assert instance.observe(2, observed_monotonic=clock) is None
    assert instance.observe(1, observed_monotonic=20) is None
    for clock in range(22, 42, 2):
        assert instance.observe(2, observed_monotonic=clock) is None
    assert instance.observe(2, observed_monotonic=42) == "host_memory_warning_sustained"


def test_critical_trips_immediately_and_normal_is_not_a_free_ram_percent(tmp_path):
    instance, _ = guard(tmp_path)
    assert instance.observe(1, observed_monotonic=1) is None
    assert instance.observe(4, observed_monotonic=2) == "host_memory_critical"


@pytest.mark.parametrize("pressure", [None, True, 0, 3, 5, "1", float("nan")])
def test_unknown_or_internal_kernel_enum_is_not_valid_dispatch_level(
    tmp_path, pressure
):
    instance, _ = guard(tmp_path)
    with pytest.raises(memory.MemoryTelemetryError):
        instance.observe(pressure, observed_monotonic=1)


@pytest.mark.parametrize("clock", [0, 1, 4.1, float("nan"), float("inf")])
def test_stale_nonmonotonic_observations_never_bridge_sustained_window(tmp_path, clock):
    instance, _ = guard(tmp_path)
    instance.observe(2, observed_monotonic=1)
    with pytest.raises(memory.MemoryTelemetryError):
        instance.observe(2, observed_monotonic=clock)


@pytest.mark.parametrize("pressure", [2, 4])
def test_preflight_rejects_warning_and_critical(tmp_path, pressure):
    instance, trips = guard(tmp_path, sampler=lambda: pressure)
    assert instance.preflight() == pressure
    assert instance.tripped
    assert trips == [
        {
            "reason_code": "host_memory_start_blocked",
            "pressure": pressure,
            "telemetry_valid": True,
            "evidence_valid": True,
        }
    ]
    with pytest.raises(RuntimeError):
        instance.start()


def test_preflight_normal_persists_compact_bounded_tick(tmp_path):
    instance, trips = guard(
        tmp_path,
        sampler=lambda: 1,
        context_provider=lambda: {
            "phase": "preflight",
            "source": "jll",
            "raw": "private-secret",
        },
    )
    assert instance.preflight() == 1
    record = json.loads(instance.log_path.read_text())
    assert record["pressure_level"] == 1
    assert record["context"] == {"phase": "preflight", "source": "jll"}
    assert "config" not in record and "private-secret" not in json.dumps(record)
    # Even maximal compact context and longest stop reason leave >24h at2s.
    instance.context_provider = lambda: {"phase": "x" * 32, "source": "x" * 32}
    instance._write(pressure=4, reason_code="host_memory_warning_sustained")
    maximum_tick = max(
        len(line) + 1 for line in instance.log_path.read_bytes().splitlines()
    )
    assert maximum_tick * (24 * 3600 // 2) < 16 * 1024 * 1024
    assert trips == []


@pytest.mark.parametrize(
    "stdout,rc", [("0", 0), ("3", 0), ("private-body", 0), ("1" * 40, 0), ("1", 1)]
)
def test_malformed_or_failed_pressure_command_is_fail_closed(monkeypatch, stdout, rc):
    monkeypatch.setattr(
        memory.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, rc, stdout, "private-error"),
    )
    with pytest.raises(memory.MemoryTelemetryError, match="unavailable"):
        REAL_READ_PRESSURE()


def test_pressure_read_timeout_is_bounded_and_fail_closed(monkeypatch):
    calls = []

    def timed_out(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(memory.subprocess, "run", timed_out)
    with pytest.raises(memory.MemoryTelemetryError):
        REAL_READ_PRESSURE(timeout=0.5)
    assert calls[0][0] == [
        "/usr/sbin/sysctl",
        "-n",
        "kern.memorystatus_vm_pressure_level",
    ]
    assert calls[0][1]["timeout"] == 0.5


def test_missing_telemetry_preflight_stops_even_if_callback_returns(tmp_path):
    def unavailable():
        raise OSError("private-error")

    instance, trips = guard(tmp_path, sampler=unavailable)
    with pytest.raises(OSError):
        instance.preflight()
    assert trips[0]["reason_code"] == "host_memory_telemetry_failed"
    assert trips[0]["telemetry_valid"] is False
    assert "private-error" not in instance.log_path.read_text()


def test_evidence_failure_still_trips(tmp_path, monkeypatch):
    instance, trips = guard(tmp_path, sampler=lambda: 1)
    monkeypatch.setattr(
        instance, "_write", lambda **_: (_ for _ in ()).throw(OSError("private-error"))
    )
    with pytest.raises(memory.MemoryEvidenceError):
        instance.preflight()
    assert trips == [
        {
            "reason_code": "host_memory_evidence_failed",
            "pressure": None,
            "telemetry_valid": True,
            "evidence_valid": False,
        }
    ]


def test_stop_request_cannot_discard_already_observed_memory_danger(tmp_path):
    instance, trips = guard(tmp_path)
    instance.stop_event.set()
    instance._trip("host_memory_critical", pressure=4, telemetry_valid=True)
    assert trips[0]["reason_code"] == "host_memory_critical"


@pytest.mark.parametrize("winner", ["cpu", "memory"])
def test_only_first_independent_guard_signals_so_cleanup_cannot_be_reinterrupted(
    tmp_path, monkeypatch, winner
):
    signals = []
    monkeypatch.setattr(refresh.os, "kill", lambda *values: signals.append(values))
    cpu = refresh.HostCpuGuard(
        log_path=tmp_path / "cpu.jsonl", coordinator_pid=os.getpid()
    )

    def cpu_trip():
        cpu._trip("cpu high", percent=95)

    def ram_trip():
        refresh._trip_host_memory_guard(
            reason_code="host_memory_critical",
            pressure=4,
            telemetry_valid=True,
            evidence_valid=True,
        )

    first, second = (cpu_trip, ram_trip) if winner == "cpu" else (ram_trip, cpu_trip)
    first()
    second()
    first()
    second()
    assert signals == [(os.getpid(), signal.SIGTERM)]
    details = refresh._peek_cpu_guard_trip_details()
    # RAM always vetoes CPU recovery, even if CPU already sent the only signal.
    # Later CPU evidence updates cannot overwrite the sticky memory decision.
    assert details.reason_code == "host_memory_critical"
    assert details.host_memory_pressure_level == 4
    refresh._set_cpu_guard_trip(
        "late CPU cleanup evidence failed",
        reason_code="host_cpu_evidence_failed",
        evidence_valid=False,
        owned_processes_reaped=False,
    )
    details = refresh._peek_cpu_guard_trip_details()
    assert details.reason_code == "host_memory_critical"
    assert details.evidence_valid is False
    assert details.owned_processes_reaped is False


@pytest.mark.parametrize("cpu_already_tripped", [False, True])
def test_terminal_boundary_drains_inflight_memory_veto_before_persistence(
    tmp_path, monkeypatch, cpu_already_tripped
):
    signals = []
    monkeypatch.setattr(refresh.os, "kill", lambda *values: signals.append(values))
    cpu = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl")
    if cpu_already_tripped:
        cpu._trip("cpu high", percent=95)
    instance = memory.HostMemoryGuard(
        log_path=tmp_path / "memory.jsonl",
        on_trip=refresh._trip_host_memory_guard,
    )
    entered = threading.Event()
    release = threading.Event()

    def delayed_write(**_):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(instance, "_write", delayed_write)
    instance.thread = threading.Thread(
        target=lambda: instance._trip(
            "host_memory_critical", pressure=4, telemetry_valid=True
        )
    )
    instance.thread.start()
    assert entered.wait(3)
    original_stop = instance.stop

    def stop_and_release():
        release.set()
        original_stop()

    monkeypatch.setattr(instance, "stop", stop_and_release)
    persisted = []
    try:
        with refresh.resource_guard_boundary(instance, cpu):
            refresh.quiesce_resource_guards(instance, cpu)
            persisted.append("supported_scope_complete")
    except refresh.CpuGuardTrip:
        details = refresh._peek_cpu_guard_trip_details()
        persisted.append(details.reason_code)
    assert persisted == ["host_memory_critical"]
    assert signals == ([(os.getpid(), signal.SIGTERM)] if cpu_already_tripped else [])
    assert not instance.thread.is_alive()


def test_series_passes_memory_policy_and_cpu_recovery_does_not_admit_ram_stop(tmp_path):
    config = series.series_config(
        sources=("cbre",),
        page_cap=400,
        concurrency=6,
        attempts_per_source=3,
        max_resume_age_hours=24,
        max_host_cpu_percent=90,
        cpu_sustain_seconds=10,
        cpu_sample_seconds=2,
        nice=0,
        memory_sustain_seconds=12,
        memory_sample_seconds=1,
    )
    argv = series.build_checkpoint_argv(
        "cbre", child_out_root=tmp_path / "runs", env_file=None, config=config
    )
    assert argv[argv.index("--memory-sustain-seconds") + 1] == "12"
    assert argv[argv.index("--memory-sample-seconds") + 1] == "1"
    legacy_parent = series.new_manifest(tmp_path, git_sha="a" * 40, config=config)
    del legacy_parent["config"]["host_memory_guard"]
    # Historical reconciliation support cannot admit a new runnable series
    # whose recorded policy omits the mandatory guard.
    series.save_manifest(tmp_path, legacy_parent)
    with pytest.raises(series.SeriesError, match="configuration differs"):
        series.load_resume_manifest(
            tmp_path / "manifest.json", git_sha="a" * 40, config=config
        )
    manifest = series.new_manifest(
        tmp_path, git_sha="a" * 40, config=config, database_target={"value": "b" * 64}
    )
    path = tmp_path / "runs" / "2026-10-12T010000Z" / "manifest.json"
    child = {
        "status": series.RESOURCE_GUARD_STATUS,
        "run_id": path.parent.name,
        "collector_git_sha": "a" * 40,
        "config": series._expected_child_config("cbre", config),
        "preflight": {"database_target": {"value": "b" * 64}},
        "started_at": "2026-10-12T01:00:00Z",
        "finished_at": "2026-10-12T01:01:00Z",
        "resource_stop": {"schema_version": 1, "reason_code": "host_memory_critical"},
    }
    with pytest.raises(series.SeriesError, match="not recoverable CPU"):
        series.validate_resource_recovery_admission(
            tmp_path,
            manifest,
            path,
            child,
            source="cbre",
            now=datetime(2026, 10, 12, 1, 2, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    "sustain,sample",
    [(0, 2), (31, 2), (20, 0), (20, 6), (1, 2), (float("nan"), 2), (20, True)],
)
def test_memory_guard_cannot_be_disabled_by_invalid_policy(sustain, sample):
    with pytest.raises(ValueError):
        memory.memory_guard_config(sustain, sample)


@pytest.mark.parametrize("sustain,sample", [(14, 1), (12, 2)])
def test_resume_rejects_custom_memory_policy_drift(tmp_path, sustain, sample):
    manifest = refresh.new_manifest(
        tmp_path,
        git_sha="a" * 40,
        git_dirty=False,
        sources=("cbre",),
        page_cap=400,
        concurrency=6,
        memory_sustain_seconds=12,
        memory_sample_seconds=1,
    )
    path = tmp_path / "manifest.json"
    refresh.atomic_write_json(path, manifest)
    options = {
        "git_sha": "a" * 40,
        "sources": ("cbre",),
        "page_cap": 400,
        "concurrency": 6,
    }
    loaded = refresh.load_resume_manifest(
        path, **options, memory_sustain_seconds=12, memory_sample_seconds=1
    )
    assert loaded["config"]["host_memory_guard"] == memory.memory_guard_config(12, 1)
    with pytest.raises(refresh.RefreshError, match="configuration differs"):
        refresh.load_resume_manifest(
            path,
            **options,
            memory_sustain_seconds=sustain,
            memory_sample_seconds=sample,
        )


def test_real_memory_signal_reaps_owned_command_and_releases_tmp_lock(tmp_path):
    script = r"""
import json, os, signal, sys
from pathlib import Path
import cre_checkpoint_refresh as refresh
from cre_memory_guard import HostMemoryGuard
root = Path(sys.argv[1])
# Short fixture grace; production cleanup retains its documented bound.
refresh.COMMAND_INTERRUPT_GRACE_SECONDS = 0.1
samples = iter([1, 4])
guard = HostMemoryGuard(root / "memory.jsonl", on_trip=refresh._trip_host_memory_guard,
    sampler=lambda: next(samples), sample_seconds=0.25, sustain_seconds=1)
signal.signal(signal.SIGTERM, refresh.checkpoint_sigterm_handler)
try:
    with refresh.SharedLock(root / ".cre.lock"):
        guard.preflight()
        guard.start()
        refresh._run_logged_command([sys.executable, "-c", "import os,time; from pathlib import Path; Path(" + repr(str(root / "pid")) + ").write_text(str(os.getpid())); time.sleep(60)"], root / "command.log")
except refresh.CpuGuardTrip:
    details = refresh._peek_cpu_guard_trip_details()
    assert details.reason_code == "host_memory_critical"
    assert details.host_memory_pressure_level == 4
    raise SystemExit(75)
finally:
    guard.stop()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(refresh.__file__).parent,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert completed.returncode == 75, completed.stderr
    assert not (tmp_path / ".cre.lock").exists()
    pid = int((tmp_path / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
