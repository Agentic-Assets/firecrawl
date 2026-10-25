"""Explicit synthetic producer-shaped restoration cases; never altered A data."""

import copy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_cre_code_handoff as fixtures

import cre_code_handoff as operation
from cre_code_restoration_evidence import (
    validate_failed_handoff,
    validate_restoration,
    validate_restoration_authorization,
    validate_restoration_enable_boundary,
    validate_restoration_observation,
)
from cre_code_handoff_evidence import (
    DEPENDENCY_CHECKS,
    HandoffError,
    enabled_xml_contract,
    json_bytes,
    sha,
    validate_lineage,
)
from cre_code_handoff_transition import make_reconciliation, validate_reconciliation

admitted = fixtures.admitted
complete_case = fixtures.complete_case
lineage_case = fixtures.lineage_case
NOW, OLD, NEW = fixtures.NOW, fixtures.OLD, fixtures.NEW


def test_enabled_xml_contract_normalizes_only_enabled_position():
    # Actual A export shape: final omits Enabled; held places it before time limit.
    before = (b'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Settings>'
              b'<AllowHardTerminate>false</AllowHardTerminate><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>'
              b'<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy></Settings></Task>')
    held = before.replace(b"<ExecutionTimeLimit>", b"<Enabled>false</Enabled><ExecutionTimeLimit>")
    assert enabled_xml_contract(before, False) == enabled_xml_contract(held, False)
    attributed = held.replace(b"<Enabled>", b'<Enabled unexpected="true">')
    assert enabled_xml_contract(before, False) != enabled_xml_contract(attributed, False)
    reordered = held.replace(b"<AllowHardTerminate>false</AllowHardTerminate>", b"").replace(
        b"</ExecutionTimeLimit>", b"</ExecutionTimeLimit><AllowHardTerminate>false</AllowHardTerminate>")
    assert enabled_xml_contract(before, False) != enabled_xml_contract(reordered, False)


