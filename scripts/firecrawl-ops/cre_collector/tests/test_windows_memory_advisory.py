"""Windows telemetry failures are advisory; observed memory pressure is not."""
import cre_memory_guard as memory
import pytest


def test_missing_preflight_is_unknown_and_reports_once(tmp_path, capsys):
    def unavailable():
        raise OSError("private detail")

    trips = []
    guard = memory.HostMemoryGuard(tmp_path / "guard.jsonl", trips.append,
                                   sampler=unavailable, advisory_failures=True)
    assert guard.preflight() is None
    assert guard.preflight() is None
    assert not guard.tripped and trips == []
    diagnostic = capsys.readouterr().err
    assert diagnostic.count("monitoring degraded") == 1
    assert "private detail" not in diagnostic


def test_gap_resets_warning_and_later_samples_can_trip(tmp_path):
    guard = memory.HostMemoryGuard(tmp_path / "guard.jsonl", lambda **_: None,
                                   advisory_failures=True)
    assert guard.observe(2, observed_monotonic=1) is None
    with pytest.raises(memory.MemoryTelemetryError) as error:
        guard.observe(2, observed_monotonic=30)
    guard._degrade(error.value)
    assert guard.high_since is None and guard.previous_observed is None
    for tick in range(31, 52, 2):
        result = guard.observe(2, observed_monotonic=tick)
    assert result == "host_memory_warning_sustained"


