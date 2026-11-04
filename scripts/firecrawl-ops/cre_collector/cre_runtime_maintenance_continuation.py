"""Bound the no-image continuation of a failed held-fleet maintenance."""

import json
import re
from itertools import pairwise
from pathlib import PurePosixPath

from cre_code_handoff_evidence import (
    TASKS,
    canonical_path,
    identity,
    json_bytes,
    require,
    sha,
    unique,
    validate_xml_transition,
    xml_enabled,
)
from cre_code_handoff_git import local_path


def runtime_before(packet):
    """Keep legacy packets bound to the original task identity."""
    return identity(packet["runtime_before"] if "runtime_before" in packet else packet.get("old"))


def validate_fresh_held_capture(packet, prior, evidence, fresh, receipt, git, repo, receipt_path):
    """Bind the actual read-only native producer and both closed streams."""
    from cre_code_handoff_evidence import clock
    from cre_code_restoration_evidence import retained_reference

    require(receipt.get("schema") == "cre-held-partial-maintenance-native-capture-v1"
            and identity(receipt.get("source_identity")) == identity(prior["new"])
            and canonical_path(receipt.get("source_root")) == canonical_path(packet["roots"]["native"]),
            "fresh held capture source differs")
    source = receipt["source_identity"]
    require(git.text(repo, "rev-parse", source["sha"] + "^{tree}") == source["tree"],
            "fresh held capture source tree differs")
    names = ("CreCodeHandoff.ps1", "CreTaskContract.ps1", "CreRestorationExecution.ps1",
             "CreRuntimeMaintenance.ps1", "CreNativeCapture.cs")
    files = receipt.get("source_files")
    require(isinstance(files, list) and len(files) == len(names), "fresh held capture source files differ")
    for row, name in zip(files, names, strict=True):
        relative = "scripts/firecrawl-ops/cre_collector/windows/" + name
        expected_path = canonical_path(packet["roots"]["native"]) + "/" + relative
        raw = git.command(repo, "show", source["sha"] + ":" + relative)
        require(isinstance(row, dict) and set(row) == {"path", "sha256", "bytes"}
                and canonical_path(row["path"]) == expected_path
                and type(row["bytes"]) is int and row["bytes"] == len(raw) and row["sha256"] == sha(raw),
                "fresh held capture source blob differs")
    script = receipt.get("capture_script")
    retained_reference(script, evidence)
    program = receipt.get("program")
    require(isinstance(program, dict) and set(program) == {"path", "sha256", "bytes"}
            and canonical_path(program["path"]) == canonical_path(packet["programs"]["powershell"]),
            "fresh held capture program differs")
    archived_program = evidence.raw(packet["continuation"]["fresh_held_program"])
    require(type(program["bytes"]) is int and program["bytes"] == len(archived_program)
            and program["sha256"] == sha(archived_program), "fresh held captured program bytes differ")
    role = packet["continuation"]["fresh_held_observation"]
    obs_ref = evidence.inputs[role]
    observation_ref = receipt.get("observation")
    require(isinstance(observation_ref, dict) and set(observation_ref) == {"path", "sha256", "bytes"}
            and canonical_path(observation_ref["path"]) == canonical_path(obs_ref["path"])
            and observation_ref["sha256"] == obs_ref["sha256"] and observation_ref["bytes"] == obs_ref["bytes"]
            and retained_reference(observation_ref, evidence) == evidence.raw(role),
            "fresh held captured observation differs")
    capture_dir = canonical_path(receipt_path).rsplit("/", 1)[0]
    observation_path = canonical_path(observation_ref["path"])
    require(observation_path == capture_dir + "/observation/observation.json"
            and receipt.get("argv")[:3] == ["-NoProfile", "-NonInteractive", "-File"]
            and len(receipt["argv"]) == 6 and receipt["argv"][4] == "-Output"
            and canonical_path(receipt["argv"][3]) == canonical_path(script["path"])
            and canonical_path(receipt["argv"][5]) == capture_dir + "/observation",
            "fresh held capture command differs")
    capture = receipt.get("capture", {})
    require(capture.get("StartAttempted") is True and capture.get("Started") is True
            and capture.get("StartFailed") is False and capture.get("ReadOnlyDeadlineExpired") is False
            and type(capture.get("NativeExitCode")) is int and capture["NativeExitCode"] == 0
            and capture.get("Failed") is False, "fresh held capture did not complete")
    for name in ("Stdout", "Stderr"):
        ref = receipt.get(name.lower())
        raw = retained_reference(ref, evidence)
        stream = capture.get(name, {})
        require(canonical_path(ref["path"]) == capture_dir + "/" + name.lower() + ".raw"
                and type(stream.get("ObservedBytes")) is int and stream["ObservedBytes"] == len(raw)
                and type(stream.get("CapturedBytes")) is int and stream["CapturedBytes"] == len(raw)
                and stream.get("Eof") is True and all(stream.get(field) is False for field in
                    ("Truncated", "ReadFailed", "WriteFailed", "FlushFailed", "CloseFailed",
                     "DrainTimedOut", "ReaderDidNotStop", "RunReferenceConflict", "Failed")),
                "fresh held capture stream incomplete")
    require(clock(receipt["started_at"]) <= clock(fresh["observed_at"]) <= clock(receipt["finished_at"]),
            "fresh held capture clock differs")


