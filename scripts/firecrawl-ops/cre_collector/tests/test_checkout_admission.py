"""Synthetic layout/startup probes only; never run collector code in a worktree."""

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import _checkout_admission as admission
import pytest

SOURCE = Path(__file__).resolve().parent.parent
RELATIVE_COLLECTOR = Path("scripts/firecrawl-ops/cre_collector")


def _git(root, *args):
    return subprocess.run(
        # Fixture commits must settle before the recursive read-only witness.
        # Otherwise Git's automatic background maintenance can change .git
        # independently of the admission operation under test.
        ["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-C", str(root), *args],
        check=True, capture_output=True,
        text=True, timeout=10,
    )


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "ordinary clone with spaces"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "commit", "--allow-empty", "-qm", "fixture")
    collector = root / RELATIVE_COLLECTOR
    collector.mkdir(parents=True)
    return root, collector


def _seed_lock(collector):
    lock = collector / "out/daily/.cre.lock"
    lock.mkdir(parents=True)
    (lock / "capacity-benchmark-quarantine.json").write_text('{"fixture": true}\n')
    (lock / "nested").mkdir()
    (lock / "nested/marker").write_bytes(b"nested forensic bytes")
    lock.with_name(".cre.lock.authority").write_bytes(b"fixture authority: preserve\n")


