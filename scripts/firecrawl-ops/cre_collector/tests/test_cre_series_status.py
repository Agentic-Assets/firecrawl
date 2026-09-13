"""Pure, no-network contracts for cre_series_status.py."""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import cre_series_status as status
import pytest


def _manifest(series_dir: Path, *, updated: str = "2026-09-13T12:00:00+00:00") -> Path:
    series_dir.mkdir(parents=True)
    path = series_dir / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "series_id": series_dir.name,
                "status": "running",
                "started_at": "2026-09-13T11:00:00+00:00",
                "updated_at": updated,
                "finished_at": None,
                "collector_git_sha": "a" * 40,
                "config": {"sources": ["cbre", "jll", "svn"]},
                "sources": {
                    "cbre": {"state": "complete"},
                    "jll": {
                        "state": "running",
                        "attempts": [{"number": 2, "log": "logs/02-jll-attempt-2.log"}],
                    },
                    "svn": {"state": "pending"},
                },
                "error": None,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_resolve_series_path_uses_newest_manifest(tmp_path):
    older = _manifest(tmp_path / "2026-09-13T110000Z")
    newer = _manifest(tmp_path / "2026-09-13T120000Z")
    older.touch()
    newer.touch()

    assert status.resolve_series_path(None, tmp_path) == newer


def test_snapshot_and_render_include_parent_bound_progress(tmp_path):
    manifest_path = _manifest(tmp_path / "2026-09-13T120000Z")

    snapshot = status.build_snapshot(
        status.load_manifest(manifest_path),
        manifest_path,
        now=datetime(2026, 9, 13, 12, 5, tzinfo=timezone.utc),
    )
    rendered = status.render(snapshot, color=False)

    assert snapshot["complete"] == 1
    assert snapshot["handled"] == 1
    assert snapshot["percent_complete"] == pytest.approx(100 / 3)
    assert snapshot["current_source"] == "jll"
    assert snapshot["current_attempt"] == 2
    assert snapshot["attempt_log"].endswith("logs/02-jll-attempt-2.log")
    assert "1/3 complete (33.3%)" in rendered
    assert "Recorded source        jll" in rendered
    assert "\033[" not in rendered


def test_snapshot_separates_handled_from_success(tmp_path):
    manifest_path = _manifest(tmp_path / "2026-09-13T120000Z")
    manifest = status.load_manifest(manifest_path)
    manifest["sources"]["jll"] = {"state": "failed_source"}

    snapshot = status.build_snapshot(manifest, manifest_path)

    assert snapshot["complete"] == 1
    assert snapshot["handled"] == 2
    assert snapshot["failed"] == 1


@pytest.mark.parametrize(
    "stopped_state",
    ["failed_global", "interrupted", "resource_guard_interrupted"],
)
def test_resumable_stop_is_not_counted_as_handled(tmp_path, stopped_state):
    manifest_path = _manifest(tmp_path / "2026-09-13T120000Z")
    manifest = status.load_manifest(manifest_path)
    manifest["sources"]["jll"] = {"state": stopped_state}

    snapshot = status.build_snapshot(manifest, manifest_path)

    assert snapshot["handled"] == 1
    assert snapshot["failed"] == 1


def test_loader_rejects_child_or_future_manifest(tmp_path):
    manifest_path = _manifest(tmp_path / "2026-09-13T120000Z")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TypeError, match="unsupported checkpoint-series manifest"):
        status.load_manifest(manifest_path)


def test_loader_rejects_malformed_source_checkpoint(tmp_path):
    manifest_path = _manifest(tmp_path / "2026-09-13T120000Z")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["status"] = "complete"
    payload["config"]["sources"] = ["jll"]
    payload["sources"] = {"jll": None}
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TypeError, match="invalid source checkpoint"):
        status.load_manifest(manifest_path)


def test_render_does_not_echo_raw_error_text(tmp_path):
    manifest_path = _manifest(tmp_path / "2026-09-13T120000Z")
    manifest = status.load_manifest(manifest_path)
    manifest["status"] = "failed"
    manifest["error"] = "failed for https://user:secret@example.test/private"

    snapshot = status.build_snapshot(manifest, manifest_path)
    rendered = status.render(snapshot, color=False)

    assert snapshot["error_recorded"] is True
    assert "recorded in manifest" in rendered
    assert "secret" not in rendered


def test_render_sanitizes_terminal_controls(tmp_path):
    manifest_path = _manifest(tmp_path / "2026-09-13T120000Z")
    manifest = status.load_manifest(manifest_path)
    manifest["series_id"] = "safe\x1b]2;spoofed\x07\u202ehidden\u2066"

    rendered = status.render(
        status.build_snapshot(manifest, manifest_path), color=False
    )

    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert "\u202e" not in rendered
    assert "\u2066" not in rendered
    assert "safe?]2;spoofed?" in rendered


