"""Bounded required-watchdog faults, with no live CPU, Docker, or source work."""

import json

import cre_checkpoint_refresh as refresh
import cre_runtime_observability as observability
import pytest


@pytest.fixture(autouse=True)
def clear_trip():
    refresh._clear_cpu_guard_trip()
    yield
    refresh._clear_cpu_guard_trip()


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(refresh.time, "monotonic", lambda: now[0])
    return now


def test_deliberate_two_second_initial_read_is_valid(tmp_path, clock):
    def sample():
        clock[0] += 2.0
        return 20.0

    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl", sampler=sample)
    assert refresh.run_cpu_guard_preflight(guard) == 20.0
    assert refresh._peek_cpu_guard_trip() is None


@pytest.mark.parametrize("initial", [True, False])
def test_late_cpu_read_is_not_admitted_as_fresh(tmp_path, clock, initial):
    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl", sampler=lambda: 20.0)
    if not initial:
        guard.sample_once()
        clock[0] += 2.0

    def delayed():
        clock[0] += 3.1 if initial else 1.1
        return 20.0

    guard.sampler = delayed
    with pytest.raises(refresh.CpuGuardTrip):
        refresh.run_cpu_guard_preflight(guard)
    details = refresh._peek_cpu_guard_trip_details()
    assert details.reason_code == "host_cpu_telemetry_failed"
    assert details.telemetry_valid is False
    record = json.loads(guard.log_path.read_text().splitlines()[-1])
    assert record["reason_code"] == details.reason_code
    assert record["telemetry_valid"] is False


@pytest.mark.parametrize("gap", [0.0, -1.0, 3.1])
def test_frozen_backward_or_stale_interval_stops_even_on_low_cpu(tmp_path, clock, gap):
    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl", sampler=lambda: 20.0)
    guard.sample_once()
    clock[0] = gap
    with pytest.raises(refresh.CpuGuardTrip):
        refresh.run_cpu_guard_preflight(guard)
    assert refresh._peek_cpu_guard_trip_details().telemetry_valid is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "clock", 10**1000])
def test_invalid_observation_time_is_rejected(tmp_path, bad):
    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl")
    with pytest.raises(refresh.CpuTelemetryError, match="time|clock"):
        guard.observe(20.0, observed_monotonic=bad)


def test_read_clock_cannot_move_backward(tmp_path, clock):
    clock[0] = 5.0

    def sample():
        clock[0] = 4.0
        return 20.0

    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl", sampler=sample)
    with pytest.raises(refresh.CpuGuardTrip):
        refresh.run_cpu_guard_preflight(guard)


def test_next_read_start_cannot_precede_last_observation(tmp_path, monkeypatch):
    times = iter([0.0, 0.0, -0.1, 0.1])
    monkeypatch.setattr(refresh.time, "monotonic", lambda: next(times))
    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl", sampler=lambda: 20.0)
    guard.sample_once()
    with pytest.raises(refresh.CpuGuardTrip):
        refresh.run_cpu_guard_preflight(guard)


@pytest.mark.parametrize("failure", ["sampler", "required_log"])
def test_ordinary_thread_failure_trips_and_signals_instead_of_dying(
    tmp_path, monkeypatch, failure
):
    signals = []
    guard = refresh.HostCpuGuard(
        log_path=tmp_path / "cpu.jsonl",
        sampler=lambda: 20.0,
        sample_seconds=0.01,
        coordinator_pid=4321,
    )
    original_write = guard._write_record

    def broken():
        raise RuntimeError("synthetic required failure")

    if failure == "sampler":
        guard.sampler = broken
    else:

        def write(**kwargs):
            if kwargs["state"] == "ok":
                broken()
            original_write(**kwargs)

        monkeypatch.setattr(guard, "_write_record", write)
    monkeypatch.setattr(refresh.os, "kill", lambda *args: signals.append(args))
    guard.start()
    guard.thread.join(timeout=1)
    guard.stop()
    assert not guard.thread.is_alive()
    assert signals == [(4321, refresh.signal.SIGTERM)]
    details = refresh._peek_cpu_guard_trip_details()
    expected = (
        "host_cpu_evidence_failed"
        if failure == "required_log"
        else "host_cpu_telemetry_failed"
    )
    assert details.reason_code == expected
    assert details.telemetry_valid is (failure == "required_log")
    assert details.evidence_valid is (failure != "required_log")
    assert (
        json.loads(guard.log_path.read_text().splitlines()[-1])["reason_code"]
        == expected
    )


