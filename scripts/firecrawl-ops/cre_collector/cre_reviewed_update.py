"""Determine the inputs affected by a reviewed Windows runtime update."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

from cre_code_handoff_git import (
    Git,
    installed_dependencies,
    local_path,
    read_file,
    windows_path,
)
from cre_runtime_maintenance_build import SERVICES

SERVICE_CONTEXTS = {context + "/": service for service, context in SERVICES.items()}
COLLECTOR_LOCKS = {
    "scripts/firecrawl-ops/cre_collector/package.json",
    "scripts/firecrawl-ops/cre_collector/package-lock.json",
}
DEPENDENCY_FILES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "requirements.txt",
    "pyproject.toml",
    "uv.lock",
}


def classify_paths(paths):
    """Conservatively identify image contexts and unsupported host dependencies.

    Compare both checkout revisions separately. The source-only updater
    refuses image, Compose, and unsupported dependency changes.
    """
    if not isinstance(paths, (list, tuple)):
        raise TypeError("changed paths must be a list or tuple")
    services, unsupported = set(), set()
    install = compose = False
    for path in paths:
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise ValueError("changed path must be a canonical relative Git path")
        affected_contexts = {
            service
            for prefix, service in SERVICE_CONTEXTS.items()
            if path.startswith(prefix)
            or prefix.rstrip("/") == path
            or prefix.startswith(path + "/")
        }
        if affected_contexts:
            services.update(affected_contexts)
        elif path in COLLECTOR_LOCKS or any(
            lock.startswith(path + "/") for lock in COLLECTOR_LOCKS
        ):
            install = True
        elif PurePosixPath(path).name in DEPENDENCY_FILES:
            unsupported.add(path)
        if path in {
            "docker-compose.yaml",
            "docker-compose.yml",
            "docker-compose.override.yaml",
            "docker-compose.override.yml",
            "compose.yaml",
            "compose.yml",
            "compose.override.yaml",
            "compose.override.yml",
        }:
            compose = True
    return {
        "collector_install": install,
        "image_services": sorted(services),
        "compose_changed": compose,
        "unsupported": sorted(unsupported),
    }


def changed_paths_between(git, repo, old, selected):
    """Compare immutable commits only, rejecting a non-forward update."""
    if any(
        not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None
        for sha in (old, selected)
    ):
        raise ValueError("exact lowercase Git commit IDs are required")
    if any(
        git.text(repo, "cat-file", "-t", sha) != "commit" for sha in (old, selected)
    ):
        raise ValueError("Git object must be a commit")
    git.command(repo, "merge-base", "--is-ancestor", old, selected)
    raw = git.command(repo, "diff", "--no-renames", "--name-only", "-z", old, selected)
    if raw and not raw.endswith(b"\0"):
        raise ValueError("Git returned a truncated path list")
    paths = [
        value.decode("utf-8", errors="strict") for value in raw.split(b"\0") if value
    ]
    if len(set(paths)) != len(paths):
        raise ValueError("Git returned duplicate changed paths")
    classify_paths(paths)
    return sorted(paths)


def inspect_git_update(git, development, native, runtime, selected):
    """Inspect three clean standalone roots and an exact forward selected commit.

    This only reads Git; the caller remains responsible for fetching and
    reviewing the remote main revision before invoking it.
    """
    if not isinstance(selected, str) or re.fullmatch(r"[0-9a-f]{40}", selected) is None:
        raise ValueError("an exact lowercase selected Git commit is required")
    inputs = [local_path(root) for root in (development, native, runtime)]
    if any(
        not root.is_absolute()
        or any(part.is_symlink() for part in (root, *root.parents))
        for root in inputs
    ):
        raise ValueError("Git roots must be absolute and unaliased")
    roots = [root.resolve(strict=True) for root in inputs]
    if any(
        a == b or a in b.parents or b in a.parents
        for i, a in enumerate(roots)
        for b in roots[i + 1 :]
    ):
        raise ValueError("development and runtime roots must be distinct")

    def identity(root):
        sha = git.text(root, "rev-parse", "HEAD")
        tree = git.text(root, "rev-parse", "HEAD^{tree}")
        return {"sha": sha, "tree": tree}

    if not (inputs[0] / ".git").is_dir() or (inputs[0] / ".git").is_symlink():
        raise ValueError("development source must be a standalone Git checkout")
    dev_identity = identity(development)
    origin_main = git.text(development, "rev-parse", "refs/remotes/origin/main")
    if git.text(development, "cat-file", "-t", selected) != "commit":
        raise ValueError("selected Git object is not a commit")
    git.command(development, "merge-base", "--is-ancestor", selected, origin_main)
    selected_tree = git.text(development, "rev-parse", selected + "^{tree}")
    result = {
        "development": dev_identity,
        "origin_main": origin_main,
        "selected": {"sha": selected, "tree": selected_tree},
    }
    for role, root in (("native", native), ("runtime", runtime)):
        old = identity(root)
        git.clean(root, old)
        tracking = git.text(root, "rev-parse", "refs/remotes/origin/main")
        git.command(development, "merge-base", "--is-ancestor", old["sha"], tracking)
        git.command(development, "merge-base", "--is-ancestor", tracking, selected)
        paths = changed_paths_between(git, development, old["sha"], selected)
        result[role] = {"before": old, "tracking_sha": tracking, "changed_paths": paths}
    if git.text(development, "rev-parse", "refs/remotes/origin/main") != origin_main:
        raise ValueError("reviewed origin/main changed during inspection")
    return result


def assert_live_owner(runtime, pid, generation):
    """Read-only cross-process check of the canonical held recovery authority."""
    import cre_checkpoint_refresh as checkpoint
    from cre_posix import fcntl

    if (
        type(pid) is not int
        or pid <= 0
        or not isinstance(generation, str)
        or not generation
    ):
        raise ValueError("exact owner identity required")
    lock = checkpoint.canonical_shared_lock_dir(local_path(runtime))
    authority = lock.with_name(lock.name + ".authority")
    if checkpoint._lock_owner(lock) != pid or not checkpoint._pid_alive(pid):
        raise ValueError("update owner absent")
    fd = os.open(authority, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (
            authority.stat().st_dev,
            authority.stat().st_ino,
        ):
            raise ValueError("authority alias changed")
        fields = checkpoint.SharedLock._authority_fields(fd)
        if (
            fields is None
            or fields[0] != pid
            or fields[2] != generation
            or fields[3] is not True
        ):
            raise ValueError("update authority differs")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            raise ValueError("update authority is not held")
        if (
            checkpoint.SharedLock._authority_fields(fd) != fields
            or checkpoint._lock_owner(lock) != pid
            or (info.st_dev, info.st_ino)
            != (authority.lstat().st_dev, authority.lstat().st_ino)
        ):
            raise ValueError("update authority changed during check")
    finally:
        os.close(fd)


def _git_advance_inputs(git, source, before, selected):
    paths = changed_paths_between(git, source, before["sha"], selected["sha"])
    old_entries = git.entries(source, before["sha"])
    new_entries = git.entries(source, selected["sha"])
    objects = {
        path: {"before": old_entries.get(path), "after": new_entries.get(path)}
        for path in paths
    }
    dependencies = [
        path for path in paths if PurePosixPath(path).name in DEPENDENCY_FILES
    ]
    allowed = sorted(COLLECTOR_LOCKS & set(paths))
    if sorted(dependencies) != allowed:
        raise ValueError("unsupported dependency change")
    release = {
        "deployed": before,
        "selected": selected,
        "allowed_paths": paths,
        "objects": objects,
    }

    return release, allowed


def advance_checkout(
    git, lease, runtime, checkout, before, selected, source, bundle, check_held
):
    """Advance one real checkout only while the caller proves the fleet held."""
    import cre_checkpoint_refresh as checkpoint

    if lease.path != checkpoint.canonical_shared_lock_dir(local_path(runtime)):
        raise ValueError("owner lock is not the qualified runtime lock")
    if not callable(check_held):
        raise TypeError("live held-fleet check is required")
    release, allowed = _git_advance_inputs(git, source, before, selected)

    def assert_owner():
        descriptor = lease._owned_directory_fd()
        os.close(descriptor)

    class OwnedGit(type(git)):
        def command(self, repo, *args, **kwargs):
            changing = bool(args) and args[0] in {"fetch", "update-ref", "merge"}
            if changing:
                assert_owner()
            try:
                return super().command(repo, *args, **kwargs)
            finally:
                if changing:
                    assert_owner()

    owned_git = OwnedGit(git.native_git)
    assert_owner()
    check_held()
    owned_git.clean(checkout, before)
    assert_owner()
    if before == selected:
        owned_git.clean(checkout, selected, tracking=selected["sha"])
        check_held()
        assert_owner()
        return
    owned_git.maintenance_fast_forward(
        checkout, release, bundle, allowed_dependency_paths=allowed
    )
    assert_owner()
    check_held()


def preflight_checkout_paths(git, targets, selected, source):
    """Read both clean roots and collision-check their exact selected changes."""
    for target in targets:
        release, allowed = _git_advance_inputs(git, source, target["before"], selected)
        git.clean(target["root"], target["before"], tracking=target["tracking"])
        git.paths(
            target["root"],
            release,
            object_repo=source,
            allowed_dependency_paths=allowed,
            allow_ignored_absent=True,
        )


def advance_checkouts(
    git, lease, runtime, targets, selected, source, bundle, check_held
):
    """Preflight both captured checkouts, then advance each under the owner lock."""
    if not isinstance(targets, (list, tuple)) or len(targets) != 2:
        raise ValueError("exactly two checkout targets are required")
    if any(
        not isinstance(target, dict) or set(target) != {"root", "before", "tracking"}
        for target in targets
    ):
        raise ValueError("captured checkout bindings are required")
    roots = [local_path(target["root"]) for target in targets]
    if roots.count(local_path(runtime)) != 1:
        raise ValueError("qualified runtime must be one checkout target")
    if (
        roots[0] == roots[1]
        or roots[0] in roots[1].parents
        or roots[1] in roots[0].parents
    ):
        raise ValueError("checkout targets must be separate")
    import cre_checkpoint_refresh as checkpoint

    if lease.path != checkpoint.canonical_shared_lock_dir(local_path(runtime)):
        raise ValueError("owner lock is not the qualified runtime lock")
    descriptor = lease._owned_directory_fd()
    os.close(descriptor)
    check_held()
    preflight_checkout_paths(git, targets, selected, source)
    descriptor = lease._owned_directory_fd()
    os.close(descriptor)
    check_held()
    for target in targets:
        advance_checkout(
            git,
            lease,
            runtime,
            target["root"],
            target["before"],
            selected,
            source,
            bundle,
            check_held,
        )


class NativeStageFailed(RuntimeError):
    """Native stage process completed normally but reported failure."""


def run_source_update(
    selected_sha,
    *,
    development=None,
    native=None,
    runtime=None,
    native_git=None,
    powershell=None,
    docker=None,
):
    """Perform a reviewed source-only update in the foreground on this Windows host."""
    import hashlib
    import pwd

    import cre_checkpoint_refresh as checkpoint
    from cre_code_handoff import visible_linux_frontends, write_new
    from cre_ingest import SOURCE_TO_BROKERAGE
    from cre_refresh_pipeline import command_poll_hook

    development = (
        Path(development)
        if development is not None
        else Path(__file__).resolve().parents[3]
    )
    native = Path(native) if native is not None else development.parent / "firecrawl"
    runtime = (
        Path(runtime) if runtime is not None else Path("/home/cayman/code/firecrawl")
    )
    native_git = (
        Path(native_git)
        if native_git is not None
        else Path("/mnt/c/Program Files/Git/bin/git.exe")
    )
    powershell = (
        Path(powershell)
        if powershell is not None
        else development.parents[1] / "AppData/Local/Programs/PowerShell/7/pwsh.exe"
    )
    docker = (
        Path(docker)
        if docker is not None
        else Path("/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe")
    )
    if os.geteuid() != pwd.getpwnam("cayman").pw_uid:
        raise ValueError("the reviewed update must run as the configured WSL user")
    for program in (native_git, powershell):
        if not program.is_file() or program.is_symlink():
            raise ValueError("required native program unavailable")
    git = Git(native_git)
    plan = inspect_git_update(git, development, native, runtime, selected_sha)
    selected = plan["selected"]
    if plan["origin_main"] != selected_sha:
        raise ValueError("selected commit is not current reviewed origin/main")
    git.clean(development, selected, tracking=selected_sha)
    for role in ("native", "runtime"):
        scope = classify_paths(plan[role]["changed_paths"])
        if scope["compose_changed"] or scope["image_services"]:
            raise ValueError("affected image or Compose update is not yet supported")
        if scope["collector_install"] or scope["unsupported"]:
            raise ValueError("dependency change is not yet supported")
    if visible_linux_frontends():
        raise ValueError("competing local CRE frontend exists")
    installed_dependencies(runtime / "scripts/firecrawl-ops/cre_collector")

    def observe_health():
        """Return bounded local observations; do not fence on service readiness."""
        result = {"containers": "unknown", "api": "unknown", "browser": "unknown"}
        environment = dict(os.environ)
        for key in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "DOCKER_TLS",
        ):
            environment.pop(key, None)
        names = [
            "firecrawl-api-1",
            "firecrawl-nuq-postgres-1",
            "firecrawl-playwright-service-1",
            "firecrawl-redis-1",
            "firecrawl-rabbitmq-1",
        ]
        try:
            completed = subprocess.run(
                [
                    str(docker),
                    "--host",
                    "npipe:////./pipe/dockerDesktopLinuxEngine",
                    "inspect",
                    *names,
                ],
                check=True,
                capture_output=True,
                env=environment,
                timeout=10,
            )
            rows = json.loads(completed.stdout)
            services = {}
            for row in rows:
                service = row["Config"]["Labels"]["com.docker.compose.service"]
                state = row["State"]
                if service in services:
                    raise ValueError("duplicate service")
                if state.get("Status") != "running":
                    services[service] = "not-running"
                elif isinstance(state.get("Health"), dict):
                    status = state["Health"].get("Status")
                    services[service] = (
                        status
                        if status in {"healthy", "unhealthy", "starting"}
                        else "unknown"
                    )
                elif service in {"nuq-postgres", "rabbitmq"}:
                    services[service] = "unknown"
                else:
                    services[service] = "running"
            if set(services) != {
                "api",
                "nuq-postgres",
                "playwright-service",
                "redis",
                "rabbitmq",
            }:
                raise ValueError("service inventory differs")
            result["containers"] = services
        except Exception:  # noqa: BLE001 - informational read-only observation
            result["containers"] = "unknown"
        try:
            ops_path = str(development / "scripts/firecrawl-ops")
            if ops_path not in sys.path:
                sys.path.insert(0, ops_path)
            from firecrawl_operator_handoff import ReadOnlyLoopbackClient

            root = ReadOnlyLoopbackClient(
                "http://127.0.0.1:3002", timeout_seconds=5
            ).get_json("/")
            result["api"] = (
                "identity-ok"
                if root.get("message") == "Firecrawl API"
                else "unexpected-response"
            )
        except Exception:  # noqa: BLE001 - informational read-only observation
            result["api"] = "unknown"
        try:
            script = "fetch('http://127.0.0.1:'+(process.env.PORT||'3000')+'/health',{signal:AbortSignal.timeout(3000)}).then(async r=>{if(!r.ok)throw Error('health');process.stdout.write(await r.text())}).catch(()=>process.exit(1))"
            completed = subprocess.run(
                [
                    str(docker),
                    "--host",
                    "npipe:////./pipe/dockerDesktopLinuxEngine",
                    "exec",
                    "firecrawl-playwright-service-1",
                    "node",
                    "-e",
                    script,
                ],
                check=True,
                capture_output=True,
                env=environment,
                timeout=10,
            )
            browser = json.loads(completed.stdout)
            result["browser"] = (
                "healthy"
                if isinstance(browser, dict) and browser.get("status") == "healthy"
                else "unhealthy"
            )
        except Exception:  # noqa: BLE001 - informational read-only observation
            result["browser"] = "unknown"
        return result

    initial_health = observe_health()
    scratch = development / ".scratch"
    scratch.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="reviewed-update-", dir=scratch))
    bundle = output / "selected.bundle"
    git.command(
        development,
        "bundle",
        "create",
        windows_path(bundle),
        "refs/remotes/origin/main",
    )
    release = {
        "roots": {
            "native": windows_path(native),
            "runtime": str(runtime),
            "development": windows_path(development),
        },
        "wsl": {
            "distro": "Ubuntu-24.04",
            "user": "cayman",
            "env_file": "/home/cayman/.config/cre/equire.env",
        },
        "supported_sources": list(SOURCE_TO_BROKERAGE),
        "task_baseline": [
            {
                "key": "interra",
                "task_path": "\\Agentic Assets\\CRE\\",
                "task_name": "Interra Refresh",
            },
            {
                "key": "newmark",
                "task_path": "\\Agentic Assets\\CRE\\",
                "task_name": "Newmark Refresh",
            },
            {
                "key": "qualified",
                "task_path": "\\Agentic Assets\\CRE\\",
                "task_name": "Qualified Brokerage Pipeline",
            },
        ],
    }
    task_step = (
        development
        / "scripts/firecrawl-ops/cre_collector/windows/Invoke-CreReviewedUpdate.ps1"
    )
    control = Path(__file__).resolve()
    control_sha = hashlib.sha256(control.read_bytes()).hexdigest()
    lock = checkpoint.canonical_shared_lock_dir(runtime)
    old = plan["runtime"]["before"]
    targets = [
        {
            "root": root,
            "before": plan[role]["before"],
            "tracking": plan[role]["tracking_sha"],
        }
        for role, root in (("native", native), ("runtime", runtime))
    ]
    started_hold = False
    reconciled_after = None
    hold_reconciled = False
    phase = "inspection"
    with checkpoint.SharedLock(lock, recovery_required=True) as lease:

        def owner():
            descriptor = lease._owned_directory_fd()
            os.close(descriptor)

        def native_stage(name, plan_path):
            owner()
            git.clean(development, selected, tracking=selected_sha)
            if hashlib.sha256(control.read_bytes()).hexdigest() != control_sha:
                raise ValueError("update control code changed")
            raw = plan_path.read_bytes()
            plan_sha = hashlib.sha256(raw).hexdigest()
            result_file = output / (name.lower() + "-" + str(time.time_ns()) + ".json")
            failure_file = output / (
                name.lower() + "-" + str(time.time_ns()) + ".failure.json"
            )
            log = output / (name.lower() + "-" + str(time.time_ns()) + ".log")
            argv = [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-File",
                windows_path(task_step),
                "-Stage",
                name,
                "-Plan",
                windows_path(plan_path),
                "-PlanSha",
                plan_sha,
                "-Control",
                windows_path(control),
                "-ControlSha",
                control_sha,
                "-OwnerPid",
                str(os.getpid()),
                "-Generation",
                lease.authority_generation,
                "-Output",
                windows_path(result_file),
                "-FailureOutput",
                windows_path(failure_file),
            ]
            deadline = time.monotonic() + 180

            def poll():
                owner()
                if time.monotonic() >= deadline:
                    raise TimeoutError("native update step timed out")

            with command_poll_hook(poll):
                rc = checkpoint.run_command(
                    argv, log, env=checkpoint.safe_process_env()
                )
            owner()
            git.clean(development, selected, tracking=selected_sha)
            if plan_path.read_bytes() != raw:
                raise RuntimeError(
                    "native task stage inputs changed; preserve recovery fence"
                )
            if rc != 0:
                expected = {
                    "stage": name,
                    "plan_sha256": plan_sha,
                    "owner_pid": os.getpid(),
                    "generation_sha256": hashlib.sha256(
                        lease.authority_generation.encode()
                    ).hexdigest(),
                    "completed": True,
                }
                try:
                    raw_failure = read_file(failure_file)
                    receipt = (
                        json.loads(raw_failure) if len(raw_failure) <= 4096 else None
                    )
                except Exception:  # noqa: BLE001 - missing or unsafe receipt means unknown
                    receipt = None
                if (
                    rc > 0
                    and isinstance(receipt, dict)
                    and receipt == expected
                    and receipt.get("completed") is True
                    and type(receipt.get("owner_pid")) is int
                ):
                    raise NativeStageFailed(
                        "native task stage reported failure after completion"
                    )
                raise RuntimeError(
                    "native task stage settlement is unknown; preserve recovery fence"
                )
            return json.loads(result_file.read_text(encoding="utf-8-sig"))

        try:
            preflight_checkout_paths(git, targets, selected, development)
            owner()
            if visible_linux_frontends():
                raise ValueError("competing local CRE frontend exists")
            inspect_plan = output / "inspect-plan.json"
            write_new(
                inspect_plan,
                {"release": release, "old_sha": old["sha"], "new_sha": selected_sha},
            )
            snapshot = native_stage("Inspect", inspect_plan)
            release["qualified_sources"] = snapshot["qualified_sources"]
            release["task_baseline"] = snapshot["task_baseline"]
            release["reviewed_fleet"] = True
            active_plan = output / "active-plan.json"
            write_new(
                active_plan,
                {
                    "release": release,
                    "old_sha": old["sha"],
                    "new_sha": selected_sha,
                    "snapshot": snapshot["snapshot"],
                    "digest": snapshot["digest"],
                },
            )
            mid_health = None
            if all(target["before"] == selected for target in targets):
                native_stage("VerifyRestored", active_plan)
            else:
                phase = "hold"
                started_hold = True
                native_stage("Hold", active_plan)
                phase = "advance"

                def held():
                    native_stage("VerifyHeld", active_plan)

                advance_checkouts(
                    git, lease, runtime, targets, selected, development, bundle, held
                )
                installed_dependencies(runtime / "scripts/firecrawl-ops/cre_collector")
                mid_health = observe_health()
                owner()
                if old != selected:
                    phase = "repin"
                    native_stage("Repin", active_plan)
                phase = "restore"
                native_stage("Restore", active_plan)
                native_stage("VerifyRestored", active_plan)
            owner()
            final_health = observe_health()
            owner()
            lease.clear_recovery_requirement()
        except BaseException as original:
            if not started_hold:
                lease.clear_recovery_requirement()
            elif phase == "hold" and isinstance(original, NativeStageFailed):
                try:
                    owner()
                    for target in targets:
                        git.clean(
                            target["root"],
                            target["before"],
                            tracking=target["tracking"],
                        )
                    native_stage("ReconcileHold", active_plan)
                    native_stage("VerifyOriginal", active_plan)
                    for target in targets:
                        git.clean(
                            target["root"],
                            target["before"],
                            tracking=target["tracking"],
                        )
                    owner()
                    lease.clear_recovery_requirement()
                    hold_reconciled = True
                except Exception as secondary:  # noqa: BLE001 - retain the original failure and fence
                    if hasattr(original, "add_note"):
                        original.add_note(
                            "Hold reconciliation uncertain: " + type(secondary).__name__
                        )
            elif phase in {"repin", "restore"} and isinstance(
                original, NativeStageFailed
            ):
                try:
                    owner()
                    for target in targets:
                        git.clean(target["root"], selected, tracking=selected_sha)
                    installed_dependencies(
                        runtime / "scripts/firecrawl-ops/cre_collector"
                    )
                    if phase == "repin":
                        native_stage("CompleteRepin", active_plan)
                    native_stage("CompleteRestore", active_plan)
                    native_stage("VerifyRestored", active_plan)
                    for target in targets:
                        git.clean(target["root"], selected, tracking=selected_sha)
                    owner()
                    final_health = observe_health()
                    owner()
                    lease.clear_recovery_requirement()
                    reconciled_after = phase
                except Exception as secondary:  # noqa: BLE001 - retain the original failure and fence
                    if hasattr(original, "add_note"):
                        original.add_note(
                            "Task reconciliation uncertain: " + type(secondary).__name__
                        )
            if reconciled_after is None:
                if hasattr(original, "add_note"):
                    if hold_reconciled:
                        original.add_note(
                            "Original task flags were restored; the update was not applied."
                        )
                    else:
                        original.add_note(
                            "Inspect current owner and task state before any retry."
                        )
                    original.add_note(f"Update phase: {phase}; logs: {output}")
                raise
    return {
        "status": "completed",
        "selected": selected_sha,
        "output": str(output),
        "existing_image_provenance": "not established",
        "image_changes_applied": False,
        "reconciled_after": reconciled_after,
        "service_health_before": initial_health,
        "service_health_after_advance": mid_health,
        "service_health_after": final_health,
    }


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--owner-check":
        assert_live_owner(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    else:
        parser = argparse.ArgumentParser(
            description="Apply a reviewed source-only Windows update"
        )
        parser.add_argument("--selected-sha", required=True)
        args = parser.parse_args()
        print(json.dumps(run_source_update(args.selected_sha), sort_keys=True))
