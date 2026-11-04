"""Synthetic failed second maintenance with partial native Repin and owner archive."""
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import test_cre_maintenance_predecessor as fixtures
from test_cre_code_handoff import xml

from cre_code_handoff_evidence import TASKS, Evidence, HandoffError, sha
from cre_runtime_maintenance_continuation import validate_continuation_predecessor

BEFORE, OLD, encoded = fixtures.BEFORE, fixtures.OLD, fixtures.encoded
predecessor_case = fixtures.predecessor_case

SID = "S-1-5-21-111111111-222222222-333333333-1001"


def ref(path, role=None):
    raw = path.read_bytes()
    row = {"path": str(path), "bytes": len(raw), "sha256": sha(raw)}
    if role is not None: row["role"] = role
    return row


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value if isinstance(value, bytes) else encoded(value))
    return path


@pytest.fixture
def partial_predecessor_case(predecessor_case, tmp_path, request):
    packet, evidence, continuation, archive, operation = predecessor_case
    prior_path = Path(evidence.inputs["failed_release"]["path"])
    prior = json.loads(prior_path.read_bytes())
    prior.update(continuation={"failed_release": "synthetic-earlier"}, runtime_before=OLD,
                 expected_images={}, image_tags={}, install_scope=[])
    audit = tmp_path / "partial-audit"; audit.mkdir()
    inputs, rows, tasks, current_inputs = [], [], [], []
    for key, name in TASKS.items():
        raw = xml(OLD["sha"], False).replace(b"<Actions>",
            ("<Principals><Principal><UserId>" + SID + "</UserId></Principal></Principals><Actions>").encode())
        before_path = write(tmp_path / (key + "-prior.xml"), raw)
        role = "prior-" + key
        inputs.append(ref(before_path, role))
        row = {"key": key, "task_path": "\\CRE\\", "task_name": name, "enabled": False,
               "desired_enabled": True, "missed_runs": 23, "last_task_result": 1 if key == "qualified" else 0,
               "last_run_utc": "2026-10-31T00:00:00Z",
               "next_run_utc": "2026-11-04T12:00:00Z" if key == "qualified" else "2026-11-04T18:00:00Z",
               "actions": [{"execute": "pwsh", "arguments": OLD["sha"], "working_directory": "C:/runtime"}], "xml": role}
        rows.append(row)
        selected = key == "interra"
        current_xml = raw.replace(OLD["sha"].encode(), BEFORE["sha"].encode()) if selected else raw
        current_path = write(tmp_path / (key + "-current.xml"), current_xml)
        current_role = "current-" + key
        current_inputs.append(ref(current_path, current_role))
        current = copy.deepcopy(row)
        current.update(xml=current_role, pin_before=BEFORE["sha"] if selected else OLD["sha"])
        if selected: current["actions"][0]["arguments"] = BEFORE["sha"]
        write(operation / (key + "-before.xml"), raw)
        if selected: write(operation / (key + "-final.xml"), current_xml)
        observed_xml = write(audit / (key + "-observed.xml"), current_xml)
        observed = {field: value for field, value in current.items() if field not in {"xml", "pin_before", "desired_enabled"}}
        observed.update(state="Disabled", xml_sha256=sha(current_xml), xml_reference=ref(observed_xml),
                        pin_state="selected" if selected else "original")
        tasks.append(observed)
        packet.setdefault("task_baseline", []).append(current)
    prior.update(inputs=inputs, task_baseline=rows)
    write(prior_path, prior)
    git_path = operation / "git-observation.json"
    git_observation = json.loads(git_path.read_bytes())
    git_observation["native"]["before"]["tracking"] = getattr(request, "param", BEFORE["sha"])
    write(git_path, git_observation)
    digest = sha(prior_path.read_bytes())
    for name in ("failure.json", "mutation-start.json"):
        path = operation / name; data = json.loads(path.read_bytes()); data["release_sha256"] = digest
        if name == "failure.json":
            data.update(original_error="native-repin failed (1); preserve held tasks and recovery", original_error_type="HandoffError")
        write(path, data)
    outer = Path(str(operation) + ".outer-failure.json")
    data = json.loads(outer.read_bytes()); data.update(release_sha256=digest, original_error="Native foreground exit/drain uncertain", commit_and_recovery_state="unknown; independent readback required"); write(outer, data)
    for name in ("01-native-hold", "01-native-failurehold"):
        path = operation / (name + ".json"); data = json.loads(path.read_bytes())
        data["argv"][data["argv"].index("-MaintenanceSha") + 1] = digest; write(path, data)
    for old_name, new_name in (("03-npm-ls", "02-npm-ls"), ("04-doctor", "03-doctor"), ("05-gfm", "04-gfm")):
        for suffix in (".json", ".log"): write(operation / (new_name + suffix), (operation / (old_name + suffix)).read_bytes())
    hold = json.loads((operation / "01-native-hold.json").read_bytes())
    repin = copy.deepcopy(hold); repin["argv"][repin["argv"].index("-Mode") + 1] = "Repin"
    repin["started_at"] = (datetime(2026, 11, 2, tzinfo=UTC) + timedelta(seconds=5)).isoformat()
    repin["finished_at"] = (datetime(2026, 11, 2, tzinfo=UTC) + timedelta(seconds=5, microseconds=900000)).isoformat()
    repin["exit_code"] = 1
    log = write(operation / "05-native-repin.log", b"maintenance observation expired/future\n")
    repin["command_log_sha256"] = sha(log.read_bytes()); write(operation / "05-native-repin.json", repin)
    native = {"schema": "partial-maintenance-native-readback-v1", "release_sha256": digest,
              "tasks": tasks, "scheduler_instances": [], "native_frontends": [],
              "observed_at": (datetime(2026, 11, 2, tzinfo=UTC) + timedelta(seconds=7)).isoformat()}
    write(audit / "native-observation.json", native)
    start = datetime(2026, 11, 2, tzinfo=UTC)
    watermark = {"schema": "cre-restoration-watermark-v1", "channel": "Microsoft-Windows-TaskScheduler/Operational",
                 "computer": "synthetic-host", "record_id": 100, "record_sha256": "0" * 64,
                 "captured_at": (start + timedelta(microseconds=100000)).isoformat()}
    wm = write(operation / "scheduler-watermark.json", watermark)
    execution = {"schema": "cre-restoration-execution-v1", "watermark": watermark, "watermark_reference": ref(wm),
                 "channel": watermark["channel"], "computer": watermark["computer"], "upper_record_id": 100,
                 "started_at": (start + timedelta(seconds=8)).isoformat(), "finished_at": (start + timedelta(seconds=9)).isoformat(),
                 "events": [], "instances": [], "principals": [{"key": key, "user_id": SID, "sid": SID,
                    "aliases": [SID, SID, "HOST\\fixture"]} for key in TASKS], "observer_sid": SID, "observer_is_admin": False,
                 "tasks": copy.deepcopy(tasks)}
    execution_path = write(audit / "scheduler-observation.json", execution)
    write(audit / "scheduler-execution-reference.json", ref(execution_path))
    containers = [{"service": name, "id": name} for name in ("api", "rabbitmq", "redis", "nuq-postgres", "playwright-service")]
    for path in (operation / "containers-before.json", operation / "containers-after.json", audit / "containers-after.json"):
        write(path, containers)
    config = write(tmp_path / "container-config.json", containers)
    current_inputs.append(ref(config, "container_config_before"))
    old_manifest_path = Path(evidence.inputs["failure_manifest"]["path"])
    old_manifest = json.loads(old_manifest_path.read_bytes())
    keep = [Path(row["path"]) for row in old_manifest["files"] if Path(row["path"]).exists()]
    files = {str(path): path for path in [*keep, *audit.iterdir(), *operation.iterdir(), prior_path, outer] if path.is_file()}
    manifest = {"schema": "independent-maintenance-failure-audit-manifest-v1", "release_input_count": len(inputs),
                "release_inputs": inputs, "files": [ref(path, "synthetic") for path in files.values()]}
    write(old_manifest_path, manifest)
    known = write(archive / "known_outcome.evidence", old_manifest_path.read_bytes())
    for name in ("prepared.json", "completed.json"):
        path = archive / name; data = json.loads(path.read_bytes())
        data["evidence"]["known_outcome"].update(sha256=sha(known.read_bytes()), size=len(known.read_bytes()))
        write(path, data)
    roles = [ref(Path(evidence.inputs[name]["path"]), name) for name in
             ("failed_release", "failure_manifest", "recovery_prepared", "recovery_completed", "recovery_expected_state")]
    packet["inputs"] = roles + current_inputs
    return packet, Evidence(packet, lambda path: Path(path).read_bytes()), continuation


