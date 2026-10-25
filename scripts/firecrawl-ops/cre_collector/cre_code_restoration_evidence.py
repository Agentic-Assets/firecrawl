"""Finite failed-handoff restoration admission and execution evidence."""

import re
import xml.etree.ElementTree as ET

from cre_code_handoff_advancement import EVENTS, NS as EVENT_NS, instance

from cre_code_handoff_evidence import (
    DEPENDENCY_CHECKS, TASKS, Evidence, canonical_path, clock,
    enabled_xml_contract, identity, json_bytes, require, role_paths, sha, unique,
    validate_observation, validate_owned_qa, validate_review,
    validate_xml_transition, xml_enabled,
)


def retained_reference(reference, evidence):
    require(isinstance(reference, dict) and type(reference.get("bytes")) is int,
            "restoration raw reference missing")
    raw = evidence.read(reference["path"])
    require(len(raw) == reference["bytes"] and sha(raw) == reference.get("sha256"),
            "restoration raw evidence changed")
    evidence.original_payloads[reference["path"]] = raw
    return raw


def restoration_execution(packet, evidence, observation):
    """Authenticate bounded native history; never certify source settlement."""
    record = json_bytes(retained_reference(observation.get("execution"), evidence))
    watermark_raw = retained_reference(record.get("watermark_reference"), evidence)
    lower = json_bytes(watermark_raw)
    require(record.get("schema") == "cre-restoration-execution-v1"
            and lower.get("schema") == "cre-restoration-watermark-v1"
            and record.get("watermark") == lower
            and record.get("channel") == lower.get("channel") == "Microsoft-Windows-TaskScheduler/Operational"
            and record.get("computer") == lower.get("computer") and record["computer"],
            "restoration Scheduler producer identity differs")
    require(type(lower.get("record_id")) is int and type(record.get("upper_record_id")) is int
            and 0 < lower["record_id"] <= record["upper_record_id"]
            and clock(lower["captured_at"]) <= clock(record["started_at"]) <= clock(record["finished_at"])
            <= clock(observation["observed_at"]), "restoration Scheduler interval differs")
    tasks = unique(observation["tasks"], "key", TASKS)
    require(record.get("tasks") == [{field: value for field, value in row.items() if field != "xml_reference"}
                                   for row in observation["tasks"]], "native execution/task samples differ")
    principals = unique(record.get("principals"), "key", TASKS)
    paths = {row["task_path"] + row["task_name"]: key for key, row in tasks.items()}
    baselines = unique(packet["task_baseline"], "key", TASKS)
    for key, baseline in baselines.items():
        root = ET.fromstring(evidence.raw(baseline["xml"]))
        user = root.findtext("{*}Principals/{*}Principal/{*}UserId")
        require(isinstance(user, str) and principals[key].get("user_id") == user,
                "restoration Scheduler principal differs")
        require(type(record.get("observer_is_admin")) is bool
                and isinstance(principals[key].get("sid"), str)
                and principals[key]["sid"].startswith("S-1-")
                and (record["observer_is_admin"] or record.get("observer_sid") == principals[key]["sid"]),
                "restoration Scheduler instance visibility insufficient")
    raw_events = record.get("events")
    require(isinstance(raw_events, list) and len(raw_events) <= 768, "restoration Scheduler event bound exceeded")
    seen, instances, process_events = set(), {}, []
    for raw in raw_events:
        require(isinstance(raw, str) and "<!DOCTYPE" not in raw.upper() and "<!ENTITY" not in raw.upper(),
                "restoration Scheduler XML declaration refused")
        root = ET.fromstring(raw)
        system = root.find("e:System", EVENT_NS)
        require(system is not None and system.find("e:Provider", EVENT_NS).get("Name") == "Microsoft-Windows-TaskScheduler"
                and system.findtext("e:Channel", namespaces=EVENT_NS) == record["channel"]
                and system.findtext("e:Computer", namespaces=EVENT_NS) == record["computer"],
                "restoration Scheduler event host/channel differs")
        rid = int(system.findtext("e:EventRecordID", namespaces=EVENT_NS))
        require(lower["record_id"] < rid <= record["upper_record_id"] and rid not in seen,
                "restoration Scheduler duplicate/outside record")
        seen.add(rid)
        when = clock(system.find("e:TimeCreated", EVENT_NS).get("SystemTime"))
        require(clock(lower["captured_at"]) <= when <= clock(record["finished_at"]),
                "restoration Scheduler event outside time interval")
        data = {}
        for node in root.findall("e:EventData/e:Data", EVENT_NS):
            require(node.get("Name") not in data, "restoration Scheduler duplicate data")
            data[node.get("Name")] = node.text
        require(data.get("TaskName") in paths, "restoration Scheduler foreign task")
        event_id = int(system.findtext("e:EventID", namespaces=EVENT_NS))
        if event_id not in EVENTS:
            # Registration/trigger metadata cannot stand in for execution proof.
            continue
        if event_id == 129:
            process_events.append((paths[data["TaskName"]], rid, when, data))
            continue
        guid = instance(data.get("InstanceId") or data.get("TaskInstanceId"))
        key = paths[data["TaskName"]]
        group = instances.setdefault((key, guid), {})
        require(event_id not in group, "duplicate restoration instance event")
        group[event_id] = (rid, when, data)
    for key, rid, when, data in process_events:
        candidates = [group for (member, _), group in instances.items() if member == key
                      and 200 in group and group[200][2].get("EnginePID") == data.get("ProcessID")]
        require(len(candidates) == 1 and 129 not in candidates[0], "ambiguous native process-start event")
        candidates[0][129] = (rid, when, data)
    active = record.get("instances")
    require(isinstance(active, list), "restoration native instances missing")
    active_ids = set()
    for row in active:
        require(row.get("key") in TASKS and type(row.get("engine_pid")) is int and row["engine_pid"] > 0,
                "restoration native instance malformed")
        pair = row["key"], instance(row.get("instance_guid"))
        require(pair not in active_ids and pair in instances, "unjoined/duplicate active Scheduler instance")
        active_ids.add(pair)
    outcomes = {}
    for key, baseline in baselines.items():
        task = tasks[key]
        matches = sorted(((guid, events) for (member, guid), events in instances.items() if member == key),
                         key=lambda row: min(value[0] for value in row[1].values()))
        if not matches:
            require(task["last_task_result"] == baseline["last_task_result"]
                    and task["last_run_utc"] == baseline["last_run_utc"]
                    and task["missed_runs"] == baseline["missed_runs"]
                    and task["next_run_utc"] == baseline["next_run_utc"]
                    and task["state"] == ("Ready" if task["enabled"] else "Disabled"),
                    "task history changed without native scheduled execution")
            outcomes[key] = {"state": "no-observed-execution", "source_settlement": "unverified"}
            continue
        for guid, events in matches:
            require({107, 129, 100, 200} <= events.keys(), "restoration scheduled start/action history missing")
            ordered = [events[event] for event in EVENTS if event in events]
            require(all(a[0] < b[0] and a[1] <= b[1] for a, b in zip(ordered, ordered[1:])),
                    "restoration scheduled event chronology differs")
            require(events[100][2].get("UserContext", "").casefold() in
                    {alias.casefold() for alias in principals[key]["aliases"]},
                    "restoration scheduled event principal differs")
            action = events[200][2]
            require(action.get("ActionName") == task["actions"][0]["execute"]
                    and action.get("EnginePID") == events[129][2].get("ProcessID"),
                    "restoration scheduled action/PID differs")
            completed = 201 in events and 102 in events
            require(completed or (key, guid) in active_ids, "native scheduled execution lacks terminal or live instance")
            if completed:
                require((key, guid) not in active_ids and events[201][2].get("ActionName") == action["ActionName"]
                        and events[201][2].get("EnginePID") == action["EnginePID"],
                        "native completion action/PID differs")
        guid, latest = matches[-1]
        # Scheduler LastRunTime is sampled to seconds by the native producer.
        require(clock(task["last_run_utc"]) == latest[100][1].replace(microsecond=0),
                "current task last-run does not match newest scheduled instance")
        if (key, guid) in active_ids:
            require(task["state"] == "Running", "active Scheduler/task state differs")
            state = "running-native-source-settlement-unverified"
        else:
            result = latest[201][2].get("ResultCode")
            require(isinstance(result, str) and result.isdecimal()
                    and type(task["last_task_result"]) is int and task["last_task_result"] == int(result)
                    and task["state"] == "Ready", "native terminal/current task result differs")
            state = "completed-native-source-settlement-unverified"
        outcomes[key] = {"state": state, "instance": str(guid), "source_settlement": "unverified"}
    return outcomes

