"""Code-only release contracts. No subprocesses, scheduler access or network.

Readers are explicit dependencies so tests use retained producer bytes without
loading operational scripts. The reviewed input is data, never executable policy.
"""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import PurePosixPath


class HandoffError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise HandoffError(message)


def unique(rows, field, expected=None):
    require(isinstance(rows, list), f"{field} rows must be a list")
    result = {}
    for row in rows:
        require(isinstance(row, dict) and isinstance(row.get(field), str),
                f"invalid {field} row")
        require(row[field] not in result, f"duplicate {field}")
        result[row[field]] = row
    if expected is not None:
        require(set(result) == set(expected), f"{field} set differs")
    return result


def json_bytes(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def clock(value):
    require(isinstance(value, str), "UTC clock missing")
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, "clock must include timezone")
    return parsed.astimezone(UTC)


def canonical_path(value):
    require(isinstance(value, str) and value and "\0" not in value, "invalid role path")
    value = value.replace("\\", "/").rstrip("/")
    if re.match(r"^/mnt/[a-z]/", value):
        value = value[5].upper() + ":/" + value[7:]
    if re.match(r"^[A-Za-z]:/", value):
        value = value[0].upper() + value[1:]
    require(value.startswith("/") or re.match(r"^[A-Z]:/", value), "absolute role path required")
    require(all(part not in {".", ".."} for part in value.split("/")), "role path traversal")
    return value


def role_paths(release):
    roots = {key: canonical_path(value) for key, value in release["roots"].items()}
    keys = {key: value.casefold() if re.match(r"^[A-Z]:/", value) else value for key, value in roots.items()}
    require(keys["native"] != keys["runtime"], "native/runtime alias")
    require(keys["tooling"] not in {keys["native"], keys["runtime"]}, "tooling aliases updated checkout")
    if keys["development"] == keys["tooling"]:
        require(release["development"] == release["tooling"], "co-located DEV/tooling identities differ")
    return roots


def validate_review(attestation, body, subject, scope):
    require(attestation.get("schema") == "cre-code-review-v1"
            and attestation.get("subject") == subject
            and attestation.get("scope") == scope
            and attestation.get("verdict") == "approved"
            and isinstance(attestation.get("reviewer"), str) and attestation["reviewer"]
            and isinstance(attestation.get("approval_ref"), str) and attestation["approval_ref"]
            and attestation.get("body", {}).get("sha256") == sha(body)
            and attestation["body"].get("bytes") == len(body), "review subject/scope/body binding differs")


def validate_qa_review(release, evidence, final):
    policy = release["qa"]
    attestation = evidence.obj(policy["review_attestation"])
    body = evidence.raw(policy["independent_review"])
    scope = policy.get("scope") or policy["expectations"]["scope"]
    validate_review(attestation, body, {"qa_identity": final, "tooling": release["tooling"]}, scope)


def identity(value):
    require(isinstance(value, dict) and set(value) == {"sha", "tree"},
            "identity must contain exact SHA and tree")
    require(all(isinstance(x, str) and re.fullmatch(r"[0-9a-f]{40}", x)
                for x in value.values()), "invalid Git identity")
    return value


def safe_relative(raw):
    require(isinstance(raw, str) and raw and "\\" not in raw and "\0" not in raw,
            "invalid relative path")
    parts = raw.split("/")
    forbidden = {".git", ".scratch", "out", "node_modules", "cache", "__pycache__"}
    require(not PurePosixPath(raw).is_absolute()
            and all(p not in {"", ".", ".."} and p.casefold() not in forbidden
                    and not p.casefold().startswith((".env", ".cre.lock", ".cre-quarantine"))
                    for p in parts), "protected changed path")
    return PurePosixPath(raw)


TASKS = {"interra": "Interra Refresh", "newmark": "Newmark Refresh",
         "qualified": "Qualified Brokerage Pipeline"}