def _witness(root):
    """Recursive entry identities and bytes, solely for owned fixture evidence."""
    if not root.exists():
        return None
    result = {}
    for path in (root, *sorted(root.rglob("*"))):
        observed = path.lstat()
        digest = None
        if stat.S_ISREG(observed.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(observed.st_mode):
            digest = os.readlink(path)
        result[str(path.relative_to(root))] = (
            observed.st_dev, observed.st_ino, observed.st_mode,
            observed.st_size, observed.st_mtime_ns, digest,
        )
    return result


def test_ordinary_checkout_is_source_anchored_and_read_only(checkout, monkeypatch, tmp_path):
    root, collector = checkout
    monkeypatch.chdir(tmp_path)
    before = _witness(root)
    assert admission.admitted_lock_path(collector) == collector / "out/daily/.cre.lock"
    assert _witness(root) == before
    assert not (collector / "out").exists()
    alias = tmp_path / "whole checkout alias"
    alias.symlink_to(root, target_is_directory=True)
    assert admission.admitted_lock_path(alias / RELATIVE_COLLECTOR) == collector / "out/daily/.cre.lock"


def test_existing_fixture_lock_is_never_opened_by_admission(checkout, monkeypatch):
    root, collector = checkout
    _seed_lock(collector)
    before = _witness(root)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", lambda _path: pytest.fail("admission read file contents"))
        assert admission.admitted_lock_path(collector) == collector / "out/daily/.cre.lock"
    assert _witness(root) == before


def test_linked_worktree_refuses_without_changing_either_fixture_lock(checkout, tmp_path):
    root, collector = checkout
    _seed_lock(collector)
    worktree = tmp_path / "linked worktree with spaces"
    _git(root, "worktree", "add", "--detach", str(worktree))
    linked_collector = worktree / RELATIVE_COLLECTOR
    linked_collector.mkdir(parents=True)
    before = _witness(root), _witness(worktree)
    with pytest.raises(admission.CheckoutAdmissionError, match=".git"):
        admission.admitted_lock_path(linked_collector)
    assert (_witness(root), _witness(worktree)) == before
    assert not (linked_collector / "out").exists()


@pytest.mark.parametrize("redirect", [
    "scripts", "scripts/firecrawl-ops", str(RELATIVE_COLLECTOR), ".git",
    f"{RELATIVE_COLLECTOR}/out", f"{RELATIVE_COLLECTOR}/out/daily",
    f"{RELATIVE_COLLECTOR}/out/daily/.cre.lock",
    f"{RELATIVE_COLLECTOR}/out/daily/.cre.lock.authority",
])
def test_interior_symlink_redirects_refuse_untouched(checkout, tmp_path, redirect):
    root, collector = checkout
    _seed_lock(collector)
    path = root / redirect
    external = tmp_path / "owned redirected target"
    path.rename(external)
    path.symlink_to(external, target_is_directory=external.is_dir())
    before = _witness(root), _witness(external)
    with pytest.raises(admission.CheckoutAdmissionError):
        admission.admitted_lock_path(collector)
    assert (_witness(root), _witness(external)) == before


def test_separate_git_directory_refuses(tmp_path):
    root, metadata = tmp_path / "separate checkout", tmp_path / "separate metadata"
    root.mkdir()
    _git(root, "init", "-q", f"--separate-git-dir={metadata}")
    collector = root / RELATIVE_COLLECTOR
    collector.mkdir(parents=True)
    before = _witness(root), _witness(metadata)
    with pytest.raises(admission.CheckoutAdmissionError, match="local directory"):
        admission.admitted_lock_path(collector)
    assert (_witness(root), _witness(metadata)) == before


def test_submodule_style_gitfile_refuses(checkout):
    root, collector = checkout
    metadata = root / ".git"
    moved = root / "module metadata"
    metadata.rename(moved)
    metadata.write_text(f"gitdir: {moved}\n")
    before = _witness(root)
    with pytest.raises(admission.CheckoutAdmissionError, match="gitfile"):
        admission.admitted_lock_path(collector)
    assert _witness(root) == before


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"])
def test_git_environment_redirection_refuses(checkout, tmp_path, monkeypatch, variable):
    root, collector = checkout
    other = tmp_path / "other checkout"
    other.mkdir()
    _git(other, "init", "-q")
    target = other if variable == "GIT_WORK_TREE" else other / ".git"
    monkeypatch.setenv(variable, str(target))
    before = _witness(root), _witness(other)
    with pytest.raises(admission.CheckoutAdmissionError):
        admission.admitted_lock_path(collector)
    assert (_witness(root), _witness(other)) == before


def test_core_worktree_redirection_refuses(checkout, tmp_path):
    root, collector = checkout
    other = tmp_path / "other worktree"
    other.mkdir()
    _git(root, "config", "core.worktree", str(other))
    before = _witness(root), _witness(other)
    with pytest.raises(admission.CheckoutAdmissionError, match="does not belong"):
        admission.admitted_lock_path(collector)
    assert (_witness(root), _witness(other)) == before


def _nested_repositories(tmp_path):
    ancestor = tmp_path / "owned ancestor"
    ancestor.mkdir()
    _git(ancestor, "init", "-q")
    root = ancestor / "one/two/qa"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    collector = root / RELATIVE_COLLECTOR
    collector.mkdir(parents=True)
    _seed_lock(collector)
    _seed_lock(ancestor / RELATIVE_COLLECTOR)
    return ancestor, root, collector


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_COMMON_DIR"])
def test_relative_git_dir_refuses_ancestor_metadata_without_lock_changes(tmp_path, monkeypatch, variable):
    ancestor, root, collector = _nested_repositories(tmp_path)
    monkeypatch.setenv(variable, "../../../.git")
    monkeypatch.setenv("GIT_WORK_TREE", str(root))
    before = _witness(ancestor)
    metadata = {}
    for cwd in (collector, root):
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir", "--git-common-dir"],
            cwd=cwd, capture_output=True, text=True, check=True, timeout=5,
        )
        metadata[cwd] = [Path(record).resolve() for record in result.stdout.splitlines()]
    # Discovery anchors relative GIT_COMMON_DIR to the repository context;
    # explicit relative GIT_DIR retains the caller-cwd divergence from F1.
    collector_common = root / ".git" if variable == "GIT_DIR" else ancestor / ".git"
    assert metadata[collector] == [root, root / ".git", collector_common]
    expected_git_dir = ancestor / ".git" if variable == "GIT_DIR" else root / ".git"
    assert metadata[root] == [root, expected_git_dir, ancestor / ".git"]
    print(f"RELATIVE_GIT_METADATA: collector={metadata[collector]}, root={metadata[root]}")
    try:
        with pytest.raises(admission.CheckoutAdmissionError, match="does not belong"):
            admission.admitted_lock_path(collector)
    finally:
        assert _witness(ancestor) == before


