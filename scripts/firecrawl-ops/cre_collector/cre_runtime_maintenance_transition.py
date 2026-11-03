"""Build and validate retained maintenance operation evidence."""

from itertools import pairwise

from cre_code_handoff_evidence import (
    TASKS,
    canonical_path,
    clock,
    identity,
    json_bytes,
    require,
    sha,
    unique,
)

TUPLE_FIELDS = ("task_path", "task_name", "enabled", "last_task_result", "last_run_utc", "next_run_utc", "actions")


def reference(path, read):
    raw = read(path)
    return {"path": str(path), "sha256": sha(raw), "bytes": len(raw)}


def validate_maintenance_command(row, argv, selected, cwd, read, *, retain=None, failed_repin=False):
    """Consume actual settled foreground receipt and its unchanged log bytes."""
    def payload(ref):
        require(isinstance(ref, dict) and set(ref) == {"path", "sha256", "bytes"}
                and type(ref["bytes"]) is int and ref["bytes"] >= 0, "invalid command reference")
        canonical_path(ref["path"])
        raw = read(ref["path"])
        require(len(raw) == ref["bytes"] and sha(raw) == ref["sha256"], "command payload changed")
        if retain is not None:
            retain[ref["path"]] = raw
        return raw

    require(type(failed_repin) is bool and (not failed_repin or
            (selected is None and cwd is None and argv.count("-Mode") == 1
             and argv.index("-Mode") + 1 < len(argv) and argv[argv.index("-Mode") + 1] == "Repin")),
            "unsupported command outcome")
    receipt = json_bytes(payload(row["receipt"]))
    log = payload(row["log"])
    require(receipt.get("producer") == "checkpoint-run-command-v1" and receipt.get("argv") == argv
            and type(receipt.get("exit_code")) is int and receipt["exit_code"] == (1 if failed_repin else 0)
            and receipt.get("local_settlement") == "confirmed" and receipt.get("log_closed") is True
            and receipt.get("command_log_sha256") == sha(log)
            and clock(receipt.get("started_at")) <= clock(receipt.get("finished_at")),
            "maintenance command did not settle")
    if selected is None:
        require(cwd is None and receipt.get("schema") == "cre-code-command-v1"
                and receipt.get("effective_cwd") is None, "maintenance native command scope differs")
    else:
        require(receipt.get("schema") == "cre-owned-gate-v1" and receipt.get("identity") == identity(selected)
                and receipt.get("unchanged") is True and canonical_path(receipt.get("cwd")) == canonical_path(cwd)
                and canonical_path(receipt.get("effective_cwd")) == canonical_path(cwd),
                "maintenance collector command scope differs")
    return receipt


def validate_maintenance_git_history(git, repo, scope):
    """Recheck reviewed object changes without depending on the current checkout HEAD."""
    from pathlib import PurePosixPath

    from cre_code_handoff_evidence import safe_relative

    old, new = identity(scope["deployed"]), identity(scope["selected"])
    for value in (old, new):
        require(git.text(repo, "rev-parse", value["sha"] + "^{tree}") == value["tree"],
                "maintenance historical commit tree differs")
    git.command(repo, "merge-base", "--is-ancestor", old["sha"], new["sha"])
    paths = scope["allowed_paths"]
    require(git.command(repo, "diff", "--no-renames", "--name-only", "-z", old["sha"], new["sha"])
            == b"".join(path.encode() + b"\0" for path in paths), "maintenance historical diff differs")
    before, after = git.entries(repo, old["sha"]), git.entries(repo, new["sha"])
    require(set(scope["objects"]) == set(paths), "maintenance historical object set differs")
    for path in paths:
        safe_relative(path)
        require(scope["objects"][path] == {"before": before.get(path), "after": after.get(path)},
                "maintenance historical object differs")
        require(all(entry is None or (entry["mode"] in {"100644", "100755"} and entry["type"] == "blob")
                    for entry in (before.get(path), after.get(path))), "maintenance historical object type differs")
    names = {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "requirements.txt", "pyproject.toml", "uv.lock"}
    dependencies = {path for path in set(before) | set(after)
                    if PurePosixPath(path).name in names and before.get(path) != after.get(path)}
    require(dependencies == set(scope["changed_dependency_paths"]), "maintenance historical dependency diff differs")


