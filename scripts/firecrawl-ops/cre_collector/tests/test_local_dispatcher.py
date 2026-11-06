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
    return {"pipeline": True, "sources": ["colliers"], "admissions": [], "daum_subset": False,
        "daum_subset_policy": "tenure-only-v1",
        "concurrency": 1, "workers": 1, "page_cap": 0, "attempts": 1,
        "deadline_seconds": 1800, "expected_sha": "a" * 40, "env_file": "/synthetic/env",
        "windows_launcher": r"C:\synthetic\Invoke-CreGuardedTier.ps1", "windows_launcher_sha256": "c" * 64,
        "task_path": None, "task_name": None, "task_xml_sha256": None,
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


def test_serial_policy_preserves_native_source_admission_and_notification(policy):
    policy.update(pipeline=False, sources=["interra-realty"], workers=1,
                  admissions=["interra-realty"], notification_mode="StartFinish")
    request = dispatcher.policy_request(policy, "20261106T042000Z_interra")
    argv = dispatcher.launcher_arguments(request)
    assert request["config"]["pipeline"] is False
    assert "-Pipeline" not in argv and "-SourceWorkers" not in argv
    assert argv[argv.index("-Source") + 1] == "interra-realty"
    assert argv[argv.index("-AdmitCurrentInventorySource") + 1] == "interra-realty"
    assert argv[argv.index("-NotificationMode") + 1] == "StartFinish"


def test_serial_policy_refuses_multiple_sources_and_workers(policy):
    policy.update(pipeline=False, sources=["colliers", "interra-realty"])
    with pytest.raises(dispatcher.SourcePolicyRefusal, match="serial"):
        dispatcher.policy_request(policy, "20261106T042000Z_bad")
    policy.update(sources=["colliers"], workers=2)
    with pytest.raises(dispatcher.SourcePolicyRefusal, match="serial"):
        dispatcher.policy_request(policy, "20261106T042000Z_bad")


@pytest.mark.parametrize("version", ["tenure-only-v1", "tenure-and-empty-title-v2"])
def test_serial_daum_preserves_subset_policy_version(policy, version):
    policy.update(pipeline=False, sources=["daum-commercial"], workers=1,
                  daum_subset=True, daum_subset_policy=version)
    request = dispatcher.policy_request(policy, "20261106T042000Z_daum")
    argv = dispatcher.launcher_arguments(request)
    assert argv[argv.index("-DaumSubsetPolicy") + 1] == version


def test_wake_retries_due_intent_through_existing_dispatch_and_qualification(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, "HERE", tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    queue = out / "local-dispatch"
    queue.mkdir(mode=0o700)
    dispatch_queue.enqueue(queue, "20261106T042000Z_peer", {"due_unix_ms": 0})
    calls = []
    monkeypatch.setattr(dispatcher, "prepare_launch", lambda req: calls.append("prepare") or [])
    monkeypatch.setattr(dispatcher, "launch", lambda req, argv: calls.append("launch"))
    monkeypatch.setattr(dispatcher, "qualify", lambda req: calls.append("qualify") or {"outcome": "qualified"})
    assert dispatcher.main(["--wake"]) == 0
    assert calls == ["prepare", "launch", "qualify"]
    assert dispatcher.main(["--wake"]) == 0
    assert calls == ["prepare", "launch", "qualify"]


def test_wake_cannot_mix_a_new_policy_or_slot():
    with pytest.raises(SystemExit):
        dispatcher.main(["--wake", "--slot", "20261106T042000Z_peer"])


def test_current_snapshot_records_unknown_history_and_keeps_current_peers(tmp_path, policy):
    first = copy.deepcopy(policy)
    second = copy.deepcopy(policy)
    first.update(task_path="\\Agentic Assets\\CRE\\", task_name="Colliers Partial Refresh", task_xml_sha256="e" * 64)
    second.update(task_path="\\Agentic Assets\\CRE\\", task_name="Savills", task_xml_sha256="f" * 64)
    second["sources"] = ["savills"]
    snapshot = {"schema": "cre-dispatch-current-snapshot/v1", "observed_unix_ms": 1793939400000,
                "tasks": [{"task": "colliers", "policy": first, "due": ["20261106T042000Z"]},
                          {"task": "savills", "policy": second, "due": ["20261106T042000Z"]}]}
    dispatcher.enqueue_current_snapshot(tmp_path, snapshot)
    dispatcher.enqueue_current_snapshot(tmp_path, snapshot)
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert len(list(tmp_path.glob(".history-*.audit"))) == 2
    for audit in tmp_path.glob(".history-*.audit"):
        assert json.loads(audit.read_text())["historical_intervals"] == "unknown_not_replayed"
    changed = copy.deepcopy(snapshot)
    changed["tasks"][0]["policy"]["expected_sha"] = "d" * 40
    dispatcher.enqueue_current_snapshot(tmp_path, changed)
    assert len(list(tmp_path.glob("*.json"))) == 3


def test_current_snapshot_holds_bad_policy_and_keeps_valid_peer(tmp_path, policy):
    policy.update(task_path="\\Agentic Assets\\CRE\\", task_name="Colliers Partial Refresh", task_xml_sha256="e" * 64)
    bad = copy.deepcopy(policy)
    bad["pipeline"] = "invalid"
    snapshot = {"schema": "cre-dispatch-current-snapshot/v1", "observed_unix_ms": 1793939400000,
                "tasks": [{"task": "bad", "policy": bad, "due": ["20261106T042000Z"]},
                          {"task": "good", "policy": policy, "due": ["20261106T042000Z"]}]}
    dispatcher.enqueue_current_snapshot(tmp_path, snapshot)
    states = [json.loads(item.read_text())["state"] for item in tmp_path.glob("*.json")]
    assert sorted(states) == ["held_no_work", "queued"]


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
        dispatcher.prepare_launch(request)


def test_tampered_derived_scope_refuses_before_native_launcher(policy, monkeypatch):
    request = dispatcher.policy_request(policy, "20261106T042000Z_colliers")
    request["config"]["sources"] = ["savills"]
    monkeypatch.setattr(dispatcher.subprocess, "run", lambda *a, **kw: pytest.fail("must not launch"))
    with pytest.raises(dispatch_queue.LocalPreflightRefused, match="source-specific"):
        dispatcher.prepare_launch(request)


def test_failed_native_exec_is_proven_no_work(policy, monkeypatch):
    request = dispatcher.policy_request(policy, "20261106T042000Z_colliers")
    def missing(*args, **kwargs):
        raise FileNotFoundError("synthetic executable missing")
    monkeypatch.setattr(dispatcher.subprocess, "Popen", missing)
    assert dispatcher.launch(request, ["synthetic-missing"])["status"] == "native_start_failed"


def test_error_after_native_start_is_not_no_work(policy, monkeypatch):
    request = dispatcher.policy_request(policy, "20261106T042000Z_colliers")
    class Started:
        def communicate(self):
            raise OSError("synthetic lost process")
    monkeypatch.setattr(dispatcher.subprocess, "Popen", lambda *a, **kw: Started())
    with pytest.raises(OSError, match="lost process"):
        dispatcher.launch(request, ["synthetic"])


def test_deadline_successors_are_unbounded_and_no_progress_is_deferred(policy, monkeypatch):
    slot = "20261106T042000Z_colliers"
    first = dispatcher.policy_request(policy, slot)
    result = {"status": "deadline_interrupted", "outcome": "qualified", "unfinished_sources": ["colliers"],
              "finished_unix_ms": first["due_unix_ms"] + 100}
    next_slot, next_request = dispatcher.successor(slot, first, result)
    assert next_slot == slot + ".continuation1"
    assert next_request["predecessor_series_id"] == first["series_id"]
    assert next_request["series_id"] != first["series_id"]
    assert next_request["target"] == first["target"] and next_request["config"] == first["config"]
    assert next_request["eligible_unix_ms"] == result["finished_unix_ms"] + 300_000
    third_slot, third = dispatcher.successor(next_slot, next_request, result)
    assert third_slot == slot + ".continuation2"
    assert third["predecessor_series_id"] == next_request["series_id"]
    dispatcher.validate_request(third)
    assert dispatcher.successor(slot, first, {**result, "unfinished_sources": []}) is None
    monkeypatch.setattr(refresh, "database_target_fingerprint", lambda _: {"algorithm": "sha256", "value": "d" * 64})
    with pytest.raises(ValueError, match="target or source policy changed"):
        dispatcher.successor(slot, first, result)


def test_crash_after_successor_enqueue_reuses_receipt_bound_time(tmp_path, policy, monkeypatch):
    slot = "20261106T042000Z_colliers"
    first = dispatcher.policy_request(policy, slot)
    result = {"status": "deadline_interrupted", "outcome": "qualified", "unfinished_sources": ["colliers"],
              "finished_unix_ms": first["due_unix_ms"] + 100}
    dispatch_queue.enqueue(tmp_path, slot, first)
    monkeypatch.setattr(dispatch_queue.time, "time", lambda: (first["due_unix_ms"] + 100) / 1000)
    original = dispatch_queue._write
    crashed = []
    def interrupted(directory, name, record, *, create):
        if record["state"] == "settled" and not crashed:
            crashed.append(True)
            raise OSError("synthetic crash")
        return original(directory, name, record, create=create)
    monkeypatch.setattr(dispatch_queue, "_write", interrupted)
    launches = []
    with pytest.raises(OSError, match="synthetic crash"):
        dispatch_queue.drain(tmp_path, launch=lambda _: launches.append(1), qualify=lambda _: dict(result),
                             successor=dispatcher.successor)
    assert dispatch_queue.drain(tmp_path, launch=lambda _: launches.append(1), qualify=lambda _: dict(result),
                                successor=dispatcher.successor)
    assert launches == [1]
    assert (tmp_path / (slot + ".continuation1.json")).exists()


def test_multi_source_wave_does_not_delay_other_unfinished_sources(policy):
    policy["sources"] = ["colliers", "savills"]
    first = dispatcher.policy_request(policy, "20261106T042000Z_broad")
    result = {"status": "deadline_interrupted", "outcome": "qualified",
              "unfinished_sources": ["colliers", "savills"],
              "finished_unix_ms": first["due_unix_ms"] + 100}
    _, following = dispatcher.successor(first["slot"], first, result)
    assert following["eligible_unix_ms"] == result["finished_unix_ms"]


def test_deadline_with_settled_no_child_requalifies_and_omits_priority(policy, monkeypatch):
    slot = "20261106T042000Z_colliers"
    first = dispatcher.policy_request(policy, slot)
    result = {"status": "deadline_interrupted", "outcome": "settled_no_work", "unfinished_sources": ["colliers"],
              "finished_unix_ms": first["due_unix_ms"]}
    _, successor = dispatcher.successor(slot, first, result)
    assert successor["predecessor_series_id"] == first["series_id"]
    assert successor["predecessor_no_work"] is True
    assert "-PriorityFromSeriesId" not in dispatcher.launcher_arguments(successor)
    seen = []
    monkeypatch.setattr(dispatcher, "qualify", lambda request, **kwargs: seen.append(request) or result)
    dispatcher.verify_no_work_predecessor(successor)
    assert seen == [first]
    monkeypatch.setattr(dispatcher, "qualify", lambda _, **kwargs: {"status": "deadline_interrupted", "outcome": "qualified"})
    with pytest.raises(ValueError, match="did not requalify"):
        dispatcher.verify_no_work_predecessor(successor)


def test_scheduled_task_identity_cannot_collide_with_reserved_successor(policy):
    initial = "20261106T042000Z_colliers"
    first = dispatcher.policy_request(policy, initial)
    next_slot, _ = dispatcher.successor(initial, first,
        {"status": "deadline_interrupted", "unfinished_sources": ["colliers"],
         "finished_unix_ms": first["due_unix_ms"]})
    with pytest.raises(ValueError, match="slot must bind"):
        dispatcher.policy_request(policy, next_slot)
    assert dispatcher.policy_request(policy, "20261106T042000Z_colliers_continuation1")["series_id"] != first["series_id"]


def test_real_cli_path_persists_and_drains_synthetic_intent(tmp_path, policy, monkeypatch):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(dispatcher, "HERE", tmp_path)
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(policy))
    calls = []
    monkeypatch.setattr(dispatcher, "prepare_launch", lambda request: ["synthetic"])
    monkeypatch.setattr(dispatcher, "launch", lambda request, _argv: calls.append(("launch", request["series_id"])))
    monkeypatch.setattr(dispatcher, "qualify", lambda request: calls.append(("qualify", request["series_id"]))
                        or {"status": "complete", "outcome": "qualified", "unfinished_sources": []})
    slot = "20200101T000000Z_colliers"
    assert dispatcher.main(["--slot", slot, "--policy", str(policy_file)]) == 0
    assert [row[0] for row in calls] == ["launch", "qualify"]
    assert json.loads((tmp_path / "out" / "local-dispatch" / (slot + ".json")).read_text())["state"] == "settled"


def test_invalid_source_policy_is_held_without_persisting_policy_bytes(tmp_path, policy, monkeypatch):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(dispatcher, "HERE", tmp_path)
    policy["sources"] = ["not-a-real-source"]
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(policy))
    monkeypatch.setattr(dispatcher, "launch", lambda *_: pytest.fail("invalid source must not launch"))
    slot = "20200101T000000Z_invalid"
    assert dispatcher.main(["--slot", slot, "--policy", str(policy_file)]) == 0
    raw = (tmp_path / "out" / "local-dispatch" / (slot + ".json")).read_text()
    record = json.loads(raw)
    assert record["state"] == "held_no_work" and record["result"]["reason"] == "invalid_source_policy"
    assert "not-a-real-source" not in raw


