"""Real temporary SharedLock release contract; never resolve a runtime lock."""

import os
from pathlib import Path

import pytest

import cre_checkpoint_refresh as checkpoint
import cre_code_handoff as handoff
from cre_posix import fcntl


@pytest.fixture(autouse=True)
def isolated_resource_trip_state():
    """Earlier fault tests can leave a synthetic sticky cleanup-unknown trip."""
    checkpoint._clear_cpu_guard_trip()
    try:
        yield
    finally:
        checkpoint._clear_cpu_guard_trip()


@pytest.fixture
def cleared(tmp_path):
    path = tmp_path / "fixture.lock"
    with checkpoint.SharedLock(path, recovery_required=True) as lease:
        with pytest.raises(handoff.HandoffError, match="owned and normal"):
            handoff.released_authority(checkpoint, lease)
        lease.clear_recovery_requirement()
        receipt = handoff.released_authority(checkpoint, lease)
        assert path.is_dir()
        assert lease.authority_locked
    assert not path.exists()
    assert Path("/proc", str(os.getpid())).exists()
    assert lease.authority_fd == -1
    return path, receipt


def test_unknown_cleanup_during_real_lease_retains_recovery(tmp_path):
    path = tmp_path / "uncertain-fixture.lock"
    with checkpoint.SharedLock(path, recovery_required=True) as lease:
        lease.clear_recovery_requirement()
        receipt = handoff.released_authority(checkpoint, lease)
        checkpoint._set_cpu_guard_trip(
            "synthetic owned cleanup remains unknown",
            evidence_valid=False,
            owned_processes_reaped=False,
        )
    assert path.is_dir()
    assert lease.retain_on_exit
    descriptor = os.open(lease.authority_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        assert checkpoint.SharedLock._authority_fields(descriptor)[3] is True
    finally:
        os.close(descriptor)
    with pytest.raises(handoff.HandoffError, match="directory present"):
        handoff.lock_clear(checkpoint, path, released=receipt)


def test_real_release_preserves_normal_record_and_accepts_only_exact_own_receipt(cleared):
    path, receipt = cleared
    authority = path.with_name(path.name + ".authority")
    original = authority.read_bytes()
    descriptor = os.open(authority, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        assert checkpoint.SharedLock._authority_fields(descriptor) == receipt[0]
    finally:
        os.close(descriptor)
    assert receipt[0][0] == os.getpid() and receipt[0][3] is False
    with pytest.raises(handoff.HandoffError, match="not clear"):
        handoff.lock_clear(checkpoint, path)
    handoff.lock_clear(checkpoint, path, released=receipt)
    assert authority.read_bytes() == original
    assert not path.exists()


@pytest.mark.parametrize("mutation", ["pid", "token", "generation", "inode"])
def test_released_receipt_must_match_every_owned_identity_field(cleared, mutation):
    path, (fields, identity) = cleared
    if mutation == "inode":
        receipt = fields, (identity[0], identity[1] + 1)
    else:
        changed = list(fields)
        index = {"pid": 0, "token": 1, "generation": 2}[mutation]
        changed[index] = os.getppid() if index == 0 else "x" * 43
        receipt = tuple(changed), identity
    with pytest.raises(handoff.HandoffError, match="identity differs"):
        handoff.lock_clear(checkpoint, path, released=receipt)


@pytest.mark.parametrize("mutation", ["recovery", "foreign-live", "malformed", "replacement", "retained-directory"])
def test_released_authority_rejects_changed_or_incomplete_release(cleared, mutation):
    path, receipt = cleared
    authority = path.with_name(path.name + ".authority")
    fields, _ = receipt
    if mutation == "retained-directory":
        path.mkdir()
    elif mutation == "replacement":
        authority.rename(authority.with_suffix(".original"))
        authority.write_text(f"v1 {fields[0]} {fields[1]} {fields[2]} normal\n")
        authority.chmod(0o600)
    elif mutation == "malformed":
        authority.write_text("invalid authority\n")
    elif mutation == "recovery":
        authority.write_text(f"v1 {fields[0]} {fields[1]} {fields[2]} recovery-required\n")
    else:
        assert os.getppid() != os.getpid() and Path("/proc", str(os.getppid())).exists()
        authority.write_text(f"v1 {os.getppid()} {fields[1]} {fields[2]} normal\n")
        with pytest.raises(handoff.HandoffError, match="not clear"):
            handoff.lock_clear(checkpoint, path)
    with pytest.raises((handoff.HandoffError, checkpoint.LockHeldError)):
        handoff.lock_clear(checkpoint, path, released=receipt)


def test_released_authority_requires_unheld_flock(cleared):
    path, receipt = cleared
    descriptor = os.open(path.with_name(path.name + ".authority"), os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(handoff.HandoffError, match="authority is held"):
            handoff.lock_clear(checkpoint, path, released=receipt)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_released_authority_rejects_path_replacement_during_clearance(cleared, monkeypatch):
    path, receipt = cleared
    authority = path.with_name(path.name + ".authority")
    original_reader = checkpoint.SharedLock._authority_fields
    replaced = False

    def replacing(descriptor):
        nonlocal replaced
        result = original_reader(descriptor)
        if not replaced:
            replaced = True
            raw = authority.read_bytes()
            authority.rename(authority.with_suffix(".original"))
            authority.write_bytes(raw)
            authority.chmod(0o600)
        return result

    monkeypatch.setattr(checkpoint.SharedLock, "_authority_fields", staticmethod(replacing))
    with pytest.raises(handoff.HandoffError, match="changed during clearance"):
        handoff.lock_clear(checkpoint, path, released=receipt)
    assert replaced
