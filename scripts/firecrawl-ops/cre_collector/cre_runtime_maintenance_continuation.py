"""Bound the no-image continuation of a failed held-fleet maintenance."""

import json
import re
from itertools import pairwise
from pathlib import PurePosixPath

from cre_code_handoff_evidence import canonical_path, identity, json_bytes, require, sha
from cre_code_handoff_git import local_path


def runtime_before(packet):
    """Keep legacy packets bound to the original task identity."""
    return identity(packet["runtime_before"] if "runtime_before" in packet else packet.get("old"))


def validate_continuation_code(packet, git, source_repo):
    """Prove the resumed checkout is an ancestor and image inputs are unchanged."""
    old, before, selected = identity(packet.get("old")), runtime_before(packet), identity(packet.get("new"))
    native = identity(packet.get("native_before"))
    require(native == before, "continuation requires aligned pre-update checkouts")
    git.maintenance_ancestry(source_repo, old, before, selected)
    git.maintenance_ancestry(source_repo, old, native, selected)
    require(packet.get("expected_images") == {} and packet.get("image_tags") == {}
            and packet.get("install_scope") == [], "continuation must not rebuild images or install dependencies")
    previous, current = git.entries(source_repo, before["sha"]), git.entries(source_repo, selected["sha"])
    changed = {path for path in previous.keys() | current.keys() if previous.get(path) != current.get(path)}
    manifests = {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                 "requirements.txt", "pyproject.toml", "uv.lock"}
    require(not any(not path.startswith(("scripts/firecrawl-ops/", "docs/firecrawl-ops/"))
                    or PurePosixPath(path).name in manifests for path in changed),
            "continuation changes image or dependency inputs")


def validate_continuation(packet, evidence, git, source_repo, *, live=False, output=None):
    """Qualify retained failed maintenance and its governed owner recovery."""
    if runtime_before(packet) == identity(packet.get("old")):
        require("continuation" not in packet, "legacy maintenance cannot carry continuation evidence")
        return
    validate_continuation_code(packet, git, source_repo)
    continuation = packet.get("continuation")
    fields = {"failed_release", "failed_operation", "failure_manifest", "recovery_completed",
              "recovery_prepared", "recovery_expected_state", "current_stack", "current_stack_review"}
    require(isinstance(continuation, dict) and set(continuation) == fields,
            "complete continuation evidence required")
    roles = [value for key, value in continuation.items() if key != "failed_operation"]
    require(all(isinstance(role, str) and role in evidence.inputs for role in roles)
            and len(set(roles)) == len(roles), "continuation evidence roles differ")
    validate_continuation_predecessor(packet, evidence, continuation)
    from cre_current_stack import validate_current_stack, validate_current_stack_live

    receipt = evidence.obj(continuation["current_stack"])
    if live:
        require(output is not None, "fresh current-stack output required")
        validate_current_stack_live(packet, evidence, receipt, output)
    else:
        require(output is None, "retained current-stack validation has no output")
        validate_current_stack(packet, evidence, receipt)


def validate_continuation_predecessor(packet, evidence, continuation):
    """Bind the original failed operation and the exact governed recovery."""
    operation = local_path(continuation["failed_operation"])
    require(operation.is_absolute(), "absolute failed operation path required")
    prior_raw = evidence.raw(continuation["failed_release"])
    prior = json_bytes(prior_raw)
    require(prior.get("schema") == "cre-runtime-maintenance-v1"
            and "continuation" not in prior and "runtime_before" not in prior
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
        require(isinstance(ref, dict) and set(ref) == {"path", "bytes", "sha256"}, "failure audit reference malformed")
        path = canonical_path(ref["path"])
        require(path not in observed, "duplicate failure audit reference")
        raw = evidence.read(ref["path"])
        require(type(ref["bytes"]) is int and len(raw) == ref["bytes"] and sha(raw) == ref["sha256"],
                "failure audit evidence changed")
        evidence.original_payloads[ref["path"]] = raw
        observed[path] = raw
        references[path] = ref
    prior_path = canonical_path(evidence.inputs[continuation["failed_release"]]["path"])
    require(observed.get(prior_path) == prior_raw, "failure audit does not bind prior release")
    def audited(path):
        key = canonical_path(str(path))
        require(key in observed, "required failed operation evidence missing")
        return json_bytes(observed[key])

    failure = audited(operation / "failure.json")
    require(failure.get("schema") == "cre-runtime-maintenance-failure-v1"
            and failure.get("classification") == "precommit-failure"
            and failure.get("original_error") == "container runtime configuration changed"
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
        require({key: before_row.get(key) for key in ("sha", "tree")} == before_identity
                and before_row.get("status") == "" and before_row.get("tracking") == before_identity["sha"]
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
                              hold, observed, references)
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
                              hold, observed, references):
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
    validate_maintenance_release(prior, prior_evidence)
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
    expected = [("01-native-hold", native("Hold"), None, None),
                ("02-npm-ci", ["bash", "-c", 'cd -- "$1" && exec npm ci --ignore-scripts',
                                "cre-maintenance", str(collector)], prior["new"], str(collector))]
    for index, (name, args) in enumerate(DEPENDENCY_CHECKS.items(), start=3):
        expected.append((f"{index:02d}-{name}", ["bash", "-c", 'cd -- "$1" && shift && exec "$@"',
                                                   "cre-maintenance", str(collector), *args],
                         prior["new"], str(collector)))
    expected += [("06-compose-up", compose_argv(prior, prior_evidence), None, None),
                 ("01-native-failurehold", native("FailureHold"), None, None)]
    receipts = []
    for name, argv, selected, cwd in expected:
        receipt_key = canonical_path(str(operation / (name + ".json")))
        log_key = canonical_path(str(operation / (name + ".log")))
        require(receipt_key in references and log_key in references, "failed command evidence missing")
        receipts.append(validate_maintenance_command({"receipt": references[receipt_key], "log": references[log_key]},
                                                     argv, selected, cwd, evidence.read,
                                                     retain=evidence.original_payloads))
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
            and canonical_path(outer.get("operation_output")) == canonical_path(str(operation)),
            "native outer failure differs")