def validate_failed_handoff(packet, evidence):
    """Finite reader for an actually failed post-ready handoff and sealed audit."""
    failed = packet["failed_handoff"]
    source_raw = evidence.raw(failed["release"])
    source = json_bytes(source_raw)
    require(source.get("schema") == "cre-code-release-v1" and source.get("selected") == packet["code"],
            "failed handoff selected code differs")
    mutation = evidence.obj(failed["mutation"])
    authorization_raw = evidence.raw(failed["authorization"])
    authorization = json_bytes(authorization_raw)
    operation = canonical_path(mutation.get("operation_output"))
    require(mutation.get("schema") == "cre-code-mutation-v1"
            and mutation.get("release_sha256") == sha(source_raw)
            and mutation.get("authorization_sha256") == sha(authorization_raw)
            and mutation.get("tooling") == source["tooling"]
            and mutation.get("fleet") == source["task_baseline"]
            and authorization.get("schema") == "cre-code-authorization-v1"
            and authorization.get("release_sha256") == sha(source_raw)
            and authorization.get("observation_sha256") == sha(evidence.raw(failed["observation"]))
            and canonical_path(authorization.get("operation_output")) == operation,
            "failed handoff operation/authentication differs")
    require(clock(authorization.get("authorized_at")) <= clock(mutation.get("entered_at"))
            < clock(authorization.get("expires_at")), "original mutation outside original authorization")
    ready = evidence.obj(failed["commit_ready"])
    require(ready.get("schema") == "cre-code-commit-ready-v1" and ready.get("selected") == packet["code"]
            and ready.get("release_sha256") == sha(source_raw)
            and ready.get("authorization_sha256") == sha(authorization_raw)
            and canonical_path(ready.get("operation_output")) == operation
            and ready.get("recovery_required_at_publication") is True,
            "failed handoff actual commit readiness missing")
    historical_roles = {"runtime_snapshot", *(row["xml"] for row in source["task_baseline"])}
    historical = Evidence({"inputs": [row for row in source["inputs"] if row["role"] in historical_roles]}, evidence.read)
    evidence.original_payloads.update({historical.inputs[role]["path"]: raw for role, raw in historical.original.items()})
    readiness = ready["readiness"]
    validate_observation(source, historical, readiness, clock(readiness["qualified_at"]),
                         selected=True, ownership_phase="owned-maintenance")
    held = evidence.obj(failed["held_ready"])
    require(held.get("schema") == "cre-code-held-ready-v1" and held.get("tasks_held") is True
            and held.get("release_sha256") == sha(source_raw)
            and held.get("dependencies") == ready.get("dependencies"), "failed handoff held evidence differs")
    dependencies = ready.get("dependencies")
    require(isinstance(dependencies, dict) and set(dependencies) == {
        "package.json", "package-lock.json", "node_modules/.package-lock.json",
    } and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
              for value in dependencies.values()), "failed handoff dependency proof missing")
    failure = evidence.obj(failed["failure"])
    outer = evidence.obj(failed["outer_failure"])
    require(failure.get("schema") == "cre-code-failure-v1"
            and failure.get("classification") == "commit-uncertain"
            and failure.get("original_error_type") == "HandoffError"
            and failure.get("original_error") == "canonical owner/recovery not clear"
            and outer.get("schema") == "cre-code-outer-failure-v1"
            and outer.get("release_sha256") == sha(source_raw)
            and canonical_path(outer.get("operation_output")) == operation,
            "unsupported failed handoff boundary")
    commands = unique(failed.get("commands"), "name", {
        "hold", "npm-ls", "doctor", "gfm", "repin", "restore", "verify",
    })
    owner = None
    previous = clock(mutation["entered_at"])
    for name in ("hold", "npm-ls", "doctor", "gfm", "repin", "restore", "verify"):
        row = commands[name]
        receipt = evidence.obj(row["receipt"])
        require(receipt.get("producer") == "checkpoint-run-command-v1"
                and type(receipt.get("exit_code")) is int and receipt["exit_code"] == 0
                and receipt.get("local_settlement") == "confirmed" and receipt.get("log_closed") is True
                and receipt.get("command_log_sha256") == sha(evidence.raw(row["log"])),
                "failed handoff command settlement differs")
        started, finished = clock(receipt["started_at"]), clock(receipt["finished_at"])
        # Hold starts before its durable mutation marker; later commands are serial.
        require(started <= finished and (name == "hold" or previous <= started), "failed command chronology differs")
        if name == "hold":
            require(started <= clock(mutation["entered_at"]) <= finished, "mutation marker outside original Hold")
        previous = finished
        argv = receipt.get("argv")
        require(isinstance(argv, list) and argv, "failed command argv missing")
        if name in {"hold", "repin", "restore", "verify"}:
            require(receipt.get("schema") == "cre-code-command-v1" and len(argv) == 19,
                    "native failed command shape differs")
            expected_script = canonical_path(source["roots"]["tooling"]) + "/scripts/firecrawl-ops/cre_collector/windows/Invoke-CreCodeHandoff.ps1"
            flags = dict(zip(argv[5::2], argv[6::2], strict=True))
            require(argv[1:4] == ["-NoProfile", "-NonInteractive", "-File"]
                    and canonical_path(argv[0]) == canonical_path(source["programs"]["powershell"])
                    and canonical_path(argv[4]) == expected_script
                    and flags.get("-Mode") == name.capitalize()
                    and flags.get("-ReleaseSha") == sha(source_raw)
                    and canonical_path(flags.get("-ReleasePath")) == canonical_path(evidence.inputs[failed["release"]]["path"])
                    and canonical_path(flags.get("-Output")) == operation
                    and canonical_path(flags.get("-Observation")) == canonical_path(evidence.inputs[failed["observation"]]["path"])
                    and canonical_path(flags.get("-Authorization")) == canonical_path(evidence.inputs[failed["authorization"]]["path"]),
                    "native failed command operation differs")
            if owner is None:
                owner = flags.get("-OwnerPid")
            require(flags.get("-OwnerPid") == owner and isinstance(owner, str) and owner.isdecimal(), "failed command owner differs")
        else:
            collector = canonical_path(source["roots"]["runtime"]) + "/scripts/firecrawl-ops/cre_collector"
            require(receipt.get("schema") == "cre-owned-gate-v1" and receipt.get("identity") == packet["code"]
                    and receipt.get("unchanged") is True
                    and canonical_path(receipt.get("effective_cwd")) == collector
                    and canonical_path(receipt.get("cwd")) == collector
                    and argv == ["bash", "-c", 'cd -- "$1" && shift && exec "$@"', "cre-handoff", collector,
                                 *DEPENDENCY_CHECKS[name]], "failed dependency command identity differs")
    require(previous <= clock(readiness["qualified_at"]), "commit readiness precedes native verification")
    require(clock(readiness["qualified_at"]) < clock(authorization["expires_at"]),
            "original commit readiness outside authorization")
    for row in unique(failed.get("native_steps"), "mode", {"Hold", "Repin", "Restore"}).values():
        step = evidence.obj(row["receipt"])
        require(step.get("status") == "passed" and step.get("mode") == row["mode"]
                and str(step.get("owner_pid")) == owner and step.get("release_sha256") == sha(source_raw),
                "failed handoff native step receipt differs")
    seal = evidence.obj(failed["settlement_seal"])
    require(isinstance(seal, list) and seal, "failed settlement seal missing")
    for row in unique(seal, "path").values():
        raw = evidence.read(row["path"])
        require(type(row.get("bytes")) is int and row["bytes"] == len(raw) and row.get("sha256") == sha(raw),
                "failed settlement seal changed")
        evidence.original_payloads[row["path"]] = raw
    audit_raw = evidence.raw(failed["settlement_report"])
    require(any(row["sha256"] == sha(audit_raw) and row["bytes"] == len(audit_raw) for row in seal),
            "failed audit report not sealed")
    audit = json_bytes(audit_raw)
    require(clock(audit.get("current_windows_observed_at")) >= clock(readiness["qualified_at"])
            and clock(audit.get("current_linux_observed_at")) >= clock(readiness["qualified_at"]),
            "failed audit precedes original readiness")
    require(audit.get("canonical_directory_exists") is False and audit.get("quarantine_guard_exists") is False
            and audit.get("actual_authority_recovery_required") is False
            and audit.get("authority_owner_pid_exists") is False and audit.get("authority_nonblocking_flock_available") is True
            and audit.get("authority_probe_released") is True and audit.get("authority_bytes_unchanged") is True
            and str(audit.get("authority_owner_pid")) == owner
            and audit.get("current_com_instances") == [] and audit.get("relevant_current_uid_frontends") == []
            and audit.get("mutations_performed") == [] and audit.get("pending_handles") == []
            and audit.get("saved_failed_operation", {}).get("terminal_exists") is False
            and audit["saved_failed_operation"].get("native_completion_exists") is False
            and type(audit["saved_failed_operation"].get("original_exit")) is int
            and audit["saved_failed_operation"]["original_exit"] != 0,
            "failed handoff settlement is not independently clear")
    require({key: audit["current_linux_git"].get(key) for key in ("sha", "tree")} == packet["code"]
            and audit["current_linux_git"].get("status") == "", "failed audit code differs")
    native_git_raw = evidence.raw(failed["native_git"])
    require(any(row["sha256"] == sha(native_git_raw) for row in seal), "failed native Git observation not sealed")
    native_git = unique(json_bytes(native_git_raw), "role")["native"]
    require({key: native_git.get(key) for key in ("sha", "tree")} == packet["code"]
            and native_git.get("status") == ""
            and canonical_path(native_git.get("root")) == canonical_path(source["roots"]["native"]),
            "failed native code identity differs")
    return source, mutation, ready, audit


