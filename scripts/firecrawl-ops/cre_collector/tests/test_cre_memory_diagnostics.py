"""Exact Linux guard inputs from synthetic files, never live kernel evidence."""

import copy
import json
from collections import Counter

import cre_checkpoint_series as series
import cre_linux_telemetry as linux
import cre_memory_guard as memory
import pytest
from test_cre_linux_telemetry import EVENTS, PSI, write
from test_cre_linux_telemetry import kernel as kernel

REAL_READ_MEMORY = memory.read_memory_pressure


@pytest.mark.parametrize(
    "target,value,level,cause",
    [
        ("available", 100000, 2, "host_available_warning"),
        ("available", 50000, 4, "host_available_critical"),
        ("swap", 10000, 2, "host_swap_low"),
        ("host_some", 10, 2, "host_psi_some"),
        ("host_full", 2, 2, "host_psi_full"),
        ("cgroup_some", 10, 2, "cgroup_psi_some"),
        ("cgroup_full", 2, 2, "cgroup_psi_full"),
        ("usage", 900000000, 2, "cgroup_usage_warning"),
        ("usage", 950000000, 4, "cgroup_usage_critical"),
    ],
)
def test_exact_validated_inputs_explain_each_pressure_branch(
    kernel, target, value, level, cause
):
    sampler, group = kernel
    if target in {"available", "swap"}:
        available = value if target == "available" else 200000
        write(
            sampler.proc / "meminfo",
            f"MemTotal: 1000000 kB\nMemAvailable: {available} kB\n"
            f"SwapTotal: 100000 kB\nSwapFree: {value if target == 'swap' else 100000} kB\n",
        )
    elif target == "usage":
        write(group / "memory.current", str(value))
    else:
        path = (
            sampler.proc / "pressure/memory"
            if target.startswith("host")
            else group / "memory.pressure"
        )
        text = PSI.replace("some avg10=0.00", f"some avg10={value}")
        if target.endswith("full"):
            text = text.replace("full avg10=0.00", f"full avg10={value}")
        write(path, text)
    assert sampler() == level
    evidence = sampler.last_sample
    assert evidence["pressure_level"] == level
    assert cause in evidence["causes"]
    assert str(sampler.filesystem) not in json.dumps(evidence)
    assert all(
        isinstance(entry["ancestor_index"], int) for entry in evidence["cgroups"]
    )


@pytest.mark.parametrize(
    "counter,level", [("high", 2), ("max", 2), ("oom", 4), ("oom_kill", 4)]
)
def test_event_evidence_retains_delta_and_not_historical_count(kernel, counter, level):
    sampler, group = kernel
    write(group / "memory.events", EVENTS.replace(f"{counter} 0", f"{counter} 5"))
    assert sampler() == 1
    assert sampler.last_sample["cgroups"][0]["event_deltas"] is None
    write(group / "memory.events", EVENTS.replace(f"{counter} 0", f"{counter} 6"))
    assert sampler() == level
    entry = sampler.last_sample["cgroups"][0]
    assert entry["events"][counter] == 6
    assert entry["event_deltas"][counter] == 1
    assert f"cgroup_event_{counter}" in entry["causes"]
    assert sampler() == 1
    assert sampler.last_sample["causes"] == []


def test_simultaneous_causes_and_exact_numbers_are_not_collapsed(kernel):
    sampler, group = kernel
    assert sampler() == 1
    write(sampler.proc / "pressure/memory", PSI.replace("avg10=0.00", "avg10=2.00"))
    write(group / "memory.current", "960000000")
    write(group / "memory.events", EVENTS.replace("oom 0", "oom 1"))
    assert sampler() == 4
    evidence = sampler.last_sample
    assert {"host_psi_full", "cgroup_usage_critical", "cgroup_event_oom"} <= set(
        evidence["causes"]
    )
    assert evidence["host"]["psi_full_avg10_percent"] == 2
    assert evidence["host"]["memory_available_bytes"] == 700000 * 1024
    assert evidence["cgroups"][0]["current_bytes"] == 960000000
    assert evidence["cgroups"][0]["limit_bytes"] == 1000000000