def validate_maintenance_image_review(packet, evidence):
    """Bind the selected configuration and generated Compose overlay to review."""
    from cre_runtime_maintenance_build import reviewed_override

    require(evidence.obj("expected_after_config") == packet["expected_after_config"]
            and evidence.raw("compose_override") == reviewed_override(packet["image_tags"]),
            "maintenance image configuration/overlay review differs")


def validate_maintenance_projection_review(packet, evidence, reference_value):
    """Retain the real pre-lease projection and bind it to reviewed role bytes."""
    from cre_code_handoff_git import local_path
    from cre_code_restoration_evidence import retained_reference

    require(canonical_path(reference_value.get("path")) == canonical_path(str(
        local_path(packet["projection_output"]) / "configuration-projection.json")), "maintenance projection path differs")
    projection = json_bytes(retained_reference(reference_value, evidence))
    require(projection.get("schema") == "cre-image-configuration-projection-v1"
            and projection.get("identity") == packet["new"]
            and projection.get("docker_sha256") == packet["native_docker_sha256"]
            and projection.get("changed_image_default_keys") == packet["allowed_image_default_changes"],
            "maintenance projection identity differs")
    for role in ("container_config_before", "expected_after_config", "compose_sources", "compose_override"):
        require(retained_reference(projection.get(role), evidence) == evidence.raw(role),
                "maintenance projection reviewed input differs")
    validate_maintenance_image_review(packet, evidence)


def validate_maintenance_dependency_history(git, repo, selected, dependencies):
    """Bind installed manifest and lock hashes to the reviewed Git commit."""
    selected = identity(selected)
    for name in ("package.json", "package-lock.json"):
        blob = git.command(repo, "show", selected["sha"] + ":scripts/firecrawl-ops/cre_collector/" + name)
        require(dependencies.get(name) == sha(blob), "installed dependency manifest differs from selected source")


def validate_maintenance_tracking_history(git, repo, deployed, tracking, selected):
    """Prove the old tracking ref lay on the reviewed forward path."""
    import re

    require(isinstance(tracking, str) and re.fullmatch(r"[0-9a-f]{40}", tracking), "invalid maintenance tracking SHA")
    git.command(repo, "merge-base", "--is-ancestor", identity(deployed)["sha"], tracking)
    git.command(repo, "merge-base", "--is-ancestor", tracking, identity(selected)["sha"])


def validate_maintenance_execution(packet, evidence, observation, *, hold_at, mutation_at, terminal_at):
    """Reuse bounded Scheduler history, refusing every observed task execution."""
    from cre_code_restoration_evidence import restoration_execution, retained_reference

    require(all(row.get("enabled") is False and row.get("state") == "Disabled"
                for row in observation.get("tasks", [])), "maintenance fleet is not held")
    outcomes = restoration_execution(packet, evidence, observation)
    require(all(row["state"] == "no-observed-execution" for row in outcomes.values()),
            "scheduled execution crossed maintenance")
    execution = json_bytes(retained_reference(observation["execution"], evidence))
    require(hold_at <= clock(execution["watermark"]["captured_at"]) <= mutation_at <= terminal_at
            <= clock(execution["finished_at"]), "Scheduler history does not span maintenance")


def validate_maintenance_git_witness(packet, git, history_git, history_repo):
    """Check each actual pre-update root while retaining A's task identity."""
    from cre_runtime_maintenance_continuation import runtime_before

    require(git.get("schema") == "cre-runtime-maintenance-git-v1", "maintenance Git observation missing")
    for key, old in (("native", packet["native_before"]), ("runtime", runtime_before(packet))):
        for phase, wanted in (("before", old), ("after", packet["new"])):
            row = git[key][phase]
            require(canonical_path(row.get("root")) == canonical_path(packet["roots"][key])
                    and {field: row.get(field) for field in ("sha", "tree")} == wanted and row.get("status") == ""
                    and (phase != "after" or row.get("tracking") == wanted["sha"]), "maintenance Git identity differs")
        validate_maintenance_tracking_history(history_git, history_repo, old, git[key]["before"]["tracking"], packet["new"])


