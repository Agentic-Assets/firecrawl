"""Pure, bounded resource diagnostics: no live processes, Docker, or network."""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import threading
from types import SimpleNamespace

import cre_resource_usage as usage
from cre_linux_telemetry import LinuxMemorySampler
import pytest

VM = "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n" + "\n".join(
    f"{key}: {index}."
    for index, key in enumerate([*usage._VM_FIELDS, *usage._VM_COUNTERS], 1)
)
CONTAINERS = [
    {
        "id": "a" * 64,
        "name": "trial-api-1",
        "image": "sha256:" + "b" * 64,
        "service": "api",
        "running": True,
        "oom_killed": False,
        "restart_count": 0,
        "cpu_limit": 4.0,
    },
    {
        "id": "c" * 64,
        "name": "trial-playwright-service-1",
        "image": "sha256:" + "d" * 64,
        "service": "playwright-service",
        "running": True,
        "oom_killed": False,
        "restart_count": 0,
        "cpu_limit": 0,
    },
]


def stats_output():
    return "\n".join(
        json.dumps(
            {
                "ID": row["id"][:12],
                "CPUPerc": "150.5%",
                "MemUsage": "1.5GiB / 8GiB",
                "NetIO": "2kB / 3MB",
                "BlockIO": "0B / 4KiB",
                "PIDs": "12",
                "Name": "private-unused-body",
            }
        )
        for row in CONTAINERS
    )


@pytest.fixture(autouse=True)
def no_live_commands(monkeypatch):
    # Existing fixtures assert the Darwin diagnostic schema on every test host.
    monkeypatch.setattr(usage.sys, "platform", "darwin")

    def refuse(*args, **kwargs):
        raise AssertionError("unexpected live diagnostic command")

    monkeypatch.setattr(usage.subprocess, "run", refuse)


@pytest.fixture
def sampled(tmp_path, monkeypatch):
    run_dir = tmp_path / "trial-run"
    run_dir.mkdir()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        key = tuple(argv)
        if key == ("/usr/bin/vm_stat",):
            output = VM
        elif argv[0] == "/usr/sbin/sysctl":
            output = {
                "hw.memsize": str(128 * 1024**3),
                "vm.swapusage": "total = 2048.00M used = 752.25M free = 1295.75M (encrypted)",
                "kern.memorystatus_vm_pressure_level": "2",
            }[argv[-1]]
        elif argv[0] == "/bin/ps":
            output = f"{os.getpid()} 1 1.5 100\n200000 {os.getpid()} 2.5 200\n300000 1 90 900\n"
        elif argv[:2] == ["docker", "stats"]:
            output = stats_output()
        elif argv[:2] == ["docker", "info"]:
            output = str(32 * 1024**3)
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout=output, stderr="private-command-error"
        )

    monkeypatch.setattr(usage.subprocess, "run", fake_run)
    monkeypatch.setattr(
        usage,
        "resolve_runtime_target",
        lambda env, **kwargs: {
            "availability": "available",
            "unavailable_code": None,
            "api_endpoint": "http://127.0.0.1:3302",
            "project": "trial",
            "containers": CONTAINERS,
        },
    )
    monkeypatch.setattr(usage.os, "getloadavg", lambda: (1.0, 2.0, 3.0))
    monkeypatch.setattr(
        usage.shutil,
        "disk_usage",
        lambda _: SimpleNamespace(total=1000, used=400, free=600),
    )
    sampler = usage.ResourceUsageSampler(
        run_dir,
        env={"COMPOSE_PROJECT_NAME": "trial", "SECRET": "private-secret"},
        cpu_sampler=lambda: 25.0,
        context_provider=lambda: {
            "source": "jll",
            "phase": "collect",
            "raw": "private-context",
        },
    )
    return sampler, calls