@pytest.fixture
def restoration_case(complete_case, lineage_case):
    source, _, _, inputs = complete_case
    source = copy.deepcopy(source)
    # Both fixtures use the same admitted fleet; preserve the original failed tuple.
    source["task_baseline"] = copy.deepcopy(lineage_case[0]["task_baseline"])
    source["inputs"] = copy.deepcopy(inputs.rows)

    def put(name, value):
        role = inputs.add("restoration-" + name, value)
        path = "/synthetic/" + role
        inputs.rows[-1]["path"] = path
        inputs.data[path] = inputs.data[role]
        return role

    def path(role):
        return next(row["path"] for row in inputs.rows if row["role"] == role)

    def raw(role):
        return inputs.data[path(role)]

    failed = {}
    failed["release"] = put("failed-release", source)
    failed["observation"] = put("failed-observation", {"historical": "original producer bytes"})
    output = "/synthetic/failed-operation"
    digest = sha(raw(failed["release"]))
    auth = {"schema": "cre-code-authorization-v1", "release_sha256": digest,
            "observation_sha256": sha(raw(failed["observation"])), "operation_output": output,
            "authorized_at": (NOW - timedelta(seconds=40)).isoformat(),
            "expires_at": (NOW + timedelta(seconds=259)).isoformat()}
    failed["authorization"] = put("failed-authorization", auth)
    auth_digest = sha(raw(failed["authorization"]))
    failed["mutation"] = put("failed-mutation", {
        "schema": "cre-code-mutation-v1", "release_sha256": digest, "authorization_sha256": auth_digest,
        "operation_output": output, "tooling": source["tooling"], "fleet": source["task_baseline"],
        "entered_at": (NOW - timedelta(seconds=19)).isoformat(),
    })
    rows, current, audit_tasks, pins, xml_rows = [], [], [], [], []
    for baseline in source["task_baseline"]:
        key = baseline["key"]
        before = inputs.data[baseline["xml"]]
        final = before.replace(OLD["sha"].encode(), NEW["sha"].encode())
        held = final.replace(b"<Enabled>true</Enabled>", b"<Enabled>false</Enabled>")
        before_role, final_role, held_role = (put(key + "-" + name, data) for name, data in
                                              (("before", before), ("final", final), ("held", held)))
        xml_rows.append({"key": key, "before": before_role, "final": final_role, "audited": held_role})
        actions = [{field: value.replace(OLD["sha"], NEW["sha"]) for field, value in action.items()}
                   for action in baseline["actions"]]
        rows.append(dict(baseline, actions=actions, state="Ready", xml_sha256=sha(final)))
        current.append(dict(baseline, actions=actions, enabled=False, desired_enabled=baseline["enabled"],
                            missed_runs=1 if key == "qualified" else 0, xml=held_role,
                            next_run_utc=(NOW + timedelta(days=1)).isoformat() if key == "qualified" else baseline["next_run_utc"]))
        audit_tasks.append({"key": key, "enabled": False, "state": "Disabled", "last_result": baseline["last_task_result"],
                                "last_run_utc": baseline["last_run_utc"], "next_run_utc": baseline["next_run_utc"], "missed_runs": 0})
        pins.append({"key": key, "xml_sha256": sha(held), "xml_path": path(held_role), "expected_sha_values": [NEW["sha"]]})
    readiness = dict(fixtures.host_observation(), schema="cre-code-observation-v1", native=NEW, runtime=NEW,
                     tasks=rows, observed_at=(NOW - timedelta(seconds=4)).isoformat(),
                     qualified_at=(NOW - timedelta(seconds=3)).isoformat(), resources_ok=True,
                     queues_idle=True, ownership={"phase": "owned-maintenance", "verified": True, "recovery_required": True})
    dependencies = dict.fromkeys(("package.json", "package-lock.json", "node_modules/.package-lock.json"), "0" * 64)
    failed["commit_ready"] = put("failed-ready", {"schema": "cre-code-commit-ready-v1", "selected": NEW,
        "release_sha256": digest, "authorization_sha256": auth_digest, "operation_output": output,
        "readiness": readiness, "dependencies": dependencies, "recovery_required_at_publication": True})
    failed["held_ready"] = put("failed-held", {"schema": "cre-code-held-ready-v1", "tasks_held": True,
        "release_sha256": digest, "dependencies": dependencies})
    failed["failure"] = put("failed-error", {"schema": "cre-code-failure-v1", "classification": "commit-uncertain",
        "original_error_type": "HandoffError", "original_error": "canonical owner/recovery not clear"})
    failed["outer_failure"] = put("failed-outer", {"schema": "cre-code-outer-failure-v1", "release_sha256": digest,
                                                       "operation_output": output})
    commands = []
    for index, name in enumerate(("hold", "npm-ls", "doctor", "gfm", "repin", "restore", "verify")):
        log = put("command-" + name + "-log", b"synthetic actual producer stream model\n")
        receipt = {"producer": "checkpoint-run-command-v1", "exit_code": 0, "local_settlement": "confirmed", "log_closed": True,
                       "started_at": (NOW - timedelta(seconds=20 - index * 2)).isoformat(),
                       "finished_at": (NOW - timedelta(seconds=18 - index * 2)).isoformat(),
                       "command_log_sha256": sha(raw(log))}
        if name in {"hold", "repin", "restore", "verify"}:
            receipt.update(schema="cre-code-command-v1", argv=[source["programs"]["powershell"], "-NoProfile", "-NonInteractive",
                "-File", "C:/dev/scripts/firecrawl-ops/cre_collector/windows/Invoke-CreCodeHandoff.ps1",
                "-Mode", name.capitalize(), "-ReleasePath", path(failed["release"]), "-ReleaseSha", digest,
                "-Output", output, "-OwnerPid", "42", "-Observation", path(failed["observation"]),
                "-Authorization", path(failed["authorization"])])
        else:
            cwd = source["roots"]["runtime"] + "/scripts/firecrawl-ops/cre_collector"
            receipt.update(schema="cre-owned-gate-v1", identity=NEW, unchanged=True, effective_cwd=cwd, cwd=cwd,
                           argv=["bash", "-c", 'cd -- "$1" && shift && exec "$@"', "cre-handoff", cwd, *DEPENDENCY_CHECKS[name]])
        commands.append({"name": name, "log": log, "receipt": put("command-" + name, receipt)})
    failed["commands"] = commands
    failed["native_steps"] = [{"mode": mode, "receipt": put("step-" + mode, {
        "status": "passed", "mode": mode, "owner_pid": 42, "release_sha256": digest})} for mode in ("Hold", "Repin", "Restore")]
    failed["xml"] = xml_rows
    failed["native_git"] = put("native-git", [dict(role="native", root=source["roots"]["native"], status="", **NEW)])
    report = {"tasks": audit_tasks, "task_pins": pins, "canonical_directory_exists": False, "quarantine_guard_exists": False,
                  "actual_authority_recovery_required": False, "authority_owner_pid": 42, "authority_owner_pid_exists": False,
                  "authority_nonblocking_flock_available": True, "authority_probe_released": True, "authority_bytes_unchanged": True,
                  "current_com_instances": [], "relevant_current_uid_frontends": [], "mutations_performed": [], "pending_handles": [],
                  "current_linux_git": dict(NEW, status=""),
                  "current_windows_observed_at": (NOW - timedelta(seconds=2)).isoformat(),
                  "current_linux_observed_at": (NOW - timedelta(seconds=1)).isoformat(),
                  "saved_failed_operation": {"terminal_exists": False, "native_completion_exists": False, "original_exit": 1}}
    failed["settlement_report"] = put("audit-report", report)
    sealed = [failed["settlement_report"], failed["native_git"], *(row["audited"] for row in xml_rows)]
    failed["settlement_seal"] = put("audit-seal", [{"path": path(role), "bytes": len(raw(role)), "sha256": sha(raw(role))} for role in sealed])
    packet = {field: copy.deepcopy(source[field]) for field in
              ("tooling", "development", "roots", "programs", "wsl", "preserve_files", "runtime_env_file", "qa")}
    packet.update(schema="cre-code-restoration-v1", scope="restore-held-task-states", code=NEW,
                  task_baseline=current, failed_handoff=failed)
    return packet, inputs.evidence(packet), inputs, put, path


