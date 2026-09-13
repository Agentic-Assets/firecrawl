"""Pure contracts for bounded checkpoint-series CPU recovery."""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_checkpoint_series as series
import pytest
from cre_resource_recovery import (
    CooldownProgress,
    CooldownResult,
    RecoveryBudgetExhausted,
    RecoveryCancelled,
    RecoveryConfig,
    RecoveryEvidenceError,
    RecoveryOwnershipError,
    RecoveryTelemetryError,
    SeriesOwnershipLock,
    wait_for_cpu_recovery,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def utc_now(self) -> str:
        observed = datetime(2026, 9, 13, tzinfo=timezone.utc) + timedelta(
            seconds=self.now
        )
        return observed.isoformat()


def recovery_config(**overrides: object) -> RecoveryConfig:
    values: dict[str, object] = {
        "max_recoveries_per_source": 1,
        "low_cpu_percent": 60.0,
        "low_cpu_seconds": 30.0,
        "sample_seconds": 2.0,
        "max_cooldown_seconds": 600.0,
        "max_series_cooldown_seconds": 1800.0,
    }
    values.update(overrides)
    return RecoveryConfig(**values)  # type: ignore[arg-type]


def parent_config(
    *,
    source: str = "cbre",
    recovery: RecoveryConfig | None = None,
) -> dict[str, object]:
    return series.series_config(
        sources=(source,),
        page_cap=400,
        concurrency=3,
        attempts_per_source=3,
        max_resume_age_hours=24.0,
        max_host_cpu_percent=75.0,
        cpu_sustain_seconds=10.0,
        cpu_sample_seconds=2.0,
        nice=10,
        recovery=recovery or recovery_config(),
    )


def recovery_manifests(
    tmp_path: Path,
    *,
    phase: str = "collection",
    active_operation: str = "source_collection",
    checkpoint_state: str = "collecting",
) -> tuple[Path, dict[str, object], Path, dict[str, object], datetime]:
    series_dir = tmp_path / "series"
    child_dir = series_dir / "runs" / "2026-09-13T120000Z"
    child_dir.mkdir(parents=True)
    database_target = {"sha256": "d" * 64}
    parent = series.new_manifest(
        series_dir,
        git_sha="a" * 40,
        config=parent_config(),
        database_target=database_target,
    )
    parent["sources"]["cbre"]["state"] = "resource_guard_interrupted"
    parent["sources"]["cbre"]["checkpoint_run"] = f"runs/{child_dir.name}"
    started = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    occurred = started + timedelta(seconds=10)
    recorded = occurred + timedelta(seconds=1)
    finished = recorded + timedelta(seconds=1)
    child: dict[str, object] = {
        "schema_version": 1,
        "run_id": child_dir.name,
        "status": series.RESOURCE_GUARD_STATUS,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "collector_git_sha": "a" * 40,
        "config": series._expected_child_config("cbre", parent["config"]),
        "preflight": {"database_target": database_target},
        "sources": {
            "cbre": {
                "state": checkpoint_state,
                "ingest": None,
                "readback": None,
            }
        },
        "aggregate_gate": None,
        "validation": None,
        "resource_stop": {
            "schema_version": 1,
            "reason_code": "host_cpu_sustained",
            "phase": phase,
            "active_operation": active_operation,
            "source": "cbre",
            "generation_id": child_dir.name,
            "telemetry_valid": True,
            "evidence_valid": True,
            "host_cpu_percent": 75.0,
            "occurred_at": occurred.isoformat(),
            "recorded_at": recorded.isoformat(),
            "owned_processes_reaped": True,
        },
    }
    child_path = child_dir / "manifest.json"
    child_path.write_text(json.dumps(child), encoding="utf-8")
    return series_dir, parent, child_path, child, finished


def test_config_defaults_disable_recovery_and_round_trip() -> None:
    default = RecoveryConfig()

    assert default.max_recoveries_per_source == 0
    assert RecoveryConfig.from_mapping(default.as_dict()) == default


@pytest.mark.parametrize(
    "config",
    [
        recovery_config(max_recoveries_per_source=-1),
        recovery_config(max_recoveries_per_source=4),
        recovery_config(max_recoveries_per_source=True),
        recovery_config(low_cpu_percent=0.0),
        recovery_config(low_cpu_percent=True),
        recovery_config(low_cpu_percent="60"),
        recovery_config(low_cpu_percent=float("nan")),
        pytest.param(
            recovery_config(low_cpu_percent=10**10000),
            id="huge-low-cpu-percent",
        ),
        recovery_config(low_cpu_seconds=0.0),
        recovery_config(sample_seconds=31.0),
        recovery_config(max_cooldown_seconds=601.0),
        recovery_config(max_series_cooldown_seconds=1801.0),
    ],
)
def test_config_rejects_unsafe_values(config: RecoveryConfig) -> None:
    with pytest.raises(ValueError):
        config.validate()


@pytest.mark.parametrize("value", [True, 1.5, "1", None])
def test_config_mapping_rejects_noninteger_recovery_count(value: object) -> None:
    mapping = RecoveryConfig().as_dict()
    mapping["max_recoveries_per_source"] = value  # type: ignore[assignment]

    with pytest.raises(ValueError, match="malformed"):
        RecoveryConfig.from_mapping(mapping)


@pytest.mark.parametrize(
    "value", [True, "60", pytest.param(10**10000, id="huge-integer")]
)
def test_config_mapping_rejects_malformed_numeric_values(value: object) -> None:
    mapping = RecoveryConfig().as_dict()
    mapping["low_cpu_percent"] = value  # type: ignore[assignment]

    with pytest.raises(ValueError, match="malformed"):
        RecoveryConfig.from_mapping(mapping)


def test_wait_requires_strict_continuous_low_cpu_window() -> None:
    clock = FakeClock()
    samples = iter([59.0, 60.0] + [59.0] * 16)
    progress: list[CooldownProgress] = []

    result = wait_for_cpu_recovery(
        recovery_config(),
        already_waited_seconds=0.0,
        series_waited_seconds=0.0,
        sampler=lambda: next(samples),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        utc_now=clock.utc_now,
        on_progress=progress.append,
    )

    assert result.waited_seconds == 34.0
    assert result.low_cpu_seconds == 30.0
    assert progress[1].current_host_cpu_percent == 60.0
    assert progress[1].low_cpu_seconds == 0.0


def test_wait_charges_persisted_per_recovery_and_series_budgets() -> None:
    clock = FakeClock()

    with pytest.raises(RecoveryBudgetExhausted):
        wait_for_cpu_recovery(
            recovery_config(
                max_cooldown_seconds=10.0,
                max_series_cooldown_seconds=20.0,
            ),
            already_waited_seconds=9.0,
            series_waited_seconds=19.0,
            sampler=lambda: 59.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
            on_progress=lambda _progress: None,
        )

    assert clock.now == 1.0


def test_wait_fails_before_sampling_when_budget_is_already_exhausted() -> None:
    with pytest.raises(RecoveryBudgetExhausted):
        wait_for_cpu_recovery(
            recovery_config(max_cooldown_seconds=10.0),
            already_waited_seconds=10.0,
            series_waited_seconds=10.0,
            sampler=lambda: pytest.fail("sampler must not run"),
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
            utc_now=lambda: "2026-09-13T00:00:00+00:00",
            on_progress=lambda _progress: None,
        )


@pytest.mark.parametrize(
    ("already_waited", "series_waited"),
    [(-1.0, 0.0), (0.0, float("nan"))],
)
def test_wait_rejects_invalid_persisted_waits(
    already_waited: float,
    series_waited: float,
) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=already_waited,
            series_waited_seconds=series_waited,
            sampler=lambda: 59.0,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
            utc_now=lambda: "2026-09-13T00:00:00+00:00",
            on_progress=lambda _progress: None,
        )