def test_partial_failure_predecessor_authenticates_each_task_and_archive(partial_predecessor_case):
    packet, evidence, continuation = partial_predecessor_case
    validate_continuation_predecessor(packet, evidence, continuation)


def test_partial_failure_predecessor_rejects_unbacked_pin(partial_predecessor_case):
    packet, evidence, continuation = partial_predecessor_case
    packet["task_baseline"][0]["pin_before"] = OLD["sha"]
    with pytest.raises(HandoffError):
        validate_continuation_predecessor(packet, evidence, continuation)


@pytest.mark.parametrize("item", ["repin", "scheduler", "native", "final-xml", "container", "owner", "prior-input"])
def test_partial_failure_predecessor_rejects_changed_evidence(partial_predecessor_case, item):
    packet, evidence, continuation = partial_predecessor_case
    operation = Path(continuation["failed_operation"])
    base = operation.parent
    targets = {"repin": operation / "05-native-repin.log", "scheduler": base / "partial-audit/scheduler-observation.json",
               "native": base / "partial-audit/native-observation.json", "final-xml": operation / "interra-final.xml",
               "container": base / "partial-audit/containers-after.json", "owner": base / "authority-observation.json",
               "prior-input": base / "interra-prior.xml"}
    targets[item].write_bytes(b"tampered")
    with pytest.raises(HandoffError):
        validate_continuation_predecessor(packet, evidence, continuation)