def test_journal_failure_does_not_suppress_sustained_warning(tmp_path, monkeypatch):
    trips = []
    guard = memory.HostMemoryGuard(tmp_path / "guard.jsonl",
                                   lambda **kw: trips.append(kw),
                                   sustain_seconds=2, sample_seconds=1,
                                   sampler=lambda: 2, advisory_failures=True)
    clock = iter([1, 1, 2, 2, 3, 3, 4, 4])
    monkeypatch.setattr(memory.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(guard, "_write", lambda **_: (_ for _ in ()).throw(OSError()))
    for _ in range(4):
        try:
            guard.sample_once()
        except memory.MemoryEvidenceError as error:
            guard._degrade(error)
    assert trips[0]["reason_code"] == "host_memory_warning_sustained"
    assert trips[0]["evidence_valid"] is False


def test_slow_critical_still_trips(tmp_path, monkeypatch):
    trips = []
    guard = memory.HostMemoryGuard(tmp_path / "guard.jsonl",
                                   lambda **kw: trips.append(kw),
                                   sampler=lambda: 4, advisory_failures=True)
    clock = iter([1, 3])
    monkeypatch.setattr(memory.time, "monotonic", lambda: next(clock))
    assert guard.sample_once() == 4
    assert trips[0]["reason_code"] == "host_memory_critical"


def test_non_wsl_telemetry_failure_still_raises(tmp_path):
    def unavailable():
        raise OSError("unavailable")
    trips = []
    guard = memory.HostMemoryGuard(tmp_path / "guard.jsonl",
                                   lambda **kw: trips.append(kw),
                                   sampler=unavailable, advisory_failures=False)
    with pytest.raises(OSError):
        guard.preflight()
    assert trips[0]["reason_code"] == "host_memory_telemetry_failed"


def test_policy_records_wsl_degradation(monkeypatch):
    monkeypatch.setattr(memory, "windows_wsl", lambda: True)
    assert memory.memory_guard_config()["telemetry_failure_action"] == "diagnostic_only"
    monkeypatch.setattr(memory, "windows_wsl", lambda: False)
    assert memory.memory_guard_config()["telemetry_failure_action"] == "interrupt_and_checkpoint"


def test_inflight_critical_during_stop_remains_a_trip(tmp_path):
    import threading

    entered = threading.Event()
    release = threading.Event()
    trips = []
    def sample():
        entered.set()
        assert release.wait(2)
        return 4

    guard = memory.HostMemoryGuard(tmp_path / "guard.jsonl",
                                   lambda **kw: trips.append(kw),
                                   sample_seconds=0.25, sampler=sample,
                                   advisory_failures=True)
    guard.start()
    assert entered.wait(2)
    guard.stop_event.set()
    release.set()
    guard.stop()
    assert not guard.thread.is_alive()
    assert trips[0]["reason_code"] == "host_memory_critical"


def test_unknown_preflight_serializes_in_real_checkpoint(tmp_path, monkeypatch):
    import json
    from contextlib import nullcontext

    import cre_checkpoint_refresh as refresh

    monkeypatch.setattr(memory, "windows_wsl", lambda: True)
    monkeypatch.setattr(refresh, "windows_wsl", lambda: True)
    monkeypatch.setattr(refresh, "git_identity", lambda: ("b" * 40, False))
    monkeypatch.setattr(refresh, "checkpoint_lock_dir", lambda _: tmp_path / "lock")
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"algorithm": "sha256", "value": "a" * 64})
    monkeypatch.setattr(refresh, "observe_resource_usage", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(memory, "read_memory_pressure", lambda **kw: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(refresh, "run_command", lambda *a, **kw: 7)
    with pytest.raises(refresh.GlobalStageError, match="healthcheck failed"):
        refresh.main(["--out-root", str(tmp_path / "runs"), "--sources", "cbre"])
    manifest = json.loads(next((tmp_path / "runs").glob("*/manifest.json")).read_text())
    assert manifest["preflight"]["host_memory_guard"]["pressure_level"] is None
    assert manifest["config"]["host_memory_guard"]["telemetry_failure_action"] == "diagnostic_only"


def test_historical_policy_reconciles_but_cannot_resume(tmp_path, monkeypatch):
    import copy

    import cre_checkpoint_series as series

    monkeypatch.setattr(memory, "windows_wsl", lambda: True)
    config = series.series_config(
        sources=("cbre",), page_cap=1, concurrency=1, attempts_per_source=1,
        max_resume_age_hours=24, max_host_cpu_percent=90,
        cpu_sustain_seconds=10, cpu_sample_seconds=2, nice=0,
    )
    historical = copy.deepcopy(config)
    historical["host_memory_guard"]["telemetry_failure_action"] = "interrupt_and_checkpoint"
    assert series._expected_child_config("cbre", historical)["host_memory_guard"] == historical["host_memory_guard"]
    manifest = series.new_manifest(tmp_path, git_sha="a" * 40, config=historical)
    series.save_manifest(tmp_path, manifest)
    with pytest.raises(series.SeriesError, match="configuration differs"):
        series.load_resume_manifest(tmp_path / "manifest.json", git_sha="a" * 40, config=config)


def test_advisory_loop_continues_after_failure_and_trips_on_later_critical(tmp_path):
    class BoundedTicks:
        def __init__(self):
            self.count = 0

        def wait(self, _seconds):
            self.count += 1
            return self.count > 3

    samples = iter([ValueError("private"), 1, 4])
    def sample():
        value = next(samples)
        if isinstance(value, Exception):
            raise value
        return value

    trips = []
    guard = memory.HostMemoryGuard(tmp_path / "guard.jsonl",
                                   lambda **kw: trips.append(kw),
                                   sampler=sample, advisory_failures=True)
    guard.stop_event = BoundedTicks()
    guard._run()
    assert guard.stop_event.count == 3
    assert trips[0]["reason_code"] == "host_memory_critical"
    assert "private" not in guard.log_path.read_text()


def test_guard_journal_records_degradation_then_recovery(tmp_path):
    import json

    class Ticks:
        def __init__(self):
            self.count = 0

        def wait(self, _seconds):
            self.count += 1
            return self.count > 4

    samples = iter([1, OSError("private"), OSError("private"), 1])

    def sample():
        value = next(samples)
        if isinstance(value, Exception):
            raise value
        return value

    guard = memory.HostMemoryGuard(tmp_path / "guard.jsonl", lambda **_: None,
                                   sampler=sample, advisory_failures=True)
    guard.stop_event = Ticks()
    guard._run()
    rows = [json.loads(line) for line in guard.log_path.read_text().splitlines()]
    assert [row["state"] for row in rows] == ["ok", "degraded", "recovered"]
    assert rows[1]["pressure_level"] is None
    assert rows[1]["telemetry_failure"]["category"] == "read_unavailable"
    assert rows[2]["preceding_telemetry_failure"]["category"] == "read_unavailable"
    assert "private" not in guard.log_path.read_text()


def test_guard_journal_recovers_after_failed_degradation_write(tmp_path, monkeypatch):
    import json

    guard = memory.HostMemoryGuard(tmp_path / "guard.jsonl", lambda **_: None,
                                   sampler=lambda: 1, advisory_failures=True)
    append = memory._append_record
    def unavailable(*_args, **_kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(memory, "_append_record", unavailable)
    guard._degrade(memory.MemoryEvidenceError("unavailable"))
    assert guard.failure_reported and not guard.tripped
    monkeypatch.setattr(memory, "_append_record", append)
    guard.sample_once()
    row = json.loads(guard.log_path.read_text())
    assert row["state"] == "recovered"
    assert row["preceding_telemetry_failure"]["category"] == "evidence_write"


def test_degradation_record_retries_after_journal_recovers(tmp_path, monkeypatch):
    import json

    guard = memory.HostMemoryGuard(tmp_path / "guard.jsonl", lambda **_: None,
                                   sampler=lambda: 1, advisory_failures=True)
    append = memory._append_record
    writes = 0

    def fail_once(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("journal unavailable")
        return append(*args, **kwargs)

    monkeypatch.setattr(memory, "_append_record", fail_once)
    for _ in range(3):
        guard._degrade(OSError("telemetry unavailable"))
    rows = [json.loads(line) for line in guard.log_path.read_text().splitlines()]
    assert writes == 2
    assert [row["state"] for row in rows] == ["degraded"]
    assert rows[0]["pressure_level"] is None
    assert rows[0]["telemetry_failure"]["category"] == "read_unavailable"
    assert not guard.tripped