def test_relative_worktree_uses_production_root_context(checkout, monkeypatch):
    root, collector = checkout
    monkeypatch.setenv("GIT_WORK_TREE", ".")
    before = _witness(root)
    assert admission.admitted_lock_path(collector) == collector / "out/daily/.cre.lock"
    assert _witness(root) == before


@pytest.mark.parametrize("failure", [
    FileNotFoundError("git absent"),
    subprocess.CalledProcessError(1, ["git"], stderr="private config"),
    subprocess.TimeoutExpired(["git"], 5),
])
def test_git_failures_refuse_with_a_bounded_probe(checkout, monkeypatch, failure):
    root, collector = checkout
    before = _witness(root)

    def fail(command, **kwargs):
        assert command[0] == "git" and kwargs["cwd"] == root
        assert 0 < kwargs["timeout"] <= 5 and kwargs["check"]
        assert "env" not in kwargs  # Same environment as the production resolver.
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(admission.subprocess, "run", fail)
        with pytest.raises(admission.CheckoutAdmissionError) as refused:
            admission.admitted_lock_path(collector)
        assert "private config" not in str(refused.value)
    assert _witness(root) == before


@pytest.mark.parametrize("metadata", ["", "\n", "relative\nrelative\nrelative\n", "/a\n/b\n", "/a\n/b\n/c\n/d\n"])
def test_malformed_git_output_refuses(checkout, monkeypatch, metadata):
    _, collector = checkout
    monkeypatch.setattr(admission.subprocess, "run", lambda *_a, **_kw: subprocess.CompletedProcess([], 0, metadata))
    with pytest.raises(admission.CheckoutAdmissionError, match="ambiguous"):
        admission.admitted_lock_path(collector)


def test_wrong_source_layout_refuses_before_git(checkout, monkeypatch):
    root, _ = checkout
    monkeypatch.setattr(admission.subprocess, "run", lambda *_a, **_kw: pytest.fail("unexpected Git probe"))
    with pytest.raises(admission.CheckoutAdmissionError, match="source layout"):
        admission.admitted_lock_path(root)


def _synthetic_startup(collector, event, *, trap, unsupported=False, git_missing=False, git_env=None):
    """Copy startup bytes; no actual runtime modules can enter this interpreter."""
    tests = collector / "tests"
    tests.mkdir(exist_ok=True)
    for name in ("conftest.py", "_checkout_admission.py"):
        shutil.copyfile(SOURCE / "tests" / name, tests / name)
    shutil.copyfile(SOURCE / "cre_platform.py", collector / "cre_platform.py")
    record = (
        "from pathlib import Path\n"
        f"events = Path({str(event)!r})\n"
        "def record(message):\n"
        "    with events.open('a') as output: output.write(message + '\\n')\n"
    )
    for name in ("cre_checkpoint_refresh", "cre_checkpoint_series", "cre_memory_guard"):
        stub = record + f"record({name!r})\n"
        if trap:
            stub += "raise RuntimeError('RUNTIME_IMPORT_TRAP')\n"
        elif name == "cre_checkpoint_refresh":
            stub += (
                "def canonical_shared_lock_dir(*args):\n"
                "    record('RESOLVER_TRAP')\n"
                "    raise RuntimeError('RESOLVER_TRAP')\n"
            )
        (collector / f"{name}.py").write_text(stub)
    (tests / "test_probe.py").write_text(record + "record('COLLECTION')\ndef test_probe(): pass\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PYTEST", "GIT_"))}
    env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1")
    # Explicit, owned fixture values only; never inherit the source Git context.
    env.update(git_env or {})
    if git_missing or unsupported:
        env["PATH"] = ""
    nested = collector / "nested invocation"
    nested.mkdir()
    command = [sys.executable, "-B", "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", str(tests)]
    if unsupported:
        command = [sys.executable, "-B", "-c", (
            "import pytest, runpy, sys\n"
            "sys.platform = 'win32'\n"
            f"namespace = runpy.run_path({str(tests / 'conftest.py')!r})\n"
            "try:\n"
            "    namespace['pytest_configure'](None)\n"
            "except pytest.exit.Exception as exc:\n"
            "    print(exc)\n"
            "    raise SystemExit(exc.returncode)\n"
            "raise RuntimeError('unsupported platform did not exit')\n"
        )]
    return subprocess.run(
        command, cwd=nested, env=env, capture_output=True, text=True, timeout=15, check=False,
    )


