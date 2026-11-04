"""Staged collector dependency installation for reviewed Windows updates."""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import cre_checkpoint_refresh as checkpoint
from cre_code_handoff_evidence import identity, require, sha
from cre_code_handoff_git import installed_dependencies, local_path, read_file
from cre_refresh_pipeline import command_poll_hook

COLLECTOR = Path("scripts/firecrawl-ops/cre_collector")


def reject_local_dependencies(manifest, lock):
    """Refuse links whose meaning depends on the staging checkout location."""
    require(isinstance(manifest, dict) and isinstance(lock, dict), "dependency manifests invalid")
    require(not manifest.get("workspaces"), "collector workspaces require separate handling")
    pending = [manifest, lock]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            require(value.get("link") is not True, "linked dependencies are unsupported")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            require(not value.startswith(("file:", "link:", "workspace:")),
                    "local dependencies are unsupported")


def validate_dependency_links(folder):
    """Reject installed links that leave their staged node_modules tree."""
    root = Path(folder)
    require(root.is_dir() and not root.is_symlink(), "staged node_modules missing or aliased")
    root = root.resolve(strict=True)
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(parent) / name
            if path.is_symlink():
                target = path.resolve(strict=True)
                require(target == root or root in target.parents, "staged dependency link escapes tree")


def dependency_versions(cwd):
    versions = {}
    for name in ("node", "npm"):
        result = subprocess.run([name, "--version"], cwd=cwd, capture_output=True, timeout=30, check=False)
        require(result.returncode == 0 and result.stdout.strip(), "dependency tool version unavailable")
        versions[name] = result.stdout.decode().strip()
    architecture = subprocess.run(["node", "-p", "process.platform + ':' + process.arch"],
                                  cwd=cwd, capture_output=True, timeout=30, check=False)
    require(architecture.returncode == 0 and architecture.stdout.strip(), "Node architecture unavailable")
    versions["platform"] = architecture.stdout.decode().strip()
    return versions


def prepare_current_dependencies(git, source, selected, runtime, output):
    """Install selected collector dependencies in a separate same-filesystem clone."""
    identity(selected)
    source, runtime, output = map(local_path, (source, runtime, output))
    git.clean(source, selected, tracking=selected["sha"])
    current = runtime / COLLECTOR
    previous = installed_dependencies(current)
    old_modules = current / "node_modules"
    old_stat = old_modules.lstat()
    require(old_modules.is_dir() and not old_modules.is_symlink(), "runtime node_modules is unsafe")
    stage = Path(tempfile.mkdtemp(prefix=".cre-reviewed-deps-", dir=runtime.parent))
    require(os.stat(stage).st_dev == os.stat(runtime).st_dev, "dependency stage must share runtime filesystem")
    repo = stage / "source"
    clone = ["git", "clone", "--no-hardlinks", "--no-local", "--no-checkout", str(source), str(repo)]
    result = subprocess.run(clone, capture_output=True, timeout=180, check=False)
    require(result.returncode == 0, "isolated dependency clone failed")
    git.hooks(repo)
    result = subprocess.run(["git", "-C", str(repo), "checkout", "--detach", selected["sha"]],
                            capture_output=True, timeout=180, check=False)
    require(result.returncode == 0, "isolated dependency checkout failed")
    git.clean(repo, selected, branch="")
    collector = repo / COLLECTOR
    for name in ("package.json", "package-lock.json"):
        expected = git.command(source, "show", selected["sha"] + ":" + str(COLLECTOR / name))
        require(read_file(collector / name) == expected, "staged dependency input differs")
    reject_local_dependencies(json.loads(read_file(collector / "package.json")),
                              json.loads(read_file(collector / "package-lock.json")))
    versions = dependency_versions(collector)
    log = output / "dependency-install.log"
    require(not os.path.lexists(log), "dependency install log already exists")
    deadline = time.monotonic() + 1800

    def poll():
        if time.monotonic() >= deadline:
            raise TimeoutError("staged dependency installation timed out")

    command = ["bash", "-c", 'cd -- "$1" && exec npm ci --ignore-scripts --no-audit --no-fund',
               "cre-reviewed-dependencies", str(collector)]
    with command_poll_hook(poll):
        code = checkpoint.run_command(command, log, env=checkpoint.safe_process_env())
    require(code == 0, "staged dependency installation failed")
    staged = installed_dependencies(collector)
    validate_dependency_links(collector / "node_modules")
    result = subprocess.run(["npm", "ls", "--all", "--json"], cwd=collector,
                            capture_output=True, timeout=120, check=False)
    require(result.returncode == 0, "staged dependency graph invalid")
    require(installed_dependencies(collector) == staged, "staged dependencies changed during verification")
    git.clean(repo, selected, branch="")
    git.clean(source, selected, tracking=selected["sha"])
    staged_stat = (collector / "node_modules").lstat()
    return {"selected": selected, "stage": str(stage), "previous": previous, "staged": staged,
            "old_identity": [old_stat.st_dev, old_stat.st_ino],
            "new_identity": [staged_stat.st_dev, staged_stat.st_ino], "versions": versions}


