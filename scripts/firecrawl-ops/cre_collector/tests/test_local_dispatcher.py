"""No-route policy and durable dispatch handoff tests."""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import cre_checkpoint_refresh as refresh
import cre_local_dispatcher as dispatcher
import pytest
from scheduling import dispatch_queue
from scheduling import dispatch_settlement


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


def test_full_registry_uses_supported_native_all_selection(policy):
    policy["sources"] = ["all"]
    request = dispatcher.policy_request(policy, "20261106T042000Z_all")
    argv = dispatcher.launcher_arguments(request)
    assert argv[argv.index("-Source") + 1] == "all"
    assert "-Sources" not in argv
    assert request["config"]["sources"] == list(dispatcher.series.SOURCE_KEYS)


def test_native_no_work_marker_must_bind_exact_completed_launch(policy):
    request = dispatcher.policy_request(policy, "20261106T042000Z_colliers")
    marker = {"schema": "cre-dispatch-no-work/v1", "seriesId": request["series_id"],
              "expectedSha": policy["expected_sha"], "targetSha256": request["target"]["value"],
              "source": "colliers", "reason": "local_api_not_ready_no_dispatch"}
    line = "CRE_DISPATCH_NO_WORK:" + json.dumps(marker)
    assert dispatcher.native_no_work_result(1, line, request)["outcome"] == "settled_no_work"
    assert dispatcher.native_no_work_result(0, line, request) is None
    assert dispatcher.native_no_work_result(1, line + "\n" + line, request) is None
    marker["targetSha256"] = "d" * 64
    assert dispatcher.native_no_work_result(1, "CRE_DISPATCH_NO_WORK:" + json.dumps(marker), request) is None


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


def test_tampered_derived_scope_refuses_before_native_launcher(policy, monkeypatch):
    request = dispatcher.policy_request(policy, "20261106T042000Z_colliers")
    request["config"]["sources"] = ["savills"]
    monkeypatch.setattr(dispatcher.subprocess, "run", lambda *a, **kw: pytest.fail("must not launch"))
    with pytest.raises(ValueError, match="intent differs"):
        dispatcher.launch(request)


def test_only_one_target_bound_deadline_successor(policy, monkeypatch):
    slot = "20261106T042000Z_colliers"
    first = dispatcher.policy_request(policy, slot)
    result = {"status": "deadline_interrupted", "outcome": "qualified", "unfinished_sources": ["colliers"]}
    next_slot, next_request = dispatcher.successor(slot, first, result)
    assert next_slot == slot + ".continuation1"
    assert next_request["predecessor_series_id"] == first["series_id"]
    assert next_request["series_id"] != first["series_id"]
    assert next_request["target"] == first["target"] and next_request["config"] == first["config"]
    follow_on_result = dict(result)
    assert dispatcher.successor(next_slot, next_request, follow_on_result) is None
    assert follow_on_result["continuation_limit_reached"] is True
    assert dispatcher.successor(slot, first, {**result, "unfinished_sources": []}) is None
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"algorithm": "sha256", "value": "d" * 64})
    with pytest.raises(ValueError, match="target or source policy changed"):
        dispatcher.successor(slot, first, result)


def test_scheduled_task_identity_cannot_collide_with_reserved_successor(policy):
    initial = "20261106T042000Z_colliers"
    first = dispatcher.policy_request(policy, initial)
    next_slot, _ = dispatcher.successor(initial, first,
        {"status": "deadline_interrupted", "unfinished_sources": ["colliers"]})
    with pytest.raises(ValueError, match="slot must bind"):
        dispatcher.policy_request(policy, next_slot)
    assert dispatcher.policy_request(policy, "20261106T042000Z_colliers_continuation1")["series_id"] != first["series_id"]


def test_real_cli_path_persists_and_drains_synthetic_intent(tmp_path, policy, monkeypatch):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(dispatcher, "HERE", tmp_path)
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(policy))
    calls = []
    monkeypatch.setattr(dispatcher, "launch", lambda request: calls.append(("launch", request["series_id"])))
    monkeypatch.setattr(dispatcher, "qualify", lambda request: calls.append(("qualify", request["series_id"]))
                        or {"status": "complete", "outcome": "qualified", "unfinished_sources": []})
    slot = "20200101T000000Z_colliers"
    assert dispatcher.main(["--slot", slot, "--policy", str(policy_file)]) == 0
    assert [row[0] for row in calls] == ["launch", "qualify"]
    assert json.loads((tmp_path / "out" / "local-dispatch" / (slot + ".json")).read_text())["state"] == "settled"


def test_queue_controls_real_synthetic_series_child_and_receipt(tmp_path, policy, monkeypatch):
    """Exercise actual POSIX control/receipt; native Windows launcher is separate."""
    root = tmp_path / "series"
    root.mkdir()
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir(mode=0o700)
    lock_parent = tmp_path / "lock"
    lock_parent.mkdir(mode=0o700)
    lock_path = lock_parent / ".cre.lock"
    monkeypatch.setattr(refresh, "canonical_shared_lock_dir", lambda: lock_path)
    request = dispatcher.policy_request(policy, "20261106T042000Z_colliers")
    fixture = tmp_path / "synthetic_series.py"
    fixture.write_text('''
import sys
import cre_checkpoint_series as s
s.__name__ = "__main__"
s._CLI_CONTROL_CAPABILITY = object()
s.admit_series_invocation = lambda *_: None
s.git_identity = lambda: ("a" * 40, False)
s.database_target_fingerprint = lambda *_: {"algorithm": "sha256", "value": "b" * 64}
def work(_path, manifest, **_kwargs):
    manifest.update(status="complete", finished_at=s.utc_now())
    return 0
s.run_series = work
raise SystemExit(s.main(sys.argv[1:]))
''')
    collector = Path(__file__).resolve().parents[1]
    calls = []
    def launch(value):
        calls.append("launch")
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(collector)}
        argv = [sys.executable, str(fixture), "--sources", "colliers", "--pipeline", "--source-workers", "1",
            "--page-cap", "0", "--concurrency", "1", "--attempts-per-source", "1",
            "--max-resource-recoveries-per-source", "0", "--max-host-cpu-percent", "90",
            "--cpu-sustain-seconds", "30", "--cpu-sample-seconds", "2", "--memory-sustain-seconds", "20",
            "--memory-sample-seconds", "2", "--nice", "15", "--work-deadline-seconds", "1800",
            "--fresh-series-id", value["series_id"], "--expected-database-target-sha256", "b" * 64,
            "--out-root", str(root)]
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
    def qualify(value):
        calls.append("qualify")
        manifest = json.loads((root / value["series_id"] / "manifest.json").read_text())
        with refresh.SharedLock(lock_path) as owner:
            return dispatch_settlement.qualify(root, value["series_id"],
                manifest["latest_invocation_control"]["invocation_id"], owner=owner,
                expected_sha=value["policy"]["expected_sha"], expected_target=value["target"],
                expected_config=value["config"], expected_deadline_seconds=1800)
    slot = "20200101T000000Z_colliers"
    request = dispatcher.policy_request(policy, slot)
    dispatch_queue.enqueue(queue_dir, slot, request)
    assert dispatch_queue.drain(queue_dir, launch=launch, qualify=qualify)
    assert calls == ["launch", "qualify"]
    record = json.loads((queue_dir / (slot + ".json")).read_text())
    assert record["state"] == "settled" and record["result"]["outcome"] == "settled_no_work"