def validate_restoration(packet, evidence, *, historical=False):
    require(packet.get("schema") == "cre-code-restoration-v1"
            and packet.get("scope") == "restore-held-task-states", "unknown restoration packet/scope")
    for field in ("code", "development", "tooling"):
        identity(packet.get(field))
    require(set(packet.get("roots", {})) == {"native", "runtime", "development", "tooling"},
            "restoration checkout roots missing")
    role_paths(packet)
    require(set(packet.get("programs", {})) == {"native_git", "powershell"}
            and set(packet.get("wsl", {})) == {"distro", "user"}
            and all(isinstance(value, str) and value and "\0" not in value
                    for group in (packet["programs"], packet["wsl"]) for value in group.values()),
            "restoration native tools/Ubuntu missing")
    require(isinstance(packet.get("preserve_files"), list)
            and packet.get("runtime_env_file") in packet["preserve_files"], "restoration preservation references missing")
    source, _, ready, audit = validate_failed_handoff(packet, evidence)
    require(all(canonical_path(packet["roots"][key]) == canonical_path(source["roots"][key])
                for key in ("native", "runtime")), "restoration target checkout differs")
    require(packet["runtime_env_file"] == source["runtime_env_file"]
            and packet["preserve_files"] == source["preserve_files"], "restoration private preservation references differ")
    require(evidence.obj("runtime_snapshot") == json_bytes(evidence.read(
        next(row["path"] for row in source["inputs"] if row["role"] == "runtime_snapshot"))),
        "restoration runtime snapshot differs from failed operation")
    current = unique(packet.get("task_baseline"), "key", TASKS)
    original = unique(source["task_baseline"], "key", TASKS)
    audited = unique(audit.get("tasks"), "key", TASKS)
    pins = unique(audit.get("task_pins"), "key", TASKS)
    xml_rows = unique(packet["failed_handoff"].get("xml"), "key", TASKS)
    for key, baseline in current.items():
        old = original[key]
        xml = xml_rows[key]
        before, final, held = (evidence.raw(xml[field]) for field in ("before", "final", "audited"))
        validate_xml_transition(before, final, source["deployed"]["sha"], packet["code"]["sha"])
        require(before == evidence.read(next(row["path"] for row in source["inputs"] if row["role"] == old["xml"])),
                "restoration original XML differs")
        require(pins[key].get("xml_sha256") == sha(held)
                and any(row.get("sha256") == sha(held) and canonical_path(row["path"]) == canonical_path(pins[key]["xml_path"])
                        for row in evidence.obj(packet["failed_handoff"]["settlement_seal"]))
                and pins[key].get("expected_sha_values") == [packet["code"]["sha"]]
                and xml_enabled(final) is old["enabled"] and xml_enabled(held) is False
                and xml_enabled(evidence.raw(baseline["xml"])) is False
                and enabled_xml_contract(final, False) == enabled_xml_contract(held, False)
                and enabled_xml_contract(held, False) == enabled_xml_contract(evidence.raw(baseline["xml"]), False),
                "restoration held XML differs beyond original code/Enabled change")
        require(baseline.get("enabled") is False and baseline.get("desired_enabled") is old["enabled"]
                and type(baseline.get("missed_runs")) is int and baseline["missed_runs"] >= 0,
                "restoration original flags/current missed count malformed")
        for field in ("task_path", "task_name", "last_task_result", "last_run_utc"):
            require(type(baseline.get(field)) is type(old.get(field)) and baseline[field] == old[field],
                    "restoration original task history changed")
        expected_actions = [{name: value.replace(source["deployed"]["sha"], packet["code"]["sha"])
                             for name, value in action.items()} for action in old["actions"]]
        require(baseline.get("actions") == expected_actions
                and audited[key].get("enabled") is False and audited[key].get("state") == "Disabled"
                and audited[key].get("last_result") == old["last_task_result"]
                and audited[key].get("last_run_utc") == old["last_run_utc"],
                "restoration audit/task tuple differs")
        clock(baseline["next_run_utc"])
        require(ready["readiness"]["tasks"] is not None, "failed operation task readback missing")
    if not historical:
        require(packet["qa"].get("producer") == "owned-gates-v1", "restoration requires actual owned QA gates")
        validate_owned_qa(packet, evidence, expected_tree=packet["tooling"]["tree"])


