"""Finite runtime-update steps executed only inside the canonical held lease."""

import os
import subprocess
from pathlib import Path

from cre_code_handoff import Foreground, assert_owned, now, write_new
from cre_code_handoff_evidence import (
    DEPENDENCY_CHECKS,
    canonical_path,
    json_bytes,
    require,
    sha,
    unique,
)
from cre_code_handoff_git import (
    installed_dependencies,
    local_path,
    read_file,
    windows_path,
)
from cre_runtime_maintenance_build import (
    ENGINE,
    verify_native_compose,
    verify_native_docker,
)
from cre_runtime_maintenance_images import observe_containers, qualify_image_transition


def _docker(executable, argv):
    """Inspect in memory; raw container environments never enter command logs."""
    result = subprocess.run([str(local_path(executable)), "--host", ENGINE, *argv],
                            capture_output=True, timeout=20, check=False)
    require(result.returncode == 0, "Docker read-only observation failed")
    require(len(result.stdout) <= 4 * 1024 * 1024, "Docker observation exceeds bound")
    return result.stdout


def _reference(path):
    raw = read_file(path)
    return {"path": str(path), "sha256": sha(raw), "bytes": len(raw)}


def _git_observation(git, root):
    return {"root": canonical_path(str(root)), "sha": git.text(root, "rev-parse", "HEAD"),
            "tree": git.text(root, "rev-parse", "HEAD^{tree}"),
            "status": git.text(root, "status", "--porcelain=v1", "--untracked-files=all"),
            "tracking": git.text(root, "rev-parse", "refs/remotes/origin/main")}


def compose_argv(packet, evidence):
    """Use the existing Windows project and its private environment lookup."""
    native = windows_path(local_path(packet["roots"]["native"]))
    override = windows_path(local_path(evidence.inputs["compose_override"]["path"]))
    return [str(local_path(packet["programs"]["native_docker"])), "--host", ENGINE,
            "compose", "--project-name", "firecrawl", "--project-directory", native,
            "-f", native + "/docker-compose.yaml", "-f", native + "/docker-compose.override.yaml", "-f", override,
            "up", "-d", "--no-deps", "--no-build", "--pull", "never", *sorted(packet["expected_images"])]