def test_ordinary_preflight_failure_uses_resource_exit(tmp_path):
    def broken():
        raise RuntimeError("synthetic sampler failure")

    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl", sampler=broken)
    with pytest.raises(refresh.CpuGuardTrip):
        refresh.run_cpu_guard_preflight(guard)
    assert (
        refresh._peek_cpu_guard_trip_details().reason_code
        == "host_cpu_telemetry_failed"
    )


def test_failed_trip_log_still_signals_with_invalid_evidence(tmp_path, monkeypatch):
    guard = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl", coordinator_pid=4321)
    signals = []

    def broken(**kwargs):
        raise RuntimeError("synthetic journal failure")

    monkeypatch.setattr(guard, "_write_record", broken)
    monkeypatch.setattr(refresh.os, "kill", lambda *args: signals.append(args))
    guard._trip("sustained saturation")
    assert signals == [(4321, refresh.signal.SIGTERM)]
    assert refresh._peek_cpu_guard_trip_details().evidence_valid is False


def test_real_journal_serialization_failure_cannot_silently_end_thread(
    tmp_path, monkeypatch
):
    signals = []
    guard = refresh.HostCpuGuard(
        log_path=tmp_path / "cpu.jsonl",
        sampler=lambda: 20.0,
        sample_seconds=0.01,
        coordinator_pid=4321,
    )

    def broken(*args, **kwargs):
        raise ValueError("synthetic JSON encoder failure")

    monkeypatch.setattr(refresh.json, "dumps", broken)
    monkeypatch.setattr(refresh.os, "kill", lambda *args: signals.append(args))
    guard.start()
    guard.thread.join(timeout=1)
    guard.stop()
    assert signals == [(4321, refresh.signal.SIGTERM)]
    details = refresh._peek_cpu_guard_trip_details()
    assert details.reason_code == "host_cpu_evidence_failed"
    assert details.telemetry_valid is True
    assert details.evidence_valid is False


def test_bounded_incident_time_is_allowed_for_only_the_following_gap(
    tmp_path, monkeypatch, clock
):
    def incident(*args, **kwargs):
        clock[0] += 2.1  # Two one-second diagnostic commands plus bounded overhead.

    monkeypatch.setattr(refresh, "append_incident", incident)
    samples = iter([95.0, 20.0, 20.0])
    guard = refresh.HostCpuGuard(
        log_path=tmp_path / "cpu.jsonl",
        incident_log_path=tmp_path / "incident.jsonl",
        sampler=lambda: next(samples),
    )
    guard.sample_once()
    clock[0] += 2.0
    assert guard.sample_once() == 20.0
    clock[0] += 3.1
    with pytest.raises(refresh.CpuTelemetryError, match="stale"):
        guard.sample_once()


def test_slow_incident_cannot_grant_unbounded_freshness(tmp_path, monkeypatch, clock):
    def incident(*args, **kwargs):
        clock[0] += 6.0

    monkeypatch.setattr(refresh, "append_incident", incident)
    guard = refresh.HostCpuGuard(
        log_path=tmp_path / "cpu.jsonl",
        incident_log_path=tmp_path / "incident.jsonl",
        sampler=lambda: 95.0,
    )
    guard.sample_once()
    clock[0] += 2.0
    with pytest.raises(refresh.CpuTelemetryError, match="stale"):
        guard.sample_once()


def test_optional_diagnostic_exception_remains_harmless(tmp_path, monkeypatch, clock):
    def broken(_context):
        raise RuntimeError("optional diagnostic failure")

    monkeypatch.setattr(
        refresh,
        "append_incident",
        lambda path, **kwargs: observability.append_incident(
            path, **kwargs, snapshotter=broken
        ),
    )
    samples = iter([95.0, 20.0])
    guard = refresh.HostCpuGuard(
        log_path=tmp_path / "cpu.jsonl",
        incident_log_path=tmp_path / "incident.jsonl",
        sampler=lambda: next(samples),
    )
    guard.sample_once()
    clock[0] += 2.0
    assert guard.sample_once() == 20.0
    assert guard.high_since is None
    assert refresh._peek_cpu_guard_trip() is None
