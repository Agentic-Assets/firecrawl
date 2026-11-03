"""Synthetic failed attempt with a real Scheduler principal in original XML."""

import copy
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_cre_code_handoff as handoff
import test_cre_code_handoff_restoration as restoration

import cre_code_handoff as operation
from cre_code_handoff_evidence import DEPENDENCY_CHECKS, Evidence, sha
from cre_code_restoration_evidence import validate_restoration
from cre_runtime_maintenance_transition import validate_maintenance_execution

admitted = restoration.admitted
complete_case = restoration.complete_case
lineage_case = restoration.lineage_case
restoration_case = restoration.restoration_case


@pytest.fixture(autouse=True)
def scheduler_principal_in_synthetic_xml(monkeypatch):
    original = handoff.xml

    def xml(commit, enabled=True):
        return original(commit, enabled).replace(
            b"<Actions>", b"<Principals><Principal><UserId>S-1-5-21-111111111-222222222-333333333-1001"
            b"</UserId></Principal></Principals><Actions>")

    monkeypatch.setattr(handoff, "xml", xml)


def test_synthetic_failed_handoff_with_scheduler_principal_is_consistent(restoration_case):
    packet, evidence, _, _, _ = restoration_case
    validate_restoration(packet, evidence)


def test_scheduler_evidence_joins_the_synthetic_failed_fleet(restoration_case):
    packet, evidence, inputs, _, _ = restoration_case
    now = handoff.NOW
    sid = "S-1-5-21-111111111-222222222-333333333-1001"
    tasks = [dict(row, state="Disabled") for row in packet["task_baseline"]]
    lower = {"schema": "cre-restoration-watermark-v1", "channel": "Microsoft-Windows-TaskScheduler/Operational",
             "computer": "synthetic-host", "record_id": 100, "record_sha256": "0" * 64,
             "captured_at": now.isoformat()}
    lower_raw = handoff.encoded(lower)
    inputs.data["/synthetic/maintenance-watermark"] = lower_raw
    record = {"schema": "cre-restoration-execution-v1", "watermark": lower,
              "watermark_reference": {"path": "/synthetic/maintenance-watermark", "bytes": len(lower_raw),
                                      "sha256": handoff.sha(lower_raw)},
              "channel": lower["channel"], "computer": lower["computer"], "upper_record_id": 100,
              "started_at": (now + timedelta(seconds=1)).isoformat(),
              "finished_at": (now + timedelta(seconds=10)).isoformat(), "events": [], "instances": [],
              "principals": [{"key": row["key"], "user_id": "test-user", "sid": sid,
                              "aliases": ["test-user", sid, "HOST\\test-user"]} for row in tasks],
              "observer_sid": sid, "observer_is_admin": False, "tasks": tasks}
    raw = handoff.encoded(record)
    inputs.data["/synthetic/maintenance-execution"] = raw
    observation = {"tasks": tasks, "observed_at": (now + timedelta(seconds=11)).isoformat(),
                   "execution": {"path": "/synthetic/maintenance-execution", "bytes": len(raw),
                                 "sha256": handoff.sha(raw)}}
    validate_maintenance_execution(packet, evidence, observation, hold_at=now - timedelta(seconds=1),
                                   mutation_at=now + timedelta(seconds=1), terminal_at=now + timedelta(seconds=9))