def test_complete_synthetic_failed_attempt_and_restoration_packet_join(restoration_case):
    packet, evidence, _, _, _ = restoration_case
    validate_restoration(packet, evidence)
    source, _, _, _ = validate_failed_handoff(packet, evidence)
    assert source["task_baseline"][2]["next_run_utc"] != packet["task_baseline"][2]["next_run_utc"]
    assert source["task_baseline"][2]["last_task_result"] == packet["task_baseline"][2]["last_task_result"] == 1


@pytest.mark.parametrize("change", ["code", "flags", "result", "last", "xml", "duplicate", "ready", "failure", "command", "audit", "seal"])
def test_restoration_packet_rejects_substitution_or_missing_settlement(restoration_case, change):
    packet, _, inputs, _, path = restoration_case
    if change == "code":
        packet["code"] = OLD
    elif change == "flags":
        packet["task_baseline"][2]["desired_enabled"] = False
    elif change == "result":
        packet["task_baseline"][2]["last_task_result"] = 0
    elif change == "last":
        packet["task_baseline"][2]["last_run_utc"] = NOW.isoformat()
    elif change == "duplicate":
        packet["task_baseline"].append(copy.deepcopy(packet["task_baseline"][0]))
    else:
        failed = packet["failed_handoff"]
        role = (packet["task_baseline"][0]["xml"] if change == "xml" else
                failed["commands"][0]["receipt"] if change == "command" else
                failed[{"ready": "commit_ready", "failure": "failure", "audit": "settlement_report", "seal": "settlement_seal"}[change]])
        target = path(role)
        raw = inputs.data[target]
        if change == "xml":
            raw = raw.replace(b"pwsh.exe", b"foreign.exe")
        else:
            value = json_bytes(raw)
            if change == "ready": value["recovery_required_at_publication"] = False
            elif change == "failure": value["classification"] = "committed"
            elif change == "command": value["local_settlement"] = "unknown"
            elif change == "audit": value["current_com_instances"] = [{"instance": "still-running"}]
            else: value[0]["sha256"] = "0" * 64
            raw = fixtures.encoded(value)
        inputs.data[target] = raw
        next(row for row in inputs.rows if row["role"] == role).update(bytes=len(raw), sha256=sha(raw))
        if change == "audit":
            seal_role = failed["settlement_seal"]
            seal_path = path(seal_role)
            seal = json_bytes(inputs.data[seal_path])
            next(row for row in seal if row["path"] == target).update(bytes=len(raw), sha256=sha(raw))
            inputs.data[seal_path] = fixtures.encoded(seal)
            next(row for row in inputs.rows if row["role"] == seal_role).update(
                bytes=len(inputs.data[seal_path]), sha256=sha(inputs.data[seal_path]))
    with pytest.raises(HandoffError):
        validate_restoration(packet, inputs.evidence(packet))


def held_observation(packet, inputs):
    return dict(fixtures.host_observation(), schema="cre-code-restoration-observation-v1", native=NEW, runtime=NEW,
                restoration_sha256=sha(fixtures.encoded(packet)),
                observed_at=NOW.isoformat(), qualified_at=NOW.isoformat(), resources_ok=True, queues_idle=True,
                ownership={"phase": "released-clear", "verified": True, "recovery_required": False},
                tasks=[dict(row, state="Disabled", xml_sha256=sha(inputs.data[next(
                    item["path"] for item in inputs.rows if item["role"] == row["xml"])])) for row in packet["task_baseline"]])


@pytest.mark.parametrize("change", [None, "next", "missed", "enabled", "owner", "stale"])
def test_fresh_held_clock_and_missed_count_are_independent_exact_facts(restoration_case, change):
    packet, evidence, inputs, _, _ = restoration_case
    observed = held_observation(packet, inputs)
    if change == "next": observed["tasks"][2]["next_run_utc"] = NOW.isoformat()
    elif change == "missed": observed["tasks"][2]["missed_runs"] = 0
    elif change == "enabled": observed["tasks"][2]["enabled"] = True
    elif change == "owner": observed["ownership"]["recovery_required"] = True
    elif change == "stale": observed["observed_at"] = (NOW - timedelta(seconds=300)).isoformat()
    if change is None:
        validate_restoration_observation(packet, evidence, observed, NOW)
    else:
        with pytest.raises(HandoffError): validate_restoration_observation(packet, evidence, observed, NOW)


