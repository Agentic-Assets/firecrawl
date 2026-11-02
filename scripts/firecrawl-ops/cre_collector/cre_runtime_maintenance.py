"""Guarded runtime maintenance admission and operation."""

import os
import re
import sys
from datetime import timedelta
from pathlib import PurePosixPath

from cre_code_handoff_evidence import (
    TASKS,
    Evidence,
    canonical_path,
    clock,
    identity,
    json_bytes,
    require,
    safe_relative,
    sha,
    unique,
    validate_owned_qa,
    validate_review,
)
from cre_code_handoff_git import local_path, read_file
from cre_code_handoff_host import qualify_host, qualify_native
from cre_code_restoration_evidence import validate_restoration


def run_maintenance_steps(*args, **kwargs):
    from cre_runtime_maintenance_steps import run_maintenance_steps as run

    return run(*args, **kwargs)


def validate_prepared_images(*args, **kwargs):
    from cre_runtime_maintenance_build import validate_prepared_images as validate

    return validate(*args, **kwargs)


def validate_current_projection(packet, evidence):
    """Recompute native Compose/image projection and compare reviewed bytes."""
    from cre_runtime_maintenance_build import (
        capture_fleet_projection,
        validate_fleet_projection,
    )

    output = local_path(packet["projection_output"])
    observed = capture_fleet_projection(packet, output)
    validate_fleet_projection(packet, evidence, observed)
    require(json_bytes(evidence.raw("expected_after_config")) == packet["expected_after_config"],
            "reviewed expected container configuration differs")


def validate_operation_paths(packet, output):
    """Keep native-visible exclusive outputs inside the development scratch tree."""
    from cre_code_handoff_git import windows_path

    development = canonical_path(packet["roots"]["development"])
    operation = canonical_path(windows_path(local_path(output)))
    projection = canonical_path(windows_path(local_path(packet["projection_output"])))
    prefix = (development.rstrip("/") + "/.scratch/").casefold()
    require(operation.casefold().startswith(prefix) and projection.casefold().startswith(prefix),
            "maintenance outputs must be in development scratch")
    left, right = operation.casefold(), projection.casefold()
    require(left != right and not left.startswith(right + "/") and not right.startswith(left + "/"),
            "maintenance operation and projection outputs must be distinct")
    for path in (local_path(output), local_path(packet["projection_output"])):
        require(all(not ancestor.is_symlink() for ancestor in (path, *path.parents)),
                "maintenance output ancestor alias")


def validate_maintenance_ready(*args, **kwargs):
    from cre_runtime_maintenance_transition import (
        validate_maintenance_ready as validate,
    )

    return validate(*args, **kwargs)


def validate_historical_admission(packet, evidence):
    """Qualify the original failed handoff without rewriting its evidence."""
    require(packet.get("schema") == "cre-runtime-maintenance-v1"
            and packet.get("scope") == "held-runtime-maintenance", "unknown maintenance scope")
    old, new = identity(packet.get("old")), identity(packet.get("new"))
    identity(packet.get("native_before"))
    require(old != new, "selected maintenance release already deployed")
    failed = packet.get("failed_handoff", {}).get("release")
    source_ref = packet.get("failed_handoff_release")
    original_row = evidence.inputs.get(failed)
    require(isinstance(source_ref, dict) and original_row is not None
            and source_ref == {"path": original_row["path"], "sha256": original_row["sha256"],
                               "bytes": original_row["bytes"]}, "failed handoff release reference differs")
    programs = packet.get("programs")
    require(isinstance(programs, dict) and set(programs) == {"native_git", "powershell", "native_docker"},
            "maintenance native tools missing")
    from cre_code_handoff_git import windows_path

    windows_path(programs["native_docker"])
    historical = dict(packet, schema="cre-code-restoration-v1", scope="restore-held-task-states", code=old)
    historical["programs"] = {key: programs[key] for key in ("native_git", "powershell")}
    historical.pop("maintenance_transition", None)
    original = evidence.inputs.get("historical_runtime_snapshot")
    require(original is not None, "original failed-A runtime snapshot missing")
    rows = [dict(row, role="runtime_snapshot") if row["role"] == "historical_runtime_snapshot" else row
            for row in packet["inputs"] if row["role"] != "runtime_snapshot"]
    historical_evidence = Evidence({"inputs": rows}, evidence.read)
    validate_restoration(historical, historical_evidence, historical=True)
    evidence.original_payloads.update(historical_evidence.original_payloads)


