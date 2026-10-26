"""Regression coverage for the pid-attribution refinement in the real-lock
snapshot guard (`tests/conftest.py::_never_touch_real_canonical_cre_lock`).

See LOCK_AUTHORITY_RECOVERY.md. The guard fails a test whenever the real
`out/daily/.cre.lock` pair changes during that test, but a legitimate
non-pytest owner (e.g. a launchd tier-dispatch run) touching the real lock at
the same time is not a leak from the test suite. These tests exercise the
pid-attribution helpers directly against tmp_path-based authority files, and
never the real repo-root lock path.
"""

from __future__ import annotations

import os

import conftest as _conftest


def _write_authority(path, pid: int, state: str = "normal") -> None:
    # token/generation must satisfy SharedLock._authority_fields'
    # [A-Za-z0-9_-]{32,128} shape or the whole record is rejected as
    # malformed.
    token = "t" * 32
    generation = "g" * 32
    path.write_text(f"v1 {pid} {token} {generation} {state}\n", encoding="utf-8")


def test_authority_owner_pid_reads_the_real_5_field_format(tmp_path) -> None:
    authority = tmp_path / ".cre.lock.authority"
    _write_authority(authority, pid=4242)
    assert _conftest._authority_owner_pid(authority) == 4242


def test_authority_owner_pid_returns_none_when_missing(tmp_path) -> None:
    assert _conftest._authority_owner_pid(tmp_path / "absent.authority") is None


def test_authority_owner_pid_returns_none_when_malformed(tmp_path) -> None:
    authority = tmp_path / ".cre.lock.authority"
    authority.write_text("not an authority record\n", encoding="utf-8")
    assert _conftest._authority_owner_pid(authority) is None


def test_process_is_pytest_or_descendant_true_for_self() -> None:
    assert _conftest._process_is_pytest_or_descendant(os.getpid()) is True


def test_lock_change_verdict_fails_closed_when_unattributable(tmp_path) -> None:
    attributable, owner_pid = _conftest._lock_change_verdict(tmp_path / "absent")
    assert attributable is True
    assert owner_pid is None


def test_lock_change_verdict_attributes_to_pytest_for_own_pid(tmp_path) -> None:
    authority = tmp_path / ".cre.lock.authority"
    _write_authority(authority, pid=os.getpid())
    attributable, owner_pid = _conftest._lock_change_verdict(authority)
    assert attributable is True
    assert owner_pid == os.getpid()


def test_lock_change_verdict_warns_instead_of_failing_for_external_owner(
    tmp_path, monkeypatch
) -> None:
    """A positively-identified external pid (e.g. a launchd tier-dispatch
    run) must flip the verdict to "warn, don't fail" -- exercised by
    monkeypatching the process-tree check itself so this test never depends
    on any real external process actually running."""
    authority = tmp_path / ".cre.lock.authority"
    _write_authority(authority, pid=999999)
    monkeypatch.setattr(
        _conftest, "_process_is_pytest_or_descendant", lambda pid: False
    )
    attributable, owner_pid = _conftest._lock_change_verdict(authority)
    assert attributable is False
    assert owner_pid == 999999


def test_admitted_clone_lock_matches_the_production_resolver() -> None:
    """Admission's local lock formula must match the actual Git resolver."""
    from pathlib import Path

    collector = Path(_conftest._PARENT)
    assert _conftest.admitted_lock_path(collector) == _conftest._REAL_CANONICAL_LOCK_DIR
    assert (
        _conftest._REAL_CANONICAL_SHARED_LOCK_DIR().resolve()
        == _conftest._REAL_CANONICAL_LOCK_DIR
    )