def test_unsupported_catchup_refuses_before_any_apply_lease_or_task_call(restoration_case, tmp_path, monkeypatch):
    packet, evidence, inputs, _, _ = restoration_case
    raw = fixtures.encoded(packet)
    observed = fixtures.encoded(held_observation(packet, inputs))
    args = SimpleNamespace(release=tmp_path / "packet.json", observation=tmp_path / "observe.json",
                           authorization=tmp_path / "auth.json", output=tmp_path / "never-created")
    args.release.write_bytes(raw); args.observation.write_bytes(observed)
    args.authorization.write_bytes(fixtures.encoded({"operation_output": str(args.output)}))
    calls = []
    monkeypatch.setattr(operation, "validate_restoration_authorization", lambda *args, **kwargs: calls.append("input-auth"))
    with pytest.raises(HandoffError, match="catch-up admission unsupported"):
        operation.apply_restoration(args, raw, packet, evidence, object(), (object(), None, None, None))
    assert calls == ["input-auth"] and not args.output.exists()


@pytest.mark.parametrize("change", [None, "packet", "scope", "tooling", "expired", "capture"])
def test_restoration_authorization_binds_distinct_subject_and_fresh_held_facts(restoration_case, change):
    packet, evidence, inputs, _, _ = restoration_case
    packet_raw = fixtures.encoded(packet)
    observation_raw = fixtures.encoded(held_observation(packet, inputs))
    body = b"independent synthetic restoration review; no actual Enable approval"
    attestation = fixtures.attestation({"restoration_sha256": sha(packet_raw), "tooling": NEW},
                                       "/synthetic/review-body", body, "restore-held-task-states")
    if change == "scope": attestation["scope"] = "code-only-release"
    elif change == "tooling": attestation["subject"]["tooling"] = OLD
    attestation_raw = fixtures.encoded(attestation)
    reviewed = {"/synthetic/review": attestation_raw, "/synthetic/review-body": body}
    authorization = {"schema": "cre-code-restoration-authorization-v1", "scope": "restore-held-task-states",
        "restoration_sha256": sha(packet_raw), "observation_sha256": sha(observation_raw),
        "authorized_at": NOW.isoformat(), "expires_at": (NOW + timedelta(seconds=299)).isoformat(),
        "review": {"path": "/synthetic/review", "sha256": sha(attestation_raw), "reviewer": "independent", "approval_ref": "AGENTIC-test"}}
    if change == "packet": authorization["restoration_sha256"] = "0" * 64
    elif change == "expired": authorization["expires_at"] = NOW.isoformat()
    elif change == "capture": authorization["observation_sha256"] = "0" * 64
    if change is None:
        validate_restoration_authorization(packet_raw, packet, evidence, observation_raw, authorization, NOW,
                                           review_reader=reviewed.__getitem__)
    else:
        with pytest.raises(HandoffError):
            validate_restoration_authorization(packet_raw, packet, evidence, observation_raw, authorization, NOW,
                                               review_reader=reviewed.__getitem__)


def test_actual_restoration_preview_reads_complete_packet_without_host_dispatch(restoration_case, monkeypatch, capsys):
    packet, _, inputs, _, _ = restoration_case
    inputs.data["/synthetic/packet.json"] = fixtures.encoded(packet)
    monkeypatch.setattr(operation, "read_file", lambda path: inputs.data[str(path)])

    def forbidden(*args, **kwargs):
        raise AssertionError("offline preview dispatched a host adapter")

    monkeypatch.setattr(operation, "Git", forbidden)
    monkeypatch.setattr(operation, "runtime_modules", forbidden)
    assert operation.main(["--restoration-packet", "/synthetic/packet.json"]) == 0
    output = json_bytes(capsys.readouterr().out.encode())
    assert output["preview_only"] is True and output["execution"] is False and output["code"] == NEW


