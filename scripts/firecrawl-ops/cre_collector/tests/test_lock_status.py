"""Read actual authority formats without acquiring or mutating the lock."""

import os

import pytest

from scheduling.lock_status import SharedLock, observe


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("v1 neutral\n", "neutral"),
        (f"v1 {os.getpid()} {'a' * 43} {'b' * 43} recovery-required\n", "armed-visible"),
        (f"v1 99999999 {'a' * 43} {'b' * 43} recovery-required\n", "armed-not-visible"),
        (f"v1 99999999 {'a' * 43} {'b' * 43} normal\n", "normal-not-visible"),
        ("v1 reclaiming neutral - - - 1 2 0\n", "reclaiming"),
        ("", "unsafe"),
        ("malformed\n", "unsafe"),
        ("x" * 512, "unsafe"),
    ],
)
def test_authority_snapshot_does_not_mutate_files(tmp_path, payload, expected):
    path = tmp_path / ".cre.lock.authority"
    path.write_text(payload)
    before = path.stat()
    assert observe(path) == expected
    after = path.stat()
    assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    )
    assert path.read_text() == payload
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_absent_authority_creates_nothing(tmp_path):
    assert observe(tmp_path / ".cre.lock.authority") == "absent"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_unsafe_authority_paths_are_not_read_or_changed(tmp_path, kind):
    path = tmp_path / ".cre.lock.authority"
    target = tmp_path / "target"
    target.write_text("v1 neutral\n")
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        path.mkdir()
    before = path.lstat()
    assert observe(path) == "unsafe"
    assert path.lstat().st_ino == before.st_ino
    assert target.read_text() == "v1 neutral\n"


def test_changed_read_is_unknown_without_mutating_authority(tmp_path, monkeypatch):
    path = tmp_path / ".cre.lock.authority"
    path.write_text("v1 neutral\n")
    reads = iter([b"v1 neutral\n", b"v1 changed\n"])
    monkeypatch.setattr(SharedLock, "_safe_authority_bytes", lambda _fd: next(reads))
    assert observe(path) == "unsafe"
    assert path.read_text() == "v1 neutral\n"


def test_unobservable_pid_is_not_reported_as_dead_or_live(tmp_path, monkeypatch):
    path = tmp_path / ".cre.lock.authority"
    path.write_text(f"v1 {os.getpid()} {'a' * 43} {'b' * 43} recovery-required\n")
    def denied(_pid, _signal):
        raise PermissionError("not observable")
    monkeypatch.setattr(os, "kill", denied)
    assert observe(path) == "armed-unknown"


def test_fifo_authority_is_not_read_or_changed(tmp_path):
    path = tmp_path / ".cre.lock.authority"
    os.mkfifo(path)
    assert observe(path) == "unsafe"
    assert path.is_fifo()


def test_unreadable_authority_is_unknown(tmp_path, monkeypatch):
    path = tmp_path / ".cre.lock.authority"
    path.write_text("v1 neutral\n")
    def denied(*_args, **_kwargs):
        raise PermissionError("unreadable")
    monkeypatch.setattr(os, "open", denied)
    assert observe(path) == "unsafe"
    assert path.read_text() == "v1 neutral\n"


def test_replaced_authority_path_is_unknown(tmp_path, monkeypatch):
    path = tmp_path / ".cre.lock.authority"
    path.write_text("v1 neutral\n")
    replacement = tmp_path / "replacement"
    replacement.write_text("v1 neutral\n")
    original_lstat = os.lstat
    calls = iter([original_lstat(path), original_lstat(replacement)])
    monkeypatch.setattr(os, "lstat", lambda _path: next(calls))
    assert observe(path) == "unsafe"
