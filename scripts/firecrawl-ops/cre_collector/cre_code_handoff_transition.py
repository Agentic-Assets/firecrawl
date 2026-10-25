"""Maintained postreadback receipt producer and finite lineage consumer."""

import re

from cre_code_restoration_evidence import (
    validate_restoration,
    validate_restoration_enable_boundary,
    validate_restoration_observation,
)
from cre_code_handoff_evidence import (
    TASKS,
    Evidence,
    canonical_path,
    clock,
    enabled_xml_contract,
    json_bytes,
    require,
    sha,
    unique,
    validate_observation,
    validate_xml_transition,
    xml_enabled,
)

TUPLE_FIELDS = ("task_path", "task_name", "enabled", "last_task_result", "last_run_utc", "next_run_utc", "actions")


def original_failures(source):
    inputs = unique(source.get("inputs"), "role")
    rows = []
    for baseline in source["task_baseline"]:
        if baseline["last_task_result"] == 0:
            continue
        failure = baseline["failure"]
        rows.append({"key": baseline["key"], "operation": failure["operation"],
                     "terminal_sha256": inputs[failure["terminal"]]["sha256"],
                     "last_task_result": baseline["last_task_result"], "last_run_utc": baseline["last_run_utc"]})
    return rows


def reference(path, read):
    raw = read(path)
    return {"path": str(path), "sha256": sha(raw), "bytes": len(raw)}


def bound(reference_value, read):
    raw = read(reference_value["path"])
    require(sha(raw) == reference_value["sha256"] and len(raw) == reference_value["bytes"], "transition payload changed")
    return raw


def make_transition(release, release_path, operation, independent, read):
    """Use actual operation and independent outputs; never fabricate history."""
    tasks = unique(json_bytes(read(independent / "observation.json"))["tasks"], "key", TASKS)
    rows = []
    for baseline in release["task_baseline"]:
        key = baseline["key"]
        rows.append({"key": key, "before_xml": reference(operation / (key + "-before.xml"), read),
                     "final_xml": reference(operation / (key + "-final.xml"), read),
                     "independent_xml": reference(independent / (key + "-observed.xml"), read),
                     "before_tuple": {field: baseline[field] for field in TUPLE_FIELDS},
                     "after_tuple": {field: tasks[key][field] for field in TUPLE_FIELDS}})
    return {"schema": "cre-code-transition-v1", "old": release["deployed"], "new": release["selected"],
            "original_failures": original_failures(release),
            "release": reference(release_path, read), "mutation": reference(operation / "mutation-start.json", read),
            "commit_ready": reference(operation / "commit-ready.json", read),
            "terminal": reference(operation / "terminal.json", read),
            "native_completion": reference(str(operation) + ".native.json", read),
            "native_stdout": reference(str(operation) + ".stdout.raw", read),
            "native_stderr": reference(str(operation) + ".stderr.raw", read),
            "independent_observation": reference(independent / "observation.json", read), "tasks": rows}