def validate_restoration_observation(packet, evidence, observation, current, *, final=False, owned=False):
    from cre_code_handoff_host import qualify_host, qualify_native

    require(observation.get("schema") == "cre-code-restoration-observation-v1"
            and observation.get("native") == observation.get("runtime") == packet["code"],
            "restoration observation code/schema differs")
    observed, qualified = clock(observation.get("observed_at")), clock(observation.get("qualified_at"))
    require(observed <= qualified <= current and 0 <= (current - observed).total_seconds() < 300,
            "restoration observation expired/future")
    released = final and not owned and packet.get("enable_boundary") == "founder-approved-catchup-v1"
    if released:
        ready = json_bytes(retained_reference(observation.get("admission_reference"), evidence))
        require(ready.get("schema") == "cre-code-restoration-ready-v1"
                and ready.get("restoration_sha256") == observation.get("restoration_sha256")
                and ready.get("code") == packet["code"] and ready.get("recovery_required_at_publication") is True,
                "released observation lacks original owned admission")
        validate_restoration_observation(packet, evidence, ready["readiness"],
                                         clock(ready["readiness"]["qualified_at"]), final=True, owned=True)
        ownership = observation.get("ownership", {})
        require(ownership.get("verified") is True and type(ownership.get("recovery_required")) is bool
                and ownership.get("phase") in {"released-clear", "released-scheduled-successor"},
                "released observation ownership is unverified")
        if ownership["phase"] == "released-clear":
            require(ownership["recovery_required"] is False, "released clear ownership still recovery fenced")
        else:
            require(ownership.get("source_settlement") == "unverified"
                    and ownership.get("key") in TASKS
                    and ownership.get("child_birth", {}).get("pid") != ownership.get("parent_birth", {}).get("pid")
                    and all(type(ownership.get("child_birth", {}).get(field)) is int
                            and ownership["child_birth"][field] > 0 for field in ("pid", "start_ticks"))
                    and all(isinstance(ownership.get(field), str) and re.fullmatch("[0-9a-f]{64}", ownership[field])
                            for field in ("authority_token_sha256", "authority_generation_sha256")),
                    "scheduled successor identity missing")
            for field in ("run_reference", "manifest_reference"):
                retained_reference(ownership.get(field), evidence)
    else:
        require(observation.get("ownership") == {"phase": "owned-maintenance" if owned else "released-clear",
                "verified": True, "recovery_required": owned}
                and observation.get("resources_ok") is True and observation.get("queues_idle") is True,
                "restoration ownership/admission differs")
        qualify_host(observation, evidence.obj("runtime_snapshot"))
        qualify_native(observation.get("windows_resources"))
    tasks = unique(observation.get("tasks"), "key", TASKS)
    catchup = final and packet.get("enable_boundary") == "founder-approved-catchup-v1"
    if catchup:
        restoration_execution(packet, evidence, observation)
    for key, baseline in unique(packet["task_baseline"], "key", TASKS).items():
        task = tasks[key]
        enabled = baseline["desired_enabled"] if final else False
        require(task.get("enabled") is enabled
                and (catchup or task.get("state") == ("Ready" if enabled else "Disabled"))
                and type(task.get("missed_runs")) is int and task["missed_runs"] >= 0
                and (catchup or task["missed_runs"] == baseline["missed_runs"]),
                "restoration task state/missed count drift")
        for field in ("task_path", "task_name", "last_task_result", "last_run_utc", "next_run_utc", "actions"):
            mutable = catchup and field in {"last_task_result", "last_run_utc"}
            require(type(task.get(field)) is type(baseline.get(field)) and (mutable or task[field] == baseline[field]),
                    "restoration task tuple/clock drift")
        xml = evidence.raw(baseline["xml"])
        if final:
            reference = task.get("xml_reference")
            require(isinstance(reference, dict), "restoration final complete XML reference missing")
            raw = evidence.read(reference["path"])
            require(type(reference.get("bytes")) is int and reference["bytes"] == len(raw)
                    and reference.get("sha256") == task.get("xml_sha256") == sha(raw)
                    and xml_enabled(raw) is enabled
                    and enabled_xml_contract(raw, enabled) == enabled_xml_contract(xml, enabled),
                    "restoration final XML differs beyond Enabled")
            evidence.original_payloads[reference["path"]] = raw
        else:
            require(task.get("xml_sha256") == sha(xml), "restoration held complete XML drift")
        if enabled:
            require((clock(task["next_run_utc"]) - current).total_seconds() >= 900, "restoration enabled trigger too close")