def validate_maintenance_ready(packet, evidence, ready, current, *, release_raw, release_path, git=None, finite=None):
    """Authenticate the still-owned, held-fleet ready boundary from retained bytes."""
    import re

    from cre_code_handoff import windows_path
    from cre_code_handoff_evidence import DEPENDENCY_CHECKS, role_paths
    from cre_code_handoff_git import Git, local_path
    from cre_code_restoration_evidence import retained_reference
    from cre_runtime_maintenance import (
        validate_maintenance_authorization,
        validate_maintenance_observation,
        validate_maintenance_release,
    )
    from cre_runtime_maintenance_continuation import runtime_before
    from cre_runtime_maintenance_images import qualify_image_transition
    from cre_runtime_maintenance_steps import compose_argv

    if finite is not None:
        from cre_finite_handoff import FiniteProof

        require(type(finite) is FiniteProof, 'finite ready qualification differs')
        finite.recheck_ready(release_raw, packet, evidence)

    def raw(field):
        return retained_reference(ready[field], evidence)

    require(raw("release_reference") == release_raw
            and canonical_path(ready["release_reference"]["path"]) == canonical_path(str(release_path)),
            "maintenance ready release bytes/path differ")
    release = json_bytes(release_raw)
    release["roots"] = role_paths(release)
    require(release == packet and ready.get("schema") == "cre-runtime-maintenance-ready-v1"
            and ready.get("release_sha256") == sha(release_raw)
            and ready.get("old") == packet["old"] and ready.get("new") == packet["new"]
            and ready.get("recovery_required_at_publication") is True, "maintenance ready release differs")
    if finite is None:
        if git is None:
            validate_maintenance_release(packet, evidence)
        else:
            validate_maintenance_release(packet, evidence, git=git)
    history_git = git if git is not None else Git(packet["programs"]["native_git"])
    history_repo = packet["roots"]["development"]
    history_git.maintenance_ancestry(history_repo, packet["old"], packet["native_before"], packet["new"])
    if runtime_before(packet) != packet["old"]:
        history_git.maintenance_ancestry(history_repo, packet["old"], runtime_before(packet), packet["new"])
    for scope in (packet["native_change"], packet["runtime_change"]):
        validate_maintenance_git_history(history_git, history_repo, scope)
    mutation_raw, authorization_raw = raw("mutation"), raw("authorization")
    mutation, authorization = json_bytes(mutation_raw), json_bytes(authorization_raw)
    operation = canonical_path(ready.get("operation_output"))
    require(mutation.get("schema") == "cre-runtime-maintenance-mutation-v1"
            and mutation.get("release_sha256") == sha(release_raw)
            and mutation.get("authorization_sha256") == ready.get("authorization_sha256") == sha(authorization_raw)
            and canonical_path(mutation.get("operation_output")) == operation
            and mutation.get("tooling") == packet["tooling"] and mutation.get("fleet") == packet["task_baseline"],
            "maintenance mutation provenance differs")
    mutation_at = clock(mutation.get("entered_at"))
    initial_raw = raw("initial_observation")
    validate_maintenance_authorization(release_raw, packet, evidence, initial_raw, authorization, mutation_at,
                                       output=operation)
    authority = ready.get("owned_authority")
    require(isinstance(authority, dict) and type(authority.get("pid")) is int and authority["pid"] > 0
            and authority.get("recovery_required") is True
            and all(isinstance(authority.get(key), str) and authority[key] for key in ("token", "generation"))
            and all(isinstance(authority.get(key), list) and len(authority[key]) == 2
                    and all(type(part) is int and part >= 0 for part in authority[key])
                    for key in ("authority_identity", "directory_identity")), "maintenance owner witness differs")
    names = ["native-hold"] + (["npm-ci"] if packet["install_scope"] else []) + list(DEPENDENCY_CHECKS)
    names += (["compose-up"] if packet["expected_images"] else []) + ["native-repin", "native-verify"]
    commands = ready.get("commands")
    require(isinstance(commands, list) and [row.get("name") for row in commands] == names,
            "maintenance command order differs")
    collector = local_path(packet["roots"]["runtime"]) / "scripts/firecrawl-ops/cre_collector"
    script = local_path(packet["roots"]["tooling"]) / "scripts/firecrawl-ops/cre_collector/windows/Invoke-CreCodeHandoff.ps1"
    def native(mode):
        return [str(local_path(packet["programs"]["powershell"])), "-NoProfile", "-NonInteractive", "-File",
                windows_path(script), "-Mode", mode, "-MaintenancePath", windows_path(local_path(ready["release_reference"]["path"])),
                "-MaintenanceSha", sha(release_raw), "-Output", windows_path(local_path(operation)), "-OwnerPid",
                str(authority["pid"]), "-Observation", windows_path(local_path(ready["initial_observation"]["path"])),
                "-Authorization", windows_path(local_path(ready["authorization"]["path"]))]
    receipts = []
    for row in commands:
        name = row["name"]
        if name.startswith("native-"):
            argv, selected, cwd = native(name[7:].capitalize()), None, None
        elif name == "compose-up":
            argv, selected, cwd = compose_argv(packet, evidence), None, None
        elif name == "npm-ci":
            argv = ["bash", "-c", 'cd -- "$1" && exec npm ci --ignore-scripts', "cre-maintenance", str(collector)]
            selected, cwd = packet["new"], str(collector)
        else:
            argv = ["bash", "-c", 'cd -- "$1" && shift && exec "$@"', "cre-maintenance", str(collector), *DEPENDENCY_CHECKS[name]]
            selected, cwd = packet["new"], str(collector)
        receipts.append(validate_maintenance_command(row, argv, selected, cwd, evidence.read, retain=evidence.original_payloads))
    for previous, following in pairwise(receipts):
        require(clock(previous["finished_at"]) <= clock(following["started_at"]), "maintenance command chronology differs")
    hold_start, hold_end = clock(receipts[0]["started_at"]), clock(receipts[0]["finished_at"])
    require(hold_start <= mutation_at <= hold_end, "mutation marker outside native hold")
    before, after = json_bytes(raw("before_snapshot")), json_bytes(raw("after_snapshot"))
    require(unique(before, "service") == unique(evidence.obj("container_config_before"), "service"),
            "maintenance initial configuration differs")
    if packet["expected_images"]:
        validate_maintenance_projection_review(packet, evidence, ready["projection"])
        qualify_image_transition(before, after, packet["expected_images"], expected_after_config=packet["expected_after_config"])
        from cre_runtime_maintenance_build import validate_prepared_image_history
        validate_prepared_image_history(packet, evidence, history_git, packet["roots"]["development"])
    else:
        require(before == after, "unchanged images/containers differ")
    selected_snapshot = json_bytes(raw("selected_snapshot"))
    observation = ready["readiness"]
    require(clock(authorization["authorized_at"]) <= hold_start
            and clock(receipts[-1]["finished_at"]) <= clock(observation["qualified_at"])
            < clock(authorization["expires_at"]), "maintenance readiness outside authorization or command interval")
    validate_maintenance_observation(packet, evidence, observation, current, selected=True, owned=True,
                                     selected_snapshot=selected_snapshot)
    actual, normalized = unique(observation["containers"], "id"), unique(after, "id")
    require(set(actual) == set(normalized) and all(all(actual[key].get(field) == normalized[key].get(field)
            for field in ("image", "started_at", "status")) for key in actual), "selected host/container snapshot differs")
    initial = unique(json_bytes(initial_raw)["containers"], "name")
    current_by_name = unique(observation["containers"], "name")
    require(set(initial) == set(current_by_name) and all(all(initial[name].get(field) == current_by_name[name].get(field)
            for field in ("memory", "nano_cpus", "cpu_quota", "cpu_period")) for name in initial),
            "maintenance container names/resource caps differ")
    git = json_bytes(raw("git_observation"))
    validate_maintenance_git_witness(packet, git, history_git, history_repo)
    expected_files = {"package.json", "package-lock.json", "node_modules/.package-lock.json"}
    deps = ready.get("dependencies_after")
    require(isinstance(deps, dict) and set(deps) == expected_files
            and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in deps.values()),
            "maintenance installed dependency proof differs")
    validate_maintenance_dependency_history(history_git, history_repo, packet["new"], deps)
    if not packet["install_scope"]:
        require(ready.get("dependencies_before") == deps, "unchanged dependencies differ")
    execution = json_bytes(retained_reference(observation["execution"], evidence))
    capture_finished = clock(execution.get("finished_at"))
    require(clock(receipts[-1]["started_at"]) <= clock(execution.get("started_at")) <= capture_finished
            <= clock(receipts[-1]["finished_at"]), "Scheduler capture outside native verify")
    validate_maintenance_execution(packet, evidence, observation, hold_at=hold_start, mutation_at=mutation_at,
                                   terminal_at=capture_finished)
    evidence.recheck()