def validate_transition(receipt, old, new, read, *, retain=None):
    require(receipt.get("schema") == "cre-code-transition-v1"
            and receipt.get("old") == old and receipt.get("new") == new, "maintained transition identities differ")

    def payload(ref):
        raw = bound(ref, read)
        if retain is not None:
            retain[ref["path"]] = raw
        return raw

    release_raw = payload(receipt["release"])
    source = json_bytes(release_raw)
    require(source.get("deployed") == old and source.get("selected") == new, "transition release differs")
    require(receipt.get("original_failures") == original_failures(source), "transition original failed provenance differs")
    mutation = json_bytes(payload(receipt["mutation"]))
    ready_raw = payload(receipt["commit_ready"])
    ready = json_bytes(ready_raw)
    terminal_raw = payload(receipt["terminal"])
    terminal = json_bytes(terminal_raw)
    native_raw = payload(receipt["native_completion"])
    native = json_bytes(native_raw)
    release_digest = sha(release_raw)
    require(mutation.get("schema") == "cre-code-mutation-v1"
            and mutation.get("release_sha256") == release_digest
            and mutation.get("tooling") == source.get("tooling")
            and mutation.get("fleet") == source.get("task_baseline"), "transition original mutation differs")
    operation_output = canonical_path(mutation.get("operation_output"))
    authorization_digest = mutation.get("authorization_sha256")
    require(isinstance(authorization_digest, str) and len(authorization_digest) == 64,
            "transition original authorization missing")
    for record in (ready, terminal, native):
        require(canonical_path(record.get("operation_output")) == operation_output
                and record.get("authorization_sha256") == authorization_digest,
                "transition mixes distinct operation attempts")
    require(ready.get("schema") == "cre-code-commit-ready-v1"
            and ready.get("release_sha256") == release_digest
            and ready.get("selected") == new and ready.get("recovery_required_at_publication") is True
            and terminal.get("schema") == "cre-code-terminal-v1" and terminal.get("status") == "committed"
            and terminal.get("selected") == new and terminal.get("release_sha256") == release_digest
            and terminal.get("canonical_lease_released") is True
            and terminal.get("commit_ready_sha256") == sha(ready_raw), "transition lacks actual committed terminal")
    capture = native.get("capture", {})
    require(native.get("schema") == "cre-code-native-completion-v1"
            and native.get("release_sha256") == release_digest
            and capture.get("Started") is True and capture.get("Failed") is False
            and type(capture.get("NativeExitCode")) is int and capture["NativeExitCode"] == 0
            and capture.get("Stdout", {}).get("Eof") is True and capture.get("Stderr", {}).get("Eof") is True
            and capture.get("Stdout", {}).get("Truncated") is False and capture.get("Stderr", {}).get("Truncated") is False
            and native.get("stdout_sha256") == sha(payload(receipt["native_stdout"]))
            and native.get("stderr_sha256") == sha(payload(receipt["native_stderr"])), "native terminal/drain confirmation absent")
    observation = json_bytes(payload(receipt["independent_observation"]))
    require(observation.get("schema") == "cre-code-observation-v1"
            and observation.get("native") == observation.get("runtime") == new
            and observation.get("operation_terminal_sha256") == sha(terminal_raw)
            and observation.get("release_sha256") == release_digest
            and canonical_path(observation.get("operation_output")) == operation_output
            and observation.get("authorization_sha256") == authorization_digest
            and observation.get("native_completion_sha256") == sha(native_raw),
            "independent transition observation differs")
    dependencies = ready.get("dependencies")
    require(isinstance(dependencies, dict) and set(dependencies) == {
        "package.json", "package-lock.json", "node_modules/.package-lock.json",
    } and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
              for value in dependencies.values()), "transition dependency parity missing")
    # Only the original pinned host snapshot and baseline XML are needed here.
    # Reopening historical QA/releases recursively would change this finite contract.
    roles = {"runtime_snapshot", *(row["xml"] for row in source["task_baseline"])}
    pinned = Evidence({"inputs": [row for row in source["inputs"] if row["role"] in roles]}, read)
    if retain is not None:
        for role, raw in pinned.original.items():
            retain[pinned.inputs[role]["path"]] = raw
    readiness = ready.get("readiness")
    require(isinstance(readiness, dict), "transition commit readiness missing")
    qualified = clock(readiness.get("qualified_at"))
    finished = clock(terminal.get("finished_at"))
    require(qualified <= finished <= clock(observation.get("observed_at")), "transition chronology differs")
    validate_observation(source, pinned, readiness, qualified, selected=True, ownership_phase="owned-maintenance")
    validate_observation(source, pinned, observation, clock(observation.get("qualified_at")), selected=True)
    observed = unique(observation.get("tasks"), "key", TASKS)
    rows = unique(receipt.get("tasks"), "key", TASKS)
    baselines = unique(source.get("task_baseline"), "key", TASKS)
    for key, row in rows.items():
        before = payload(row["before_xml"])
        final = payload(row["final_xml"])
        require(payload(row["independent_xml"]) == final, "independent final XML differs")
        validate_xml_transition(before, final, old["sha"], new["sha"])
        prior = {field: baselines[key][field] for field in TUPLE_FIELDS}
        after = {field: observed[key][field] for field in TUPLE_FIELDS}
        wanted = dict(prior, actions=[{field: value.replace(old["sha"], new["sha"])
                                      for field, value in action.items()} for action in prior["actions"]])
        require(row["before_tuple"] == prior and row["after_tuple"] == after == wanted
                and observed[key].get("xml_sha256") == sha(final)
                and observed[key].get("state") == ("Ready" if prior["enabled"] else "Disabled"),
                "maintained transition task tuple changed")
    return rows


def restoration_history_inputs(packet):
    """Only the finite failed-attempt/host/task subset, never historical QA."""
    failed = packet["failed_handoff"]
    roles = {"runtime_snapshot", *(row["xml"] for row in packet["task_baseline"])}
    roles.update(failed[key] for key in (
        "release", "authorization", "observation", "mutation", "held_ready", "commit_ready",
        "failure", "outer_failure", "settlement_seal", "settlement_report", "native_git",
    ))
    for row in failed["commands"]:
        roles.update((row["receipt"], row["log"]))
    roles.update(row["receipt"] for row in failed["native_steps"])
    if packet.get("enable_boundary") == "founder-approved-catchup-v1":
        roles.add(packet["catchup_policy"]["decision"])
    for row in failed["xml"]:
        roles.update(row[field] for field in ("before", "final", "audited"))
    return [row for row in packet["inputs"] if row["role"] in roles]