def validate_partial_task_lineage(packet, prior, evidence, observed, operation, *, prior_evidence=None, fresh=None):
    """Join the exact first-task repin to independent held-fleet XML readback.

    The caller authenticates the enclosing failure, manifest and owner recovery.
    """
    operation = local_path(operation)
    prior_evidence = prior_evidence or evidence
    old, selected = identity(prior["old"]), identity(prior["new"])
    require(old == identity(packet["old"]), "partial task original identity differs")
    before = unique(prior.get("task_baseline"), "key", TASKS)
    after = unique(packet.get("task_baseline"), "key", TASKS)
    readback = unique(observed.get("tasks"), "key", TASKS)
    fresh_tasks = unique(fresh.get("tasks"), "key", TASKS) if fresh is not None else None
    for key, row in after.items():
        original, actual = before[key], readback[key]
        changed = key == "interra"
        pin = selected["sha"] if changed else old["sha"]
        require(row.get("pin_before") == pin and actual.get("pin_state") == ("selected" if changed else "original"),
                "partial task pin differs")
        raw = prior_evidence.raw(original["xml"])
        current = evidence.raw(row["xml"])
        require(xml_enabled(raw) is False and xml_enabled(current) is False
                and observed.get(canonical_path(str(operation / (key + "-before.xml")))) == raw,
                "partial task original XML differs")
        if changed:
            validate_xml_transition(raw, current, old["sha"], selected["sha"])
            require(observed.get(canonical_path(str(operation / (key + "-final.xml")))) == current,
                    "partial task final XML missing or differs")
        else:
            require(raw == current and canonical_path(str(operation / (key + "-final.xml"))) not in observed,
                    "unmodified partial task XML differs")
        reference = actual.get("xml_reference", {})
        path = canonical_path(reference.get("path"))
        require(observed.get(path) == current and reference.get("sha256") == actual.get("xml_sha256") == sha(current)
                and type(reference.get("bytes")) is int and reference["bytes"] == len(current),
                "partial task independent XML differs")
        actions = [{name: value.replace(old["sha"], pin) for name, value in action.items()}
                   for action in original["actions"]]
        for field in ("task_path", "task_name", "enabled", "last_task_result", "last_run_utc"):
            require(type(row.get(field)) is type(original.get(field)) and row[field] == original[field]
                    and type(actual.get(field)) is type(original[field]) and actual[field] == original[field],
                    "partial task history differs")
        for field in ("missed_runs", "next_run_utc"):
            require(type(actual.get(field)) is type(original.get(field)) and actual[field] == original[field],
                    "partial task historical clock differs")
        if fresh_tasks is None:
            require(row.get("missed_runs") == original.get("missed_runs")
                    and row.get("next_run_utc") == original.get("next_run_utc"), "partial task history differs")
        else:
            from cre_code_handoff_evidence import clock
            from cre_code_restoration_evidence import retained_reference

            current_task = fresh_tasks[key]
            for field in ("task_path", "task_name", "enabled", "last_task_result", "last_run_utc", "actions",
                          "missed_runs", "next_run_utc"):
                require(type(current_task.get(field)) is type(row.get(field)) and current_task[field] == row[field],
                        "fresh held task differs")
            reference = current_task.get("xml_reference")
            require(current_task.get("state") == "Disabled" and current_task.get("xml_sha256") == sha(current)
                    and retained_reference(reference, evidence) == current, "fresh held task XML differs")
            require(type(row["missed_runs"]) is int and type(original["missed_runs"]) is int
                    and row["missed_runs"] >= original["missed_runs"], "fresh missed count decreased")
            old_next, new_next, captured = clock(original["next_run_utc"]), clock(row["next_run_utc"]), clock(fresh["observed_at"])
            require(new_next >= old_next and captured < new_next
                    and (new_next == old_next or old_next <= captured), "fresh task trigger transition unsupported")
            require(new_next != old_next or row["missed_runs"] == original["missed_runs"],
                    "fresh missed count lacks crossed trigger")
        require(original.get("enabled") is False and actual.get("state") == "Disabled"
                and row.get("desired_enabled") is original.get("desired_enabled")
                and row.get("actions") == actual.get("actions") == actions,
                "partial task actions or desired state differs")