def validate_maintenance_release(packet, evidence):
    """Qualify retained A, the selected QA, and the closed maintenance scope."""
    require(packet.get("install_scope") in ([], ["collector"]), "unsupported dependency install scope")
    validate_historical_admission(packet, evidence)
    build_source = packet.get("build_source")
    require(isinstance(build_source, dict) and set(build_source) == {"root", "identity"}
            and build_source.get("identity") == packet["new"], "build source identity differs")
    build_root = canonical_path(build_source.get("root"))
    require(build_root.startswith("/") and not build_root.startswith("/mnt/")
            and all(build_root != canonical_path(root) and not build_root.startswith(canonical_path(root) + "/")
                    and not canonical_path(root).startswith(build_root + "/") for root in packet["roots"].values()),
            "build source must be a separate Linux checkout")
    allowed = {"scripts/firecrawl-ops/cre_collector/package.json",
               "scripts/firecrawl-ops/cre_collector/package-lock.json",
               "scripts/firecrawl-ops/cre_collector/document_text/package.json",
               "scripts/firecrawl-ops/cre_collector/document_text/package-lock.json"}
    dependency_names = {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
                        "requirements.txt", "pyproject.toml", "uv.lock"}
    scopes = {}
    for key, before in (("runtime", packet["old"]), ("native", packet["native_before"])):
        scope = packet.get(key + "_change")
        require(isinstance(scope, dict) and scope.get("deployed") == before and scope.get("selected") == packet["new"],
                "per-root maintenance identities differ")
        paths = scope.get("allowed_paths")
        require(isinstance(paths, list) and len(paths) == len(set(paths))
                and (bool(paths) if before["tree"] != packet["new"]["tree"] else not paths),
                "per-root changed path set differs")
        for path in paths:
            safe_relative(path)
        require(evidence.raw(key + "_allowed_paths_nul") == b"".join(p.encode() + b"\0" for p in paths),
                "per-root changed path evidence differs")
        require(set(scope.get("objects", {})) == set(paths), "per-root changed object map missing")
        dependencies = scope.get("changed_dependency_paths")
        changed_dependencies = {path for path in paths if PurePosixPath(path).name in dependency_names}
        require(isinstance(dependencies, list) and len(dependencies) == len(set(dependencies))
                and set(dependencies) <= allowed and set(dependencies) == changed_dependencies,
                "unsupported changed dependency manifest")
        scopes[key] = set(dependencies)
    collector_changes = scopes["runtime"] & {"scripts/firecrawl-ops/cre_collector/package.json",
                                               "scripts/firecrawl-ops/cre_collector/package-lock.json"}
    require(bool(collector_changes) == (packet["install_scope"] == ["collector"]),
            "collector dependency installation scope differs")
    images = packet.get("expected_images")
    require(isinstance(images, dict) and set(images) <= {"api", "playwright-service", "nuq-postgres"}
            and all(isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value)
                    for value in images.values()), "unsupported image scope/identity")
    runtime_paths = packet["runtime_change"]["allowed_paths"]
    required_images = set()
    for path in runtime_paths:
        if path.startswith("apps/api/"):
            required_images.add("api")
        elif path.startswith("apps/playwright-service-ts/"):
            required_images.add("playwright-service")
        elif path.startswith("apps/nuq-postgres/"):
            required_images.add("nuq-postgres")
        elif path in {"docker-compose.yaml", "docker-compose.yml"}:
            required_images.update(("api", "playwright-service", "nuq-postgres"))
    require(required_images <= set(images), "changed service build context missing reviewed image")
    evidence.raw("bundle")
    require(packet.get("qa", {}).get("producer") == "owned-gates-v1", "maintenance QA evidence missing")
    validate_owned_qa(packet, evidence, expected_tree=packet["new"]["tree"])