@pytest.mark.parametrize("change", [None, "enabled-xml", "action", "margin", "missed", "result"])
def test_final_readback_qualifies_original_flags_and_fresh_history(restoration_case, change):
    packet, evidence, inputs, put, path = restoration_case
    observed = held_observation(packet, inputs)
    for task in observed["tasks"]:
        task.update(enabled=True, state="Ready")
        raw = inputs.data[path(task["xml"])].replace(b"<Enabled>false</Enabled>", b"<Enabled>true</Enabled>")
        if change == "enabled-xml" and task["key"] == "qualified": raw = raw.replace(b"<Enabled>true", b"<Enabled>false")
        if change == "action" and task["key"] == "qualified": raw = raw.replace(b"pwsh.exe", b"foreign.exe")
        role = put("observed-final-" + task["key"], raw)
        task.update(xml_sha256=sha(raw), xml_reference={"path": path(role), "bytes": len(raw), "sha256": sha(raw)})
    if change == "margin":
        packet["task_baseline"][2]["next_run_utc"] = (NOW + timedelta(seconds=899)).isoformat()
        observed["tasks"][2]["next_run_utc"] = packet["task_baseline"][2]["next_run_utc"]
    elif change == "missed": observed["tasks"][2]["missed_runs"] += 1
    elif change == "result": observed["tasks"][2]["last_task_result"] = 0
    if change is None:
        validate_restoration_observation(packet, evidence, observed, NOW, final=True)
    else:
        with pytest.raises(HandoffError): validate_restoration_observation(packet, evidence, observed, NOW, final=True)


def zero_missed_packet(case):
    packet, _, inputs, _, path = case
    source = json_bytes(inputs.data[path(packet["failed_handoff"]["release"])])
    original = {row["key"]: row for row in source["task_baseline"]}
    packet["enable_boundary"] = "zero-missed-no-crossed-trigger-v1"
    for row in packet["task_baseline"]:
        row.update(missed_runs=0, next_run_utc=original[row["key"]]["next_run_utc"])
    return packet, inputs.evidence(packet), inputs


def test_supported_zero_missed_case_requires_original_trigger_still_in_future(restoration_case):
    packet, evidence, _ = zero_missed_packet(restoration_case)
    validate_restoration_enable_boundary(packet, evidence, NOW)
    with pytest.raises(HandoffError, match="crossed or inside"):
        validate_restoration_enable_boundary(packet, evidence, NOW + timedelta(hours=2))
    packet["task_baseline"][2]["missed_runs"] = 1
    with pytest.raises(HandoffError, match="missed or crossed"):
        validate_restoration_enable_boundary(packet, evidence, NOW)


@pytest.mark.parametrize("fault", [None, "pre-lease", "first-enable", "expired-after-enable", "restoration-ready",
                                   "release-start-publication", "clear-recovery", "lease-exit",
                                   "released-observation", "terminal-publication"])