def run_maintenance_steps(args, raw, packet, evidence, git, modules, lock, output, admitted, boundary=None):
    """Update the selected code/dependencies/images; leave tasks held and owner intact."""
    from cre_runtime_maintenance import (
        maintenance_readiness,
        validate_maintenance_observation,
    )

    output = Path(output)
    checkpoint = modules[0]
    runner = Foreground(packet, checkpoint, output, lock, boundary)
    commands = []
    def docker(argv):
        verify_native_docker(packet)
        result = _docker(packet["programs"]["native_docker"], argv)
        verify_native_docker(packet)
        return result

    def boundary():
        require(read_file(args.release) == raw, "maintenance release changed during execution")
        verify_native_docker(packet)
        if packet["expected_images"]:
            verify_native_compose(packet)
        admitted()
        assert_owned(checkpoint, lock, os.getpid())

    def recorded(name, action):
        action()
        log = output / f"{runner.counter:02d}-{name}.log"
        commands.append({"name": name, "receipt": _reference(log.with_suffix(".json")),
                         "log": _reference(log)})

    def native(mode):
        boundary()
        recorded("native-" + mode.lower(), lambda: runner.native(
            mode, args.release, sha(raw), args.observation, args.authorization))

    def image_ids():
        services = sorted(packet["expected_images"])
        require(set(packet["image_tags"]) == set(services), "image tag scope differs")
        if not services:
            return
        rows = json_bytes(docker(["image", "inspect", *(packet["image_tags"][name] for name in services)]))
        require(isinstance(rows, list) and len(rows) == len(services), "image readback incomplete")
        for name, row in zip(services, rows, strict=True):
            require(isinstance(row, dict) and row.get("Id") == packet["expected_images"][name],
                    "reviewed image tag changed")

    native("Hold")
    boundary()
    mutation_path = output / "mutation-start.json"
    mutation = json_bytes(read_file(mutation_path))
    require(mutation.get("schema") == "cre-runtime-maintenance-mutation-v1"
            and mutation.get("release_sha256") == sha(raw)
            and mutation.get("authorization_sha256") == sha(read_file(args.authorization))
            and canonical_path(mutation.get("operation_output")) == canonical_path(str(output))
            and mutation.get("tooling") == packet["tooling"] and mutation.get("fleet") == packet["task_baseline"],
            "native mutation context differs before runtime changes")
    before = observe_containers(docker)
    write_new(output / "containers-before.json", before)
    require(unique(before, "service") == unique(evidence.obj("container_config_before"), "service"),
            "prepared container configuration changed before mutation")
    collector = local_path(packet["roots"]["runtime"]) / "scripts/firecrawl-ops/cre_collector"
    dependency_files = ("package.json", "package-lock.json", "node_modules/.package-lock.json")
    dependency_before = {name: sha(read_file(collector / name)) if (collector / name).is_file() else None
                         for name in dependency_files}
    git_rows = {}
    for role in ("native", "runtime"):
        boundary()
        root, scope = packet["roots"][role], packet[role + "_change"]
        git.clean(root, scope["deployed"])
        previous = _git_observation(git, root)
        if scope["deployed"] != packet["new"]:
            git.maintenance_fast_forward(root, scope, evidence.inputs["bundle"]["path"],
                                         allowed_dependency_paths=scope["changed_dependency_paths"])
        else:
            require(scope["allowed_paths"] == [] and scope["objects"] == {}
                    and scope["changed_dependency_paths"] == [], "already-selected checkout has a change scope")
        git.clean(root, packet["new"], tracking=packet["new"]["sha"])
        git_rows[role] = {"before": previous, "after": _git_observation(git, root)}
    write_new(output / "git-observation.json", {"schema": "cre-runtime-maintenance-git-v1", **git_rows})

    if packet["install_scope"] == ["collector"]:
        boundary()
        recorded("npm-ci", lambda: runner.run(
            "npm-ci", ["bash", "-c", 'cd -- "$1" && exec npm ci --ignore-scripts', "cre-maintenance", str(collector)],
            effective_cwd=collector))
    dependencies = installed_dependencies(collector)
    for name, argv in DEPENDENCY_CHECKS.items():
        boundary()
        recorded(name, lambda name=name, argv=argv: runner.run(
            name, ["bash", "-c", 'cd -- "$1" && shift && exec "$@"', "cre-maintenance", str(collector), *argv],
            effective_cwd=collector))
    require(installed_dependencies(collector) == dependencies, "read-only dependency checks changed installed tree")

    boundary()
    image_ids()
    if packet["expected_images"]:
        argv = compose_argv(packet, evidence)
        verify_native_compose(packet)
        try:
            recorded("compose-up", lambda: runner.run("compose-up", argv))
        finally:
            verify_native_compose(packet)
    boundary()
    image_ids()
    after = observe_containers(docker)
    if packet["expected_images"]:
        qualify_image_transition(before, after, packet["expected_images"],
                                 expected_after_config=packet["expected_after_config"])
    else:
        require(before == after, "containers changed during code-only maintenance")
    write_new(output / "containers-after.json", after)
    native("Repin")
    native("Verify")
    native_observation = json_bytes(read_file(output / "native-observation.json"))
    observation = maintenance_readiness(packet, modules, git, selected=True, owned=True)
    observation.update(schema="cre-runtime-maintenance-observation-v1", release_sha256=sha(raw),
                       observed_at=native_observation["observed_at"], qualified_at=now().isoformat(),
                       tasks=native_observation["tasks"], windows_resources=native_observation["windows_resources"],
                       execution=native_observation["execution"])
    fields = ("image", "started_at", "status")
    actual = {row["id"]: {key: row[key] for key in fields} for row in after}
    sampled = {row["id"]: {key: row[key] for key in fields} for row in observation["containers"]}
    require(len(sampled) == len(observation["containers"]) and sampled == actual,
            "selected container readback differs from recreated fleet")
    require(unique(observe_containers(docker), "service") == unique(after, "service"),
            "container configuration changed during selected host observation")
    selected_snapshot = {"containers": observation["containers"]}
    write_new(output / "selected-runtime-snapshot.json", selected_snapshot)
    validate_maintenance_observation(packet, evidence, observation, now(), selected=True, owned=True,
                                     selected_snapshot=selected_snapshot)
    boundary()
    ready = {"schema": "cre-runtime-maintenance-ready-v1", "release_sha256": sha(raw),
            "authorization_sha256": sha(read_file(args.authorization)),
            "operation_output": canonical_path(str(output)), "old": packet["old"], "new": packet["new"],
            "recovery_required_at_publication": True, "readiness": observation,
            "release_reference": _reference(args.release), "mutation": _reference(mutation_path),
            "initial_observation": _reference(args.observation), "authorization": _reference(args.authorization),
            "before_snapshot": _reference(output / "containers-before.json"),
            "after_snapshot": _reference(output / "containers-after.json"),
            "selected_snapshot": _reference(output / "selected-runtime-snapshot.json"),
            "git_observation": _reference(output / "git-observation.json"), "commands": commands,
            "dependencies_before": dependency_before, "dependencies_after": dependencies}
    if packet["expected_images"]:
        ready["projection"] = _reference(local_path(packet["projection_output"]) / "configuration-projection.json")
    return ready
