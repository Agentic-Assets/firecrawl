"""Regression coverage for the session-wide real-canonical-lock guard.

See LOCK_AUTHORITY_RECOVERY.md and the 2026-09-15 incident: a benchmark
rollback test resolved the real production `canonical_shared_lock_dir()`
default and left quarantine markers inside the real out/daily/.cre.lock
directory, blocking live collector runs. `tests/conftest.py` now installs a
session-wide autouse guard (`_never_touch_real_canonical_cre_lock`) that fails
loudly the instant any code under test would resolve that real path, plus a
before/after on-disk snapshot backstop. These tests prove the preventive half
of that guard is actually wired up and is surgical (it must not break
legitimate unit tests of the resolver itself against a tmp_path checkout).
"""

from __future__ import annotations

from pathlib import Path

import conftest as _conftest
import pytest

import cre_capacity_benchmark as benchmark
import cre_checkpoint_refresh as refresh


def test_default_lock_resolution_under_pytest_never_yields_the_real_path() -> None:
    """Calling the production resolver with the real repo root must fail
    loudly under pytest rather than silently returning the real canonical
    lock path."""
    with pytest.raises(pytest.fail.Exception):
        refresh.canonical_shared_lock_dir(Path(_conftest._PARENT))


def test_downstream_module_bindings_are_guarded_too() -> None:
    """Every module that imported `canonical_shared_lock_dir` directly (not
    just cre_checkpoint_refresh itself) must be covered, since a
    `from cre_checkpoint_refresh import canonical_shared_lock_dir` binds an
    independent name in the importing module's namespace."""
    with pytest.raises(pytest.fail.Exception):
        benchmark.canonical_shared_lock_dir(Path(_conftest._PARENT))


def test_guard_is_surgical_and_does_not_break_real_resolver_unit_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legitimate unit test of the resolver against an unrelated tmp_path
    git checkout (not the real repo) must still work normally -- the guard
    only fails on the specific real production path, not on every call."""
    primary = tmp_path / "primary"
    common_git = primary / ".git"

    class Proc:
        stdout = str(common_git) + "\n"

    monkeypatch.setattr(refresh.subprocess, "run", lambda *_args, **_kwargs: Proc())
    result = refresh.canonical_shared_lock_dir(tmp_path / "worktree")
    assert result == (
        primary
        / "scripts"
        / "firecrawl-ops"
        / "cre_collector"
        / "out"
        / "daily"
        / ".cre.lock"
    )
    assert result != _conftest._REAL_CANONICAL_LOCK_DIR


def test_real_lock_pair_snapshot_helper_is_read_only(tmp_path: Path) -> None:
    """`_lock_snapshot` must never create, modify, or delete anything -- it is
    the read side of the before/after backstop assertion."""
    missing = tmp_path / "does-not-exist" / ".cre.lock"
    assert _conftest._lock_snapshot(missing) is None
    assert not missing.parent.exists()

    real_dir = tmp_path / "real" / ".cre.lock"
    real_dir.mkdir(parents=True)
    before = _conftest._lock_snapshot(real_dir)
    again = _conftest._lock_snapshot(real_dir)
    assert before == again
    assert real_dir.is_dir()
    # A directory snapshot must change once an entry is added inside it, the
    # exact shape of the original leak (quarantine marker files written
    # inside the real lock directory).
    (real_dir / "capacity-benchmark-active.json").write_text("{}", encoding="utf-8")
    after = _conftest._lock_snapshot(real_dir)
    assert after != before
