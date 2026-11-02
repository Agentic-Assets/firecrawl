"""Real Git ignore transitions and pre-lease maintenance admission."""

import os
import subprocess
from types import SimpleNamespace

import pytest
from test_cre_code_handoff import git_command

import cre_runtime_maintenance as maintenance
from cre_code_handoff_evidence import HandoffError
from cre_code_handoff_git import Git

PATHS = [
    "docs/firecrawl-ops/references/document-input-benchmark/2026-11-01/measurements.csv",
    "scripts/firecrawl-ops/cre_collector/data/zip_cbsa_crosswalk.csv",
]


@pytest.fixture
def ignored_transition(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    source = tmp_path / "source"
    source.mkdir()
    git_command(source, "init", "-b", "main")
    git_command(source, "config", "user.email", "fixture@example.invalid")
    git_command(source, "config", "user.name", "Fixture")
    (source / ".gitignore").write_text("*.csv\n")
    for path in PATHS:
        parent = (source / path).parent
        parent.mkdir(parents=True, exist_ok=True)
        (parent / "existing.txt").write_text("tracked\n")
    git_command(source, "add", ".")
    git_command(source, "commit", "-m", "old")
    git = Git("unused")
    old = {
        "sha": git.text(source, "rev-parse", "HEAD"),
        "tree": git.text(source, "rev-parse", "HEAD^{tree}"),
    }
    target = tmp_path / "target"
    subprocess.run(["git", "clone", "--no-hardlinks", str(source), str(target)], check=True, capture_output=True)
    (source / ".gitignore").write_text("*.csv\n" + "".join("!" + path + "\n" for path in PATHS))
    for path in PATHS:
        (source / path).write_text("reviewed,blob\n")
    git_command(source, "add", ".")
    git_command(source, "commit", "-m", "selected")
    new = {
        "sha": git.text(source, "rev-parse", "HEAD"),
        "tree": git.text(source, "rev-parse", "HEAD^{tree}"),
    }
    paths = git.command(source, "diff", "--no-renames", "--name-only", "-z", old["sha"], new["sha"])
    paths = paths.decode().rstrip("\0").split("\0")
    before, after = git.entries(source, old["sha"]), git.entries(source, new["sha"])
    release = {
        "deployed": old,
        "selected": new,
        "allowed_paths": paths,
        "objects": {path: {"before": before.get(path), "after": after.get(path)} for path in paths},
    }
    return git, source, target, release


def test_absent_reviewed_new_files_survive_old_ignore(ignored_transition):
    git, source, target, release = ignored_transition
    git.paths(target, release, object_repo=source, allow_ignored_absent=True)
    assert git.text(target, "rev-parse", "HEAD") == release["deployed"]["sha"]


def test_maintenance_fast_forward_adds_reviewed_files(ignored_transition, tmp_path):
    git, source, target, release = ignored_transition
    git_command(source, "update-ref", "refs/remotes/origin/main", release["selected"]["sha"])
    bundle = tmp_path / "selected.bundle"
    git_command(source, "bundle", "create", str(bundle), "refs/remotes/origin/main")
    git.maintenance_fast_forward(target, release, bundle, allowed_dependency_paths=[])
    assert all((target / path).read_text() == "reviewed,blob\n" for path in PATHS)
    assert git.text(target, "rev-parse", "HEAD") == release["selected"]["sha"]


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_ignored_existing_private_data_refused(ignored_transition, tmp_path, kind):
    git, source, target, release = ignored_transition
    leaf = target / PATHS[0]
    if kind == "file":
        leaf.write_text("private")
    else:
        leaf.symlink_to(tmp_path / "missing")
    with pytest.raises(HandoffError):
        git.paths(target, release, object_repo=source, allow_ignored_absent=True)
    assert git.text(target, "rev-parse", "HEAD") == release["deployed"]["sha"]


def test_readiness_checks_both_roots_before_lock():
    calls = []

    class FakeGit:
        def clean(self, *args, **kwargs):
            pass

        def maintenance_ancestry(self, *args):
            pass

        def paths(self, root, scope, **kwargs):
            calls.append((root, scope, kwargs))
            if root == "/runtime":
                raise HandoffError("collision")

    packet = {
        "roots": {"development": "/dev", "native": "/native", "runtime": "/runtime"},
        "old": {"sha": "old"},
        "native_before": {"sha": "native"},
        "new": {"sha": "new"},
        "development": {"sha": "dev"},
        "native_change": {"changed_dependency_paths": []},
        "runtime_change": {"changed_dependency_paths": ["package.json"]},
    }
    checkpoint = SimpleNamespace(canonical_shared_lock_dir=lambda _: pytest.fail("lock reached before path preflight"))
    with pytest.raises(HandoffError, match="collision"):
        maintenance.maintenance_readiness(packet, (checkpoint, None, None, None), FakeGit())
    assert [call[0] for call in calls] == ["/native", "/runtime"]
    assert calls[1][2]["allowed_dependency_paths"] == ["package.json"]
    assert calls[1][2]["object_repo"] == "/dev"