def test_wait_fails_if_injected_clock_overshoots_budget() -> None:
    readings = iter([0.0, 11.0])

    with pytest.raises(RecoveryBudgetExhausted):
        wait_for_cpu_recovery(
            recovery_config(max_cooldown_seconds=10.0),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: pytest.fail("sampler must not run"),
            monotonic=lambda: next(readings),
            sleep=lambda _seconds: None,
            utc_now=lambda: "2026-09-13T00:00:00+00:00",
            on_progress=lambda _progress: None,
        )


def test_slow_sampler_is_charged_to_budget_and_rejected_when_stale() -> None:
    clock = FakeClock()

    def stale_sample() -> float:
        clock.sleep(3.0)
        return 59.0

    with pytest.raises(RecoveryTelemetryError, match="stale"):
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=stale_sample,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
            on_progress=lambda _progress: None,
        )
    assert clock.now == 3.0

    gap_clock = FakeClock()

    def widening_sample_gap() -> float:
        if gap_clock.now:
            gap_clock.sleep(1.1)
        return 59.0

    with pytest.raises(RecoveryTelemetryError, match="sample gap is stale"):
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=widening_sample_gap,
            monotonic=gap_clock.monotonic,
            sleep=gap_clock.sleep,
            utc_now=gap_clock.utc_now,
            on_progress=lambda _progress: None,
        )
    assert gap_clock.now == 3.1

    deadline_clock = FakeClock()

    def over_deadline() -> float:
        deadline_clock.sleep(1.1)
        return 59.0

    with pytest.raises(RecoveryBudgetExhausted):
        wait_for_cpu_recovery(
            recovery_config(max_cooldown_seconds=1.0),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=over_deadline,
            monotonic=deadline_clock.monotonic,
            sleep=deadline_clock.sleep,
            utc_now=deadline_clock.utc_now,
            on_progress=lambda _progress: None,
        )


