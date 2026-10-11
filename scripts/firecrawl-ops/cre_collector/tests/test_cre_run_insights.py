"""Saved evidence contracts; no collector, network, Docker or database calls."""

from __future__ import annotations

import json
from pathlib import Path

import cre_run_insights as insights
import pytest


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def write_records(path: Path, values: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(value) for value in values) + "\n", encoding="utf-8"
    )


def resource_record(
    run: Path, clock: float, cpu: float, *, session: str = "a" * 32
) -> dict:
    return {
        "schema_version": 1,
        "kind": "cre_resource_usage",
        "run_id": run.name,
        "session_id": session,
        "observed_at": "2026-10-11T15:00:00Z",
        "elapsed_seconds": clock,
        "sample_duration_seconds": 0.1,
        "sample_seconds": 5,
        "host": {
            "cpu_percent": cpu,
            "memory_total_bytes": 128 * 1024**3,
            "memory_pressure": "normal",
            "swap": {"used_bytes": 1024},
            "vm": {"compressor_bytes": 2048},
        },
        "processes": {"rss_bytes": 4096, "cpu_percent_lifetime": 8},
        "runtime": {
            "availability": "available",
            "docker_vm_memory_total_bytes": 32 * 1024**3,
            "containers": [
                {
                    "role": "browser",
                    "id": "b" * 64,
                    "running": True,
                    "cpu_percent": cpu * 4,
                    "cpu_quota_cores": 4,
                    "memory_current_bytes": 1024,
                    "memory_limit_bytes": 4096,
                    "pids": 8,
                    "restart_count": 2,
                    "oom_killed": True,
                    "env": "private-secret",
                }
            ],
        },
        "context": {"source": "private-secret"},
    }


def test_weighting_percentiles_and_gap_coverage_use_elapsed_intervals():
    result = insights.sampled_stats([(0, 0), (1, 100), (10, 0), (100, 50)])
    assert result["mean"] == 37.5
    assert result["p50"] == 0
    assert result["p95"] == 100
    assert result["duration_weighted_estimate"] == 90
    assert result["covered_seconds"] == 10
    assert result["uncovered_gap_seconds"] == 90
    assert result["max_sample_gap_seconds"] == 90


def test_missing_invalid_and_reversed_samples_are_not_zero_measurements():
    result = insights.sampled_stats([(1, 10), (1, 99), (0, 20), (2, float("nan"))])
    assert result["sample_count"] == 1
    assert result["invalid_or_nonmonotonic_samples"] == 3
    assert result["duration_weighted_estimate"] is None
    assert insights.sampled_stats([])["mean"] is None


def test_resources_keep_resume_sessions_separate_and_normalize_verified_quota(tmp_path):
    records = [
        resource_record(tmp_path, 0, 10),
        resource_record(tmp_path, 5, 20),
        resource_record(tmp_path, 0, 80, session="c" * 32),
        resource_record(tmp_path, 5, 90, session="c" * 32),
    ]
    write_records(tmp_path / "resource-usage.jsonl", records)
    result = insights.resource_report(tmp_path)
    assert result["state"] == "observed"
    assert len(result["sessions"]) == 2
    assert [
        session["metrics"]["cpu_percent"]["mean"] for session in result["sessions"]
    ] == [15, 85]
    container = result["sessions"][0]["containers"][0]["metrics"]
    assert container["cpu_percent"]["sampled_max"] == 80
    assert container["cpu_percent_of_verified_quota"]["sampled_max"] == 20
    assert container["memory_percent_of_limit"]["mean"] == 25
    assert container["restart_count"]["sampled_max"] == 2
    assert result["sessions"][0]["containers"][0]["oom_killed_true_snapshots"] == 2
    assert "private-secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "mutation", ["wrong_run", "bad_session", "future", "wrong_schema"]
)
def test_resource_identity_mismatch_does_not_create_measurements(tmp_path, mutation):
    record = resource_record(tmp_path, 0, 10)
    if mutation == "wrong_run":
        record["run_id"] = "other"
    elif mutation == "bad_session":
        record["session_id"] = "private-secret"
    elif mutation == "future":
        record["observed_at"] = "2099-01-01T00:00:00Z"
    else:
        record["schema_version"] = True
    write_records(tmp_path / "resource-usage.jsonl", [record])
    result = insights.resource_report(tmp_path)
    assert result["state"] == "unavailable"
    assert result["sessions"] == []


def test_unverified_or_stopped_container_quota_remains_unknown(tmp_path):
    record = resource_record(tmp_path, 0, 10)
    record["runtime"]["containers"][0]["running"] = False
    write_records(tmp_path / "resource-usage.jsonl", [record])
    metrics = insights.resource_report(tmp_path)["sessions"][0]["containers"][0][
        "metrics"
    ]
    assert "cpu_percent_of_verified_quota" not in metrics


@pytest.mark.parametrize("oom", [None, "false", 0])
def test_missing_or_non_bool_oom_evidence_remains_unknown(tmp_path, oom):
    record = resource_record(tmp_path, 0, 10)
    record["runtime"]["containers"][0]["oom_killed"] = oom
    write_records(tmp_path / "resource-usage.jsonl", [record])
    container = insights.resource_report(tmp_path)["sessions"][0]["containers"][0]
    assert container["oom_killed_true_snapshots"] is None
    assert container["oom_killed_observed_bool_snapshots"] == 0


def test_unbound_runtime_metadata_is_a_gap_not_workload_measurement(tmp_path):
    record = resource_record(tmp_path, 0, 10)
    record["runtime"].update(
        availability="unavailable", unavailable_code="api_port_mismatch"
    )
    write_records(tmp_path / "resource-usage.jsonl", [record])
    session = insights.resource_report(tmp_path)["sessions"][0]
    assert session["metrics"]["cpu_percent"]["mean"] == 10
    assert session["containers"] == []
    assert session["runtime_availability_samples"] == {"unavailable": 1}
    assert session["runtime_gap_code_samples"] == {"api_port_mismatch": 1}
    assert "oom_killed" not in json.dumps(session)