@pytest.mark.parametrize("failure", ["meminfo", "psi", "counter_backwards"])
def test_failed_read_clears_prior_valid_sample(kernel, failure):
    sampler, group = kernel
    assert sampler() == 1
    assert sampler.last_sample is not None
    if failure == "counter_backwards":
        write(group / "memory.events", EVENTS.replace("oom 0", "oom 1"))
        assert sampler() == 4
        write(group / "memory.events", EVENTS)
    elif failure == "psi":
        write(sampler.proc / "pressure/memory", "invalid")
    else:
        (sampler.proc / "meminfo").unlink()
    with pytest.raises(linux.LinuxTelemetryError):
        sampler()
    assert sampler.last_sample is None


def test_snapshot_adds_no_kernel_reads_and_owns_its_counter_values(kernel, monkeypatch):
    sampler, _ = kernel
    reads = []
    original = linux._read

    def read(path, *args):
        reads.append(path)
        return original(path, *args)

    monkeypatch.setattr(linux, "_read", read)
    assert sampler() == 1
    assert Counter(path.name for path in reads) == Counter(
        {
            "meminfo": 1,
            "memory": 1,
            "cgroup": 1,
            "mountinfo": 1,
            "memory.current": 1,
            "memory.max": 1,
            "memory.pressure": 1,
            "memory.events": 1,
            "cgroup.controllers": 1,
        }
    )
    snapshot = copy.deepcopy(sampler.last_sample)
    assert sampler() == 1
    assert snapshot["cgroups"][0]["event_deltas"] is None
    sampler.last_sample["cgroups"][0]["events"]["oom"] = 999
    assert sampler() == 1


def test_snapshot_is_bounded_by_existing_ancestry_and_numeric_limits(kernel):
    sampler, group = kernel
    base = group.parent
    relative = "/" + "/".join(["private-scope", *(["x"] * 62)])
    write(sampler.proc / "self/cgroup", f"0::{relative}\n")
    leaf = base / relative.lstrip("/")
    directories = [leaf, *leaf.parents]
    for directory in directories[:64]:
        write(directory / "memory.current", "200000000")
        write(directory / "memory.max", "1000000000")
        write(directory / "memory.events", EVENTS)
        write(directory / "memory.pressure", PSI)
    assert sampler() == 1
    maximum = "9" * 24
    for directory in directories[:64]:
        write(directory / "memory.current", maximum)
        write(
            directory / "memory.events",
            "\n".join(
                f"{name} {maximum}" for name in ("high", "max", "oom", "oom_kill")
            ),
        )
    assert sampler() == 4
    assert len(sampler.last_sample["cgroups"]) == 64
    encoded = json.dumps(sampler.last_sample)
    assert len(encoded) < 65536
    assert "private-scope" not in encoded
    write(sampler.proc / "self/cgroup", f"0::{relative}/extra\n")
    with pytest.raises(linux.LinuxTelemetryError, match="ancestry exceeds bound"):
        sampler()
    assert sampler.last_sample is None


def actual_linux_guard(tmp_path, kernel, monkeypatch):
    sampler, _ = kernel
    monkeypatch.setattr(memory.sys, "platform", "linux")
    monkeypatch.setattr(memory, "read_memory_pressure", REAL_READ_MEMORY)
    trips = []
    guard = memory.HostMemoryGuard(
        tmp_path / "guard.jsonl", lambda **value: trips.append(value)
    )
    guard.linux_sampler = sampler
    return guard, trips


@pytest.mark.parametrize("available,level", [(100000, 2), (50000, 4)])
def test_warning_and_trip_journals_contain_exact_original_sample(
    tmp_path, kernel, monkeypatch, available, level
):
    guard, trips = actual_linux_guard(tmp_path, kernel, monkeypatch)
    assert guard.preflight() == 1
    assert "linux_memory_sample" not in json.loads(guard.log_path.read_text())
    sampler, _ = kernel
    write(
        sampler.proc / "meminfo",
        f"MemTotal: 1000000 kB\nMemAvailable: {available} kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
    )
    assert guard.sample_once() == level
    record = json.loads(guard.log_path.read_text().splitlines()[-1])
    assert record["linux_memory_sample"] == sampler.last_sample
    assert (
        record["linux_memory_sample"]["host"]["memory_available_bytes"]
        == available * 1024
    )
    assert bool(trips) == (level == 4)
    write(sampler.proc / "meminfo", "invalid")
    guard._write(pressure=level, reason_code=record["reason_code"])
    assert (
        json.loads(guard.log_path.read_text().splitlines()[-1])["linux_memory_sample"]
        == record["linux_memory_sample"]
    )