def validate_maintenance_observation(packet, evidence, observation, current, *, selected=False, owned=False,
                                     selected_snapshot=None):
    """Qualify a fresh host and held fleet at a specific maintenance boundary."""
    expected_native = packet["new"] if selected else packet["native_before"]
    expected_runtime = packet["new"] if selected else packet["old"]
    require(observation.get("schema") == "cre-runtime-maintenance-observation-v1"
            and observation.get("native") == expected_native and observation.get("runtime") == expected_runtime,
            "maintenance checkout identity differs")
    observed, qualified = clock(observation.get("observed_at")), clock(observation.get("qualified_at"))
    require(observed <= qualified <= current and 0 <= (current - observed).total_seconds() < 300,
            "maintenance observation expired/future")
    require(observation.get("ownership") == {"phase": "owned-maintenance" if owned else "released-clear",
                                            "verified": True, "recovery_required": owned}
            and observation.get("resources_ok") is True and observation.get("queues_idle") is True,
            "maintenance ownership/admission differs")
    require(not selected or isinstance(selected_snapshot, dict), "actual selected runtime snapshot required")
    qualify_host(observation, selected_snapshot if selected else evidence.obj("runtime_snapshot"))
    qualify_native(observation.get("windows_resources"))
    tasks = unique(observation.get("tasks"), "key", TASKS)
    for key, baseline in unique(packet["task_baseline"], "key", TASKS).items():
        task = tasks[key]
        require(task.get("enabled") is False and task.get("state") == "Disabled"
                and type(task.get("missed_runs")) is int and task["missed_runs"] == baseline["missed_runs"],
                "maintenance held task state differs")
        for field in ("task_path", "task_name", "last_task_result", "last_run_utc", "next_run_utc"):
            require(type(task.get(field)) is type(baseline.get(field)) and task[field] == baseline[field],
                    "maintenance task history/clock differs")
        if not selected:
            require(task.get("actions") == baseline["actions"]
                    and task.get("xml_sha256") == sha(evidence.raw(baseline["xml"])),
                    "maintenance original task XML or action differs")
        else:
            # Post-repin XML is validated against the original by the native
            # consumer and the retained transition reader, not by a digest alone.
            from cre_code_handoff_evidence import validate_xml_transition, xml_enabled
            reference = task.get("xml_reference")
            require(isinstance(reference, dict), "maintenance final XML reference missing")
            raw = evidence.read(reference["path"])
            require(reference.get("bytes") == len(raw) and reference.get("sha256") == sha(raw)
                    and task.get("xml_sha256") == sha(raw) and xml_enabled(raw) is False,
                    "maintenance final XML binding differs")
            validate_xml_transition(evidence.raw(baseline["xml"]), raw, packet["old"]["sha"], packet["new"]["sha"])
            expected = [{name: value.replace(packet["old"]["sha"], packet["new"]["sha"])
                         for name, value in action.items()} for action in baseline["actions"]]
            require(task.get("actions") == expected, "maintenance final action differs")
            evidence.original_payloads[reference["path"]] = raw


def validate_maintenance_authorization(release_raw, packet, evidence, observation_raw, authorization, current,
                                       *, output, review_reader=None):
    """Bind a reviewed release and live observation to one exclusive operation."""
    require(authorization.get("schema") == "cre-runtime-maintenance-authorization-v1"
            and authorization.get("scope") == "held-runtime-maintenance"
            and authorization.get("release_sha256") == sha(release_raw)
            and authorization.get("observation_sha256") == sha(observation_raw)
            and canonical_path(authorization.get("operation_output")) == canonical_path(str(output)),
            "maintenance authorization input/output differs")
    require(json_bytes(observation_raw).get("release_sha256") == sha(release_raw),
            "maintenance observation belongs to another release")
    started, expires = clock(authorization.get("authorized_at")), clock(authorization.get("expires_at"))
    require(started <= current < expires and 0 < (expires - started).total_seconds() <= 300,
            "maintenance authorization expired/lifetime differs")
    reader = review_reader or evidence.read
    review = authorization.get("review")
    require(isinstance(review, dict), "maintenance review missing")
    raw = reader(review["path"])
    require(review.get("sha256") == sha(raw), "maintenance review changed")
    attestation = json_bytes(raw)
    body = reader(attestation["body"]["path"])
    require(review.get("reviewer") == attestation.get("reviewer")
            and review.get("approval_ref") == attestation.get("approval_ref"),
            "maintenance review attribution differs")
    validate_review(attestation, body,
                    {"release_sha256": sha(release_raw), "tooling": packet["tooling"]}, "runtime-maintenance")
    evidence.original_payloads[review["path"]] = raw
    evidence.original_payloads[attestation["body"]["path"]] = body
    validate_maintenance_observation(packet, evidence, json_bytes(observation_raw), current)
    evidence.recheck()