def validate_restoration_authorization(packet_raw, packet, evidence, observation_raw, authorization, current, *, review_reader=None):
    require(authorization.get("schema") == "cre-code-restoration-authorization-v1"
            and authorization.get("restoration_sha256") == sha(packet_raw)
            and authorization.get("observation_sha256") == sha(observation_raw)
            and authorization.get("scope") == "restore-held-task-states", "restoration authorization binding differs")
    require(json_bytes(observation_raw).get("restoration_sha256") == sha(packet_raw),
            "restoration observation belongs to another packet")
    start, end = clock(authorization["authorized_at"]), clock(authorization["expires_at"])
    require(start <= current < end and 0 < (end - start).total_seconds() <= 300, "restoration authorization expired/lifetime differs")
    reader = review_reader or evidence.read
    review = authorization["review"]
    raw = reader(review["path"])
    require(sha(raw) == review["sha256"], "restoration review changed")
    attestation = json_bytes(raw)
    validate_review(attestation, reader(attestation["body"]["path"]),
                    {"restoration_sha256": sha(packet_raw), "tooling": packet["tooling"]}, "restore-held-task-states")
    require(review.get("reviewer") == attestation["reviewer"] and review.get("approval_ref") == attestation["approval_ref"],
            "restoration reviewer/reference differs")
    validate_restoration_observation(packet, evidence, json_bytes(observation_raw), current)
    evidence.recheck()


