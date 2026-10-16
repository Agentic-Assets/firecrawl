"""Guarded serial pure-compute isolation, without acquisition polling authority."""

import json
from datetime import datetime, timezone

import pytest
import cre_checkpoint_refresh as refresh
import cre_refresh_pipeline as pipeline
from test_cre_checkpoint_refresh import ATTEMPT, artifact

NOW = datetime(2026, 7, 29, 12, 2, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_guard_state():
    refresh._clear_cpu_guard_trip()
    yield
    refresh._clear_cpu_guard_trip()


def test_serial_scope_uses_real_worker_without_parent_decode_or_poll_hook(
    tmp_path, monkeypatch
):
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(artifact("transwestern")))
    expected = refresh.validate_source_artifact(path, "transwestern", ATTEMPT, now=NOW)
    real = refresh._load_json

    def refuse_parent_payload(target):
        assert target != path, "guard coordinator decoded source artifact"
        return real(target)

    monkeypatch.setattr(refresh, "_load_json", refuse_parent_payload)
    assert pipeline.poll_hook() is None
    with pipeline.artifact_compute_scope(tmp_path):
        assert pipeline.poll_hook() is None
        actual = refresh.validate_source_artifact(
            path, "transwestern", ATTEMPT, now=NOW
        )
        assert actual == expected
        assert refresh.sha256_file(path) == expected["sha256"]
    assert not pipeline.artifact_compute_enabled()


def test_scope_resets_after_error_and_unscoped_validation_stays_local(
    tmp_path, monkeypatch
):
    with pytest.raises(RuntimeError, match="synthetic"):
        with pipeline.artifact_compute_scope(tmp_path):
            raise RuntimeError("synthetic")
    assert not pipeline.artifact_compute_enabled()
    calls = []
    monkeypatch.setattr(
        refresh,
        "_validate_source_artifact_local",
        lambda *a, **kw: calls.append(kw) or {"local": True},
    )
    monkeypatch.setattr(
        refresh,
        "run_artifact_compute",
        lambda *a: pytest.fail("unscoped work must stay local"),
    )
    assert refresh.validate_source_artifact(
        tmp_path / "unused", "transwestern", ATTEMPT
    ) == {"local": True}
    assert len(calls) == 1


def test_serial_worker_error_and_hash_tamper_still_refuse(tmp_path):
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(artifact("transwestern")))
    digest = refresh.sha256_file(path)
    with pipeline.artifact_compute_scope(tmp_path):
        with pytest.raises(refresh.ArtifactValidationError):
            refresh.validate_source_artifact(path, "savills", ATTEMPT, now=NOW)
        with pytest.raises(refresh.ArtifactValidationError):
            pipeline.run_artifact_compute(
                tmp_path, "hash", {"path": str(path), "sha256": "a" * 64}
            )
    assert refresh.sha256_file(path) == digest


@pytest.mark.parametrize("unknown", [False, True])
def test_actual_serial_owner_scope_and_failure_fence(tmp_path, monkeypatch, unknown):
    from contextlib import nullcontext

    lock_path = tmp_path / ".cre.lock"
    monkeypatch.setattr(
        refresh,
        "database_target_fingerprint",
        lambda value: {"algorithm": "sha256", "value": "a" * 64},
    )
    monkeypatch.setattr(refresh, "git_identity", lambda: ("a" * 40, False))
    monkeypatch.setattr(refresh, "checkpoint_lock_dir", lambda value: lock_path)
    monkeypatch.setattr(refresh, "capture_runtime_configuration", lambda root: None)
    monkeypatch.setattr(
        refresh, "observe_resource_usage", lambda *a, **kw: nullcontext()
    )
    monkeypatch.setattr(refresh, "run_cpu_guard_preflight", lambda guard: 1.0)
    monkeypatch.setattr(refresh.HostCpuGuard, "start", lambda self: None)
    monkeypatch.setattr(refresh.HostCpuGuard, "stop", lambda self: None)
    monkeypatch.setattr(refresh.HostMemoryGuard, "start", lambda self: None)
    import cre_memory_guard as memory

    monkeypatch.setattr(memory, "read_memory_pressure", lambda **kw: 1)
    called = []

    def command(argv, log, **kwargs):
        assert lock_path.is_dir(), "scope must follow actual lease acquisition"
        assert pipeline.poll_hook() is None
        assert pipeline.artifact_compute_enabled()
        called.append(argv)
        if unknown:
            if any("cre_pipeline_artifact_worker.py" in arg for arg in argv):
                raise refresh.ProcessCleanupUnknown(
                    "synthetic owned worker uncertainty"
                )
            return pipeline.run_artifact_compute(tmp_path, "hash", {"path": "unused"})
        raise refresh.GlobalStageError("synthetic settled failure")

    monkeypatch.setattr(refresh, "run_command", command)
    with pytest.raises(
        refresh.ProcessCleanupUnknown if unknown else refresh.GlobalStageError
    ):
        refresh.main(
            ["--out-root", str(tmp_path / "runs"), "--sources", "transwestern"]
        )
    assert len(called) == (2 if unknown else 1)
    logs = list((tmp_path / "runs").glob("*/logs/host-memory-guard.jsonl"))
    assert json.loads(logs[0].read_text().splitlines()[0])["pressure_level"] == 1
    assert not pipeline.artifact_compute_enabled()
    assert lock_path.exists() is unknown