def test_oversleep_cannot_claim_an_unobserved_low_cpu_window() -> None:
    clock = FakeClock()

    def oversleep(_seconds: float) -> None:
        clock.now += 40.0

    with pytest.raises(RecoveryTelemetryError, match="sample gap is stale"):
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: 55.0,
            monotonic=clock.monotonic,
            sleep=oversleep,
            utc_now=clock.utc_now,
            on_progress=lambda _progress: None,
        )


def test_slow_nonfinal_progress_write_breaks_low_cpu_continuity() -> None:
    clock = FakeClock()
    writes = 0

    def slow_progress(_progress: CooldownProgress) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            clock.now += 40.0

    with pytest.raises(RecoveryTelemetryError, match="sample gap is stale"):
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: 55.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
            on_progress=slow_progress,
        )


def test_cancellation_is_rechecked_immediately_before_success() -> None:
    clock = FakeClock()
    cancelled = False

    def persist(progress: CooldownProgress) -> None:
        nonlocal cancelled
        if progress.low_cpu_seconds >= 2.0:
            cancelled = True

    with pytest.raises(RecoveryCancelled):
        wait_for_cpu_recovery(
            recovery_config(low_cpu_seconds=2.0, sample_seconds=2.0),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: 59.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
            on_progress=persist,
            cancelled=lambda: cancelled,
        )


def test_cancellation_is_rechecked_after_a_sampler_returns() -> None:
    cancelled = False

    def sample() -> float:
        nonlocal cancelled
        cancelled = True
        return 59.0

    with pytest.raises(RecoveryCancelled):
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=sample,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
            utc_now=lambda: "2026-09-13T00:00:00+00:00",
            on_progress=lambda _progress: None,
            cancelled=lambda: cancelled,
        )


def test_progress_latency_cannot_cross_deadline_before_success() -> None:
    clock = FakeClock()

    def persist(progress: CooldownProgress) -> None:
        if progress.low_cpu_seconds >= 2.0:
            clock.sleep(9.0)

    with pytest.raises(RecoveryBudgetExhausted):
        wait_for_cpu_recovery(
            recovery_config(
                low_cpu_seconds=2.0,
                sample_seconds=2.0,
                max_cooldown_seconds=10.0,
            ),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: 59.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
            on_progress=persist,
        )


def test_stale_sample_cannot_be_used_after_slow_progress_persistence() -> None:
    clock = FakeClock()

    def persist(progress: CooldownProgress) -> None:
        if progress.low_cpu_seconds >= 2.0:
            clock.sleep(3.0)

    with pytest.raises(RecoveryTelemetryError, match="stale"):
        wait_for_cpu_recovery(
            recovery_config(low_cpu_seconds=2.0, sample_seconds=2.0),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: 59.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            utc_now=clock.utc_now,
            on_progress=persist,
        )