def test_guard_failed_sample_does_not_attach_previous_warning(
    tmp_path, kernel, monkeypatch
):
    guard, _ = actual_linux_guard(tmp_path, kernel, monkeypatch)
    sampler, _ = kernel
    write(
        sampler.proc / "meminfo",
        "MemTotal: 1000000 kB\nMemAvailable: 100000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
    )
    assert guard.sample_once() == 2
    (sampler.proc / "meminfo").unlink()
    with pytest.raises(memory.MemoryTelemetryError):
        guard.sample_once()
    guard._trip("host_memory_telemetry_failed", pressure=None, telemetry_valid=False)
    assert "linux_memory_sample" not in json.loads(
        guard.log_path.read_text().splitlines()[-1]
    )


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_custom_sampler_never_publishes_linux_diagnostics(
    tmp_path, kernel, monkeypatch, platform
):
    monkeypatch.setattr(memory.sys, "platform", platform)
    sampler, _ = kernel
    sampler()
    original = copy.deepcopy(sampler.last_sample)
    guard = memory.HostMemoryGuard(
        tmp_path / "custom.jsonl", lambda **value: None, sampler=lambda: 4
    )
    guard.linux_sampler = sampler
    assert guard.sample_once() == 4
    assert "linux_memory_sample" not in json.loads(guard.log_path.read_text())
    assert sampler.last_sample == original


def test_default_darwin_tick_is_unchanged_and_does_not_touch_linux(
    tmp_path, kernel, monkeypatch
):
    monkeypatch.setattr(memory.sys, "platform", "darwin")
    monkeypatch.setattr(memory, "read_memory_pressure", REAL_READ_MEMORY)
    monkeypatch.setattr(memory, "read_darwin_memory_pressure", lambda **kwargs: 2)
    sampler, _ = kernel
    sampler()
    original = copy.deepcopy(sampler.last_sample)
    guard = memory.HostMemoryGuard(tmp_path / "darwin.jsonl", lambda **value: None)
    guard.linux_sampler = sampler
    assert guard.sample_once() == 2
    assert set(json.loads(guard.log_path.read_text())) == {
        "schema_version",
        "kind",
        "observed_at",
        "pressure_level",
        "state",
        "reason_code",
        "context",
    }
    assert sampler.last_sample == original


def test_guard_stale_read_and_stale_interval_do_not_publish_diagnostics(
    tmp_path, kernel, monkeypatch
):
    guard, _ = actual_linux_guard(tmp_path, kernel, monkeypatch)
    sampler, _ = kernel
    write(
        sampler.proc / "meminfo",
        "MemTotal: 1000000 kB\nMemAvailable: 100000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
    )
    for previous, clocks in [(None, [0, 2]), (0, [10, 10.1])]:
        guard.previous_observed = previous
        values = iter(clocks)
        monkeypatch.setattr(memory.time, "monotonic", lambda: next(values))
        with pytest.raises(memory.MemoryTelemetryError, match="stale"):
            guard.sample_once()
        assert guard.linux_sample_evidence is None
        guard._write(pressure=2, reason_code="host_memory_telemetry_failed")
        assert "linux_memory_sample" not in json.loads(
            guard.log_path.read_text().splitlines()[-1]
        )


def test_parent_nonadmission_is_resource_neutral_and_preserves_child_cause(
    tmp_path, monkeypatch
):
    manifest = {
        "resource_recovery": {"schema_version": 1},
        "sources": {
            "jll": {
                "error": "host memory pressure guard stopped the checkpoint run",
                "resource_stop": {"reason_code": "host_memory_critical"},
            }
        },
    }
    original = copy.deepcopy(manifest["sources"]["jll"])
    saved = []
    monkeypatch.setattr(
        series, "save_manifest", lambda path, data: saved.append(copy.deepcopy(data))
    )
    assert (
        series._record_recovery_nonadmission(
            tmp_path,
            manifest,
            source="jll",
            reason_code="recovery_disabled",
            exhausted=True,
        )
        == 75
    )
    assert (
        manifest["error"]
        == "resource guard stop was not admitted for automatic recovery"
    )
    for key, value in original.items():
        assert manifest["sources"]["jll"][key] == value
    assert (
        saved[-1]["resource_recovery"]["last_event"]["reason_code"]
        == "recovery_disabled"
    )