def test_serial_owned_compute_cancel_settles_without_poll_hook(tmp_path, monkeypatch):
    import os
    import signal
    import sys
    import threading
    import time

    ready = tmp_path / "owned-child.pid"
    wrapper = tmp_path / "owned-python"
    wrapper.write_text(
        "#!"
        + sys.executable
        + "\nimport os,pathlib,time\npathlib.Path("
        + repr(str(ready))
        + ").write_text(str(os.getpid()))\ntime.sleep(5)\n"
    )
    wrapper.chmod(0o700)
    original = signal.signal(signal.SIGTERM, refresh.checkpoint_sigterm_handler)
    failure = []

    def request_stop():
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if ready.exists():
            os.kill(os.getpid(), signal.SIGTERM)
        else:
            failure.append("fixture child never ready")

    requester = threading.Thread(target=request_stop)
    try:
        with (
            pipeline.artifact_compute_scope(tmp_path),
            monkeypatch.context() as patcher,
        ):
            patcher.setattr(refresh.sys, "executable", str(wrapper))
            requester.start()
            with pytest.raises(KeyboardInterrupt):
                pipeline.run_artifact_compute(
                    tmp_path, "hash", {"path": "unused-owned-input"}
                )
        requester.join(4)
        assert not requester.is_alive() and not failure
        pid = int(ready.read_text())
        assert not __import__("pathlib").Path("/proc", str(pid)).exists()
        assert pipeline.poll_hook() is None
        assert not pipeline.artifact_compute_enabled()
    finally:
        signal.signal(signal.SIGTERM, original)
        requester.join(4)


@pytest.mark.parametrize(
    "fault",
    ["failed", "oversized", "bad_request", "nonobject", "boolean_count", "bad_digest"],
)
def test_serial_worker_invalid_metadata_never_becomes_valid(
    tmp_path, monkeypatch, fault
):
    def response(argv, log, **kw):
        if fault == "failed":
            return 1
        from pathlib import Path

        request = json.loads(Path(argv[argv.index("--request") + 1]).read_text())
        path = Path(argv[argv.index("--response") + 1])
        value = {
            "sha256": "a" * 64,
            "bytes": 1,
            "flat_listings": 1,
            "staged_unique": 1,
            "inventory_only": 0,
            "rejected_by_ingest": 0,
            "detail_errors": 0,
        }
        if fault == "boolean_count":
            value["staged_unique"] = True
        if fault == "bad_digest":
            value["sha256"] = "bad"
        if fault == "nonobject":
            value = []
        result = {
            "request_id": "wrong" if fault == "bad_request" else request["request_id"],
            "ok": True,
            "value": value,
        }
        if fault == "oversized":
            result["extra"] = "x" * 65536
        path.write_text(json.dumps(result))
        return 0

    monkeypatch.setattr(refresh, "run_command", response)
    with (
        pipeline.artifact_compute_scope(tmp_path),
        pytest.raises(refresh.GlobalStageError),
    ):
        pipeline.run_artifact_compute(tmp_path, "validate", {})
    assert pipeline.poll_hook() is None and not pipeline.artifact_compute_enabled()


def test_worker_local_validation_no_recursion_and_detects_mid_read_tamper(
    tmp_path, monkeypatch
):
    import cre_pipeline_artifact_worker as worker

    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(artifact("transwestern")))
    args = {
        "path": str(path),
        "source": "transwestern",
        "attempt_started_at": ATTEMPT,
        "kwargs": {"now": NOW.isoformat()},
    }
    result = worker.compute("validate", args, tmp_path / "response.json")
    assert result["staged_unique"] == 2
    wrong = dict(args, file_identity=[0, 0, 0, 0, 0])
    with pytest.raises(refresh.GlobalStageError, match="identity changed"):
        worker.compute("validate", wrong, tmp_path / "response.json")
    actual = refresh.validate_source_artifact

    def mutate(*a, **kw):
        result = actual(*a, **kw)
        path.write_text(path.read_text() + " ")
        return result

    monkeypatch.setattr(refresh, "validate_source_artifact", mutate)
    with pytest.raises(refresh.GlobalStageError, match="changed during"):
        worker.compute("validate", args, tmp_path / "response.json")


def test_manifest_validation_avoids_coordinator_decode_stale_guard_path(
    tmp_path, monkeypatch
):
    from unittest.mock import patch
    import cre_memory_guard as memory
    from test_cre_checkpoint_refresh import strict_artifact

    path = tmp_path / "artifact.json"
    payload = strict_artifact(
        "svn", generation=tmp_path.name, generation_started_at=ATTEMPT
    )
    path.write_text(json.dumps(payload))
    manifest = {
        "started_at": ATTEMPT,
        "config": {"transactions": ["sale", "lease"]},
        "sources": {
            "svn": {
                "artifact": {
                    "path": path.name,
                    "sha256": refresh.sha256_file(path),
                    "attempt_started_at": ATTEMPT,
                }
            }
        },
    }
    guard = memory.HostMemoryGuard(
        tmp_path / "guard.jsonl", lambda **kw: None, sampler=lambda: 1
    )
    guard.previous_observed = 10.0
    actual = refresh._load_json

    def decode(target):
        if target == path:
            # Deterministically model the observed >3s coordinator samplinggap.
            with patch.object(memory.time, "monotonic", side_effect=[13.28, 13.29]):
                guard.sample_once()
        return actual(target)

    monkeypatch.setattr(refresh, "_load_json", decode)
    with pytest.raises(memory.MemoryTelemetryError, match="sample stale"):
        refresh._manifest_checkpoint_artifact_valid(tmp_path, manifest, "svn")
    with pipeline.artifact_compute_scope(tmp_path):
        verified = refresh._manifest_checkpoint_artifact_valid(
            tmp_path, manifest, "svn"
        )
        assert verified is not None and verified[0] == path
        assert pipeline.poll_hook() is None
        with patch.object(memory.time, "monotonic", side_effect=[11.99, 12.0]):
            assert guard.sample_once() == 1
    assert not guard.tripped