def test_dumb_terminal_watch_emits_no_ansi(tmp_path, monkeypatch):
    manifest_path = _manifest(tmp_path / "2026-09-13T120000Z")

    class TtyBuffer(io.StringIO):
        def isatty(self):
            return True

    output = TtyBuffer()
    monkeypatch.setattr(status.sys, "stdout", output)
    monkeypatch.setenv("TERM", "dumb")

    def stop_after_first_snapshot(_interval):
        raise KeyboardInterrupt

    monkeypatch.setattr(status.time, "sleep", stop_after_first_snapshot)

    assert status.main([str(manifest_path), "--watch", "0.5"]) == 0
    assert "\x1b[" not in output.getvalue()


def test_json_mode_rejects_watch():
    try:
        status.main(["--json", "--watch"])
    except SystemExit as exc:
        assert str(exc) == "--json cannot be combined with --watch"
    else:
        raise AssertionError("expected SystemExit")


@pytest.mark.parametrize("interval", ["nan", "inf", "-inf", "0", "0.49"])
def test_watch_rejects_nonfinite_or_busy_loop_interval(interval):
    with pytest.raises(SystemExit, match="finite and between 0.5 and 3600"):
        status.main([f"--watch={interval}"])


@pytest.mark.parametrize("interval", ["3601", "1e308"])
def test_watch_rejects_unbounded_sleep_before_reading_artifacts(interval):
    with pytest.raises(SystemExit, match="between 0.5 and 3600"):
        status.main([f"--watch={interval}"])


def _cooling_manifest(path):
    manifest = status.load_manifest(path)
    manifest["status"] = "cooling_down"
    manifest["sources"]["jll"]["state"] = "resource_guard_interrupted"
    manifest["config"]["resource_recovery"] = {"low_cpu_percent": 60}
    manifest["error"] = "private raw failure text"
    manifest["resource_recovery"] = {
        "state": "cooling_down",
        "cumulative_wait_seconds": 65,
        "active": {
            "source": "jll",
            "reason_code": "host_cpu_sustained",
            "phase": "collection",
            "waited_seconds": 65,
            "current_host_cpu_percent": 57.5,
            "low_cpu_seconds": 20,
            "required_low_cpu_seconds": 30,
            "remaining_cooldown_seconds": 535,
            "remaining_series_wait_seconds": 1735,
            "source_recoveries": 1,
            "max_source_recoveries": 3,
            "preserved_detail_count": 1224,
        },
    }
    return manifest


