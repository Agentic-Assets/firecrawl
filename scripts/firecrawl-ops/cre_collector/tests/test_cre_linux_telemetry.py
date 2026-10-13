"""Injected Linux kernel/cgroup evidence, never the operator's host or lock."""

import cre_checkpoint_refresh as refresh
import cre_linux_telemetry as linux
import cre_memory_guard as memory
import pytest

PSI = "some avg10=0.00 avg60=0.00 avg300=0.00 total=100\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
EVENTS = "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="ascii")


@pytest.fixture
def kernel(tmp_path):
    proc = tmp_path / "proc"
    write(
        proc / "meminfo",
        "MemTotal: 1000000 kB\nMemAvailable: 700000 kB\nSwapTotal: 200000 kB\nSwapFree: 200000 kB\n",
    )
    write(proc / "pressure/memory", PSI)
    write(proc / "self/cgroup", "0::/workload\n")
    write(
        proc / "self/mountinfo", "30 20 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"
    )
    base = tmp_path / "sys/fs/cgroup"
    write(base / "cgroup.controllers", "cpu memory pids\n")
    group = base / "workload"
    write(group / "memory.current", "200000000\n")
    write(group / "memory.max", "1000000000\n")
    write(group / "memory.events", EVENTS)
    write(group / "memory.pressure", PSI)
    return linux.LinuxMemorySampler(proc=proc, filesystem=tmp_path), group


def test_cpu_aggregate_skips_guest_double_count_and_per_cpu_rows(tmp_path):
    path = tmp_path / "stat"
    write(path, "cpu 100 10 20 500 5 3 2 0 50 5\ncpu0 BAD\n")
    assert linux.read_cpu_ticks(path) == (100, 10, 20, 500, 5, 3, 2, 0)
    assert linux.cpu_percent((0,) * 8, (20, 0, 10, 60, 10, 0, 0, 0)) == 30


@pytest.mark.parametrize(
    "text",
    [
        "",
        "cpu0 1 2 3 4 5 6 7 8",
        "cpu 1 2 3",
        "cpu 1 2 -3 4 5 6 7 8",
        "cpu 1 2 x 4 5 6 7 8",
        "cpu " + "1 " * 1000,
    ],
)
def test_cpu_invalid_aggregate_fails_closed(tmp_path, text):
    path = tmp_path / "stat"
    write(path, text)
    with pytest.raises(linux.LinuxTelemetryError):
        linux.read_cpu_ticks(path)


@pytest.mark.parametrize(
    "before,after",
    [
        ((1,) * 8, (1,) * 8),
        ((2,) * 8, (1,) * 8),
        ((2**32 - 1,) * 8, (0,) * 8),
        ((1,) * 8, (2,) * 7),
        ((True,) * 8, (2,) * 8),
    ],
)
def test_cpu_zero_reset_wrap_shape_and_invalid_counters_fail_closed(before, after):
    with pytest.raises(linux.LinuxTelemetryError):
        linux.cpu_percent(before, after)


def test_linux_sampler_first_and_recurring_delta(monkeypatch):
    ticks = iter([(0,) * 8, (20, 0, 10, 70, 0, 0, 0, 0), (60, 0, 30, 110, 0, 0, 0, 0)])
    sleeps = []
    monkeypatch.setattr(refresh.time, "sleep", sleeps.append)
    sampler = refresh.LinuxCpuSampler(
        read_ticks=lambda: next(ticks), initial_interval_seconds=2
    )
    assert sampler() == 30
    assert sampler() == 60
    assert sleeps == [2]


def test_linux_sampler_adapts_missing_evidence_to_guard_error(monkeypatch):
    def unavailable():
        raise linux.LinuxTelemetryError("unavailable")

    with pytest.raises(refresh.CpuTelemetryError):
        refresh.LinuxCpuSampler(read_ticks=unavailable)()


@pytest.mark.parametrize(
    "platform,expected",
    [
        ("darwin", refresh.DarwinCpuSampler),
        ("linux", refresh.LinuxCpuSampler),
        ("linux2", refresh.LinuxCpuSampler),
    ],
)
def test_cpu_platform_selection(monkeypatch, platform, expected):
    monkeypatch.setattr(refresh.sys, "platform", platform)
    assert isinstance(refresh.host_cpu_sampler(), expected)