@pytest.fixture
def qualified_ready_case(restoration_case, tmp_path):
    """Synthetic A and reviewed maintenance admission with real retained bytes."""
    restoration.zero_missed_packet(restoration_case)
    packet, _, inputs, put, path = restoration_case
    now = handoff.NOW
    old = packet.pop("code")
    new = {"sha": "e" * 40, "tree": old["tree"]}
    inputs.add("historical_runtime_snapshot", inputs.data[path("runtime_snapshot")])
    inputs.add("runtime_allowed_paths_nul", b"")
    inputs.add("native_allowed_paths_nul", b"")
    if not any(row["role"] == "bundle" for row in inputs.rows):
        inputs.add("bundle", b"synthetic bundle")
    host = handoff.host_observation()
    normalized = [{"service": "service-" + row["id"], "id": row["id"], "image": row["image"],
                   "started_at": row["started_at"], "status": "running", "restart_policy": "always",
                   "config_sha256": "1" * 64} for row in host["containers"]]
    inputs.add("container_config_before", normalized)
    scope = {"deployed": old, "selected": new, "allowed_paths": [], "objects": {}, "changed_dependency_paths": []}
    packet.update(schema="cre-runtime-maintenance-v1", scope="held-runtime-maintenance", old=old, new=new,
                  native_before=old, runtime_change=scope, native_change=scope,
                  install_scope=[], expected_images={}, image_tags={}, expected_after_config={})
    packet["build_source"] = {"root": "/home/cayman/synthetic-build-source", "identity": new}
    packet["programs"]["native_docker"] = "C:/Docker/docker.exe"
    failed_ref = next(row for row in inputs.rows if row["role"] == packet["failed_handoff"]["release"])
    packet["failed_handoff_release"] = {key: failed_ref[key] for key in ("path", "sha256", "bytes")}
    from cre_code_handoff_evidence import role_paths
    packet["roots"] = role_paths(packet)
    packet["inputs"] = inputs.rows
    release_raw = handoff.encoded(packet)
    release = tmp_path / "release.json"
    release.write_bytes(release_raw)
    evidence = Evidence(packet, lambda name: inputs.data[name] if name in inputs.data else Path(name).read_bytes())
    before = host
    before.update(schema="cre-runtime-maintenance-observation-v1", native=old, runtime=old,
                  observed_at=(now - timedelta(seconds=10)).isoformat(), qualified_at=(now - timedelta(seconds=9)).isoformat(),
                  release_sha256=handoff.sha(release_raw),
                  ownership={"phase": "released-clear", "verified": True, "recovery_required": False},
                  resources_ok=True, queues_idle=True,
                  tasks=[dict(row, state="Disabled", xml_sha256=handoff.sha(inputs.data[path(row["xml"])]))
                         for row in packet["task_baseline"]])
    observation = tmp_path / "initial-observation.json"
    observation.write_bytes(handoff.encoded(before))
    body = tmp_path / "review.md"
    body.write_bytes(b"synthetic independent review")
    review = tmp_path / "review.json"
    review.write_bytes(handoff.encoded(handoff.attestation({"release_sha256": handoff.sha(release_raw),
                                                            "tooling": packet["tooling"]}, body, body.read_bytes(),
                                                           scope="runtime-maintenance")))
    output = tmp_path / "operation"
    output.mkdir()
    auth = {"schema": "cre-runtime-maintenance-authorization-v1", "scope": "held-runtime-maintenance",
            "release_sha256": handoff.sha(release_raw), "observation_sha256": handoff.sha(observation.read_bytes()),
            "authorized_at": (now - timedelta(seconds=8)).isoformat(),
            "expires_at": (now + timedelta(seconds=200)).isoformat(), "operation_output": str(output),
            "review": {"path": str(review), "sha256": handoff.sha(review.read_bytes()),
                       "reviewer": "independent", "approval_ref": "AGENTIC-test"}}
    authorization = tmp_path / "authorization.json"
    authorization.write_bytes(handoff.encoded(auth))
    return SimpleNamespace(packet=packet, evidence=evidence, inputs=inputs, put=put, path=path,
                           now=now, old=old, new=new, release=release, release_raw=release_raw,
                           observation=observation, authorization=authorization, output=output, before=before,
                           normalized=normalized)


def test_qualified_ready_fixture_preserves_a_and_original_authorization(qualified_ready_case):
    from cre_code_handoff_evidence import json_bytes
    from cre_runtime_maintenance import (
        validate_historical_admission,
        validate_maintenance_authorization,
    )

    case = qualified_ready_case
    validate_historical_admission(case.packet, case.evidence)
    validate_maintenance_authorization(case.release_raw, case.packet, case.evidence, case.observation.read_bytes(),
                                       json_bytes(case.authorization.read_bytes()), case.now, output=case.output)