TASK_PATH = "\\Agentic Assets\\CRE\\"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def validate_xml_transition(before, after, old, new):
    """Compare actual complete XML bytes, not a producer's equality boolean."""
    require(old != new and re.fullmatch(r"[0-9a-f]{40}", old)
            and re.fullmatch(r"[0-9a-f]{40}", new), "invalid transition pins")
    text = before.decode("utf-8-sig")
    root = ET.fromstring(text)
    actions = root.findall("t:Actions/*", NS)
    args = root.findall("t:Actions/t:Exec/t:Arguments", NS)
    require(len(actions) == len(args) == 1, "unexpected XML action shape")
    parameter = r'(?<!\S)(?:"-ExpectedSha"|-ExpectedSha)(?=\s|$)'
    binding = parameter + r'\s+(?:"' + old + r'"|' + old + r')(?=\s|$)'
    require(len(re.findall(parameter, args[0].text or "")) == 1
            and len(re.findall(binding, args[0].text or "")) == 1,
            "expected one decoded old action pin")
    require(before.count(old.encode()) == 2, "expected action and description pins")
    require(before.replace(old.encode(), new.encode()) == after,
            "task XML changed beyond SHA-only replacement")


class Evidence:
    def __init__(self, release, read):
        self.release, self.read = release, read
        self.inputs = unique(release.get("inputs"), "role")
        self.original = {}
        self.original_payloads = {}
        for role, row in self.inputs.items():
            require(set(row) == {"role", "path", "sha256", "bytes"}
                    and isinstance(row["path"], str)
                    and type(row["bytes"]) is int and row["bytes"] >= 0
                    and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]),
                    "invalid pinned input")
            self.original[role] = self.raw(role)

    def raw(self, role):
        require(role in self.inputs, f"missing input: {role}")
        row = self.inputs[role]
        raw = self.read(row["path"])
        require(sha(raw) == row["sha256"] and len(raw) == row["bytes"],
                f"changed input: {role}")
        return raw

    def obj(self, role):
        return json_bytes(self.raw(role))

    def recheck(self):
        for role, raw in self.original.items():
            require(self.raw(role) == raw, "input changed before publication")
        for path, raw in self.original_payloads.items():
            require(self.read(path) == raw, "QA manifest payload changed before publication")


def validate_release(release, evidence):
    require(release.get("schema") == "cre-code-release-v1", "unknown release schema")
    for role in ("deployed", "selected", "development", "tooling"):
        identity(release.get(role))
    require(release["selected"] != release["deployed"], "release already deployed")
    paths = release.get("allowed_paths")
    require(isinstance(paths, list) and paths and len(paths) == len(set(paths)),
            "missing/duplicate changed paths")
    for path in paths:
        safe_relative(path)
        require(PurePosixPath(path).name not in {
            "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
            "requirements.txt", "pyproject.toml", "uv.lock"},
            "dependency changes require a separate maintenance procedure")
        require(not path.startswith(("apps/", "scripts/firecrawl-ops/sql/")),
                "first route supports fork-owned code-only changes")
    require(evidence.raw("allowed_paths_nul") ==
            b"".join(p.encode() + b"\0" for p in paths), "NUL allowlist differs")
    require(set(release.get("roots", {})) == {"native", "runtime", "development", "tooling"},
            "explicit checkout roots missing")
    require(all(isinstance(value, str) and value and "\0" not in value
                for value in release["roots"].values()), "invalid checkout root")
    role_paths(release)
    require(set(release.get("programs", {})) == {"native_git", "powershell"},
            "explicit native tools missing")
    require(set(release.get("wsl", {})) == {"distro", "user"}
            and all(isinstance(v, str) and v for v in release["wsl"].values()), "explicit Ubuntu identity missing")
    preserved = release.get("preserve_files")
    require(isinstance(preserved, list) and len(preserved) == len(set(preserved))
            and all(isinstance(path, str) and path for path in preserved)
            and release.get("runtime_env_file") in preserved,
            "explicit private environment preservation reference missing")
    evidence.raw("bundle")
    unique(evidence.obj("runtime_snapshot").get("containers"), "id")
    unique(release.get("task_baseline"), "key", TASKS)
    validate_qa(release, evidence)
    validate_lineage(release, evidence)