def test_vm_categories_and_swap_units_do_not_invent_used_memory():
    parsed = usage.parse_vm_stat(VM)
    assert parsed["page_size_bytes"] == 16384
    assert parsed["free_bytes"] == 16384
    assert parsed["compressor_bytes"] == 7 * 16384
    assert parsed["pageins"] == 8
    assert parsed["decompressions"] == 13
    assert "used_bytes" not in parsed
    assert all(
        value is None for value in usage.parse_vm_stat("provider unavailable").values()
    )
    assert usage.parse_swap("total = 2.00G used = 0.50G free = 1.50G") == {
        "total_bytes": 2 * 1024**3,
        "used_bytes": 512 * 1024**2,
        "free_bytes": 1536 * 1024**2,
    }
    assert usage.parse_swap("unavailable")["used_bytes"] is None


def test_owned_process_aggregate_excludes_unrelated_and_labels_lifetime_cpu():
    result = usage.parse_owned_processes(
        "20 1 1.5 100\n22 21 3 300\n21 20 2.5 200\n23 1 90 900\n", 20
    )
    assert result == {"count": 3, "rss_bytes": 600 * 1024, "cpu_percent_lifetime": 7.0}
    assert (
        usage.parse_owned_processes("20 1 private command arguments", 20)["count"]
        is None
    )
    assert usage.parse_owned_processes("21 1 0 10", 20)["rss_bytes"] is None


def test_docker_numeric_usage_and_exact_identity_binding():
    rows = usage.parse_docker_usage(stats_output(), CONTAINERS)
    assert rows[0]["role"] == "api"
    assert rows[0]["cpu_quota_cores"] == 4.0
    assert rows[0]["cpu_cores"] == 1.505
    assert rows[0]["memory_current_bytes"] == 1536 * 1024**2
    assert rows[0]["memory_limit_bytes"] == 8 * 1024**3
    assert rows[0]["network_rx_bytes"] == 2000
    assert rows[0]["block_write_bytes"] == 4096
    assert rows[1]["cpu_quota_cores"] is None
    assert "private" not in json.dumps(rows)
    for changed in (
        stats_output().replace("c" * 12, "e" * 12),
        stats_output().replace("c" * 12, "a" * 12),
    ):
        with pytest.raises(ValueError, match="identity"):
            usage.parse_docker_usage(changed, CONTAINERS)
    rows = usage.parse_docker_usage(
        stats_output().replace("150.5%", "NaN%"), CONTAINERS
    )
    assert rows[0]["cpu_percent"] is None


def test_real_sample_writes_secure_body_free_session_record(sampled, capsys):
    sampler, calls = sampled
    result = sampler.sample_once()
    path = sampler.run_dir / "resource-usage.jsonl"
    assert json.loads(path.read_text()) == result
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert result["schema_version"] == 1
    assert len(result["session_id"]) == 32
    assert result["elapsed_seconds"] >= 0
    assert result["sample_duration_seconds"] >= 0
    assert result["host"]["cpu_percent"] == 25.0
    assert result["host"]["memory_total_bytes"] == 128 * 1024**3
    assert result["host"]["memory_pressure"] == "warning"
    assert result["processes"]["rss_bytes"] == 300 * 1024
    assert result["processes"]["cpu_basis"] == "ps_lifetime_average_percent"
    assert result["runtime"]["project"] == "trial"
    assert result["runtime"]["docker_vm_memory_total_bytes"] == 32 * 1024**3
    assert result["context"] == {"source": "jll", "phase": "collect"}
    assert result["unavailable_fields"] == []
    assert "private" not in path.read_text()
    assert all(0 < kwargs["timeout"] <= 2.5 for _, kwargs in calls)
    assert all("args" not in " ".join(argv) for argv, _ in calls)
    assert capsys.readouterr().err == ""


def test_resumes_have_unique_sessions_not_one_downtime_average(sampled, monkeypatch):
    first, _ = sampled
    second = usage.ResourceUsageSampler(first.run_dir)
    assert first.session_id != second.session_id
    monkeypatch.setattr(usage.time, "monotonic", lambda: first.started + 2.5)
    result = first.sample_once()
    assert result["elapsed_seconds"] == 2.5