@pytest.fixture
def foreground_ready_case(qualified_ready_case, monkeypatch):
    import cre_code_handoff_git
    import cre_runtime_maintenance_transition as transition

    case = qualified_ready_case
    # This synthetic-A fixture has intentionally invented SHA values. The
    # historical Git reader is exercised with real temporary commits separately.
    monkeypatch.setattr(cre_code_handoff_git.Git, "maintenance_ancestry", lambda *args: None)
    monkeypatch.setattr(transition, "validate_maintenance_git_history", lambda *args: None)
    monkeypatch.setattr(transition, "validate_maintenance_dependency_history", lambda *args: None)
    monkeypatch.setattr(transition, "validate_maintenance_tracking_history", lambda *args: None)
    checkpoint = SimpleNamespace(safe_process_env=dict,
                                 run_command=lambda argv, log, env: (log.write_bytes(b"settled synthetic output\n") and 0))
    monkeypatch.setattr(operation, "Git", lambda _: SimpleNamespace(clean=lambda *args, **kwargs: None))
    monkeypatch.setattr(operation, "windows_path", lambda value: "C:/synthetic/" + Path(str(value)).name)
    times = iter(case.now + timedelta(seconds=index) for index in range(30))
    monkeypatch.setattr(operation, "now", lambda: next(times))
    runner = operation.Foreground(case.packet, checkpoint, case.output)
    runner.native("Hold", case.release, sha(case.release_raw), case.observation, case.authorization)
    collector = operation.local_path(case.packet["roots"]["runtime"]) / "scripts/firecrawl-ops/cre_collector"
    for name, argv in DEPENDENCY_CHECKS.items():
        runner.run(name, ["bash", "-c", 'cd -- "$1" && shift && exec "$@"', "cre-maintenance", str(collector), *argv],
                   effective_cwd=collector)
    for mode in ("Repin", "Verify"):
        runner.native(mode, case.release, sha(case.release_raw), case.observation, case.authorization)
    assert runner.counter == 6
    return case, collector


def test_real_foreground_emits_full_synthetic_maintenance_command_sequence(foreground_ready_case):
    case, collector = foreground_ready_case
    from cre_runtime_maintenance_transition import validate_maintenance_command
    for file in sorted(case.output.glob("*.json")):
        from cre_code_handoff_evidence import json_bytes
        data = json_bytes(file.read_bytes())
        log = file.with_suffix(".log")
        def ref(path): return {"path": str(path), "sha256": sha(path.read_bytes()), "bytes": path.stat().st_size}
        expected = case.new if data["schema"] == "cre-owned-gate-v1" else None
        validate_maintenance_command({"receipt": ref(file), "log": ref(log)}, data["argv"], expected,
                                     str(collector) if expected else None, lambda path: Path(path).read_bytes())