def test_unsupported_platform_cpu_admission_retains_exit_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(refresh.sys, "platform", "win32")
    instance = refresh.HostCpuGuard(log_path=tmp_path / "cpu.jsonl")
    refresh._clear_cpu_guard_trip()
    try:
        with pytest.raises(refresh.CpuGuardTrip):
            refresh.run_cpu_guard_preflight(instance)
        assert (
            refresh._peek_cpu_guard_trip_details().reason_code
            == "host_cpu_telemetry_failed"
        )
        with pytest.raises(memory.MemoryTelemetryError):
            # The conftest replaces the dispatcher for offline main-path tests.
            REAL_READ_MEMORY()
    finally:
        refresh._clear_cpu_guard_trip()


REAL_READ_MEMORY = memory.read_memory_pressure


def test_normal_linux_memory_and_old_swap_are_not_pressure(kernel):
    sampler, _ = kernel
    write(
        sampler.proc / "meminfo",
        "MemTotal: 1000000 kB\nMemAvailable: 700000 kB\nSwapTotal: 200000 kB\nSwapFree: 0 kB\n",
    )
    assert sampler() == 1
    assert sampler() == 1


@pytest.mark.parametrize(
    "available,level", [(100001, 1), (100000, 2), (50000, 4), (0, 4)]
)
def test_available_memory_threshold_boundaries(kernel, available, level):
    sampler, _ = kernel
    write(
        sampler.proc / "meminfo",
        f"MemTotal: 1000000 kB\nMemAvailable: {available} kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n",
    )
    assert sampler() == level


def test_low_available_and_exhausted_swap_warn(kernel):
    sampler, _ = kernel
    write(
        sampler.proc / "meminfo",
        "MemTotal: 1000000 kB\nMemAvailable: 200000 kB\nSwapTotal: 100000 kB\nSwapFree: 10000 kB\n",
    )
    assert sampler() == 2


@pytest.mark.parametrize(
    "some,full,level", [(9.99, 0, 1), (10, 0, 2), (10, 1.99, 2), (2, 2, 4)]
)
@pytest.mark.parametrize("location", ["host", "cgroup"])
def test_psi_warning_and_critical(kernel, some, full, level, location):
    sampler, group = kernel
    path = (
        sampler.proc / "pressure/memory"
        if location == "host"
        else group / "memory.pressure"
    )
    write(
        path,
        PSI.replace("some avg10=0.00", f"some avg10={some}").replace(
            "full avg10=0.00", f"full avg10={full}"
        ),
    )
    assert sampler() == level


@pytest.mark.parametrize(
    "current,level", [(899999999, 1), (900000000, 2), (950000000, 4), (1000000001, 4)]
)
def test_cgroup_limit_usage_thresholds(kernel, current, level):
    sampler, group = kernel
    write(group / "memory.current", str(current))
    assert sampler() == level


def test_visible_ancestor_limit_also_protects_siblings(kernel):
    sampler, group = kernel
    base = group.parent
    for name in ("memory.current", "memory.max", "memory.events", "memory.pressure"):
        write(base / name, (group / name).read_text())
    write(base / "memory.current", "960000000")
    assert sampler() == 4


def test_namespaced_membership_maps_to_mount_root(kernel):
    sampler, group = kernel
    write(sampler.proc / "self/cgroup", "0::/\n")
    write(
        sampler.proc / "self/mountinfo",
        "30 20 0:26 /docker/abc /sys/fs/cgroup/workload rw - cgroup2 cgroup rw\n",
    )
    assert linux.cgroup_directories(sampler.proc, sampler.filesystem) == [group]
    assert sampler() == 1


def test_namespaced_root_cannot_hide_missing_memory_current(kernel):
    sampler, group = kernel
    write(sampler.proc / "self/cgroup", "0::/\n")
    write(
        sampler.proc / "self/mountinfo",
        "30 20 0:26 /docker/abc /sys/fs/cgroup/workload rw - cgroup2 cgroup rw\n",
    )
    write(group / "cgroup.controllers", "memory cpu\n")
    write(group / "cgroup.type", "domain\n")
    (group / "memory.current").unlink()
    with pytest.raises(linux.LinuxTelemetryError, match="controller unavailable"):
        sampler()


