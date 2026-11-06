"""Durable due-slot behavior; callbacks are local synthetic stand-ins."""
import json

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