def test_missing_policy_file_records_held_slot_and_does_not_start(tmp_path, monkeypatch):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(dispatcher, "HERE", tmp_path)
    monkeypatch.setattr(dispatcher, "launch", lambda *_: pytest.fail("missing policy must not launch"))
    slot = "20200101T000000Z_missing"
    assert dispatcher.main(["--slot", slot, "--policy", str(tmp_path / "missing.json")]) == 0
    record = json.loads((tmp_path / "out" / "local-dispatch" / (slot + ".json")).read_text())
    assert record["state"] == "held_no_work" and record["result"]["reason"] == "source_policy_unreadable"


def test_unavailable_target_records_due_slot_and_stops_shared_drain(tmp_path, policy, monkeypatch):
    (tmp_path / "out").mkdir()
    monkeypatch.setattr(dispatcher, "HERE", tmp_path)
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps(policy))
    def unavailable(_):
        raise OSError("synthetic target unavailable")
    monkeypatch.setattr(refresh, "database_target_fingerprint", unavailable)
    monkeypatch.setattr(dispatcher, "launch", lambda *_: pytest.fail("unknown target must not launch"))
    slot = "20200101T000000Z_target"
    with pytest.raises(dispatch_queue.DispatchRefused, match="shared target preflight"):
        dispatcher.main(["--slot", slot, "--policy", str(policy_file)])
    record = json.loads((tmp_path / "out" / "local-dispatch" / (slot + ".json")).read_text())
    assert record["state"] == "held_no_work" and record["result"]["reason"] == "shared_preflight_unavailable"


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
