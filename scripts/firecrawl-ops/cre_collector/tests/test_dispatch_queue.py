"""Durable due-slot behavior; callbacks are local synthetic stand-ins."""
import json
import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest
from scheduling import dispatch_queue as queue


def test_due_slot_is_dispatched_and_verified_once(tmp_path):
    request = {"series_id": "2026-11-06T010203Z-012345abcdef"}
    queue.enqueue(tmp_path, "20261106T010203Z_task", request)
    queue.enqueue(tmp_path, "20261106T010203Z_task", request)
    calls = []
    assert queue.drain(tmp_path, launch=lambda value: calls.append(("launch", value)),
        qualify=lambda value: calls.append(("qualify", value)) or {"outcome": "qualified"})
    assert [call[0] for call in calls] == ["launch", "qualify"]
    assert queue.drain(tmp_path, launch=lambda _: pytest.fail("duplicate dispatch"),
                       qualify=lambda _: pytest.fail("duplicate qualification"))
    with pytest.raises(queue.DispatchRefused, match="different request"):
        queue.enqueue(tmp_path, "20261106T010203Z_task", {"series_id": "different"})


def test_unknown_attempt_prevents_queued_peer_and_is_never_replayed(tmp_path):
    queue.enqueue(tmp_path, "z_prior", {"series_id": "prior"})
    with pytest.raises(RuntimeError):
        queue.drain(tmp_path, launch=lambda _: (_ for _ in ()).throw(RuntimeError("lost launcher")),
                    qualify=lambda _: pytest.fail("unknown launcher cannot be assumed settled"))
    queue.enqueue(tmp_path, "a_peer", {"series_id": "peer"})
    launched = []
    with pytest.raises(queue.DispatchRefused):
        queue.drain(tmp_path, launch=lambda value: launched.append(value),
                    qualify=lambda _: {"outcome": "unknown"})
    assert launched == []
    assert json.loads((tmp_path / "z_prior.json").read_text())["state"] == "dispatch_uncertain"
    assert json.loads((tmp_path / "a_peer.json").read_text())["state"] == "queued"


def test_preexisting_unsafe_queue_is_rejected(tmp_path):
    tmp_path.chmod(0o755)
    with pytest.raises(Exception):
        queue.enqueue(tmp_path, "slot", {})


def test_due_peer_precedes_automatic_successor(tmp_path):
    queue.enqueue(tmp_path, "20261106T000000Z_broad", {"kind": "broad", "continuation_depth": 0})
    events = []
    def launch(request):
        events.append(request["kind"])
        if request["kind"] == "broad":
            queue.enqueue(tmp_path, "20261106T010000Z_peer", {"kind": "peer", "continuation_depth": 0})
    def successor(slot, request, _result):
        if request["kind"] == "broad":
            return slot + ".continuation1", {"kind": "continuation", "continuation_depth": 1}
        return None
    queue.drain(tmp_path, launch=launch, qualify=lambda _: {"outcome": "qualified"}, successor=successor)
    assert events == ["broad", "peer", "continuation"]


def test_bound_no_work_refusal_does_not_block_due_peer(tmp_path):
    queue.enqueue(tmp_path, "a_refused", {"kind": "refused"})
    queue.enqueue(tmp_path, "b_peer", {"kind": "peer"})
    calls = []
    def launch(request):
        calls.append(("launch", request["kind"]))
        if request["kind"] == "refused":
            return {"outcome": "settled_no_work", "status": "native_refused"}
    queue.drain(tmp_path, launch=launch,
        qualify=lambda request: calls.append(("qualify", request["kind"])) or {"outcome": "qualified"})
    assert calls == [("launch", "refused"), ("launch", "peer"), ("qualify", "peer")]