def validate_qa(release, evidence):
    """Finite reader for the actual combined-candidate producer (no adapters)."""
    policy = release["qa"]
    if policy.get("producer") == "owned-gates-v1":
        validate_owned_qa(release, evidence)
        return
    require(policy.get("producer") == "combined-candidate-v1", "unknown QA producer")
    qa = evidence.obj(policy["summary"])
    final, broad = qa.get("final_candidate"), qa.get("broad_executed_candidate")
    require(isinstance(final, dict) and isinstance(broad, dict)
            and final == policy["final_executed"] and broad == policy["broad_executed"]
            and final.get("status") == broad.get("status") == ""
            and final.get("tree") == release["selected"]["tree"],
            "QA execution identities/tree differ")
    for field in ("scope", "full_python", "node", "ops", "final_focused_passed",
                  "reuse_boundary", "remaining_gates"):
        require(field in policy["expectations"]
                and qa.get(field) == policy["expectations"][field],
                f"QA reviewed {field} differs")
    require(qa["node"].get("failed") == 0 and qa["node"].get("includes_typecheck") is True
            and type(qa["final_focused_passed"]) is int and qa["final_focused_passed"] > 0,
            "QA required checks missing")
    phases = qa.get("phases")
    require(isinstance(phases, dict) and phases, "QA phase receipts missing")
    for phase in phases.values():
        receipt = phase.get("receipt", {})
        require(receipt.get("exit_code") == 0 and receipt.get("child_settled") is True
                and receipt.get("unchanged") is True
                and receipt.get("before") == receipt.get("after")
                and receipt.get("before") in (final, broad),
                "QA final execution/settlement changed")
        # The producer's hash refers to saved receipt bytes, not reserialized JSON.
        matches = [r for r in evidence.inputs if evidence.inputs[r]["sha256"] == phase.get("receipt_sha256")]
        require(len(matches) == 1 and evidence.obj(matches[0]) == receipt,
                "QA actual phase receipt absent")
    manifest = evidence.obj(policy["manifest"])
    require(isinstance(manifest, list) and manifest, "QA artifact manifest missing")
    unique(manifest, "path")
    for row in manifest:
        raw = evidence.read(row["path"])
        require(sha(raw) == row["sha256"] and len(raw) == row["bytes"],
                "QA manifest payload changed")
        evidence.original_payloads[row["path"]] = raw
    validate_qa_review(release, evidence, {"sha": final["head"], "tree": final["tree"]})
    merge = evidence.obj(policy["merge"])
    require(merge.get("state") == "MERGED"
            and merge.get("mergeCommit", {}).get("oid") == release["selected"]["sha"]
            and merge.get("candidateTree") == release["selected"]["tree"]
            and merge.get("originMainTree") == release["selected"]["tree"]
            and merge.get("verifiedOriginMain") == release["selected"]["sha"]
            and merge.get("candidateAncestryVerified") is True,
            "actual merge producer differs")


def validate_owned_qa(release, evidence):
    """Finite command-receipt consumer for new proof, not a historical adapter."""
    policy = release["qa"]
    gates = unique(policy.get("gates"), "name")
    require(gates and isinstance(policy.get("scope"), str) and policy["scope"], "QA scope missing")
    final = identity(policy["final_executed"])
    require(final["tree"] == release["selected"]["tree"], "final executed tree differs")
    for gate in gates.values():
        receipt = evidence.obj(gate["receipt"])
        executed = identity(gate["executed"])
        require(receipt.get("schema") == "cre-owned-gate-v1"
                and receipt.get("identity") == executed
                and receipt.get("argv") == gate["argv"] and gate["argv"]
                and receipt.get("cwd") == gate["cwd"]
                and receipt.get("exit_code") == 0
                and receipt.get("local_settlement") == "confirmed"
                and receipt.get("unchanged") is True, "owned QA command/settlement differs")
        producer = receipt.get("producer")
        require(producer in {"checkpoint-run-command-v1", "native-capture-v1"}, "unknown command producer")
        streams = ("command_log",)
        if producer == "native-capture-v1":
            require(receipt.get("stdout_eof") is True and receipt.get("stderr_eof") is True
                    and receipt.get("stream_failure") is False, "native stream drain uncertain")
            streams = ("stdout", "stderr")
        else:
            require(receipt.get("log_closed") is True, "merged command log not closed")
        for stream in streams:
            require(receipt.get(stream + "_sha256") == sha(evidence.raw(gate[stream])),
                    "owned QA output bytes differ")
        clock(receipt["started_at"])
        require(clock(receipt["finished_at"]) >= clock(receipt["started_at"]), "QA clocks reversed")
        if executed != final:
            reuse = gate.get("reuse")
            require(isinstance(reuse, dict) and reuse.get("review") in evidence.inputs,
                    "reused QA scope needs explicit review")
            require(reuse.get("executed") == executed and reuse.get("final") == final,
                    "reuse identities differ")
            require(isinstance(reuse.get("unchanged_inputs"), list) and reuse["unchanged_inputs"],
                    "reuse unchanged-input proof missing")
            for item in reuse["unchanged_inputs"]:
                require(item.get("executed_sha256") == item.get("final_sha256")
                        == sha(evidence.raw(item["input"])), "reused input changed")
            evidence.raw(reuse["review"])
        else:
            require("reuse" not in gate, "final execution must not claim reuse")
    validate_qa_review(release, evidence, final)