def test_cooling_down_reports_recovery_without_counting_a_failure(tmp_path):
    path = _manifest(tmp_path / "series")
    manifest = _cooling_manifest(path)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    snapshot = status.build_snapshot(status.load_manifest(path), path)
    rendered = status.render(snapshot, color=False)

    assert snapshot["failed"] == 0
    assert snapshot["handled"] == 1
    assert snapshot["cooling_down"] == 1
    assert snapshot["current_source"] == "jll"
    assert "Waiting for host CPU to cool" in rendered
    assert "57.5%  (resume below 60.0%)" in rendered
    assert "20s / 30s" in rendered
    assert "1 / 3" in rendered
    assert "1,224  (original observation times)" in rendered
    assert "Series recovery budget 28m 55s remaining" in rendered
    assert "Latest error" not in rendered
    assert "private raw failure text" not in rendered


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "unknown-source"),
        ("source", []),
        ("state", "running"),
        ("active", None),
    ],
)
def test_loader_rejects_inconsistent_cooldown(tmp_path, field, value):
    path = _manifest(tmp_path / "series")
    manifest = _cooling_manifest(path)
    if field == "state":
        manifest["sources"]["jll"]["state"] = value
    elif field == "active":
        manifest["resource_recovery"][field] = value
    else:
        manifest["resource_recovery"]["active"][field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(TypeError, match="inconsistent cooling-down"):
        status.load_manifest(path)


@pytest.mark.parametrize(
    "invalid", [True, "10", float("nan"), float("inf"), -1, 10**400]
)
def test_recovery_omits_invalid_metrics_and_untrusted_text(tmp_path, invalid):
    path = _manifest(tmp_path / "series")
    manifest = _cooling_manifest(path)
    active = manifest["resource_recovery"]["active"]
    active.update(
        {
            "current_host_cpu_percent": invalid,
            "reason_code": "secret-token",
            "phase": "secret-phase",
            "active_operation": "https://user:secret@example.test",
            "message": "private body",
            "unknown": "secret-value",
        }
    )
    snapshot = status.build_snapshot(manifest, path)
    encoded = json.dumps(snapshot)
    assert snapshot["recovery"]["current_host_cpu_percent"] is None
    assert snapshot["recovery"]["reason"] is None
    assert snapshot["recovery"]["phase"] is None
    assert "secret" not in encoded
    assert "private body" not in encoded
    assert "Host CPU               " not in status.render(snapshot, color=False)


def test_other_source_failure_remains_visible_during_cooldown(tmp_path):
    path = _manifest(tmp_path / "series")
    manifest = _cooling_manifest(path)
    manifest["sources"]["svn"]["state"] = "failed_source"
    snapshot = status.build_snapshot(manifest, path)
    assert snapshot["failed"] == 1
    assert snapshot["handled"] == 2


def test_cooldown_color_and_unknown_optional_recovery_fields(tmp_path):
    path = _manifest(tmp_path / "series")
    manifest = _cooling_manifest(path)
    manifest["resource_recovery"]["active"] = {"source": "jll"}
    snapshot = status.build_snapshot(manifest, path)
    assert "\033[33mcooling_down" in status.render(snapshot, color=True)
    assert snapshot["recovery"]["reason"] is None


def _child_fixture(path, manifest, *, source="jll"):
    run_dir = path.parent / "runs" / "child-generation"
    (run_dir / "logs").mkdir(parents=True)
    child = {
        "schema_version": 2,
        "collector_git_sha": manifest["collector_git_sha"],
        "run_id": run_dir.name,
        "sources": {
            source: {
                "state": "collecting",
                "attempts": [{"number": 1, "log": "logs/collect.log"}],
            }
        },
    }
    manifest["sources"][source]["checkpoint_run"] = f"runs/{run_dir.name}"
    (run_dir / "manifest.json").write_text(json.dumps(child), encoding="utf-8")
    (run_dir / "logs" / "collect.log").write_text(
        "private provider message\n  jll/sale: detail enriched 100/1825\n"
        "  jll/lease: detail enriched 200/300\nprivate token\n",
        encoding="utf-8",
    )
    return run_dir, child


def test_child_progress_shows_latest_counter_without_exposing_log(tmp_path):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    _child_fixture(path, manifest)
    snapshot = status.build_snapshot(manifest, path)
    rendered = status.render(snapshot, color=False)
    assert snapshot["child_progress"] == {
        "state": "collecting",
        "detail_pass": {
            "transaction": "lease",
            "completed": 200,
            "total": 300,
            "attempt": 1,
        },
    }
    assert "lease: 200/300 in this pass" in rendered
    assert "Includes cache/error outcomes; not listing completion" in rendered
    assert "private provider message" not in rendered
    assert "private token" not in json.dumps(snapshot)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 3),
        ("collector_git_sha", "wrong"),
        ("run_id", "wrong"),
        ("sources", {}),
        ("sources", {"jll": None}),
    ],
)
def test_child_progress_rejects_wrong_generation_and_shape(tmp_path, field, value):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    run_dir, child = _child_fixture(path, manifest)
    child[field] = value
    (run_dir / "manifest.json").write_text(json.dumps(child), encoding="utf-8")
    assert status.child_progress(manifest, path, "jll") is None


@pytest.mark.parametrize(
    "run", ["../escape", "/tmp/escape", "not-runs/child", [], None]
)
def test_child_progress_rejects_paths_outside_series_runs(tmp_path, run):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    manifest["sources"]["jll"]["checkpoint_run"] = run
    assert status.child_progress(manifest, path, "jll") is None


def test_child_progress_rejects_symlinked_manifest(tmp_path):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    run_dir, _ = _child_fixture(path, manifest)
    external = tmp_path / "external.json"
    (run_dir / "manifest.json").rename(external)
    (run_dir / "manifest.json").symlink_to(external)
    assert status.child_progress(manifest, path, "jll") is None


@pytest.mark.parametrize("payload", [b"{", b"x" * (4 * 1024 * 1024 + 1)])
def test_child_progress_ignores_partial_or_oversized_manifest(tmp_path, payload):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    run_dir, _ = _child_fixture(path, manifest)
    (run_dir / "manifest.json").write_bytes(payload)
    assert status.child_progress(manifest, path, "jll") is None


def test_child_progress_bounds_log_tail_and_rejects_invalid_counters(tmp_path):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    run_dir, _ = _child_fixture(path, manifest)
    (run_dir / "logs" / "collect.log").write_text(
        "  jll/sale: detail enriched 100/200\n"
        + "x" * 70_000
        + "\n  jll/sale: detail enriched 300/200\n"
        + "  jll/sale: detail enriched 0/0\n",
        encoding="utf-8",
    )
    assert status.child_progress(manifest, path, "jll") == {
        "state": "collecting",
        "detail_pass": None,
    }


