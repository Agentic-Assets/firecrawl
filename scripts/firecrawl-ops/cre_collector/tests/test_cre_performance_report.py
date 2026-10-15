"""Pure synthetic contracts for the body-free optimization report."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import cre_performance_report as report
import pytest

RUN = "2026-09-13T120000Z-0123456789ab"
SERIES = "2026-09-13T115959Z"
SHA = "a" * 40
NOW = datetime(2026, 9, 13, 12, 1, tzinfo=timezone.utc)
COMMAND = "b" * 32
LOG = "jll-collect-attempt-1.log"
METRICS = f"jll-collect-attempt-1.{COMMAND}.scrape-performance.json"


def pipeline_query_evidence(tmp_path, count):
    run = tmp_path / RUN
    run.mkdir()
    manifest = {
        "config": {"pipeline": True},
        "started_at": "2026-09-13T12:00:00Z",
        "finished_at": "2026-09-13T12:01:00Z",
        "pipeline": {"units": []},
    }
    raw = json.dumps(
        {
            "generated_at": "2026-09-13T12:00:30Z",
            "validation_profile": "full",
            "query_timings": {
                "source_counts": {"status": "available", "elapsed_ms": 5}
            },
        }
    ).encode()
    for number in range(1, count + 1):
        name = f"unit-{number:04d}"
        path = run / "pipeline" / name / "validation.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(raw)
        manifest["pipeline"]["units"].append(
            {
                "unit_id": name,
                "validation_path": str(path.relative_to(run)),
                "validation_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return run, manifest, len(raw)


def test_retry_units_beyond_source_count_retain_query_metrics(tmp_path):
    run, manifest, _ = pipeline_query_evidence(tmp_path, 52)
    # Rejected preparation attempts also remain in the real unit history.
    manifest["pipeline"]["units"].insert(0, {"unit_id": "unit-0000", "state": "failed"})
    frames = report._validation_timings(run, manifest)
    assert len(frames) == 52
    assert (
        sum(frame["queries"]["source_counts"]["elapsed_ms"] for frame in frames) == 260
    )


def test_query_unit_budget_preserves_known_frames_and_exposes_gap(
    tmp_path, monkeypatch
):
    run, manifest, _ = pipeline_query_evidence(tmp_path, 3)
    monkeypatch.setattr(report, "MAX_VALIDATION_UNITS", 2)
    frames = report._validation_timings(run, manifest)
    assert [frame["stage"] for frame in frames if frame["queries"]] == [
        "unit-0001",
        "unit-0002",
    ]
    assert frames[0]["unavailable_code"] == "unit_timing_budget_exhausted"


def test_query_byte_budget_preserves_known_frames_and_exposes_gap(
    tmp_path, monkeypatch
):
    run, manifest, size = pipeline_query_evidence(tmp_path, 3)
    monkeypatch.setattr(report, "MAX_TOTAL_VALIDATION_BYTES", size)
    frames = report._validation_timings(run, manifest)
    assert [frame["stage"] for frame in frames if frame["queries"]] == ["unit-0001"]
    assert frames[-1]["unavailable_code"] == "query_timing_byte_budget_exhausted"


def checkpoint(tmp_path: Path) -> Path:
    run = tmp_path / RUN
    (run / "logs").mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "run_id": RUN,
                "collector_git_sha": SHA,
                "status": "supported_scope_complete",
                "started_at": "2026-09-13T12:00:00Z",
                "finished_at": "2026-09-13T12:00:40Z",
                "config": {
                    "sources": ["jll"],
                    "concurrency": 2,
                    "source_workers": 1,
                    "page_cap": 400,
                    "host_cpu_guard": {
                        "max_host_cpu_percent": 75,
                        "sustain_seconds": 10,
                        "sample_seconds": 2,
                    },
                },
                "sources": {
                    "jll": {
                        "artifact": {
                            "staged_unique": 9,
                            "rejected_by_ingest": 1,
                            "detail_errors": 1,
                            "detail_unavailable": 0,
                        },
                        "readback": {"ok": True},
                        "attempts": [
                            {
                                "freshness_overrides": {
                                    "JLL_DETAIL_CONCURRENCY": "4",
                                    "JLL_DETAIL_CACHE_DIR": "/private/do-not-publish",
                                    "OPENAI_API_KEY": "do-not-publish",
                                }
                            }
                        ],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return run


def test_pipeline_report_reads_shared_child_journals_once(tmp_path, monkeypatch):
    run = checkpoint(tmp_path / "child")
    data = json.loads((run / "manifest.json").read_text())
    data["config"].update(
        pipeline=True, sources=["jll", "srs"], transactions=["sale", "lease"]
    )
    data["sources"]["jll"]["state"] = "ingested"
    data["sources"]["srs"] = {
        "state": "ingested",
        "artifact": {"staged_unique": 20},
        "readback": {"ok": True},
    }
    data["pipeline"] = {
        "source_timings": {
            "jll": {
                "collection_started_at": "2026-09-13T12:00:00Z",
                "collection_finished_at": "2026-09-13T12:00:10Z",
                "validation_started_at": "2026-09-13T12:00:12Z",
                "verified_at": "2026-09-13T12:00:40Z",
            }
        }
    }
    (run / "manifest.json").write_text(json.dumps(data))
    journal(run)
    series = tmp_path / SERIES
    (series / "runs").mkdir(parents=True)
    moved = series / "runs" / RUN
    shutil.move(run, moved)
    (series / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "series_id": SERIES,
                "collector_git_sha": SHA,
                "config": data["config"],
                "sources": {
                    key: {"state": "complete", "checkpoint_run": f"runs/{RUN}"}
                    for key in ["jll", "srs"]
                },
            }
        )
    )
    reads = []
    original = report._commands

    def counted(path, **kwargs):
        reads.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(report, "_commands", counted)
    result = report.build_report(series, now=NOW)
    assert reads == [moved]
    assert len(result["shared_commands"]) == 1
    assert len(result["checkpoint_resources"]) == 1
    assert result["sources"][0]["source_elapsed_seconds"] == 40
    assert (
        result["sources"][0]["stage_wall_seconds"]["awaiting_validation_seconds"] == 2
    )
    assert result["sources"][1]["staged_unique"] == 20


def test_query_timing_projection_and_current_inventory_status_are_retained(tmp_path):
    run = checkpoint(tmp_path)
    data = json.loads((run / "manifest.json").read_text())
    data["status"] = "additive_scope_complete_current_inventory"
    (run / "manifest.json").write_text(json.dumps(data))
    (run / "validation.json").write_text(
        json.dumps(
            {
                "validation_profile": "full",
                "env_file": "private-password",
                "queries": {"secret": "provider-body"},
                "query_timings": {
                    "source_counts": {"status": "available", "elapsed_ms": 125.2},
                    "private-password": {"status": "available", "elapsed_ms": 1},
                },
            }
        )
    )
    result = report.build_report(run, now=NOW)
    assert result["sources"][0]["status"] == "additive_scope_complete_current_inventory"
    assert result["validation_query_timings"][0]["queries"] == {
        "source_counts": {"status": "available", "elapsed_ms": 125.2}
    }
    assert "private-password" not in json.dumps(result)
    assert "provider-body" not in json.dumps(result)


def test_cli_compares_saved_generations_without_network(tmp_path, capsys):
    before = checkpoint(tmp_path / "before")
    after = checkpoint(tmp_path / "after")
    assert report.main([str(after), "--baseline", str(before), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["comparison"]["baseline_generation_id"] == RUN
    assert result["comparison"]["causal_improvement_established"] is False


def test_total_journal_budget_keeps_prior_measurements_and_marks_gap(
    tmp_path, monkeypatch
):
    run = checkpoint(tmp_path)
    path = journal(run)
    (run / "logs" / "z-extra.performance.jsonl").write_bytes(path.read_bytes())
    monkeypatch.setattr(report, "MAX_TOTAL_JOURNAL_BYTES", path.stat().st_size)
    commands, warnings = report._commands(run, now=NOW)
    assert len(commands) == 1
    assert "journal_total_byte_limit_exceeded" in warnings


def test_pipeline_query_timings_include_first_before_and_refuse_changed_hash(tmp_path):
    import hashlib

    run = checkpoint(tmp_path)
    unit = run / "pipeline" / "unit-0001"
    unit.mkdir(parents=True)
    before = json.dumps(
        {
            "generated_at": "2026-09-13T12:00:05Z",
            "validation_profile": "checkpoint-preflight",
            "query_timings": {
                "source_counts": {"status": "available", "elapsed_ms": 111}
            },
        }
    ).encode()
    post = json.dumps(
        {
            "generated_at": "2026-09-13T12:00:30Z",
            "validation_profile": "full",
            "query_timings": {
                "source_counts": {"status": "available", "elapsed_ms": 222}
            },
        }
    ).encode()
    (unit / "before.json").write_bytes(before)
    (unit / "validation.json").write_bytes(post)
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["config"]["pipeline"] = True
    manifest["pipeline"] = {
        "units": [
            {
                "unit_id": "unit-0001",
                "before_path": "pipeline/unit-0001/before.json",
                "before_sha256": hashlib.sha256(before).hexdigest(),
                "validation_path": "pipeline/unit-0001/validation.json",
                "validation_sha256": hashlib.sha256(post).hexdigest(),
            }
        ]
    }
    result = report._validation_timings(run, manifest)
    assert (
        sum(
            row["queries"].get("source_counts", {}).get("elapsed_ms", 0) or 0
            for row in result
        )
        == 333
    )
    (unit / "validation.json").write_bytes(post.replace(b"222", b"999"))
    result = report._validation_timings(run, manifest)
    assert not any(
        row["queries"].get("source_counts", {}).get("elapsed_ms") == 999
        for row in result
    )
    assert any(
        row.get("unavailable_code") == "report_binding_invalid" for row in result
    )


def test_copied_reused_initial_preflight_is_one_query_not_two(tmp_path):
    import hashlib

    run = checkpoint(tmp_path)
    unit = run / "pipeline" / "unit-0001"
    unit.mkdir(parents=True)
    raw = json.dumps(
        {
            "generated_at": "2026-09-13T12:00:05Z",
            "query_timings": {
                "source_counts": {"status": "available", "elapsed_ms": 111}
            },
        }
    ).encode()
    (run / "pre-validation.json").write_bytes(raw)
    (unit / "before.json").write_bytes(raw)
    manifest = json.loads((run / "manifest.json").read_text())
    manifest["config"]["pipeline"] = True
    manifest["preflight"] = {"validation_path": "pre-validation.json"}
    manifest["pipeline"] = {
        "units": [
            {
                "unit_id": "unit-0001",
                "before_path": "pipeline/unit-0001/before.json",
                "before_sha256": hashlib.sha256(raw).hexdigest(),
                "before_reused": True,
                "before_reuse_kind": "initial_preflight",
                "before_source_path": "pre-validation.json",
            }
        ]
    }
    result = report._validation_timings(run, manifest)
    assert len(result) == 1
    assert result[0]["queries"]["source_counts"]["elapsed_ms"] == 111


def journal(run: Path, *, identity: str = COMMAND, finish: bool = True) -> Path:
    path = run / "logs" / Path(LOG).with_suffix(".performance.jsonl")
    base = {
        "schema_version": 1,
        "kind": "cre_command_performance",
        "run_id": RUN,
        "command_id": identity,
        "command_log": LOG,
        "metrics_file": METRICS,
        "phase": "collection",
    }
    records = [
        {
            **base,
            "event": "started",
            "observed_at": "2026-09-13T12:00:00Z",
            "outcome": "running",
            "returncode": None,
            "elapsed_seconds": 0,
        }
    ]
    if finish:
        records.append(
            {
                **base,
                "event": "finished",
                "observed_at": "2026-09-13T12:00:20Z",
                "outcome": "success",
                "returncode": 0,
                "elapsed_seconds": 20,
            }
        )
    with path.open("a", encoding="utf-8") as output:
        for record in records:
            output.write(json.dumps(record) + "\n")
    return path


def snapshot(run: Path, **changes: object) -> Path:
    value = {
        "schema_version": 1,
        "kind": "cre_scrape_performance",
        "run_id": RUN,
        "command_id": COMMAND,
        "started_at": "2026-09-13T12:00:00Z",
        "updated_at": "2026-09-13T12:00:20Z",
        "terminal": True,
        "degraded": False,
        "metrics": {
            "elapsed_ms": 20000,
            "requests": {
                "attempts_started": 12,
                "attempts_completed": 12,
                "succeeded": 10,
                "failed": 2,
                "active_locally_awaited": 0,
                "max_active_locally_awaited": 4,
                "approximate_p50_ms": 3000,
                "approximate_p95_ms": 5000,
                "retry": {
                    "http_helper": {"retry_attempts": 2, "backoff_ms": 7500},
                    "json_parse": {"retry_attempts": 1, "backoff_ms": 8000},
                },
                "timed_out_remote_settlement_unknown": 1,
                "secret": "https://user:private@example.invalid/path",
            },
            "cache": {"jll_detail": {"hits": 3, "misses": 7, "refresh_bypasses": 1}},
            "resources": {
                "node_rss_bytes": {"current": 200_000_000, "max_sampled": 250_000_000}
            },
        },
    }
    value.update(changes)
    path = run / "logs" / METRICS
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_report_separates_request_success_staged_and_database_proof(tmp_path):
    run = checkpoint(tmp_path)
    journal(run)
    snapshot(run)
    result = report.build_report(run, now=NOW)
    row = result["sources"][0]
    scrape = row["commands"][0]["scrape"]
    assert row["staged_unique"] == 9
    assert row["rejected_by_ingest"] == 1
    assert row["acquisition_quality_signals"]["detail_errors"] == 1
    assert row["acquisition_quality_signals"]["detail_unavailable"] == 0
    assert row["acquisition_quality_signals"]["inventory_only"] is None
    assert row["database_readback_ok"] is True
    assert row["source_elapsed_seconds"] == 40
    assert row["timed_command_seconds"] == {"collection": 20}
    assert scrape["successful_client_attempts_per_second"] == 0.5
    assert scrape["requests"]["http_helper"]["backoff_ms"] == 7500
    assert scrape["jll_detail_cache"]["hits"] == 3
    assert scrape["collector_process"]["max_sampled_rss_bytes"] == 250_000_000
    assert row["settings"]["recorded_source_knobs"] == {"JLL_DETAIL_CONCURRENCY": 4}
    assert "private" not in json.dumps(result)
    assert "do-not-publish" not in json.dumps(result)
    assert "not listing completion" in report.render(result)


def test_repeated_command_log_keeps_invocations_separate(tmp_path):
    run = checkpoint(tmp_path)
    journal(run)
    journal(run, identity="c" * 32)
    snapshot(run)
    row = report.build_report(run, now=NOW)["sources"][0]
    assert len(row["commands"]) == 2
    assert row["timed_command_seconds"] == {"collection": 40}
    assert row["commands"][0]["scrape"] is not None
    assert row["commands"][1]["scrape"] is None
    assert "scrape_snapshot_unavailable" in row["warnings"]


def test_abruptly_stopped_worker_remains_partial_not_success(tmp_path):
    run = checkpoint(tmp_path)
    journal(run, finish=False)
    snapshot(run, terminal=False)
    row = report.build_report(run, now=NOW)["sources"][0]
    command = row["commands"][0]
    assert command["measurement_state"] == "running_or_abruptly_stopped"
    assert command["scrape"]["state"] == "stale_or_stalled"
    assert row["timed_command_seconds"] == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "2026-09-12T120000Z"},
        {"command_id": "d" * 32},
        {"updated_at": "2026-09-14T12:00:00Z"},
        {"updated_at": "2026-09-12T12:00:00Z"},
        {"kind": "other"},
    ],
)
def test_wrong_generation_invocation_or_clock_is_not_attributed(tmp_path, changes):
    run = checkpoint(tmp_path)
    journal(run)
    snapshot(run, **changes)
    assert (
        report.build_report(run, now=NOW)["sources"][0]["commands"][0]["scrape"] is None
    )


@pytest.mark.parametrize("kind", ["symlink", "fifo", "oversize", "invalid_json"])
def test_unsafe_metric_files_are_bounded_unknowns(tmp_path, kind):
    run = checkpoint(tmp_path)
    journal(run)
    path = run / "logs" / METRICS
    if kind == "symlink":
        target = tmp_path / "private.txt"
        target.write_text("private-secret", encoding="utf-8")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversize":
        path.write_bytes(b"x" * (report.MAX_SNAPSHOT_BYTES + 1))
    else:
        path.write_text("{bad", encoding="utf-8")
    result = report.build_report(run, now=NOW)
    assert result["sources"][0]["commands"][0]["scrape"] is None
    assert "private-secret" not in json.dumps(result)


def test_unknown_metrics_are_not_zero_performance(tmp_path):
    run = checkpoint(tmp_path)
    row = report.build_report(run, now=NOW)["sources"][0]
    assert row["commands"] == []
    assert row["host_cpu"]["state"] == "unavailable"
    assert "command_timing_unavailable" in row["warnings"]
    assert "unknown" in report.render(report.build_report(run, now=NOW))


def test_cpu_summary_omits_incident_bodies_and_labels_bounded_window(tmp_path):
    run = checkpoint(tmp_path)
    path = run / "logs" / "host-cpu-guard.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(
                {
                    "state": "high" if value >= 75 else "ok",
                    "host_cpu_percent": value,
                    "reason": "private URL",
                }
            )
            for value in (20, 40, 80)
        )
        + "\n",
        encoding="utf-8",
    )
    cpu = report.build_report(run, now=NOW)["sources"][0]["host_cpu"]
    assert cpu["sample_count"] == 3
    assert cpu["max_percent"] == 80
    assert cpu["high_samples"] == 1
    assert cpu["high_samples_are_not_contiguous_duration"] is True
    assert "private" not in json.dumps(cpu)


def test_parent_binds_child_sha_and_exact_source(tmp_path):
    root = tmp_path / SERIES
    run = checkpoint(root / "runs")
    journal(run)
    manifest = {
        "schema_version": 1,
        "series_id": SERIES,
        "collector_git_sha": SHA,
        "sources": {"jll": {"checkpoint_run": f"runs/{RUN}"}, "cbre": {}},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = report.build_report(root, now=NOW)
    assert result["sources"][0]["run_id"] == RUN
    assert result["sources"][1]["warnings"] == ["bound_child_evidence_unavailable"]
    child = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    child["collector_git_sha"] = "e" * 40
    (run / "manifest.json").write_text(json.dumps(child), encoding="utf-8")
    assert report.build_report(root, now=NOW)["sources"][0]["commands"] == []


def test_report_never_follows_parent_child_escape(tmp_path):
    root = tmp_path / SERIES
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "series_id": SERIES,
                "collector_git_sha": SHA,
                "sources": {"jll": {"checkpoint_run": "../private"}},
            }
        ),
        encoding="utf-8",
    )
    assert report.build_report(root, now=NOW)["sources"][0]["commands"] == []


def test_cli_missing_evidence_is_read_only_and_redaction_safe(tmp_path, capsys):
    assert report.main([str(tmp_path / "private-token")]) == 2
    output = capsys.readouterr().out
    assert "private-token" not in output
    assert "no database was queried" in output


@pytest.mark.parametrize("field", ["phase", "event", "outcome", "schema_version"])
def test_malformed_journal_types_are_diagnostic_unknowns(tmp_path, field):
    run = checkpoint(tmp_path)
    path = journal(run)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        row[field] = [] if field != "schema_version" else True
    path.write_text("\n".join(json.dumps(row) for row in rows))
    row = report.build_report(run, now=NOW)["sources"][0]
    assert row["commands"] == []
    assert "journal_identity_or_schema_invalid" in row["warnings"]


def test_missing_readback_and_timing_are_unknown_not_false_or_zero(tmp_path):
    run = checkpoint(tmp_path)
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["sources"]["jll"]["readback"] = {}
    path.write_text(json.dumps(manifest))
    result = report.build_report(run, now=NOW)
    assert result["sources"][0]["database_readback_ok"] is None
    assert result["sources"][0]["collection_seconds"] is None
    assert " 0.0s" not in report.render(result)


def test_directory_enumeration_is_bounded_including_unrelated_files(
    tmp_path, monkeypatch
):
    run = checkpoint(tmp_path)
    monkeypatch.setattr(report, "MAX_LOG_DIRECTORY_ENTRIES", 3)
    for index in range(4):
        (run / "logs" / f"other-{index}.log").touch()
    row = report.build_report(run, now=NOW)["sources"][0]
    assert row["warnings"] == ["log_directory_entry_limit_exceeded"]


def test_deeply_nested_snapshot_does_not_crash_report(tmp_path):
    run = checkpoint(tmp_path)
    journal(run)
    (run / "logs" / METRICS).write_text("[" * 2000 + "0" + "]" * 2000)
    assert (
        report.build_report(run, now=NOW)["sources"][0]["commands"][0]["scrape"] is None
    )


def test_public_command_wrapper_preserves_cleanup_owner_and_saved_identity(
    tmp_path, monkeypatch
):
    import cre_checkpoint_refresh as refresh

    run = checkpoint(tmp_path)
    supplied = {"CRE_REFRESH_GENERATION": RUN, "PRIVATE_KEY": "not-in-logs"}
    observed = {}

    def runner(argv, log_path, *, env):
        observed.update(env)
        assert argv == ["npx", "tsx", "collect.ts", "--source", "jll"]
        return 7

    monkeypatch.setattr(refresh, "_run_logged_command", runner)
    assert (
        refresh.run_command(
            ["npx", "tsx", "collect.ts", "--source", "jll"],
            run / "logs" / LOG,
            env=supplied,
        )
        == 7
    )
    row = report.build_report(run)["sources"][0]["commands"][0]
    assert row["returncode"] == 7
    assert row["outcome"] == "failed"
    assert row["command_id"] == observed["CRE_PERFORMANCE_COMMAND_ID"]
    assert observed["PRIVATE_KEY"] == "not-in-logs"
    assert "CRE_PERFORMANCE_PATH" not in supplied
    assert "not-in-logs" not in json.dumps(row)


def test_runtime_projection_keeps_initial_configuration_without_private_values(
    tmp_path,
):
    run = checkpoint(tmp_path)
    value = {
        "schema_version": 1,
        "kind": "cre_runtime_performance",
        "run_id": RUN,
        "observed_at": "2026-09-13T12:00:00Z",
        "availability": "available",
        "hardware": {
            "logical_cpu_count": 18,
            "memory_bytes": None,
            "secret": "private",
        },
        "containers": [
            {
                "name": "firecrawl-api-1",
                "id": "c" * 64,
                "image": "sha256:" + "d" * 64,
                "cpu_limit": 1,
                "memory_limit_bytes": 8589934592,
                "memory_swap_limit_bytes": -1,
                "pids_limit": 384,
                "shm_size_bytes": 1024,
                "ports": ["3002/tcp->3102", "private"],
                "env": "private",
            }
        ],
    }
    (run / "runtime-performance.json").write_text(json.dumps(value))
    runtime = report.build_report(run, now=NOW)["sources"][0]["runtime_configuration"]
    assert runtime["hardware"] == {"logical_cpu_count": 18, "memory_bytes": None}
    assert runtime["containers"][0]["memory_swap_limit_bytes"] == -1
    assert runtime["containers"][0]["ports"] == ["3002/tcp->3102"]
    assert "private" not in json.dumps(runtime)
    assert "not_live_usage" in runtime["coverage"]
    value["run_id"] = SERIES
    (run / "runtime-performance.json").write_text(json.dumps(value))
    assert (
        report.build_report(run, now=NOW)["sources"][0]["runtime_configuration"][
            "state"
        ]
        == "invalid"
    )


def test_real_typescript_snapshot_is_consumed_by_python_report(tmp_path):
    collector = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if node is None or not (collector / "node_modules/tsx").is_dir():
        pytest.skip(
            "cross-language telemetry contract requires installed collector Node dependencies"
        )
    run = checkpoint(tmp_path)
    journal(run)
    script = """
        import { createPerformanceRecorder } from './lib/performance.ts';
        let clock = 0;
        const recorder = createPerformanceRecorder({
          path: process.argv[1], runId: process.argv[2], commandId: process.argv[3],
          monotonicMs: () => clock,
          nowIso: () => new Date(Date.UTC(2026,8,13,12) + clock).toISOString(),
          processResources: () => ({rssBytes: 200000000, cpuUserMicros: 50, cpuSystemMicros: 25})
        });
        const tokens = Array.from({length: 4}, () => recorder.recordClientAttemptStarted(true, {source:'jll',transaction:'sale'}));
        clock = 2000;
        for (const token of tokens) recorder.recordClientAttemptCompleted(token, 'succeeded');
        recorder.flush(true);
    """
    completed = subprocess.run(
        [
            node,
            "--import",
            "tsx",
            "--input-type=module",
            "-e",
            script,
            str(run / "logs" / METRICS),
            RUN,
            COMMAND,
        ],
        cwd=collector,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    scrape = report.build_report(run, now=NOW)["sources"][0]["commands"][0]["scrape"]
    assert scrape["requests"]["succeeded"] == 4
    assert scrape["requests"]["max_active_locally_awaited"] == 4
    assert scrape["requests"]["by_source_transaction"][0]["succeeded"] == 4
    assert scrape["requests"]["other_valid_statuses"] == 0
    assert scrape["successful_client_attempts_per_second"] == 2
    assert scrape["collector_process"]["rss_bytes"] == 200_000_000


def test_multi_source_coordinator_filters_commands_and_labels_shared_resources(
    tmp_path,
):
    run = checkpoint(tmp_path)
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["config"]["sources"] = ["jll", "cbre"]
    manifest["sources"]["cbre"] = {
        "state": "dry_run_failed",
        "artifact": {"staged_unique": 4, "flat_listings": 6, "rejected_by_ingest": 2},
    }
    manifest["sources"]["jll"]["state"] = "complete"
    write = journal(run)
    records = [json.loads(line) for line in write.read_text().splitlines()]
    for item in records:
        item["source"] = "jll"
    write.write_text("\n".join(json.dumps(item) for item in records))
    path.write_text(json.dumps(manifest))
    snapshot(run)
    result = report.build_report(run, now=NOW)
    jll, cbre = result["sources"]
    assert jll["timed_command_seconds"] == {"collection": 20}
    assert cbre["timed_command_seconds"] == {}
    assert cbre["commands"] == []
    assert cbre["source_elapsed_seconds"] is None
    assert cbre["host_cpu"]["scope"] == "shared_coordinator"
    assert cbre["resources"]["scope"] == "shared_coordinator"
    assert cbre["status"] == "dry_run_failed"
    rendered = report.render(result)
    assert "Rejected" in rendered and "Readback" in rendered
    assert "dry_run_failed" in rendered
    assert result["comparison"]["speedup_percent"] is None


def test_shared_coordinator_gate_is_reported_once(tmp_path):
    run = checkpoint(tmp_path)
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["config"]["sources"] = ["jll", "cbre"]
    manifest["sources"]["cbre"] = {}
    path.write_text(json.dumps(manifest))
    log = journal(run)
    records = [json.loads(line) for line in log.read_text().splitlines()]
    for item in records:
        item["phase"] = "aggregate_gate"
    log.write_text("\n".join(json.dumps(item) for item in records))
    result = report.build_report(run, now=NOW)
    assert len(result["shared_commands"]) == 1
    assert all(row["commands"] == [] for row in result["sources"])


def test_save_report_is_best_effort_and_writes_only_sanitized_projection(
    tmp_path, monkeypatch, capsys
):
    run = checkpoint(tmp_path)
    assert report.save_report(run) is True
    saved = json.loads((run / "performance-report.json").read_text())
    assert saved["collector_git_sha"] == SHA
    assert "do-not-publish" not in json.dumps(saved)
    assert (
        "No matched quality-valid baseline"
        in (run / "performance-report.md").read_text()
    )

    def broken(_path):
        raise RuntimeError("private-token")

    monkeypatch.setattr(report, "build_report", broken)
    assert report.save_report(run) is False
    assert "private-token" not in capsys.readouterr().err


def test_output_replaces_file_symlink_without_following_it(tmp_path):
    run = checkpoint(tmp_path)
    secret = tmp_path / "private.txt"
    secret.write_text("private-secret")
    (run / "performance-report.json").symlink_to(secret)
    report.write_report(report.build_report(run, now=NOW), run)
    assert secret.read_text() == "private-secret"
    assert not (run / "performance-report.json").is_symlink()


def test_series_includes_child_resource_sessions_without_assuming_parent_usage(
    tmp_path,
):
    root = tmp_path / SERIES
    run = checkpoint(root / "runs")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "series_id": SERIES,
                "collector_git_sha": SHA,
                "sources": {"jll": {"checkpoint_run": f"runs/{RUN}"}},
            }
        )
    )
    records = [
        {
            "schema_version": 1,
            "kind": "cre_resource_usage",
            "run_id": RUN,
            "session_id": "a" * 32,
            "observed_at": "2026-09-13T12:00:00Z",
            "elapsed_seconds": 0,
            "host": {"cpu_percent": 20},
        },
        {
            "schema_version": 1,
            "kind": "cre_resource_usage",
            "run_id": RUN,
            "session_id": "a" * 32,
            "observed_at": "2026-09-13T12:00:05Z",
            "elapsed_seconds": 5,
            "host": {"cpu_percent": 30},
        },
    ]
    (run / "resource-usage.jsonl").write_text(
        "\n".join(json.dumps(item) for item in records)
    )
    result = report.build_report(root, now=NOW)
    assert result["resources"]["state"] == "unavailable"
    assert (
        result["sources"][0]["resources"]["sessions"][0]["metrics"]["cpu_percent"][
            "mean"
        ]
        == 25
    )
    assert "Checkpoint resource evidence for jll" in report.render(result)
    assert "prior failed/recovery attempts are not aggregated" in report.render(result)


def test_custom_project_runtime_configuration_is_bound_to_project_and_service(tmp_path):
    run = checkpoint(tmp_path)
    value = {
        "schema_version": 1,
        "kind": "cre_runtime_performance",
        "run_id": RUN,
        "observed_at": "2026-09-13T12:00:00Z",
        "availability": "available",
        "project": "cre-trial",
        "containers": [
            {
                "name": "cre-trial-api-1",
                "project": "cre-trial",
                "service": "api",
                "running": True,
                "id": "c" * 64,
                "image": "sha256:" + "d" * 64,
                "cpu_limit": 2,
            }
        ],
    }
    path = run / "runtime-performance.json"
    path.write_text(json.dumps(value))
    runtime = report.build_report(run, now=NOW)["sources"][0]["runtime_configuration"]
    assert runtime["state"] == "available"
    assert runtime["containers"][0]["project"] == "cre-trial"
    value["containers"][0]["name"] = "canary-api"
    path.write_text(json.dumps(value))
    runtime = report.build_report(run, now=NOW)["sources"][0]["runtime_configuration"]
    assert runtime["state"] == "available"
    assert runtime["containers"][0]["name"] == "canary-api"
    value["containers"][0]["name"] = "private/secret"
    path.write_text(json.dumps(value))
    runtime = report.build_report(run, now=NOW)["sources"][0]["runtime_configuration"]
    assert runtime["state"] == "invalid"
    assert "private/secret" not in json.dumps(runtime)


def test_save_report_suppresses_writer_and_broken_stderr_failures(
    tmp_path, monkeypatch
):
    run = checkpoint(tmp_path)

    def broken_write(*_args):
        raise OSError("private-secret")

    class BrokenStream:
        def write(self, _value):
            raise RuntimeError("private-secret")

    monkeypatch.setattr(report, "write_report", broken_write)
    monkeypatch.setattr(report.sys, "stderr", BrokenStream())
    assert report.save_report(run) is False


def test_report_writer_creates_private_output_directory(tmp_path):
    import stat

    run = checkpoint(tmp_path)
    out = tmp_path / "reports"
    report.write_report(report.build_report(run, now=NOW), out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert (out / "performance-report.json").is_file()


def test_coordinator_overlap_counts_duration_and_ignores_nonterminal_commands(tmp_path):
    run = checkpoint(tmp_path)
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["config"]["sources"] = ["jll", "cbre", "svn"]
    manifest["sources"].update(cbre={}, svn={})
    path.write_text(json.dumps(manifest))
    for source, offset, terminal in (
        ("jll", 0, True),
        ("cbre", 10, True),
        ("svn", 15, False),
    ):
        log = f"{source}-collect.log"
        base = {
            "schema_version": 1,
            "kind": "cre_command_performance",
            "run_id": RUN,
            "command_id": {"jll": "a", "cbre": "b", "svn": "c"}[source] * 32,
            "command_log": log,
            "phase": "collection",
            "source": source,
        }
        records = [
            {
                **base,
                "event": "started",
                "outcome": "running",
                "observed_at": f"2026-09-13T12:00:{offset:02d}Z",
                "elapsed_seconds": 0,
            }
        ]
        if terminal:
            records.append(
                {
                    **base,
                    "event": "finished",
                    "outcome": "success",
                    "observed_at": f"2026-09-13T12:00:{offset + 20:02d}Z",
                    "elapsed_seconds": 20,
                }
            )
        (run / "logs" / Path(log).with_suffix(".performance.jsonl")).write_text(
            "\n".join(json.dumps(item) for item in records)
        )
    result = report.build_report(run, now=NOW)
    measured = result["collection_overlap"]
    assert measured["peak_overlapping_source_collections"] == 2
    assert measured["seconds_by_active_sources"] == {"1": 20, "2": 10}
    assert measured["summed_source_collection_seconds"] == 40
    assert "subprocess wall-time overlap" in report.render(result)