def test_native_no_work_retries_same_intent_after_durable_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(queue.time, "time", lambda: 100.0)
    request = {"series_id": "2026-11-06T010203Z-012345abcdef", "due_unix_ms": 1}
    queue.enqueue(tmp_path, "20261106T010203Z_task", request)
    calls = []
    def launch(value):
        calls.append(value)
        if len(calls) == 1:
            return {"outcome": "settled_no_work", "status": "native_refused", "scope": "source"}
    queue.drain(tmp_path, launch=launch, qualify=lambda _: {"outcome": "qualified"})
    saved = json.loads((tmp_path / "20261106T010203Z_task.json").read_text())
    assert saved["state"] == "queued" and saved["request"] == request
    assert saved["result"]["retry_after_unix_ms"] == 400000
    assert len(list(tmp_path.glob(".refusal-*.audit"))) == 1
    queue.drain(tmp_path, launch=launch, qualify=lambda _: {"outcome": "qualified"})
    assert len(calls) == 1
    monkeypatch.setattr(queue.time, "time", lambda: 400.0)
    queue.drain(tmp_path, launch=launch, qualify=lambda _: {"outcome": "qualified"})
    assert calls == [request, request]
    assert json.loads((tmp_path / "20261106T010203Z_task.json").read_text())["state"] == "settled"
    assert not list(tmp_path.glob("*.continuation*.json"))


def test_crash_before_no_work_record_stays_uncertain(tmp_path, monkeypatch):
    queue.enqueue(tmp_path, "slot", {"series_id": "synthetic"})
    original = queue._write
    def interrupted(directory, name, value, *, create):
        if name.endswith('.audit'):
            raise OSError("synthetic audit interruption")
        return original(directory, name, value, create=create)
    monkeypatch.setattr(queue, "_write", interrupted)
    with pytest.raises(OSError, match="synthetic audit interruption"):
        queue.drain(tmp_path, launch=lambda _: {"outcome": "settled_no_work", "status": "native_refused"},
                    qualify=lambda _: pytest.fail("not yet qualified"))
    assert json.loads((tmp_path / "slot.json").read_text())["state"] == "dispatch_uncertain"
    with pytest.raises(RuntimeError, match="unknown"):
        queue.drain(tmp_path, launch=lambda _: pytest.fail("uncertain launch cannot replay"),
                    qualify=lambda _: (_ for _ in ()).throw(RuntimeError("unknown")))


def test_future_slot_waits_for_later_wake(tmp_path, monkeypatch):
    monkeypatch.setattr(queue.time, "time", lambda: 100.0)
    queue.enqueue(tmp_path, "future", {"due_unix_ms": 101000})
    queue.drain(tmp_path, launch=lambda _: pytest.fail("future intent launched"),
                qualify=lambda _: pytest.fail("future intent qualified"))
    assert json.loads((tmp_path / "future.json").read_text())["state"] == "queued"
    monkeypatch.setattr(queue.time, "time", lambda: 101.0)
    calls = []
    queue.drain(tmp_path, launch=lambda _: calls.append("launch"),
                qualify=lambda _: {"outcome": "qualified"})
    assert calls == ["launch"]


def test_busy_wake_returns_without_dispatch_and_later_wake_retries(tmp_path):
    queue.enqueue(tmp_path, "20261106T010203Z_task", {"due_unix_ms": 0})
    lock = tmp_path / ".owner.lock"
    lock.touch(mode=0o600)
    calls = []
    with lock.open("r+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert queue.drain(tmp_path, launch=lambda _: calls.append("launch"),
                           qualify=lambda _: {"outcome": "qualified"}, wait=False) is False
    assert calls == []
    assert queue.drain(tmp_path, launch=lambda _: calls.append("launch"),
                       qualify=lambda _: {"outcome": "qualified"}, wait=False) is True
    assert calls == ["launch"]