def make_maintenance_transition(release_path, operation, independent, read):
    """Reference existing producer bytes, including each independent XML readback."""
    release = json_bytes(read(release_path))
    require(release.get("schema") == "cre-runtime-maintenance-v1", "maintenance release schema differs")
    ready = json_bytes(read(operation / "maintenance-ready.json"))
    original = unique(release.get("task_baseline"), "key", TASKS)
    observed = unique(json_bytes(read(independent / "observation.json")).get("tasks"), "key", TASKS)
    rows = []
    for key, before in original.items():
        after = observed[key]
        rows.append({"key": key,
                     "before_xml": reference(operation / (key + "-before.xml"), read),
                     "final_xml": reference(operation / (key + "-final.xml"), read),
                     "independent_xml": reference(independent / (key + "-observed.xml"), read),
                     "before_tuple": {field: before[field] for field in TUPLE_FIELDS},
                     "after_tuple": {field: after[field] for field in TUPLE_FIELDS}})
    return {"schema": "cre-runtime-maintenance-transition-v1", "old": release["old"], "new": release["new"],
            "roots": release["roots"], "failed_handoff_release": release["failed_handoff_release"],
            "release": reference(release_path, read),
            "authorization": ready["authorization"], "initial_observation": ready["initial_observation"],
            "mutation": reference(operation / "mutation-start.json", read),
            "ready": reference(operation / "maintenance-ready.json", read),
            "release_start": reference(operation / "release-start.json", read),
            "terminal": reference(operation / "terminal.json", read),
            "native_completion": reference(str(operation) + ".native.json", read),
            "native_stdout": reference(str(operation) + ".stdout.raw", read),
            "native_stderr": reference(str(operation) + ".stderr.raw", read),
            "independent_observation": reference(independent / "observation.json", read), "tasks": rows}