def maintenance_readiness(packet, modules, git, *, selected=False, owned=False):
    """Observe the exact two checkout identities and existing canonical owner."""
    from cre_code_handoff import assert_owned, lock_clear, visible_linux_frontends
    from cre_code_handoff_host import observe_host

    checkpoint, memory, client, idle = modules
    native = packet["new"] if selected else packet["native_before"]
    runtime = packet["new"] if selected else packet["old"]
    git.clean(packet["roots"]["development"], packet["development"], tracking=packet["development"]["sha"])
    git.maintenance_ancestry(packet["roots"]["development"], packet["old"], packet["native_before"], packet["new"])
    git.clean(packet["roots"]["native"], native)
    git.clean(packet["roots"]["runtime"], runtime)
    lock = checkpoint.canonical_shared_lock_dir(local_path(packet["roots"]["runtime"]))
    if owned:
        assert_owned(checkpoint, lock, os.getpid())
    else:
        lock_clear(checkpoint, lock)
    frontends = visible_linux_frontends()
    require(not frontends, "visible competing Linux CRE frontend exists")
    host = observe_host(checkpoint, memory, client, idle)
    host.update(native=native, runtime=runtime,
                ownership={"phase": "owned-maintenance" if owned else "released-clear",
                           "verified": True, "recovery_required": owned},
                resources_ok=True, queues_idle=True, linux_frontends=frontends,
                linux_visibility="current UID /proc roles; own ancestry excluded; PID/role only")
    return host