def test_optional_failures_record_null_and_fixed_degradation(
    sampled, monkeypatch, capsys
):
    sampler, _ = sampled

    def fail(*args, **kwargs):
        raise RuntimeError("private-exception")

    sampler.cpu_sampler = fail
    sampler.context_provider = fail
    monkeypatch.setattr(usage, "resolve_runtime_target", fail)
    result = sampler.sample_once()
    sampler.sample_once()
    assert result["host"]["cpu_percent"] is None
    assert result["context"] == {}
    assert result["runtime"]["unavailable_code"] == "diagnostic_failed"
    assert {"host_cpu", "context", "runtime_binding"} <= set(
        result["unavailable_fields"]
    )
    assert capsys.readouterr().err == usage.WARNING + "\n"
    assert (
        "private-exception"
        not in (sampler.run_dir / "resource-usage.jsonl").read_text()
    )


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "cap"])
def test_unsafe_or_full_journals_never_change_workflow(
    sampled, monkeypatch, capsys, kind
):
    sampler, _ = sampled
    target = sampler.run_dir / "resource-usage.jsonl"
    protected = sampler.run_dir / "protected"
    protected.write_text("unchanged")
    if kind == "symlink":
        target.symlink_to(protected)
    elif kind == "hardlink":
        target.hardlink_to(protected)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        monkeypatch.setattr(usage, "MAX_JOURNAL_BYTES", 1)
    assert sampler.sample_once() is not None
    assert protected.read_text() == "unchanged"
    assert capsys.readouterr().err == usage.WARNING + "\n"


def test_stop_during_sample_prevents_late_journal_write(sampled, monkeypatch):
    sampler, _ = sampled
    entered, release = threading.Event(), threading.Event()
    original = sampler._command

    def blocked(argv, deadline):
        if argv == ["/usr/bin/vm_stat"]:
            entered.set()
            assert release.wait(timeout=2)
        return original(argv, deadline)

    monkeypatch.setattr(sampler, "_command", blocked)
    sampler.start()
    assert entered.wait(timeout=2)
    sampler.stop_event.set()
    release.set()
    sampler.stop()
    assert not sampler.thread.is_alive()
    assert not (sampler.run_dir / "resource-usage.jsonl").exists()


@pytest.mark.parametrize("stage", ["constructor", "start", "stop"])
def test_observer_diagnostic_exception_preserves_caller_exception(
    tmp_path, monkeypatch, capsys, stage
):
    class Broken:
        def __init__(self, *args, **kwargs):
            if stage == "constructor":
                raise RuntimeError("private-constructor")

        def start(self):
            if stage == "start":
                raise RuntimeError("private-start")

        def stop(self):
            if stage == "stop":
                raise RuntimeError("private-stop")

    monkeypatch.setattr(usage, "ResourceUsageSampler", Broken)
    with (
        pytest.raises(LookupError, match="caller failure"),
        usage.observe_resource_usage(tmp_path),
    ):
        raise LookupError("caller failure")
    assert "private" not in capsys.readouterr().err


def test_command_bounds_and_stopped_sampler_never_invoke_subprocess(
    tmp_path, monkeypatch
):
    sampler = usage.ResourceUsageSampler(tmp_path)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(
            argv, 0, stdout="x" * (usage.MAX_COMMAND_BYTES + 1)
        )

    monkeypatch.setattr(usage.subprocess, "run", fake_run)
    with pytest.raises(ValueError):
        sampler._command(["safe"], usage.time.monotonic() + 100)
    assert calls[0]["timeout"] <= 2.5
    sampler.stop()
    with pytest.raises(TimeoutError):
        sampler._command(["safe"], usage.time.monotonic() + 100)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--duration-seconds", "0"),
        ("--duration-seconds", "61"),
        ("--sample-seconds", "nan"),
    ],
)
def test_read_only_cli_rejects_unbounded_duration(tmp_path, flag, value):
    with pytest.raises(SystemExit) as caught:
        usage.main(["--out-dir", str(tmp_path / "diagnostics"), flag, value])
    assert caught.value.code == 2