@pytest.fixture
def ready_payload_case(foreground_ready_case):
    case, collector = foreground_ready_case
    now, output = case.now, case.output
    sid = "S-1-5-21-111111111-222222222-333333333-1001"
    def write(name, value):
        path = output / name
        path.write_bytes(value if isinstance(value, bytes) else handoff.encoded(value))
        return path
    def ref(path):
        raw = path.read_bytes()
        return {"path": str(path), "sha256": sha(raw), "bytes": len(raw)}
    mutation = write("mutation-start.json", {"schema": "cre-runtime-maintenance-mutation-v1",
        "release_sha256": sha(case.release_raw), "authorization_sha256": sha(case.authorization.read_bytes()),
        "operation_output": str(output), "tooling": case.packet["tooling"], "fleet": case.packet["task_baseline"],
        "entered_at": (now + timedelta(microseconds=500000)).isoformat()})
    tasks = []
    for row in case.packet["task_baseline"]:
        final = case.inputs.data[case.path(row["xml"])].replace(case.old["sha"].encode(), case.new["sha"].encode())
        file = write(row["key"] + "-final.xml", final)
        actions = [{key: value.replace(case.old["sha"], case.new["sha"]) for key, value in action.items()}
                   for action in row["actions"]]
        tasks.append(dict(row, state="Disabled", actions=actions, xml_sha256=sha(final), xml_reference=ref(file)))
    lower = {"schema": "cre-restoration-watermark-v1", "channel": "Microsoft-Windows-TaskScheduler/Operational",
             "computer": "synthetic-host", "record_id": 100, "record_sha256": "0" * 64,
             "captured_at": (now + timedelta(microseconds=250000)).isoformat()}
    watermark = write("scheduler-watermark.json", lower)
    record = {"schema": "cre-restoration-execution-v1", "watermark": lower, "watermark_reference": ref(watermark),
              "channel": lower["channel"], "computer": lower["computer"], "upper_record_id": 100,
              "started_at": (now + timedelta(seconds=10, microseconds=200000)).isoformat(),
              "finished_at": (now + timedelta(seconds=10, microseconds=800000)).isoformat(), "events": [], "instances": [],
              "principals": [{"key": row["key"], "user_id": "test-user", "sid": sid,
                              "aliases": ["test-user", sid, "HOST\\test-user"]} for row in tasks],
              "observer_sid": sid, "observer_is_admin": False,
              "tasks": [{key: value for key, value in row.items() if key != "xml_reference"} for row in tasks]}
    execution = write("scheduler-execution.json", record)
    selected = dict(case.before, native=case.new, runtime=case.new,
                    observed_at=(now + timedelta(seconds=10, microseconds=900000)).isoformat(),
                    qualified_at=(now + timedelta(seconds=11, microseconds=500000)).isoformat(),
                    ownership={"phase": "owned-maintenance", "verified": True, "recovery_required": True},
                    tasks=tasks, execution=ref(execution))
    before = write("containers-before.json", case.normalized)
    after = write("containers-after.json", case.normalized)
    snapshot = write("selected-runtime-snapshot.json", {"containers": selected["containers"]})
    roots = case.packet["roots"]
    states = {key: {"before": {"root": roots[key], **case.old, "status": "", "tracking": case.old["sha"]},
                    "after": {"root": roots[key], **case.new, "status": "", "tracking": case.new["sha"]}}
              for key in ("native", "runtime")}
    git = write("git-observation.json", {"schema": "cre-runtime-maintenance-git-v1", **states})
    commands = []
    for file in sorted(output.glob("[0-9][0-9]-*.json")):
        commands.append({"name": file.stem[3:], "receipt": ref(file), "log": ref(file.with_suffix(".log"))})
    deps = dict.fromkeys(("package.json", "package-lock.json", "node_modules/.package-lock.json"), "0" * 64)
    ready = {"schema": "cre-runtime-maintenance-ready-v1", "release_sha256": sha(case.release_raw),
             "authorization_sha256": sha(case.authorization.read_bytes()), "operation_output": str(output),
             "old": case.old, "new": case.new, "recovery_required_at_publication": True,
             "owned_authority": {"pid": os.getpid(), "token": "a" * 32, "generation": "b" * 32,
                                 "recovery_required": True, "authority_identity": [1, 2], "directory_identity": [1, 3]},
             "readiness": selected, "release_reference": ref(case.release), "mutation": ref(mutation),
             "initial_observation": ref(case.observation), "authorization": ref(case.authorization),
             "before_snapshot": ref(before), "after_snapshot": ref(after), "selected_snapshot": ref(snapshot),
             "git_observation": ref(git), "commands": commands, "dependencies_before": deps, "dependencies_after": deps}
    return case, ready, collector


def test_ready_reader_accepts_qualified_a_and_real_foreground_receipts(ready_payload_case):
    from cre_runtime_maintenance_transition import validate_maintenance_ready

    case, ready, _ = ready_payload_case
    validate_maintenance_ready(case.packet, case.evidence, ready, case.now + timedelta(seconds=23),
                               release_raw=case.release_raw, release_path=case.release)


@pytest.mark.parametrize("change", ["original_failure", "release", "command", "missing_command", "owner", "snapshot", "dependencies", "clock", "late_command"])
def test_ready_reader_rejects_forged_or_incomplete_evidence(ready_payload_case, change):
    from cre_code_handoff_evidence import HandoffError, json_bytes
    from cre_runtime_maintenance_transition import validate_maintenance_ready

    case, original, _ = ready_payload_case
    ready = copy.deepcopy(original)
    if change == "original_failure":
        role = case.packet["failed_handoff"]["failure"]
        case.inputs.data[case.path(role)] = b'{"classification":"committed"}'
    elif change == "release":
        ready["release_reference"]["sha256"] = "0" * 64
    elif change == "command":
        ref = ready["commands"][1]["receipt"]
        path = Path(ref["path"])
        receipt = json_bytes(path.read_bytes())
        receipt["argv"] = ["foreign-command"]
        raw = handoff.encoded(receipt)
        path.write_bytes(raw)
        ref.update(bytes=len(raw), sha256=sha(raw))
    elif change == "missing_command":
        ready["commands"].pop(2)
    elif change == "owner":
        ready["owned_authority"]["pid"] += 1
    elif change == "snapshot":
        ref = ready["after_snapshot"]
        path = Path(ref["path"])
        rows = json_bytes(path.read_bytes())
        rows[0]["image"] = "sha256:foreign"
        raw = handoff.encoded(rows)
        path.write_bytes(raw)
        ref.update(bytes=len(raw), sha256=sha(raw))
    elif change == "dependencies":
        ready["dependencies_after"]["package-lock.json"] = "bad"
    elif change == "late_command":
        ref = ready["commands"][-1]["receipt"]
        path = Path(ref["path"])
        receipt = json_bytes(path.read_bytes())
        receipt["finished_at"] = (case.now + timedelta(seconds=50)).isoformat()
        raw = handoff.encoded(receipt)
        path.write_bytes(raw)
        ref.update(bytes=len(raw), sha256=sha(raw))
    else:
        ready["readiness"]["qualified_at"] = (case.now + timedelta(hours=1)).isoformat()
    with pytest.raises(HandoffError):
        validate_maintenance_ready(case.packet, case.evidence, ready, case.now + timedelta(seconds=23),
                                   release_raw=case.release_raw, release_path=case.release)


