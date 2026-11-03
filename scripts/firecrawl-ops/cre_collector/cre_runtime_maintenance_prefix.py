"""Authenticate a specific failed Hold-only maintenance prefix, not a transition."""
import json
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
)
from cre_code_handoff_git import Git, local_path, windows_path
from cre_code_restoration_evidence import restoration_execution, retained_reference


def validate_held_prefix(packet, evidence, continuation, *, git=None):
    """Verify retained Hold-only failure, separate root readback and governed recovery."""
    from cre_runtime_maintenance_continuation import (
        _validate_failed_commands,
        runtime_before,
        validate_fresh_held_capture,
    )

    operation = local_path(continuation["failed_operation"])
    require(operation.is_absolute(), "held-prefix operation path differs")
    prior_role = continuation["failed_release"]
    prior_raw = evidence.raw(prior_role)
    prior = json_bytes(prior_raw)
    require(prior.get("schema") == "cre-runtime-maintenance-v1" and prior.get("old") == packet["old"]
            and prior.get("new") == packet["native_before"] and prior.get("runtime_before") == runtime_before(packet)
            and prior.get("expected_images") == {} and prior.get("image_tags") == {} and prior.get("install_scope") == []
            and prior.get("failed_handoff_release") == packet.get("failed_handoff_release")
            and all(canonical_path(prior["roots"][key]) == canonical_path(packet["roots"][key])
                    for key in ("native", "runtime")), "held-prefix predecessor differs")
    manifest_raw = evidence.raw(continuation["failure_manifest"])
    manifest = json_bytes(manifest_raw)
    require(manifest.get("schema") == "independent-maintenance-failure-audit-manifest-v1"
            and isinstance(manifest.get("files"), list), "held-prefix audit missing")
    observed, refs, operation_names = {}, {}, set()
    operation_path = canonical_path(str(operation))
    for row in manifest["files"]:
        require(isinstance(row, dict) and set(row) == {"path", "bytes", "sha256", "role"}, "held-prefix reference differs")
        path = canonical_path(row["path"])
        require(path not in observed, "duplicate held-prefix reference")
        raw = evidence.read(row["path"])
        require(type(row["bytes"]) is int and len(raw) == row["bytes"] and sha(raw) == row["sha256"],
                "held-prefix audit bytes changed")
        evidence.original_payloads[row["path"]] = raw
        observed[path], refs[path] = raw, {key: row[key] for key in ("path", "bytes", "sha256")}
        if path.startswith(operation_path + "/"):
            require(row["role"] == "operation" and "/" not in path[len(operation_path) + 1:],
                    "held-prefix operation inventory differs")
            operation_names.add(path[len(operation_path) + 1:])
    expected_names = {"01-native-hold.json", "01-native-hold.log", "01-native-failurehold.json", "01-native-failurehold.log",
                      "containers-before.json", "failure-hold.json", "failure.json", "mutation-start.json",
                      "native-hold.json", "scheduler-watermark.json", "interra-before.xml", "newmark-before.xml",
                      "qualified-before.xml", "hold-windows-admission.capture.json", "hold-windows-admission.stderr.raw",
                      "hold-windows-admission.stdout.raw"}
    require(operation_names == expected_names, "held-prefix includes missing or later-phase artifacts")
    inputs = manifest.get("release_inputs")
    require(isinstance(inputs, list) and manifest.get("release_input_count") == len(prior["inputs"]) == len(inputs),
            "held-prefix release inventory differs")
    remaining = {row["role"]: row for row in prior["inputs"]}
    require(len(remaining) == len(inputs), "duplicate held-prefix release role")
    for row in inputs:
        require(isinstance(row, dict) and set(row) == {"role", "path", "bytes", "sha256"}
                and row.get("role") in remaining, "held-prefix release role differs")
        wanted = remaining.pop(row["role"])
        raw = evidence.read(row["path"])
        require(canonical_path(row["path"]) == canonical_path(wanted["path"])
                and row["bytes"] == wanted["bytes"] and row["sha256"] == wanted["sha256"]
                and len(raw) == row["bytes"] and sha(raw) == row["sha256"], "held-prefix release input changed")
        evidence.original_payloads[row["path"]] = raw
    require(not remaining and observed.get(canonical_path(evidence.inputs[prior_role]["path"])) == prior_raw,
            "held-prefix release not audited")
    def audited(path):
        key = canonical_path(str(path))
        require(key in observed, "held-prefix required evidence missing")
        return json_bytes(observed[key])
    def unique_path(suffix):
        paths = [path for path in observed if path.endswith(suffix)]
        require(len(paths) == 1, "held-prefix independent evidence missing")
        return paths[0]

    def validate_failure_and_settlement():
        failure, mutation = audited(operation / "failure.json"), audited(operation / "mutation-start.json")
        require(failure.get("schema") == "cre-runtime-maintenance-failure-v1" and failure.get("classification") == "precommit-failure"
                and failure.get("original_error_type") == "HandoffError" and failure.get("original_error") == "maintenance observation expired/future"
                and failure.get("release_sha256") == sha(prior_raw), "held-prefix failure differs")
        require(mutation.get("schema") == "cre-runtime-maintenance-mutation-v1" and mutation.get("release_sha256") == sha(prior_raw)
                and canonical_path(mutation.get("operation_output")) == operation_path, "held-prefix mutation differs")
        expected = evidence.obj(continuation["recovery_expected_state"])
        expected_role = continuation["recovery_expected_state"]
        require(expected.get("schema") == "cre-known-settled-owner-v1"
                and observed.get(canonical_path(evidence.inputs[expected_role]["path"])) == evidence.raw(expected_role),
                "held-prefix owner state differs")
        fingerprint = expected.get("fingerprint")
        body = {key: value for key, value in expected.items() if key != "fingerprint"}
        require(fingerprint == sha(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()), "held-prefix owner fingerprint differs")
        lock = local_path(packet["roots"]["runtime"]) / "scripts/firecrawl-ops/cre_collector/out/daily/.cre.lock"
        require(canonical_path(expected.get("lock_path")) == canonical_path(str(lock)), "held-prefix canonical lock differs")
        linux = audited(unique_path("/linux-observation-01.json"))
        require(linux.get("schema") == "cre-known-owner-linux-settlement-observation-v1"
                and linux.get("fingerprint") == fingerprint and linux.get("owner_pid") == expected.get("owner_pid")
                and linux.get("expected_state_matches") is True and linux.get("owner_exists") is False
                and linux.get("owner_group_members") == [] and linux.get("unreadable_same_uid_pids") == []
                and linux.get("visible_competing_roles") == [], "held-prefix owner did not settle")
        owner = {"owner_pid": expected["owner_pid"]}
        hold = audited(operation / "01-native-hold.json")
        _validate_failed_commands(packet, evidence, prior, prior_raw, operation, mutation, failure, owner,
                                  hold, observed, refs, hold_only=True, git=git)
        finished_hold = audited(operation / "01-native-failurehold.json")
        launcher_path = unique_path("/03-apply.json")
        launcher = audited(launcher_path)
        launcher_dir = launcher_path.rsplit("/", 1)[0]
        stdout_path, stderr_path = launcher_dir + "/03-apply.stdout.raw", launcher_dir + "/03-apply.stderr.raw"
        require(stdout_path in observed and stderr_path in observed, "held-prefix launcher streams missing")
        hold_argv = hold.get("argv", [])
        def argument(flag):
            require(hold_argv.count(flag) == 1 and hold_argv.index(flag) + 1 < len(hold_argv),
                    "held-prefix Hold argument missing")
            return hold_argv[hold_argv.index(flag) + 1]
        source = local_path(prior["roots"]["tooling"]) / "scripts/firecrawl-ops/cre_collector/windows/Invoke-CreCodeHandoff.ps1"
        expected_argv = ["-NoProfile", "-NonInteractive", "-File", windows_path(source), "-Mode", "Apply",
                         "-MaintenancePath", windows_path(local_path(evidence.inputs[prior_role]["path"])),
                         "-MaintenanceSha", sha(prior_raw), "-Observation", argument("-Observation"),
                         "-Authorization", argument("-Authorization"), "-Output", windows_path(operation)]
        capture = launcher.get("capture", {})
        require(launcher.get("schema") == "reviewed-maintenance-launch-stage-v1" and launcher.get("stage") == "03-apply"
                and launcher.get("release_sha256") == sha(prior_raw)
                and canonical_path(launcher.get("program")) == canonical_path(prior["programs"]["powershell"])
                and launcher.get("argv") == expected_argv
                and launcher.get("stdout_sha256") == sha(observed[stdout_path])
                and launcher.get("stderr_sha256") == sha(observed[stderr_path])
                and capture.get("Started") is True and capture.get("Failed") is False
                and type(capture.get("NativeExitCode")) is int and capture["NativeExitCode"] == 1
                and all(capture.get(name, {}).get("Eof") is True and capture[name].get("Truncated") is False
                        and capture[name].get("Failed") is False and type(capture[name].get("ObservedBytes")) is int
                        and capture[name]["ObservedBytes"] == len(observed[path])
                        and type(capture[name].get("CapturedBytes")) is int and capture[name]["CapturedBytes"] == len(observed[path])
                        for name, path in (("Stdout", stdout_path), ("Stderr", stderr_path)))
                and clock(launcher["started_at"]) <= clock(hold["started_at"])
                <= clock(finished_hold["finished_at"]) <= clock(launcher["finished_at"]),
                "held-prefix launcher capture differs")
        require(clock(finished_hold["finished_at"]) <= clock(linux["observed_at"]), "held-prefix owner observation predates settlement")
        failure_hold = audited(operation / "failure-hold.json")
        held = unique(failure_hold.get("tasks"), "key", TASKS)
        prior_tasks = unique(prior["task_baseline"], "key", TASKS)
        require(failure_hold.get("schema") == "cre-runtime-maintenance-failure-hold-v1" and failure_hold.get("attempted") is True
                and failure_hold.get("concurrent_native_frontends") == [] and failure_hold.get("visibility_error") is None
                and all(row.get("disable_attempted") is True and row.get("disabled") is True and row.get("state") == "Disabled"
                        and row.get("error") is None and row.get("task_path") == prior_tasks[key]["task_path"]
                        and row.get("task_name") == prior_tasks[key]["task_name"] for key, row in held.items()),
                "held-prefix failure hold differs")
        return expected, fingerprint, lock, hold, finished_hold, mutation, prior_tasks

    expected, fingerprint, lock, hold, finished_hold, mutation, prior_tasks = validate_failure_and_settlement()

    def validate_git_and_host_readback():
        git_observation = audited(unique_path("/git-readback.json"))
        require(git_observation.get("schema") == "independent-post-failure-git-readback-v1"
                and clock(git_observation.get("observed_at")) >= clock(finished_hold["finished_at"]), "held-prefix Git readback differs")
        for key, wanted in (("native", packet["native_before"]), ("runtime", runtime_before(packet))):
            row = git_observation.get("roots", {}).get(key, {})
            require(row.get("head") == wanted["sha"] and row.get("tree") == wanted["tree"]
                    and row.get("tracking") == wanted["sha"] and row.get("branch") == "main" and row.get("status") == "",
                    "held-prefix current Git identity differs")
        host = audited(unique_path("/host-observation-01.json"))
        before = audited(operation / "containers-before.json")
        require(clock(host.get("observed_at")) >= clock(finished_hold["finished_at"])
                and host.get("queues") == {"jobs_in_queue": 0, "active_jobs_in_queue": 0,
                                            "waiting_jobs_in_queue": 0, "active_crawl_count": 0}
                and all(type(value) is int for value in host["queues"].values())
                and host.get("browser", {}).get("status") == "healthy"
                and type(host["browser"].get("activePages")) is int and host["browser"]["activePages"] == 0
                and unique(before, "service") == unique(host.get("normalized_containers"), "service")
                == unique(evidence.obj("container_config_before"), "service"), "held-prefix container readback differs")

    validate_git_and_host_readback()

    def validate_native_and_fresh_readback():
        native = audited(unique_path("/native/observation.json"))
        require(native.get("schema") == "cre-held-partial-maintenance-observation-v1"
                and native.get("frontends") == [] and native.get("instances") == []
                and clock(native.get("observed_at")) >= clock(finished_hold["finished_at"]), "held-prefix native readback differs")
        native_tasks = unique(native.get("tasks"), "key", TASKS)
        prior_evidence = Evidence(prior, evidence.read)
        current_tasks = unique(packet["task_baseline"], "key", TASKS)
        for key, row in current_tasks.items():
            old, sampled = prior_tasks[key], native_tasks[key]
            old_xml = prior_evidence.raw(old["xml"])
            current_xml = evidence.raw(row["xml"])
            require(observed.get(canonical_path(str(operation / (key + "-before.xml")))) == old_xml == current_xml
                    and retained_reference(sampled.get("xml_reference"), evidence) == current_xml
                    and sampled.get("xml_sha256") == sha(current_xml), "held-prefix complete XML differs")
            require(row.get("pin_before") == old.get("pin_before") and row.get("desired_enabled") is old.get("desired_enabled")
                    and sampled.get("state") == "Disabled" and row.get("enabled") is False, "held-prefix task flags differ")
            for field in ("task_path", "task_name", "enabled", "actions", "last_task_result", "last_run_utc"):
                require(type(row.get(field)) is type(old.get(field)) is type(sampled.get(field))
                        and row[field] == old[field] == sampled[field], "held-prefix task tuple differs")
            for field in ("missed_runs", "next_run_utc"):
                require(type(sampled.get(field)) is type(old.get(field)) and sampled[field] == old[field],
                        "held-prefix historical clock differs")
        execution_ref = native.get("execution")
        execution_raw = retained_reference(execution_ref, prior_evidence)
        execution = json_bytes(execution_raw)
        require(canonical_path(execution_ref.get("path")) in observed
                and observed[canonical_path(execution_ref["path"])] == execution_raw
                and canonical_path(execution.get("watermark_reference", {}).get("path")) == operation_path + "/scheduler-watermark.json"
                and clock(hold["started_at"]) <= clock(execution["watermark"]["captured_at"]) <= clock(mutation["entered_at"])
                <= clock(finished_hold["finished_at"]) <= clock(execution["started_at"]), "held-prefix Scheduler interval differs")
        outcomes = restoration_execution(prior, prior_evidence, native)
        require(all(row["state"] == "no-observed-execution" for row in outcomes.values()), "execution crossed held-prefix failure")
        fresh_role, capture_role = continuation["fresh_held_observation"], continuation["fresh_held_capture"]
        fresh, capture = evidence.obj(fresh_role), evidence.obj(capture_role)
        validate_fresh_held_capture(packet, prior, evidence, fresh, capture,
                                    git if git is not None else Git(packet["programs"]["native_git"]),
                                    packet["roots"]["development"], evidence.inputs[capture_role]["path"])
        require(fresh.get("schema") == "cre-held-partial-maintenance-observation-v1" and fresh.get("frontends") == []
                and fresh.get("instances") == [] and clock(fresh["observed_at"]) >= clock(native["observed_at"]),
                "held-prefix fresh readback differs")
        fresh_tasks = unique(fresh.get("tasks"), "key", TASKS)
        for key, row in current_tasks.items():
            sampled = fresh_tasks[key]
            require(sampled.get("state") == "Disabled" and sampled.get("xml_sha256") == sha(evidence.raw(row["xml"]))
                    and retained_reference(sampled.get("xml_reference"), evidence) == evidence.raw(row["xml"]),
                    "held-prefix fresh XML differs")
            for field in ("task_path", "task_name", "enabled", "actions", "last_task_result", "last_run_utc", "missed_runs", "next_run_utc"):
                require(type(row.get(field)) is type(sampled.get(field)) and row[field] == sampled[field], "held-prefix fresh tuple differs")
            original = prior_tasks[key]
            require(type(row["missed_runs"]) is int and type(original["missed_runs"]) is int
                    and row["missed_runs"] >= original["missed_runs"], "held-prefix missed count decreased")
            old_next, new_next, captured = clock(original["next_run_utc"]), clock(row["next_run_utc"]), clock(fresh["observed_at"])
            require(new_next >= old_next and captured < new_next and (new_next == old_next or old_next <= captured)
                    and (new_next != old_next or row["missed_runs"] == original["missed_runs"]),
                    "held-prefix fresh trigger transition unsupported")
        fresh_record = json_bytes(retained_reference(fresh.get("execution"), evidence))
        require(canonical_path(fresh_record.get("watermark_reference", {}).get("path")) == operation_path + "/scheduler-watermark.json"
                and clock(fresh_record.get("started_at")) >= clock(native["observed_at"]), "held-prefix fresh interval differs")
        sampled_baseline = dict(prior, task_baseline=[
            dict(row, missed_runs=fresh_tasks[row["key"]]["missed_runs"],
                 next_run_utc=fresh_tasks[row["key"]]["next_run_utc"]) for row in prior["task_baseline"]])
        outcomes = restoration_execution(sampled_baseline, prior_evidence, fresh)
        require(all(row["state"] == "no-observed-execution" for row in outcomes.values()), "execution crossed fresh held-prefix readback")
        prior_evidence.recheck()
        evidence.original_payloads.update(prior_evidence.original_payloads)
        evidence.original_payloads.update({prior_evidence.inputs[role]["path"]: raw for role, raw in prior_evidence.original.items()})


    validate_native_and_fresh_readback()

    def validate_governed_recovery():
        expected_archive = lock.parent / ".cre-owner-recovery-forensics" / fingerprint
        prepared = evidence.obj(continuation["recovery_prepared"])
        completed = evidence.obj(continuation["recovery_completed"])
        require(prepared.get("status") == "PREPARED_EXACT_KNOWN_SETTLED_RECOVERY" and prepared.get("expected") == expected
                and completed.get("status") == "KNOWN_SETTLED_OWNER_RECOVERED" and completed.get("executed") is True
                and completed.get("expected_fingerprint") == fingerprint
                and completed.get("authority_identity") == expected["authority"]["identity"]
                and canonical_path(completed.get("archive")) == canonical_path(str(expected_archive))
                and prepared.get("evidence") == completed.get("evidence") and prepared.get("operator") == completed.get("operator")
                and completed.get("operator") in {"cayman", "stace"} and prepared.get("reference") == completed.get("reference")
                and isinstance(completed.get("reference"), str) and re.fullmatch(r"AGENTIC-[0-9]+", completed["reference"]),
                "held-prefix governed recovery differs")
        for role, name in (("recovery_prepared", "prepared.json"), ("recovery_completed", "completed.json")):
            require(canonical_path(evidence.inputs[continuation[role]]["path"]) == canonical_path(str(expected_archive / name)),
                    "held-prefix recovery path differs")
        proofs = completed.get("evidence")
        require(isinstance(proofs, dict) and set(proofs) == {"known_outcome", "remote_settlement", "descendant_settlement"}
                and proofs["known_outcome"].get("sha256") == sha(manifest_raw), "held-prefix recovery evidence differs")
        for role, proof in proofs.items():
            path = str(expected_archive / (role + ".evidence"))
            raw = evidence.read(path)
            require(type(proof.get("size")) is int and len(raw) == proof["size"] and sha(raw) == proof.get("sha256"),
                    "held-prefix archived evidence differs")
            evidence.original_payloads[path] = raw
        for name, value in (("authority-before", expected["authority"]), ("lock/pid", expected["pid"]), ("lock/lease", expected["lease"])):
            path = str(expected_archive / name)
            raw = evidence.read(path)
            require(len(raw) == value.get("size") and sha(raw) == value.get("sha256"), "held-prefix archived owner differs")
            evidence.original_payloads[path] = raw

    validate_governed_recovery()