def test_read_only_cli_only_starts_observer(tmp_path, monkeypatch):
    received = []

    @contextlib.contextmanager
    def observer(run_dir, **kwargs):
        received.append((run_dir, kwargs))
        yield

    monkeypatch.setattr(usage, "observe_resource_usage", observer)
    monkeypatch.setattr(usage.time, "sleep", lambda seconds: received.append(seconds))
    assert (
        usage.main(
            ["--out-dir", str(tmp_path / "diagnostics"), "--duration-seconds", "1"]
        )
        == 0
    )
    assert received[0][1]["sample_seconds"] == 5
    assert received[1] == 1


def test_stopped_runtime_retains_identity_but_never_reads_usage(sampled, monkeypatch):
    sampler, calls = sampled
    stopped = [
        dict(row, running=False, oom_killed=True, restart_count=2) for row in CONTAINERS
    ]
    monkeypatch.setattr(
        usage,
        "resolve_runtime_target",
        lambda *args, **kwargs: {
            "availability": "unavailable",
            "unavailable_code": "container_not_running",
            "api_endpoint": "http://127.0.0.1:3302",
            "project": "trial",
            "containers": stopped,
        },
    )
    result = sampler.sample_once()
    assert len(result["runtime"]["containers"]) == 2
    assert result["runtime"]["containers"][0]["oom_killed"] is True
    assert result["runtime"]["containers"][0]["restart_count"] == 2
    assert result["runtime"]["containers"][0]["memory_current_bytes"] is None
    assert not any(argv[0] == "docker" for argv, _ in calls)


@pytest.fixture
def linux_sampled(sampled, tmp_path, monkeypatch):
    sampler, calls = sampled
    monkeypatch.setattr(usage.sys, "platform", "linux")
    proc = tmp_path / "proc"
    for name, text in {
        "proc/meminfo": "MemTotal: 1000000 kB\nMemAvailable: 700000 kB\nSwapTotal: 200000 kB\nSwapFree: 150000 kB\n",
        "proc/pressure/memory": "some avg10=0.00 avg60=0.00 avg300=0.00 total=100\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n",
        "proc/self/cgroup": "0::/\n",
        "proc/self/mountinfo": "30 20 0:26 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
        "sys/fs/cgroup/cgroup.controllers": "cpu memory pids\n",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="ascii")
    sampler.linux_memory_sampler = LinuxMemorySampler(proc=proc, filesystem=tmp_path)
    return sampler, calls


def test_linux_observation_has_real_units_scope_and_no_darwin_categories(
    linux_sampled, capsys
):
    sampler, calls = linux_sampled
    result = sampler.sample_once()
    host = result["host"]
    assert host["memory_total_bytes"] == 1000000 * 1024
    assert host["memory_available_bytes"] == 700000 * 1024
    assert host["swap"] == {
        "total_bytes": 200000 * 1024,
        "free_bytes": 150000 * 1024,
        "used_bytes": 50000 * 1024,
    }
    assert host["memory_pressure"] == "normal"
    assert host["vm"] == {}
    assert (
        host["memory_telemetry_scope"]
        == "linux_kernel_or_vm_and_visible_cgroups_not_windows_host"
    )
    assert result["unavailable_fields"] == []
    assert not any(
        argv[0] in {"/usr/bin/vm_stat", "/usr/sbin/sysctl"} for argv, _ in calls
    )
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "available,expected", [(100000, "warning"), (50000, "critical")]
)
def test_linux_observer_reuses_required_sampler_pressure_policy(
    linux_sampled, available, expected
):
    sampler, _ = linux_sampled
    path = sampler.linux_memory_sampler.proc / "meminfo"
    path.write_text(path.read_text().replace("700000", str(available)))
    assert sampler.sample_once()["host"]["memory_pressure"] == expected