@pytest.fixture
def transition_case(ready_payload_case, tmp_path):
    from cre_code_handoff_evidence import json_bytes
    from cre_runtime_maintenance_transition import make_maintenance_transition

    case, ready, _ = ready_payload_case
    output, now = case.output, case.now
    independent = tmp_path / "independent"
    independent.mkdir()
    def write(path, value):
        path.write_bytes(value if isinstance(value, bytes) else handoff.encoded(value))
        return path
    def ref(path):
        raw = path.read_bytes()
        return {"path": str(path), "sha256": sha(raw), "bytes": len(raw)}
    for row in case.packet["task_baseline"]:
        write(output / (row["key"] + "-before.xml"), case.inputs.data[case.path(row["xml"])])
    ready_path = write(output / "maintenance-ready.json", ready)
    started = write(output / "release-start.json", {"schema": "cre-runtime-maintenance-release-start-v1",
        "release_sha256": sha(case.release_raw), "authorization_sha256": sha(case.authorization.read_bytes()),
        "operation_output": str(output), "ready_sha256": sha(ready_path.read_bytes()),
        "owned_authority": ready["owned_authority"], "started_at": (now + timedelta(seconds=24)).isoformat()})
    owner = ready["owned_authority"]
    released = [[owner["pid"], owner["token"], "z" * 43, False], owner["authority_identity"]]
    terminal = write(output / "terminal.json", {"schema": "cre-runtime-maintenance-terminal-v1", "status": "committed",
        "release_sha256": sha(case.release_raw), "authorization_sha256": sha(case.authorization.read_bytes()),
        "operation_output": str(output), "ready_sha256": sha(ready_path.read_bytes()),
        "release_start_sha256": sha(started.read_bytes()), "canonical_lease_released": True,
        "released_authority": released, "finished_at": (now + timedelta(seconds=25)).isoformat()})
    stdout = write(Path(str(output) + ".stdout.raw"), b"synthetic native output")
    stderr = write(Path(str(output) + ".stderr.raw"), b"")
    completion = write(Path(str(output) + ".native.json"), {"schema": "cre-runtime-maintenance-native-completion-v1",
        "release_sha256": sha(case.release_raw), "authorization_sha256": sha(case.authorization.read_bytes()),
        "operation_output": str(output), "stdout_sha256": sha(stdout.read_bytes()), "stderr_sha256": sha(stderr.read_bytes()),
        "capture": {"Started": True, "Failed": False, "NativeExitCode": 0,
                    "Stdout": {"Eof": True, "Truncated": False}, "Stderr": {"Eof": True, "Truncated": False}}})
    observed = copy.deepcopy(ready["readiness"])
    for row in observed["tasks"]:
        target = write(independent / (row["key"] + "-observed.xml"), Path(row["xml_reference"]["path"]).read_bytes())
        row["xml_reference"] = ref(target)
    execution = json_bytes(Path(observed["execution"]["path"]).read_bytes())
    execution["started_at"] = (now + timedelta(seconds=26)).isoformat()
    execution["finished_at"] = (now + timedelta(seconds=27)).isoformat()
    execution["tasks"] = [{key: value for key, value in row.items() if key != "xml_reference"} for row in observed["tasks"]]
    execution_path = write(independent / "scheduler-execution.json", execution)
    container_snapshot = write(independent / "container-snapshot.json", case.normalized)
    observed.update(ownership={"phase": "released-clear", "verified": True, "recovery_required": False},
                    observed_at=(now + timedelta(seconds=28)).isoformat(), qualified_at=(now + timedelta(seconds=29)).isoformat(),
                    execution=ref(execution_path), terminal_sha256=sha(terminal.read_bytes()),
                    native_completion_sha256=sha(completion.read_bytes()), released_authority=released,
                    operation_output=str(output), container_snapshot=ref(container_snapshot))
    write(independent / "observation.json", observed)
    transition = make_maintenance_transition(case.release, output, independent, case.evidence.read)
    return case, transition