@pytest.mark.parametrize("partial_predecessor_case", ["f" * 40], indirect=True)
def test_partial_predecessor_rejects_foreign_before_tracking(partial_predecessor_case):
    packet, evidence, continuation = partial_predecessor_case
    with pytest.raises(HandoffError, match="original Git readback"):
        validate_continuation_predecessor(packet, evidence, continuation)


@pytest.mark.parametrize("fault", [None, "history", "xml", "missed", "next", "execution"])
def test_fresh_held_readback_joins_crossed_disabled_trigger(partial_predecessor_case, tmp_path, monkeypatch, fault):
    packet, evidence, continuation = partial_predecessor_case
    audit = Path(continuation["failed_operation"]).parent / "partial-audit"
    native = json.loads((audit / "native-observation.json").read_bytes())
    tasks = copy.deepcopy(native["tasks"])
    for task in tasks: task.pop("pin_state")
    qualified = next(row for row in tasks if row["key"] == "qualified")
    baseline = next(row for row in packet["task_baseline"] if row["key"] == "qualified")
    qualified.update(missed_runs=24, next_run_utc="2026-11-04T15:00:00Z")
    baseline.update(missed_runs=24, next_run_utc="2026-11-04T15:00:00Z")
    if fault == "history": qualified["last_task_result"] = 0
    if fault == "xml": qualified["xml_sha256"] = "0" * 64
    if fault == "missed": qualified["missed_runs"] = baseline["missed_runs"] = 22
    if fault == "next": qualified["next_run_utc"] = baseline["next_run_utc"] = "2026-11-04T12:30:00Z"
    execution = json.loads((audit / "scheduler-observation.json").read_bytes())
    execution.update(started_at="2026-11-04T12:59:59Z", finished_at="2026-11-04T13:00:00Z",
                     tasks=[{key: value for key, value in row.items() if key != "xml_reference"} for row in tasks])
    if fault == "execution": execution["instances"] = [{"key": "qualified", "instance": "running"}]
    execution_path = write(tmp_path / "fresh-scheduler.json", execution)
    fresh = {"schema": "cre-held-partial-maintenance-observation-v1", "observed_at": "2026-11-04T13:00:01Z",
             "tasks": tasks, "execution": ref(execution_path), "frontends": [], "instances": []}
    fresh_path = write(tmp_path / "fresh-observation.json", fresh)
    packet["inputs"].append(ref(fresh_path, "fresh_held"))
    capture_path = write(tmp_path / "synthetic-capture.json", {"fixture": True})
    packet["inputs"].append(ref(capture_path, "capture_fixture"))
    program_path = write(tmp_path / "synthetic-program.bin", b"fixture")
    packet["inputs"].append(ref(program_path, "program_fixture"))
    packet["programs"] = {"native_git": "unused"}
    packet["roots"]["development"] = str(tmp_path)
    continuation.update(fresh_held_observation="fresh_held", fresh_held_capture="capture_fixture", fresh_held_program="program_fixture")
    monkeypatch.setattr("cre_runtime_maintenance_continuation.validate_fresh_held_capture", lambda *_args: None)
    evidence = Evidence(packet, lambda path: Path(path).read_bytes())
    if fault:
        with pytest.raises(HandoffError): validate_continuation_predecessor(packet, evidence, continuation)
    else:
        validate_continuation_predecessor(packet, evidence, continuation)