@pytest.mark.parametrize(
    "relative", ["meminfo", "pressure/memory", "self/cgroup", "self/mountinfo"]
)
def test_missing_linux_evidence_degrades_without_darwin_fallback(
    linux_sampled, relative, capsys
):
    sampler, calls = linux_sampled
    (sampler.linux_memory_sampler.proc / relative).unlink()
    result = sampler.sample_once()
    assert result["host"]["memory_total_bytes"] is None
    assert result["host"]["memory_available_bytes"] is None
    assert result["host"]["memory_pressure"] is None
    assert result["host"]["vm"] == {}
    assert "host_memory_fields" in result["unavailable_fields"]
    assert not any("sysctl" in argv[0] or "vm_stat" in argv[0] for argv, _ in calls)
    assert capsys.readouterr().err == usage.WARNING + "\n"


@pytest.mark.parametrize(
    "replacement",
    [
        "MemTotal: private-secret kB",
        "MemTotal: 0 kB",
        "MemTotal: 1000000 MB",
        "MemTotal: 1000000 kB\nMemTotal: 1 kB",
        "MemTotal: 1 kB",
        "MemTotal: 999999999999999999999999 kB",
    ],
)
def test_malformed_linux_totals_degrade_without_secret_values(
    linux_sampled, replacement, capsys
):
    sampler, _ = linux_sampled
    path = sampler.linux_memory_sampler.proc / "meminfo"
    path.write_text(path.read_text().replace("MemTotal: 1000000 kB", replacement))
    result = sampler.sample_once()
    assert result["host"]["memory_total_bytes"] is None
    assert "host_memory_fields" in result["unavailable_fields"]
    assert (
        "private-secret" not in (sampler.run_dir / "resource-usage.jsonl").read_text()
    )
    assert capsys.readouterr().err == usage.WARNING + "\n"


@pytest.mark.parametrize("corruption", ["oversized", "swap_exceeds_total", "psi_nan"])
def test_linux_memory_size_swap_and_psi_failures_remain_unknown(
    linux_sampled, corruption
):
    sampler, _ = linux_sampled
    proc = sampler.linux_memory_sampler.proc
    if corruption == "oversized":
        (proc / "meminfo").write_text("x" * (usage.MAX_COMMAND_BYTES + 1))
    elif corruption == "swap_exceeds_total":
        p = proc / "meminfo"
        p.write_text(p.read_text().replace("SwapFree: 150000", "SwapFree: 200001"))
    else:
        p = proc / "pressure/memory"
        p.write_text(p.read_text().replace("some avg10=0.00", "some avg10=nan"))
    result = sampler.sample_once()
    assert result["host"]["memory_total_bytes"] is None
    assert result["host"]["memory_pressure"] is None
    assert all(value is None for value in result["host"]["swap"].values())
    assert "linux_memory" in result["unavailable_fields"]


def test_linux_memory_sample_past_deadline_is_unknown(linux_sampled, monkeypatch):
    sampler, calls = linux_sampled
    original = sampler.linux_memory_sampler
    clock = [1000.0]

    class SlowMemory:
        proc = original.proc

        def __call__(self):
            result = original()
            clock[0] += usage.SAMPLE_BUDGET_SECONDS + 1
            return result

    sampler.linux_memory_sampler = SlowMemory()
    monkeypatch.setattr(usage.time, "monotonic", lambda: clock[0])
    result = sampler.sample_once()
    assert result["host"]["memory_total_bytes"] is None
    assert result["host"]["memory_pressure"] is None
    assert "host_memory_fields" in result["unavailable_fields"]
    assert calls == []


def test_unknown_platform_does_not_invent_darwin_metrics(sampled, monkeypatch):
    sampler, calls = sampled
    monkeypatch.setattr(usage.sys, "platform", "unsupported")
    result = sampler.sample_once()
    assert result["host"]["memory_total_bytes"] is None
    assert result["host"]["memory_pressure"] is None
    assert "host_memory_unsupported_platform" in result["unavailable_fields"]
    assert not any("sysctl" in argv[0] or "vm_stat" in argv[0] for argv, _ in calls)


