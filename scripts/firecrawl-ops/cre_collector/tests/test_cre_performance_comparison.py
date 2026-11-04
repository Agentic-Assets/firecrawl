"""Offline comparisons preserve scope, missing data and overlapping wall time."""

from copy import deepcopy

import pytest

from cre_performance_compare import compare_reports, source_stage_timing
from cre_performance import _known_source, _phase


def saved_report(*, wall=100, count=20, readback=True):
    return {
        "kind": "cre_performance_report",
        "generation_id": "2026-10-15T120000Z",
        "collector_git_sha": "a" * 40,
        "run_elapsed_seconds": wall,
        "run_status": "complete",
        "run_scope": {"transactions": ["sale", "lease"], "pipeline": True},
        "sources": [
            {
                "source": "hanley",
                "status": "ingested",
                "staged_unique": count,
                "database_readback_ok": readback,
                "quality_no_regression": True,
                "collection_seconds": wall / 2,
                "source_elapsed_seconds": wall,
                "timed_command_seconds": {"collection": wall / 2, "ingest": 5},
                "settings": {"source_workers": 2},
                "rejected_by_ingest": 0,
                "acquisition_quality_signals": {"detail_errors": 0},
            }
        ],
    }


def test_comparison_uses_actual_run_wall_not_sum_of_overlapping_commands():
    baseline = saved_report(wall=100)
    current = saved_report(wall=50)
    current["sources"][0]["timed_command_seconds"]["readback"] = 200
    result = compare_reports(current, baseline)
    assert result["elapsed"]["current_seconds"] == 50
    assert result["elapsed"]["reduction_percent"] == 50
    assert result["sources"][0]["staged_per_minute"]["current"] == 24
    assert result["causal_improvement_established"] is False


def test_verified_current_inventory_rate_is_compared_as_a_separate_readback_metric():
    baseline, current = saved_report(wall=100), saved_report(wall=50)
    for report, count, rate in ((baseline, 20, 12), (current, 20, 24)):
        report["sources"][0]["verified_current_inventory_unique"] = count
        report["sources"][0]["verified_current_inventory_per_minute"] = rate
    result = compare_reports(current, baseline)
    source = result["sources"][0]
    assert source["verified_current_inventory_unique"] == {
        "current": 20,
        "baseline": 20,
        "change": 0,
    }
    assert source["verified_current_inventory_per_minute"] == {
        "current": 24,
        "baseline": 12,
        "change": 12,
    }
    assert result["causal_improvement_established"] is False


@pytest.mark.parametrize(
    "change", ["source", "transaction", "readback", "unknown_wall"]
)
def test_unmatched_or_unqualified_work_never_gets_run_speedup(change):
    baseline = saved_report()
    current = saved_report(wall=50)
    if change == "source":
        current["sources"][0]["source"] = "srs"
    elif change == "transaction":
        current["run_scope"]["transactions"] = ["sale"]
    elif change == "readback":
        current["sources"][0]["database_readback_ok"] = False
    else:
        current["run_elapsed_seconds"] = None
    result = compare_reports(current, baseline)
    assert result["elapsed"]["reduction_percent"] is None
    assert result["causal_improvement_established"] is False


def test_changed_code_settings_and_workload_are_explicit_context():
    baseline = saved_report()
    current = saved_report(wall=50, count=40)
    current["collector_git_sha"] = "b" * 40
    current["sources"][0]["settings"]["source_workers"] = 4
    result = compare_reports(current, baseline)
    assert "collector_code_changed" in result["context_differences"]
    assert "hanley:settings_changed" in result["context_differences"]
    assert "hanley:staged_count_changed" in result["context_differences"]
    assert result["elapsed"]["reduction_percent"] is None
    assert result["sources"][0]["staged_per_minute"]["current"] == 48


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf"), None])
def test_invalid_or_zero_duration_is_unknown_not_infinite_speed(value):
    current = saved_report(wall=50)
    current["run_elapsed_seconds"] = value
    result = compare_reports(current, saved_report())
    assert result["elapsed"]["reduction_percent"] is None