@pytest.mark.parametrize(
    "sample",
    [None, True, -1.0, 101.0, float("nan"), pytest.param(10**10000, id="huge")],
)
def test_wait_fails_closed_on_invalid_cpu_samples(sample: object) -> None:
    with pytest.raises(RecoveryTelemetryError):
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: sample,  # type: ignore[return-value]
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
            utc_now=lambda: "2026-09-13T00:00:00+00:00",
            on_progress=lambda _progress: None,
        )


def test_wait_wraps_sampler_and_progress_failures_without_raw_details() -> None:
    def unavailable() -> float:
        raise OSError("postgres://secret")

    with pytest.raises(RecoveryTelemetryError, match="unavailable") as telemetry:
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=unavailable,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
            utc_now=lambda: "2026-09-13T00:00:00+00:00",
            on_progress=lambda _progress: None,
        )
    assert "secret" not in str(telemetry.value)

    with pytest.raises(RecoveryEvidenceError, match="evidence") as evidence:
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: 59.0,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
            utc_now=lambda: "2026-09-13T00:00:00+00:00",
            on_progress=lambda _progress: (_ for _ in ()).throw(
                OSError("token=secret")
            ),
        )
    assert "secret" not in str(evidence.value)


def test_wait_is_promptly_cancelled_without_sampling() -> None:
    with pytest.raises(RecoveryCancelled):
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: pytest.fail("sampler must not run"),
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
            utc_now=lambda: "2026-09-13T00:00:00+00:00",
            on_progress=lambda _progress: None,
            cancelled=lambda: True,
        )


@pytest.mark.parametrize("values", [[None], [float("nan")], [1.0, 0.0]])
def test_wait_rejects_invalid_monotonic_clock(values: list[object]) -> None:
    readings = iter(values)

    with pytest.raises(RecoveryTelemetryError, match="clock"):
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: 59.0,
            monotonic=lambda: next(readings),  # type: ignore[return-value]
            sleep=lambda _seconds: None,
            utc_now=lambda: "2026-09-13T00:00:00+00:00",
            on_progress=lambda _progress: None,
        )


def test_wait_rejects_clock_regression_after_sampling_or_before_return() -> None:
    after_sample = iter([0.0, 0.0, -1.0])
    with pytest.raises(RecoveryTelemetryError, match="clock"):
        wait_for_cpu_recovery(
            recovery_config(),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: 59.0,
            monotonic=lambda: next(after_sample),
            sleep=lambda _seconds: None,
            utc_now=lambda: "2026-09-13T00:00:00+00:00",
            on_progress=lambda _progress: None,
        )

    before_return = iter([0.0, 0.0, 0.0, 2.0, 2.0, 1.0])
    with pytest.raises(RecoveryTelemetryError, match="clock"):
        wait_for_cpu_recovery(
            recovery_config(low_cpu_seconds=2.0, sample_seconds=2.0),
            already_waited_seconds=0.0,
            series_waited_seconds=0.0,
            sampler=lambda: 59.0,
            monotonic=lambda: next(before_return),
            sleep=lambda _seconds: None,
            utc_now=lambda: "2026-09-13T00:00:00+00:00",
            on_progress=lambda _progress: None,
        )


def test_series_ownership_lock_rejects_a_second_writer_and_releases(
    tmp_path: Path,
) -> None:
    path = tmp_path / "series" / ".series.lock"
    first = SeriesOwnershipLock(path)
    second = SeriesOwnershipLock(path)
    first.acquire()

    with pytest.raises(RecoveryOwnershipError):
        second.acquire()
    with pytest.raises(RuntimeError, match="already held"):
        first.acquire()
    assert path.read_text(encoding="utf-8") == f"{os.getpid()}\n"
    assert path.stat().st_mode & 0o777 == 0o600

    first.release()
    second.acquire()
    second.release()
    second.release()

    with SeriesOwnershipLock(path):
        assert path.is_file()


