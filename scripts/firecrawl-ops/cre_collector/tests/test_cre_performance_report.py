"""Pure synthetic contracts for the body-free optimization report."""

from __future__ import annotations

import json
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
    assert "0.0s" not in report.render(result)


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
