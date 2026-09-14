"""Pure, no-network contracts for the JLL capacity benchmark adapter."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import cre_capacity_benchmark as benchmark
import cre_capacity_experiment as experiment
import pytest


def _cache_record(index: int) -> dict[str, object]:
    transaction = ["sale", "rent", "both"][index % 3]
    tenures = ["sale", "rent"] if transaction == "both" else [transaction]
    property_type = ["office", "industrial", "land", "retail", "medical"][index % 5]
    image_count = 12 if index % 4 == 0 else index % 3
    property_value = {
        "id": index,
        "pageUrl": f"https://property.jll.com/listings/property-{index}",
        "tenureTypes": tenures,
        "propertyTypes": [property_type],
        "images": [
            f"https://assets.example/{index}/{item}.jpg" for item in range(image_count)
        ],
        "brochures": [f"https://assets.example/{index}/brochure.pdf"]
        if index % 2
        else [],
        "floorPlans": {"images": [], "files": []},
        "videos": [],
        "virtualTours": [],
        "view360URLs": [],
    }
    filler = "x" * (index * 17)
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps({"props": {"pageProps": {"property": property_value}}})
        + "</script>"
        + filler
    )
    return {
        "url": property_value["pageUrl"],
        "cachedAt": "2026-09-13T00:00:00Z",
        "detailObservedAt": "2026-09-13T00:00:00Z",
        "rawHtml": html,
    }


def _sample(tmp_path: Path) -> dict[str, object]:
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(180):
        (cache / f"{index:04}.json").write_text(
            json.dumps(_cache_record(index)), encoding="utf-8"
        )
    return benchmark.build_sample(cache)


def test_build_sample_is_exact_unique_and_spans_declared_strata(tmp_path: Path) -> None:
    sample = _sample(tmp_path)

    assert sample["source"] == "jll"
    assert len(sample["details"]) == 128
    assert len({row["id"] for row in sample["details"]}) == 128
    assert sample["coverage"]["supports_sale"] > 0
    assert sample["coverage"]["supports_lease"] > 0
    assert len(sample["coverage"]["primary_property_types"]) == 5
    assert {"light", "heavy"}.issubset(sample["coverage"]["page_weight_bands"])
    assert "not a population-weighted estimate" in sample["representation_claim"]


def test_validate_sample_rejects_jll_investor_and_duplicate_identity(
    tmp_path: Path,
) -> None:
    sample = _sample(tmp_path)
    sample["source"] = "jll-investor"
    with pytest.raises(benchmark.BenchmarkError, match="never JLL Investor"):
        benchmark.validate_sample(sample)

    sample["source"] = "jll"
    sample["details"][1]["id"] = sample["details"][0]["id"]
    sample["details"][1]["sample_id"] = hashlib.sha256(
        f"{sample['details'][1]['id']}\0{sample['details'][1]['url']}".encode()
    ).hexdigest()[:24]
    sample["inventory_sha256"] = hashlib.sha256(
        json.dumps(
            [(row["id"], row["url"]) for row in sample["details"]],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with pytest.raises(benchmark.BenchmarkError, match="unique"):
        benchmark.validate_sample(sample)


def test_validate_sample_rejects_non_jll_url_and_missing_provenance(
    tmp_path: Path,
) -> None:
    sample = _sample(tmp_path)
    original_url = sample["details"][0]["url"]
    sample["details"][0]["url"] = "https://example.com/listings/not-jll"
    with pytest.raises(benchmark.BenchmarkError, match="not a JLL listing"):
        benchmark.validate_sample(sample)

    sample = json.loads(json.dumps(sample))
    sample["details"][0]["url"] = original_url
    sample["details"][0]["historic"].pop("raw_html_sha256")
    with pytest.raises(benchmark.BenchmarkError, match="historic provenance"):
        benchmark.validate_sample(sample)


def test_verify_sample_provenance_rechecks_selected_cache_records(
    tmp_path: Path,
) -> None:
    sample = _sample(tmp_path)
    receipt = benchmark.verify_sample_provenance(sample)
    assert receipt["verified_records"] == 128

    cache_file = (
        Path(sample["population"]["cache_directory"])
        / sample["details"][0]["historic"]["cache_file"]
    )
    cache_file.write_text(cache_file.read_text() + " ", encoding="utf-8")
    with pytest.raises(benchmark.BenchmarkError, match="does not match cache"):
        benchmark.verify_sample_provenance(sample)


def test_dry_plan_never_claims_execution(tmp_path: Path) -> None:
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    value = benchmark.plan(profile, "bold-jll-128", digest, _sample(tmp_path))

    assert value["sample"]["validated"] is True
    assert value["safety"]["live_run_performed"] is False
    assert value["execution"]["startable"] is False
    assert "explicit_run_flag_required" in value["execution"]["blockers"]


def test_scrub_environment_removes_database_credentials_but_keeps_local_api() -> None:
    cleaned = benchmark.scrub_environment(
        {
            "POSTGRES_URL": "secret",
            "SUPABASE_KEY": "secret",
            "PGPASSWORD": "secret",
            "DB_URL": "secret",
            "NUQ_DATABASE_URL": "secret",
            "REDIS_URL": "secret",
            "OPENAI_API_KEY": "secret",
            "CRE_ENV_FILE": "/secret",
            "FIRECRAWL_API_URL": "http://localhost:3102",
            "PATH": "/bin",
        }
    )

    assert cleaned == {"PATH": "/bin"}


def _runtime_public(source_sha: str, variant: str = "candidate") -> dict[str, object]:
    contract = benchmark._experiment_contract()
    requested = contract["requested"][variant]
    digests = {
        key: f"{index:x}" * 64
        for index, key in enumerate(benchmark.capacity_runtime.EXECUTION_INPUTS, 1)
    }
    public: dict[str, object] = {
        "repo": {
            "git_sha": source_sha,
            "dirty": False,
            "compose_sha256": "a" * 64,
            "override_sha256": "b" * 64,
            "execution_inputs_sha256": digests,
        },
        "host": {
            "orb_status": "Running",
            "orbstack_memory_mib": 32768,
            "docker_context": "orbstack",
            "docker_memtotal_bytes": 33_669_808_128,
        },
        "api": {
            "image": "sha256:api-image",
            "env": {
                "keys_sha256": "c" * 64,
                "values_sha256": "d" * 64,
                "excluding_pages_sha256": "d" * 64,
            },
            "nano_cpus": requested["api_cpus"] * 1_000_000_000,
            "memory_bytes": 8_589_934_592,
            "swap_bytes": 0,
            "cgroup_memory_max": 8_589_934_592,
            "cgroup_memory_current": 1_073_741_824,
            "cgroup_swap_max": 0,
            "port_bindings": {"3002/tcp": [{"HostPort": "3102"}]},
            "network_mode": "firecrawl_backend",
            "mounts_sha256": "e" * 64,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
        },
        "browser": {
            "image": "sha256:browser-image",
            "env": {
                "keys_sha256": "f" * 64,
                "values_sha256": ("1" if variant == "baseline" else "2") * 64,
                "excluding_pages_sha256": "3" * 64,
            },
            "nano_cpus": requested["browser_cpus"] * 1_000_000_000,
            "page_slots": str(requested["global_pages"]),
            "pids_limit": requested["browser_pids"],
            "memory_bytes": 17_179_869_184,
            "swap_bytes": 0,
            "cgroup_memory_max": 17_179_869_184,
            "cgroup_memory_current": 2_147_483_648,
            "cgroup_swap_max": 0,
            "shm_bytes": 8_408_530_944,
            "port_bindings": {
                "3000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3103"}]
            },
            "network_mode": "firecrawl_backend",
            "mount_count": 0,
            "mounts_sha256": "4" * 64,
            "security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"],
        },
        "settlement": {
            "api": {"active": 0, "waiting": 0, "total": 0},
            "api_root_status": 200,
            "browser_root_status": 404,
            "active_crawls": 0,
            "rabbitmq_queue_count": 1,
            "rabbitmq_ready": 0,
            "rabbitmq_unacknowledged": 0,
            "nuq": {
                "queue_scrape_total": 0,
                "queue_scrape_backlog_total": 0,
                "queue_crawl_finished_total": 0,
            },
            "cre_process_active": False,
        },
    }
    public["transition_sha256"] = benchmark.capacity_runtime.transition_fingerprint(
        public
    )
    public["snapshot_sha256"] = benchmark.capacity_runtime.snapshot_fingerprint(public)
    return public


def _admission(tmp_path: Path, *, source_sha: str = "a" * 40) -> dict[str, object]:
    contract = benchmark._experiment_contract()
    nonce = "f" * 64
    return {
        "schema_version": 1,
        "kind": benchmark.ADMISSION_KIND,
        "admitted": True,
        "profile": contract["profiles"]["candidate"],
        "config_sha256": contract["config_sha256"],
        "source_git_sha": source_sha,
        "transition_receipt_sha256": "9" * 64,
        "review_approval_nonce_sha256": nonce,
        "review_benchmark_grant_path": str(
            tmp_path / f".cre-capacity-benchmark-grant-{nonce}.json"
        ),
        "created_at": "2026-09-13T01:00:00+00:00",
        "review_approval_created_at": "2026-09-13T01:00:00+00:00",
        "expires_after_seconds": 600,
        "writes": "forbidden",
        "checks": {"candidate": True, "preserved": True},
        "effective": _runtime_public(source_sha),
    }


def _review_grant(admission: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": benchmark.capacity_runtime.BENCHMARK_GRANT_KIND,
        "profile": admission["profile"],
        "config_sha256": admission["config_sha256"],
        "transition_receipt_sha256": admission["transition_receipt_sha256"],
        "source_git_sha": admission["source_git_sha"],
        "review_approval_nonce_sha256": admission["review_approval_nonce_sha256"],
        "review_approval_created_at": admission["review_approval_created_at"],
        "expires_after_seconds": 600,
        "approved": True,
    }


def _audit_file(tmp_path: Path, admission: dict[str, object]) -> Path:
    path = tmp_path / "admission-audit.json"
    benchmark._atomic_private_json(
        path,
        {
            "admission_sha256": hashlib.sha256(
                benchmark._canonical(admission)
            ).hexdigest(),
            "review_benchmark_grant_sha256": "8" * 64,
            "review_approval_created_at": admission["review_approval_created_at"],
            "expires_after_seconds": 600,
        },
    )
    return path


def test_validate_admission_requires_exact_profile_and_idle_loopback() -> None:
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    source_sha = "a" * 40
    receipt = _admission(Path("/private/review-grants"), source_sha=source_sha)

    validated = benchmark.validate_admission(
        receipt,
        profile,
        "bold-jll-128",
        digest,
        source_git_sha=source_sha,
        now=benchmark.datetime(2026, 9, 13, 1, 5, tzinfo=benchmark.UTC),
    )
    assert validated["endpoints"]["api_url"] == "http://127.0.0.1:3102"
    receipt["checks"] = {"candidate": True, "preserved": False}
    with pytest.raises(benchmark.BenchmarkError, match="all true"):
        benchmark.validate_admission(
            receipt,
            profile,
            "bold-jll-128",
            digest,
            source_git_sha=source_sha,
            now=benchmark.datetime(2026, 9, 13, 1, 5, tzinfo=benchmark.UTC),
        )


@pytest.mark.parametrize(
    ("key", "invalid", "message"),
    [
        ("schema_version", True, "kind is invalid"),
        ("config_sha256", "a" * 40, "profile/config"),
        ("transition_receipt_sha256", "a" * 40, "transition receipt"),
        ("review_approval_nonce_sha256", "a" * 40, "review approval"),
        ("source_git_sha", "a" * 39, "source SHA"),
    ],
)
def test_validate_admission_rejects_nonexact_schema_and_digest_lengths(
    key: str,
    invalid: object,
    message: str,
) -> None:
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    receipt = _admission(Path("/private/review-grants"))
    receipt[key] = invalid

    with pytest.raises(benchmark.BenchmarkError, match=message):
        benchmark.validate_admission(
            receipt,
            profile,
            "bold-jll-128",
            digest,
            source_git_sha="a" * 40,
            now=benchmark.datetime(2026, 9, 13, 1, 5, tzinfo=benchmark.UTC),
        )


def test_loopback_admission_rejects_remote_or_credentialed_endpoint() -> None:
    with pytest.raises(benchmark.BenchmarkError, match="loopback"):
        benchmark._loopback_url("https://example.com", "API URL")
    with pytest.raises(benchmark.BenchmarkError, match="credentials"):
        benchmark._loopback_url("http://user:pass@localhost:3102", "API URL")


def test_live_admission_rejects_changed_container_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile, _digest = experiment.load_profile(
        experiment.DEFAULT_CONFIG, "bold-jll-128"
    )
    admitted = {
        "repo": {"git_sha": "a" * 40, "dirty": False},
        "host": {},
        "api": {"nano_cpus": 2_000_000_000},
        "browser": {"nano_cpus": 6_000_000_000},
    }
    live = {
        **admitted,
        "api": {"nano_cpus": 1_000_000_000},
        "settlement": {"cre_process_active": True},
    }
    live["transition_sha256"] = benchmark.capacity_runtime.transition_fingerprint(live)
    live["snapshot_sha256"] = benchmark.capacity_runtime.snapshot_fingerprint(live)
    capture = benchmark.capacity_runtime.RuntimeCapture(live, {}, {})
    monkeypatch.setattr(benchmark.capacity_runtime, "capture_runtime", lambda: capture)

    with pytest.raises(benchmark.BenchmarkError, match="live transition state changed"):
        benchmark.verify_live_admission({"effective": admitted}, profile)


def test_other_collector_process_check_ignores_only_own_ancestry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own = benchmark.os.getpid()
    process_output = (
        f"{own} 42 python cre_capacity_benchmark.py\n"
        "42 1 zsh launch-cre_capacity_benchmark\n"
        "900 1 node collect.ts\n"
    )
    completed = benchmark.subprocess.CompletedProcess([], 0, process_output, "")
    monkeypatch.setattr(
        benchmark.subprocess, "run", lambda *_args, **_kwargs: completed
    )

    assert benchmark._other_collector_process_active() is True


def test_settlement_snapshot_requires_exact_zero_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            {
                "jobsInQueue": 0,
                "activeJobsInQueue": 0,
                "waitingJobsInQueue": 0,
            },
            {"activePages": 0},
        ]
    )
    monkeypatch.setattr(benchmark, "_http_json", lambda _url: next(responses))
    monkeypatch.setattr(
        benchmark,
        "_settlement_backends",
        lambda _url: {
            "active_crawls": 0,
            "rabbitmq_queue_count": 1,
            "rabbitmq_ready": 0,
            "rabbitmq_unacknowledged": 0,
            "nuq": {
                "queue_scrape_total": 0,
                "queue_scrape_backlog_total": 0,
                "queue_crawl_finished_total": 0,
            },
        },
    )

    assert (
        benchmark._settlement_snapshot(
            "http://localhost:3102", "http://localhost:3103/health"
        )["idle"]
        is True
    )


def test_settlement_backends_requires_complete_rabbit_and_nuq_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark, "_http_json", lambda _url: {"data": {"crawls": []}})
    outputs = iter(
        [
            "scrape_queue 0 0\n",
            (
                "queue_crawl_finished_total|0\n"
                "queue_scrape_backlog_total|0\n"
                "queue_scrape_total|0\n"
            ),
        ]
    )

    def command(*_args, **_kwargs):
        return benchmark.subprocess.CompletedProcess([], 0, next(outputs), "")

    monkeypatch.setattr(benchmark.subprocess, "run", command)

    result = benchmark._settlement_backends("http://127.0.0.1:3102")

    assert result["active_crawls"] == 0
    assert result["rabbitmq_queue_count"] == 1
    assert result["nuq"]["queue_scrape_total"] == 0


def test_settlement_poll_propagates_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        benchmark,
        "_settlement_snapshot",
        lambda *_args: {
            "queue": {"active": 1, "waiting": 0, "total": 1},
            "browser_active_pages": 0,
            "idle": False,
            "observed_at": "busy",
        },
    )
    monkeypatch.setattr(
        benchmark.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        benchmark._await_idle_settlement("api", "browser")


def test_resource_verdict_fails_closed_on_oom_pid_or_missing_telemetry() -> None:
    before = {
        "complete": True,
        "api": {
            "memory_events": {"oom": 0, "oom_kill": 0},
            "pids_events": {"max": 0},
        },
        "browser": {
            "memory_events": {"oom": 0, "oom_kill": 0},
            "pids_events": {"max": 0},
        },
    }
    after = {
        "complete": True,
        "api": {
            "memory_events": {"oom": 0, "oom_kill": 0},
            "pids_events": {"max": 0},
        },
        "browser": {
            "memory_events": {"oom": 1, "oom_kill": 0},
            "pids_events": {"max": 1},
            "pids_peak": 768,
        },
    }

    verdict = benchmark._resource_verdict(before, after, {"browser_pids": 768})
    assert verdict["state"] == "failed"
    assert set(verdict["reasons"]) == {
        "browser_oom",
        "browser_pids_limit_event",
        "browser_pids_peak_at_limit",
    }
    assert (
        benchmark._resource_verdict({"complete": False}, after, {"browser_pids": 768})[
            "state"
        ]
        == "inconclusive"
    )


def test_worker_source_is_hashable_and_imports_real_jll_adapter(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[4]
    source = benchmark._worker_source(repo)

    assert "sources/jll.ts" in source
    assert "enrichJllListing" in source
    assert "providerStop !== null" in source
    assert "performanceHas429" in source
    assert 'if (value === "sale" || value === "sale_or_lease") return "Sale"' in source
    assert 'if (value === "lease") return "Lease"' in source
    assert "transactionType: expectedTransactionType" in source
    assert benchmark._worker_contract(128, 10)["details"] == 128
    assert benchmark._worker_contract(128, 10)["concurrency"] == 10
    assert "jll-investor" not in source.lower()
    assert len(hashlib.sha256(source.encode()).hexdigest()) == 64


def test_worker_scheduler_stops_before_pulling_queued_items() -> None:
    script = (
        benchmark.WORKER_SCHEDULER_JS
        + "\n"
        + "let stopped=false; const started=[]; "
        + "const rows=await pmap([0,1,2,3],2,async(value)=>{"
        + "started.push(value); if(value===0) stopped=true; return value;"
        + "},()=>stopped); console.log(JSON.stringify({started,rows}));"
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"started": [0], "rows": [0]}


@pytest.mark.parametrize(
    ("failure", "error_type", "expected_reason", "propagates"),
    [
        (
            benchmark.BenchmarkError("telemetry"),
            "BenchmarkError",
            "monitor_telemetry_failure",
            None,
        ),
        (
            KeyboardInterrupt(),
            "KeyboardInterrupt",
            "operator_interrupt",
            KeyboardInterrupt,
        ),
        (
            SystemExit("unexpected"),
            "SystemExit",
            "worker_monitor_unexpected_failure",
            SystemExit,
        ),
    ],
)
def test_worker_monitor_failure_or_interrupt_terminates_and_records_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    error_type: str,
    expected_reason: str,
    propagates: type[BaseException] | None,
) -> None:
    repo = tmp_path / "repo"
    tsx = repo / "scripts/firecrawl-ops/cre_collector/node_modules/.bin/tsx"
    tsx.parent.mkdir(parents=True)
    tsx.write_text("stub", encoding="utf-8")
    sample = tmp_path / "sample.json"
    sample.write_text("{}", encoding="utf-8")
    replicate = tmp_path / "replicate"

    class FakeProcess:
        pid = 123
        returncode = None

        def poll(self):
            raise failure

        def wait(self, timeout=None):
            raise benchmark.subprocess.TimeoutExpired("worker", timeout)

    process = FakeProcess()
    terminated: list[object] = []
    popen_environments: list[dict[str, str]] = []
    ticks = iter([(1, 1, 1, 1)])

    def cpu_ticks():
        value = next(ticks)
        if isinstance(value, BaseException):
            raise value
        return value

    def terminate(value):
        terminated.append(value)
        value.returncode = -15

    monkeypatch.setattr(benchmark, "_cpu_ticks", cpu_ticks)
    monkeypatch.setattr(benchmark, "_terminate", terminate)

    def popen(*_args, **kwargs):
        popen_environments.append(kwargs["env"])
        return process

    monkeypatch.setattr(benchmark.subprocess, "Popen", popen)

    kwargs = {
        "repo_root": repo,
        "sample_path": sample,
        "replicate_dir": replicate,
        "requested": {
            "jll_detail_concurrency": 10,
            "host_cpu_sample_seconds": 2,
            "host_cpu_guard_percent": 90,
            "host_cpu_guard_seconds": 30,
        },
        "api_url": "http://127.0.0.1:3102",
        "timeout_seconds": 60,
        "expected_details": 128,
    }
    if propagates is not None:
        with pytest.raises(propagates):
            benchmark._run_worker(**kwargs)
    else:
        code, _samples, reason = benchmark._run_worker(**kwargs)
        assert (code, reason) == (-15, "monitor_telemetry_failure")

    assert terminated == [process]
    guard = json.loads((replicate / "guard.json").read_text())
    assert guard["monitor_error_type"] == error_type
    assert guard["termination_reason"] == expected_reason
    assert popen_environments[0]["CRE_SCRAPE_MAX_ATTEMPTS"] == "1"


def test_post_worker_settlement_failure_persists_unknown_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    sample_path = tmp_path / "sample.json"
    sample = {"inventory_sha256": "a" * 64}
    sample_path.write_text(json.dumps(sample), encoding="utf-8")
    admission_path = tmp_path / "admission.json"
    admission = _admission(tmp_path)
    admission["review_approval_created_at"] = benchmark._now()
    admission["endpoints"] = {
        "api_url": "http://127.0.0.1:3102",
        "browser_health_url": "http://127.0.0.1:3103/health",
    }
    admission_path.write_text(json.dumps(admission), encoding="utf-8")
    admission_path.chmod(0o600)
    events: list[str] = []

    class FakeLock:
        def __init__(self, _path):
            pass

        def __enter__(self):
            events.append("lock_enter")
            return self

        def __exit__(self, *_args):
            events.append("lock_exit")

        def arm_benchmark(self, _evidence):
            events.append("lock_armed")

        def disarm_benchmark(self):
            events.append("lock_disarmed")

    def live_admission(*_args):
        assert events[-1] == "sample_provenance"
        events.append("live_admission")
        return {
            "effective_runtime": benchmark._effective_runtime_evidence(
                admission["effective"], profile["requested"]
            )
        }

    def consume(*args, **kwargs):
        assert events[-1] == "live_admission"
        events.append("admission_consumed")
        return _audit_file(tmp_path, admission)

    monkeypatch.setattr(benchmark, "verify_live_admission", live_admission)
    monkeypatch.setattr(benchmark, "_consume_admission", consume)

    def sample_provenance(*_args):
        assert events[-1] == "implementation_verified"
        events.append("sample_provenance")
        return {}

    monkeypatch.setattr(benchmark, "verify_sample_provenance", sample_provenance)
    monkeypatch.setattr(benchmark, "validate_sample", lambda value, _details: value)
    monkeypatch.setattr(benchmark, "_implementation_manifest", lambda *_args: {})
    monkeypatch.setattr(
        benchmark,
        "_verify_implementation_manifest",
        lambda *_args: events.append("implementation_verified"),
    )
    monkeypatch.setattr(
        benchmark,
        "canonical_shared_lock_dir",
        lambda *_args: tmp_path / "out" / "daily" / ".cre.lock",
    )
    monkeypatch.setattr(benchmark, "SharedLock", FakeLock)
    monkeypatch.setattr(
        benchmark,
        "_settlement_snapshot",
        lambda *_args: {
            "queue": {"active": 0, "waiting": 0, "total": 0},
            "browser_active_pages": 0,
            "idle": True,
            "observed_at": "before",
        },
    )
    monkeypatch.setattr(
        benchmark,
        "_await_idle_settlement",
        lambda *_args: (
            events.append("settlement_polled")
            or {
                "idle": False,
                "state": "unknown",
                "observed_at": "after",
                "error": "bounded_idle_settlement_not_proven",
            }
        ),
    )
    monkeypatch.setattr(benchmark, "_resource_snapshot", lambda: {"complete": False})
    monkeypatch.setattr(benchmark, "_run_worker", lambda **_kwargs: (0, [], None))
    monkeypatch.setattr(
        benchmark,
        "_quarantine_shared_lock",
        lambda *_args, **_kwargs: (
            events.append("lock_quarantined")
            or {"state": "quarantined", "evidence_sha256": "7" * 64}
        ),
    )

    result = benchmark.run_benchmark(
        repo_root=tmp_path,
        artifact_root=artifact,
        sample_path=sample_path,
        sample=sample,
        profile=profile,
        profile_name="bold-jll-128",
        config_sha256=digest,
        admission=admission,
        admission_path=admission_path,
        timeout_seconds=60,
    )

    assert result["completed"] is False
    assert result["replicates"][0]["source_owned_settlement"] == "unknown"
    assert (artifact / "result.json").is_file()
    assert events.index("lock_enter") < events.index("sample_provenance")
    assert events.index("sample_provenance") < events.index("live_admission")
    assert events.index("lock_enter") < events.index("live_admission")
    assert events.index("admission_consumed") < events.index("settlement_polled")
    assert events.index("settlement_polled") < events.index("lock_quarantined")
    assert events.index("settlement_polled") < events.index("lock_exit")


def _resource_snapshot() -> dict[str, object]:
    counters = {
        "memory_current": 1,
        "memory_peak": 2,
        "pids_current": 3,
        "pids_peak": 4,
        "pids_max": 768,
        "memory_events": {"oom": 0, "oom_kill": 0},
        "pids_events": {"max": 0},
        "cpu_stat": {"usage_usec": 1},
    }
    return {
        "observed_at": "2026-09-13T01:00:00Z",
        "complete": True,
        "api": counters,
        "browser": counters,
    }


def _settlement(*, final: bool) -> dict[str, object]:
    value: dict[str, object] = {
        "queue": {"active": 0, "waiting": 0, "total": 0},
        "browser_active_pages": 0,
        "active_crawls": 0,
        "rabbitmq_queue_count": 1,
        "rabbitmq_ready": 0,
        "rabbitmq_unacknowledged": 0,
        "nuq": {
            "queue_scrape_total": 0,
            "queue_scrape_backlog_total": 0,
            "queue_crawl_finished_total": 0,
        },
        "idle": True,
        "observed_at": "2026-09-13T01:00:00Z",
    }
    if final:
        value.update({"state": "idle", "polls": 1, "observations": [dict(value)]})
    return value


def _performance(concurrency: int = 10) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "cre_scrape_performance",
        "terminal": True,
        "degraded": False,
        "metrics": {
            "logical_scrape_calls": {"raw": 0, "doc": 128, "json": 0},
            "requests": {
                "attempts_started": 128,
                "attempts_completed": 128,
                "succeeded": 128,
                "failed": 0,
                "fresh_requested": 128,
                "active_locally_awaited": 0,
                "max_active_locally_awaited": concurrency,
                "timed_out_remote_settlement_unknown": 0,
                "status_counts": {},
                "other_valid_statuses": 0,
                "retry": {
                    "http_helper": {
                        "retry_attempts": 0,
                        "backoff_ms": 0,
                        "terminal_backoff_ms": 0,
                    },
                    "json_parse": {
                        "retry_attempts": 0,
                        "backoff_ms": 0,
                        "terminal_backoff_ms": 0,
                    },
                },
                "error_categories": {
                    "timeout": 0,
                    "http_4xx": 0,
                    "http_5xx": 0,
                    "transport": 0,
                    "empty_response": 0,
                    "unknown": 0,
                },
                "by_source_transaction": [
                    {
                        "source": "jll",
                        "transaction": "sale",
                        "attempts_started": 128,
                        "attempts_completed": 128,
                        "succeeded": 128,
                        "failed": 0,
                        "fresh_requested": 128,
                    }
                ],
            },
        },
    }


def _comparison_result(rate: float, variant: str = "baseline") -> dict[str, object]:
    contract = benchmark._experiment_contract()
    requested = dict(contract["requested"][variant])
    repo = Path(__file__).resolve().parents[4]
    implementation = benchmark._implementation_manifest(repo)
    worker_contract = benchmark._worker_contract(
        int(contract["workload"]["details"]),
        requested["jll_detail_concurrency"],
    )
    worker_sha = hashlib.sha256(
        benchmark._worker_source(
            repo, concurrency=requested["jll_detail_concurrency"]
        ).encode()
    ).hexdigest()
    sample_canonical_sha256 = "6" * 64
    records = [
        {
            "sample_index": index,
            "sample_id": f"{index:024x}",
            "identity_match": True,
            "identity_sha256": "a" * 64,
            "freshness_match": True,
            "native_complete": True,
            "native_evidence_sha256": "b" * 64,
            "structural_complete": True,
            "structural_evidence_sha256": "c" * 64,
            "transaction_type": "Sale" if index % 2 == 0 else "Lease",
            "transaction_type_match": True,
        }
        for index in range(128)
    ]
    sample_ids = [record["sample_id"] for record in records]
    record_manifest = {
        "schema_version": 1,
        "kind": "cre_jll_capacity_record_evidence",
        "sample_canonical_sha256": sample_canonical_sha256,
        "worker_output_sha256": "7" * 64,
        "record_count": 128,
        "sample_ids_sha256": hashlib.sha256(
            benchmark._canonical(sample_ids)
        ).hexdigest(),
        "records_sha256": hashlib.sha256(benchmark._canonical(records)).hexdigest(),
    }
    replicates = []
    for index in range(3):
        replicates.append(
            {
                "replicate": index + 1,
                "worker_exit_code": 0,
                "guard_triggered": False,
                "termination_reason": None,
                "qualified_fresh_unique_rows": 128,
                "qualified_fresh_unique_per_minute": rate,
                "freshness_matches": 128,
                "historic_native_asset_matches": 128,
                "native_asset_deltas": 0,
                "normalized_structural_matches": 128,
                "normalized_structural_drops": [],
                "quality_errors": [],
                "comparison_state": "measured",
                "resource_verdict": {"state": "measured"},
                "source_owned_settlement": "locally_awaited_terminal",
                "settlement_before": _settlement(final=False),
                "settlement_after": _settlement(final=True),
                "remote_settlement_unknown": 0,
                "provider_cooldown": {"required": False},
                "host_samples": [
                    {
                        "observed_at": "2026-09-13T01:00:01Z",
                        "host_cpu_percent": 50,
                    }
                ],
                "guard_telemetry": {
                    "triggered": False,
                    "termination_reason": None,
                    "monitor_error_type": None,
                    "worker_exit_code": 0,
                    "stderr_sha256": "9" * 64,
                    "stderr_bytes": 0,
                    "stderr_file": "/private/replicate/worker.stderr",
                    "samples": [
                        {
                            "observed_at": "2026-09-13T01:00:01Z",
                            "host_cpu_percent": 50,
                        }
                    ],
                    "worker_source_sha256": worker_sha,
                },
                "resources_before": _resource_snapshot(),
                "resources_after": _resource_snapshot(),
                "performance": _performance(requested["jll_detail_concurrency"]),
                "performance_telemetry_complete": True,
                "worker_contract_sha256": worker_contract["sha256"],
                "worker_output_sha256": "7" * 64,
                "record_evidence": json.loads(json.dumps(records)),
                "record_evidence_manifest": dict(record_manifest),
                "latency_ms": {"p50": 10, "p95": 20, "p99": 30},
            }
        )
    effective_runtime = benchmark._effective_runtime_evidence(
        _runtime_public("c" * 40, variant), requested
    )
    return {
        "schema_version": 1,
        "kind": benchmark.RESULT_KIND,
        "mode": "run",
        "completed": True,
        "comparison_state": "complete",
        "profile": contract["profiles"][variant],
        "config_sha256": contract["config_sha256"],
        "sample_inventory_sha256": "a" * 64,
        "sample_manifest_sha256": "b" * 64,
        "sample_canonical_sha256": sample_canonical_sha256,
        "source_git_sha": "c" * 40,
        "worker_source_sha256": worker_sha,
        "worker_contract": worker_contract,
        "implementation_manifest": implementation,
        "admission_sha256": "f" * 64,
        "review_approval_nonce_sha256": "2" * 64,
        "admission_consumption_sha256": "1" * 64,
        "review_benchmark_grant_sha256": "8" * 64,
        "freshness_policy": dict(benchmark.EXPECTED_FRESHNESS_POLICY),
        "workload": dict(contract["workload"]),
        "requested": requested,
        "effective_runtime": effective_runtime,
        "live_admission": {
            "observed_at": "2026-09-13T01:00:00Z",
            "snapshot_sha256": "d" * 64,
            "transition_sha256": "e" * 64,
            "checks": {"candidate_exact": True, "collector_idle": True},
            "other_collector_process_active": False,
            "effective_runtime": effective_runtime,
        },
        "shared_lock": {"canonical": True},
        "final_settlement": _settlement(final=True),
        "safety": {
            "database_writes": 0,
            "canonical_cache_writes": 0,
            "provider_retry_policy": {
                "jll_graphql_attempts": 1,
                "jll_detail_fallback": "disabled",
                "shared_scrape_helper_attempts": 1,
            },
            "cancellation_limitation": "no_supported_scrape_job_cancel_endpoint_or_job_ids; idle_settlement_required_before_lock_release",
        },
        "replicates": replicates,
    }


@pytest.mark.parametrize(
    ("worker_error", "final_settlement", "error_type"),
    [
        (SystemExit("unexpected"), _settlement(final=True), SystemExit),
        (
            KeyboardInterrupt("interrupted"),
            {
                "idle": False,
                "state": "unknown",
                "polls": 2,
                "observed_at": "2026-09-13T01:00:00Z",
                "observations": [],
                "error": "bounded_idle_settlement_not_proven",
            },
            KeyboardInterrupt,
        ),
    ],
)
def test_outer_worker_failure_persists_settlement_then_reraises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_error: BaseException,
    final_settlement: dict[str, object],
    error_type: type[BaseException],
) -> None:
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    sample = {"inventory_sha256": "a" * 64}
    sample_path = tmp_path / "sample.json"
    sample_path.write_text(json.dumps(sample), encoding="utf-8")
    admission = _admission(tmp_path)
    admission["review_approval_created_at"] = benchmark._now()
    admission["endpoints"] = {
        "api_url": "http://127.0.0.1:3102",
        "browser_health_url": "http://127.0.0.1:3103/health",
    }
    admission_path = tmp_path / "admission.json"
    admission_path.write_text(json.dumps(admission), encoding="utf-8")
    admission_path.chmod(0o600)
    lock_held = False
    settlement_calls = 0

    class FakeLock:
        def __init__(self, _path):
            pass

        def __enter__(self):
            nonlocal lock_held
            lock_held = True
            return self

        def __exit__(self, *_args):
            nonlocal lock_held
            lock_held = False

        def arm_benchmark(self, _evidence):
            assert lock_held

        def disarm_benchmark(self):
            assert lock_held

    def settlement(*_args):
        nonlocal settlement_calls
        assert lock_held is True
        settlement_calls += 1
        return final_settlement

    monkeypatch.setattr(benchmark, "SharedLock", FakeLock)
    monkeypatch.setattr(
        benchmark,
        "canonical_shared_lock_dir",
        lambda *_args: tmp_path / "out" / "daily" / ".cre.lock",
    )
    monkeypatch.setattr(benchmark, "validate_sample", lambda value, _details: value)
    monkeypatch.setattr(benchmark, "verify_sample_provenance", lambda _sample: {})
    monkeypatch.setattr(
        benchmark,
        "verify_live_admission",
        lambda *_args: {
            "effective_runtime": benchmark._effective_runtime_evidence(
                admission["effective"], profile["requested"]
            )
        },
    )
    monkeypatch.setattr(
        benchmark,
        "_consume_admission",
        lambda *_args, **_kwargs: _audit_file(tmp_path, admission),
    )
    monkeypatch.setattr(benchmark, "_implementation_manifest", lambda *_args: {})
    monkeypatch.setattr(benchmark, "_verify_implementation_manifest", lambda *_args: {})
    monkeypatch.setattr(
        benchmark, "_settlement_snapshot", lambda *_args: _settlement(final=False)
    )
    monkeypatch.setattr(benchmark, "_resource_snapshot", lambda: {"complete": False})
    monkeypatch.setattr(
        benchmark,
        "_run_worker",
        lambda **_kwargs: (_ for _ in ()).throw(worker_error),
    )
    monkeypatch.setattr(benchmark, "_await_idle_settlement", settlement)
    monkeypatch.setattr(
        benchmark,
        "_quarantine_shared_lock",
        lambda *_args, **_kwargs: {
            "state": "quarantined",
            "evidence_sha256": "7" * 64,
        },
    )

    with pytest.raises(error_type):
        benchmark.run_benchmark(
            repo_root=tmp_path,
            artifact_root=artifact,
            sample_path=sample_path,
            sample=sample,
            profile=profile,
            profile_name="bold-jll-128",
            config_sha256=digest,
            admission=admission,
            admission_path=admission_path,
            timeout_seconds=60,
        )

    persisted = json.loads((artifact / "result.json").read_text())
    assert settlement_calls == 1
    assert persisted["final_settlement"]["state"] == final_settlement["state"]
    assert persisted["comparison_state"] == "failed"
    assert persisted["interruption_type"] == error_type.__name__
    if final_settlement["state"] == "unknown":
        assert persisted["stop_reason"] == "final_settlement_unknown"


def test_sigterm_handler_fails_into_interrupt_cleanup_path() -> None:
    with pytest.raises(KeyboardInterrupt, match=str(benchmark.signal.SIGTERM)):
        benchmark._signal_as_interrupt(benchmark.signal.SIGTERM, None)


def test_compare_results_requires_matched_complete_evidence_and_fifteen_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark, "SUPPORTED_BASELINE_ADMISSION_AVAILABLE", True)
    baseline = _comparison_result(100)
    candidate = _comparison_result(115, "candidate")
    repo = Path(__file__).resolve().parents[4]
    baseline["worker_source_sha256"] = hashlib.sha256(
        benchmark._worker_source(repo, concurrency=4).encode()
    ).hexdigest()
    candidate["worker_source_sha256"] = hashlib.sha256(
        benchmark._worker_source(repo, concurrency=10).encode()
    ).hexdigest()
    assert baseline["worker_source_sha256"] != candidate["worker_source_sha256"]

    comparison = benchmark.compare_results(baseline, candidate)
    assert comparison["state"] == "measured"
    assert comparison["gain_percent"] == 15
    assert comparison["decision"] == "adoptable"
    assert comparison["candidate"]["completeness_fidelity"][
        "historic_native_asset_matches_per_replicate"
    ] == [128, 128, 128]

    candidate["replicates"][0]["resource_verdict"] = {"state": "inconclusive"}
    comparison = benchmark.compare_results(baseline, candidate)
    assert comparison["state"] == "inconclusive"
    assert "candidate_replicate_1_resources" in comparison["reasons"]


def test_compare_results_safe_negative_and_mismatch_are_nonfatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark, "SUPPORTED_BASELINE_ADMISSION_AVAILABLE", True)
    baseline = _comparison_result(100)
    candidate = _comparison_result(114, "candidate")
    assert benchmark.compare_results(baseline, candidate)["decision"] == "do_not_adopt"

    candidate["sample_manifest_sha256"] = "e" * 64
    comparison = benchmark.compare_results(baseline, candidate)
    assert comparison["state"] == "inconclusive"
    assert "mismatch_sample_manifest_sha256" in comparison["reasons"]


def test_compare_cli_is_read_only_and_needs_no_artifact_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps(_comparison_result(100)), encoding="utf-8")
    candidate.write_text(
        json.dumps(_comparison_result(114, "candidate")), encoding="utf-8"
    )

    code = benchmark.main(
        [
            "--compare-baseline",
            str(baseline),
            "--compare-candidate",
            str(candidate),
        ]
    )

    assert code == 0
    comparison = json.loads(capsys.readouterr().out)
    assert comparison["decision"] == "no_adoption_decision"
    assert "supported_baseline_admission_unavailable" in comparison["reasons"]


def test_summarize_replicate_fails_closed_on_native_delta_and_remote_timeout(
    tmp_path: Path,
) -> None:
    sample = _sample(tmp_path)
    contract = benchmark._worker_contract(128, 10)
    sample_sha256 = hashlib.sha256(benchmark._canonical(sample)).hexdigest()
    replicate = tmp_path / "replicate"
    replicate.mkdir()
    rows = []
    for row in sample["details"]:
        rows.append(
            {
                "sample_index": row["sample_index"],
                "sample_id": row["sample_id"],
                "latency_ms": 10,
                "transaction_type": benchmark._transaction_type(
                    row["transaction_class"]
                ),
                "normalized": {
                    "id": row["id"],
                    "url": row["url"],
                    "transactionType": benchmark._transaction_type(
                        row["transaction_class"]
                    ),
                    "detailObservedAt": "2026-09-13T01:00:00Z",
                    "freshnessProvenance": {"cacheDisposition": "live"},
                },
                "native": {"fingerprints": {}},
            }
        )
    (replicate / "worker-output.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "cre_jll_capacity_worker",
                "worker_contract_sha256": contract["sha256"],
                "generation": "2026-09-13T010000Z-abcdefabcdef",
                "started_at": "2026-09-13T00:59:00Z",
                "rows": rows,
            }
        ),
        encoding="utf-8",
    )
    (replicate / "performance.json").write_text(
        json.dumps(
            {"metrics": {"requests": {"timed_out_remote_settlement_unknown": 1}}}
        ),
        encoding="utf-8",
    )

    summary = benchmark.summarize_replicate(
        replicate,
        sample,
        60,
        sample_canonical_sha256=sample_sha256,
        worker_contract=contract,
    )

    assert summary["qualified_fresh_unique_rows"] == 0
    assert summary["native_asset_deltas"] == 128
    assert summary["remote_settlement_unknown"] == 1
    assert summary["provider_cooldown"]["required"] is False
    assert summary["comparison_state"] == "quality_failed"


def test_admission_consumption_requires_review_grant_then_writes_private_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission_path = tmp_path / "admission.json"
    admission = _admission(tmp_path)
    admission["review_approval_created_at"] = benchmark._now()
    grant = _review_grant(admission)
    admission_path.write_text(json.dumps(admission), encoding="utf-8")
    admission_path.chmod(0o600)
    monkeypatch.setattr(
        benchmark,
        "canonical_shared_lock_dir",
        lambda *_args: tmp_path / "out" / "daily" / ".cre.lock",
    )
    calls = []

    def consume(argv, **kwargs):
        calls.append((argv, kwargs))
        return benchmark.subprocess.CompletedProcess(
            argv, 0, json.dumps(grant).encode(), b""
        )

    monkeypatch.setattr(benchmark.subprocess, "run", consume)

    marker = benchmark._consume_admission(admission_path, admission)

    assert calls[0][0][:2] == ["/usr/bin/python3", "-c"]
    assert calls[0][0][-1] == admission["review_benchmark_grant_path"]
    assert marker.parent == tmp_path / "out" / ".capacity-admission-consumption"
    assert marker.stat().st_mode & 0o077 == 0
    audit = json.loads(marker.read_text())
    assert (
        audit["admission_sha256"]
        == hashlib.sha256(benchmark._canonical(admission)).hexdigest()
    )
    assert (
        audit["review_benchmark_grant_sha256"]
        == hashlib.sha256(benchmark._canonical(grant)).hexdigest()
    )
    copied = tmp_path / "copied-admission.json"
    altered_copy = {**admission, "created_at": "2026-09-13T01:00:01Z"}
    copied.write_text(json.dumps(altered_copy), encoding="utf-8")
    copied.chmod(0o600)
    with pytest.raises(benchmark.BenchmarkError, match="already consumed"):
        benchmark._consume_admission(copied, altered_copy)


def test_admission_consumption_executes_same_user_grant_helper_once(
    tmp_path: Path,
) -> None:
    admission_path = tmp_path / "admission.json"
    admission = _admission(tmp_path)
    admission["review_approval_created_at"] = benchmark._now()
    grant_path = Path(str(admission["review_benchmark_grant_path"]))
    benchmark._atomic_private_json(grant_path, _review_grant(admission))
    benchmark._atomic_private_json(admission_path, admission)

    marker = benchmark._consume_admission(
        admission_path,
        admission,
        canonical_lock_path=tmp_path / "out" / "daily" / ".cre.lock",
    )

    assert marker.exists()
    assert not grant_path.exists()


@pytest.mark.parametrize("unsafe_kind", ["public", "hardlink", "symlink"])
def test_admission_consumption_rejects_unsafe_grant_file(
    unsafe_kind: str, tmp_path: Path
) -> None:
    admission_path = tmp_path / "admission.json"
    admission = _admission(tmp_path)
    admission["review_approval_created_at"] = benchmark._now()
    grant_path = Path(str(admission["review_benchmark_grant_path"]))
    benchmark._atomic_private_json(admission_path, admission)
    if unsafe_kind == "symlink":
        target = tmp_path / "grant-target.json"
        benchmark._atomic_private_json(target, _review_grant(admission))
        grant_path.symlink_to(target)
    else:
        benchmark._atomic_private_json(grant_path, _review_grant(admission))
        if unsafe_kind == "public":
            grant_path.chmod(0o644)
        else:
            os.link(grant_path, tmp_path / "grant-hardlink.json")

    with pytest.raises(benchmark.BenchmarkError, match="grant consumption failed"):
        benchmark._consume_admission(
            admission_path,
            admission,
            canonical_lock_path=tmp_path / "out" / "daily" / ".cre.lock",
        )

    assert grant_path.exists()


@pytest.mark.parametrize(
    ("uid", "euid"),
    [
        (0, 0),
        (501, 0),
        (501, 502),
    ],
)
def test_admission_consumption_rejects_root_or_switched_account(
    uid: int, euid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(benchmark.os, "getuid", lambda: uid)
    monkeypatch.setattr(benchmark.os, "geteuid", lambda: euid)

    with pytest.raises(benchmark.BenchmarkError, match="non-root unswitched"):
        benchmark._operator_uid()


def test_admission_consumption_rejects_public_replay_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission_path = tmp_path / "admission.json"
    admission = _admission(tmp_path)
    admission["review_approval_created_at"] = benchmark._now()
    grant = _review_grant(admission)
    admission_path.write_text(json.dumps(admission), encoding="utf-8")
    admission_path.chmod(0o600)
    consumption_root = tmp_path / "out" / ".capacity-admission-consumption"
    consumption_root.mkdir(parents=True, mode=0o700)
    consumption_root.chmod(0o755)
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda argv, **_kwargs: benchmark.subprocess.CompletedProcess(
            argv, 0, json.dumps(grant).encode(), b""
        ),
    )

    with pytest.raises(benchmark.BenchmarkError, match="directory is unsafe"):
        benchmark._consume_admission(
            admission_path,
            admission,
            canonical_lock_path=tmp_path / "out" / "daily" / ".cre.lock",
        )


def test_admission_consumption_rejects_unsafe_opened_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission_path = tmp_path / "admission.json"
    admission = _admission(tmp_path)
    admission["review_approval_created_at"] = benchmark._now()
    grant = _review_grant(admission)
    admission_path.write_text(json.dumps(admission), encoding="utf-8")
    admission_path.chmod(0o600)
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda argv, **_kwargs: benchmark.subprocess.CompletedProcess(
            argv, 0, json.dumps(grant).encode(), b""
        ),
    )
    monkeypatch.setattr(
        benchmark.os,
        "fstat",
        lambda _descriptor: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644,
            st_nlink=1,
            st_uid=os.geteuid(),
        ),
    )
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"

    with pytest.raises(benchmark.BenchmarkError, match="marker is unsafe"):
        benchmark._consume_admission(
            admission_path, admission, canonical_lock_path=lock_path
        )

    marker = (
        tmp_path
        / "out"
        / ".capacity-admission-consumption"
        / f"{admission['review_approval_nonce_sha256']}.json"
    )
    assert not marker.exists()


@pytest.mark.parametrize(
    ("key", "invalid"),
    [
        ("config_sha256", "0" * 64),
        ("schema_version", True),
        ("review_approval_created_at", "2026-09-14T00:00:00Z"),
        ("expires_after_seconds", 601),
    ],
)
def test_review_grant_binding_mismatch_is_never_admitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    invalid: object,
) -> None:
    admission = _admission(tmp_path)
    path = tmp_path / "admission.json"
    path.write_text(json.dumps(admission), encoding="utf-8")
    path.chmod(0o600)
    grant = {
        "schema_version": 1,
        "kind": benchmark.capacity_runtime.BENCHMARK_GRANT_KIND,
        "profile": admission["profile"],
        "config_sha256": admission["config_sha256"],
        "transition_receipt_sha256": admission["transition_receipt_sha256"],
        "source_git_sha": admission["source_git_sha"],
        "review_approval_nonce_sha256": admission["review_approval_nonce_sha256"],
        "review_approval_created_at": admission["review_approval_created_at"],
        "expires_after_seconds": 600,
        "approved": True,
    }
    grant[key] = invalid
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda argv, **_kwargs: benchmark.subprocess.CompletedProcess(
            argv, 0, json.dumps(grant).encode(), b""
        ),
    )

    with pytest.raises(benchmark.BenchmarkError, match="does not bind"):
        benchmark._consume_admission(
            path,
            admission,
            canonical_lock_path=tmp_path / "out" / "daily" / ".cre.lock",
        )


@pytest.mark.parametrize(
    ("created", "expiry"),
    [
        ("2026-09-13T00:49:59Z", 600),
        ("2026-09-13T01:00:01Z", 600),
        ("2026-09-13T01:00:00", 600),
        (None, 600),
        ("2026-09-13T01:00:00Z", True),
        ("2026-09-13T01:00:00Z", 600.0),
        ("2026-09-13T01:00:00Z", 601),
    ],
)
def test_review_grant_freshness_rejects_stale_future_and_invalid_fields(
    created, expiry
):
    with pytest.raises(benchmark.BenchmarkError, match="review benchmark grant"):
        benchmark._validate_review_grant_freshness(
            {"review_approval_created_at": created, "expires_after_seconds": expiry},
            now=benchmark.datetime(2026, 9, 13, 1, 0, tzinfo=benchmark.UTC),
        )


def test_editing_admission_timestamp_cannot_renew_review_grant(tmp_path):
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    admission = _admission(tmp_path)
    admission["created_at"] = "2026-09-14T01:00:00Z"
    with pytest.raises(
        benchmark.BenchmarkError, match="review benchmark grant is stale"
    ):
        benchmark.validate_admission(
            admission,
            profile,
            "bold-jll-128",
            digest,
            source_git_sha="a" * 40,
            now=benchmark.datetime(2026, 9, 14, 1, 0, tzinfo=benchmark.UTC),
        )


def test_consumption_enforces_authentic_review_grant_age(tmp_path, monkeypatch):
    admission = _admission(tmp_path)
    admission["created_at"] = benchmark._now()
    path = tmp_path / "admission.json"
    benchmark._atomic_private_json(path, admission)
    grant = {
        key: admission[key]
        for key in (
            "schema_version",
            "profile",
            "config_sha256",
            "source_git_sha",
            "transition_receipt_sha256",
            "review_approval_nonce_sha256",
            "review_approval_created_at",
            "expires_after_seconds",
        )
    }
    grant.update(
        {"kind": benchmark.capacity_runtime.BENCHMARK_GRANT_KIND, "approved": True}
    )
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda argv, **_kwargs: benchmark.subprocess.CompletedProcess(
            argv, 0, json.dumps(grant).encode(), b""
        ),
    )
    with pytest.raises(
        benchmark.BenchmarkError, match="review benchmark grant is stale"
    ):
        benchmark._consume_admission(
            path,
            admission,
            canonical_lock_path=tmp_path / "out" / "daily" / ".cre.lock",
        )
    assert not (tmp_path / "out" / ".capacity-admission-consumption").exists()


def test_worker_rechecks_review_grant_immediately_before_popen(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    tsx = repo / "scripts/firecrawl-ops/cre_collector/node_modules/.bin/tsx"
    tsx.parent.mkdir(parents=True)
    tsx.write_text("stub")
    monkeypatch.setattr(benchmark, "_cpu_ticks", lambda: (1, 1, 1, 1))
    calls = []
    monkeypatch.setattr(
        benchmark.subprocess, "Popen", lambda *_args, **_kwargs: calls.append("popen")
    )
    with pytest.raises(benchmark.BenchmarkError, match="could not be started"):
        benchmark._run_worker(
            repo_root=repo,
            sample_path=tmp_path / "sample.json",
            replicate_dir=tmp_path / "replicate",
            requested={"jll_detail_concurrency": 10, "host_cpu_sample_seconds": 2},
            api_url="http://127.0.0.1:3102",
            timeout_seconds=60,
            expected_details=128,
            review_grant={
                "review_approval_created_at": "2000-01-01T00:00:00Z",
                "expires_after_seconds": 600,
            },
        )
    assert calls == []


@pytest.mark.parametrize(
    "outcome",
    ["idle", "unknown", "result_write_failure", "arm_failure", "worker_error"],
)
def test_benchmark_interlock_spans_workers_settlement_and_durable_result(
    tmp_path, monkeypatch, outcome
):
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    admission = _admission(tmp_path)
    admission["review_approval_created_at"] = benchmark._now()
    admission["endpoints"] = {
        "api_url": "http://127.0.0.1:3102",
        "browser_health_url": "http://127.0.0.1:3103/health",
    }
    sample = {"inventory_sha256": "a" * 64}
    sample_path = tmp_path / "sample.json"
    sample_path.write_text(json.dumps(sample))
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    active_path = lock_path / "capacity-benchmark-active.json"
    events = []
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(benchmark, "_implementation_manifest", lambda *_args: {})
    monkeypatch.setattr(benchmark, "_verify_implementation_manifest", lambda *_args: {})
    monkeypatch.setattr(benchmark, "validate_sample", lambda value, _details: value)
    monkeypatch.setattr(benchmark, "verify_sample_provenance", lambda *_args: {})
    monkeypatch.setattr(
        benchmark, "verify_live_admission", lambda *_args: {"effective_runtime": {}}
    )
    monkeypatch.setattr(
        benchmark,
        "_consume_admission",
        lambda *_args, **_kwargs: _audit_file(tmp_path, admission),
    )
    monkeypatch.setattr(
        benchmark, "_settlement_snapshot", lambda *_args: {"idle": True}
    )
    monkeypatch.setattr(benchmark, "_resource_snapshot", dict)
    monkeypatch.setattr(benchmark, "_resource_verdict", lambda *_args: {})
    monkeypatch.setattr(
        benchmark,
        "summarize_replicate",
        lambda *_args, **_kwargs: {"remote_settlement_unknown": 0},
    )
    monkeypatch.setattr(benchmark, "_replicate_state", lambda *_args: "measured")
    real_arm = benchmark.SharedLock.arm_benchmark
    real_disarm = benchmark.SharedLock.disarm_benchmark
    real_write = benchmark._atomic_private_json

    def arm(lock, evidence):
        real_arm(lock, evidence)
        events.append("armed")
        if outcome == "arm_failure":
            raise OSError("arm failed after durable marker")

    def worker(**kwargs):
        assert active_path.is_file()
        if kwargs["review_grant"] is not None:
            assert (
                kwargs["review_grant"]["review_approval_created_at"]
                == admission["review_approval_created_at"]
            )
        events.append(
            "first_worker" if kwargs["review_grant"] is not None else "later_worker"
        )
        if outcome == "worker_error":
            raise OSError("worker failed unexpectedly")
        return 0, [], None

    def settlement(*_args):
        assert active_path.is_file()
        events.append("settlement")
        return {
            "idle": outcome != "unknown",
            "state": "unknown" if outcome == "unknown" else "idle",
        }

    def write(path, value):
        if path.name == "result.json":
            assert active_path.is_file()
            if outcome == "result_write_failure":
                raise OSError("result write failed")
            real_write(path, value)
            events.append("durable_result")
        else:
            real_write(path, value)

    def disarm(lock):
        assert events[-1] == "durable_result"
        assert (
            json.loads((artifact / "result.json").read_text())["final_settlement"][
                "state"
            ]
            == "idle"
        )
        real_disarm(lock)
        events.append("disarmed")

    monkeypatch.setattr(benchmark.SharedLock, "arm_benchmark", arm)
    monkeypatch.setattr(benchmark.SharedLock, "disarm_benchmark", disarm)
    monkeypatch.setattr(benchmark, "_run_worker", worker)
    monkeypatch.setattr(benchmark, "_await_idle_settlement", settlement)
    monkeypatch.setattr(benchmark, "_atomic_private_json", write)
    kwargs = {
        "repo_root": tmp_path,
        "artifact_root": artifact,
        "sample_path": sample_path,
        "sample": sample,
        "profile": profile,
        "profile_name": "bold-jll-128",
        "config_sha256": digest,
        "admission": admission,
        "admission_path": tmp_path / "admission.json",
        "timeout_seconds": 60,
    }
    if outcome in {"result_write_failure", "arm_failure", "worker_error"}:
        with pytest.raises(OSError):
            benchmark.run_benchmark(**kwargs)
    else:
        benchmark.run_benchmark(**kwargs)
    assert events[0] == "armed"
    if outcome == "arm_failure":
        assert "first_worker" not in events
    else:
        assert events.count("first_worker") == 1
        assert events.count("later_worker") == (0 if outcome == "worker_error" else 2)
    if outcome == "idle":
        assert events[-2:] == ["durable_result", "disarmed"]
        assert not lock_path.exists()
    else:
        assert active_path.is_file()
        assert "disarmed" not in events
        with pytest.raises(benchmark.LockHeldError):
            benchmark.SharedLock(lock_path).acquire()


def test_unknown_settlement_quarantines_canonical_lock_against_reclaim(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    lock = benchmark.SharedLock(lock_path)
    lock.acquire()

    quarantine = benchmark._quarantine_shared_lock(
        lock,
        artifact_result_path=tmp_path / "result.json",
        admission_sha256="a" * 64,
        review_approval_nonce_sha256="b" * 64,
    )
    lock.release()

    evidence = Path(quarantine["evidence_path"])
    assert lock_path.is_dir()
    assert lock_path.stat().st_mode & 0o077 == 0
    assert not (lock_path / "pid").exists()
    assert not (lock_path / "lease").exists()
    assert evidence.stat().st_mode & 0o077 == 0
    assert json.loads(evidence.read_text())["recovery"]["automatic_reclaim"] == (
        "disabled_missing_pid_and_lease"
    )
    with pytest.raises(benchmark.LockHeldError):
        benchmark.SharedLock(lock_path).acquire()


def test_implementation_manifest_is_config_independent_and_rechecked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path(__file__).resolve().parents[4]
    manifest = benchmark._implementation_manifest(repo)

    assert set(manifest["files"]) == set(benchmark.IMPLEMENTATION_PATHS)
    assert not any("experiment_profiles" in path for path in manifest["files"])
    monkeypatch.setattr(benchmark, "_require_clean_git", lambda _repo: "a" * 40)
    monkeypatch.setattr(
        benchmark,
        "_implementation_manifest",
        lambda _repo: {**manifest, "sha256": "0" * 64},
    )
    with pytest.raises(benchmark.BenchmarkError, match="changed between replicates"):
        benchmark._verify_implementation_manifest(repo, manifest)


def test_require_clean_git_rejects_index_or_worktree_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dirty = benchmark.subprocess.CompletedProcess([], 0, " M tracked.py\n", "")
    monkeypatch.setattr(benchmark.subprocess, "run", lambda *_args, **_kwargs: dirty)

    with pytest.raises(benchmark.BenchmarkError, match="clean Git index and worktree"):
        benchmark._require_clean_git(tmp_path)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda result: result.update({"schema_version": True}), "result_kind"),
        (lambda result: result["workload"].update({"source": "cbre"}), "workload"),
        (lambda result: result.update({"profile": "production-current"}), "profile"),
        (lambda result: result["requested"].update({"global_pages": 5}), "requested"),
        (
            lambda result: result["worker_contract"].update({"schema_version": True}),
            "worker_contract",
        ),
        (
            lambda result: result["live_admission"].update(
                {"snapshot_sha256": "a" * 40}
            ),
            "live_admission",
        ),
        (lambda result: result["replicates"][0].pop("host_samples"), "host_telemetry"),
        (
            lambda result: result["replicates"][0]["guard_telemetry"].pop(
                "stderr_sha256"
            ),
            "guard_telemetry",
        ),
        (
            lambda result: result["replicates"][0].pop("resources_after"),
            "resources",
        ),
        (
            lambda result: result["replicates"][0]["performance"]["metrics"][
                "requests"
            ].pop("status_counts"),
            "performance_telemetry",
        ),
        (
            lambda result: result["replicates"][0]["performance"]["metrics"][
                "requests"
            ]["error_categories"].pop("unknown"),
            "performance_telemetry",
        ),
        (
            lambda result: result["replicates"][0]["record_evidence"][0].update(
                {"transaction_type_match": False}
            ),
            "record_evidence",
        ),
        (
            lambda result: result["replicates"][0].update(
                {"remote_settlement_unknown": None}
            ),
            "settlement",
        ),
    ],
)
def test_compare_never_adopts_missing_or_drifted_evidence(mutation, reason) -> None:
    baseline = _comparison_result(100)
    candidate = _comparison_result(120, "candidate")
    mutation(candidate)

    comparison = benchmark.compare_results(baseline, candidate)

    assert comparison["decision"] == "no_adoption_decision"
    assert comparison["state"] == "inconclusive"
    assert any(reason in item for item in comparison["reasons"])


def test_summarize_accepts_value_changes_but_rejects_supported_channel_drops(
    tmp_path: Path,
) -> None:
    sample = _sample(tmp_path)
    contract = benchmark._worker_contract(128, 10)
    sample_sha256 = hashlib.sha256(benchmark._canonical(sample)).hexdigest()
    replicate = tmp_path / "replicate"
    replicate.mkdir()
    generation = "2026-09-13T010000Z-abcdefabcdef"
    rows = []
    for row in sample["details"]:
        historic_native = row["historic"]["native"]
        rows.append(
            {
                "sample_index": row["sample_index"],
                "sample_id": row["sample_id"],
                "latency_ms": 10,
                "transaction_type": benchmark._transaction_type(
                    row["transaction_class"]
                ),
                "normalized": {
                    "id": row["id"],
                    "url": row["url"],
                    "transactionType": benchmark._transaction_type(
                        row["transaction_class"]
                    ),
                    "detailObservedAt": "2026-09-13T01:00:00Z",
                    "freshnessProvenance": {
                        "cacheDisposition": "live",
                        "generationId": generation,
                    },
                },
                "native": {
                    "counts": historic_native["counts"],
                    "fingerprints": {
                        key: "f" * 64 for key in historic_native["fingerprints"]
                    },
                },
                "fidelity": json.loads(json.dumps(row["historic"]["fidelity"])),
            }
        )
    (replicate / "worker-output.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "cre_jll_capacity_worker",
                "worker_contract_sha256": contract["sha256"],
                "generation": generation,
                "started_at": "2026-09-13T00:59:00Z",
                "rows": rows,
            }
        ),
        encoding="utf-8",
    )
    (replicate / "performance.json").write_text(
        json.dumps(_performance()), encoding="utf-8"
    )

    summary = benchmark.summarize_replicate(
        replicate,
        sample,
        60,
        sample_canonical_sha256=sample_sha256,
        worker_contract=contract,
    )

    assert summary["qualified_fresh_unique_rows"] == 128
    assert summary["historic_native_asset_matches"] == 128
    assert summary["historic_native_value_matches"] < 128
    assert summary["normalized_structural_matches"] == 128
    assert summary["normalized_structural_drops"] == []
    assert summary["comparison_state"] == "measured"

    first_supported = rows[0]["fidelity"]["supported_fields"]
    assert first_supported
    channel, field = first_supported[0].split(".", 1)
    rows[0]["fidelity"]["fields"][channel][field] = False
    rows[0]["fidelity"] = benchmark._structural_fidelity(rows[0]["fidelity"]["fields"])
    (replicate / "worker-output.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "cre_jll_capacity_worker",
                "worker_contract_sha256": contract["sha256"],
                "generation": generation,
                "started_at": "2026-09-13T00:59:00Z",
                "rows": rows,
            }
        ),
        encoding="utf-8",
    )

    failed = benchmark.summarize_replicate(
        replicate,
        sample,
        60,
        sample_canonical_sha256=sample_sha256,
        worker_contract=contract,
    )
    assert failed["qualified_fresh_unique_rows"] == 0
    assert failed["normalized_structural_matches"] == 127
    assert failed["comparison_state"] == "quality_failed"