def test_series_ownership_lock_closes_handle_after_acquire_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "series" / ".series.lock"
    original_fsync = os.fsync
    calls = 0

    def fail_once(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk unavailable")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_once)
    failed = SeriesOwnershipLock(path)
    with pytest.raises(OSError, match="disk unavailable"):
        failed.acquire()

    replacement = SeriesOwnershipLock(path)
    replacement.acquire()
    replacement.release()


@pytest.mark.parametrize(
    ("phase", "operation", "checkpoint_state"),
    [
        ("preflight", "host_cpu_admission", "pending"),
        ("preflight", "source_collection_preflight", "pending"),
        ("collection", "source_collection", "collecting"),
    ],
)
def test_exact_preflight_and_collection_cpu_stops_are_admitted(
    tmp_path: Path,
    phase: str,
    operation: str,
    checkpoint_state: str,
) -> None:
    series_dir, parent, child_path, child, finished = recovery_manifests(
        tmp_path,
        phase=phase,
        active_operation=operation,
        checkpoint_state=checkpoint_state,
    )

    stop = series.validate_resource_recovery_admission(
        series_dir,
        parent,
        child_path,
        child,
        source="cbre",
        now=finished,
    )

    assert stop["phase"] == phase
    assert stop["active_operation"] == operation


def test_enabled_recovery_requires_low_threshold_below_watchdog() -> None:
    with pytest.raises(ValueError, match="below the host CPU watchdog"):
        series.series_config(
            sources=("cbre",),
            page_cap=400,
            concurrency=3,
            attempts_per_source=3,
            max_resume_age_hours=24.0,
            max_host_cpu_percent=60.0,
            cpu_sustain_seconds=10.0,
            cpu_sample_seconds=2.0,
            nice=10,
            recovery=recovery_config(low_cpu_percent=60.0),
        )

    disabled = series.series_config(
        sources=("cbre",),
        page_cap=400,
        concurrency=3,
        attempts_per_source=3,
        max_resume_age_hours=24.0,
        max_host_cpu_percent=50.0,
        cpu_sustain_seconds=10.0,
        cpu_sample_seconds=2.0,
        nice=10,
        recovery=RecoveryConfig(),
    )
    assert disabled["resource_recovery"]["max_recoveries_per_source"] == 0


def test_preflight_stop_cannot_replay_a_post_collection_checkpoint(
    tmp_path: Path,
) -> None:
    series_dir, parent, child_path, child, finished = recovery_manifests(
        tmp_path,
        phase="preflight",
        active_operation="host_cpu_admission",
        checkpoint_state="validated",
    )

    with pytest.raises(series.SeriesError, match="post-collection"):
        series.validate_resource_recovery_admission(
            series_dir,
            parent,
            child_path,
            child,
            source="cbre",
            now=finished,
        )


def _set_nested(
    mapping: dict[str, object], path: tuple[str, ...], value: object
) -> None:
    target = mapping
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment]
    target[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("resource_stop", "reason_code"), "host_cpu_telemetry_failed"),
        (("resource_stop", "phase"), "ingest"),
        (("resource_stop", "active_operation"), "source_gate"),
        (("resource_stop", "telemetry_valid"), False),
        (("resource_stop", "evidence_valid"), False),
        (("resource_stop", "owned_processes_reaped"), False),
        (("resource_stop", "host_cpu_percent"), 74.9),
        (("resource_stop", "host_cpu_percent"), 10**400),
        (("sources", "cbre", "state"), "validated"),
        (("sources", "cbre", "ingest"), {}),
        (("sources", "cbre", "readback"), {}),
        (("aggregate_gate",), {}),
        (("validation",), {}),
    ],
)
def test_unsafe_or_ambiguous_stops_are_not_admitted(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
) -> None:
    series_dir, parent, child_path, original, finished = recovery_manifests(tmp_path)
    child = copy.deepcopy(original)
    _set_nested(child, path, value)

    with pytest.raises(series.SeriesError):
        series.validate_resource_recovery_admission(
            series_dir,
            parent,
            child_path,
            child,
            source="cbre",
            now=finished,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "sha",
        "config",
        "bool_alias",
        "database",
        "age",
        "timestamp",
        "future",
        "path",
        "generation",
    ],
)
def test_child_identity_age_and_target_are_exact(
    tmp_path: Path,
    mutation: str,
) -> None:
    series_dir, parent, child_path, original, finished = recovery_manifests(tmp_path)
    child = copy.deepcopy(original)
    if mutation == "sha":
        child["collector_git_sha"] = "b" * 40
    elif mutation == "config":
        child["config"]["page_cap"] = 401  # type: ignore[index]
    elif mutation == "bool_alias":
        child["config"]["source_workers"] = True  # type: ignore[index]
    elif mutation == "database":
        child["preflight"]["database_target"] = {"sha256": "e" * 64}  # type: ignore[index]
    elif mutation == "age":
        finished += timedelta(hours=25)
    elif mutation == "timestamp":
        child["resource_stop"]["recorded_at"] = (  # type: ignore[index]
            finished + timedelta(seconds=1)
        ).isoformat()
    elif mutation == "future":
        future = finished + timedelta(hours=2)
        child["resource_stop"]["occurred_at"] = (  # type: ignore[index]
            future - timedelta(seconds=2)
        ).isoformat()
        child["resource_stop"]["recorded_at"] = (  # type: ignore[index]
            future - timedelta(seconds=1)
        ).isoformat()
        child["finished_at"] = future.isoformat()
    elif mutation == "path":
        child_path = tmp_path / "outside" / "manifest.json"
    else:
        child["run_id"] = "different"

    with pytest.raises(series.SeriesError):
        series.validate_resource_recovery_admission(
            series_dir,
            parent,
            child_path,
            child,
            source="cbre",
            now=finished,
        )