@pytest.fixture
def trial(tmp_path):
    result = {
        "git_sha": "a" * 40,
        "elapsed_seconds": 15,
        "sources": {},
        "settled": True,
        "database_writes": 0,
    }
    for source in ("jll", "transwestern"):
        result["sources"][source] = {
            "rc": 0,
            "started_at": "2026-10-11T15:00:00Z",
            "finished_at": "2026-10-11T15:00:14Z",
        }
        write_json(
            tmp_path / f"{source}.json",
            {
                "runMeta": {
                    "startedAt": "2026-10-11T15:00:01Z",
                    "finishedAt": "2026-10-11T15:00:11Z",
                    "transactions": ["sale", "lease"],
                },
                "sources": [
                    {
                        "sourceKey": source,
                        "transaction": transaction,
                        "listingsCollected": 2,
                        "truncated": True,
                        "freshness": {"detailErrors": 0},
                    }
                    for transaction in ("sale", "lease")
                ],
                "listings": [{"url": "private-secret", "contacts": "private-secret"}]
                * 4,
            },
        )
    write_json(tmp_path / "result.json", result)
    write_json(
        tmp_path / "dry-run-results.json",
        {"jll": {"returncode": 0}, "transwestern": {"returncode": 1}},
    )
    (tmp_path / "jll-dry-run.log").write_text(
        "staged listings: 3; inventory-only index rows: 0\nprivate-secret"
    )
    write_records(
        tmp_path / "runtime-samples.jsonl",
        [
            {
                "at": f"2026-10-11T15:00:{seconds:02d}Z",
                "browser": {"activePages": pages},
                "active_source_processes": 2,
                "containers": [
                    {
                        "Name": "private-secret-playwright-service-1",
                        "CPUPerc": "200%",
                        "MemUsage": "1GiB / 16GiB",
                        "MemPerc": "10%",
                        "PIDs": "20",
                    }
                ],
            }
            for seconds, pages in ((0, 0), (5, 8), (10, 0))
        ],
    )
    write_records(
        tmp_path / "host-cpu-guard.jsonl",
        [{"observed_at": "2026-10-11T15:00:00Z", "host_cpu_percent": 50}],
    )
    return tmp_path


def test_trial_separates_quality_wall_overlap_and_unknowns_without_private_data(trial):
    result = insights.build_trial_report(trial)
    jll, tw = result["sources"]
    assert result["trial_wall_seconds"] == 15
    assert result["overlap"]["summed_source_collection_seconds"] == 20
    assert result["overlap"]["collection_window_seconds"] == 10
    assert result["overlap"]["peak_overlapping_source_collections"] == 2
    assert jll["staged_unique"] == 3 and jll["raw_emitted_rows"] == 4
    assert jll["collection_seconds"] == 10 and jll["observed_process_seconds"] == 14
    assert tw["collector_returncode"] == 0 and tw["status"] == "failed"
    assert tw["ingested_rows"] is None and tw["staged_unique"] is None
    assert result["resources"]["host_memory"] is None
    assert result["resources"]["request_latency"] is None
    assert (
        result["resources"]["metrics"]["browser_memory_current_bytes"]["mean"]
        == 1024**3
    )
    assert (
        result["resources"]["metrics"]["browser_memory_limit_bytes"]["mean"]
        == 16 * 1024**3
    )
    assert result["comparison"]["speedup_percent"] is None
    assert "private-secret" not in json.dumps(result)


def test_trial_collection_error_overrides_successful_process_and_dry_run(trial):
    path = trial / "jll.json"
    value = json.loads(path.read_text())
    value["sources"][1]["error"] = "private-secret"
    write_json(path, value)
    assert insights.build_trial_report(trial)["sources"][0]["status"] == "failed"


@pytest.mark.parametrize(
    "mutation", ["wrong_source", "outside_process", "missing_transaction"]
)
def test_trial_mismatched_artifact_is_unknown_and_excluded_from_overlap(
    trial, mutation
):
    path = trial / "jll.json"
    value = json.loads(path.read_text())
    if mutation == "wrong_source":
        value["sources"][0]["sourceKey"] = "cbre"
    elif mutation == "outside_process":
        value["runMeta"]["finishedAt"] = "2026-10-11T15:00:59Z"
    else:
        value["sources"] = value["sources"][:1]
    write_json(path, value)
    result = insights.build_trial_report(trial)
    assert result["sources"][0]["status"] == "unknown"
    assert result["sources"][0]["collection_seconds"] is None
    assert result["sources"][0]["raw_emitted_rows"] is None
    assert result["overlap"]["peak_overlapping_source_collections"] == 1


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversize", "invalid_json"])
def test_trial_evidence_is_bounded_and_never_follows_aliases(trial, kind):
    import os

    path = trial / "runtime-samples.jsonl"
    path.unlink()
    if kind == "symlink":
        secret = trial / "private.txt"
        secret.write_text("private-secret")
        path.symlink_to(secret)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversize":
        path.write_bytes(b"x" * (insights.MAX_EVIDENCE_BYTES + 1))
    else:
        path.write_text("{bad\n")
    result = insights.build_trial_report(trial)
    assert "browser_active_pages" not in result["resources"]["metrics"]
    assert "private-secret" not in json.dumps(result)


def test_trial_result_symlink_is_rejected(trial):
    path = trial / "result.json"
    target = trial / "result-copy.json"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(OSError):
        insights.build_trial_report(trial)