def original_held_release(packet, evidence, read):
    """Find the original no-pin held release, retaining every predecessor input."""
    from cre_code_handoff_evidence import Evidence

    current, current_evidence, seen = packet, evidence, set()
    for depth in range(9):
        tasks = unique(current.get("task_baseline"), "key", TASKS)
        pins = ["pin_before" in row for row in tasks.values()]
        if not any(pins):
            return current, current_evidence
        require(depth < 8, "historical held predecessor depth exceeded")
        role = current.get("continuation", {}).get("failed_release")
        require(isinstance(role, str), "historical held predecessor missing")
        raw = current_evidence.raw(role)
        digest = sha(raw)
        require(digest not in seen, "historical held predecessor cycle")
        seen.add(digest)
        prior = json_bytes(raw)
        require(prior.get("schema") == "cre-runtime-maintenance-v1" and prior.get("old") == packet["old"],
                "historical held predecessor identity differs")
        if current_evidence is not evidence:
            current_evidence.recheck()
            evidence.original_payloads.update(current_evidence.original_payloads)
            evidence.original_payloads.update({current_evidence.inputs[key]["path"]: value
                                               for key, value in current_evidence.original.items()})
        current_evidence = Evidence(prior, read)
        current = prior
    require(False, "historical held predecessor depth exceeded")


def historical_held_tasks(packet, evidence, read):
    """Return the original held task rows after the bounded predecessor walk."""
    release, original = original_held_release(packet, evidence, read)
    return original, unique(release.get("task_baseline"), "key", TASKS)