def test_stage_timing_separates_dispatch_wait_database_wait_and_end_to_end():
    timings = {
        "queued_at": "2026-10-15T12:00:00Z",
        "collection_started_at": "2026-10-15T12:00:02Z",
        "collection_finished_at": "2026-10-15T12:00:12Z",
        "validation_started_at": "2026-10-15T12:00:20Z",
        "ingested_at": "2026-10-15T12:00:25Z",
        "verified_at": "2026-10-15T12:00:40Z",
    }
    result = source_stage_timing(timings)
    assert result["dispatch_wait_seconds"] == 2
    assert result["awaiting_validation_seconds"] == 8
    assert result["collection_wall_seconds"] == 10
    assert result["source_elapsed_seconds"] == 38
    assert result["queued_to_verified_seconds"] == 40
    assert result["post_ingest_to_verified_seconds"] == 15
    bad = deepcopy(timings)
    bad["validation_started_at"] = "2026-10-15T11:59:00Z"
    assert source_stage_timing(bad)["awaiting_validation_seconds"] is None


def test_comparison_projection_never_copies_free_text_or_credentials():
    current = saved_report()
    current["sources"][0]["secret"] = "https://user:password@example.invalid"
    result = compare_reports(current, saved_report())
    assert "password" not in str(result)
    assert "secret" not in str(result)


def test_quality_regression_or_unknown_cannot_get_elapsed_reduction_claim():
    for value in (False, None):
        current = saved_report(wall=50)
        current["sources"][0]["quality_no_regression"] = value
        result = compare_reports(current, saved_report())
        assert result["elapsed"]["reduction_percent"] is None


def test_runtime_observation_timestamp_alone_is_not_a_changed_machine():
    current, baseline = saved_report(wall=50), saved_report()
    current["shared_runtime_configuration"] = {
        "state": "available",
        "observed_at": "2026-10-15T12:00:00Z",
        "hardware": {"logical_cpu_count": 12},
        "containers": [],
    }
    baseline["shared_runtime_configuration"] = deepcopy(
        current["shared_runtime_configuration"]
    )
    baseline["shared_runtime_configuration"]["observed_at"] = "2026-10-14T12:00:00Z"
    assert (
        "runtime_context_changed_or_unavailable"
        not in compare_reports(current, baseline)["context_differences"]
    )


@pytest.mark.parametrize(
    "log,argv,phase,source",
    [
        (
            "unit-0001-hanley-gate.log",
            ["python3", "cre_gate.py"],
            "source_gate",
            "hanley",
        ),
        ("unit-0001-validation.log", ["python3", "cre_validate.py"], "readback", None),
        (
            "srs-artifact-validation.log",
            ["python3", "cre_pipeline_artifact_worker.py"],
            "artifact_validation",
            "srs",
        ),
    ],
)
def test_pipeline_unit_commands_keep_stage_and_source_attribution(
    log, argv, phase, source
):
    assert _phase(argv, log) == phase
    assert _known_source(argv, log) == source


def test_query_comparison_shows_call_count_and_client_cost_not_run_wall():
    baseline, current = saved_report(), saved_report(wall=50)
    baseline["validation_query_timings"] = [
        {"queries": {"source_counts": {"elapsed_ms": 100}}},
        {"queries": {"source_counts": {"elapsed_ms": 300}}},
    ]
    current["validation_query_timings"] = [
        {"queries": {"source_counts": {"elapsed_ms": 50}}}
    ]
    row = compare_reports(current, baseline)["query_client_timings"][0]
    assert row["query"] == "source_counts"
    assert row["calls"] == {"current": 1.0, "baseline": 2.0, "change": -1.0}
    assert row["summed_client_ms"]["baseline"] == 400
    assert row["summed_client_ms"]["current"] == 50


def test_failed_outcome_with_old_success_fields_never_gets_reduction():
    current = saved_report(wall=50)
    current["run_status"] = "failed"
    current["sources"][0]["status"] = "failed_global"
    result = compare_reports(current, saved_report())
    assert "run_outcome_changed" in result["context_differences"]
    assert "hanley:outcome_changed" in result["context_differences"]
    assert result["elapsed"]["reduction_percent"] is None


def test_known_cache_retry_differences_are_exposed_as_comparison_context():
    current, baseline = saved_report(wall=50), saved_report()
    for report, hits, retries in ((baseline, 0, 0), (current, 100, 20)):
        report["sources"][0]["commands"] = [
            {
                "phase": "collection",
                "scrape": {
                    "jll_detail_cache": {"hits": hits, "misses": 10},
                    "requests": {
                        "http_helper": {
                            "retry_attempts": retries,
                            "backoff_ms": retries * 100,
                        }
                    },
                },
            }
        ]
    result = compare_reports(current, baseline)
    assert "hanley:cache_retry_context_changed" in result["context_differences"]
    assert result["sources"][0]["cache_retry"]["current"]["cache_hits"] == 100
    assert result["sources"][0]["cache_retry"]["baseline"]["http_retry_attempts"] == 0
