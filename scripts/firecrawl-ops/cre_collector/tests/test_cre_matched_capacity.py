"""No network/Docker/DB: real coordinator scheduling and owned-child faults."""

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import cre_matched_capacity as matched

ActivitySampler = matched.RuntimeActivitySampler
ResourceEvidence = matched.resource_evidence


@pytest.fixture(autouse=True)
def no_live_activity(monkeypatch):
    monkeypatch.setattr(
        matched, "RuntimeActivitySampler", lambda *_a, **_k: nullcontext()
    )
    monkeypatch.setattr(
        matched,
        "resource_evidence",
        lambda *_a, **_k: {"measurement_complete_for_qualification": True},
    )
    matched.refresh._clear_cpu_guard_trip()
    yield
    matched.refresh._clear_cpu_guard_trip()


@pytest.fixture
def inputs(tmp_path):
    tmp_path.chmod(0o700)
    containers = []
    limits = {
        "cpu_limit": 2,
        "memory_limit_bytes": 8 * 1024**3,
        "memory_swap_limit_bytes": 0,
        "pids_limit": 384,
        "shm_size_bytes": 8 * 1024**3,
    }
    for index, service in enumerate(("api", "playwright-service"), 1):
        containers.append(
            dict(
                id=str(index) * 64,
                image="sha256:" + "a" * 64,
                project="firecrawl-cre-test",
                service=service,
                restart_policy={"Name": "no", "MaximumRetryCount": 0},
                started_at="2026-10-12T00:00:00Z",
                restart_count=0,
                running=True,
                restarting=False,
                dead=False,
                oom_killed=False,
                exit_code=0,
                finished_at="0001-01-01T00:00:00Z",
                ports={},
                **limits,
            )
        )
    identity = {
        "schema_version": 1,
        "kind": "cre_owned_runtime",
        "project": "firecrawl-cre-test",
        "daemon": {
            "context": "test",
            "endpoint": "unix:///tmp/docker.sock",
            "id": "daemon",
            "version": "27",
        },
        "containers": containers,
    }
    profile = {
        "schema_version": 1,
        "kind": "cre_matched_capacity_profile",
        "project": identity["project"],
        "review_reference": "synthetic-reviewed-fixture",
        "api_queue_cap": 4,
        "browser_page_cap": 4,
        "services": {
            service: copy.deepcopy(limits) for service in ("api", "playwright-service")
        },
    }
    cohort = {
        "cohortSha256": "a" * 64,
        "cohort": {
            "source": "marcus-millichap",
            "inventoryStartedAt": "2026-10-12T00:00:00Z",
            "members": [{} for _ in range(16)],
        },
    }
    args = SimpleNamespace(
        out=tmp_path,
        total_seconds=1800,
        cell_seconds=300,
        replicates=1,
        max_p95_ratio=1.5,
        api_url="http://127.0.0.1:3302",
        browser_url="http://127.0.0.1:3303",
        inventory=tmp_path / "original-inventory.json",
        max_inventory_age_hours=2,
        paired_jll=False,
        prepare_marcus=False,
    )
    observed = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    rows = [
        {
            "id": str(index),
            "url": f"https://www.marcusmillichap.com/properties/synthetic-{index}",
            "sourceKey": "marcus-millichap",
            "inventoryObservedAt": observed,
        }
        for index in range(16)
    ]
    raw = json.dumps(
        {
            "runMeta": {
                "mode": "monitor",
                "startedAt": observed,
                "finishedAt": observed,
            },
            "listings": rows,
        }
    ).encode()
    matched.write_bytes(args.inventory, raw)
    cohort["cohort"].update(
        inventorySha256=hashlib.sha256(raw).hexdigest(),
        inventoryStartedAt=observed,
        inventoryFinishedAt=observed,
        members=[
            {"providerId": row["id"], "canonicalUrl": row["url"], "baseRow": row}
            for row in rows
        ],
    )
    return args, cohort, profile, identity