def validate_continuation_code(packet, git, source_repo):
    """Prove the resumed checkout is an ancestor and image inputs are unchanged."""
    old, before, selected = identity(packet.get("old")), runtime_before(packet), identity(packet.get("new"))
    native = identity(packet.get("native_before"))
    kind = packet.get("continuation", {}).get("failure_kind")
    require(native == before if kind is None else kind == "native-advanced-hold-only-v1" and native != before,
            "continuation requires aligned checkouts or an authenticated held-prefix failure")
    git.maintenance_ancestry(source_repo, old, before, selected)
    git.maintenance_ancestry(source_repo, old, native, selected)
    require(packet.get("expected_images") == {} and packet.get("image_tags") == {}
            and packet.get("install_scope") == [], "continuation must not rebuild images or install dependencies")
    current = git.entries(source_repo, selected["sha"])
    manifests = {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                 "requirements.txt", "pyproject.toml", "uv.lock"}
    for identity_before in (before, native):
        previous = git.entries(source_repo, identity_before["sha"])
        changed = {path for path in previous.keys() | current.keys() if previous.get(path) != current.get(path)}
        require(not any(not path.startswith(("scripts/firecrawl-ops/", "docs/firecrawl-ops/"))
                        or PurePosixPath(path).name in manifests for path in changed),
                "continuation changes image or dependency inputs")


def validate_continuation(packet, evidence, git, source_repo, *, live=False, output=None):
    """Qualify retained failed maintenance and its governed owner recovery."""
    if runtime_before(packet) == identity(packet.get("old")):
        require("continuation" not in packet and all("pin_before" not in row for row in packet.get("task_baseline", [])),
                "legacy maintenance cannot carry continuation evidence or task pins")
        return
    validate_continuation_code(packet, git, source_repo)
    continuation = packet.get("continuation")
    fields = {"failed_release", "failed_operation", "failure_manifest", "recovery_completed",
              "recovery_prepared", "recovery_expected_state", "current_stack", "current_stack_review"}
    fresh_fields = {"fresh_held_observation", "fresh_held_capture", "fresh_held_program"}
    prefix_fields = fields | fresh_fields | {"failure_kind"}
    require(isinstance(continuation, dict) and set(continuation) in (fields, fields | fresh_fields, prefix_fields),
            "complete continuation evidence required")
    hold_only = "failure_kind" in continuation
    require(not hold_only or continuation["failure_kind"] == "native-advanced-hold-only-v1",
            "held-prefix failure kind differs")
    roles = [value for key, value in continuation.items() if key not in {"failed_operation", "failure_kind"}]
    require(all(isinstance(role, str) and role in evidence.inputs for role in roles)
            and len(set(roles)) == len(roles), "continuation evidence roles differ")
    if hold_only:
        from cre_runtime_maintenance_prefix import validate_held_prefix

        if git is None:
            validate_held_prefix(packet, evidence, continuation)
        else:
            validate_held_prefix(packet, evidence, continuation, git=git)
    else:
        if git is None:
            validate_continuation_predecessor(packet, evidence, continuation)
        else:
            validate_continuation_predecessor(packet, evidence, continuation, git=git)
    from cre_current_stack import validate_current_stack, validate_current_stack_live

    receipt = evidence.obj(continuation["current_stack"])
    if live:
        require(output is not None, "fresh current-stack output required")
        validate_current_stack_live(packet, evidence, receipt, output)
    else:
        require(output is None, "retained current-stack validation has no output")
        if git is None:
            validate_current_stack(packet, evidence, receipt)
        else:
            validate_current_stack(packet, evidence, receipt, git=git)