def test_continuation_backoff_yields_to_due_peer_and_wakes_later(tmp_path, monkeypatch):
    monkeypatch.setattr(queue.time, "time", lambda: 100.0)
    queue.enqueue(tmp_path, "20261106T000000Z_a.continuation2", {
        "slot": "20261106T000000Z_a.continuation2", "policy": {}, "due_unix_ms": 1,
        "eligible_unix_ms": 110000, "continuation_depth": 2})
    queue.enqueue(tmp_path, "20261106T000001Z_b", {"due_unix_ms": 1, "kind": "peer"})
    calls = []
    queue.drain(tmp_path, launch=lambda request: calls.append(request.get("kind", "continuation")),
                qualify=lambda _: {"outcome": "qualified"})
    assert calls == ["peer"]
    monkeypatch.setattr(queue.time, "time", lambda: 110.0)
    queue.drain(tmp_path, launch=lambda request: calls.append(request.get("kind", "continuation")),
                qualify=lambda _: {"outcome": "qualified"})
    assert calls == ["peer", "continuation"]


def test_real_process_death_after_prespawn_fences_restart(tmp_path):
    queue.enqueue(tmp_path, "20261106T000000Z_broad", {"kind": "broad"})
    collector = Path(__file__).resolve().parents[1]
    script = (
        "import os,sys; from pathlib import Path; "
        "from scheduling import dispatch_queue as q; "
        "q.drain(Path(sys.argv[1]), launch=lambda _: os._exit(19), "
        "qualify=lambda _: {'outcome':'qualified'})"
    )
    env = {**os.environ, "PYTHONPATH": str(collector)}
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], env=env, timeout=10)
    assert result.returncode == 19
    attempts = []
    with pytest.raises(queue.DispatchRefused):
        queue.drain(tmp_path, launch=lambda _: pytest.fail("must never redispatch uncertain work"),
                    qualify=lambda request: attempts.append(request) or {"outcome": "unknown"})
    assert attempts == [{"kind": "broad"}]


def test_preparation_failure_remains_queued_and_can_be_retried(tmp_path):
    queue.enqueue(tmp_path, "slot", {"kind": "source"})
    with pytest.raises(ValueError, match="preflight"):
        queue.drain(tmp_path, prepare=lambda _: (_ for _ in ()).throw(ValueError("preflight")),
                    launch=lambda *_: pytest.fail("must not launch"), qualify=lambda _: pytest.fail("must not qualify"))
    record = json.loads((tmp_path / "slot.json").read_text())
    assert record["state"] == "queued" and record["result"]["outcome"] == "pre_spawn_refused"
    events = []
    queue.drain(tmp_path, prepare=lambda _: ["prepared"],
        launch=lambda _request, prepared: events.append(prepared),
        qualify=lambda _: {"outcome": "qualified"})
    assert events == [["prepared"]]
    assert json.loads((tmp_path / "slot.json").read_text())["state"] == "settled"


def test_source_local_preparation_hold_preserves_peer_without_spinning(tmp_path):
    queue.enqueue(tmp_path, "a_bad", {"kind": "bad"})
    queue.enqueue(tmp_path, "b_peer", {"kind": "peer"})
    events = []
    def prepare(request):
        events.append(("prepare", request["kind"]))
        if request["kind"] == "bad":
            raise queue.LocalPreflightRefused("source-specific")
        return ["safe"]
    queue.drain(tmp_path, prepare=prepare, launch=lambda request, _: events.append(("launch", request["kind"])),
                qualify=lambda _: {"outcome": "qualified"})
    assert events == [("prepare", "bad"), ("prepare", "peer"), ("launch", "peer")]
    assert json.loads((tmp_path / "a_bad.json").read_text())["state"] == "held_no_work"
    assert json.loads((tmp_path / "b_peer.json").read_text())["state"] == "settled"


def test_shared_native_refusal_stops_and_preserves_due_peer(tmp_path):
    queue.enqueue(tmp_path, "a_first", {"kind": "first"})
    queue.enqueue(tmp_path, "b_peer", {"kind": "peer"})
    with pytest.raises(queue.DispatchRefused, match="shared native"):
        queue.drain(tmp_path, launch=lambda _: {"outcome": "settled_no_work", "scope": "shared"},
                    qualify=lambda _: pytest.fail("must not qualify"))
    assert json.loads((tmp_path / "a_first.json").read_text())["state"] == "settled"
    assert json.loads((tmp_path / "b_peer.json").read_text())["state"] == "queued"
