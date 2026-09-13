"""Pure, no-network contracts for cre_series_status.py."""

from __future__ import annotations

import io
import json
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
    manifest["series_id"] = "safe\x1b]2;spoofed\x07"

    rendered = status.render(
        status.build_snapshot(manifest, manifest_path), color=False
    )

    assert "\x1b" not in rendered
    assert "\x07" not in rendered
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