def test_child_progress_is_optional_for_legacy_series_and_other_sources(tmp_path):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    assert status.child_progress(manifest, path, "jll") is None
    _child_fixture(path, manifest, source="cbre")
    assert status.child_progress(manifest, path, "cbre") == {
        "state": "collecting",
        "detail_pass": None,
    }


@pytest.mark.parametrize(
    "invalid", ["runs/child\x00escape", "../escape", "/tmp/outside"]
)
def test_malformed_run_and_outer_log_do_not_crash_or_escape(tmp_path, invalid):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    manifest["sources"]["jll"]["checkpoint_run"] = invalid
    manifest["sources"]["jll"]["attempts"][-1]["log"] = invalid
    path.write_text(json.dumps(manifest), encoding="utf-8")
    snapshot = status.build_snapshot(manifest, path)
    assert snapshot["child_progress"] is None
    assert snapshot["attempt_log"] is None
    assert status.main([str(path)]) == 0


def test_cooldown_requires_consistent_nested_recovery_state(tmp_path):
    path = _manifest(tmp_path / "series")
    manifest = _cooling_manifest(path)
    manifest["resource_recovery"]["state"] = "failed"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(TypeError, match="inconsistent cooling-down"):
        status.load_manifest(path)
    snapshot = status.build_snapshot(manifest, path)
    assert snapshot["cooling_down"] == 0
    assert snapshot["failed"] == 1
    assert "Waiting for host CPU" not in status.render(snapshot, color=False)


def test_last_detail_counter_survives_empty_newer_attempt(tmp_path):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    run_dir, child = _child_fixture(path, manifest)
    (run_dir / "logs" / "new.log").write_text("started only\n", encoding="utf-8")
    child["sources"]["jll"]["attempts"].append({"number": 2, "log": "logs/new.log"})
    (run_dir / "manifest.json").write_text(json.dumps(child), encoding="utf-8")
    snapshot = status.build_snapshot(manifest, path)
    assert snapshot["child_progress"]["detail_pass"]["completed"] == 200
    assert snapshot["child_progress"]["detail_pass"]["attempt"] == 1
    assert "Detail attempt         1  (last recorded counter)" in status.render(
        snapshot, color=False
    )


def test_progress_history_is_bounded_and_skips_missing_logs(tmp_path):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    run_dir, child = _child_fixture(path, manifest)
    child["sources"]["jll"]["attempts"].extend(
        {"number": index, "log": f"logs/missing-{index}.log"} for index in range(2, 10)
    )
    (run_dir / "manifest.json").write_text(json.dumps(child), encoding="utf-8")
    assert status.child_progress(manifest, path, "jll")["detail_pass"] is None


def test_path_helper_fails_closed_on_symlink_cycle(tmp_path):
    (tmp_path / "loop-a").symlink_to(tmp_path / "loop-b")
    (tmp_path / "loop-b").symlink_to(tmp_path / "loop-a")
    assert status._contained_path(tmp_path, "loop-a") is None


def test_invalid_utf8_manifest_reports_read_failure_without_traceback(tmp_path, capsys):
    path = _manifest(tmp_path / "series")
    path.write_bytes(b"\xff\xfeinvalid")
    assert status.main([str(path)]) == 2
    assert "cannot render series status" in capsys.readouterr().err


@pytest.mark.parametrize(
    "phase",
    [
        "collect_infrastructure_failed",
        "collect_failed",
        "artifact_rejected",
        "gate_failed",
        "dry_run_failed",
        "gate_blocked",
        "baseline_seed_required",
    ],
)
def test_valid_child_failure_phase_remains_visible(tmp_path, phase):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    run_dir, child = _child_fixture(path, manifest)
    child["sources"]["jll"]["state"] = phase
    (run_dir / "manifest.json").write_text(json.dumps(child), encoding="utf-8")
    assert status.child_progress(manifest, path, "jll")["state"] == phase


def test_regular_reader_rejects_fifo_without_blocking(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(OSError, match="not a regular file"):
        status._read_regular_file(fifo, limit=10)


def test_regular_reader_rejects_final_symlink_swap(tmp_path):
    target = tmp_path / "target"
    target.write_text("private data", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        status._read_regular_file(link, limit=100)


def test_fifo_child_log_cannot_hang_dashboard(tmp_path):
    path = _manifest(tmp_path / "series")
    manifest = status.load_manifest(path)
    run_dir, _ = _child_fixture(path, manifest)
    log = run_dir / "logs" / "collect.log"
    log.unlink()
    os.mkfifo(log)
    assert status.child_progress(manifest, path, "jll")["detail_pass"] is None
