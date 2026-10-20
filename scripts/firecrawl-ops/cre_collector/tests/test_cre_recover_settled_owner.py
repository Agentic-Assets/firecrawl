"""Exact dead-owner recovery tests; synthetic private paths, no live runtime."""
import hashlib
import importlib
import json
import os
import stat
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX flock protocol')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    recovery = importlib.import_module('cre_recover_settled_owner')
    root = tmp_path / 'daily'
    root.mkdir(mode=0o700)
    lock = root / '.cre.lock'
    lock.mkdir(mode=0o700)
    generation = 'operator-recovery-required:' + 'b' * 40
    authority = root / '.cre.lock.authority'
    authority.write_text('v1 99999999 ' + 'a' * 40 + ' ' + generation + ' recovery-required\n')
    (lock/'pid').write_text('99999999 123456\n')
    (lock/'lease').write_text(generation + '\n')
    for path in (authority, lock/'pid', lock/'lease'):
        path.chmod(0o600)
    monkeypatch.setattr(recovery.cp, 'canonical_shared_lock_dir', lambda: lock)
    proofs = tmp_path / 'proofs'
    proofs.mkdir(mode=0o700)
    evidence = {}
    for role in ('known_outcome', 'remote_settlement', 'descendant_settlement'):
        path = proofs / (role + '.json')
        path.write_text(json.dumps({'synthetic': role}))
        path.chmod(0o600)
        evidence[role] = {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    expected = recovery.inspect_state()
    options = {'evidence': evidence, 'operator': 'cayman', 'reference': 'AGENTIC-4607', 'confirmation': 'RECOVER CRE OWNER '+expected['fingerprint']}
    return recovery, lock, authority, expected, options


def recover(setup, **changes):
    subject, _lock, _authority, expected, options = setup
    return subject.recover(expected, **{**options, **changes})


def test_default_inspect_creates_no_sync_archive_or_namespace(setup):
    subject, lock, authority, expected, _ = setup
    before = {str(p): (p.stat().st_ino, p.read_bytes()) for p in (authority, lock/'pid', lock/'lease')}
    result = recover(setup)
    assert result['executed'] is False
    assert result['state'] == expected
    assert not subject.cp.quarantine_recovery_sync_path(lock).exists()
    assert not (lock.parent/'.cre-owner-recovery-forensics').exists()
    assert {str(p): (p.stat().st_ino, p.read_bytes()) for p in (authority, lock/'pid', lock/'lease')} == before


def test_exact_recovery_preserves_inodes_forensics_and_allows_normal_owner(setup):
    subject, lock, authority, expected, _ = setup
    inode = authority.stat().st_ino
    originals = {n: (lock/n).read_bytes() for n in ('pid','lease')}
    result = recover(setup, execute=True)
    assert result['executed'] is True
    assert result['status'] == 'KNOWN_SETTLED_OWNER_RECOVERED'
    assert not lock.exists()
    assert authority.stat().st_ino == inode
    assert authority.read_bytes() == b'v1 neutral\n'
    archive = Path(result['archive'])
    assert stat.S_IMODE(archive.stat().st_mode) == 0o700
    assert [archive.joinpath('lock').stat().st_dev, archive.joinpath('lock').stat().st_ino] == expected['directory_identity']
    for name, value in originals.items():
        assert (archive/'lock'/name).read_bytes() == value
    assert hashlib.sha256((archive/'authority-before').read_bytes()).hexdigest() == expected['authority']['sha256']
    completed = json.loads((archive/'completed.json').read_text())
    assert completed['evidence'] == result['evidence']
    assert completed['authority_identity'] == expected['authority']['identity']
    sync_inode = subject.cp.quarantine_recovery_sync_path(lock).stat().st_ino
    with subject.cp.SharedLock(lock):
        assert lock.exists()
    assert authority.stat().st_ino == inode
    assert subject.cp.quarantine_recovery_sync_path(lock).stat().st_ino == sync_inode


@pytest.mark.parametrize('which', ['authority','pid','lease','directory'])
def test_expected_identity_or_raw_bytes_drift_refuses(setup, which):
    subject, lock, authority, _expected, _ = setup
    if which == 'directory':
        moved = lock.with_name('foreign')
        lock.rename(moved)
        lock.mkdir(mode=0o700)
        for n in ('pid','lease'):
            (lock/n).write_bytes((moved/n).read_bytes())
            (lock/n).chmod(0o600)
    else:
        path = authority if which == 'authority' else lock/which
        path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises((ValueError, subject.cp.LockHeldError)):
        recover(setup, execute=True)
    assert authority.read_bytes() != b'v1 neutral\n'
    assert lock.exists()


@pytest.mark.parametrize('name', ['extra','capacity-benchmark-active.json','capacity-benchmark-quarantine.json'])
def test_unknown_or_interlocked_directory_refuses(setup, name):
    subject, lock, authority, _expected, _ = setup
    (lock/name).write_text('{}')
    with pytest.raises((ValueError, subject.cp.LockHeldError)):
        recover(setup, execute=True)
    assert lock.exists() and authority.read_bytes() != b'v1 neutral\n'


@pytest.mark.parametrize('kind', ['alive','symlink','hardlink','mode','malformed','guard'])
def test_unsafe_or_live_state_refuses(setup, monkeypatch, kind):
    subject, lock, authority, _expected, _ = setup
    if kind == 'alive':
        monkeypatch.setattr(subject.cp, '_pid_alive', lambda _pid: True)
    elif kind == 'symlink':
        path = lock/'pid'
        path.rename(lock/'old')
        path.symlink_to(lock/'old')
    elif kind == 'hardlink':
        os.link(authority, authority.with_name('linked'))
    elif kind == 'mode':
        authority.chmod(0o644)
    elif kind == 'malformed':
        authority.write_text('bad authority\n')
    else:
        (lock.parent/subject.cp.QUARANTINE_RECOVERY_GUARD).write_text('{}')
    with pytest.raises((ValueError, subject.cp.LockHeldError, OSError)):
        recover(setup, execute=True)
    assert lock.exists() and authority.read_bytes() != b'v1 neutral\n'


@pytest.mark.parametrize('change', ['operator','reference','confirmation','missing_evidence','changed_evidence'])
def test_explicit_operator_and_immutable_evidence_required(setup, change):
    subject, lock, authority, _expected, options = setup
    edits = {}
    if change in ('operator','reference','confirmation'):
        edits[change] = 'invalid'
    elif change == 'missing_evidence':
        edits['evidence'] = {}
    else:
        Path(options['evidence']['known_outcome']['path']).write_text('changed')
    with pytest.raises((ValueError, subject.cp.LockHeldError)):
        recover(setup, execute=True, **edits)
    assert lock.exists() and authority.read_bytes() != b'v1 neutral\n'


@pytest.mark.parametrize('held', ['authority','sync'])
def test_held_flock_refuses_without_namespace_changes(setup, held):
    subject, lock, authority, _expected, _ = setup
    if held == 'sync':
        fd = subject.cp.acquire_quarantine_recovery_sync(lock)
    else:
        fd = os.open(authority, os.O_RDWR)
        subject.fcntl.flock(fd, subject.fcntl.LOCK_EX | subject.fcntl.LOCK_NB)
    try:
        with pytest.raises((ValueError, subject.cp.LockHeldError, BlockingIOError)):
            recover(setup, execute=True)
    finally:
        os.close(fd)
    assert lock.exists() and authority.read_bytes() != b'v1 neutral\n'


def test_existing_forensic_prefix_refuses_no_automatic_resume(setup):
    _subject, lock, authority, expected, _ = setup
    root = lock.parent/'.cre-owner-recovery-forensics'
    root.mkdir(mode=0o700)
    (root/expected['fingerprint']).mkdir(mode=0o700)
    with pytest.raises((ValueError, FileExistsError)):
        recover(setup, execute=True)
    assert lock.exists() and authority.read_bytes() != b'v1 neutral\n'


@pytest.mark.parametrize('stage', ['prepared','rename','archive_fsync','source_fsync','neutral_before','neutral_after','complete'])
def test_fault_prefixes_never_lose_forensics_or_claim_unconditional_fence(setup, monkeypatch, stage):
    subject, lock, authority, _expected, _ = setup
    inode = authority.stat().st_ino
    write = subject._write_private
    rename = subject.q._rename_exact_to_empty_target
    sync = subject.q.fsync_directory
    neutral = subject._neutralize
    def bad_write(path, raw):
        if (stage == 'prepared' and path.name == 'prepared.json') or (stage == 'complete' and path.name == 'completed.json'):
            raise OSError('injected')
        return write(path, raw)
    def bad_rename(*args, **kwargs):
        result = rename(*args, **kwargs)
        if stage == 'rename':
            raise OSError('injected after rename')
        return result
    def bad_sync(path):
        if stage == 'archive_fsync' and Path(path).name == _expected['fingerprint'] and not lock.exists():
            raise OSError('injected after archive rename')
        if stage == 'source_fsync' and Path(path) == lock.parent and not lock.exists():
            raise OSError('injected original parent fsync after rename')
        return sync(path)
    def bad_neutral(fd, path, identity):
        assert not lock.exists()
        archives = list((lock.parent/'.cre-owner-recovery-forensics').glob('*/lock'))
        assert len(archives) == 1 and (archives[0].parent/'prepared.json').exists()
        if stage == 'neutral_before':
            raise OSError('injected before neutral')
        result = neutral(fd, path, identity)
        if stage == 'neutral_after':
            raise OSError('injected after durable neutral')
        return result
    monkeypatch.setattr(subject, '_write_private', bad_write)
    monkeypatch.setattr(subject.q, '_rename_exact_to_empty_target', bad_rename)
    monkeypatch.setattr(subject.q, 'fsync_directory', bad_sync)
    monkeypatch.setattr(subject, '_neutralize', bad_neutral)
    with pytest.raises((OSError, subject.RecoveryAmbiguous)):
        recover(setup, execute=True)
    assert authority.stat().st_ino == inode
    if stage in ('neutral_after','complete'):
        assert authority.read_bytes() == b'v1 neutral\n'
        assert not lock.exists()
        assert list((lock.parent/'.cre-owner-recovery-forensics').glob('*/lock/lease'))
        with subject.cp.SharedLock(lock):
            pass
    else:
        assert authority.read_bytes() != b'v1 neutral\n'
        with pytest.raises(subject.cp.LockHeldError), subject.cp.SharedLock(lock):
            pass


@pytest.mark.parametrize('fault', ['partial_write', 'fsync'])
def test_actual_inplace_neutral_syscall_fault_is_ambiguous_after_durable_archive(setup, monkeypatch, fault):
    subject, lock, authority, expected, _ = setup
    original_write, original_sync = os.write, os.fsync
    identity = expected['authority']['identity']
    def is_authority(fd):
        value = os.fstat(fd)
        return [value.st_dev, value.st_ino] == identity
    def fail_write(fd, raw):
        if is_authority(fd):
            original_write(fd, b'v1 ')
            raise OSError('partial neutral write')
        return original_write(fd, raw)
    def fail_sync(fd):
        if is_authority(fd):
            raise OSError('neutral fsync acknowledgement failed')
        return original_sync(fd)
    if fault == 'partial_write':
        monkeypatch.setattr(os, 'write', fail_write)
    else:
        monkeypatch.setattr(os, 'fsync', fail_sync)
    with pytest.raises(subject.RecoveryAmbiguous):
        recover(setup, execute=True)
    monkeypatch.setattr(os, 'write', original_write)
    monkeypatch.setattr(os, 'fsync', original_sync)
    assert not lock.exists()
    assert [authority.stat().st_dev, authority.stat().st_ino] == identity
    archive = lock.parent/'.cre-owner-recovery-forensics'/expected['fingerprint']
    assert (archive/'prepared.json').is_file() and (archive/'lock/lease').is_file()
    assert not (archive/'completed.json').exists()
    if fault == 'partial_write':
        assert authority.read_bytes() == b'v1 '
        with pytest.raises(subject.cp.LockHeldError), subject.cp.SharedLock(lock):
            pass
    else:
        assert authority.read_bytes() == b'v1 neutral\n'
        with subject.cp.SharedLock(lock):
            pass


def test_fifo_authority_refuses_without_blocking_or_touching_namespace(setup):
    _subject, lock, authority, _expected, _ = setup
    authority.unlink()
    os.mkfifo(authority, 0o600)
    with pytest.raises(ValueError, match='private owned singleton'):
        recover(setup, execute=True)
    assert lock.exists() and stat.S_ISFIFO(authority.lstat().st_mode)


def test_cli_default_outputs_direct_expected_state_without_creating_sync(setup, capsys):
    subject, lock, _authority, expected, _ = setup
    subject.main([])
    assert json.loads(capsys.readouterr().out) == expected
    assert not subject.cp.quarantine_recovery_sync_path(lock).exists()
