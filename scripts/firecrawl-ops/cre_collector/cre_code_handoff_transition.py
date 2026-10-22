"""Maintained postreadback receipt producer and finite lineage consumer."""

import re

from cre_code_handoff_evidence import (
    TASKS,
    Evidence,
    canonical_path,
    clock,
    json_bytes,
    require,
    sha,
    unique,
    validate_observation,
    validate_xml_transition,
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