def validate_continuation_predecessor(packet, evidence, continuation, *, git=None):
    """Bind the original failed operation and the exact governed recovery."""
    operation = local_path(continuation["failed_operation"])
    require(operation.is_absolute(), "absolute failed operation path required")
    prior_raw = evidence.raw(continuation["failed_release"])
    prior = json_bytes(prior_raw)
    partial = "continuation" in prior or "runtime_before" in prior
    if not partial:
        require("fresh_held_observation" not in continuation
                and all("pin_before" not in row for row in packet.get("task_baseline", [])),
                "task pins require authenticated partial failure")
    require(prior.get("schema") == "cre-runtime-maintenance-v1"
            and (not partial or ("continuation" in prior and "runtime_before" in prior
                                 and prior.get("expected_images") == {} and prior.get("install_scope") == []))
            and prior.get("old") == packet["old"] and prior.get("new") == runtime_before(packet)
            and prior.get("failed_handoff_release") == packet.get("failed_handoff_release")
            and all(canonical_path(prior["roots"][role]) == canonical_path(packet["roots"][role])
                    for role in ("native", "runtime")), "failed maintenance release differs")
    manifest_raw = evidence.raw(continuation["failure_manifest"])
    manifest = json_bytes(manifest_raw)
    require(manifest.get("schema") == "independent-maintenance-failure-audit-manifest-v1"
            and isinstance(manifest.get("files"), list), "failed maintenance audit missing")
    observed, references = {}, {}
    for ref in manifest["files"]:
        require(isinstance(ref, dict) and set(ref) == ({"path", "bytes", "sha256", "role"} if partial
                                                     else {"path", "bytes", "sha256"}),
                "failure audit reference malformed")
        path = canonical_path(ref["path"])
        require(path not in observed, "duplicate failure audit reference")
        raw = evidence.read(ref["path"])
        require(type(ref["bytes"]) is int and len(raw) == ref["bytes"] and sha(raw) == ref["sha256"],
                "failure audit evidence changed")
        evidence.original_payloads[ref["path"]] = raw
        observed[path] = raw
        references[path] = {key: ref[key] for key in ("path", "bytes", "sha256")}
    if partial:
        release_inputs = manifest.get("release_inputs")
        require(manifest.get("release_input_count") == len(prior["inputs"])
                and isinstance(release_inputs, list) and len(release_inputs) == len(prior["inputs"]),
                "partial failure release input inventory differs")
        expected_inputs = {row["role"]: row for row in prior["inputs"]}
        require(len(expected_inputs) == len(prior["inputs"]), "duplicate prior release inputs")
        for row in release_inputs:
            require(isinstance(row, dict) and set(row) == {"role", "path", "bytes", "sha256"}
                    and row.get("role") in expected_inputs, "partial release input malformed")
            original = expected_inputs.pop(row["role"])
            input_raw = evidence.read(row["path"])
            require(canonical_path(row["path"]) == canonical_path(original["path"])
                    and row["bytes"] == original["bytes"] and row["sha256"] == original["sha256"]
                    and len(input_raw) == row["bytes"] and sha(input_raw) == row["sha256"],
                    "partial release input differs")
            evidence.original_payloads[row["path"]] = input_raw
        require(not expected_inputs, "partial release input omitted")
    prior_path = canonical_path(evidence.inputs[continuation["failed_release"]]["path"])
    require(observed.get(prior_path) == prior_raw, "failure audit does not bind prior release")
    def audited(path):
        key = canonical_path(str(path))
        require(key in observed, "required failed operation evidence missing")
        return json_bytes(observed[key])

    failure = audited(operation / "failure.json")
    require(failure.get("schema") == "cre-runtime-maintenance-failure-v1"
            and failure.get("classification") == "precommit-failure"
            and (not partial or failure.get("original_error_type") == "HandoffError")
            and failure.get("original_error") == ("native-repin failed (1); preserve held tasks and recovery"
                                                  if partial else "container runtime configuration changed")
            and failure.get("release_sha256") == sha(prior_raw), "failed operation lineage differs")
    mutation = audited(operation / "mutation-start.json")
    require(mutation.get("schema") == "cre-runtime-maintenance-mutation-v1"
            and mutation.get("release_sha256") == sha(prior_raw)
            and canonical_path(mutation.get("operation_output")) == canonical_path(str(operation)),
            "failed maintenance mutation differs")
    git_rows = audited(operation / "git-observation.json")
    require(git_rows.get("schema") == "cre-runtime-maintenance-git-v1", "failed Git observation missing")
    for role in ("native", "runtime"):
        before_identity = prior[role + "_change"]["deployed"]
        before_row = git_rows.get(role, {}).get("before", {})
        tracking = {before_identity["sha"], prior["new"]["sha"]} if partial else {before_identity["sha"]}
        require({key: before_row.get(key) for key in ("sha", "tree")} == before_identity
                and before_row.get("status") == "" and before_row.get("tracking") in tracking
                and canonical_path(before_row.get("root")) == canonical_path(packet["roots"][role]),
                "failed maintenance original Git readback differs")
        row = git_rows.get(role, {}).get("after", {})
        require({key: row.get(key) for key in ("sha", "tree")} == runtime_before(packet)
                and row.get("status") == "" and row.get("tracking") == runtime_before(packet)["sha"]
                and canonical_path(row.get("root")) == canonical_path(packet["roots"][role]),
                "failed maintenance Git readback differs")
    expected = evidence.obj(continuation["recovery_expected_state"])
    prepared = evidence.obj(continuation["recovery_prepared"])
    completed = evidence.obj(continuation["recovery_completed"])
    require(expected.get("schema") == "cre-known-settled-owner-v1", "recovery state missing")
    canonical_lock = (local_path(packet["roots"]["runtime"])
                      / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock")
    require(canonical_path(expected.get("lock_path")) == canonical_path(str(canonical_lock)),
            "recovery lock is not the runtime canonical lock")
    fingerprint = expected.get("fingerprint")
    body = {key: value for key, value in expected.items() if key != "fingerprint"}
    require(fingerprint == sha(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()),
            "recovery fingerprint differs")
    authority_matches = [raw for path, raw in observed.items() if path.endswith("/authority-observation.json")]
    require(len(authority_matches) == 1, "independent failed-owner observation missing")
    owner = json_bytes(authority_matches[0])
    require(owner.get("owner_pid") == expected.get("owner_pid") and owner.get("proc_state") == "absent"
            and owner.get("recovery_required") is True and owner.get("lock_directory_present") is True
            and owner.get("pid_file_present") is True and owner.get("lease_file_present") is True
            and owner.get("authority_inode") == expected.get("authority", {}).get("identity")
            and owner.get("directory_inode") == expected.get("directory_identity")
            and owner.get("authority_sha256") == expected.get("authority", {}).get("sha256"),
            "recovery owner is not the failed maintenance owner")
    hold = audited(operation / "01-native-hold.json")
    argv = hold.get("argv")
    require(hold.get("schema") == "cre-code-command-v1"
            and hold.get("producer") == "checkpoint-run-command-v1"
            and hold.get("exit_code") == 0 and hold.get("local_settlement") == "confirmed"
            and hold.get("log_closed") is True and isinstance(argv, list), "failed operation hold not settled")
    flags = {"-Mode": "Hold", "-MaintenanceSha": sha(prior_raw), "-OwnerPid": str(owner["owner_pid"])}
    for key, value in flags.items():
        require(argv.count(key) == 1 and argv.index(key) + 1 < len(argv)
                and argv[argv.index(key) + 1] == value, "failed operation owner command differs")
    for key, value in (("-MaintenancePath", prior_path), ("-Output", str(operation))):
        require(argv.count(key) == 1 and argv.index(key) + 1 < len(argv)
                and canonical_path(argv[argv.index(key) + 1]) == canonical_path(value),
                "failed operation native path differs")
    log_path = canonical_path(str(operation / "01-native-hold.log"))
    require(log_path in observed and sha(observed[log_path]) == hold.get("command_log_sha256"),
            "failed operation hold log differs")
    _validate_failed_commands(packet, evidence, prior, prior_raw, operation, mutation, failure, owner,
                              hold, observed, references, partial=partial, git=git)
    if partial:
        native_path = [path for path in observed if path.endswith("/native-observation.json")]
        require(len(native_path) == 1, "partial native readback missing")
        native = json_bytes(observed[native_path[0]])
        require(native.get("schema") == "partial-maintenance-native-readback-v1"
                and native.get("release_sha256") == sha(prior_raw)
                and native.get("scheduler_instances") == [] and native.get("native_frontends") == [],
                "partial native readback differs")
        observed["tasks"] = native.get("tasks")
        from cre_code_handoff_evidence import Evidence, clock
        from cre_code_restoration_evidence import (
            restoration_execution,
            retained_reference,
        )

        prior_evidence = Evidence(prior, evidence.read)
        audit_dir = native_path[0].rsplit("/", 1)[0]
        execution_ref = audited(audit_dir + "/scheduler-execution-reference.json")
        execution = audited(audit_dir + "/scheduler-observation.json")
        require(canonical_path(execution_ref.get("path")) == audit_dir + "/scheduler-observation.json"
                and canonical_path(execution.get("watermark_reference", {}).get("path"))
                == canonical_path(str(operation / "scheduler-watermark.json"))
                and canonical_path(str(operation / "scheduler-watermark.json")) in observed,
                "partial Scheduler evidence differs")
        failure_hold = audited(operation / "01-native-failurehold.json")
        require(clock(hold["started_at"]) <= clock(execution["watermark"]["captured_at"])
                <= clock(mutation["entered_at"]) <= clock(failure_hold["finished_at"])
                <= clock(native["observed_at"]) <= clock(execution["started_at"])
                <= clock(execution["finished_at"]),
                "partial Scheduler readback precedes failure settlement")
        scheduler_observation = {"tasks": native["tasks"], "execution": execution_ref,
                                 "observed_at": execution["finished_at"]}
        outcomes = restoration_execution(prior, prior_evidence, scheduler_observation, legacy_task_references=True)
        require(all(row["state"] == "no-observed-execution" for row in outcomes.values()),
                "scheduled execution crossed partial maintenance")
        fresh = None
        if "fresh_held_observation" in continuation:
            fresh = evidence.obj(continuation["fresh_held_observation"])
            capture_role = continuation["fresh_held_capture"]
            capture = evidence.obj(capture_role)
            from cre_code_handoff_git import Git

            validate_fresh_held_capture(packet, prior, evidence, fresh, capture,
                                        git if git is not None else Git(packet["programs"]["native_git"]),
                                        packet["roots"]["development"], evidence.inputs[capture_role]["path"])
            require(fresh.get("schema") == "cre-held-partial-maintenance-observation-v1"
                    and fresh.get("frontends") == [] and fresh.get("instances") == []
                    and clock(fresh.get("observed_at")) >= clock(native["observed_at"]),
                    "fresh held observation differs")
            fresh_record = json_bytes(retained_reference(fresh.get("execution"), evidence))
            require(canonical_path(fresh_record.get("watermark_reference", {}).get("path"))
                    == canonical_path(str(operation / "scheduler-watermark.json"))
                    and clock(fresh_record.get("started_at")) >= clock(native["observed_at"]),
                    "fresh held Scheduler interval differs")
            # The execution parser compares its baseline clocks to the sampled
            # clocks. Keep the original packet untouched and supply those
            # observed clocks only for this no-execution classification; the
            # separate lineage check below validates the old-to-fresh change.
            sampled = unique(fresh.get("tasks"), "key", TASKS)
            execution_packet = dict(prior, task_baseline=[
                dict(row, missed_runs=sampled[row["key"]]["missed_runs"],
                     next_run_utc=sampled[row["key"]]["next_run_utc"])
                for row in prior["task_baseline"]])
            fresh_outcomes = restoration_execution(execution_packet, prior_evidence, fresh)
            require(all(row["state"] == "no-observed-execution" for row in fresh_outcomes.values()),
                    "scheduled execution crossed fresh held observation")
        validate_partial_task_lineage(packet, prior, evidence, observed, operation,
                                      prior_evidence=prior_evidence, fresh=fresh)
        before_containers = audited(operation / "containers-before.json")
        after_containers = audited(operation / "containers-after.json")
        independent = audited(audit_dir + "/containers-after.json")
        require(unique(before_containers, "service") == unique(after_containers, "service")
                == unique(independent, "service") == unique(evidence.obj("container_config_before"), "service"),
                "partial maintenance container baseline differs")
        prior_evidence.recheck()
        for role, raw in prior_evidence.original.items():
            evidence.original_payloads[prior_evidence.inputs[role]["path"]] = raw
        evidence.original_payloads.update(prior_evidence.original_payloads)
    archive = local_path(expected["lock_path"]).parent / ".cre-owner-recovery-forensics" / fingerprint
    require(completed.get("status") == "KNOWN_SETTLED_OWNER_RECOVERED" and completed.get("executed") is True
            and completed.get("expected_fingerprint") == fingerprint
            and completed.get("authority_identity") == expected.get("authority", {}).get("identity")
            and canonical_path(completed.get("archive")) == canonical_path(str(archive))
            and prepared.get("status") == "PREPARED_EXACT_KNOWN_SETTLED_RECOVERY"
            and prepared.get("expected") == expected and prepared.get("evidence") == completed.get("evidence")
            and prepared.get("operator") == completed.get("operator")
            and completed.get("operator") in {"cayman", "stace"}
            and isinstance(completed.get("reference"), str)
            and re.fullmatch(r"AGENTIC-[0-9]+", completed["reference"])
            and prepared.get("reference") == completed.get("reference"), "governed recovery differs")
    for role, name in (("recovery_completed", "completed.json"), ("recovery_prepared", "prepared.json")):
        require(canonical_path(evidence.inputs[continuation[role]]["path"]) == canonical_path(str(archive / name)),
                "recovery archive path differs")
    proofs = completed.get("evidence")
    require(isinstance(proofs, dict) and set(proofs) == {"known_outcome", "remote_settlement", "descendant_settlement"}
            and proofs["known_outcome"].get("sha256") == sha(manifest_raw), "recovery failure evidence differs")
    for role, proof in proofs.items():
        path = str(archive / (role + ".evidence"))
        raw = evidence.read(path)
        require(isinstance(proof, dict) and type(proof.get("size")) is int and len(raw) == proof["size"]
                and sha(raw) == proof.get("sha256"), "archived recovery evidence differs")
        evidence.original_payloads[path] = raw
    for name, value in (("authority-before", expected["authority"]), ("lock/pid", expected["pid"]),
                        ("lock/lease", expected["lease"])):
        path = str(archive / name)
        raw = evidence.read(path)
        require(len(raw) == value.get("size") and sha(raw) == value.get("sha256"),
                "archived owner evidence differs")
        evidence.original_payloads[path] = raw


def _validate_failed_commands(packet, evidence, prior, prior_raw, operation, mutation, failure, owner,
                              hold, observed, references, *, partial=False, hold_only=False, git=None):
    """Verify the seven actual settled commands and the original authorization."""
    from cre_code_handoff_evidence import DEPENDENCY_CHECKS, Evidence, clock
    from cre_code_handoff_git import windows_path
    from cre_runtime_maintenance import (
        validate_maintenance_authorization,
        validate_maintenance_release,
    )
    from cre_runtime_maintenance_steps import compose_argv
    from cre_runtime_maintenance_transition import validate_maintenance_command

    def arg(flag):
        argv = hold["argv"]
        require(argv.count(flag) == 1 and argv.index(flag) + 1 < len(argv), "native admission argument missing")
        return argv[argv.index(flag) + 1]

    original_observation, original_authorization = arg("-Observation"), arg("-Authorization")
    observation_key = canonical_path(original_observation)
    require(observation_key in observed, "original observation not in failure audit")
    observation_raw = observed[observation_key]
    authorization_raw = evidence.read(original_authorization)
    require(sha(authorization_raw) == mutation.get("authorization_sha256"), "original authorization differs")
    evidence.original_payloads[original_authorization] = authorization_raw
    prior_evidence = Evidence(prior, evidence.read)
    if git is None:
        validate_maintenance_release(prior, prior_evidence)
    else:
        validate_maintenance_release(prior, prior_evidence, git=git)
    validate_maintenance_authorization(prior_raw, prior, prior_evidence, observation_raw,
                                       json_bytes(authorization_raw), clock(mutation["entered_at"]),
                                       output=str(operation), review_reader=evidence.read)
    prior_evidence.recheck()
    for role, raw in prior_evidence.original.items():
        evidence.original_payloads[prior_evidence.inputs[role]["path"]] = raw
    evidence.original_payloads.update(prior_evidence.original_payloads)
    script = local_path(prior["roots"]["tooling"]) / "scripts/firecrawl-ops/cre_collector/windows/Invoke-CreCodeHandoff.ps1"
    prior_path = windows_path(local_path(evidence.inputs[packet["continuation"]["failed_release"]]["path"]))

    def native(mode):
        return [str(local_path(prior["programs"]["powershell"])), "-NoProfile", "-NonInteractive", "-File",
                windows_path(script), "-Mode", mode, "-MaintenancePath", prior_path,
                "-MaintenanceSha", sha(prior_raw), "-Output", windows_path(operation), "-OwnerPid",
                str(owner["owner_pid"]), "-Observation", windows_path(local_path(original_observation)),
                "-Authorization", windows_path(local_path(original_authorization))]

    collector = local_path(prior["roots"]["runtime"]) / "scripts/firecrawl-ops/cre_collector"
    expected = [("01-native-hold", native("Hold"), None, None)]
    if not partial and not hold_only:
        expected.append(("02-npm-ci", ["bash", "-c", 'cd -- "$1" && exec npm ci --ignore-scripts',
                                        "cre-maintenance", str(collector)], prior["new"], str(collector)))
    if not hold_only:
        for index, (name, args) in enumerate(DEPENDENCY_CHECKS.items(), start=2 if partial else 3):
            expected.append((f"{index:02d}-{name}", ["bash", "-c", 'cd -- "$1" && shift && exec "$@"',
                                                       "cre-maintenance", str(collector), *args],
                             prior["new"], str(collector)))
        expected.append(("05-native-repin", native("Repin"), None, None) if partial else
                        ("06-compose-up", compose_argv(prior, prior_evidence), None, None))
    expected.append(("01-native-failurehold", native("FailureHold"), None, None))
    receipts = []
    for name, argv, selected, cwd in expected:
        receipt_key = canonical_path(str(operation / (name + ".json")))
        log_key = canonical_path(str(operation / (name + ".log")))
        require(receipt_key in references and log_key in references, "failed command evidence missing")
        receipts.append(validate_maintenance_command({"receipt": references[receipt_key], "log": references[log_key]},
                                                     argv, selected, cwd, evidence.read,
                                                     retain=evidence.original_payloads,
                                                     failed_repin=partial and name == "05-native-repin"))
        if partial and name == "05-native-repin":
            require(b"maintenance observation expired/future" in observed[log_key],
                    "partial Repin failure cause differs")
    require(all(clock(left["finished_at"]) <= clock(right["started_at"])
                for left, right in pairwise(receipts)), "failed command chronology differs")
    require(clock(receipts[0]["started_at"]) <= clock(mutation["entered_at"]) <= clock(receipts[0]["finished_at"]),
            "failed mutation outside native hold")
    require(failure.get("failure_hold") == {"attempted": True, "invocation_completed": True, "error_type": None},
            "failure-hold invocation not settled")
    outer_key = canonical_path(str(operation) + ".outer-failure.json")
    stdout_key, stderr_key = (canonical_path(str(operation) + suffix) for suffix in (".stdout.raw", ".stderr.raw"))
    require(all(key in observed for key in (outer_key, stdout_key, stderr_key)), "native outer failure evidence missing")
    outer = json_bytes(observed[outer_key])
    require(outer.get("schema") == "cre-runtime-maintenance-outer-failure-v1"
            and outer.get("release_sha256") == sha(prior_raw)
            and (not (partial or hold_only) or (outer.get("original_error") == "Native foreground exit/drain uncertain"
                                 and outer.get("commit_and_recovery_state") == "unknown; independent readback required"))
            and canonical_path(outer.get("operation_output")) == canonical_path(str(operation)),
            "native outer failure differs")