def test_worktree_startup_refuses_before_runtime_import_and_collection(checkout, tmp_path):
    root, collector = checkout
    _seed_lock(collector)
    linked = tmp_path / "synthetic linked worktree"
    _git(root, "worktree", "add", "--detach", str(linked))
    linked_collector = linked / RELATIVE_COLLECTOR
    linked_collector.mkdir(parents=True)
    event = tmp_path / "startup events"
    before = _witness(collector / "out")
    result = _synthetic_startup(linked_collector, event, trap=True)
    output = result.stdout + result.stderr
    assert result.returncode == 4, output
    assert "git clone --no-hardlinks" in output and "checkout refused" in output
    assert "RUNTIME_IMPORT_TRAP" not in output and not event.exists()
    assert _witness(collector / "out") == before
    assert not (linked_collector / "out").exists()


def test_ordinary_startup_control_collects_using_only_stubs(checkout, tmp_path):
    _, collector = checkout
    event = tmp_path / "startup events"
    result = _synthetic_startup(collector, event, trap=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 test collected" in result.stdout
    assert event.read_text().splitlines() == [
        "cre_checkpoint_refresh", "cre_checkpoint_series", "cre_memory_guard", "COLLECTION",
    ]
    assert not (collector / "out").exists()


@pytest.mark.parametrize("variable", ["GIT_DIR", "GIT_COMMON_DIR"])
def test_relative_git_startup_refuses_before_runtime_import_and_collection(tmp_path, variable):
    ancestor, root, collector = _nested_repositories(tmp_path)
    event = tmp_path / "relative Git startup events"
    before = _witness(ancestor / RELATIVE_COLLECTOR / "out"), _witness(collector / "out")
    result = _synthetic_startup(
        collector, event, trap=True,
        git_env={variable: "../../../.git", "GIT_WORK_TREE": str(root)},
    )
    output = result.stdout + result.stderr
    print(f"RELATIVE_GIT_STARTUP_EXIT={result.returncode}\n{output}")
    assert (
        _witness(ancestor / RELATIVE_COLLECTOR / "out"), _witness(collector / "out")
    ) == before
    assert result.returncode == 4, output
    assert "git clone --no-hardlinks" in output and "checkout refused" in output
    assert not event.exists() and "RUNTIME_IMPORT_TRAP" not in output


def test_missing_git_startup_refuses_before_runtime_import(checkout, tmp_path):
    _, collector = checkout
    event = tmp_path / "startup events"
    result = _synthetic_startup(collector, event, trap=True, git_missing=True)
    assert result.returncode == 4, result.stdout + result.stderr
    assert "git clone --no-hardlinks" in result.stderr and "FileNotFoundError" in result.stderr
    assert not event.exists() and not (collector / "out").exists()


def test_unsupported_platform_preserves_exit_without_git_or_runtime(checkout, tmp_path):
    _, collector = checkout
    event = tmp_path / "startup events"
    result = _synthetic_startup(collector, event, trap=True, unsupported=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Native Windows is unsupported" in result.stdout
    assert not event.exists() and not (collector / "out").exists()