@pytest.mark.parametrize(
    "counter,level", [("high", 2), ("max", 2), ("oom", 4), ("oom_kill", 4)]
)
def test_new_cgroup_events_trip_but_historical_events_do_not(kernel, counter, level):
    sampler, group = kernel
    write(group / "memory.events", EVENTS.replace(f"{counter} 0", f"{counter} 5"))
    assert sampler() == 1
    write(group / "memory.events", EVENTS.replace(f"{counter} 0", f"{counter} 6"))
    assert sampler() == level
    write(group / "memory.events", EVENTS)
    with pytest.raises(linux.LinuxTelemetryError, match="backwards"):
        sampler()


@pytest.mark.parametrize(
    "target,value",
    [
        ("meminfo", "MemTotal: 1 kB\n"),
        (
            "meminfo",
            "MemTotal: 1 kB\nMemAvailable: 2 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB",
        ),
        ("pressure/memory", PSI.replace("0.00", "nan", 1)),
        ("pressure/memory", PSI.replace("some", "full", 1)),
        ("self/cgroup", "2:memory:/workload\n"),
        ("self/cgroup", "0::/../escape\n"),
        ("self/mountinfo", "invalid"),
    ],
)
def test_malformed_required_kernel_evidence_fails_closed(kernel, target, value):
    sampler, _ = kernel
    write(sampler.proc / target, value)
    with pytest.raises(linux.LinuxTelemetryError):
        sampler()


@pytest.mark.parametrize(
    "name,value",
    [
        ("memory.max", "0"),
        ("memory.max", "-1"),
        ("memory.current", "broken"),
        ("memory.pressure", ""),
        ("memory.events", "high 0"),
    ],
)
def test_malformed_cgroup_evidence_fails_closed(kernel, name, value):
    sampler, group = kernel
    write(group / name, value)
    with pytest.raises(linux.LinuxTelemetryError):
        sampler()


@pytest.mark.parametrize(
    "name", ["memory.current", "memory.max", "memory.pressure", "memory.events"]
)
def test_missing_cgroup_evidence_fails_closed(kernel, name):
    sampler, group = kernel
    (group / name).unlink()
    with pytest.raises(linux.LinuxTelemetryError):
        sampler()


def test_missing_cpu_file_fails_closed(tmp_path):
    with pytest.raises(linux.LinuxTelemetryError):
        linux.read_cpu_ticks(tmp_path / "missing")


@pytest.mark.parametrize(
    "target", ["meminfo", "pressure/memory", "self/cgroup", "self/mountinfo"]
)
def test_missing_kernel_memory_evidence_fails_closed(kernel, target):
    sampler, _ = kernel
    (sampler.proc / target).unlink()
    with pytest.raises(linux.LinuxTelemetryError):
        sampler()


def test_changed_membership_fails_closed_after_baseline(kernel):
    sampler, group = kernel
    assert sampler() == 1
    other = group.parent / "other"
    for name in ("memory.current", "memory.max", "memory.events", "memory.pressure"):
        write(other / name, (group / name).read_text())
    write(sampler.proc / "self/cgroup", "0::/other\n")
    with pytest.raises(linux.LinuxTelemetryError, match="membership changed"):
        sampler()


def test_root_empty_event_baseline_cannot_hide_membership_change(kernel):
    sampler, _ = kernel
    write(sampler.proc / "self/cgroup", "0::/\n")
    assert sampler() == 1
    assert sampler.previous_events == {}
    write(sampler.proc / "self/cgroup", "0::/workload\n")
    with pytest.raises(linux.LinuxTelemetryError, match="membership changed"):
        sampler()


def test_unlimited_cgroup_keeps_host_and_psi_guards(kernel):
    sampler, group = kernel
    write(group / "memory.max", "max\n")
    assert sampler() == 1
    write(group / "memory.pressure", PSI.replace("some avg10=0.00", "some avg10=10.00"))
    assert sampler() == 2


def test_oversized_required_evidence_fails_closed(kernel):
    sampler, _ = kernel
    write(sampler.proc / "meminfo", "x" * 65537)
    with pytest.raises(linux.LinuxTelemetryError, match="size bound"):
        sampler()


def test_linux_policy_scope_and_darwin_policy_unchanged(monkeypatch):
    monkeypatch.setattr(memory.sys, "platform", "linux")
    assert "not_windows_host" in memory.memory_guard_config()["telemetry_scope"]
    monkeypatch.setattr(memory.sys, "platform", "darwin")
    config = memory.memory_guard_config()
    assert (
        config["basis"]
        == "darwin_dispatch_pressure_level_1_normal_2_warning_4_critical"
    )
    assert "telemetry_scope" not in config