def test_original_failed_a_then_maintenance_is_authenticated(transition_case):
    from cre_runtime_maintenance_transition import validate_maintenance_transition

    case, transition = transition_case
    validated = validate_maintenance_transition(transition, case.evidence.read)
    assert validated["old"] == case.old and validated["new"] == case.new


def test_transition_accepts_equivalent_windows_and_wsl_root_names(transition_case):
    from cre_runtime_maintenance_transition import validate_maintenance_transition

    case, transition = transition_case
    transition["roots"]["tooling"] = "/mnt/c/dev"
    validate_maintenance_transition(transition, case.evidence.read)


@pytest.mark.parametrize("change", ["failure", "release_start", "terminal", "native_eof", "independent", "independent_false_type", "independent_inode_type", "chronology", "xml", "replayed_terminal"])
def test_transition_refuses_forged_or_incomplete_real_boundary(transition_case, change):
    from cre_code_handoff_evidence import HandoffError, json_bytes
    from cre_runtime_maintenance_transition import validate_maintenance_transition

    case, original = transition_case
    transition = copy.deepcopy(original)
    if change == "failure":
        role = case.packet["failed_handoff"]["failure"]
        case.inputs.data[case.path(role)] = b'{"classification":"committed"}'
    elif change == "xml":
        ref = transition["tasks"][0]["independent_xml"]
        path = Path(ref["path"])
        raw = path.read_bytes().replace(b"pwsh.exe", b"foreign.exe")
        path.write_bytes(raw)
        ref.update(bytes=len(raw), sha256=sha(raw))
    elif change == "replayed_terminal":
        ref = transition["terminal"]
        original_path = Path(ref["path"])
        copied = original_path.parent.parent / "copied-terminal.json"
        copied.write_bytes(original_path.read_bytes())
        ref["path"] = str(copied)
    else:
        field = {"release_start": "release_start", "terminal": "terminal", "native_eof": "native_completion",
                 "independent": "independent_observation", "independent_false_type": "independent_observation",
                 "independent_inode_type": "independent_observation", "chronology": "independent_observation"}[change]
        ref = transition[field]
        path = Path(ref["path"])
        value = json_bytes(path.read_bytes())
        if change == "release_start": value["owned_authority"]["pid"] += 1
        elif change == "terminal": value["status"] = "failed"
        elif change == "native_eof": value["capture"]["Stdout"]["Eof"] = False
        elif change == "independent_false_type": value["released_authority"][0][3] = 0
        elif change == "independent_inode_type": value["released_authority"][1][0] = bool(value["released_authority"][1][0])
        elif change == "chronology": value["observed_at"] = (case.now + timedelta(seconds=1)).isoformat()
        else: value["released_authority"][0][0] += 1
        raw = handoff.encoded(value)
        path.write_bytes(raw)
        ref.update(bytes=len(raw), sha256=sha(raw))
    with pytest.raises(HandoffError):
        validate_maintenance_transition(transition, case.evidence.read)


def test_postterminal_container_configuration_drift_refuses_transition(transition_case):
    from cre_code_handoff_evidence import HandoffError, json_bytes
    from cre_runtime_maintenance_transition import validate_maintenance_transition

    case, transition = transition_case
    observation_ref = transition["independent_observation"]
    observation_path = Path(observation_ref["path"])
    observation = json_bytes(observation_path.read_bytes())
    snapshot_ref = observation["container_snapshot"]
    snapshot_path = Path(snapshot_ref["path"])
    rows = json_bytes(snapshot_path.read_bytes())
    rows[0]["config_sha256"] = "f" * 64
    raw = handoff.encoded(rows)
    snapshot_path.write_bytes(raw)
    snapshot_ref.update(bytes=len(raw), sha256=sha(raw))
    raw = handoff.encoded(observation)
    observation_path.write_bytes(raw)
    observation_ref.update(bytes=len(raw), sha256=sha(raw))
    with pytest.raises(HandoffError):
        validate_maintenance_transition(transition, case.evidence.read)