def validate_lineage(release, evidence):
    baseline = unique(release["task_baseline"], "key", TASKS)
    latest_tasks = None
    for key, row in baseline.items():
        require(row.get("task_path") == TASK_PATH and row.get("task_name") == TASKS[key]
                and type(row.get("enabled")) is bool
                and type(row.get("last_task_result")) is int
                and isinstance(row.get("actions"), list) and len(row["actions"]) == 1,
                "baseline task tuple malformed")
        evidence.raw(row["xml"])
        result = row["last_task_result"]
        if result == 0:
            continue
        failure = row.get("failure")
        require(isinstance(failure, dict), "nonzero result needs exact reviewed provenance")
        terminal = evidence.obj(failure["terminal"])
        settlement = evidence.obj(failure["settlement"])
        qualification = evidence.obj(failure["qualification"])
        original = evidence.raw(failure["xml"])
        operation = failure["operation"]
        require(terminal.get("operation") == operation, "terminal operation differs")
        native, linux = terminal.get("native", {}), terminal.get("linux", {})
        require(native.get("last_task_result") == result and native.get("task_state") == "Ready"
                and linux.get("parent_local_settlement") == "confirmed"
                and linux.get("guards_stopped") is True
                and linux.get("nested_cleanup_confirmed") is True
                and linux.get("canonical_lock_directory_exists") is False
                and linux.get("authority_state") == "normal"
                and terminal.get("evidence_files", {}).get("task_definition_sha256") == sha(original),
                "failed operation lacks terminal settlement")
        bound = settlement.get("operation", {})
        mapping = {"instance": "task_instance_id", "audit": "audit", "series": "series_id",
                   "run": "checkpoint_run_id", "parent": "parent_invocation_id", "child": "child_invocation_id"}
        require(all(bound.get(a) == operation.get(b) for a, b in mapping.items())
                and bound.get("task") == TASK_PATH + TASKS[key]
                and bound.get("last_run_utc") == row["last_run_utc"]
                and bound.get("task_result") == result and bound.get("task_xml_sha256") == sha(original),
                "settlement review tuple differs")
        qualified = qualification.get("operation", {})
        require(all(qualified.get(k) == operation[k] for k in
                    ("series_id", "checkpoint_run_id", "parent_invocation_id", "child_invocation_id", "task_instance_id"))
                and qualified.get("native_task_result") == result
                and qualified.get("task_definition_sha256") == sha(original),
                "partial qualification belongs to another operation")
        current = identity(failure["identity"])
        require(linux.get("git_head") == bound.get("code") == qualified.get("collector_git_sha") == current["sha"]
                and linux.get("git_tree") == bound.get("tree") == qualified.get("collector_git_tree") == current["tree"],
                "original failed code identity differs")
        seen = {current["sha"]}
        previous_fleet = None
        for edge in failure["transitions"]:
            require(edge["old"] == current, "missing/reordered lineage edge")
            following = identity(edge["new"])
            require(following["sha"] not in seen, "cyclic/duplicate lineage edge")
            xml_rows = unique(edge.get("xml"), "key", TASKS)
            before, after = evidence.raw(xml_rows[key]["before"]), evidence.raw(xml_rows[key]["after"])
            require(before == original, "lineage XML gap")
            validate_xml_transition(before, after, current["sha"], following["sha"])
            readback = evidence.obj(edge["readback"])
            if readback.get("schema") == "cre-code-transition-v1":
                from cre_code_handoff_transition import bound, validate_transition

                maintained = validate_transition(readback, current, following, evidence.read,
                                                 retain=evidence.original_payloads)
                provenance = unique(readback.get("original_failures"), "key")
                require(key in provenance and provenance[key].get("operation") == failure["operation"]
                        and provenance[key].get("terminal_sha256") == evidence.inputs[failure["terminal"]]["sha256"],
                        "maintained edge lost original failed provenance")
                fleet = {}
                for member, member_baseline in baseline.items():
                    prior = evidence.raw(xml_rows[member]["before"])
                    following_xml = evidence.raw(xml_rows[member]["after"])
                    require(bound(maintained[member]["before_xml"], evidence.read) == prior
                            and bound(maintained[member]["final_xml"], evidence.read) == following_xml,
                            "maintained edge XML role differs")
                    if previous_fleet is not None:
                        require(previous_fleet[member] == prior, "maintained fleet XML gap")
                    wanted = maintained[member]["after_tuple"]
                    require(type(wanted["last_task_result"]) is int and all(wanted[field] == member_baseline[field] for field in
                                (("enabled", "last_task_result", "last_run_utc", "next_run_utc") if member == key
                                 else ("enabled", "last_task_result"))),
                            "maintained edge failed task tuple changed")
                    fleet[member] = following_xml
                original, current, previous_fleet = after, following, fleet
                latest_tasks = {member: maintained[member]["after_tuple"] for member in baseline}
                seen.add(current["sha"])
                continue
            require(readback.get("read_only") is True and readback.get("all_checks_passed") is True
                    and readback.get("checks", {}).get("exact_three_task_lineage") is True
                    and readback.get("checks", {}).get("task_state_result_and_clocks_unchanged") is True
                    and readback.get("deployment_sha") == following["sha"]
                    and readback.get("deployment_tree") == following["tree"], "lineage readback identity differs")
            comparisons = unique(readback.get("task_comparisons"), "key", TASKS)
            tasks = unique(readback.get("tasks"), "key", TASKS)
            fleet = {}
            for member, member_baseline in baseline.items():
                prior = evidence.raw(xml_rows[member]["before"])
                following_xml = evidence.raw(xml_rows[member]["after"])
                if previous_fleet is not None:
                    require(previous_fleet[member] == prior, "whole fleet lineage XML gap")
                fleet[member] = following_xml
                validate_xml_transition(prior, following_xml, current["sha"], following["sha"])
                comparison, task = comparisons[member], tasks[member]
                enabled = member_baseline["enabled"]
                expected_state = "Ready" if enabled else "Disabled"
                require(comparison.get("prior_xml_sha256") == sha(prior)
                        and comparison.get("observed_xml_sha256") == sha(following_xml)
                        and comparison.get("raw_xml_equal_after_only_sha_replacement") is True
                        and comparison.get("enabled_before") is enabled
                        and comparison.get("enabled_after") is enabled
                        and comparison.get("state_before") == comparison.get("state_after") == expected_state
                        and comparison.get("result_before") == comparison.get("result_after") == member_baseline["last_task_result"]
                        and all(type(comparison.get(field)) is int for field in ("result_before", "result_after"))
                        and comparison.get("last_run_unchanged") is True
                        and comparison.get("next_run_unchanged") is True
                        and task.get("enabled") is enabled and task.get("state") == expected_state
                        and type(task.get("last_result")) is int and task["last_result"] == member_baseline["last_task_result"]
                        and clock(task.get("last_run_utc")) < clock(task.get("next_run_utc"))
                        and (member != key or all(task.get(field) == member_baseline[field]
                                                for field in ("last_run_utc", "next_run_utc")))
                        and task.get("xml_sha256") == sha(following_xml), "whole fleet lineage changed")
            comparison, task = comparisons[key], tasks[key]
            require(comparison.get("prior_xml_sha256") == sha(before)
                    and comparison.get("observed_xml_sha256") == sha(after)
                    and comparison.get("raw_xml_equal_after_only_sha_replacement") is True
                    and comparison.get("result_before") == comparison.get("result_after") == result
                    and comparison.get("last_run_unchanged") is True
                    and comparison.get("next_run_unchanged") is True
                    and task.get("last_result") == result
                    and task.get("last_run_utc") == row["last_run_utc"]
                    and task.get("xml_sha256") == sha(after), "lineage task changed")
            original, current = after, following
            previous_fleet = fleet
            latest_tasks = {member: {"enabled": task["enabled"], "last_task_result": task["last_result"],
                                    "last_run_utc": task["last_run_utc"], "next_run_utc": task["next_run_utc"]}
                            for member, task in tasks.items()}
            seen.add(current["sha"])
        require(current == release["deployed"] and original == evidence.raw(row["xml"]),
                "failure lineage does not reach deployed baseline")
        if previous_fleet is not None:
            require(all(previous_fleet[k] == evidence.raw(v["xml"]) for k, v in baseline.items()),
                    "whole fleet lineage does not reach current baseline")
    from cre_code_handoff_advancement import validate_advancements

    validate_advancements(release, evidence, latest_tasks)