def validate_maintenance_transition(receipt, read, *, retain=None, git=None):
    """Join one genuine failed predecessor to an owned maintenance and independent readback."""
    from cre_code_handoff_evidence import (
        Evidence,
        role_paths,
        validate_xml_transition,
        xml_enabled,
    )
    from cre_code_restoration_evidence import retained_reference
    from cre_runtime_maintenance import validate_maintenance_observation

    require(receipt.get("schema") == "cre-runtime-maintenance-transition-v1", "unknown maintenance transition")
    def payload(ref):
        require(isinstance(ref, dict) and set(ref) == {"path", "sha256", "bytes"}
                and type(ref["bytes"]) is int and ref["bytes"] >= 0, "maintenance transition reference malformed")
        canonical_path(ref["path"])
        raw = read(ref["path"])
        require(len(raw) == ref["bytes"] and sha(raw) == ref["sha256"], "maintenance transition payload changed")
        if retain is not None:
            retain[ref["path"]] = raw
        return raw

    release_raw = payload(receipt["release"])
    packet = json_bytes(release_raw)
    packet["roots"] = role_paths(packet)
    evidence = Evidence(packet, read)
    roots = receipt.get("roots")
    require(receipt.get("old") == packet["old"] and receipt.get("new") == packet["new"]
            and isinstance(roots, dict) and set(roots) == set(packet["roots"])
            and all(canonical_path(roots[key]) == canonical_path(packet["roots"][key]) for key in roots),
            "maintenance transition identity differs")
    failed_role = packet["failed_handoff"]["release"]
    failed_ref = evidence.inputs[failed_role]
    require(receipt.get("failed_handoff_release") == {key: failed_ref[key] for key in ("path", "sha256", "bytes")}
            and payload(receipt["failed_handoff_release"]) == evidence.raw(failed_role),
            "maintenance transition failed predecessor differs")
    ready_raw = payload(receipt["ready"])
    ready = json_bytes(ready_raw)
    require(payload(receipt["mutation"]) == retained_reference(ready["mutation"], evidence)
            and payload(receipt["authorization"]) == retained_reference(ready["authorization"], evidence)
            and payload(receipt["initial_observation"]) == retained_reference(ready["initial_observation"], evidence),
            "maintenance transition mixes admission inputs")
    if git is None:
        validate_maintenance_ready(packet, evidence, ready, clock(ready["readiness"]["qualified_at"]),
                                   release_raw=release_raw, release_path=receipt["release"]["path"])
    else:
        validate_maintenance_ready(packet, evidence, ready, clock(ready["readiness"]["qualified_at"]),
                                   release_raw=release_raw, release_path=receipt["release"]["path"], git=git)
    mutation = json_bytes(payload(receipt["mutation"]))
    operation = canonical_path(ready["operation_output"])
    for field, suffix in (("mutation", "/mutation-start.json"), ("ready", "/maintenance-ready.json"),
                          ("release_start", "/release-start.json"), ("terminal", "/terminal.json"),
                          ("native_completion", ".native.json"), ("native_stdout", ".stdout.raw"),
                          ("native_stderr", ".stderr.raw")):
        require(canonical_path(receipt[field]["path"]) == operation + suffix,
                "maintenance retained artifact path differs")
    observation_path = canonical_path(receipt["independent_observation"]["path"])
    independent_dir, separator, leaf = observation_path.rpartition("/")
    require(separator and leaf == "observation.json" and independent_dir != operation
            and not independent_dir.startswith(operation + "/") and not operation.startswith(independent_dir + "/"),
            "maintenance independent output aliases operation")
    start_raw, terminal_raw, native_raw = (payload(receipt[field]) for field in ("release_start", "terminal", "native_completion"))
    start, terminal, native = (json_bytes(raw) for raw in (start_raw, terminal_raw, native_raw))
    digest, auth_digest = sha(release_raw), sha(payload(receipt["authorization"]))
    for row in (start, terminal, native):
        require(row.get("release_sha256") == digest and row.get("authorization_sha256") == auth_digest
                and canonical_path(row.get("operation_output")) == operation, "maintenance operation attempts differ")
    owned = ready["owned_authority"]
    released = [[owned["pid"], owned["token"], owned["generation"], False], owned["authority_identity"]]
    require(start.get("schema") == "cre-runtime-maintenance-release-start-v1"
            and start.get("ready_sha256") == sha(ready_raw) and start.get("owned_authority") == owned
            and terminal.get("schema") == "cre-runtime-maintenance-terminal-v1" and terminal.get("status") == "committed"
            and terminal.get("ready_sha256") == sha(ready_raw) and terminal.get("release_start_sha256") == sha(start_raw)
            and terminal.get("canonical_lease_released") is True and terminal.get("released_authority") == released,
            "maintenance release/authority not settled")
    capture = native.get("capture", {})
    require(native.get("schema") == "cre-runtime-maintenance-native-completion-v1"
            and capture.get("Started") is True and capture.get("Failed") is False
            and type(capture.get("NativeExitCode")) is int and capture["NativeExitCode"] == 0
            and all(capture.get(stream, {}).get("Eof") is True and capture[stream].get("Truncated") is False
                    for stream in ("Stdout", "Stderr"))
            and native.get("stdout_sha256") == sha(payload(receipt["native_stdout"]))
            and native.get("stderr_sha256") == sha(payload(receipt["native_stderr"])), "maintenance native completion uncertain")
    observation = json_bytes(payload(receipt["independent_observation"]))
    require(observation.get("release_sha256") == digest and observation.get("terminal_sha256") == sha(terminal_raw)
            and observation.get("native_completion_sha256") == sha(native_raw)
            and canonical_path(observation.get("operation_output")) == operation
            and observation.get("released_authority") == released, "maintenance independent readback differs")
    independent_containers = json_bytes(retained_reference(observation.get("container_snapshot"), evidence))
    ready_containers = json_bytes(retained_reference(ready["after_snapshot"], evidence))
    require(unique(independent_containers, "service") == unique(ready_containers, "service"),
            "postterminal container configuration changed")
    require(clock(ready["readiness"]["qualified_at"]) <= clock(start["started_at"])
            < clock(json_bytes(payload(receipt["authorization"]))["expires_at"])
            and clock(start["started_at"]) <= clock(terminal["finished_at"]) <= clock(observation["observed_at"]),
            "maintenance release chronology differs")
    snapshot = json_bytes(retained_reference(ready["selected_snapshot"], evidence))
    validate_maintenance_observation(packet, evidence, observation, clock(observation["qualified_at"]),
                                     selected=True, selected_snapshot=snapshot)
    hold = json_bytes(payload(ready["commands"][0]["receipt"]))
    validate_maintenance_execution(packet, evidence, observation, hold_at=clock(hold["started_at"]),
                                   mutation_at=clock(mutation["entered_at"]), terminal_at=clock(terminal["finished_at"]))
    originals = unique(packet["task_baseline"], "key", TASKS)
    partial = any("pin_before" in row for row in originals.values())
    if partial:
        prior_evidence, historic = historical_held_tasks(packet, evidence, read)
    observed = unique(observation["tasks"], "key", TASKS)
    results = {}
    for key, row in unique(receipt.get("tasks"), "key", TASKS).items():
        for field, expected in (("before_xml", operation + "/" + key + "-before.xml"),
                                ("final_xml", operation + "/" + key + "-final.xml"),
                                ("independent_xml", independent_dir + "/" + key + "-observed.xml")):
            require(canonical_path(row[field]["path"]) == expected, "maintenance task evidence path differs")
        before, final, independent = (payload(row[field]) for field in ("before_xml", "final_xml", "independent_xml"))
        require(before == evidence.raw(originals[key]["xml"]) and final == independent
                and observed[key].get("xml_sha256") == sha(independent) and xml_enabled(before) is False
                and xml_enabled(final) is False, "maintenance independent task XML differs")
        validate_xml_transition(before, final, originals[key].get("pin_before", packet["old"]["sha"]), packet["new"]["sha"])
        prior = {field: originals[key][field] for field in TUPLE_FIELDS}
        after = {field: observed[key][field] for field in TUPLE_FIELDS}
        require(row["before_tuple"] == prior and row["after_tuple"] == after, "maintenance task tuple differs")
        results[key] = {"before_xml": before, "final_xml": final, "independent_xml": independent,
                        "before_tuple": prior, "after_tuple": after}
        if partial:
            results[key]["historical_before_xml"] = prior_evidence.raw(historic[key]["xml"])
            results[key]["historical_before_tuple"] = {field: historic[key][field] for field in TUPLE_FIELDS}
    if partial:
        prior_evidence.recheck()
        evidence.original_payloads.update({prior_evidence.inputs[role]["path"]: raw
                                           for role, raw in prior_evidence.original.items()})
    evidence.recheck()
    if retain is not None:
        retain.update(evidence.original_payloads)
        retain.update({evidence.inputs[role]["path"]: raw for role, raw in evidence.original.items()})
    return {"old": packet["old"], "new": packet["new"], "roots": {key: packet["roots"][key] for key in ("native", "runtime")},
            "failed_handoff_release_sha256": sha(evidence.raw(failed_role)), "tasks": results, "runtime_snapshot": snapshot}