def test_actual_apply_frontdoor_supported_case_and_own_marker_failure_hold(restoration_case, tmp_path, monkeypatch, fault):
    packet, _evidence, inputs = zero_missed_packet(restoration_case)
    packet_path, observed_path, auth_path = (tmp_path / name for name in ("packet.json", "observation.json", "auth.json"))
    output = tmp_path / "operation"
    packet_raw, observed_raw = fixtures.encoded(packet), fixtures.encoded(held_observation(packet, inputs))
    packet_path.write_bytes(packet_raw); observed_path.write_bytes(observed_raw)
    body_path, review_path = tmp_path / "review.md", tmp_path / "review.json"
    body_path.write_bytes(b"synthetic exact zero-missed restoration case review")
    review_path.write_bytes(fixtures.encoded(fixtures.attestation(
        {"restoration_sha256": sha(packet_raw), "tooling": NEW}, body_path, body_path.read_bytes(), "restore-held-task-states")))
    authorization = {"schema": "cre-code-restoration-authorization-v1", "scope": "restore-held-task-states",
        "restoration_sha256": sha(packet_raw), "observation_sha256": sha(observed_raw),
        "authorized_at": NOW.isoformat(), "expires_at": (NOW + timedelta(seconds=299)).isoformat(), "operation_output": str(output),
        "review": {"path": str(review_path), "sha256": sha(review_path.read_bytes()), "reviewer": "independent", "approval_ref": "AGENTIC-test"}}
    auth_raw = fixtures.encoded(authorization); auth_path.write_bytes(auth_raw)
    state = {"calls": [], "enabled": [False] * 3, "owned": False, "cleared": False}
    lock_path = tmp_path / "noncanonical.lock"
    released_receipt = object()

    class Lease:
        def __init__(self, path, *, recovery_required):
            assert path == lock_path and recovery_required is True
        def __enter__(self):
            assert not state["owned"]
            state["calls"].append("lease"); state["owned"] = True
            return self
        def clear_recovery_requirement(self):
            assert state["owned"] and (output / "restoration-ready.json").is_file()
            state["calls"].append("clear")
            if fault == "clear-recovery":
                raise OSError("synthetic clear-recovery failure")
            state["cleared"] = True
        def __exit__(self, *args):
            state["calls"].append("exit"); state["owned"] = False
            if fault == "lease-exit": raise OSError("synthetic lease-exit failure")

    checkpoint = SimpleNamespace(SharedLock=Lease, canonical_shared_lock_dir=lambda root: lock_path)
    class Git:
        def __init__(self, tool): assert tool == packet["programs"]["native_git"]
        def clean(self, root, identity, **kwargs):
            assert root == packet["roots"]["development"] and identity == NEW
        def command(self, root, *argv):
            assert root == packet["roots"]["development"] and argv == ("merge-base", "--is-ancestor", NEW["sha"], NEW["sha"])

    actual_read = operation.read_file
    monkeypatch.setattr(operation, "read_file", lambda path: inputs.data[str(path)] if str(path) in inputs.data else actual_read(path))
    monkeypatch.setattr(operation, "now", lambda: NOW)
    monkeypatch.setattr(operation, "Git", Git)
    monkeypatch.setattr(operation, "runtime_modules", lambda actual, git: (checkpoint, None, None, None))
    monkeypatch.setattr(operation, "preserved", lambda actual: {"explicit-synthetic-preservation": "0" * 64})
    monkeypatch.setattr(operation, "installed_dependencies", lambda root: dict.fromkeys(
        ("package.json", "package-lock.json", "node_modules/.package-lock.json"), "0" * 64))

    def ready(actual, *args, owned=False, **kwargs):
        assert owned is state["owned"] and actual["schema"] == "cre-code-restoration-v1"
        if fault == "pre-lease" and not owned: raise OSError("synthetic pre-lease failure")
        return dict(fixtures.host_observation(), native=NEW, runtime=NEW, resources_ok=True, queues_idle=True,
                    ownership={"phase": "owned-maintenance" if owned else "released-clear", "verified": True, "recovery_required": owned})

    def native(self, mode, *args):
        state["calls"].append(mode)
        assert mode in {"Restore", "Verify", "FailureHold"}
        if mode == "Restore":
            assert state["owned"]
            operation.write_new(output / "mutation-start.json", {"schema": "cre-code-restoration-mutation-v1",
                "restoration_sha256": sha(packet_raw), "authorization_sha256": sha(auth_raw), "operation_output": str(output),
                "tooling": packet["tooling"], "fleet": packet["task_baseline"]})
            for index in range(3):
                assert operation.mutation_started(output, packet, packet_raw, auth_raw)
                state["enabled"][index] = True
                if fault == "first-enable": raise OSError("synthetic first-enable receipt failure")
                if fault == "expired-after-enable":
                    monkeypatch.setattr(operation, "now", lambda: NOW + timedelta(seconds=300))
                    raise OSError("synthetic expired-after-enable failure")
        elif mode == "FailureHold":
            assert state["owned"] and not state["cleared"], "failure hold requires this operation's held lease"
            assert operation.mutation_started(output, packet, packet_raw, auth_raw)
            state["enabled"] = [False] * 3
        else:
            assert state["enabled"] == [True] * 3
            tasks = held_observation(packet, inputs)["tasks"]
            for task in tasks:
                raw = inputs.data[next(row["path"] for row in inputs.rows if row["role"] == task["xml"])].replace(
                    b"<Enabled>false</Enabled>", b"<Enabled>true</Enabled>")
                (output / (task["key"] + "-observed.xml")).write_bytes(raw)
                task.update(enabled=True, state="Ready", xml_sha256=sha(raw))
            operation.write_new(output / "native-observation.json", {"observed_at": NOW.isoformat(), "tasks": tasks,
                                                                        "windows_resources": fixtures.host_observation()["windows_resources"]})

    def captured(actual, lease):
        assert actual is checkpoint and state["owned"] and state["cleared"]
        return released_receipt

    def clear(actual, path, *, released):
        assert actual is checkpoint and path == lock_path and released is released_receipt
        assert not state["owned"] and state["cleared"]
        state["calls"].append("released")
        if fault == "released-observation":
            raise OSError("synthetic released-observation failure")

    original_write = operation.write_new

    def write(path, value):
        if fault == "terminal-publication" and path.name == "restoration-terminal.json":
            raise OSError("synthetic terminal-publication failure")
        if fault == "restoration-ready" and path.name == "restoration-ready.json":
            raise OSError("synthetic restoration-ready failure")
        if fault == "release-start-publication" and path.name == "release-start.json":
            raise OSError("synthetic release-start-publication failure")
        original_write(path, value)

    monkeypatch.setattr(operation, "readiness", ready)
    monkeypatch.setattr(operation.Foreground, "native", native)
    monkeypatch.setattr(operation, "released_authority", captured)
    monkeypatch.setattr(operation, "owned_authority", lambda actual, lease: {
        "pid": 123, "token": "synthetic-owned-token", "generation": "synthetic-generation",
        "recovery_required": True, "authority_identity": [1, 2], "directory_identity": [1, 3]})
    monkeypatch.setattr(operation, "lock_clear", clear)
    monkeypatch.setattr(operation, "write_new", write)
    argv = ["--restoration-packet", str(packet_path), "--apply", "--observation", str(observed_path),
            "--authorization", str(auth_path), "--output", str(output)]
    if fault:
        with pytest.raises(OSError, match="synthetic " + fault): operation.main(argv)
        if fault == "pre-lease":
            assert state["enabled"] == [False] * 3 and state["calls"] == []
        elif fault in {"first-enable", "expired-after-enable", "restoration-ready"}:
            assert state["enabled"] == [False] * 3
            assert state["calls"] == (["lease", "Restore", "Verify", "FailureHold", "exit"]
                                      if fault == "restoration-ready" else ["lease", "Restore", "FailureHold", "exit"])
        else:
            assert state["enabled"] == [True] * 3
            assert "FailureHold" not in state["calls"]
            assert not state["owned"]
        assert not (output / "restoration-terminal.json").exists()
    else:
        assert operation.main(argv) == 0
        assert state["calls"] == ["lease", "Restore", "Verify", "clear", "exit", "released"]
        terminal = json_bytes((output / "restoration-terminal.json").read_bytes())
        assert terminal["status"] == "restored" and terminal["canonical_lease_released"] is True


