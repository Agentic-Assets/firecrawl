"""No-route policy and durable dispatch handoff tests."""
import copy

import cre_checkpoint_refresh as refresh
import cre_local_dispatcher as dispatcher
import pytest
from scheduling import dispatch_queue


@pytest.fixture
def policy(monkeypatch):
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"algorithm": "sha256", "value": "b" * 64})
    return {"sources": ["colliers"], "admissions": [], "daum_subset": False,
        "concurrency": 1, "workers": 1, "page_cap": 0, "attempts": 1,
        "deadline_seconds": 1800, "expected_sha": "a" * 40, "env_file": "/synthetic/env",
        "windows_launcher": r"C:\synthetic\Invoke-CreGuardedTier.ps1", "windows_launcher_sha256": "c" * 64,
        "windows_repo": str(refresh.REPO_ROOT), "distro": "Ubuntu-24.04", "linux_user": "synthetic",
        "alert_file": None, "notification_mode": "Failure"}


def test_policy_preserves_selected_sources_and_stable_due_locator(policy):
    one = dispatcher.policy_request(policy, "20261106T042000Z_colliers")
    two = dispatcher.policy_request(copy.deepcopy(policy), "20261106T042000Z_colliers")
    assert one == two
    assert one["config"]["sources"] == ["colliers"]
    assert one["config"]["pipeline"] is True
    assert one["target"] == {"algorithm": "sha256", "value": "b" * 64}


def test_same_due_slot_cannot_change_its_source_policy(tmp_path, policy):
    slot = "20261106T042000Z_colliers"
    first = dispatcher.policy_request(policy, slot)
    dispatch_queue.enqueue(tmp_path, slot, first)
    changed = copy.deepcopy(policy)
    changed["sources"] = ["savills"]
    with pytest.raises(dispatch_queue.DispatchRefused, match="different request"):
        dispatch_queue.enqueue(tmp_path, slot, dispatcher.policy_request(changed, slot))


def test_target_change_refuses_before_native_launcher(policy, monkeypatch):
    request = dispatcher.policy_request(policy, "20261106T042000Z_colliers")
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"algorithm": "sha256", "value": "d" * 64})
    monkeypatch.setattr(dispatcher.subprocess, "run", lambda *a, **kw: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="target changed"):
        dispatcher.launch(request)


def test_only_one_target_bound_deadline_successor(policy, monkeypatch):
    slot = "20261106T042000Z_colliers"
    first = dispatcher.policy_request(policy, slot)
    result = {"status": "deadline_interrupted", "outcome": "qualified", "unfinished_sources": ["colliers"]}
    next_slot, next_request = dispatcher.successor(slot, first, result)
    assert next_slot == slot + "_continuation1"
    assert next_request["series_id"] != first["series_id"]
    assert next_request["target"] == first["target"] and next_request["config"] == first["config"]
    assert dispatcher.successor(next_slot, next_request, result) is None
    assert dispatcher.successor(slot, first, {**result, "unfinished_sources": []}) is None
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"algorithm": "sha256", "value": "d" * 64})
    with pytest.raises(ValueError, match="target or source policy changed"):
        dispatcher.successor(slot, first, result)