def make_reconciliation(packet, packet_path, operation, independent, read):
    failed = packet["failed_handoff"]
    inputs = unique(packet["inputs"], "role")
    source = json_bytes(read(inputs[failed["release"]]["path"]))
    observed = unique(json_bytes(read(independent / "observation.json"))["tasks"], "key", TASKS)
    original = unique(source["task_baseline"], "key", TASKS)
    xml = unique(failed["xml"], "key", TASKS)
    tasks = []
    for key, baseline in original.items():
        tasks.append({"key": key, "before_xml": dict(inputs[xml[key]["before"]]),
                      "final_xml": reference(operation / (key + "-final.xml"), read),
                      "independent_xml": reference(independent / (key + "-observed.xml"), read),
                      "before_tuple": {field: baseline[field] for field in TUPLE_FIELDS},
                      "after_tuple": {field: observed[key][field] for field in (*TUPLE_FIELDS, "missed_runs")}})
    receipt = {"schema": "cre-code-reconciled-failure-v1", "old": source["deployed"], "new": packet["code"],
            "failed_predecessor_remains_failed": True, "original_failures": original_failures(source),
            "restoration_packet": reference(packet_path, read),
            "mutation": reference(operation / "mutation-start.json", read),
            "ready": reference(operation / "restoration-ready.json", read),
            "terminal": reference(operation / "restoration-terminal.json", read),
            "native_completion": reference(str(operation) + ".native.json", read),
            "native_stdout": reference(str(operation) + ".stdout.raw", read),
            "native_stderr": reference(str(operation) + ".stderr.raw", read),
            "independent_observation": reference(independent / "observation.json", read), "tasks": tasks}
    if packet.get("enable_boundary") == "founder-approved-catchup-v1":
        receipt.update(release_start=reference(operation / "release-start.json", read),
                       release_observation=reference(operation / "release-observation.json", read))
    return receipt