def synthetic_reconciliation(case):
    """Distinct synthetic restoration outputs, preserving the synthetic failed A."""
    packet, _, inputs = zero_missed_packet(case)
    raw = fixtures.encoded(packet)
    files = dict(inputs.data)
    packet_path = Path("/synthetic/restoration-packet.json")
    root, independent = Path("/synthetic/restored-operation"), Path("/synthetic/independent-restoration")
    files[str(packet_path)] = raw

    def write(path, value):
        files[str(path)] = value if isinstance(value, bytes) else fixtures.encoded(value)

    auth = "6" * 64
    common = {"restoration_sha256": sha(raw), "authorization_sha256": auth, "operation_output": str(root)}
    write(root / "mutation-start.json", dict(common, schema="cre-code-restoration-mutation-v1",
          tooling=packet["tooling"], fleet=packet["task_baseline"]))
    ready = held_observation(packet, inputs)
    ready["ownership"] = {"phase": "owned-maintenance", "verified": True, "recovery_required": True}
    for row in ready["tasks"]:
        before = inputs.data[next(item["path"] for item in inputs.rows if item["role"] == row["xml"])]
        final = before.replace(b"<Enabled>false</Enabled>", b"<Enabled>true</Enabled>")
        for path in (root / (row["key"] + "-final.xml"), root / (row["key"] + "-observed.xml"),
                     independent / (row["key"] + "-observed.xml")):
            write(path, final)
        row.update(enabled=True, state="Ready", xml_sha256=sha(final),
                   xml_reference={"path": str(root / (row["key"] + "-observed.xml")), "bytes": len(final), "sha256": sha(final)})
    write(root / "restoration-ready.json", dict(common, schema="cre-code-restoration-ready-v1", code=NEW,
          readiness=ready, recovery_required_at_publication=True,
          dependencies=dict.fromkeys(("package.json", "package-lock.json", "node_modules/.package-lock.json"), "0" * 64)))
    failed_source = next(row for row in packet["inputs"] if row["role"] == packet["failed_handoff"]["release"])
    write(root / "restoration-terminal.json", dict(common, schema="cre-code-restoration-terminal-v1", code=NEW,
          status="restored", canonical_lease_released=True, finished_at=(NOW + timedelta(seconds=1)).isoformat(),
          ready_sha256=sha(files[str(root / "restoration-ready.json")]), failed_predecessor_release_sha256=failed_source["sha256"]))
    write(str(root) + ".stdout.raw", b"synthetic real-stream-shaped completion"); write(str(root) + ".stderr.raw", b"")
    write(str(root) + ".native.json", dict(common, schema="cre-code-restoration-native-completion-v1",
          capture={"Started": True, "Failed": False, "NativeExitCode": 0,
                       "Stdout": {"Eof": True, "Truncated": False}, "Stderr": {"Eof": True, "Truncated": False}},
          stdout_sha256=sha(files[str(root) + ".stdout.raw"]), stderr_sha256=sha(b"")))
    observed = copy.deepcopy(ready)
    observed.update(common, observed_at=(NOW + timedelta(seconds=2)).isoformat(),
                    qualified_at=(NOW + timedelta(seconds=3)).isoformat(),
                    ownership={"phase": "released-clear", "verified": True, "recovery_required": False},
                    operation_terminal_sha256=sha(files[str(root / "restoration-terminal.json")]),
                    native_completion_sha256=sha(files[str(root) + ".native.json"]))
    write(independent / "observation.json", observed)
    def read(path):
        return files[str(path)]
    receipt = make_reconciliation(packet, packet_path, root, independent, read)
    validate_reconciliation(receipt, OLD, NEW, read)
    return packet, receipt, files


