"""Finite catch-up policy and authentic Scheduler history boundaries."""

import copy
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_cre_code_handoff as fixtures
import test_cre_code_handoff_restoration as restoration_fixtures

import cre_checkpoint_refresh as checkpoint
import cre_code_handoff as operation
from cre_code_handoff_evidence import TASK_PATH, TASKS, Evidence, HandoffError, sha
from cre_code_restoration_evidence import (
    restoration_execution,
    validate_restoration_enable_boundary,
)

complete_case = fixtures.complete_case
lineage_case = fixtures.lineage_case
admitted = fixtures.admitted
restoration_case = restoration_fixtures.restoration_case
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


@pytest.mark.parametrize("encoding_case", ["export-utf16-declaration", "export-with-utf8-bom", "malformed-utf8", "missing-principal"])
def test_execution_reads_native_export_utf8_bytes_without_rewriting(encoding_case):
    packet, inputs, record, observed = scheduler_case("busy")
    originals = {}
    for baseline in packet["task_baseline"]:
        role = baseline["xml"]
        raw = inputs.data[role]
        if encoding_case == "malformed-utf8": raw += b"\xff"
        elif encoding_case == "missing-principal":
            raw = raw.replace(("<UserId>" + SYNTHETIC_SID + "</UserId>").encode(), b"")
        else:
            raw = b'<?xml version="1.0" encoding="UTF-16"?>\r\n' + raw
            if encoding_case == "export-with-utf8-bom": raw = b"\xef\xbb\xbf" + raw
        inputs.data[role] = originals[role] = raw
        pin = next(row for row in inputs.rows if row["role"] == role)
        pin.update(bytes=len(raw), sha256=sha(raw))
    raw_record = fixtures.encoded(record)
    inputs.data["/synthetic/execution.json"] = raw_record
    observed["execution"] = {"path": "/synthetic/execution.json", "bytes": len(raw_record), "sha256": sha(raw_record)}
    evidence = Evidence(packet, inputs.data.__getitem__)
    if encoding_case in {"malformed-utf8", "missing-principal"}:
        with pytest.raises((UnicodeError, ET.ParseError, HandoffError)):
            restoration_execution(packet, evidence, observed)
    else:
        assert restoration_execution(packet, evidence, observed)["qualified"]["state"] == "completed-native-source-settlement-unverified"
    assert all(inputs.data[role] == raw and evidence.raw(role) == raw for role, raw in originals.items())


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


@pytest.mark.parametrize("change", [None, "replaced-sidecar", "foreign-tuple", "recovery-required"])
def test_independent_postreadback_accepts_exact_clear_from_distinct_exited_owner(tmp_path, monkeypatch, change):
    """Use the real lease and flock through the released capture front door."""
    lock = tmp_path / ".cre.lock"
    program = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import cre_checkpoint_refresh as checkpoint
import cre_code_handoff as operation
with checkpoint.SharedLock(Path(sys.argv[2]), recovery_required=True) as lease:
    lease.clear_recovery_requirement()
    released = operation.released_authority(checkpoint, lease)
print(json.dumps(released))
"""
    child = subprocess.run([sys.executable, "-c", program, str(Path(operation.__file__).parent), str(lock)],
                           check=True, capture_output=True, text=True, timeout=15)
    released = json.loads(child.stdout)
    assert released[0][0] != operation.os.getpid() and not lock.exists()
    with pytest.raises(HandoffError, match="released authority owner differs"):
        operation.lock_clear(checkpoint, lock, released=(tuple(released[0]), tuple(released[1])))
    authority = lock.with_name(lock.name + ".authority")
    if change == "replaced-sidecar":
        raw_authority = authority.read_bytes()
        authority.rename(tmp_path / "displaced-authority")
        authority.write_bytes(raw_authority)
    elif change == "foreign-tuple":
        authority.write_bytes(authority.read_bytes().replace(released[0][1].encode(), b"z" * len(released[0][1])))
    elif change == "recovery-required":
        authority.write_bytes(authority.read_bytes().replace(b" normal", b" recovery-required"))
    authority_before = authority.read_bytes()
    packet, inputs, record, native = scheduler_case("none")
    packet.update(enable_boundary="founder-approved-catchup-v1", code=fixtures.NEW,
                  roots={"runtime": str(tmp_path / "runtime"), "native": str(tmp_path / "native")})
    host = fixtures.host_observation()
    inputs.add("runtime_snapshot", host)
    packet["inputs"] = inputs.rows
    packet_raw = fixtures.encoded(packet)
    original = tmp_path / "original"
    output = tmp_path / "independent"
    original.mkdir(); output.mkdir()
    ready_tasks = copy.deepcopy(native["tasks"])
    for task, ready_task in zip(native["tasks"], ready_tasks, strict=True):
        raw = inputs.data[task["xml"]].replace(b"<Enabled>false</Enabled>", b"<Enabled>true</Enabled>")
        target = output / (task["key"] + "-observed.xml")
        target.write_bytes(raw)
        task["xml_sha256"] = ready_task["xml_sha256"] = sha(raw)
        ready_task["xml_reference"] = {"path": str(target), "bytes": len(raw), "sha256": sha(raw)}
    record["tasks"] = copy.deepcopy(native["tasks"])
    execution_raw = fixtures.encoded(record)
    inputs.data["/synthetic/execution.json"] = execution_raw
    execution = {"path": "/synthetic/execution.json", "bytes": len(execution_raw), "sha256": sha(execution_raw)}
    ready = dict(host, schema="cre-code-restoration-observation-v1", native=fixtures.NEW, runtime=fixtures.NEW,
                 observed_at=(NOW + timedelta(seconds=11)).isoformat(), qualified_at=(NOW + timedelta(seconds=12)).isoformat(),
                 tasks=ready_tasks, execution=execution, restoration_sha256=sha(packet_raw),
                 ownership={"phase": "owned-maintenance", "verified": True, "recovery_required": True},
                 resources_ok=True, queues_idle=True)
    (original / "restoration-ready.json").write_bytes(fixtures.encoded({
        "schema": "cre-code-restoration-ready-v1", "code": fixtures.NEW, "restoration_sha256": sha(packet_raw),
        "recovery_required_at_publication": True, "readiness": ready}))
    (original / "restoration-terminal.json").write_bytes(fixtures.encoded({"released_authority": released}))
    native.update(execution=execution, windows_resources={"scope": "post-release observation"},
                  observed_at=(NOW + timedelta(seconds=15)).isoformat())
    (output / "native-observation.json").write_bytes(fixtures.encoded(native))
    monkeypatch.setattr(checkpoint, "canonical_shared_lock_dir", lambda root: lock)
    monkeypatch.setattr(operation, "now", lambda: NOW + timedelta(seconds=16))
    evidence = Evidence(packet, lambda path: inputs.data[str(path)] if str(path) in inputs.data else operation.read_file(path))
    def capture():
        return operation.restoration_capture(packet, evidence, SimpleNamespace(operation=str(original)),
            SimpleNamespace(clean=lambda *args: None), (checkpoint, None, None, None), output,
            packet_raw=packet_raw, final=True)

    if change is None:
        observed = capture()
        assert observed["ownership"] == {"phase": "released-clear", "verified": True, "recovery_required": False}
    else:
        with pytest.raises(HandoffError): capture()
    assert authority.read_bytes() == authority_before
    assert not lock.exists()
