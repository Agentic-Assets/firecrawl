"""Pure, no-network contracts for the JLL capacity benchmark adapter."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import cre_capacity_benchmark as benchmark
import cre_capacity_experiment as experiment
import cre_checkpoint_refresh as refresh
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


def test_native_evidence_uses_the_bounded_jll_asset_contract_and_worker_import() -> (
    None
):
    property_value = {
        "images": [
            "https://cdn.example/image.jpg",
            {"image": "https://cdn.example/preview.jpg"},
        ],
        "brochures": [{"file": "https://cdn.example/brochure.pdf"}],
        "floorPlans": {
            "images": [{"image": "https://cdn.example/floor.jpg"}],
            "files": [{"download": "https://cdn.example/floor.pdf"}],
        },
        "videos": [{"url": "https://video.example/watch", "caption": "$3.25M"}],
        "virtualTours": {"url": "https://tour.example/virtual"},
        "view360URLs": ["https://tour.example/360"],
    }

    native = benchmark._native_evidence(property_value)

    assert native["counts"] == {
        "images": 2,
        "brochures": 1,
        "floor_plans": 2,
        "videos": 1,
        "virtual_tours": 1,
        "view_360": 1,
    }
    assert native["shape"] == sorted(native["counts"])
    assert (
        benchmark._native_evidence(
            {"videos": {"nested": {"url": "https://unsafe.example/unbounded"}}}
        )["counts"]["videos"]
        == 0
    )
    source = benchmark._worker_source(Path(__file__).resolve().parents[4])
    assert "jllNativeAssetUrls" in source
    assert 'jllNativeAssetUrls(property.videos, "videos")' in source
    assert 'jllNativeAssetUrls(property.floorPlans, "floorPlans")' in source


def test_only_a_caller_held_lock_can_retain_a_benchmark_interlock(
    tmp_path: Path,
) -> None:
    with pytest.raises(benchmark.BenchmarkError, match="caller-held canonical lock"):
        benchmark.run_benchmark(
            repo_root=tmp_path,
            artifact_root=tmp_path,
            sample_path=tmp_path / "sample.json",
            sample={},
            profile={},
            profile_name="candidate",
            config_sha256="a" * 64,
            admission={},
            admission_path=tmp_path / "admission.json",
            timeout_seconds=1,
            _retain_benchmark_interlock=True,
        )


def test_prearmed_benchmark_interlock_requires_owned_active_marker(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    with benchmark.SharedLock(lock_path) as held_lock:
        with pytest.raises(benchmark.BenchmarkError, match="no active interlock"):
            benchmark._require_prearmed_benchmark_interlock(held_lock)
        held_lock.arm_benchmark({"state": "active"})
        benchmark._require_prearmed_benchmark_interlock(held_lock)


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


def test_atomic_private_json_reports_unknown_durability_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "capacity-benchmark-quarantine.json"
    real_fsync = benchmark.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("directory fsync failed after rename")
        real_fsync(descriptor)

    monkeypatch.setattr(benchmark.os, "fsync", fail_directory_fsync)
    with pytest.raises(
        benchmark.AtomicPrivateJsonDurabilityError,
        match="rename durability is unknown",
    ):
        benchmark._atomic_private_json(target, {"state": "quarantined"})

    assert target.is_file()


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
    assert validated == receipt
    assert "endpoints" not in validated
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


def test_validate_admission_accepts_the_matched_baseline_profile() -> None:
    profile, digest = experiment.load_profile(
        experiment.DEFAULT_CONFIG, "production-current"
    )
    source_sha = "a" * 40
    receipt = _admission(Path("/private/review-grants"), source_sha=source_sha)
    receipt["profile"] = "production-current"
    receipt["config_sha256"] = digest
    receipt["effective"] = _runtime_public(source_sha, "baseline")
    receipt["source_git_sha"] = source_sha

    validated = benchmark.validate_admission(
        receipt,
        profile,
        "production-current",
        digest,
        source_git_sha=source_sha,
        now=benchmark.datetime(2026, 9, 13, 1, 5, tzinfo=benchmark.UTC),
    )

    assert validated == receipt


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
    rabbitmq = (
        Path(__file__).with_name("fixtures") / "rabbitmq-3.13.7-idle.txt"
    ).read_text(encoding="utf-8")
    outputs = iter(
        [
            rabbitmq,
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
    assert result["rabbitmq_queue_count"] == 4
    assert result["nuq"]["queue_scrape_total"] == 0


def test_settlement_backends_rejects_duplicate_rabbitmq_queue_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark, "_http_json", lambda _url: {"data": {"crawls": []}})
    outputs = iter(
        [
            (
                "name\tmessages_ready\tmessages_unacknowledged\n"
                "extract.jobs\t0\t0\n"
                "extract.jobs\t0\t0\n"
            ),
            (
                "queue_crawl_finished_total|0\n"
                "queue_scrape_backlog_total|0\n"
                "queue_scrape_total|0\n"
            ),
        ]
    )
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda *_args, **_kwargs: benchmark.subprocess.CompletedProcess(
            [], 0, next(outputs), ""
        ),
    )

    with pytest.raises(benchmark.BenchmarkError, match="RabbitMQ"):
        benchmark._settlement_backends("http://127.0.0.1:3102")


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


def test_worker_artifact_uses_explicit_esm_extension_outside_package_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    tsx = repo / "scripts/firecrawl-ops/cre_collector/node_modules/.bin/tsx"
    tsx.parent.mkdir(parents=True)
    tsx.write_text("stub", encoding="utf-8")
    sample = tmp_path / "sample.json"
    sample.write_text("{}", encoding="utf-8")
    replicate = tmp_path / "replicate-1"

    class StartedProcess:
        pid = 123
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    argv: list[str] = []

    def popen(command, **_kwargs):
        argv.extend(command)
        return StartedProcess()

    monkeypatch.setattr(benchmark.subprocess, "Popen", popen)
    monkeypatch.setattr(benchmark, "_cpu_ticks", lambda: (1, 1, 1, 1))

    code, samples, reason = benchmark._run_worker(
        repo_root=repo,
        sample_path=sample,
        replicate_dir=replicate,
        requested={
            "jll_detail_concurrency": 10,
            "host_cpu_sample_seconds": 2,
            "host_cpu_guard_seconds": 30,
            "host_cpu_guard_percent": 90,
        },
        api_url="http://127.0.0.1:3002",
        timeout_seconds=10,
        expected_details=128,
    )

    assert code == 0
    assert samples == []
    assert reason is None
    assert argv[-1] == str(replicate / "worker.mts")
    assert (replicate / "worker.mts").is_file()


def test_generated_worker_compiles_as_esm_outside_package_scope(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[4]
    tsx = repo / "scripts/firecrawl-ops/cre_collector/node_modules/.bin/tsx"
    if not tsx.is_file():
        pytest.skip("collector tsx dependency is unavailable")

    worker = tmp_path / "worker.mts"
    worker.write_text(benchmark._worker_source(repo), encoding="utf-8")
    sample = tmp_path / "invalid-sample.json"
    sample.write_text("{}", encoding="utf-8")
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "CRE_BENCHMARK_SAMPLE": str(sample),
        "CRE_BENCHMARK_OUTPUT": str(tmp_path / "output.json"),
        "JLL_DETAIL_CONCURRENCY": "10",
    }

    completed = subprocess.run(
        [str(tsx), str(worker)],
        cwd=repo / "scripts/firecrawl-ops/cre_collector",
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert completed.returncode != 0
    assert "invalid exact JLL benchmark sample" in completed.stderr
    assert "Top-level await is currently not supported" not in completed.stderr


def test_darwin_cpu_percent_uses_recorded_user_system_idle_nice_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark.sys, "platform", "darwin")
    before = (118_993_452, 45_731_497, 387_741_893, 0)
    after = (118_993_706, 45_731_643, 387_745_097, 0)

    assert benchmark._cpu_percent(before, after) == pytest.approx(
        100 * (254 + 146) / (254 + 146 + 3_204)
    )


def test_linux_cpu_percent_includes_idle_and_iowait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark.sys, "platform", "linux")

    assert benchmark._cpu_percent((0, 0, 0, 0, 0), (100, 10, 20, 700, 170)) == 13


@pytest.mark.parametrize(
    ("before", "after", "message"),
    [
        ((1, 2, 3, 4), (1, 2, 3, 4), "did not advance"),
        ((10, 20, 30, 40), (11, 21, 29, 41), "moved backwards"),
    ],
)
def test_linux_cpu_percent_rejects_zero_delta_or_counter_reset(
    before: tuple[int, ...],
    after: tuple[int, ...],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(benchmark.sys, "platform", "linux")

    with pytest.raises(benchmark.BenchmarkError, match=message):
        benchmark._cpu_percent(before, after)


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
            "classification": "active_success",
            "attrition": None,
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
        "predeclared_cohort_denominator": 128,
        "eligible_denominator": 128,
        "eligible_sample_ids_sha256": hashlib.sha256(
            benchmark._canonical(sample_ids)
        ).hexdigest(),
        "attrition_sample_ids_sha256": hashlib.sha256(
            benchmark._canonical([])
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
                "current_active_successes": 128,
                "confirmed_attrition": 0,
                "individually_qualified_rows": 128,
                "parser_failures": 0,
                "transport_failures": 0,
                "fidelity_failures": 0,
                "predeclared_cohort_denominator": 128,
                "predeclared_eligible_denominator": 128,
                "eligible_denominator": 128,
                "eligible_rows": 128,
                "cohort_rates": {
                    "current_active_successes": 1,
                    "confirmed_attrition": 0,
                    "individually_qualified_rows": 1,
                    "parser_failures": 0,
                    "transport_failures": 0,
                    "fidelity_failures": 0,
                    "eligible_rows": 1,
                },
                "cohort_throughput_per_minute": {
                    "current_active_successes": rate,
                    "confirmed_attrition": 0,
                    "individually_qualified_rows": rate,
                    "eligible_rows": rate,
                },
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


def _write_comparison_artifact(
    tmp_path: Path,
    sample: dict[str, object],
    rate: float,
    variant: str,
    *,
    attrition: bool = False,
    artifact_root: Path | None = None,
) -> tuple[dict[str, object], Path]:
    """Build one complete rehashable comparison artifact without live I/O."""
    root = (
        artifact_root.resolve()
        if artifact_root is not None
        else (tmp_path / f"artifact-{variant}-{int(rate)}").resolve()
    )
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    sample_path = root / "sample.json"
    sample_path.write_bytes(benchmark._canonical(sample))
    result = _comparison_result(rate, variant)
    requested = result["requested"]
    assert isinstance(requested, dict)
    contract = benchmark._worker_contract(128, requested["jll_detail_concurrency"])
    result["sample_inventory_sha256"] = sample["inventory_sha256"]
    result["sample_manifest_sha256"] = hashlib.sha256(
        sample_path.read_bytes()
    ).hexdigest()
    result["sample_canonical_sha256"] = hashlib.sha256(
        benchmark._canonical(sample)
    ).hexdigest()
    result["worker_contract"] = contract
    result["worker_source_sha256"] = hashlib.sha256(
        benchmark._worker_source(
            Path(__file__).resolve().parents[4],
            concurrency=requested["jll_detail_concurrency"],
        ).encode()
    ).hexdigest()
    generation = f"2026-09-14T010000Z-{variant}fixture"
    cache_root = Path(sample["population"]["cache_directory"])
    for number, replicate in enumerate(result["replicates"], 1):
        replicate_dir = root / f"replicate-{number}"
        raw_cache = replicate_dir / "raw-cache"
        raw_cache.mkdir(parents=True, mode=0o700)
        replicate_dir.chmod(0o700)
        raw_cache.chmod(0o700)
        (replicate_dir / "worker.mts").write_text(
            benchmark._worker_source(
                Path(__file__).resolve().parents[4],
                expected_details=128,
                concurrency=int(requested["jll_detail_concurrency"]),
            ),
            encoding="utf-8",
        )
        rows = _benchmark_success_rows(sample, generation)
        for index, row in enumerate(rows):
            detail = sample["details"][index]
            source_cache = cache_root / detail["historic"]["cache_file"]
            cached = json.loads(source_cache.read_text(encoding="utf-8"))
            if attrition and index == 0:
                cached["rawHtml"] = (
                    '<script id="__NEXT_DATA__" type="application/json">'
                    + json.dumps(
                        {
                            "props": {
                                "pageProps": {
                                    "notFound": True,
                                    "error": {
                                        "statusCode": 404,
                                        "message": "Not Found",
                                    },
                                }
                            }
                        }
                    )
                    + "</script>"
                )
            cached.update(
                {
                    "cachedAt": "2026-09-14T01:00:01Z",
                    "detailObservedAt": "2026-09-14T01:00:01Z",
                    "generationId": generation,
                    "metadata": {
                        "statusCode": 404 if attrition and index == 0 else 200
                    },
                }
            )
            cache_path = raw_cache / f"{index:03}.json"
            cache_path.write_bytes(benchmark._canonical(cached))
            raw_bytes = cache_path.read_bytes()
            raw_html = cached["rawHtml"]
            if attrition and index == 0:
                row["normalized"] = {
                    "id": detail["id"],
                    "url": detail["url"],
                    "transactionType": row["transaction_type"],
                    "detailError": "missing property in __NEXT_DATA__",
                }
                row.pop("native", None)
                row.pop("fidelity", None)
            else:
                row["native"].update(
                    {
                        "raw_cache_file": str(cache_path),
                        "raw_cache_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                    }
                )
            row["observation"] = {
                "cache_readable": True,
                "cache_url": detail["url"],
                "cache_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "raw_html_sha256": hashlib.sha256(raw_html.encode()).hexdigest(),
                "cached_at": cached["cachedAt"],
                "detail_observed_at": cached["detailObservedAt"],
                "generation_id": generation,
                "http_status": 404 if attrition and index == 0 else 200,
                "next_data_valid": True,
                "explicit_not_found": attrition and index == 0,
                "no_property": attrition and index == 0,
                "provider_challenge": False,
            }
            if not (attrition and index == 0):
                row["normalized"]["detailObservedAt"] = cached["detailObservedAt"]
                row["normalized"]["freshnessProvenance"] = {
                    "cacheDisposition": "live",
                    "generationId": generation,
                }
        _write_worker_result(
            replicate_dir,
            contract,
            generation,
            rows,
            performance_concurrency=int(requested["jll_detail_concurrency"]),
        )
        summary = benchmark.summarize_replicate(
            replicate_dir,
            sample,
            round(128 * 60 / rate, 3),
            sample_canonical_sha256=result["sample_canonical_sha256"],
            worker_contract=contract,
        )
        replicate.update(summary)
        replicate["guard_telemetry"]["worker_source_sha256"] = result[
            "worker_source_sha256"
        ]
    admission = {
        "profile": result["profile"],
        "config_sha256": result["config_sha256"],
        "source_git_sha": result["source_git_sha"],
        "review_approval_nonce_sha256": "2" * 64,
    }
    admission_path = root / "admission.json"
    admission_path.write_bytes(benchmark._canonical(admission))
    result["admission_sha256"] = hashlib.sha256(
        benchmark._canonical(admission)
    ).hexdigest()
    marker = {
        "kind": "cre_capacity_admission_consumption",
        "admission_sha256": result["admission_sha256"],
        "review_benchmark_grant_sha256": result["review_benchmark_grant_sha256"],
        "review_approval_nonce_sha256": result["review_approval_nonce_sha256"],
    }
    marker_path = root / "admission-consumption.json"
    marker_path.write_bytes(benchmark._canonical(marker))
    result["admission_consumption_sha256"] = hashlib.sha256(
        marker_path.read_bytes()
    ).hexdigest()
    result["artifact_evidence"] = {
        "artifact_root": str(root),
        "sample_path": str(sample_path),
        "sample_manifest_sha256": result["sample_manifest_sha256"],
        "admission_path": str(admission_path),
        "admission_consumption_path": str(marker_path),
    }
    result_path = root / "result.json"
    result_path.write_bytes(benchmark._canonical(result))
    return result, result_path


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(benchmark, "SUPPORTED_BASELINE_ADMISSION_AVAILABLE", True)
    sample = _sample(tmp_path)
    baseline, baseline_path = _write_comparison_artifact(
        tmp_path, sample, 100, "baseline"
    )
    candidate, candidate_path = _write_comparison_artifact(
        tmp_path, sample, 115.001, "candidate"
    )
    assert baseline["worker_source_sha256"] != candidate["worker_source_sha256"]

    comparison = benchmark.compare_results(
        baseline,
        candidate,
        baseline_result_path=baseline_path,
        candidate_result_path=candidate_path,
    )
    assert comparison["state"] == "measured"
    assert comparison["gain_percent"] >= 15
    assert comparison["decision"] == "do_not_adopt"
    assert comparison["reasons"] == ["counterbalanced_pair_required"]
    assert comparison["candidate"]["completeness_fidelity"][
        "historic_native_asset_matches_per_replicate"
    ] == [128, 128, 128]

    candidate["replicates"][0]["resource_verdict"] = {"state": "inconclusive"}
    comparison = benchmark.compare_results(
        baseline,
        candidate,
        baseline_result_path=baseline_path,
        candidate_result_path=candidate_path,
    )
    assert comparison["state"] == "inconclusive"
    assert "candidate_replicate_1_resources" in comparison["reasons"]


def test_compare_results_safe_negative_and_mismatch_are_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(benchmark, "SUPPORTED_BASELINE_ADMISSION_AVAILABLE", True)
    sample = _sample(tmp_path)
    baseline, baseline_path = _write_comparison_artifact(
        tmp_path, sample, 100, "baseline", attrition=True
    )
    candidate, candidate_path = _write_comparison_artifact(
        tmp_path, sample, 114, "candidate"
    )
    assert (
        benchmark.compare_results(
            baseline,
            candidate,
            baseline_result_path=baseline_path,
            candidate_result_path=candidate_path,
        )["decision"]
        == "do_not_adopt"
    )

    candidate["sample_manifest_sha256"] = "e" * 64
    comparison = benchmark.compare_results(
        baseline,
        candidate,
        baseline_result_path=baseline_path,
        candidate_result_path=candidate_path,
    )
    assert comparison["state"] == "inconclusive"
    assert "mismatch_sample_manifest_sha256" in comparison["reasons"]


def test_comparison_rederives_raw_receipt_before_accepting_worker_attrition(
    tmp_path: Path,
) -> None:
    sample = _sample(tmp_path)
    result, _result_path = _write_comparison_artifact(
        tmp_path, sample, 100, "baseline", attrition=True
    )
    artifact_root = Path(result["artifact_evidence"]["artifact_root"])
    replicate_dir = artifact_root / "replicate-1"
    cache_path = replicate_dir / "raw-cache" / "000.json"
    cache = json.loads(cache_path.read_text())
    historic_path = (
        Path(sample["population"]["cache_directory"])
        / sample["details"][0]["historic"]["cache_file"]
    )
    historic = json.loads(historic_path.read_text())
    cache["rawHtml"] = historic["rawHtml"]
    cache["metadata"] = {"statusCode": 200}
    cache_path.write_bytes(benchmark._canonical(cache))

    worker_path = replicate_dir / "worker-output.json"
    worker = json.loads(worker_path.read_text())
    derived = benchmark._derived_jll_cache_observation(cache, cache_path.read_bytes())
    worker["rows"][0]["observation"] = {
        **derived,
        "http_status": 404,
        "explicit_not_found": True,
        "no_property": True,
    }
    worker_path.write_bytes(benchmark._canonical(worker))

    assert not benchmark._valid_worker_raw_cache_evidence(worker, sample, replicate_dir)
    summary = benchmark.summarize_replicate(
        replicate_dir,
        sample,
        result["replicates"][0]["wall_seconds"],
        sample_canonical_sha256=result["sample_canonical_sha256"],
        worker_contract=result["worker_contract"],
        require_raw_receipts=True,
    )
    assert summary["confirmed_attrition"] == 0
    assert summary["parser_failures"] == 1
    assert summary["comparison_state"] == "quality_failed"


def _mark_confirmed_attrition(result: dict[str, object], sample_index: int) -> None:
    """Convert a fixture row into a provenance-bound attrition tombstone."""
    for replicate in result["replicates"]:
        record = replicate["record_evidence"][sample_index]
        record.update(
            {
                "classification": "confirmed_attrition",
                "freshness_match": None,
                "native_complete": None,
                "structural_complete": None,
                "attrition": {
                    "cache_url": "https://www.us.jll.com/en/property/removed",
                    "cache_sha256": "d" * 64,
                    "raw_html_sha256": "e" * 64,
                    "cached_at": "2026-09-13T01:00:00Z",
                    "detail_observed_at": "2026-09-13T01:00:00Z",
                    "generation_id": "2026-09-13T010000Z-fixture",
                    "http_status": 404,
                },
            }
        )
        records = replicate["record_evidence"]
        eligible_ids = [
            row["sample_id"]
            for row in records
            if row["classification"] == "active_success"
        ]
        attrition_ids = [
            row["sample_id"]
            for row in records
            if row["classification"] == "confirmed_attrition"
        ]
        manifest = replicate["record_evidence_manifest"]
        manifest.update(
            {
                "eligible_denominator": len(eligible_ids),
                "eligible_sample_ids_sha256": hashlib.sha256(
                    benchmark._canonical(eligible_ids)
                ).hexdigest(),
                "attrition_sample_ids_sha256": hashlib.sha256(
                    benchmark._canonical(attrition_ids)
                ).hexdigest(),
                "records_sha256": hashlib.sha256(
                    benchmark._canonical(records)
                ).hexdigest(),
            }
        )
        replicate.update(
            {
                "current_active_successes": len(eligible_ids),
                "confirmed_attrition": len(attrition_ids),
                "individually_qualified_rows": len(eligible_ids),
                "qualified_fresh_unique_rows": len(eligible_ids),
                "freshness_matches": len(eligible_ids),
                "historic_native_asset_matches": len(eligible_ids),
                "normalized_structural_matches": len(eligible_ids),
                "eligible_denominator": len(eligible_ids),
                "eligible_rows": len(eligible_ids),
                "cohort_rates": {
                    "current_active_successes": len(eligible_ids) / 128,
                    "confirmed_attrition": len(attrition_ids) / 128,
                    "individually_qualified_rows": len(eligible_ids) / 128,
                    "parser_failures": 0,
                    "transport_failures": 0,
                    "fidelity_failures": 0,
                    "eligible_rows": len(eligible_ids) / 128,
                },
            }
        )


def test_replicate_state_keeps_confirmed_attrition_measured_for_later_replicates() -> (
    None
):
    result = _comparison_result(100)
    _mark_confirmed_attrition(result, 0)

    assert benchmark._replicate_state(result["replicates"][0], 128) == "measured"


def test_compare_excludes_asymmetric_attrition_without_replacing_cohort_rows(
    tmp_path: Path,
) -> None:
    sample = _sample(tmp_path)
    baseline, baseline_path = _write_comparison_artifact(
        tmp_path, sample, 100, "baseline", attrition=True
    )
    candidate, candidate_path = _write_comparison_artifact(
        tmp_path, sample, 120, "candidate"
    )
    comparison = benchmark.compare_results(
        baseline,
        candidate,
        baseline_result_path=baseline_path,
        candidate_result_path=candidate_path,
    )

    assert comparison["state"] == "measured"
    assert comparison["decision"] == "do_not_adopt"
    assert comparison["cohort_matching"]["state"] == "asymmetric_confirmed_attrition"
    assert comparison["cohort_matching"]["confidence"] == "reduced"
    assert (
        comparison["baseline"]["completeness_fidelity"][
            "predeclared_cohort_denominator"
        ]
        == 128
    )
    assert comparison["baseline"]["completeness_fidelity"][
        "eligible_rows_per_replicate"
    ] == [127, 127, 127]


def test_compare_cli_is_read_only_and_needs_no_artifact_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sample = _sample(tmp_path)
    _baseline, baseline = _write_comparison_artifact(tmp_path, sample, 100, "baseline")
    _candidate, candidate = _write_comparison_artifact(
        tmp_path, sample, 114, "candidate"
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
    assert comparison["decision"] == "do_not_adopt"


def test_counterbalanced_pair_comparison_rehashes_real_ab_ba_ab_artifacts(
    tmp_path: Path,
) -> None:
    sample = _sample(tmp_path)
    sample_path = tmp_path / "immutable-sample.json"
    sample_path.write_bytes(benchmark._canonical(sample))
    pair_root = tmp_path / "pair"
    pair_root.mkdir(mode=0o700)
    pair_root.chmod(0o700)
    plan = benchmark.create_counterbalanced_pair_plan(
        artifact_root=pair_root,
        sample_path=sample_path,
        evidence_mode="sealed_offline_fixture",
    )
    plan_path = pair_root / "counterbalanced-pair-plan.json"
    plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    arms = []
    for order, variant in enumerate(plan["sequence"], 1):
        rate = 100 if variant == "baseline" else 115.001
        arm_fixture_root = tmp_path / f"arm-fixture-{order}"
        arm_fixture_root.mkdir()
        result, result_path = _write_comparison_artifact(
            arm_fixture_root, sample, rate, variant
        )
        start_second = (order - 1) * 20
        finish_second = start_second + 10
        result["started_at"] = (
            f"2026-09-14T00:{start_second // 60:02d}:{start_second % 60:02d}Z"
        )
        result["finished_at"] = (
            f"2026-09-14T00:{finish_second // 60:02d}:{finish_second % 60:02d}Z"
        )
        result["pairing"] = {
            "pair_id": plan["pair_id"],
            "pair_plan_sha256": plan_sha256,
            "arm_order": order,
            "sequence": plan["sequence"],
            "max_gap_seconds": plan["max_gap_seconds"],
            "min_gap_seconds": plan["min_gap_seconds"],
        }
        result_path.write_bytes(benchmark._canonical(result))
        arms.append(
            {
                "arm_order": order,
                "variant": variant,
                "result_path": str(result_path),
                "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            }
        )
    state = {
        "schema_version": 1,
        "kind": "cre_capacity_counterbalanced_pair_state",
        "pair_id": plan["pair_id"],
        "pair_plan_sha256": plan_sha256,
        "arms": arms,
    }
    Path(plan["state_path"]).write_bytes(benchmark._canonical(state))

    comparison = benchmark.compare_counterbalanced_pair(plan_path)

    assert comparison["state"] == "measured"
    assert comparison["decision"] == "fixture_only_not_adoptable"
    assert comparison["reasons"] == ["sealed_offline_fixture_not_production_authority"]
    assert comparison["sequence"] == [
        "baseline",
        "candidate",
        "candidate",
        "baseline",
        "baseline",
        "candidate",
    ]

    arms[1]["result_sha256"] = "0" * 64
    Path(plan["state_path"]).write_bytes(benchmark._canonical(state))
    assert (
        benchmark.compare_counterbalanced_pair(plan_path)["decision"]
        == "no_adoption_decision"
    )


def test_persisted_production_pair_is_advisory_even_when_every_artifact_rehashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = _sample(tmp_path)
    sample_path = tmp_path / "immutable-sample.json"
    sample_path.write_bytes(benchmark._canonical(sample))
    pair_root = tmp_path / "pair"
    pair_root.mkdir(mode=0o700)
    pair_root.chmod(0o700)
    plan = benchmark.create_counterbalanced_pair_plan(
        artifact_root=pair_root, sample_path=sample_path
    )
    plan_path = pair_root / "counterbalanced-pair-plan.json"
    plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    arms = []
    for order, variant in enumerate(plan["sequence"], 1):
        result, result_path = _write_comparison_artifact(
            tmp_path,
            sample,
            100 if variant == "baseline" else 120,
            variant,
            artifact_root=pair_root / f"arm-{order:02d}-{variant}",
        )
        started_second = (order - 1) * 20
        result["started_at"] = (
            f"2026-09-14T00:{started_second // 60:02d}:{started_second % 60:02d}Z"
        )
        result["finished_at"] = (
            f"2026-09-14T00:{(started_second + 10) // 60:02d}:{(started_second + 10) % 60:02d}Z"
        )
        result["pairing"] = {
            "pair_id": plan["pair_id"],
            "pair_plan_sha256": plan_sha256,
            "arm_order": order,
            "sequence": plan["sequence"],
            "max_gap_seconds": plan["max_gap_seconds"],
            "min_gap_seconds": plan["min_gap_seconds"],
        }
        result_path.write_bytes(benchmark._canonical(result))
        arms.append(
            {
                "arm_order": order,
                "variant": variant,
                "result_path": str(result_path),
                "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            }
        )
    Path(plan["state_path"]).write_bytes(
        benchmark._canonical(
            {
                "schema_version": 1,
                "kind": "cre_capacity_counterbalanced_pair_state",
                "pair_id": plan["pair_id"],
                "pair_plan_sha256": plan_sha256,
                "arms": arms,
            }
        )
    )
    monkeypatch.setattr(benchmark, "_require_clean_git", lambda _root: "c" * 40)

    comparison = benchmark.compare_counterbalanced_pair(plan_path)

    assert comparison["state"] == "measured"
    assert comparison["decision"] == "candidate_for_operator_adoption"
    assert comparison["reasons"] == [
        "production_evidence_requires_governed_operator_review"
    ]


def test_production_pair_refuses_external_arm_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = _sample(tmp_path)
    sample_path = tmp_path / "immutable-sample.json"
    sample_path.write_bytes(benchmark._canonical(sample))
    pair_root = tmp_path / "pair"
    pair_root.mkdir(mode=0o700)
    pair_root.chmod(0o700)
    plan = benchmark.create_counterbalanced_pair_plan(
        artifact_root=pair_root, sample_path=sample_path
    )
    plan_path = pair_root / "counterbalanced-pair-plan.json"
    external_root = tmp_path / "external"
    external_root.mkdir()
    _result, external_result = _write_comparison_artifact(
        external_root, sample, 100, "baseline"
    )
    monkeypatch.setattr(benchmark, "_require_clean_git", lambda _root: "c" * 40)
    state = {
        "schema_version": 1,
        "kind": "cre_capacity_counterbalanced_pair_state",
        "pair_id": plan["pair_id"],
        "pair_plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "arms": [
            {
                "arm_order": order,
                "variant": variant,
                "result_path": str(external_result),
                "result_sha256": hashlib.sha256(
                    external_result.read_bytes()
                ).hexdigest(),
            }
            for order, variant in enumerate(plan["sequence"], 1)
        ],
    }
    Path(plan["state_path"]).write_bytes(benchmark._canonical(state))

    comparison = benchmark.compare_counterbalanced_pair(plan_path)

    assert comparison["decision"] == "no_adoption_decision"
    assert "pair_arm_1_artifact" in comparison["reasons"]


def test_counterbalanced_pair_step_records_next_arm_and_rolls_back_candidate_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = _sample(tmp_path)
    sample_path = tmp_path / "immutable-sample.json"
    sample_path.write_bytes(benchmark._canonical(sample))
    pair_root = tmp_path / "pair"
    pair_root.mkdir(mode=0o700)
    pair_root.chmod(0o700)
    benchmark.create_counterbalanced_pair_plan(
        artifact_root=pair_root, sample_path=sample_path
    )
    plan_path = pair_root / "counterbalanced-pair-plan.json"
    admissions: list[str] = []
    rollbacks: list[tuple[Path, str, str, bool]] = []
    lock_windows: list[str] = []
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )

    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda value, *_args, **_kwargs: {
            **value,
            "review_approval_nonce_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda _path, _profile_name, *, require_fresh: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )

    def fake_run_benchmark(**kwargs):
        admissions.append(kwargs["profile_name"])
        held_lock = kwargs.get("_held_shared_lock")
        if held_lock is not None:
            assert held_lock.held
            with pytest.raises(benchmark.LockHeldError):
                benchmark.SharedLock(lock_path).acquire()
            assert held_lock.benchmark_marker_identity is not None
            assert (lock_path / "capacity-benchmark-active.json").is_file()
            assert kwargs["_prearmed_benchmark_interlock"] is True
            lock_windows.append("benchmark")
        result_path = kwargs["artifact_root"] / "result.json"
        result_path.write_bytes(benchmark._canonical({"completed": True}))
        return {"completed": True}

    monkeypatch.setattr(benchmark, "run_benchmark", fake_run_benchmark)

    def fake_transition(receipt, profile, target, *, execute, _held_shared_lock):
        assert _held_shared_lock.held
        assert _held_shared_lock.benchmark_marker_identity is not None
        with pytest.raises(benchmark.LockHeldError):
            benchmark.SharedLock(lock_path).acquire()
        lock_windows.append("rollback")
        rollbacks.append((receipt, profile, target, execute))

    monkeypatch.setattr(benchmark.capacity_runtime, "transition", fake_transition)
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")
    receipt = tmp_path / "candidate-receipt.json"
    receipt.write_text("{}", encoding="utf-8")

    benchmark.run_counterbalanced_pair_step(
        repo_root=Path(__file__).resolve().parents[4],
        pair_plan_path=plan_path,
        admission={},
        admission_path=admission_path,
        timeout_seconds=1,
    )
    benchmark.run_counterbalanced_pair_step(
        repo_root=Path(__file__).resolve().parents[4],
        pair_plan_path=plan_path,
        admission={},
        admission_path=admission_path,
        timeout_seconds=1,
        candidate_receipt_path=receipt,
    )

    state = json.loads((pair_root / "counterbalanced-pair-state.json").read_text())
    assert admissions == ["production-current", "bold-jll-128"]
    assert [arm["variant"] for arm in state["arms"]] == ["baseline", "candidate"]
    assert rollbacks == [(receipt, "bold-jll-128", "baseline", True)]
    assert lock_windows == ["benchmark", "rollback"]
    assert not lock_path.exists()


def test_candidate_pair_step_rejects_missing_or_invalid_rollback_before_benchmark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = _sample(tmp_path)
    sample_path = tmp_path / "immutable-sample.json"
    sample_path.write_bytes(benchmark._canonical(sample))
    pair_root = tmp_path / "pair"
    pair_root.mkdir(mode=0o700)
    pair_root.chmod(0o700)
    plan = benchmark.create_counterbalanced_pair_plan(
        artifact_root=pair_root, sample_path=sample_path
    )
    plan_path = pair_root / "counterbalanced-pair-plan.json"
    benchmark._atomic_private_json(
        Path(plan["state_path"]),
        {
            "schema_version": benchmark.SCHEMA_VERSION,
            "kind": benchmark.PAIR_STATE_KIND,
            "pair_id": plan["pair_id"],
            "pair_plan_sha256": benchmark._file_sha256(plan_path),
            "arms": [{"variant": "baseline"}],
        },
    )
    calls: list[str] = []
    transitions: list[tuple[Path, str, str, bool]] = []
    monkeypatch.setattr(
        benchmark, "validate_admission", lambda value, *_args, **_kwargs: value
    )
    monkeypatch.setattr(
        benchmark,
        "run_benchmark",
        lambda **_kwargs: calls.append("benchmark") or {"completed": True},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "transition",
        lambda receipt, profile, target, *, execute: transitions.append(
            (receipt, profile, target, execute)
        ),
    )
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")
    common = {
        "repo_root": Path(__file__).resolve().parents[4],
        "pair_plan_path": plan_path,
        "admission": {},
        "admission_path": admission_path,
        "timeout_seconds": 1,
    }

    with pytest.raises(benchmark.BenchmarkError, match="requires its rollback receipt"):
        benchmark.run_counterbalanced_pair_step(**common)

    invalid = tmp_path / "invalid-rollback.json"
    invalid.write_text("{}", encoding="utf-8")
    with pytest.raises(benchmark.BenchmarkError, match="rollback receipt is invalid"):
        benchmark.run_counterbalanced_pair_step(
            **common, candidate_receipt_path=invalid
        )

    assert calls == []
    assert transitions == []


def _candidate_pair_plan(tmp_path: Path) -> tuple[Path, Path, Path]:
    sample = _sample(tmp_path)
    sample_path = tmp_path / "candidate-sample.json"
    sample_path.write_bytes(benchmark._canonical(sample))
    pair_root = tmp_path / "candidate-pair"
    pair_root.mkdir(mode=0o700)
    pair_root.chmod(0o700)
    plan = benchmark.create_counterbalanced_pair_plan(
        artifact_root=pair_root, sample_path=sample_path
    )
    plan_path = pair_root / "counterbalanced-pair-plan.json"
    benchmark._atomic_private_json(
        Path(plan["state_path"]),
        {
            "schema_version": benchmark.SCHEMA_VERSION,
            "kind": benchmark.PAIR_STATE_KIND,
            "pair_id": plan["pair_id"],
            "pair_plan_sha256": benchmark._file_sha256(plan_path),
            "arms": [{"variant": "baseline"}],
        },
    )
    receipt = tmp_path / "candidate-rollback.json"
    receipt.write_text("{}", encoding="utf-8")
    return pair_root, plan_path, receipt


@pytest.mark.parametrize(
    ("run_failure", "rollback_failure", "quarantine_failure"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
    ids=(
        "unknown-settlement",
        "post-worker-error",
        "rollback-error",
        "quarantine-error",
    ),
)
def test_candidate_pair_defers_quarantine_until_after_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_failure: bool,
    rollback_failure: bool,
    quarantine_failure: bool,
) -> None:
    pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    events: list[str] = []
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )
    original_write = benchmark._atomic_private_json

    def write(path: Path, value: object) -> None:
        if quarantine_failure and path.name == benchmark.BENCHMARK_QUARANTINE_MARKER:
            raise OSError("quarantine receipt write failed")
        original_write(path, value)

    def fake_run(**kwargs):
        assert kwargs["_retain_benchmark_interlock"] is True
        lock = kwargs["_held_shared_lock"]
        assert kwargs["_prearmed_benchmark_interlock"] is True
        assert lock.benchmark_marker_identity is not None
        events.append("benchmark")
        with pytest.raises(benchmark.LockHeldError):
            benchmark.SharedLock(lock_path).acquire()
        if run_failure:
            raise OSError("post-worker benchmark failure")
        return {"completed": False}

    def fake_rollback(*_args, _held_shared_lock, **_kwargs):
        assert _held_shared_lock.held
        with pytest.raises(benchmark.LockHeldError):
            benchmark.SharedLock(lock_path).acquire()
        events.append("rollback")
        if rollback_failure:
            raise OSError("rollback failed")

    monkeypatch.setattr(benchmark, "_atomic_private_json", write)
    monkeypatch.setattr(benchmark, "run_benchmark", fake_run)
    monkeypatch.setattr(benchmark.capacity_runtime, "transition", fake_rollback)
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    expected = (
        "durable quarantine evidence publication failed"
        if quarantine_failure
        else "canonical lock is quarantined"
        if rollback_failure
        else "post-worker benchmark failure"
        if run_failure
        else "did not complete"
    )
    with pytest.raises((benchmark.BenchmarkError, OSError), match=expected):
        benchmark.run_counterbalanced_pair_step(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            admission={},
            admission_path=admission_path,
            timeout_seconds=1,
            candidate_receipt_path=receipt,
        )

    assert events == ["benchmark", "rollback"]
    assert not (pair_root / "arm-02-candidate" / "result.json").exists()
    assert not json.loads((pair_root / "counterbalanced-pair-state.json").read_text())[
        "arms"
    ][1:]
    assert (lock_path / "capacity-benchmark-active.json").is_file()
    with pytest.raises(benchmark.LockHeldError):
        benchmark.SharedLock(lock_path).acquire()
    if quarantine_failure:
        assert not (lock_path / benchmark.BENCHMARK_QUARANTINE_MARKER).exists()
    else:
        assert (lock_path / benchmark.BENCHMARK_QUARANTINE_MARKER).is_file()


def test_candidate_pair_prearms_interlock_before_benchmark_failure_and_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    events: list[str] = []
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )

    def fail_before_inner_arm(**kwargs):
        lock = kwargs["_held_shared_lock"]
        assert kwargs["_prearmed_benchmark_interlock"] is True
        assert lock.benchmark_marker_identity is not None
        assert (lock_path / "capacity-benchmark-active.json").is_file()
        events.append("benchmark")
        raise OSError("benchmark preflight failed before worker arm")

    def fail_rollback(*_args, _held_shared_lock, **_kwargs):
        assert _held_shared_lock.benchmark_marker_identity is not None
        events.append("rollback")
        raise OSError("baseline rollback failed")

    monkeypatch.setattr(benchmark, "run_benchmark", fail_before_inner_arm)
    monkeypatch.setattr(benchmark.capacity_runtime, "transition", fail_rollback)
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    with pytest.raises(benchmark.BenchmarkError, match="canonical lock is quarantined"):
        benchmark.run_counterbalanced_pair_step(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            admission={},
            admission_path=admission_path,
            timeout_seconds=1,
            candidate_receipt_path=receipt,
        )

    assert events == ["benchmark", "rollback"]
    assert (lock_path / "capacity-benchmark-active.json").is_file()
    assert (lock_path / benchmark.BENCHMARK_QUARANTINE_MARKER).is_file()
    with pytest.raises(benchmark.LockHeldError):
        benchmark.SharedLock(lock_path).acquire()


@pytest.mark.parametrize(
    ("rollback_failure", "lock_retained"),
    [(True, True), (False, False)],
    ids=("rollback-failure-quarantines", "rollback-success-releases"),
)
def test_candidate_pair_marker_arm_failure_obeys_rollback_interlock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollback_failure: bool,
    lock_retained: bool,
) -> None:
    _pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    events: list[str] = []
    original_arm = benchmark.SharedLock.arm_benchmark
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )

    def fail_arm_once(lock, evidence):
        assert evidence["kind"] == "cre_capacity_candidate_pair_active"
        events.append("arm")
        monkeypatch.setattr(benchmark.SharedLock, "arm_benchmark", original_arm)
        raise OSError("outer marker arm failed before create")

    def should_not_run(**_kwargs):
        raise AssertionError("benchmark must not run after marker-arm failure")

    def rollback(*_args, **_kwargs):
        events.append("rollback")
        if rollback_failure:
            raise OSError("baseline rollback failed")

    monkeypatch.setattr(benchmark.SharedLock, "arm_benchmark", fail_arm_once)
    monkeypatch.setattr(benchmark, "run_benchmark", should_not_run)
    monkeypatch.setattr(benchmark.capacity_runtime, "transition", rollback)
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    expected = benchmark.BenchmarkError if rollback_failure else OSError
    with pytest.raises(expected):
        benchmark.run_counterbalanced_pair_step(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            admission={},
            admission_path=admission_path,
            timeout_seconds=1,
            candidate_receipt_path=receipt,
        )

    assert events == ["arm", "rollback"]
    assert lock_path.exists() is lock_retained
    if lock_retained:
        assert (lock_path / benchmark.BENCHMARK_QUARANTINE_MARKER).is_file()
        evidence = json.loads(
            (lock_path / benchmark.BENCHMARK_QUARANTINE_MARKER).read_text()
        )
        assert evidence["reason"] == "candidate_baseline_rollback_failed"
        with pytest.raises(benchmark.LockHeldError):
            benchmark.SharedLock(lock_path).acquire()


@pytest.mark.parametrize(
    "initial_error",
    [OSError("candidate lock acquisition failed"), KeyboardInterrupt()],
    ids=("io-failure", "interrupt"),
)
def test_candidate_pair_transient_lock_failure_rolls_back_under_recovery_lock(
    initial_error: BaseException, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    original_acquire = benchmark.SharedLock.acquire
    acquire_calls = 0
    events: list[str] = []
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )

    def fail_once(lock):
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls == 1:
            raise initial_error
        return original_acquire(lock)

    def rollback(*_args, _held_shared_lock, **_kwargs):
        assert _held_shared_lock.held
        assert _held_shared_lock.benchmark_marker_identity is None
        events.append("rollback")

    monkeypatch.setattr(benchmark.SharedLock, "acquire", fail_once)
    monkeypatch.setattr(benchmark, "run_benchmark", lambda **_kwargs: pytest.fail())
    monkeypatch.setattr(benchmark.capacity_runtime, "transition", rollback)
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    with pytest.raises(type(initial_error)):
        benchmark.run_counterbalanced_pair_step(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            admission={},
            admission_path=admission_path,
            timeout_seconds=1,
            candidate_receipt_path=receipt,
        )

    assert acquire_calls == 2
    assert events == ["rollback"]
    assert not lock_path.exists()


def test_candidate_pair_partial_lease_failure_recovers_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    events: list[str] = []
    original_write = refresh.atomic_write_text
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )

    def fail_first_lease(path: Path, value: str) -> None:
        if path.name == "lease" and not events:
            events.append("lease failure")
            raise OSError("lease write failed before creation")
        original_write(path, value)

    def rollback(*_args, _held_shared_lock, **_kwargs):
        assert _held_shared_lock.held
        assert _held_shared_lock.recovery_required
        events.append("rollback")

    monkeypatch.setattr(refresh, "atomic_write_text", fail_first_lease)
    monkeypatch.setattr(benchmark, "run_benchmark", lambda **_kwargs: pytest.fail())
    monkeypatch.setattr(benchmark.capacity_runtime, "transition", rollback)
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    with pytest.raises(OSError, match="lease write failed before creation"):
        benchmark.run_counterbalanced_pair_step(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            admission={},
            admission_path=admission_path,
            timeout_seconds=1,
            candidate_receipt_path=receipt,
        )

    assert events == ["lease failure", "rollback"]
    assert not lock_path.exists()


@pytest.mark.parametrize("operation", ["write", "fsync"])
def test_candidate_authority_initialization_failure_recovers_under_owned_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    original_write = refresh.os.write
    original_fsync = refresh.os.fsync
    failed = False
    events: list[str] = []

    def fail_once_write(descriptor, payload):
        nonlocal failed
        if operation == "write" and not failed:
            failed = True
            raise OSError("authority write failed")
        return original_write(descriptor, payload)

    def fail_once_fsync(descriptor):
        nonlocal failed
        if operation == "fsync" and not failed:
            failed = True
            raise OSError("authority fsync failed")
        return original_fsync(descriptor)

    monkeypatch.setattr(refresh.os, "write", fail_once_write)
    monkeypatch.setattr(refresh.os, "fsync", fail_once_fsync)
    with benchmark._candidate_rollback_lock(lock_path) as (held_lock, initial_error):
        assert isinstance(initial_error, OSError)
        descriptor = held_lock._owned_directory_fd()
        os.close(descriptor)
        events.append("rollback")
        held_lock.clear_recovery_requirement()

    assert events == ["rollback"]
    assert not lock_path.exists()
    assert lock_path.with_name(f"{lock_path.name}.authority").is_file()


def test_candidate_pair_stale_reclaim_lease_failure_recovers_and_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    lock_path.mkdir(parents=True)
    (lock_path / "pid").write_text("99999999 1\n", encoding="utf-8")
    (lock_path / "lease").write_text("stale-lease\n", encoding="utf-8")
    events: list[str] = []
    original_write = refresh.atomic_write_text
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )

    def fail_first_lease(path: Path, value: str) -> None:
        if path.name == "lease" and not events:
            events.append("lease failure")
            raise OSError("stale-reclaim lease write failed before creation")
        original_write(path, value)

    def rollback(*_args, _held_shared_lock, **_kwargs):
        assert _held_shared_lock.held
        assert _held_shared_lock.recovery_required
        events.append("rollback")

    monkeypatch.setattr(refresh, "atomic_write_text", fail_first_lease)
    monkeypatch.setattr(benchmark, "run_benchmark", lambda **_kwargs: pytest.fail())
    monkeypatch.setattr(benchmark.capacity_runtime, "transition", rollback)
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    with pytest.raises(OSError, match="stale-reclaim lease write failed"):
        benchmark.run_counterbalanced_pair_step(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            admission={},
            admission_path=admission_path,
            timeout_seconds=1,
            candidate_receipt_path=receipt,
        )

    assert events == ["lease failure", "rollback"]
    assert not lock_path.exists()


def test_candidate_pair_partial_cleanup_failure_never_rolls_back_unlocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    transitions: list[str] = []
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )

    def fail_first_lease(path: Path, _value: str) -> None:
        if path.name == "lease":
            (path.parent / ".lease.abandoned.tmp").write_text("partial")
            raise OSError("lease write failed before creation")

    original_unlink = refresh.os.unlink

    def fail_partial_cleanup(path, *args, **kwargs):
        if path == ".lease.abandoned.tmp":
            raise OSError("partial cleanup failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(refresh, "atomic_write_text", fail_first_lease)
    monkeypatch.setattr(refresh.os, "unlink", fail_partial_cleanup)
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "transition",
        lambda *_args, **_kwargs: transitions.append("unsafe rollback"),
    )
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    with pytest.raises(
        benchmark.BenchmarkError,
        match="cannot acquire a verified canonical recovery lock",
    ):
        benchmark.run_counterbalanced_pair_step(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            admission={},
            admission_path=admission_path,
            timeout_seconds=1,
            candidate_receipt_path=receipt,
        )

    assert transitions == []
    assert refresh._lock_requires_operator_recovery(lock_path)


@pytest.mark.parametrize("initial_error", [OSError("disk failed"), KeyboardInterrupt()])
def test_candidate_pair_unavailable_recovery_lock_never_rolls_back_unlocked(
    initial_error: BaseException, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    transitions: list[str] = []
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )
    monkeypatch.setattr(
        benchmark.SharedLock,
        "acquire",
        lambda _lock: (_ for _ in ()).throw(initial_error),
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "transition",
        lambda *_args, **_kwargs: transitions.append("unsafe rollback"),
    )
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    with pytest.raises(
        benchmark.BenchmarkError,
        match="cannot acquire a verified canonical recovery lock",
    ) as raised:
        benchmark.run_counterbalanced_pair_step(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            admission={},
            admission_path=admission_path,
            timeout_seconds=1,
            candidate_receipt_path=receipt,
        )

    assert transitions == []
    assert any("initial lock error" in note for note in raised.value.__notes__)
    assert any("recovery lock error" in note for note in raised.value.__notes__)


def test_candidate_pair_foreign_lock_never_performs_unowned_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    foreign = benchmark.SharedLock(lock_path)
    foreign.acquire()
    transitions: list[str] = []
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "transition",
        lambda *_args, **_kwargs: transitions.append("unsafe rollback"),
    )
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    try:
        with pytest.raises(
            benchmark.BenchmarkError,
            match="cannot acquire a verified canonical recovery lock",
        ):
            benchmark.run_counterbalanced_pair_step(
                repo_root=Path(__file__).resolve().parents[4],
                pair_plan_path=plan_path,
                admission={},
                admission_path=admission_path,
                timeout_seconds=1,
                candidate_receipt_path=receipt,
            )
        assert transitions == []
        assert (lock_path / "lease").read_text(encoding="utf-8") == (
            f"{foreign.lease_token}\n"
        )
    finally:
        foreign.release()


def test_candidate_pair_retains_owned_lock_when_quarantine_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    original_arm = benchmark.SharedLock.arm_benchmark
    original_write = benchmark._atomic_private_json
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )

    def fail_arm_once(_lock, _evidence):
        monkeypatch.setattr(benchmark.SharedLock, "arm_benchmark", original_arm)
        raise OSError("outer marker arm failed before create")

    def fail_quarantine(path: Path, value: object) -> None:
        if path.name == benchmark.BENCHMARK_QUARANTINE_MARKER:
            raise OSError("quarantine publication failed")
        original_write(path, value)

    monkeypatch.setattr(benchmark.SharedLock, "arm_benchmark", fail_arm_once)
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "transition",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("rollback failed")),
    )
    monkeypatch.setattr(benchmark, "_atomic_private_json", fail_quarantine)
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    with pytest.raises(
        benchmark.BenchmarkError,
        match="durable quarantine evidence publication failed; operator intervention is required",
    ):
        benchmark.run_counterbalanced_pair_step(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            admission={},
            admission_path=admission_path,
            timeout_seconds=1,
            candidate_receipt_path=receipt,
        )

    assert lock_path.is_dir()
    assert not (lock_path / "capacity-benchmark-active.json").exists()
    assert not (lock_path / benchmark.BENCHMARK_QUARANTINE_MARKER).exists()
    assert (lock_path / "pid").is_file()
    assert (lock_path / "lease").is_file()
    with pytest.raises(benchmark.LockHeldError, match="requires operator recovery"):
        benchmark.SharedLock(lock_path).acquire()


def test_candidate_pair_unknown_quarantine_durability_blocks_stale_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    original_arm = benchmark.SharedLock.arm_benchmark
    original_write = benchmark._atomic_private_json
    real_fsync = benchmark.os.fsync
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )

    def fail_arm_once(_lock, _evidence):
        monkeypatch.setattr(benchmark.SharedLock, "arm_benchmark", original_arm)
        raise OSError("outer marker arm failed before create")

    def rename_then_fail_directory_fsync(path: Path, value: object) -> None:
        if path.name != benchmark.BENCHMARK_QUARANTINE_MARKER:
            original_write(path, value)
            return

        def fail_directory_fsync(descriptor: int) -> None:
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("quarantine directory fsync failed after rename")
            real_fsync(descriptor)

        monkeypatch.setattr(benchmark.os, "fsync", fail_directory_fsync)
        try:
            original_write(path, value)
        finally:
            monkeypatch.setattr(benchmark.os, "fsync", real_fsync)

    monkeypatch.setattr(benchmark.SharedLock, "arm_benchmark", fail_arm_once)
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "transition",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("rollback failed")),
    )
    monkeypatch.setattr(
        benchmark, "_atomic_private_json", rename_then_fail_directory_fsync
    )
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    with pytest.raises(
        benchmark.BenchmarkError,
        match="durable quarantine evidence publication failed; operator intervention is required",
    ):
        benchmark.run_counterbalanced_pair_step(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            admission={},
            admission_path=admission_path,
            timeout_seconds=1,
            candidate_receipt_path=receipt,
        )

    marker = lock_path / benchmark.BENCHMARK_QUARANTINE_MARKER
    assert marker.is_file()
    assert refresh._lock_requires_operator_recovery(lock_path)
    marker.unlink()
    (lock_path / "pid").write_text("99999999 1\n", encoding="utf-8")
    with pytest.raises(benchmark.LockHeldError, match="requires operator recovery"):
        benchmark.SharedLock(lock_path).acquire()


def test_candidate_pair_prearmed_interlock_survives_interrupt_until_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_root, plan_path, receipt = _candidate_pair_plan(tmp_path)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    events: list[str] = []
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(
        benchmark,
        "validate_admission",
        lambda *_args, **_kwargs: {"review_approval_nonce_sha256": "a" * 64},
    )
    monkeypatch.setattr(
        benchmark.capacity_runtime,
        "load_fresh_receipt",
        lambda *_args, **_kwargs: (
            {},
            {},
            benchmark._experiment_contract()["config_sha256"],
        ),
    )

    def interrupt_before_inner_arm(**kwargs):
        lock = kwargs["_held_shared_lock"]
        assert kwargs["_prearmed_benchmark_interlock"] is True
        assert lock.benchmark_marker_identity is not None
        events.append("benchmark")
        raise KeyboardInterrupt("benchmark preflight interrupted")

    def rollback(*_args, _held_shared_lock, **_kwargs):
        assert _held_shared_lock.benchmark_marker_identity is not None
        events.append("rollback")

    monkeypatch.setattr(benchmark, "run_benchmark", interrupt_before_inner_arm)
    monkeypatch.setattr(benchmark.capacity_runtime, "transition", rollback)
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")

    with pytest.raises(KeyboardInterrupt, match="benchmark preflight interrupted"):
        benchmark.run_counterbalanced_pair_step(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            admission={},
            admission_path=admission_path,
            timeout_seconds=1,
            candidate_receipt_path=receipt,
        )

    assert events == ["benchmark", "rollback"]
    assert (lock_path / "capacity-benchmark-active.json").is_file()
    assert (lock_path / benchmark.BENCHMARK_QUARANTINE_MARKER).is_file()
    with pytest.raises(benchmark.LockHeldError):
        benchmark.SharedLock(lock_path).acquire()


def test_caller_held_unknown_settlement_is_durably_deferred_to_pair_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setattr(benchmark, "_replicate_state", lambda *_args: "failed")
    monkeypatch.setattr(benchmark, "_run_worker", lambda **_kwargs: (0, [], None))
    monkeypatch.setattr(
        benchmark,
        "_await_idle_settlement",
        lambda *_args: {"idle": False, "state": "unknown"},
    )

    with benchmark.SharedLock(lock_path) as held_lock:
        result = benchmark.run_benchmark(
            repo_root=tmp_path,
            artifact_root=artifact,
            sample_path=sample_path,
            sample=sample,
            profile=profile,
            profile_name="bold-jll-128",
            config_sha256=digest,
            admission=admission,
            admission_path=tmp_path / "admission.json",
            timeout_seconds=60,
            _held_shared_lock=held_lock,
        )
        assert result["lock_quarantine"]["state"] == "deferred_to_pair_rollback"
        assert held_lock.held
        assert (lock_path / "pid").is_file()
        assert (lock_path / "lease").is_file()
        assert (lock_path / "capacity-benchmark-active.json").is_file()
        with pytest.raises(benchmark.LockHeldError):
            benchmark.SharedLock(lock_path).acquire()


def test_caller_held_idle_benchmark_retains_interlock_until_pair_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setattr(benchmark, "_run_worker", lambda **_kwargs: (0, [], None))
    monkeypatch.setattr(
        benchmark,
        "_await_idle_settlement",
        lambda *_args: {"idle": True, "state": "idle"},
    )

    with benchmark.SharedLock(lock_path) as held_lock:
        held_lock.arm_benchmark({"state": "outer-candidate-step"})
        result = benchmark.run_benchmark(
            repo_root=tmp_path,
            artifact_root=artifact,
            sample_path=sample_path,
            sample=sample,
            profile=profile,
            profile_name="bold-jll-128",
            config_sha256=digest,
            admission=admission,
            admission_path=tmp_path / "admission.json",
            timeout_seconds=60,
            _held_shared_lock=held_lock,
            _retain_benchmark_interlock=True,
            _prearmed_benchmark_interlock=True,
        )
        assert result["completed"] is True
        assert held_lock.benchmark_marker_identity is not None
        assert (lock_path / "capacity-benchmark-active.json").is_file()
        with pytest.raises(benchmark.LockHeldError):
            benchmark.SharedLock(lock_path).acquire()


def test_guarded_pair_controller_is_disabled_pending_governed_runtime_orchestration(
    tmp_path: Path,
) -> None:
    sample = _sample(tmp_path)
    sample_path = tmp_path / "immutable-sample.json"
    sample_path.write_bytes(benchmark._canonical(sample))
    pair_root = tmp_path / "pair"
    pair_root.mkdir(mode=0o700)
    pair_root.chmod(0o700)
    benchmark.create_counterbalanced_pair_plan(
        artifact_root=pair_root, sample_path=sample_path
    )
    plan_path = pair_root / "counterbalanced-pair-plan.json"
    admission_path = tmp_path / "admission.json"
    admission_path.write_text("{}", encoding="utf-8")
    receipt = tmp_path / "candidate-receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    arms = [
        {
            "admission": {},
            "admission_path": admission_path,
            "candidate_receipt_path": receipt if variant == "candidate" else None,
        }
        for variant in benchmark.PAIR_SEQUENCE
    ]

    with pytest.raises(benchmark.BenchmarkError, match="controller is disabled"):
        benchmark.run_counterbalanced_pair_orchestrator(
            repo_root=Path(__file__).resolve().parents[4],
            pair_plan_path=plan_path,
            arms=arms,
            timeout_seconds=1,
        )


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
    assert summary["native_asset_deltas"] == 0
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
    directory_fsyncs: list[Path] = []
    original_fsync_directory = benchmark._fsync_directory

    def fsync_directory(path: Path) -> None:
        directory_fsyncs.append(path)
        original_fsync_directory(path)

    monkeypatch.setattr(benchmark, "_fsync_directory", fsync_directory)

    marker = benchmark._consume_admission(admission_path, admission)

    assert calls[0][0][:2] == ["/usr/bin/python3", "-c"]
    assert calls[0][0][-1] == admission["review_benchmark_grant_path"]
    assert directory_fsyncs == [
        tmp_path / "out",
        tmp_path / "out" / ".capacity-admission-consumption",
    ]
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


def test_validated_admission_tamper_is_rejected_before_grant_consumption(
    tmp_path: Path,
) -> None:
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    source_sha = "a" * 40
    admission = _admission(tmp_path, source_sha=source_sha)
    admission["created_at"] = benchmark._now()
    admission["review_approval_created_at"] = benchmark._now()
    validated = benchmark.validate_admission(
        admission,
        profile,
        "bold-jll-128",
        digest,
        source_git_sha=source_sha,
    )
    admission_path = tmp_path / "admission.json"
    grant_path = Path(str(admission["review_benchmark_grant_path"]))
    benchmark._atomic_private_json(admission_path, admission)
    benchmark._atomic_private_json(grant_path, _review_grant(admission))
    tampered = {**admission, "writes": "allowed"}
    benchmark._atomic_private_json(admission_path, tampered)

    with pytest.raises(benchmark.BenchmarkError, match="changed after validation"):
        benchmark._consume_admission(
            admission_path,
            validated,
            canonical_lock_path=tmp_path / "out" / "daily" / ".cre.lock",
        )

    assert grant_path.exists()


def test_offline_runtime_admission_benchmark_and_rollback_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = benchmark.capacity_runtime
    source_sha = "a" * 40
    repo_root = Path(__file__).resolve().parents[4]
    controller_root = tmp_path / "repo"
    controlled = (
        controller_root / "tasks" / "tmp" / "cre-capacity-transition-integration"
    )
    controlled.mkdir(parents=True, mode=0o700)
    controlled.chmod(0o700)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"
    component_state = {"browser": "baseline", "api": "baseline"}
    transition_events: list[str] = []

    def current_capture() -> object:
        baseline = _runtime_public(source_sha, "baseline")
        candidate = _runtime_public(source_sha, "candidate")
        for value in (baseline, candidate):
            value["api"]["id"] = "api-fixed"
            value["browser"]["id"] = (
                "browser-baseline"
                if value["browser"]["page_slots"] == "4"
                else "browser-candidate"
            )
        public = json.loads(json.dumps(baseline))
        public["browser"] = json.loads(
            json.dumps(
                baseline["browser"]
                if component_state["browser"] == "baseline"
                else candidate["browser"]
            )
        )
        public["api"] = json.loads(
            json.dumps(
                baseline["api"]
                if component_state["api"] == "baseline"
                else candidate["api"]
            )
        )
        public["transition_sha256"] = runtime.transition_fingerprint(public)
        public["snapshot_sha256"] = runtime.snapshot_fingerprint(public)
        return runtime.RuntimeCapture(
            public=public,
            browser_env={"MAX_CONCURRENT_PAGES": public["browser"]["page_slots"]},
            api_env={},
        )

    def capture_runtime(_runner=runtime._default_runner):
        return current_capture()

    def recreate(
        _capture,
        _profile,
        state,
        _runner,
        *,
        execute=True,
        mutation_observer=None,
    ) -> None:
        assert execute is True
        if mutation_observer is not None:
            mutation_observer()
        component_state["browser"] = state
        transition_events.append(f"browser:{state}")

    def update_api(_profile, state, _runner, *, mutation_observer=None) -> None:
        if mutation_observer is not None:
            mutation_observer()
        component_state["api"] = state
        transition_events.append(f"api:{state}")

    monkeypatch.setattr(runtime, "REPO_ROOT", controller_root)
    monkeypatch.setattr(runtime, "capture_runtime", capture_runtime)
    monkeypatch.setattr(runtime, "_compose_recreate", recreate)
    monkeypatch.setattr(runtime, "_api_update", update_api)
    monkeypatch.setattr(runtime, "_canonical_transition_lock", lambda: lock_path)
    monkeypatch.setattr(
        benchmark, "canonical_shared_lock_dir", lambda *_args: lock_path
    )
    monkeypatch.setattr(benchmark, "_other_collector_process_active", lambda: False)
    monkeypatch.setattr(benchmark, "_require_clean_git", lambda _root: source_sha)
    monkeypatch.setattr(
        benchmark, "_settlement_snapshot", lambda *_args: _settlement(final=False)
    )
    monkeypatch.setattr(
        benchmark, "_await_idle_settlement", lambda *_args: _settlement(final=True)
    )
    monkeypatch.setattr(benchmark, "_resource_snapshot", _resource_snapshot)

    receipt_path = controlled / "receipt.json"
    receipt = runtime.preflight("bold-jll-128", receipt_path)
    profile, digest = experiment.load_profile(experiment.DEFAULT_CONFIG, "bold-jll-128")
    approval_path = controlled / "review-approval.json"
    approval = {
        "schema_version": runtime.SCHEMA_VERSION,
        "kind": runtime.APPROVAL_KIND,
        "profile": "bold-jll-128",
        "config_sha256": digest,
        "transition_receipt_sha256": receipt["receipt_sha256"],
        "source_git_sha": source_sha,
        "approved_by": "coordinating-review",
        "approved": True,
        "created_at": runtime.utc_now(),
        "expires_after_seconds": runtime.RECEIPT_MAX_AGE_SECONDS,
        "nonce": "e" * 64,
    }
    runtime.write_private(approval_path, approval)
    admission_path = controlled / "admission.json"
    runtime.transition(
        receipt_path,
        "bold-jll-128",
        "candidate",
        execute=True,
        admission_out=admission_path,
        approval_path=approval_path,
    )
    raw_admission = json.loads(admission_path.read_text())
    admission = benchmark.validate_admission(
        raw_admission,
        profile,
        "bold-jll-128",
        digest,
        source_git_sha=source_sha,
    )
    assert admission == raw_admission
    assert not approval_path.exists()
    grant_path = Path(raw_admission["review_benchmark_grant_path"])
    assert grant_path.exists()

    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    sample = _sample(fixture_root)
    sample_path = controlled / "jll-128-sample.json"
    benchmark._atomic_private_json(sample_path, sample)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    worker_calls: list[bool] = []

    def worker(**kwargs) -> tuple[int, list[dict[str, object]], None]:
        replicate_dir = kwargs["replicate_dir"]
        replicate_dir.mkdir(parents=True, mode=0o700)
        worker_calls.append(kwargs["review_grant"] is not None)
        generation = f"2026-09-14T01000{len(worker_calls)}Z-offline"
        started_at = benchmark._now()
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
                        "detailObservedAt": benchmark._now(),
                        "freshnessProvenance": {
                            "cacheDisposition": "live",
                            "generationId": generation,
                        },
                    },
                    "native": json.loads(json.dumps(row["historic"]["native"])),
                    "fidelity": json.loads(json.dumps(row["historic"]["fidelity"])),
                }
            )
        benchmark._atomic_private_json(
            replicate_dir / "worker-output.json",
            {
                "schema_version": 1,
                "kind": "cre_jll_capacity_worker",
                "worker_contract_sha256": benchmark._worker_contract(128, 10)["sha256"],
                "generation": generation,
                "started_at": started_at,
                "rows": rows,
            },
        )
        benchmark._atomic_private_json(
            replicate_dir / "performance.json", _performance()
        )
        return (
            0,
            [{"observed_at": benchmark._now(), "host_cpu_percent": 50.0}],
            None,
        )

    monkeypatch.setattr(benchmark, "_run_worker", worker)
    result = benchmark.run_benchmark(
        repo_root=repo_root,
        artifact_root=artifact_root,
        sample_path=sample_path,
        sample=sample,
        profile=profile,
        profile_name="bold-jll-128",
        config_sha256=digest,
        admission=admission,
        admission_path=admission_path,
        timeout_seconds=60,
    )

    assert result["completed"] is True
    assert worker_calls == [True, False, False]
    assert (
        result["admission_sha256"]
        == hashlib.sha256(benchmark._canonical(raw_admission)).hexdigest()
    )
    assert not grant_path.exists()
    assert (artifact_root / "result.json").exists()

    rollback = runtime.transition(
        receipt_path, "bold-jll-128", "baseline", execute=True
    )
    assert rollback["verified"] is True
    assert component_state == {"browser": "baseline", "api": "baseline"}
    assert transition_events == [
        "browser:candidate",
        "api:candidate",
        "browser:baseline",
        "api:baseline",
    ]
    assert not lock_path.exists()


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


def test_same_user_grant_helper_persists_rename_and_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission = _admission(tmp_path)
    admission["review_approval_created_at"] = benchmark._now()
    grant_path = Path(str(admission["review_benchmark_grant_path"]))
    benchmark._atomic_private_json(grant_path, _review_grant(admission))
    output = io.BytesIO()
    directory_fsyncs: list[int] = []
    original_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_fsyncs.append(descriptor)
        original_fsync(descriptor)

    with monkeypatch.context() as isolated:
        isolated.setattr(os, "fsync", fsync)
        isolated.setattr(sys, "argv", ["grant-helper", str(grant_path)])
        isolated.setattr(sys, "stdout", SimpleNamespace(buffer=output))
        exec(  # noqa: S102 - checked-in helper is tested without network/runtime
            compile(
                benchmark.REVIEW_BENCHMARK_GRANT_CONSUMER,
                "<offline-review-grant-helper>",
                "exec",
            ),
            {},
        )

    assert len(directory_fsyncs) == 2
    assert json.loads(output.getvalue()) == _review_grant(admission)
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


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_admission_consumption_fails_before_launch_when_directory_fsync_fails(
    fail_on_call: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission_path = tmp_path / "admission.json"
    admission = _admission(tmp_path)
    admission["review_approval_created_at"] = benchmark._now()
    grant = _review_grant(admission)
    benchmark._atomic_private_json(admission_path, admission)
    monkeypatch.setattr(
        benchmark.subprocess,
        "run",
        lambda argv, **_kwargs: benchmark.subprocess.CompletedProcess(
            argv, 0, json.dumps(grant).encode(), b""
        ),
    )
    calls = 0
    original_fsync_directory = benchmark._fsync_directory

    def fsync_directory(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == fail_on_call:
            raise benchmark.BenchmarkError("injected directory durability failure")
        original_fsync_directory(path)

    monkeypatch.setattr(benchmark, "_fsync_directory", fsync_directory)
    lock_path = tmp_path / "out" / "daily" / ".cre.lock"

    with pytest.raises(benchmark.BenchmarkError, match="durability failure"):
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
        if outcome == "unknown":
            assert (lock_path / benchmark.BENCHMARK_QUARANTINE_MARKER).is_file()
            assert not (lock_path / "pid").exists()
            assert not (lock_path / "lease").exists()
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
    assert (
        "scripts/firecrawl-ops/cre_collector/cre_capacity_multisource_v1.py"
        in manifest["files"]
    )
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
    assert failed["qualified_fresh_unique_rows"] == 127
    assert failed["normalized_structural_matches"] == 127
    assert failed["fidelity_failures"] == 1
    assert failed["comparison_state"] == "quality_failed"


def test_worker_uses_executed_brochure_url_classifier_without_double_counting() -> None:
    source = benchmark._worker_source(Path(__file__).resolve().parents[4])

    assert "jllHasUsableBrochure" in source
    assert "brochures: jllHasUsableBrochure(normalized)" in source


def _benchmark_success_rows(
    sample: dict[str, object], generation: str
) -> list[dict[str, object]]:
    """Build a fully qualified exact-cohort worker result without a live scrape."""
    rows: list[dict[str, object]] = []
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
                    "freshnessProvenance": {
                        "cacheDisposition": "live",
                        "generationId": generation,
                    },
                },
                "native": json.loads(json.dumps(row["historic"]["native"])),
                "fidelity": json.loads(json.dumps(row["historic"]["fidelity"])),
            }
        )
    return rows


def _attrition_observation(
    row: dict[str, object],
    generation: str,
    *,
    http_status: int | None = 404,
    next_data_valid: bool = True,
    explicit_not_found: bool = True,
    no_property: bool = True,
    challenge: bool = False,
) -> dict[str, object]:
    return {
        "cache_readable": True,
        "cache_url": row["url"],
        "cache_sha256": "a" * 64,
        "raw_html_sha256": "b" * 64,
        "cached_at": "2026-09-13T01:00:00Z",
        "detail_observed_at": "2026-09-13T01:00:00Z",
        "generation_id": generation,
        "http_status": http_status,
        "next_data_valid": next_data_valid,
        "explicit_not_found": explicit_not_found,
        "no_property": no_property,
        "provider_challenge": challenge,
    }


def _write_worker_result(
    replicate: Path,
    contract: dict[str, object],
    generation: str,
    rows: list[dict[str, object]],
    *,
    performance_concurrency: int = 10,
) -> None:
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
        json.dumps(_performance(performance_concurrency)), encoding="utf-8"
    )


def test_summarize_counts_confirmed_jll_404_attrition_without_failing_cohort(
    tmp_path: Path,
) -> None:
    sample = _sample(tmp_path)
    contract = benchmark._worker_contract(128, 10)
    sample_sha256 = hashlib.sha256(benchmark._canonical(sample)).hexdigest()
    replicate = tmp_path / "replicate"
    replicate.mkdir()
    generation = "2026-09-13T010000Z-abcdefabcdef"
    rows = _benchmark_success_rows(sample, generation)
    rows[0] = {
        "sample_index": rows[0]["sample_index"],
        "sample_id": rows[0]["sample_id"],
        "latency_ms": 10,
        "transaction_type": rows[0]["transaction_type"],
        "normalized": {
            "id": sample["details"][0]["id"],
            "url": sample["details"][0]["url"],
            "transactionType": rows[0]["transaction_type"],
            "detailError": "missing property in __NEXT_DATA__",
        },
        "observation": _attrition_observation(sample["details"][0], generation),
    }
    _write_worker_result(replicate, contract, generation, rows)

    summary = benchmark.summarize_replicate(
        replicate,
        sample,
        60,
        sample_canonical_sha256=sample_sha256,
        worker_contract=contract,
    )

    assert summary["current_active_successes"] == 127
    assert summary["confirmed_attrition"] == 1
    assert summary["individually_qualified_rows"] == 127
    assert summary["parser_failures"] == 0
    assert summary["transport_failures"] == 0
    assert summary["fidelity_failures"] == 0
    assert summary["predeclared_eligible_denominator"] == 128
    assert summary["predeclared_cohort_denominator"] == 128
    assert summary["eligible_rows"] == 127
    assert summary["comparison_state"] == "measured"
    assert summary["record_evidence"][0]["classification"] == "confirmed_attrition"


@pytest.mark.parametrize(
    ("observation", "expected_field"),
    [
        (
            {
                "http_status": 404,
                "next_data_valid": False,
                "explicit_not_found": True,
                "no_property": True,
            },
            "parser_failures",
        ),
        (
            {
                "http_status": 200,
                "next_data_valid": True,
                "explicit_not_found": True,
                "no_property": True,
            },
            "parser_failures",
        ),
        (
            {
                "http_status": 429,
                "next_data_valid": True,
                "explicit_not_found": True,
                "no_property": True,
            },
            "transport_failures",
        ),
        (
            {
                "http_status": None,
                "next_data_valid": True,
                "explicit_not_found": True,
                "no_property": True,
            },
            "transport_failures",
        ),
    ],
)
def test_summarize_never_misclassifies_ambiguous_detail_as_attrition(
    tmp_path: Path,
    observation: dict[str, object],
    expected_field: str,
) -> None:
    sample = _sample(tmp_path)
    contract = benchmark._worker_contract(128, 10)
    sample_sha256 = hashlib.sha256(benchmark._canonical(sample)).hexdigest()
    replicate = tmp_path / "replicate"
    replicate.mkdir()
    generation = "2026-09-13T010000Z-abcdefabcdef"
    rows = _benchmark_success_rows(sample, generation)
    rows[0] = {
        "sample_index": rows[0]["sample_index"],
        "sample_id": rows[0]["sample_id"],
        "latency_ms": 10,
        "transaction_type": rows[0]["transaction_type"],
        "normalized": {
            "id": sample["details"][0]["id"],
            "url": sample["details"][0]["url"],
            "transactionType": rows[0]["transaction_type"],
            "detailError": "missing property in __NEXT_DATA__",
        },
        "observation": _attrition_observation(
            sample["details"][0], generation, **observation
        ),
    }
    _write_worker_result(replicate, contract, generation, rows)

    summary = benchmark.summarize_replicate(
        replicate,
        sample,
        60,
        sample_canonical_sha256=sample_sha256,
        worker_contract=contract,
    )

    assert summary["confirmed_attrition"] == 0
    assert summary[expected_field] == 1
    assert summary["eligible_rows"] == 127
    assert summary["comparison_state"] == "quality_failed"