@pytest.mark.parametrize("new_result", [0, 1])
def test_future_release_does_not_attach_historical_failure_to_new_run(restoration_case, monkeypatch, new_result):
    """Isolate future-release policy after the finite reader authenticates an edge."""
    packet, receipt, files = synthetic_reconciliation(restoration_case)
    _, _, inputs, _, path = restoration_case
    inputs.data.update(files)
    source = json_bytes(inputs.data[path(packet["failed_handoff"]["release"])])
    release = copy.deepcopy(source)
    role = inputs.add("restoration-with-new-native-history", receipt)
    failure = release["task_baseline"][2]["failure"]
    failure["transitions"] = [{"old": OLD, "new": NEW, "readback": role}]
    rows = {row["key"]: copy.deepcopy(row) for row in receipt["tasks"]}
    rows["qualified"]["after_tuple"].update(last_task_result=new_result, last_run_utc=NOW.isoformat())
    release["task_baseline"][2].update(last_task_result=new_result, last_run_utc=NOW.isoformat())
    release["inputs"] = inputs.rows
    import cre_code_handoff_transition as transition

    monkeypatch.setattr(transition, "validate_reconciliation", lambda *args, **kwargs: rows)
    with pytest.raises(HandoffError, match="distinct current source settlement"):
        validate_lineage(release, inputs.evidence(release))
    assert failure["operation"] == source["task_baseline"][2]["failure"]["operation"]


@pytest.mark.parametrize("change", [None, "reordered", "xml", "new-history"])
def test_reconciliation_then_ordinary_delivery_feeds_next_release(restoration_case, tmp_path, change):
    packet, receipt, files = synthetic_reconciliation(restoration_case)
    _, _, inputs, _, path = restoration_case
    inputs.data.update(files)
    source = json_bytes(inputs.data[path(packet["failed_handoff"]["release"])])
    release = copy.deepcopy(source)
    role = inputs.add("explicit-failed-A-restoration", receipt)
    definitions = []
    failed_xml = {row["key"]: row for row in packet["failed_handoff"]["xml"]}
    for row in release["task_baseline"]:
        key = row["key"]
        before = inputs.add("reconciled-before-" + key, inputs.data[path(failed_xml[key]["before"])])
        final = inputs.add("reconciled-final-" + key, inputs.data[path(failed_xml[key]["final"])])
        definitions.append({"key": key, "before": before, "after": final})
        row.update(xml=final, actions=[{field: value.replace(OLD["sha"], NEW["sha"]) for field, value in action.items()}
                                       for action in row["actions"]])
    failed = release["task_baseline"][2]["failure"]
    failed["transitions"].append({"old": OLD, "new": NEW, "xml": definitions, "readback": role})
    following = {"sha": "e" * 40, "tree": "f" * 40}
    release.update(deployed=NEW, selected=following, inputs=inputs.rows)
    continued = fixtures.synthetic_transition_files(tmp_path / "ordinary", release, inputs)
    for file in (tmp_path / "ordinary").rglob("*"):
        if file.is_file(): inputs.data[str(file)] = file.read_bytes()
    continued_rows = []
    for row in release["task_baseline"]:
        key = row["key"]
        before_raw = inputs.data[row["xml"]]
        final_raw = before_raw.replace(NEW["sha"].encode(), following["sha"].encode())
        before, final = inputs.add("ordinary-before-" + key, before_raw), inputs.add("ordinary-final-" + key, final_raw)
        continued_rows.append({"key": key, "before": before, "after": final})
        row.update(xml=final, actions=[{field: value.replace(NEW["sha"], following["sha"]) for field, value in action.items()}
                                       for action in row["actions"]])
    failed["transitions"].append({"old": NEW, "new": following, "xml": continued_rows,
                                       "readback": inputs.add("ordinary-after-reconciliation", continued)})
    release["deployed"] = following
    if change == "reordered": failed["transitions"][-2:] = reversed(failed["transitions"][-2:])
    elif change == "xml":
        role = continued_rows[2]["after"]
        inputs.data[role] = inputs.data[role].replace(b"pwsh.exe", b"changed.exe")
        next(row for row in inputs.rows if row["role"] == role).update(bytes=len(inputs.data[role]), sha256=sha(inputs.data[role]))
    elif change == "new-history": release["task_baseline"][2]["last_run_utc"] = NOW.isoformat()
    evidence = inputs.evidence(release)
    if change is None:
        validate_lineage(release, evidence)
    else:
        with pytest.raises(HandoffError): validate_lineage(release, evidence)
