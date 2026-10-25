"""Finite catch-up policy and authentic Scheduler history boundaries."""

import copy
from datetime import timedelta

import pytest
import test_cre_code_handoff as fixtures
from test_cre_code_handoff_restoration import restoration_case  # noqa: F401

import cre_checkpoint_refresh as checkpoint
import cre_code_handoff as operation

from cre_code_handoff_evidence import Evidence, HandoffError, TASK_PATH, TASKS, sha
from cre_code_restoration_evidence import restoration_execution, validate_restoration_enable_boundary

complete_case = fixtures.complete_case
lineage_case = fixtures.lineage_case
admitted = fixtures.admitted
NOW = fixtures.NOW
SYNTHETIC_SID = "S-1-5-21-111111111-222222222-333333333-1001"


def approved_packet(case):
    packet, _, inputs, put, _ = case
    packet["enable_boundary"] = "founder-approved-catchup-v1"
    for row, missed in zip(packet["task_baseline"], (5, 5, 6), strict=True):
        row["missed_runs"] = missed
        row["next_run_utc"] = (NOW + timedelta(hours=3)).isoformat()
    decision = put("human-catchup-choice", {
        "catchup": {"received": True, "answer": "Authorize one-time catch-up recovery (Recommended)",
                    "questionItemId": '["request_user_input_async","call_564c89f05bae4490afe3bb3e601391bb",0]'},
        "original_approval_prompt": {"catchupIssue": "AGENTIC-4845", "originalTasks": list(TASKS.values())},
        "outreach": {"received": True, "answer": "Keep all outreach unsent"},
    })
    failed = next(row for row in packet["inputs"] if row["role"] == packet["failed_handoff"]["release"])
    packet["catchup_policy"] = {"decision": decision, "failed_release_sha256": failed["sha256"],
                              "fleet": copy.deepcopy(packet["task_baseline"]), "tooling": packet["tooling"]}
    packet["inputs"] = inputs.rows
    return packet, inputs


@pytest.mark.parametrize("change", [None, "899", "900", "bool", "fleet", "tooling", "original", "decision"])
def test_then_current_fleet_requires_packet_bound_human_policy(restoration_case, change):
    packet, inputs = approved_packet(restoration_case)
    if change in {"899", "900"}:
        packet["task_baseline"][0]["next_run_utc"] = (NOW + timedelta(seconds=int(change))).isoformat()
        packet["catchup_policy"]["fleet"] = copy.deepcopy(packet["task_baseline"])
    elif change == "bool":
        packet["task_baseline"][1]["missed_runs"] = True
        packet["catchup_policy"]["fleet"] = copy.deepcopy(packet["task_baseline"])
    elif change == "fleet": packet["task_baseline"][1]["missed_runs"] += 1
    elif change == "tooling": packet["catchup_policy"]["tooling"] = fixtures.OLD
    elif change == "original": packet["catchup_policy"]["failed_release_sha256"] = "0" * 64
    elif change == "decision": packet["catchup_policy"]["decision"] = packet["failed_handoff"]["release"]
    if change in {None, "900"}:
        validate_restoration_enable_boundary(packet, inputs.evidence(packet), NOW)
    else:
        with pytest.raises(HandoffError):
            validate_restoration_enable_boundary(packet, inputs.evidence(packet), NOW)


def scheduler_case(outcome):
    inputs = fixtures.Inputs()
    baselines, tasks = [], []
    for key, name in TASKS.items():
        raw = fixtures.xml(fixtures.NEW["sha"], False).replace(
            b"<Actions>", ("<Principals><Principal><UserId>" + SYNTHETIC_SID
                           + "</UserId></Principal></Principals><Actions>").encode())
        role = inputs.add("held-" + key, raw)
        row = {"key": key, "task_path": TASK_PATH, "task_name": name, "enabled": False,
               "desired_enabled": True, "state": "Disabled", "xml": role, "missed_runs": 6,
               "last_task_result": 1, "last_run_utc": (NOW - timedelta(days=1)).isoformat(),
               "next_run_utc": (NOW + timedelta(hours=3)).isoformat(), "actions": [{"execute": "pwsh.exe"}]}
        baselines.append(row)
        tasks.append(dict(row, enabled=True, state="Ready"))
    lower = {"schema": "cre-restoration-watermark-v1", "channel": "Microsoft-Windows-TaskScheduler/Operational",
             "computer": "synthetic-host", "record_id": 100, "record_sha256": "0" * 64,
             "captured_at": NOW.isoformat()}
    raw_lower = fixtures.encoded(lower)
    inputs.data["/synthetic/lower.json"] = raw_lower
    record = {"schema": "cre-restoration-execution-v1", "watermark": lower,
              "watermark_reference": {"path": "/synthetic/lower.json", "sha256": sha(raw_lower), "bytes": len(raw_lower)},
              "channel": lower["channel"], "computer": lower["computer"], "upper_record_id": 110,
              "started_at": (NOW + timedelta(seconds=1)).isoformat(), "finished_at": (NOW + timedelta(seconds=10)).isoformat(),
              "events": [], "instances": [], "principals": [{"key": key, "user_id": "test-user", "sid": SYNTHETIC_SID,
                                                            "aliases": ["test-user", SYNTHETIC_SID, "HOST\\test-user"]} for key in TASKS],
              "observer_sid": SYNTHETIC_SID, "observer_is_admin": False, "tasks": tasks}
    guid = "{11111111-2222-3333-4444-555555555555}"
    if outcome != "none":
        task = tasks[2]
        task.update(last_run_utc=(NOW + timedelta(seconds=4)).isoformat(),
                    last_task_result=1 if outcome == "busy" else 0, missed_runs=2)
        codes = (107, 129, 100, 200) if outcome == "running" else (107, 129, 100, 200, 201, 102)
        for offset, event in enumerate(codes):
            data = {"TaskName": TASK_PATH + TASKS["qualified"]}
            if event != 129:
                data["InstanceId" if event in {107, 100, 102} else "TaskInstanceId"] = guid
            if event == 100: data["UserContext"] = "HOST\\test-user"
            if event == 129: data["ProcessID"] = "1234"
            if event in {200, 201}: data.update(ActionName="pwsh.exe", EnginePID="1234")
            if event == 201: data["ResultCode"] = str(task["last_task_result"])
            fields = "".join(f'<Data Name="{key}">{value}</Data>' for key, value in data.items())
            when = (NOW + timedelta(seconds=offset + 2)).isoformat()
            record["events"].append(
                '<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System>'
                '<Provider Name="Microsoft-Windows-TaskScheduler"/>'
                f'<EventID>{event}</EventID><EventRecordID>{101 + offset}</EventRecordID>'
                f'<TimeCreated SystemTime="{when}"/><Computer>synthetic-host</Computer>'
                f'<Channel>{lower["channel"]}</Channel></System><EventData>{fields}</EventData></Event>')
        if outcome == "running":
            task["state"] = "Running"
            record["instances"] = [{"key": "qualified", "instance_guid": guid, "engine_pid": 1234, "state": 4}]
    packet = {"inputs": inputs.rows, "task_baseline": baselines}
    observed = {"tasks": tasks, "observed_at": (NOW + timedelta(seconds=11)).isoformat()}
    return packet, inputs, record, observed