def validate_reconciliation(receipt, old, new, read, *, retain=None):
    require(receipt.get("schema") == "cre-code-reconciled-failure-v1"
            and receipt.get("old") == old and receipt.get("new") == new
            and receipt.get("failed_predecessor_remains_failed") is True, "reconciled failure identity differs")

    def payload(ref):
        raw = bound(ref, read)
        if retain is not None:
            retain[ref["path"]] = raw
        return raw

    packet_raw = payload(receipt["restoration_packet"])
    packet = json_bytes(packet_raw)
    pinned = Evidence({"inputs": restoration_history_inputs(packet)}, read)
    validate_restoration(packet, pinned, historical=True)
    if retain is not None:
        retain.update(pinned.original_payloads)
        retain.update({pinned.inputs[role]["path"]: raw for role, raw in pinned.original.items()})
    source = pinned.obj(packet["failed_handoff"]["release"])
    require(source["deployed"] == old and packet["code"] == source["selected"] == new
            and receipt.get("original_failures") == original_failures(source), "reconciliation lost original failed provenance")
    mutation = json_bytes(payload(receipt["mutation"]))
    ready_raw, terminal_raw, native_raw = (payload(receipt[field]) for field in ("ready", "terminal", "native_completion"))
    ready, terminal, native = (json_bytes(raw) for raw in (ready_raw, terminal_raw, native_raw))
    require(mutation.get("schema") == "cre-code-restoration-mutation-v1"
            and mutation.get("restoration_sha256") == sha(packet_raw)
            and mutation.get("tooling") == packet["tooling"] and mutation.get("fleet") == packet["task_baseline"],
            "reconciliation lacks distinct owned mutation")
    operation, authorization = canonical_path(mutation.get("operation_output")), mutation.get("authorization_sha256")
    require(isinstance(authorization, str) and re.fullmatch(r"[0-9a-f]{64}", authorization), "restoration authorization absent")
    for record in (ready, terminal, native):
        require(record.get("restoration_sha256") == sha(packet_raw)
                and canonical_path(record.get("operation_output")) == operation
                and record.get("authorization_sha256") == authorization, "reconciliation mixes restoration attempts")
    require(ready.get("schema") == "cre-code-restoration-ready-v1" and ready.get("code") == new
            and ready.get("recovery_required_at_publication") is True
            and terminal.get("schema") == "cre-code-restoration-terminal-v1" and terminal.get("status") == "restored"
            and terminal.get("canonical_lease_released") is True and terminal.get("code") == new
            and terminal.get("ready_sha256") == sha(ready_raw)
            and terminal.get("failed_predecessor_release_sha256") == sha(pinned.raw(packet["failed_handoff"]["release"])),
            "reconciliation lacks actual restoration terminal")
    if packet.get("enable_boundary") == "founder-approved-catchup-v1":
        started_raw, released_raw = (payload(receipt[field]) for field in ("release_start", "release_observation"))
        started, released = json_bytes(started_raw), json_bytes(released_raw)
        for record in (started, released):
            require(record.get("restoration_sha256") == sha(packet_raw)
                    and record.get("authorization_sha256") == authorization
                    and canonical_path(record.get("operation_output")) == operation,
                    "reconciliation release attempt differs")
        require(started.get("schema") == "cre-code-restoration-release-start-v1"
                and started.get("mutation_sha256") == sha(payload(receipt["mutation"]))
                and started.get("ready_sha256") == sha(ready_raw)
                and started.get("owned_authority", {}).get("recovery_required") is True
                and released.get("schema") == "cre-code-restoration-release-observation-v1"
                and released.get("release_start_sha256") == terminal.get("release_start_sha256") == sha(started_raw)
                and terminal.get("release_observation_sha256") == sha(released_raw)
                and released.get("released_authority") == terminal.get("released_authority")
                and released.get("ownership", {}).get("verified") is True
                and released["ownership"].get("phase") in {"released-clear", "released-scheduled-successor"}
                and clock(ready["readiness"]["qualified_at"]) <= clock(started["started_at"])
                <= clock(released["observed_at"]) <= clock(terminal["finished_at"]),
                "reconciliation release was not authenticated/confirmed")
    capture = native.get("capture", {})
    require(native.get("schema") == "cre-code-restoration-native-completion-v1"
            and capture.get("Started") is True and capture.get("Failed") is False
            and type(capture.get("NativeExitCode")) is int and capture["NativeExitCode"] == 0
            and all(capture.get(stream, {}).get("Eof") is True and capture[stream].get("Truncated") is False
                    for stream in ("Stdout", "Stderr"))
            and native.get("stdout_sha256") == sha(payload(receipt["native_stdout"]))
            and native.get("stderr_sha256") == sha(payload(receipt["native_stderr"])), "restoration native completion uncertain")
    observation = json_bytes(payload(receipt["independent_observation"]))
    require(observation.get("restoration_sha256") == sha(packet_raw)
            and observation.get("operation_terminal_sha256") == sha(terminal_raw)
            and observation.get("native_completion_sha256") == sha(native_raw)
            and canonical_path(observation.get("operation_output")) == operation
            and observation.get("authorization_sha256") == authorization, "reconciliation independent observation differs")
    qualified = clock(ready["readiness"]["qualified_at"])
    validate_restoration_enable_boundary(packet, pinned, qualified)
    require(qualified <= clock(terminal["finished_at"]) <= clock(observation["observed_at"]), "restoration chronology differs")
    validate_restoration_observation(packet, pinned, ready["readiness"], qualified, final=True, owned=True)
    validate_restoration_observation(packet, pinned, observation, clock(observation["qualified_at"]), final=True)
    observed = unique(observation["tasks"], "key", TASKS)
    originals = unique(source["task_baseline"], "key", TASKS)
    failed_xml = unique(packet["failed_handoff"]["xml"], "key", TASKS)
    rows = unique(receipt.get("tasks"), "key", TASKS)
    for key, row in rows.items():
        before, final = payload(row["before_xml"]), payload(row["final_xml"])
        require(before == pinned.raw(failed_xml[key]["before"])
                and payload(row["independent_xml"]) == final
                and observed[key]["xml_sha256"] == sha(final)
                and xml_enabled(final) is originals[key]["enabled"]
                and enabled_xml_contract(final, originals[key]["enabled"])
                == enabled_xml_contract(pinned.raw(failed_xml[key]["final"]), originals[key]["enabled"]),
                "reconciled complete XML differs beyond original pins/Enabled")
        prior = {field: originals[key][field] for field in TUPLE_FIELDS}
        after = {field: observed[key][field] for field in (*TUPLE_FIELDS, "missed_runs")}
        require(row["before_tuple"] == prior and row["after_tuple"] == after
                and all(prior[field] == after[field] for field in
                        (("task_path", "task_name", "enabled") if packet.get("enable_boundary") == "founder-approved-catchup-v1"
                         else ("task_path", "task_name", "enabled", "last_task_result", "last_run_utc"))),
                "reconciliation changed original task execution history")
    if retain is not None:
        retain.update(pinned.original_payloads)
    return rows