def restored_packet(case, transition):
    packet = dict(case.packet, schema="cre-code-restoration-v1", scope="restore-held-task-states", code=case.new)
    packet["programs"] = {key: case.packet["programs"][key] for key in ("native_git", "powershell")}
    packet["task_baseline"] = copy.deepcopy(case.packet["task_baseline"])
    rows = {row["key"]: row for row in transition["tasks"]}
    for baseline in packet["task_baseline"]:
        row = rows[baseline["key"]]
        baseline["xml"] = case.put("post-maintenance-" + baseline["key"], Path(row["final_xml"]["path"]).read_bytes())
        baseline["actions"] = row["after_tuple"]["actions"]
    packet["maintenance_transition"] = case.put("actual-synthetic-transition", transition)
    packet["inputs"] = case.inputs.rows
    return packet


def test_restoration_consumes_actual_synthetic_maintenance_transition(transition_case):
    from cre_code_handoff_evidence import Evidence

    case, transition = transition_case
    packet = restored_packet(case, transition)
    validate_restoration(packet, Evidence(packet, case.evidence.read))


def test_reconciliation_carries_real_maintenance_lineage_for_future_release(transition_case):
    from cre_runtime_maintenance_transition import validate_maintenance_transition

    case, transition = transition_case
    retained = {}
    validate_maintenance_transition(transition, case.evidence.read, retain=retained)
    case.inputs.data.update(retained)
    packet = restored_packet(case, transition)
    _, receipt, _ = restoration.synthetic_reconciliation((packet, None, case.inputs, case.put, case.path))
    assert receipt["old"] != receipt["new"] == case.new


@pytest.mark.parametrize("drift", [False, True])
def test_postreadback_publishes_only_after_real_transition_validation(transition_case, tmp_path, monkeypatch, drift):
    import cre_runtime_maintenance as maintenance
    from cre_code_handoff_evidence import HandoffError, json_bytes

    case, original = transition_case
    original_observation = json_bytes(Path(original["independent_observation"]["path"]).read_bytes())
    observation_output = tmp_path / "fresh-independent"
    args = SimpleNamespace(operation=str(case.output), output=str(observation_output), release=str(case.release))
    calls = []
    samples = []
    monkeypatch.setattr(maintenance, "read_file", case.evidence.read)
    monkeypatch.setattr(operation, "predecessor_clear", lambda *args: calls.append("predecessor-clear"))
    monkeypatch.setattr(operation, "now", lambda: case.now + timedelta(seconds=29))
    host = dict(case.before, native=case.new, runtime=case.new,
                ownership={"phase": "released-clear", "verified": True, "recovery_required": False})
    monkeypatch.setattr(maintenance, "maintenance_readiness", lambda *args, **kwargs: host)
    def containers(*args):
        samples.append(True)
        rows = copy.deepcopy(case.normalized)
        if drift and len(samples) == 2:
            rows[0]["config_sha256"] = "f" * 64
        return rows
    monkeypatch.setattr(maintenance, "postreadback_containers", containers, raising=False)
    def native(self, mode, *args, **kwargs):
        assert mode == "Postreadback"
        tasks = copy.deepcopy(original_observation["tasks"])
        for row in tasks:
            source = Path(row["xml_reference"]["path"])
            target = observation_output / (row["key"] + "-observed.xml")
            target.write_bytes(source.read_bytes())
            row["xml_reference"] = {"path": str(target), "sha256": sha(target.read_bytes()), "bytes": target.stat().st_size}
        native_record = {"observed_at": original_observation["observed_at"], "tasks": tasks,
                         "execution": original_observation["execution"], "windows_resources": host["windows_resources"]}
        (observation_output / "native-observation.json").write_bytes(handoff.encoded(native_record))
    monkeypatch.setattr(operation.Foreground, "native", native)
    checkpoint = SimpleNamespace(canonical_shared_lock_dir=lambda _: tmp_path / "lock")
    if drift:
        with pytest.raises(HandoffError):
            maintenance.postreadback_maintenance(args, case.release_raw, case.packet, case.evidence, object(),
                                                 (checkpoint, None, None, None))
        assert len(samples) == 2 and not (observation_output / "transition.json").exists()
        return
    maintenance.postreadback_maintenance(args, case.release_raw, case.packet, case.evidence, object(),
                                         (checkpoint, None, None, None))
    assert len(samples) == 2
    assert calls == ["predecessor-clear", "predecessor-clear"]
    result = json_bytes((observation_output / "transition.json").read_bytes())
    assert result["old"] == case.old and result["new"] == case.new
