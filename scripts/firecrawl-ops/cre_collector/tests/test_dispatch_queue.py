"""Durable due-slot behavior; callbacks are local synthetic stand-ins."""
import json
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
