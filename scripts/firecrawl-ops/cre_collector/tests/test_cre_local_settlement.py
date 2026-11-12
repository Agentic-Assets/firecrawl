"""Synthetic local evidence tests; no production receipt or database access."""
import json
import os
import time

import pytest

import cre_local_settlement as reader

SHA = "a" * 40
INVOCATION = "b" * 32
CHILD = "c" * 32
WINDOWS_ID = "e" * 32
SERIES = "2026-11-12T003159Z"
RUN = SERIES + "-0123456789ab"


def write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    root = tmp_path / "collector"
    series = root / "out" / "checkpoint-series" / SERIES
    private = series / ".series-control"
    child_private = series / "runs" / RUN / ".series-control"
    private.mkdir(parents=True, mode=0o700)
    child_private.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(reader, "COLLECTOR", root)
    now = int(time.time() * 1000)
    admission = now - 3000
    identity = lambda p: [p.stat().st_dev, p.stat().st_ino]
    parent = {
        "schema_version": 1, "series_id": SERIES, "status": "complete",
        "collector_git_sha": SHA, "collector_git_dirty": False,
        "config": {"sources": ["franklin-street"], "pipeline": True, "transactions": ["sale", "lease"]},
        "latest_invocation_admission": {"expected_sha": SHA, "windows_admission_unix_ms": admission, "windows_invocation_id": WINDOWS_ID},
        "latest_invocation_control": {"invocation_id": INVOCATION, "receipt": f".series-control/{INVOCATION}.json"},
    }
    control = {
        "status": "complete", "local_settlement": "confirmed",
        "started_unix_ms": now - 2000, "finished_unix_ms": now - 1000,
        "stop_owner": {
            "schema_version": 1, "uid": os.geteuid(), "owner_role": "checkpoint_series_one_shot_cli",
            "invocation_id": INVOCATION, "pid": 12345, "start_ticks": 123,
            "boot_id": "57ea4126-beb5-4ea6-9b9c-1f9c518bbd5c",
            "series_directory_identity": identity(series), "control_directory_identity": identity(private),
        },
        "last_checkpoint": {"run_id": RUN, "child_invocation_id": CHILD, "pid": 12346},
    }
    child = {
        "schema_version": 1, "invocation_id": INVOCATION, "child_invocation_id": CHILD,
        "run_id": RUN, "collector_git_sha": SHA, "pid": 12346,
        "guards_stopped": True, "nested_cleanup_confirmed": True, "terminal_evidence_saved": True,
    }
    write(series / "manifest.json", parent)
    write(private / f"{INVOCATION}.json", control)
    write(child_private / f"{CHILD}.json", child)
    return series, parent, control, child, admission


def attest(evidence):
    return reader.attest(str(evidence[0]), ["franklin-street"], True, SHA, evidence[4], WINDOWS_ID)


def test_confirmed_only_local_process_scope(evidence):
    result = attest(evidence)
    assert result["local_settlement"] == "confirmed"
    assert result["scope"] == "owned_local_processes_only"
    assert result["invocation_id"] == INVOCATION
    assert "database" not in result


@pytest.mark.parametrize("which, key, value", [
    ("parent", "collector_git_sha", "d" * 40),
    ("parent", "collector_git_dirty", True),
    ("parent", "status", "running"),
    ("control", "status", "cleanup_unknown"),
    ("control", "local_settlement", "unknown"),
    ("control", "finished_unix_ms", 1),
    ("child", "nested_cleanup_confirmed", False),
    ("child", "guards_stopped", False),
    ("child", "terminal_evidence_saved", False),
    ("child", "invocation_id", "d" * 32),
    ("child", "collector_git_sha", "d" * 40),
    ("child", "pid", 12347),
])
def test_refuses_unbound_or_incomplete_receipts(evidence, which, key, value):
    series, parent, control, child, _ = evidence
    objects = {"parent": (parent, series / "manifest.json"),
               "control": (control, series / ".series-control" / f"{INVOCATION}.json"),
               "child": (child, series / "runs" / RUN / ".series-control" / f"{CHILD}.json")}
    obj, path = objects[which]
    obj[key] = value
    write(path, obj)
    with pytest.raises((ValueError, KeyError, OSError, reader.safe.StopRefused)):
        attest(evidence)


def test_wrong_admission_source_and_pipeline_refused(evidence):
    for sources, pipeline, sha, admission in [
        (["newmark"], True, SHA, evidence[4]), (["franklin-street"], False, SHA, evidence[4]),
        (["franklin-street"], True, "d" * 40, evidence[4]),
        (["franklin-street"], True, SHA, evidence[4] + 1),
    ]:
        with pytest.raises(ValueError):
            reader.attest(str(evidence[0]), sources, pipeline, sha, admission, WINDOWS_ID)


def test_wrong_windows_invocation_refused(evidence):
    with pytest.raises(ValueError):
        reader.attest(str(evidence[0]), ["franklin-street"], True, SHA, evidence[4], "f" * 32)

def test_symlink_receipt_refused(evidence, tmp_path):
    receipt = evidence[0] / ".series-control" / f"{INVOCATION}.json"
    target = tmp_path / "other"
    receipt.rename(target)
    receipt.symlink_to(target)
    with pytest.raises(OSError):
        attest(evidence)


def test_truncated_receipt_refused(evidence):
    path = evidence[0] / ".series-control" / f"{INVOCATION}.json"
    path.write_text('{"status":', encoding="utf-8")
    with pytest.raises(ValueError):
        attest(evidence)


def test_replaced_receipt_between_reads_refused(evidence, monkeypatch):
    original = reader.safe._read_at
    count = 0
    def changed(fd, name, limit=16384):
        nonlocal count
        count += 1
        raw, st = original(fd, name, limit)
        if count == 4:
            return raw + b" ", st
        return raw, st
    monkeypatch.setattr(reader.safe, "_read_at", changed)
    with pytest.raises(ValueError):
        attest(evidence)


def test_failed_series_can_have_confirmed_local_settlement(evidence):
    series, parent, control, _, _ = evidence
    parent["status"] = control["status"] = "failed"
    write(series / "manifest.json", parent)
    write(series / ".series-control" / f"{INVOCATION}.json", control)
    assert attest(evidence)["terminal_status"] == "failed"

def test_windows_token_preserved_by_producer(monkeypatch):
    import cre_checkpoint_series as series
    import cre_guarded_tier

    monkeypatch.setattr(series, "verify_reviewed_checkout", lambda sha: None)
    monkeypatch.setattr(cre_guarded_tier, "validate_windows_admission", lambda value: None)
    result = series.admit_series_invocation(SHA, 42, WINDOWS_ID)
    assert result["windows_invocation_id"] == WINDOWS_ID
    assert result["windows_admission_unix_ms"] == 42
    assert "windows_invocation_id" not in series.admit_series_invocation(SHA, 42)
    with pytest.raises(series.SeriesError):
        series.admit_series_invocation(SHA, 42, "not-an-id")
    with pytest.raises(series.SeriesError):
        series.admit_series_invocation(SHA, None, WINDOWS_ID)