def recheck_current_dependencies(plan, selected, runtime, git):
    """Recheck selected staging and the original tree before mutation."""
    identity(selected)
    require(isinstance(plan, dict) and plan.get("selected") == selected,
            "dependency stage is not selected-source bound")
    runtime = local_path(runtime)
    stage = local_path(plan["stage"])
    require(stage.parent == runtime.parent and stage.is_dir() and not stage.is_symlink(),
            "dependency stage location differs")
    staged_collector = stage / "source" / COLLECTOR
    git.clean(stage / "source", selected, branch="")
    require(installed_dependencies(staged_collector) == plan["staged"], "staged dependencies changed")
    validate_dependency_links(staged_collector / "node_modules")
    require(dependency_versions(staged_collector) == plan["versions"], "dependency toolchain changed")
    current = runtime / COLLECTOR / "node_modules"
    old_stat = current.lstat()
    staged_stat = (staged_collector / "node_modules").lstat()
    require(not current.is_symlink() and [old_stat.st_dev, old_stat.st_ino] == plan["old_identity"]
            and [staged_stat.st_dev, staged_stat.st_ino] == plan["new_identity"]
            and sha(read_file(current / ".package-lock.json")) == plan["previous"]["node_modules/.package-lock.json"],
            "dependency trees changed before apply")


def verify_current_dependencies(plan, selected, runtime):
    """Verify the installed selected dependency tree before restoring tasks."""
    identity(selected)
    require(plan.get("selected") == selected, "dependency stage is not selected-source bound")
    collector = local_path(runtime) / COLLECTOR
    modules = collector / "node_modules"
    stat = modules.lstat()
    require(not modules.is_symlink() and [stat.st_dev, stat.st_ino] == plan["new_identity"]
            and installed_dependencies(collector) == plan["staged"],
            "installed selected dependencies differ")


def apply_current_dependencies(plan, selected, runtime, owner, git):
    """Swap a verified staged tree while preserving the original for recovery."""
    identity(selected)
    require(isinstance(plan, dict) and plan.get("selected") == selected,
            "dependency stage is not selected-source bound")
    runtime = local_path(runtime)
    recheck_current_dependencies(plan, selected, runtime, git)
    collector = runtime / COLLECTOR
    stage = local_path(plan["stage"])
    staged_collector = stage / "source" / COLLECTOR
    current = collector / "node_modules"
    replacement = staged_collector / "node_modules"
    backup = stage / "previous-node_modules"
    require(stage.parent == runtime.parent and not stage.is_symlink()
            and stage.is_dir() and not backup.exists() and not backup.is_symlink(),
            "dependency stage location differs")
    for name in ("package.json", "package-lock.json"):
        require(sha(read_file(collector / name)) == plan["staged"][name],
                "selected runtime dependency manifest differs")
    old_stat, new_stat = current.lstat(), replacement.lstat()
    require(not current.is_symlink() and not replacement.is_symlink()
            and [old_stat.st_dev, old_stat.st_ino] == plan["old_identity"]
            and [new_stat.st_dev, new_stat.st_ino] == plan["new_identity"]
            and old_stat.st_dev == new_stat.st_dev,
            "dependency tree identity changed")
    require(sha(read_file(current / ".package-lock.json")) == plan["previous"]["node_modules/.package-lock.json"]
            and installed_dependencies(staged_collector) == plan["staged"],
            "dependency tree content changed")
    validate_dependency_links(replacement)
    owner()
    require(dependency_versions(staged_collector) == plan["versions"], "dependency toolchain changed")
    owner()
    os.rename(current, backup)
    owner()
    os.rename(replacement, current)
    owner()
    verify_current_dependencies(plan, selected, runtime)
    owner()
    return {"stage": str(stage), "previous_tree_retained": True}