@pytest.mark.parametrize("platform,initial_wait", [("darwin", 0.1), ("linux", 2.0)])
def test_cli_cpu_samples_use_platform_counters_and_preserve_darwin_interval(
    tmp_path, monkeypatch, platform, initial_wait
):
    import cre_checkpoint_refresh as refresh

    monkeypatch.setattr(usage.sys, "platform", platform)
    sleeps = []
    monkeypatch.setattr(usage.time, "sleep", sleeps.append)

    @contextlib.contextmanager
    def observer(_run_dir, **kwargs):
        sampler = kwargs["cpu_sampler"]
        if platform == "linux":
            assert isinstance(sampler, refresh.LinuxCpuSampler)
            ticks = iter([(0,) * 8, (20, 0, 10, 70, 0, 0, 0, 0)])
        else:
            assert isinstance(sampler, refresh.DarwinCpuSampler)
            ticks = iter([(100, 200, 300, 400), (120, 210, 370, 400)])
        sampler.read_ticks = lambda: next(ticks)
        assert sampler() == pytest.approx(30.0)
        yield

    monkeypatch.setattr(usage, "observe_resource_usage", observer)
    assert (
        usage.main(["--out-dir", str(tmp_path / "cli"), "--duration-seconds", "1"]) == 0
    )
    assert sleeps == [initial_wait, 1.0]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("initial_interval", [0.1, 2.0])
def test_first_cpu_sample_keeps_docker_reads_within_shared_budget(
    sampled, monkeypatch, platform, initial_interval
):
    """The old two-second warmup exhausts Docker's remaining read budget."""
    from cre_checkpoint_refresh import host_cpu_sampler

    sampler, _ = sampled
    clock = [0.0]
    ticks = [0]
    original_run = usage.subprocess.run
    original_target = usage.resolve_runtime_target
    monkeypatch.setattr(usage.sys, "platform", platform)
    monkeypatch.setattr(usage.time, "monotonic", lambda: clock[0])

    def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(usage.time, "sleep", advance)

    def read_ticks():
        ticks[0] += 1
        n = ticks[0]
        if platform == "linux":
            return (10 * n, 0, 10 * n, 80 * n, 0, 0, 0, 0)
        return (10 * n, 10 * n, 80 * n, 0)

    sampler.cpu_sampler = host_cpu_sampler(initial_interval_seconds=initial_interval)
    monkeypatch.setattr(sampler.cpu_sampler, "read_ticks", read_ticks)

    def memory_observation(*_args):
        advance(0.01)
        return (
            32 * 1024**3,
            24 * 1024**3,
            {"total_bytes": 2 * 1024**3, "free_bytes": 2 * 1024**3, "used_bytes": 0},
            "normal",
        )

    monkeypatch.setattr(usage, "_linux_memory_observation", memory_observation)

    def resolve(*args, **kwargs):
        advance(0.1)
        return original_target(*args, **kwargs)

    def timed_run(argv, **kwargs):
        duration = 2.1 if argv[:2] == ["docker", "stats"] else 0.01
        if duration > kwargs["timeout"]:
            advance(kwargs["timeout"])
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        advance(duration)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(usage, "resolve_runtime_target", resolve)
    monkeypatch.setattr(usage.subprocess, "run", timed_run)
    first = sampler.sample_once()
    second = sampler.sample_once()
    assert usage.SAMPLE_BUDGET_SECONDS == 4.0
    assert first["unavailable_fields"] == (
        ["docker_stats", "docker_vm_memory"] if initial_interval == 2.0 else []
    )
    assert second["unavailable_fields"] == []
    assert first["sample_duration_seconds"] <= 4.0
    assert second["sample_duration_seconds"] < 3.0
    saved = [
        json.loads(line)
        for line in (sampler.run_dir / "resource-usage.jsonl").read_text().splitlines()
    ]
    assert [record["unavailable_fields"] for record in saved] == [
        first["unavailable_fields"],
        [],
    ]
