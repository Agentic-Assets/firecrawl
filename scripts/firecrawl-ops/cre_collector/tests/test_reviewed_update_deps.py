"""Isolated dependency staging and owner-bound swap fixtures."""

import json
import subprocess
from pathlib import Path

import cre_reviewed_update_deps as direct
import pytest
from cre_code_handoff_evidence import HandoffError


def write_manifests(folder, version):
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"name": "fixture", "version": version, "dependencies": {}, "devDependencies": {}}
    lock = {"lockfileVersion": 3, "packages": {"": {"dependencies": {}, "devDependencies": {}}}}
    (folder / "package.json").write_text(json.dumps(manifest))
    (folder / "package-lock.json").write_text(json.dumps(lock))
    modules = folder / "node_modules"
    modules.mkdir()
    (modules / ".package-lock.json").write_text(json.dumps({"packages": {}}))
    return manifest, lock


def fixture_stage(monkeypatch, tmp_path, npm_exit=0):
    source = tmp_path / "development"
    source.mkdir()
    runtime = tmp_path / "code" / "runtime"
    runtime.mkdir(parents=True)
    output = tmp_path / "output"
    output.mkdir()
    current = runtime / direct.COLLECTOR
    write_manifests(current, "old")
    selected_dir = tmp_path / "selected-manifests"
    write_manifests(selected_dir, "new")
    selected = {"sha": "a" * 40, "tree": "b" * 40}

    class Git:
        def hooks(self, *args):
            pass
        def clean(self, *args, **kwargs):
            pass
        def command(self, root, *args):
            return (selected_dir / args[-1].split(":")[-1].split("/")[-1]).read_bytes()

    def run(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            repo = Path(argv[-1])
            folder = repo / direct.COLLECTOR
            folder.mkdir(parents=True)
            for name in ("package.json", "package-lock.json"):
                (folder / name).write_bytes((selected_dir / name).read_bytes())
        value = b"linux:x64" if "-p" in argv else b"v1"
        return subprocess.CompletedProcess(argv, 0, stdout=value)

    def run_owned(argv, log, *, env=None):
        assert argv[:2] == ["bash", "-c"] and "--ignore-scripts" in argv[2]
        if npm_exit:
            return npm_exit
        modules = Path(argv[-1]) / "node_modules"
        modules.mkdir()
        (modules / ".package-lock.json").write_text(json.dumps({"packages": {}}))
        Path(log).write_text("settled fixture")
        return 0

    monkeypatch.setattr(direct.subprocess, "run", run)
    monkeypatch.setattr(direct.checkpoint, "run_command", run_owned)
    git = Git()
    plan = direct.prepare_current_dependencies(git, source, selected, runtime, output)
    for name in ("package.json", "package-lock.json"):
        (current / name).write_bytes((selected_dir / name).read_bytes())
    return plan, selected, runtime, current, git


def test_staged_dependency_swap_retains_original_tree(monkeypatch, tmp_path):
    plan, selected, runtime, current, git = fixture_stage(monkeypatch, tmp_path)
    old = (current / "node_modules").stat().st_ino
    result = direct.apply_current_dependencies(plan, selected, runtime, lambda: None, git)
    assert result["previous_tree_retained"] is True
    assert (Path(plan["stage"]) / "previous-node_modules").stat().st_ino == old
    assert (current / "node_modules").stat().st_ino == plan["new_identity"][1]


def test_dependency_owner_loss_after_first_rename_does_not_continue(monkeypatch, tmp_path):
    plan, selected, runtime, current, git = fixture_stage(monkeypatch, tmp_path)
    calls = []
    def owner():
        calls.append(None)
        if len(calls) == 3:
            raise RuntimeError("owner lost")
    with pytest.raises(RuntimeError, match="owner lost"):
        direct.apply_current_dependencies(plan, selected, runtime, owner, git)
    assert not (current / "node_modules").exists()
    assert (Path(plan["stage"]) / "previous-node_modules").is_dir()
    assert (Path(plan["stage"]) / "source" / direct.COLLECTOR / "node_modules").is_dir()


def test_rejects_local_dependency_and_escaping_link(tmp_path):
    with pytest.raises(HandoffError, match="local dependencies"):
        direct.reject_local_dependencies({"dependencies": {"x": "file:../x"}}, {})
    with pytest.raises(HandoffError, match="linked dependencies"):
        direct.reject_local_dependencies({}, {"packages": {"node_modules/x": {"link": True}}})
    root = tmp_path / "node_modules"
    root.mkdir()
    external = tmp_path / "outside"
    external.write_text("outside")
    (root / "link").symlink_to(external)
    with pytest.raises(HandoffError, match="escapes"):
        direct.validate_dependency_links(root)


def test_failed_staged_install_never_changes_runtime(monkeypatch, tmp_path):
    with pytest.raises(HandoffError, match="installation failed"):
        fixture_stage(monkeypatch, tmp_path, npm_exit=1)
    modules = tmp_path / "code" / "runtime" / direct.COLLECTOR / "node_modules"
    assert (modules / ".package-lock.json").is_file()


def test_changed_toolchain_refuses_before_swap(monkeypatch, tmp_path):
    plan, selected, runtime, current, git = fixture_stage(monkeypatch, tmp_path)
    old_inode = (current / "node_modules").stat().st_ino
    monkeypatch.setattr(direct, "dependency_versions", lambda path: {"node": "different"})
    with pytest.raises(HandoffError, match="toolchain changed"):
        direct.apply_current_dependencies(plan, selected, runtime, lambda: None, git)
    assert (current / "node_modules").stat().st_ino == old_inode


def test_dependency_owner_loss_after_second_rename_still_raises(monkeypatch, tmp_path):
    plan, selected, runtime, current, git = fixture_stage(monkeypatch, tmp_path)
    calls = []
    def owner():
        calls.append(None)
        if len(calls) == 4:
            raise RuntimeError("owner lost after swap")
    with pytest.raises(RuntimeError, match="owner lost after swap"):
        direct.apply_current_dependencies(plan, selected, runtime, owner, git)
    assert (current / "node_modules").stat().st_ino == plan["new_identity"][1]
    assert (Path(plan["stage"]) / "previous-node_modules").is_dir()