def validate_restoration_enable_boundary(packet, evidence, current):
    """Finite policy admission; a missed count is never a queue certificate."""
    mode = packet.get("enable_boundary")
    require(mode in {"zero-missed-no-crossed-trigger-v1", "founder-approved-catchup-v1"},
            "restoration catch-up admission unsupported; bounded absence cannot authorize Enable")
    source, _, _, audit = validate_failed_handoff(packet, evidence)
    require(clock(audit["current_windows_observed_at"]) <= current
            and clock(audit["current_linux_observed_at"]) <= current, "restoration audit is from the future")
    original = unique(source["task_baseline"], "key", TASKS)
    audited = unique(audit["tasks"], "key", TASKS)
    if mode == "founder-approved-catchup-v1":
        policy = packet.get("catchup_policy", {})
        require(policy.get("failed_release_sha256") == sha(evidence.raw(packet["failed_handoff"]["release"]))
                and policy.get("fleet") == packet["task_baseline"]
                and policy.get("tooling") == packet["tooling"], "catch-up policy packet/fleet binding differs")
        decision = evidence.obj(policy["decision"])
        require(decision.get("catchup", {}).get("received") is True
                and decision["catchup"].get("answer") == "Authorize one-time catch-up recovery (Recommended)"
                and decision["catchup"].get("questionItemId") ==
                '["request_user_input_async","call_564c89f05bae4490afe3bb3e601391bb",0]'
                and decision.get("original_approval_prompt", {}).get("catchupIssue") == "AGENTIC-4845"
                and decision["original_approval_prompt"].get("originalTasks") == list(TASKS.values()),
                "explicit one-time human catch-up choice missing")
    for key, row in unique(packet["task_baseline"], "key", TASKS).items():
        require(type(row.get("missed_runs")) is int and row["missed_runs"] >= 0,
                "current missed count malformed")
        if mode == "zero-missed-no-crossed-trigger-v1":
            require(row["missed_runs"] == 0
                    and type(audited[key].get("missed_runs")) is int and audited[key]["missed_runs"] == 0
                    and row["next_run_utc"] == audited[key]["next_run_utc"] == original[key]["next_run_utc"],
                    "restoration catch-up admission unsupported: missed or crossed trigger")
        if row["desired_enabled"]:
            require((clock(row["next_run_utc"]) - current).total_seconds() >= 900,
                    "original next trigger crossed or inside restoration margin")
    evidence.recheck()