@pytest.mark.parametrize("outcome", ["none", "busy", "success", "running"])
@pytest.mark.parametrize("change", [None, "foreign", "truncated", "duplicate", "unexplained", "principal", "result",
                                   "missing-sid", "foreign-sid", "conflicting-sid-alias", "conflicting-name-alias", "extra-alias"])
def test_bounded_native_history_never_claims_source_settlement(outcome, change):
    packet, inputs, record, observed = scheduler_case(outcome)
    if change == "foreign": record["computer"] = "foreign-host"
    elif change == "truncated": record["upper_record_id"] = 99
    elif change == "duplicate":
        if record["events"]: record["events"].append(record["events"][0])
        else: observed["tasks"][0]["last_task_result"] = 2
    elif change == "unexplained": observed["tasks"][0]["last_run_utc"] = NOW.isoformat()
    elif change == "principal": record["observer_sid"] = "S-1-foreign"
    elif change == "result": observed["tasks"][2]["last_task_result"] = 9
    elif change == "missing-sid": del record["principals"][2]["sid"]
    elif change == "foreign-sid": record["principals"][2]["sid"] = "S-1-5-21-111111111-222222222-333333333-1002"
    elif change == "conflicting-sid-alias": record["principals"][2]["aliases"][1] = "S-1-5-18"
    elif change == "conflicting-name-alias": record["principals"][2]["aliases"][2] = "HOST\\foreign-user"
    elif change == "extra-alias": record["principals"][2]["aliases"].append("foreign-user")
    raw = fixtures.encoded(record)
    inputs.data["/synthetic/execution.json"] = raw
    observed["execution"] = {"path": "/synthetic/execution.json", "sha256": sha(raw), "bytes": len(raw)}
    evidence = Evidence(packet, inputs.data.__getitem__)
    if change is None or (change == "result" and outcome == "running"):
        outcomes = restoration_execution(packet, evidence, observed)
        assert all(row["source_settlement"] == "unverified" for row in outcomes.values())
        assert outcomes["qualified"]["state"].startswith("no-observed" if outcome == "none" else outcome if outcome == "running" else "completed")
    else:
        with pytest.raises(HandoffError): restoration_execution(packet, evidence, observed)


def test_real_lease_suppressed_removal_failure_is_not_release_proof(tmp_path, monkeypatch):
    lock = tmp_path / ".cre.lock"
    lease = checkpoint.SharedLock(lock, recovery_required=True)
    with lease:
        lease.clear_recovery_requirement()
        released = operation.released_authority(checkpoint, lease)
        monkeypatch.setattr(checkpoint.shutil, "rmtree", lambda path, **kwargs: None)
    assert not lease.held and lock.is_dir()
    with pytest.raises(HandoffError, match="directory present"):
        operation.lock_clear(checkpoint, lock, released=released)


def test_real_successor_keeps_its_authority_when_original_release_is_observed(tmp_path):
    lock = tmp_path / ".cre.lock"
    with checkpoint.SharedLock(lock, recovery_required=True) as predecessor:
        predecessor.clear_recovery_requirement()
        released = operation.released_authority(checkpoint, predecessor)
    with checkpoint.SharedLock(lock) as successor:
        token, generation = successor.authority_token, successor.authority_generation
        with pytest.raises(HandoffError): operation.lock_clear(checkpoint, lock, released=released)
        assert successor.held and successor.authority_token == token and successor.authority_generation == generation
        assert checkpoint._lock_directory_identity(lock) == successor.directory_identity