def test_reconcile_uses_exact_recorded_resume_child_despite_older_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series_dir, parent, child_path, _child, _finished = recovery_manifests(tmp_path)
    checkpoint = parent["sources"]["cbre"]  # type: ignore[index]
    checkpoint["state"] = "running"
    checkpoint["attempts"] = [
        {
            "number": 2,
            "started_at": "2026-09-13T12:00:11+00:00",
            "finished_at": None,
        }
    ]
    monkeypatch.setattr(series, "publish_series_health", lambda *_args: None)

    series.reconcile_stale_running_sources(series_dir, parent)

    assert checkpoint["state"] == "resource_guard_interrupted"
    assert checkpoint["checkpoint_run"] == f"runs/{child_path.parent.name}"
    assert checkpoint["checkpoint_status"] == series.RESOURCE_GUARD_STATUS


def test_refresh_stop_contract_maps_collect_substages_without_broad_replay(
    tmp_path: Path,
) -> None:
    _series_dir, parent, _path, child, _finished = recovery_manifests(tmp_path)
    details = refresh.CpuGuardTripDetails(
        reason_code="host_cpu_sustained",
        reason="internal diagnostic that must not be persisted",
        telemetry_valid=True,
        evidence_valid=True,
        host_cpu_percent=80.0,
        occurred_at="2026-09-13T12:00:10+00:00",
        context={"phase": "collect", "active_operation": "source_collection_preflight"},
    )
    refresh_manifest = {
        "run_id": child["run_id"],
        "config": {"sources": ["cbre"]},
        "sources": {"cbre": {"state": "validated"}},
    }

    stop = refresh.resource_stop_record(refresh_manifest, details)

    assert stop["source"] == "cbre"
    assert stop["phase"] == "gate"
    assert stop["active_operation"] == "source_gate"
    assert "reason" not in stop
    assert parent["sources"]["cbre"]["state"] == (  # type: ignore[index]
        "resource_guard_interrupted"
    )