def fake_runner(calls, *, fail=None):
    def run(argv, log_path, *, env, timeout):
        assert 0 < timeout <= 300
        body = matched.read_json(log_path.parent / "input.json")
        width = body["concurrency"]
        phase = "detail" if argv[0] == "node" else "dry_run"
        calls.append((width, phase, copy.deepcopy(body), dict(env)))
        if fail == (width, phase):
            return 1
        if phase == "detail":
            matched.write_json(
                log_path.parent / "cell.json",
                {
                    "cohortSha256": body["cohortSha256"],
                    "generationId": body["generationId"],
                    "requestedConcurrency": width,
                    "peakActiveEnrichments": width,
                    "latencyMs": {"p95": 100},
                    "rows": [],
                },
            )
        else:
            assert "--dry-run" in argv and "--require-strict-freshness" in argv
            assert not {
                "--mark-missing",
                "--apply",
                "--env-file",
                "--activate-status",
            } & set(argv)
        return 0

    return run


def fake_admission(*_args):
    return {"api_queue_cap": 4, "browser_page_cap": 4}


def test_real_coordinator_cold_widths_generations_baselines_and_dry_runs(inputs):
    args, cohort, profile, identity = inputs
    args.replicates = 3
    calls, admissions = [], []

    def admit(*values):
        admissions.append(values[-2])
        return fake_admission()

    cells = matched.run_cells(
        args,
        cohort,
        profile,
        identity,
        None,
        {},
        SimpleNamespace(incident_context={}),
        runner=fake_runner(calls),
        admission=admit,
    )
    assert [(cell["replicate"], cell["concurrency"]) for cell in cells] == [
        (r, c) for r in (1, 2, 3) for c in (1, 2, 4, 8)
    ]
    assert len(admissions) == 24
    assert len({cell["generation"] for cell in cells}) == 12
    assert [width for width, phase, *_ in calls if phase == "dry_run"] == [
        1,
        2,
        4,
        8,
    ] * 3
    details = [body for _, phase, body, _ in calls if phase == "detail"]
    assert "baseline" not in details[0]
    assert all(
        body["baseline"]["generationId"] == details[0]["generationId"]
        for body in details[1:]
    )
    assert all(body["cohort"] == cohort["cohort"] for body in details)
    assert all(
        env["CRE_REFRESH_STARTED_AT"] == cohort["cohort"]["inventoryStartedAt"]
        for *_, env in calls
    )
    assert all(
        list((args.out / cell["cell"]).parent.glob("cache/*")) == [] for cell in cells
    )


@pytest.mark.parametrize("phase", ["detail", "dry_run"])
def test_first_worker_or_sql_failure_stops_later_cells(inputs, phase):
    args, cohort, profile, identity = inputs
    calls = []
    with pytest.raises(matched.CalibrationRejected):
        matched.run_cells(
            args,
            cohort,
            profile,
            identity,
            None,
            {},
            SimpleNamespace(incident_context={}),
            runner=fake_runner(calls, fail=(2, phase)),
            admission=fake_admission,
        )
    assert max(width for width, *_ in calls) == 2


@pytest.mark.parametrize("fault", ["idle", "timeout", "binding", "latency"])
def test_faults_stop_before_c4(inputs, fault):
    args, cohort, profile, identity = inputs
    calls = []
    runner = fake_runner(calls)

    def run(argv, log, **values):
        body = matched.read_json(log.parent / "input.json")
        if fault == "timeout" and body["concurrency"] == 2:
            raise subprocess.TimeoutExpired("synthetic", 1)
        rc = runner(argv, log, **values)
        if (
            argv[0] == "node"
            and body["concurrency"] == 2
            and fault in {"binding", "latency"}
        ):
            result = matched.read_json(log.parent / "cell.json")
            result["peakActiveEnrichments" if fault == "binding" else "latencyMs"] = (
                1 if fault == "binding" else {"p95": 151}
            )
            (log.parent / "cell.json").unlink()
            matched.write_json(log.parent / "cell.json", result)
        return rc

    admissions = 0

    def admit(*values):
        nonlocal admissions
        admissions += 1
        if fault == "idle" and admissions == 4:
            raise matched.owned.RuntimeUnavailable("not idle")
        return fake_admission()

    with pytest.raises(
        (
            matched.CalibrationRejected,
            matched.owned.RuntimeUnavailable,
            subprocess.TimeoutExpired,
        )
    ):
        matched.run_cells(
            args,
            cohort,
            profile,
            identity,
            None,
            {},
            SimpleNamespace(incident_context={}),
            runner=run,
            admission=admit,
        )
    assert max(width for width, *_ in calls) <= 2


