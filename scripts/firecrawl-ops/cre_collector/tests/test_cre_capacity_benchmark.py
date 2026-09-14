"""Pure, no-network contracts for the JLL capacity benchmark adapter."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import cre_capacity_benchmark as benchmark
import cre_capacity_experiment as experiment


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


def test_validate_admission_requires_exact_profile_and_idle_loopback() -> None:
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    source_sha = "a" * 40
    effective = {
        "repo": {
            "git_sha": source_sha,
            "dirty": False,
            "execution_inputs_sha256": {
                key: str(index) * 64
                for index, key in enumerate(
                    benchmark.capacity_runtime.EXECUTION_INPUTS, 1
                )
            },
        },
        "host": {
            "orb_status": "Running",
            "orbstack_memory_mib": 32768,
            "docker_context": "orbstack",
            "docker_memtotal_bytes": 33_669_808_128,
        },
        "api": {
            "nano_cpus": 2_000_000_000,
            "memory_bytes": 8_589_934_592,
            "swap_bytes": 0,
            "cgroup_memory_max": 8_589_934_592,
            "cgroup_memory_current": 1_073_741_824,
            "cgroup_swap_max": 0,
            "port_bindings": {"3002/tcp": [{"HostPort": "3102"}]},
            "network_mode": "firecrawl_backend",
        },
        "browser": {
            "nano_cpus": 6_000_000_000,
            "page_slots": "10",
            "pids_limit": 768,
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
    effective["transition_sha256"] = benchmark.capacity_runtime.transition_fingerprint(
        effective
    )
    effective["snapshot_sha256"] = benchmark.capacity_runtime.snapshot_fingerprint(
        effective
    )
    receipt = {
        "schema_version": 1,
        "kind": benchmark.ADMISSION_KIND,
        "admitted": True,
        "profile": "bold-jll-128",
        "config_sha256": digest,
        "source_git_sha": source_sha,
        "created_at": "2026-09-13T01:00:00+00:00",
        "expires_after_seconds": 600,
        "writes": "forbidden",
        "checks": {"candidate": True, "preserved": True},
        "effective": effective,
    }

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

    assert (
        benchmark._settlement_snapshot(
            "http://localhost:3102", "http://localhost:3103/health"
        )["idle"]
        is True
    )


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
    ("failure", "error_type"),
    [
        (benchmark.BenchmarkError("telemetry"), "BenchmarkError"),
        (KeyboardInterrupt(), "KeyboardInterrupt"),
    ],
)
def test_worker_monitor_failure_or_interrupt_terminates_and_records_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    error_type: str,
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
    monkeypatch.setattr(
        benchmark.subprocess, "Popen", lambda *_args, **_kwargs: process
    )

    code, _samples, reason = benchmark._run_worker(
        repo_root=repo,
        sample_path=sample,
        replicate_dir=replicate,
        requested={
            "jll_detail_concurrency": 10,
            "host_cpu_sample_seconds": 2,
            "host_cpu_guard_percent": 90,
            "host_cpu_guard_seconds": 30,
        },
        api_url="http://127.0.0.1:3102",
        timeout_seconds=60,
        expected_details=128,
    )

    assert (code, reason) == (-15, "monitor_telemetry_failure")
    assert terminated == [process]
    guard = json.loads((replicate / "guard.json").read_text())
    assert guard["monitor_error_type"] == error_type


def test_post_worker_settlement_failure_persists_unknown_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    sample_path = tmp_path / "sample.json"
    sample_path.write_text("{}", encoding="utf-8")
    calls = 0

    def settlement(_api, _browser):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"idle": True, "observed_at": "before"}
        raise benchmark.BenchmarkError("settlement unavailable")

    monkeypatch.setattr(benchmark, "verify_live_admission", lambda *_args: {})
    monkeypatch.setattr(benchmark, "verify_sample_provenance", lambda *_args: {})
    monkeypatch.setattr(benchmark, "_settlement_snapshot", settlement)
    monkeypatch.setattr(benchmark, "_resource_snapshot", lambda: {"complete": False})
    monkeypatch.setattr(benchmark, "_run_worker", lambda **_kwargs: (0, [], None))

    result = benchmark.run_benchmark(
        repo_root=tmp_path,
        artifact_root=artifact,
        sample_path=sample_path,
        sample={"inventory_sha256": "a" * 64},
        profile=profile,
        profile_name="bold-jll-128",
        config_sha256=digest,
        admission={
            "source_git_sha": "a" * 40,
            "endpoints": {
                "api_url": "http://127.0.0.1:3102",
                "browser_health_url": "http://127.0.0.1:3103/health",
            },
        },
        timeout_seconds=60,
    )

    assert result["completed"] is False
    assert result["replicates"][0]["source_owned_settlement"] == "unknown"
    assert (artifact / "result.json").is_file()


def _comparison_result(rate: float) -> dict[str, object]:
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
                "quality_errors": [],
                "comparison_state": "measured",
                "resource_verdict": {"state": "measured"},
                "source_owned_settlement": "locally_awaited_terminal",
                "settlement_after": {"idle": True},
                "remote_settlement_unknown": 0,
                "provider_cooldown": {"required": False},
                "latency_ms": {"p50": 10, "p95": 20, "p99": 30},
            }
        )
    return {
        "kind": benchmark.RESULT_KIND,
        "mode": "run",
        "completed": True,
        "comparison_state": "complete",
        "sample_inventory_sha256": "a" * 64,
        "sample_manifest_sha256": "b" * 64,
        "source_git_sha": "c" * 40,
        "worker_source_sha256": "d" * 64,
        "freshness_policy": {"firecrawl_max_age": 0},
        "workload": {
            "source": "jll",
            "details": 128,
            "replicates": 3,
            "writes": "forbidden",
        },
        "requested": {"browser_cpus": 2},
        "safety": {"database_writes": 0, "canonical_cache_writes": 0},
        "replicates": replicates,
    }


def test_compare_results_requires_matched_complete_evidence_and_fifteen_percent() -> (
    None
):
    baseline = _comparison_result(100)
    candidate = _comparison_result(115)
    repo = Path(__file__).resolve().parents[4]
    baseline["worker_source_sha256"] = hashlib.sha256(
        benchmark._worker_source(repo, concurrency=4).encode()
    ).hexdigest()
    candidate["worker_source_sha256"] = hashlib.sha256(
        benchmark._worker_source(repo, concurrency=10).encode()
    ).hexdigest()
    candidate["requested"] = {"browser_cpus": 6}
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


def test_compare_results_safe_negative_and_mismatch_are_nonfatal() -> None:
    baseline = _comparison_result(100)
    candidate = _comparison_result(114)
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
    candidate.write_text(json.dumps(_comparison_result(114)), encoding="utf-8")

    code = benchmark.main(
        [
            "--compare-baseline",
            str(baseline),
            "--compare-candidate",
            str(candidate),
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "do_not_adopt"


def test_summarize_replicate_fails_closed_on_native_delta_and_remote_timeout(
    tmp_path: Path,
) -> None:
    sample = _sample(tmp_path)
    replicate = tmp_path / "replicate"
    replicate.mkdir()
    rows = []
    for row in sample["details"]:
        rows.append(
            {
                "latency_ms": 10,
                "normalized": {
                    "id": row["id"],
                    "url": row["url"],
                    "detailObservedAt": "2026-09-13T01:00:00Z",
                    "freshnessProvenance": {"cacheDisposition": "live"},
                },
                "native": {"fingerprints": {}},
            }
        )
    (replicate / "worker-output.json").write_text(
        json.dumps(
            {
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

    summary = benchmark.summarize_replicate(replicate, sample, 60)

    assert summary["qualified_fresh_unique_rows"] == 0
    assert summary["native_asset_deltas"] == 128
    assert summary["remote_settlement_unknown"] == 1
    assert summary["provider_cooldown"]["required"] is False
    assert summary["comparison_state"] == "quality_failed"