@pytest.mark.parametrize("cpu_interrupt", [True, False])
def test_serial_interrupt_reaps_child_even_when_log_evidence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cpu_interrupt: bool,
) -> None:
    log_path = tmp_path / "logs" / "command.log"
    signals: list[int] = []

    class FailingLog:
        def write(self, value: str) -> None:
            if "interrupt:" in value:
                raise OSError("required log unavailable")

        def flush(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakeProcess:
        pid = 4321

        def __init__(self) -> None:
            self.wait_calls = 0

        def wait(self, timeout: float | None = None) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                if cpu_interrupt:
                    raise refresh.CpuGuardTrip("host CPU saturated")
                raise KeyboardInterrupt
            if self.wait_calls == 2:
                raise refresh.subprocess.TimeoutExpired("collector", timeout)
            return 0

    proc = FakeProcess()
    original_open = Path.open

    def fake_open(path: Path, *args: object, **kwargs: object) -> object:
        if path == log_path:
            return FailingLog()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    monkeypatch.setattr(refresh.subprocess, "Popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(
        refresh.os,
        "killpg",
        lambda _pid, sent_signal: signals.append(sent_signal),
    )
    refresh._clear_cpu_guard_trip()
    if cpu_interrupt:
        refresh._set_cpu_guard_trip(
            "host CPU saturated",
            reason_code="host_cpu_sustained",
            host_cpu_percent=80.0,
            context={"phase": "collect", "source": "cbre"},
        )
        with pytest.raises(refresh.CpuGuardTrip):
            refresh.run_command(["collector"], log_path)
        details = refresh._peek_cpu_guard_trip_details()
        assert details is not None
        assert details.reason_code == "host_cpu_evidence_failed"
        assert details.evidence_valid is False
        assert details.owned_processes_reaped is True
    else:
        with pytest.raises(refresh.RefreshError, match="required evidence"):
            refresh.run_command(["collector"], log_path)

    assert signals == [refresh.signal.SIGINT, refresh.signal.SIGKILL]
    assert proc.wait_calls == 3
    refresh._clear_cpu_guard_trip()


def test_repeated_process_entry_keeps_one_reserved_recovery(tmp_path: Path) -> None:
    series_dir, parent, child_path, child, _finished = recovery_manifests(tmp_path)
    stop = child["resource_stop"]

    active = series._start_or_continue_cooldown(
        series_dir,
        parent,
        source="cbre",
        child_manifest_path=child_path,
        stop=stop,
    )
    active["waited_seconds"] = 12.0
    again = series._start_or_continue_cooldown(
        series_dir,
        parent,
        source="cbre",
        child_manifest_path=child_path,
        stop=stop,
    )

    assert again["waited_seconds"] == 12.0
    assert parent["resource_recovery"]["total_recoveries"] == 1  # type: ignore[index]
    assert parent["resource_recovery"]["recoveries_by_source"] == {"cbre": 1}  # type: ignore[index]


def test_repeated_operator_interrupts_consume_persisted_source_recoveries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series_dir, parent, child_path, child, _finished = recovery_manifests(tmp_path)
    parent["config"]["resource_recovery"]["max_recoveries_per_source"] = 3  # type: ignore[index]
    monkeypatch.setattr(series, "publish_series_health", lambda *_args: None)
    monkeypatch.setattr(
        series,
        "_now_utc",
        lambda: datetime(2026, 9, 13, 12, 0, 12, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        series,
        "wait_for_cpu_recovery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    for expected_count in (1, 2):
        parent["sources"]["cbre"]["state"] = "resource_guard_interrupted"  # type: ignore[index]
        rc = series._cool_down_for_child_resume(
            series_dir,
            parent,
            source="cbre",
            child_manifest_path=child_path,
            child_manifest=child,
            env_file=None,
        )

        assert rc == 130
        assert parent["resource_recovery"]["total_recoveries"] == expected_count  # type: ignore[index]
        assert parent["resource_recovery"]["state"] == "failed"  # type: ignore[index]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("resource_recovery", "state"), "cooling_down"),
        (("resource_recovery", "total_recoveries"), 1),
        (("resource_recovery", "recoveries_by_source", "cbre"), "1"),
        (("resource_recovery", "cumulative_wait_seconds"), 1801.0),
    ],
)
def test_resume_rejects_malformed_persisted_recovery_state(
    tmp_path: Path,
    path: tuple[str, ...],
    value: object,
) -> None:
    series_dir, parent, _child_path, _child, _finished = recovery_manifests(tmp_path)
    _set_nested(parent, path, value)
    manifest_path = series_dir / "manifest.json"
    manifest_path.write_text(json.dumps(parent), encoding="utf-8")

    with pytest.raises(series.SeriesError, match="resource recovery"):
        series.load_resume_manifest(
            manifest_path,
            git_sha="a" * 40,
            config=parent["config"],
            database_target={"sha256": "d" * 64},
        )


def test_cooldown_persists_progress_and_revalidates_before_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series_dir, parent, child_path, child, _finished = recovery_manifests(tmp_path)
    monkeypatch.setattr(series, "publish_series_health", lambda *_args: None)
    monkeypatch.setattr(series, "git_identity", lambda: ("a" * 40, False))
    monkeypatch.setattr(
        series,
        "database_target_fingerprint",
        lambda _env: {"sha256": "d" * 64},
    )
    monkeypatch.setattr(
        series,
        "_now_utc",
        lambda: datetime(2026, 9, 13, 12, 0, 12, tzinfo=timezone.utc),
    )

    def complete_wait(config: RecoveryConfig, **kwargs: object) -> CooldownResult:
        assert config.low_cpu_percent == 60.0
        on_progress = kwargs["on_progress"]
        on_progress(  # type: ignore[operator]
            CooldownProgress(
                waited_seconds=30.0,
                current_host_cpu_percent=55.0,
                low_cpu_seconds=30.0,
                remaining_cooldown_seconds=570.0,
                remaining_series_wait_seconds=1770.0,
                observed_at="2026-09-13T12:00:42+00:00",
            )
        )
        return CooldownResult(
            waited_seconds=30.0,
            final_host_cpu_percent=55.0,
            low_cpu_seconds=30.0,
            observed_at="2026-09-13T12:00:42+00:00",
        )

    monkeypatch.setattr(series, "wait_for_cpu_recovery", complete_wait)

    rc = series._cool_down_for_child_resume(
        series_dir,
        parent,
        source="cbre",
        child_manifest_path=child_path,
        child_manifest=child,
        env_file=None,
    )

    assert rc is None
    assert parent["status"] == "running"
    assert parent["sources"]["cbre"]["state"] == (  # type: ignore[index]
        "resource_guard_interrupted"
    )
    recovery = parent["resource_recovery"]
    assert recovery["state"] == "resuming"  # type: ignore[index]
    assert recovery["cumulative_wait_seconds"] == 30.0  # type: ignore[index]
    progress = json.loads(
        (series_dir / "resource-recovery-progress.json").read_text(encoding="utf-8")
    )
    assert progress["active"]["current_host_cpu_percent"] == 55.0
    assert progress["last_event"]["event"] == "cooldown_complete"


def test_progress_sidecar_whitelists_structured_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series_dir, parent, _child_path, _child, _finished = recovery_manifests(tmp_path)
    monkeypatch.setattr(series, "publish_series_health", lambda *_args: None)
    recovery = parent["resource_recovery"]
    recovery["state"] = "cooling_down"  # type: ignore[index]
    recovery["active"] = {  # type: ignore[index]
        "source": "cbre",
        "reason_code": "host_cpu_sustained",
        "phase": "collection",
        "active_operation": "source_collection",
        "started_at": "2026-09-13T12:00:00+00:00",
        "updated_at": "2026-09-13T12:00:02+00:00",
        "waited_seconds": 2.0,
        "preserved_detail_count": 10**400,
        "error": "postgres://user:secret@example.invalid",
        "checkpoint_run": "runs/private-token",
    }

    series.save_manifest(series_dir, parent)

    raw = (series_dir / "resource-recovery-progress.json").read_text(encoding="utf-8")
    assert "secret" not in raw
    assert "private-token" not in raw
    assert "error" not in json.loads(raw)["active"]
    assert json.loads(raw)["active"]["preserved_detail_count"] is None


def test_run_series_never_replays_an_unsafe_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    series_dir, parent, child_path, child, _finished = recovery_manifests(tmp_path)
    child["resource_stop"]["phase"] = "ingest"  # type: ignore[index]
    parent["sources"]["cbre"]["state"] = "pending"  # type: ignore[index]
    parent["sources"]["cbre"]["checkpoint_run"] = None  # type: ignore[index]
    starts: list[list[str]] = []

    class FakeProcess:
        pid = 4321

        def __init__(self, argv: list[str], **_kwargs: object) -> None:
            starts.append(argv)

        def wait(self, timeout: float | None = None) -> int:
            return 75

        def poll(self) -> int:
            return 75

    monkeypatch.setattr(series.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(series, "publish_series_health", lambda *_args: None)
    monkeypatch.setattr(series, "allocate_run_id", lambda: child_path.parent.name)
    monkeypatch.setattr(
        series,
        "_load_child_manifest",
        lambda *_args, **_kwargs: (child_path, child),
    )

    rc = series.run_series(
        series_dir,
        parent,
        env_file=None,
        retry_failed=False,
    )

    assert rc == 75
    assert len(starts) == 1
    assert parent["resource_recovery"]["total_recoveries"] == 0  # type: ignore[index]
    assert parent["resource_recovery"]["last_event"]["reason_code"] == (  # type: ignore[index]
        "recovery_not_admitted"
    )