def validate_observation(release, evidence, observation, now, *, selected=False, ownership_phase="released-clear"):
    from cre_code_handoff_host import qualify_host, qualify_native

    require(observation.get("schema") == "cre-code-observation-v1", "unknown observation schema")
    observed_at = clock(observation.get("observed_at"))
    qualified_at = clock(observation.get("qualified_at"))
    require(observed_at <= qualified_at <= now, "observation qualification clock differs")
    age = (now - observed_at).total_seconds()
    require(0 <= age < 300, "observation expired or from the future")
    expected = release["selected" if selected else "deployed"]
    require(observation.get("native") == observation.get("runtime") == expected,
            "old/new readiness identity mixup")
    require(ownership_phase in {"owned-maintenance", "released-clear"}
            and isinstance(observation.get("ownership"), dict)
            and observation["ownership"].get("verified") is True
            and observation["ownership"].get("recovery_required") is (ownership_phase == "owned-maintenance")
            and observation.get("ownership") == {
                "phase": ownership_phase, "verified": True,
                "recovery_required": ownership_phase == "owned-maintenance",
            } and observation.get("resources_ok") is True
            and observation.get("queues_idle") is True, "runtime admission refused")
    qualify_host(observation, evidence.obj("runtime_snapshot"))
    qualify_native(observation.get("windows_resources"))
    tasks = unique(observation.get("tasks"), "key", TASKS)
    for key, baseline in unique(release["task_baseline"], "key", TASKS).items():
        task = tasks[key]
        for field in ("task_path", "task_name", "enabled", "last_task_result", "last_run_utc", "next_run_utc"):
            require(type(task.get(field)) is type(baseline.get(field))
                    and task[field] == baseline[field], f"task {field} drift")
        require(task.get("state") == ("Ready" if baseline["enabled"] else "Disabled"),
                "task state drift")
        xml = evidence.raw(baseline["xml"])
        actions = baseline["actions"]
        if selected:
            xml = xml.replace(release["deployed"]["sha"].encode(), release["selected"]["sha"].encode())
            actions = [{name: value.replace(release["deployed"]["sha"], release["selected"]["sha"])
                        for name, value in action.items()} for action in actions]
        require(task.get("xml_sha256") == sha(xml), "complete task XML drift")
        require(task.get("actions") == actions, "exact task action tuple drift")
        if task["enabled"]:
            require((clock(task["next_run_utc"]) - now).total_seconds() >= 900,
                    "enabled task trigger too close")


def validate_authorization(release_raw, release, evidence, observation_raw, authorization, now, *, review_reader=None):
    require(authorization.get("schema") == "cre-code-authorization-v1"
            and authorization.get("release_sha256") == sha(release_raw)
            and authorization.get("observation_sha256") == sha(observation_raw),
            "authorization input binding differs")
    require(clock(authorization["authorized_at"]) <= now < clock(authorization["expires_at"]),
            "authorization expired")
    require((clock(authorization["expires_at"]) - clock(authorization["authorized_at"])).total_seconds() <= 300,
            "authorization lifetime exceeds admission")
    review = authorization.get("review")
    reader = review_reader or evidence.read
    require(isinstance(review, dict), "independent authorization review missing")
    raw = reader(review["path"])
    require(sha(raw) == review.get("sha256"), "review attestation changed")
    attestation = json_bytes(raw)
    validate_review(attestation, reader(attestation["body"]["path"]),
                    {"release_sha256": sha(release_raw), "tooling": release["tooling"]}, "code-only-release")
    require(review.get("reviewer") == attestation["reviewer"]
            and review.get("approval_ref") == attestation["approval_ref"], "reviewer/reference differs")
    evidence.recheck()
    validate_observation(release, evidence, json_bytes(observation_raw), now)