def test_whole_deadline_prevents_even_first_child(inputs):
    args, cohort, profile, identity = inputs
    with pytest.raises(TimeoutError):
        matched.run_cells(
            args,
            cohort,
            profile,
            identity,
            None,
            {},
            SimpleNamespace(),
            runner=lambda *_a, **_k: pytest.fail("must not launch"),
            deadline=1,
            clock=lambda: 1,
        )


def test_private_io_rejects_links_public_files_and_overwrite(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "input.json"
    matched.write_json(path, {"synthetic": True})
    assert matched.read_json(path) == {"synthetic": True}
    with pytest.raises(FileExistsError):
        matched.write_json(path, {})
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        matched.read_json(link)
    path.chmod(0o644)
    with pytest.raises(matched.CalibrationRejected):
        matched.read_json(path)


def test_no_write_environment_strips_database_and_activation(monkeypatch):
    for key in (
        "DATABASE_URL",
        "PGPASSWORD",
        "SUPABASE_KEY",
        "CRE_ACTIVATE_STATUS",
        "CRE_ENV_FILE",
    ):
        monkeypatch.setenv(key, "synthetic-secret")
    env = matched.no_write_env("http://127.0.0.1:3302")
    assert (
        not {
            "DATABASE_URL",
            "PGPASSWORD",
            "SUPABASE_KEY",
            "CRE_ACTIVATE_STATUS",
            "CRE_ENV_FILE",
        }
        & env.keys()
    )
    assert env["API_URL"] == env["FIRECRAWL_API_URL"] == "http://127.0.0.1:3302"
    assert env["CRE_SCRAPE_MAX_ATTEMPTS"] == "1"


def test_profile_is_exact_and_complete(inputs):
    _, cohort, profile, identity = inputs
    matched.validate_inputs(cohort, profile, identity)
    del profile["services"]["api"]["pids_limit"]
    with pytest.raises(matched.CalibrationRejected):
        matched.validate_inputs(cohort, profile, identity)


def test_settings_match_ids_epochs_limits_and_health_caps(inputs, monkeypatch):
    args, _, profile, identity = inputs
    monkeypatch.setattr(matched.owned, "admit_runtime", lambda *_a: None)
    snapshot = {
        "availability": "available",
        "containers": copy.deepcopy(identity["containers"]),
    }
    monkeypatch.setattr(
        matched, "capture_runtime_configuration", lambda *_a, **_k: snapshot
    )
    monkeypatch.setattr(
        matched.owned,
        "read_health",
        lambda url, _d: (
            {"maxConcurrency": 4}
            if "queue-status" in url
            else {"maxConcurrentPages": 4}
        ),
    )
    matched.check_runtime(
        None, identity, profile, args.api_url, args.browser_url, args.out, {}
    )
    snapshot["containers"][0]["cpu_limit"] = 4
    with pytest.raises(matched.CalibrationRejected):
        matched.check_runtime(
            None, identity, profile, args.api_url, args.browser_url, args.out, {}
        )


def test_real_owned_child_success_and_timeout_cleanup(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    assert (
        matched.bounded_child(
            [sys.executable, "-c", "pass"],
            tmp_path / "ok.log",
            env=os.environ,
            timeout=5,
        )
        == 0
    )
    original = matched.settle_owned_groups
    groups = []

    def settle(values, **kwargs):
        groups.extend(values)
        return original(values, **{**kwargs, "grace_seconds": 0.1, "kill_seconds": 0.5})

    monkeypatch.setattr(matched, "settle_owned_groups", settle)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        matched.bounded_child(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            tmp_path / "timeout.log",
            env=os.environ,
            timeout=0.1,
        )
    assert time.monotonic() - started < 3
    assert groups[-1].gone and groups[-1].process.returncode is not None


@pytest.mark.parametrize(
    "failure",
    [
        "guard",
        "timeout",
        "unknown",
        "stop_failure",
        "stop_exception",
        "stop_evidence",
        "ordinary",
    ],
)
def test_failure_retains_real_temporary_recovery_lease(inputs, monkeypatch, failure):
    args, cohort, profile, identity = inputs
    lock = args.out / ".test.lock"
    events = []

    class Guard:
        def __init__(self, *_a, **_k):
            self.incident_context = {}
            if str(_a[0]).endswith("host-cpu.jsonl"):
                assert _k == {
                    "max_percent": 90,
                    "sustain_seconds": 10,
                    "sample_seconds": 2,
                }
            else:
                assert _k["sustain_seconds"] == 20 and _k["sample_seconds"] == 2

        def preflight(self):
            events.append("memory_preflight")

        def start(self):
            events.append("start")

    monkeypatch.setattr(matched.refresh, "checkpoint_lock_dir", lambda _v: lock)
    monkeypatch.setattr(matched.refresh, "HostCpuGuard", Guard)
    monkeypatch.setattr(matched, "HostMemoryGuard", Guard)
    monkeypatch.setattr(
        matched.refresh,
        "run_cpu_guard_preflight",
        lambda _g: events.append("cpu_preflight") or 20,
    )
    monkeypatch.setattr(
        matched.refresh, "resource_guard_boundary", lambda *_a: nullcontext()
    )
    monkeypatch.setattr(
        matched, "observe_resource_usage", lambda *_a, **_k: nullcontext()
    )
    monkeypatch.setattr(matched.owned, "runtime_lease", lambda _i: nullcontext())
    monkeypatch.setattr(matched, "check_runtime", lambda *_a: {})
    stop_calls = []

    def stop(*_a, **_k):
        stop_calls.append(True)
        if failure == "stop_exception":
            raise OSError("synthetic Docker cleanup unavailable")
        return {"ok": failure != "stop_failure"}

    monkeypatch.setattr(matched.owned, "stop_runtime", stop)
    if failure == "stop_evidence":
        original_write = matched.write_json

        def write(path, value):
            if path.name == "stop.json":
                raise OSError("synthetic stop evidence disk failure")
            original_write(path, value)

        monkeypatch.setattr(matched, "write_json", write)
    error = {
        "guard": matched.refresh.CpuGuardTrip("synthetic"),
        "timeout": TimeoutError(),
        "unknown": matched.refresh.ProcessCleanupUnknown("synthetic"),
        "stop_failure": ValueError(),
        "stop_exception": ValueError(),
        "stop_evidence": ValueError(),
        "ordinary": ValueError(),
    }[failure]

    def fail(*_a, **_k):
        raise error

    monkeypatch.setattr(matched, "run_cells", fail)
    with pytest.raises(
        ValueError if failure == "ordinary" else matched.refresh.ProcessCleanupUnknown
    ):
        matched.execute(args, cohort, profile, identity)
    assert stop_calls == [True]
    assert events[:3] == ["memory_preflight", "start", "cpu_preflight"]
    terminal = matched.read_json(args.out / "failure.json")
    assert terminal["recovery_fence_required"] is (failure != "ordinary")
    if failure in {"stop_exception", "stop_evidence"}:
        assert (
            terminal["code"] == "cleanup_unknown"
            and terminal["remote_cleanup"] == "unresolved"
        )
    if failure == "ordinary":
        assert not lock.exists()
        with matched.refresh.SharedLock(lock):
            pass
        return
    assert lock.exists()
    with pytest.raises(matched.refresh.LockHeldError), matched.refresh.SharedLock(lock):
        pytest.fail("operator recovery fence must prevent overlap")


def test_lock_contention_never_inspects_or_stops_runtime(inputs, monkeypatch):
    args, cohort, profile, identity = inputs
    lock = args.out / ".test.lock"
    monkeypatch.setattr(matched.refresh, "checkpoint_lock_dir", lambda _v: lock)
    monkeypatch.setattr(
        matched.owned,
        "runtime_lease",
        lambda _i: pytest.fail("must not reach runtime lease"),
    )
    monkeypatch.setattr(
        matched.owned,
        "stop_runtime",
        lambda *_a, **_k: pytest.fail("must not stop another run"),
    )
    with matched.refresh.SharedLock(lock), pytest.raises(matched.refresh.LockHeldError):
        matched.execute(args, cohort, profile, identity)


def test_plan_never_constructs_runtime_or_output(inputs, monkeypatch):
    args, cohort, profile, identity = inputs
    paths = []
    for name, value in (
        ("cohort", cohort),
        ("profile", profile),
        ("identity", identity),
    ):
        path = args.out / f"{name}.json"
        matched.write_json(path, value)
        paths.extend([f"--{name}", str(path)])
    monkeypatch.setattr(
        matched, "execute", lambda *_a: pytest.fail("plan must not execute")
    )
    output = args.out / "2026-10-12T100000Z-abcdefabcdef"
    assert (
        matched.main(
            [
                *paths,
                "--inventory",
                str(args.inventory),
                "--api-url",
                args.api_url,
                "--browser-url",
                args.browser_url,
                "--out",
                str(output),
                "--max-p95-ratio",
                "1.5",
            ]
        )
        == 0
    )
    assert not output.exists()


@pytest.mark.parametrize("fault", ["old", "full", "enriched", "changed_member", "hash"])
def test_original_inventory_provenance_rejects_false_freshness(inputs, fault):
    args, cohort, *_ = inputs
    raw = matched.read_bytes(args.inventory)
    artifact = json.loads(raw)
    if fault == "old":
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        artifact["runMeta"]["startedAt"] = old
        cohort["cohort"]["inventoryStartedAt"] = old
    elif fault == "full":
        artifact["runMeta"]["mode"] = "full"
    elif fault == "enriched":
        artifact["listings"][0]["detailObservedAt"] = cohort["cohort"][
            "inventoryStartedAt"
        ]
        cohort["cohort"]["members"][0]["baseRow"] = copy.deepcopy(
            artifact["listings"][0]
        )
    elif fault == "changed_member":
        artifact["listings"][0]["name"] = "Changed source row"
    raw = json.dumps(artifact).encode()
    if fault == "hash":
        raw += b" "
    if fault != "hash":
        cohort["cohort"]["inventorySha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(matched.CalibrationRejected):
        matched.verify_inventory(cohort, raw, 2)


def test_activity_samples_lower_bounds_and_sanitizes_failures(tmp_path):
    tmp_path.chmod(0o700)
    values = iter([0, 3, 1])
    active = 0

    def health(url, deadline):
        nonlocal active
        assert deadline > time.monotonic()
        if "queue-status" in url:
            active = next(values)
            return {
                "jobsInQueue": active,
                "activeJobsInQueue": active,
                "waitingJobsInQueue": 0,
            }
        return {"status": "healthy", "activePages": active}

    sampler = ActivitySampler(
        tmp_path,
        "http://127.0.0.1:3302",
        "http://127.0.0.1:3303",
        health=health,
        interval=60,
    )
    with sampler:
        sampler.sample()
        sampler.sample()
    summary = matched.read_json(tmp_path / "runtime-activity.summary.json")
    assert summary["sampled_high_water_lower_bounds"]["activePages"] == 3
    assert summary["samples"] == 3
    assert "can_miss" in summary["coverage"]


def test_activity_failure_stops_scope_and_keeps_evidence(tmp_path):
    tmp_path.chmod(0o700)
    fail = False

    def health(url, _deadline):
        if fail:
            raise ValueError("private response data must not be emitted")
        return (
            {"jobsInQueue": 0, "activeJobsInQueue": 0, "waitingJobsInQueue": 0}
            if "queue-status" in url
            else {"status": "healthy", "activePages": 0}
        )

    sampler = ActivitySampler(
        tmp_path,
        "http://127.0.0.1:3302",
        "http://127.0.0.1:3303",
        health=health,
        interval=60,
    )
    with pytest.raises(matched.CalibrationRejected), sampler:
        fail = True
        sampler.sample()
    assert (
        matched.read_json(tmp_path / "runtime-activity.summary.json")["status"]
        == "unavailable"
    )
    assert "private response" not in (tmp_path / "runtime-activity.jsonl").read_text()


def test_disk_full_settlement_and_log_close_preserve_unknown(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    original = matched.os.fdopen

    class FailingClose:
        def __init__(self, file):
            self.file = file

        def __getattr__(self, key):
            return getattr(self.file, key)

        def close(self):
            self.file.close()
            raise KeyboardInterrupt("synthetic close interruption")

    monkeypatch.setattr(
        matched.os, "fdopen", lambda fd, mode: FailingClose(original(fd, mode))
    )

    def no_space(*_a, **_k):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(matched, "write_json", no_space)
    with pytest.raises(matched.refresh.ProcessCleanupUnknown):
        matched.bounded_child(
            [sys.executable, "-c", "pass"],
            tmp_path / "child.log",
            env=os.environ,
            timeout=5,
        )


@pytest.mark.parametrize(
    "fault",
    [
        "healthy",
        "missing",
        "wrong_ids",
        "degraded",
        "gap",
        "future",
        "late_start",
        "stale_end",
        "idle_exploratory",
        "idle_qualifying",
    ],
)
def test_required_resource_evidence_distinguishes_load_from_idle(
    inputs, monkeypatch, fault
):
    args, _, _, identity = inputs
    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=5)
    sample_at = now - timedelta(seconds=4)
    result = {
        "startedAt": (now - timedelta(seconds=4)).isoformat(),
        "finishedAt": (now - timedelta(seconds=3)).isoformat(),
    }
    if fault.startswith("idle"):
        sample_at = now - timedelta(seconds=1)
    if fault == "late_start":
        start = now - timedelta(seconds=20)
    if fault == "stale_end":
        start = now - timedelta(seconds=30)
        sample_at = now - timedelta(seconds=25)
    if fault == "gap":
        start = now - timedelta(seconds=20)
        sample_at = now - timedelta(seconds=15)
    record = {
        "kind": "cre_resource_usage",
        "observed_at": sample_at.isoformat(),
        "sample_duration_seconds": 1,
        "host": {
            "cpu_percent": 20,
            "memory_total_bytes": 128 * 1024**3,
            "vm": {"compressor_bytes": 0},
            "swap": {"used_bytes": 0},
            "memory_pressure": "normal",
        },
        "runtime": {
            "availability": "available",
            "containers": [
                {"id": row["id"], "cpu_percent": 10, "memory_current_bytes": 1024}
                for row in identity["containers"]
            ],
        },
    }
    if fault == "wrong_ids":
        record["runtime"]["containers"][0]["id"] = "f" * 64
    if fault == "degraded":
        record["host"]["cpu_percent"] = None
    if fault == "future":
        record["observed_at"] = (now + timedelta(seconds=60)).isoformat()
    records = [record]
    if fault == "gap":
        records.append(
            {
                **copy.deepcopy(record),
                "observed_at": (sample_at + timedelta(seconds=11)).isoformat(),
            }
        )
    if fault != "missing":
        matched.write_bytes(
            args.out / "resource-usage.jsonl",
            b"".join((json.dumps(row) + "\n").encode() for row in records),
        )
    cell = args.out / "cell"
    cell.mkdir(mode=0o700)
    if fault in {"healthy", "idle_exploratory"}:
        proof = ResourceEvidence(
            args.out,
            cell,
            identity,
            result,
            start.isoformat(),
            qualifying=False,
            work_deadline=time.monotonic(),
        )
        assert proof["measurement_complete_for_qualification"] is (fault == "healthy")
    else:
        with pytest.raises(matched.CalibrationRejected):
            ResourceEvidence(
                args.out,
                cell,
                identity,
                result,
                start.isoformat(),
                qualifying=fault == "idle_qualifying",
                work_deadline=time.monotonic(),
            )


def test_activity_finalizer_preserves_unknown_through_disk_failure(
    tmp_path, monkeypatch
):
    tmp_path.chmod(0o700)
    health = lambda url, _d: (
        {"data": {"jobsInQueue": 0, "activeJobsInQueue": 0, "waitingJobsInQueue": 0}}
        if "queue-status" in url
        else {"status": "healthy", "activePages": 0}
    )
    unknown = matched.refresh.ProcessCleanupUnknown("synthetic unresolved group")
    sampler = ActivitySampler(
        tmp_path, "http://127.0.0.1:3302", "http://127.0.0.1:3303", health=health
    )

    def no_disk(*_a, **_k):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(matched, "write_json", no_disk)
    with pytest.raises(matched.refresh.ProcessCleanupUnknown) as caught, sampler:
        raise unknown
    assert caught.value is unknown
    assert (
        matched.chained_error(caught.value, matched.refresh.ProcessCleanupUnknown)
        is unknown
    )


def test_real_coordinator_marker_survives_abrupt_owner_death(inputs):
    args, cohort, profile, identity = inputs
    # This isolated process creates only a short-lived owned sleeping child,
    # never a provider/runtime request. The actual execute path must arm first.
    body = args.out / "abrupt-input.json"
    matched.write_json(
        body, {"cohort": cohort, "profile": profile, "identity": identity}
    )
    script = r"""
import json, os, subprocess, sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import cre_matched_capacity as m
root = Path(sys.argv[1]); data = json.loads((root/'abrupt-input.json').read_text())
class Guard:
    def __init__(self, *a, **k): self.incident_context = {}
    def preflight(self): pass
    def start(self): pass
m.refresh.checkpoint_lock_dir = lambda _: root/'.abrupt.lock'
m.refresh.HostCpuGuard = m.HostMemoryGuard = Guard
m.refresh.run_cpu_guard_preflight = lambda _: 20
m.refresh.resource_guard_boundary = lambda *a: nullcontext()
m.observe_resource_usage = lambda *a, **k: nullcontext()
m.owned.runtime_lease = lambda _: nullcontext()
m.check_runtime = lambda *a: {}
def die(*a, **k):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (root/'child.pid').write_text(str(child.pid))
    os._exit(70)
m.run_cells = die
args = SimpleNamespace(out=root,total_seconds=60,api_url='http://127.0.0.1:3302',browser_url='http://127.0.0.1:3303')
m.execute(args, data['cohort'], data['profile'], data['identity'])
"""
    owner = subprocess.run(
        [sys.executable, "-c", script, str(args.out)],
        cwd=matched.refresh.COLLECTOR_DIR,
        timeout=8,
        capture_output=True,
        check=False,
    )
    assert owner.returncode == 70
    child = int((args.out / "child.pid").read_text())
    os.kill(child, 0)  # alive at successor attempt
    with (
        pytest.raises(matched.refresh.LockHeldError),
        matched.refresh.SharedLock(args.out / ".abrupt.lock"),
    ):
        pytest.fail("durable active marker must veto dead-owner reclamation")


def test_marker_failure_dispatches_no_provider_child(inputs, monkeypatch):
    args, cohort, profile, identity = inputs

    class Guard:
        def __init__(self, *_a, **_k):
            self.incident_context = {}

        def preflight(self):
            pass

        def start(self):
            pass

    lock = args.out / ".marker.lock"
    monkeypatch.setattr(matched.refresh, "checkpoint_lock_dir", lambda _: lock)
    monkeypatch.setattr(matched.refresh, "HostCpuGuard", Guard)
    monkeypatch.setattr(matched, "HostMemoryGuard", Guard)
    monkeypatch.setattr(matched.refresh, "run_cpu_guard_preflight", lambda _: 20)
    monkeypatch.setattr(
        matched.refresh, "resource_guard_boundary", lambda *_a: nullcontext()
    )
    monkeypatch.setattr(
        matched, "observe_resource_usage", lambda *_a, **_k: nullcontext()
    )
    monkeypatch.setattr(matched.owned, "runtime_lease", lambda _: nullcontext())
    monkeypatch.setattr(matched, "check_runtime", lambda *_a: {})
    monkeypatch.setattr(matched.owned, "stop_runtime", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(
        matched,
        "run_cells",
        lambda *_a, **_k: pytest.fail("marker must precede all child dispatch"),
    )

    def fail_marker(*_a):
        raise OSError("synthetic marker fsync failure")

    monkeypatch.setattr(matched.refresh.SharedLock, "arm_benchmark", fail_marker)
    with pytest.raises(matched.refresh.ProcessCleanupUnknown):
        matched.execute(args, cohort, profile, identity)
    assert (
        matched.read_json(args.out / "failure.json")["recovery_fence_required"] is True
    )


def test_coordinator_paired_worker_uses_empty_output_child_and_checks_both_measurements(
    inputs, monkeypatch
):
    args, cohort, profile, identity = inputs
    args.paired_jll = True
    for index, member in enumerate(cohort["cohort"]["members"]):
        member["sampleId"] = f"sample-{index}"
    detail = fake_runner([])
    seen, measured = [], []

    def measurements(_run, _cell, _identity, result, *_a, **_k):
        measured.append(result.get("kind", "rendered"))
        return {
            "measurement_complete_for_qualification": result.get("kind") != "direct"
        }

    monkeypatch.setattr(matched, "resource_evidence", measurements)

    def runner(argv, log_path, *, env, timeout):
        if argv[-1] != "sources/jll-direct-pair-worker.ts":
            return detail(argv, log_path, env=env, timeout=timeout)
        parent = log_path.parent
        output = matched.Path(env["CRE_JLL_PAIR_OUTPUT"])
        assert output == parent / "output"
        assert not list(output.iterdir())
        assert (parent / "input.json").exists()
        request = matched.read_json(parent / "input.json")
        seen.append(request["requestedConcurrency"])
        matched.write_json(
            output / "pair.json",
            {
                "status": "paired_equivalent",
                "cohortSha256": cohort["cohortSha256"],
                "requestedConcurrency": request["requestedConcurrency"],
                "directGenerationId": request["directGenerationId"],
                "pairs": [
                    {"sampleId": member["sampleId"]}
                    for member in cohort["cohort"]["members"]
                ],
                "directArm": {
                    "kind": "direct",
                    "startedAt": "preserved-original",
                    "finishedAt": "preserved-original",
                },
            },
        )
        return 0

    cells = matched.run_cells(
        args,
        cohort,
        profile,
        identity,
        None,
        {},
        SimpleNamespace(incident_context={}),
        runner=runner,
        admission=fake_admission,
    )
    assert seen == [1, 2, 4, 8]
    assert measured == ["rendered", "direct"] * 4
    assert all(
        not cell["resource_measurements"]["measurement_complete_for_qualification"]
        for cell in cells
    )


def test_supplied_jll_cohort_cannot_bypass_twenty_four_member_bound(inputs):
    _, cohort, profile, identity = inputs
    cohort["cohort"]["source"] = "jll"
    cohort["cohort"]["members"] = [{} for _ in range(25)]
    with pytest.raises(matched.CalibrationRejected):
        matched.validate_inputs(cohort, profile, identity)
