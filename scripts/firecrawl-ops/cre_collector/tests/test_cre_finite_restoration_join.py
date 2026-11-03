"""Finite restoration with a synthetic native Scheduler boundary."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cre_code_handoff import NEW, NOW, attestation, encoded, host_observation
from test_cre_code_handoff_restoration import (
    complete_case,  # noqa: F401
    held_observation,
    lineage_case,  # noqa: F401
    restoration_case,  # noqa: F401
    zero_missed_packet,
)
from test_cre_partial_full_join import admitted  # noqa: F401

import cre_code_handoff as operation
from cre_code_handoff_evidence import HandoffError, json_bytes, sha


@pytest.mark.parametrize("tamper_review", [False, True])
def test_finite_restoration_observes_authorizes_and_restores(restoration_case, tmp_path, monkeypatch, tamper_review):  # noqa: F811
    packet, _evidence, inputs = zero_missed_packet(restoration_case)
    packet_path = tmp_path / 'packet.json'
    packet_raw = encoded(packet)
    packet_path.write_bytes(packet_raw)
    output, obsdir, auth = tmp_path / 'operation', tmp_path / 'observation', tmp_path / 'auth.json'
    body, review = tmp_path / 'review.md', tmp_path / 'review.json'
    body.write_bytes(b'synthetic independently reviewed finite restoration')
    review.write_bytes(encoded(attestation({'restoration_sha256': sha(packet_raw), 'tooling': NEW},
                                          body, body.read_bytes(), 'restore-held-task-states')))
    state = {'calls': [], 'owned': False, 'cleared': False, 'enabled': [False] * 3}
    lock = tmp_path / 'lock'
    released = object()

    class Lease:
        def __init__(self, path, *, recovery_required):
            assert path == lock and recovery_required is True
        def __enter__(self):
            state['calls'].append('lease'); state['owned'] = True
            return self
        def clear_recovery_requirement(self):
            assert state['owned'] and (output / 'restoration-ready.json').is_file()
            state['calls'].append('clear'); state['cleared'] = True
        def __exit__(self, *_args):
            state['calls'].append('exit'); state['owned'] = False

    checkpoint = SimpleNamespace(SharedLock=Lease, canonical_shared_lock_dir=lambda _root: lock)
    class Git:
        def __init__(self, _tool): pass
        def clean(self, *_args, **_kwargs): pass
        def command(self, *_args, **_kwargs): return b''
    actual_read = operation.read_file
    monkeypatch.setattr(operation, 'read_file', lambda path: inputs.data[str(path)] if str(path) in inputs.data else actual_read(path))
    monkeypatch.setattr(operation, 'now', lambda: NOW)
    monkeypatch.setattr(operation, 'Git', Git)
    monkeypatch.setattr(operation, 'runtime_modules', lambda *_args: (checkpoint, None, None, None))
    monkeypatch.setattr(operation, 'owned_boundary', lambda *_args, **_kwargs: nullcontext(None))
    monkeypatch.setattr(operation, 'preserved', lambda _packet: {'synthetic': '0' * 64})
    monkeypatch.setattr(operation, 'installed_dependencies', lambda _root: dict.fromkeys(
        ('package.json', 'package-lock.json', 'node_modules/.package-lock.json'), '0' * 64))
    def ready(_packet, *_args, owned=False, **_kwargs):
        assert owned is state['owned']
        return dict(host_observation(), native=NEW, runtime=NEW, resources_ok=True, queues_idle=True,
                    ownership={'phase': 'owned-maintenance' if owned else 'released-clear',
                               'verified': True, 'recovery_required': owned})
    monkeypatch.setattr(operation, 'readiness', ready)
    def native(self, mode, *_args, **_kwargs):
        state['calls'].append(mode)
        if mode == 'Restore':
            assert state['owned']
            operation.write_new(output / 'mutation-start.json', {'schema': 'cre-code-restoration-mutation-v1',
                'restoration_sha256': sha(packet_raw), 'authorization_sha256': sha(auth.read_bytes()),
                'operation_output': str(output), 'tooling': packet['tooling'], 'fleet': packet['task_baseline']})
            state['enabled'] = [True] * 3
            for task in held_observation(packet, inputs)['tasks']:
                raw = inputs.data[next(row['path'] for row in inputs.rows if row['role'] == task['xml'])]
                (output / (task['key'] + '-final.xml')).write_bytes(
                    raw.replace(b'<Enabled>false</Enabled>', b'<Enabled>true</Enabled>'))
        elif mode in {'Observe', 'Verify', 'Postreadback'}:
            tasks = held_observation(packet, inputs)['tasks']
            if mode in {'Verify', 'Postreadback'}:
                assert state['enabled'] == [True] * 3
                for task in tasks:
                    raw = inputs.data[next(row['path'] for row in inputs.rows if row['role'] == task['xml'])]
                    raw = raw.replace(b'<Enabled>false</Enabled>', b'<Enabled>true</Enabled>')
                    (self.output / (task['key'] + '-observed.xml')).write_bytes(raw)
                    task.update(enabled=True, state='Ready', xml_sha256=sha(raw))
            operation.write_new(self.output / 'native-observation.json', {'observed_at': NOW.isoformat(), 'tasks': tasks,
                                'windows_resources': host_observation()['windows_resources']})
        else:
            raise AssertionError('unexpected synthetic native command: ' + mode)
    monkeypatch.setattr(operation.Foreground, 'native', native)
    monkeypatch.setattr(operation, 'released_authority', lambda *_args: released)
    monkeypatch.setattr(operation, 'owned_authority', lambda *_args: {
        'pid': 123, 'token': 'synthetic-owned-token', 'generation': 'synthetic-generation',
        'recovery_required': True, 'authority_identity': [1, 2], 'directory_identity': [1, 3]})
    def clear(_checkpoint, _lock, *, released):
        assert released is not None and not state['owned'] and state['cleared']
        state['calls'].append('released')
    monkeypatch.setattr(operation, 'lock_clear', clear)
    argv = ['--restoration-packet', str(packet_path), '--apply', '--finite-reviewed',
            '--observation-output', str(obsdir), '--observation', str(obsdir / 'observation.json'),
            '--authorization', str(auth), '--output', str(output), '--review', str(review),
            '--reviewer', 'independent', '--approval-ref', 'AGENTIC-test']
    if tamper_review:
        body.write_bytes(b'changed after review')
        with pytest.raises(HandoffError):
            operation.main(argv)
        assert state['calls'] == ['Observe'] and not output.exists()
        return
    assert operation.main(argv) == 0
    assert state['calls'] == ['Observe', 'lease', 'Restore', 'Verify', 'clear', 'exit', 'released']
    terminal = json_bytes((output / 'restoration-terminal.json').read_bytes())
    assert terminal['status'] == 'restored' and terminal['canonical_lease_released'] is True
    stdout, stderr = Path(str(output) + '.stdout.raw'), Path(str(output) + '.stderr.raw')
    stdout.write_bytes(b'synthetic closed native stream'); stderr.write_bytes(b'')
    operation.write_new(Path(str(output) + '.native.json'), {
        'schema': 'cre-code-restoration-native-completion-v1', 'restoration_sha256': sha(packet_raw),
        'authorization_sha256': sha(auth.read_bytes()), 'operation_output': str(output),
        'stdout_sha256': sha(stdout.read_bytes()), 'stderr_sha256': sha(stderr.read_bytes()),
        'capture': {'Started': True, 'Failed': False, 'NativeExitCode': 0,
                    'Stdout': {'Eof': True, 'Truncated': False}, 'Stderr': {'Eof': True, 'Truncated': False}}})
    independent = tmp_path / 'independent'
    assert operation.main(['--restoration-packet', str(packet_path), '--postreadback',
                           '--operation', str(output), '--output', str(independent)]) == 0
    reconciliation = json_bytes((independent / 'reconciliation.json').read_bytes())
    assert reconciliation['failed_predecessor_remains_failed'] is True
    assert state['calls'][-1] == 'Postreadback'