def observe_maintenance(args, raw, packet, evidence, git, modules):
    """Publish one fresh read-only observation; no runtime or task mutations."""
    from cre_code_handoff import Foreground, now, write_new

    output = local_path(args.output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    runner = Foreground(packet, modules[0], output)
    runner.native("Observe", args.release, sha(raw))
    native = json_bytes(read_file(output / "native-observation.json"))
    observation = maintenance_readiness(packet, modules, git)
    observation.update(schema="cre-runtime-maintenance-observation-v1", observed_at=native["observed_at"],
                       qualified_at=now().isoformat(), tasks=native["tasks"],
                       windows_resources=native["windows_resources"], release_sha256=sha(raw))
    validate_maintenance_observation(packet, evidence, observation, now())
    evidence.recheck()
    write_new(output / "observation.json", observation)


def postreadback_containers(packet):
    """Observe the pinned native Docker fleet without publishing raw settings."""
    from cre_runtime_maintenance_build import verify_native_docker
    from cre_runtime_maintenance_images import observe_containers
    from cre_runtime_maintenance_steps import _docker

    verify_native_docker(packet)
    rows = observe_containers(lambda argv: _docker(packet["programs"]["native_docker"], argv))
    verify_native_docker(packet)
    return rows


def postreadback_maintenance(args, raw, packet, evidence, git, modules):
    """Read back the original completed operation from a distinct output."""
    from cre_code_handoff import Foreground, now, predecessor_clear, write_new

    require(args.operation, "original maintenance operation required")
    operation = local_path(args.operation)
    terminal_raw = read_file(operation / "terminal.json")
    terminal = json_bytes(terminal_raw)
    require(terminal.get("schema") == "cre-runtime-maintenance-terminal-v1"
            and terminal.get("status") == "committed" and terminal.get("release_sha256") == sha(raw)
            and terminal.get("canonical_lease_released") is True
            and canonical_path(terminal.get("operation_output")) == canonical_path(str(operation)),
            "original maintenance terminal differs")
    completion_raw = read_file(str(operation) + ".native.json")
    completion = json_bytes(completion_raw)
    require(completion.get("schema") == "cre-runtime-maintenance-native-completion-v1"
            and completion.get("release_sha256") == sha(raw), "native maintenance completion differs")
    ready_raw = read_file(operation / "maintenance-ready.json")
    require(terminal.get("ready_sha256") == sha(ready_raw), "maintenance ready binding differs")
    selected_snapshot = json_bytes(read_file(operation / "selected-runtime-snapshot.json"))
    checkpoint = modules[0]
    lock = checkpoint.canonical_shared_lock_dir(local_path(packet["roots"]["runtime"]))
    predecessor_clear(checkpoint, lock, terminal.get("released_authority"))
    from cre_code_restoration_evidence import retained_reference

    original_after = json_bytes(retained_reference(json_bytes(ready_raw)["after_snapshot"], evidence))
    containers = postreadback_containers(packet)
    require(unique(containers, "service") == unique(original_after, "service"),
            "postreadback container configuration differs")
    output = local_path(args.output)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    runner = Foreground(packet, checkpoint, output)
    runner.native("Postreadback", args.release, sha(raw), operation=args.operation)
    native = json_bytes(read_file(output / "native-observation.json"))
    observation = maintenance_readiness(packet, modules, git, selected=True)
    observation.update(schema="cre-runtime-maintenance-observation-v1", observed_at=native["observed_at"],
                       qualified_at=now().isoformat(), tasks=native["tasks"], execution=native["execution"],
                       windows_resources=native["windows_resources"], release_sha256=sha(raw),
                       terminal_sha256=sha(terminal_raw), native_completion_sha256=sha(completion_raw),
                       released_authority=terminal["released_authority"],
                       operation_output=canonical_path(str(operation)))
    validate_maintenance_observation(packet, evidence, observation, now(), selected=True,
                                     selected_snapshot=selected_snapshot)
    evidence.recheck()
    predecessor_clear(checkpoint, lock, terminal.get("released_authority"))
    require(unique(postreadback_containers(packet), "service") == unique(containers, "service"),
            "postreadback container configuration changed during capture")
    require(read_file(operation / "terminal.json") == terminal_raw
            and read_file(str(operation) + ".native.json") == completion_raw
            and read_file(operation / "maintenance-ready.json") == ready_raw,
            "maintenance original operation changed during readback")
    from cre_runtime_maintenance_transition import (
        make_maintenance_transition,
        validate_maintenance_transition,
    )
    from cre_runtime_maintenance_transition import reference as maintenance_reference

    snapshot_path = output / "container-snapshot.json"
    write_new(snapshot_path, containers)
    observation["container_snapshot"] = maintenance_reference(snapshot_path, read_file)
    write_new(output / "observation.json", observation)
    transition = make_maintenance_transition(args.release, operation, output, read_file)
    validate_maintenance_transition(transition, read_file)
    write_new(output / "transition.json", transition)


def authorize_maintenance(args, raw, packet, evidence):
    """Publish a short-lived, single-output authorization after review."""
    from cre_code_handoff import now, write_new

    observed_raw, review_raw = read_file(args.observation), read_file(args.review)
    current = now()
    authorization = {"schema": "cre-runtime-maintenance-authorization-v1", "scope": "held-runtime-maintenance",
                     "release_sha256": sha(raw), "observation_sha256": sha(observed_raw),
                     "authorized_at": current.isoformat(), "expires_at": (current + timedelta(seconds=299)).isoformat(),
                     "operation_output": str(local_path(args.output).absolute()),
                     "review": {"path": str(args.review), "sha256": sha(review_raw),
                                "reviewer": args.reviewer, "approval_ref": args.approval_ref}}
    validate_maintenance_authorization(raw, packet, evidence, observed_raw, authorization, current,
                                       output=args.output, review_reader=read_file)
    evidence.recheck()
    require(read_file(args.observation) == observed_raw and read_file(args.review) == review_raw,
            "maintenance publication inputs changed")
    validate_maintenance_authorization(raw, packet, evidence, observed_raw, authorization, now(),
                                       output=args.output, review_reader=read_file)
    write_new(local_path(args.authorization), authorization)


def record_maintenance_failure(args, raw, packet, checkpoint, output, classification, original):
    """Record the original failure; cleanup belongs only to the still-owned lease."""
    from cre_code_handoff import Foreground, write_new

    hold = {"attempted": False, "invocation_completed": False, "error_type": None}
    if (classification == "precommit-failure" and (output / "mutation-start.json").is_file()
            and not (output / "release-start.json").exists()):
        hold["attempted"] = True
        try:
            Foreground(packet, checkpoint, output).native("FailureHold", args.release, sha(raw),
                                                          args.observation, args.authorization)
            hold["invocation_completed"] = True
        except BaseException as error:  # noqa: BLE001
            hold["error_type"] = type(error).__name__
    write_new(output / "failure.json", {"schema": "cre-runtime-maintenance-failure-v1",
              "classification": classification, "release_sha256": sha(raw),
              "original_error_type": type(original).__name__, "original_error": str(original),
              "failure_hold": hold, "recovery_retained": "not asserted; independent readback required"})


def apply_maintenance(args, raw, packet, evidence, git, modules):
    """Run one owned held-fleet operation; never enable a scheduled task."""
    from cre_code_handoff import (
        lock_clear,
        now,
        owned_authority,
        released_authority,
        write_new,
    )

    observed_raw = read_file(args.observation)
    authorization_raw = read_file(args.authorization)
    authorization = json_bytes(authorization_raw)
    output = local_path(args.output)

    def admitted():
        require(read_file(args.observation) == observed_raw and read_file(args.authorization) == authorization_raw,
                "maintenance admission inputs changed")
        validate_maintenance_authorization(raw, packet, evidence, observed_raw, authorization, now(),
                                           output=output, review_reader=read_file)

    validate_maintenance_release(packet, evidence)
    admitted()
    validate_operation_paths(packet, output)
    if packet["expected_images"]:
        validate_prepared_images(packet, evidence, git)
    git.development(dict(packet, selected=packet["new"]))
    maintenance_readiness(packet, modules, git)
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    if packet["expected_images"]:
        validate_current_projection(packet, evidence)
    admitted()
    checkpoint = modules[0]
    lock = checkpoint.canonical_shared_lock_dir(local_path(packet["roots"]["runtime"]))
    classification = "precommit-failure"
    failure_recorded = False
    try:
        with checkpoint.SharedLock(lock, recovery_required=True) as lease:
            try:
                admitted()
                ready = run_maintenance_steps(args, raw, packet, evidence, git, modules, lock, output, admitted)
                ready["owned_authority"] = owned_authority(checkpoint, lease)
                evidence.recheck()
                validate_maintenance_ready(packet, evidence, ready, now(), release_raw=raw, release_path=args.release)
                write_new(output / "maintenance-ready.json", ready)
                admitted()
                write_new(output / "release-start.json", {"schema": "cre-runtime-maintenance-release-start-v1",
                          "release_sha256": sha(raw), "authorization_sha256": sha(authorization_raw),
                          "operation_output": canonical_path(str(output)),
                          "ready_sha256": sha(read_file(output / "maintenance-ready.json")),
                          "owned_authority": ready["owned_authority"], "started_at": now().isoformat()})
                classification = "commit-uncertain"
                lease.clear_recovery_requirement()
                released = released_authority(checkpoint, lease)
            except BaseException as original:
                failure_recorded = True
                try:
                    record_maintenance_failure(args, raw, packet, checkpoint, output, classification, original)
                except BaseException as cleanup_error:  # noqa: BLE001
                    print(f"Original {type(original).__name__}: {original}; failure reporting unavailable: {type(cleanup_error).__name__}",
                          file=sys.stderr)
                raise
        lock_clear(checkpoint, lock, released=released)
        classification = "postcommit-confirmation-failure"
        write_new(output / "terminal.json", {"schema": "cre-runtime-maintenance-terminal-v1", "status": "committed",
                  "release_sha256": sha(raw), "authorization_sha256": sha(authorization_raw),
                  "operation_output": canonical_path(str(output)), "ready_sha256": sha(read_file(output / "maintenance-ready.json")),
                  "release_start_sha256": sha(read_file(output / "release-start.json")),
                  "canonical_lease_released": True, "released_authority": released, "finished_at": now().isoformat()})
    except BaseException as original:
        if not failure_recorded:
            try:
                record_maintenance_failure(args, raw, packet, checkpoint, output, classification, original)
            except BaseException as cleanup_error:  # noqa: BLE001
                print(f"Original {type(original).__name__}: {original}; failure reporting unavailable: {type(cleanup_error).__name__}",
                      file=sys.stderr)
        raise
