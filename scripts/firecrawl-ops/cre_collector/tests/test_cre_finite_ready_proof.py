"""Finite ready qualification retains evidence and replays Git witnesses."""
import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cre_finite_full_join import (  # noqa: F401
    admitted,
    complete_case,
    continued_ready_case,
    current_inputs,
    lineage_case,
    qualified_ready_case,
    restoration_case,
    scheduler_principal_in_synthetic_xml,
)

from cre_code_handoff_evidence import HandoffError


def test_ready_proof_cannot_be_constructed_without_qualification():
    from cre_maintenance_validation_context import ReadyReleaseProof, RecordingGit

    with pytest.raises(HandoffError, match='fully qualified'):
        ReadyReleaseProof(b'raw', {}, object(), RecordingGit('/git'))


def test_ready_proof_replays_full_release_git_and_rejects_drift(monkeypatch):
    import cre_code_handoff_git as git_module
    import cre_runtime_maintenance as maintenance
    from cre_maintenance_validation_context import qualify_ready_release

    value = [b'original']
    calls = []
    monkeypatch.setattr(git_module.Git, 'command', lambda *_args, **_kwargs: value[0])
    def validate(packet, evidence, *, git):
        calls.append('validate')
        git.command('/repo', 'rev-parse', 'a' * 40 + '^{tree}')
    monkeypatch.setattr(maintenance, 'validate_maintenance_release', validate)
    packet = {'programs': {'native_git': '/git'}, 'roots': {'development': '/repo'}}
    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={}, recheck=lambda: calls.append('read'))
    proof = qualify_ready_release(b'raw', packet, evidence)
    proof.recheck(b'raw', packet, evidence)
    assert calls == ['validate', 'read', 'read']
    value[0] = b'changed'
    with pytest.raises(HandoffError, match='Git witness changed'):
        proof.recheck(b'raw', packet, evidence)


def test_ready_proof_binds_packet_raw_evidence_and_witness(monkeypatch):
    import cre_runtime_maintenance as maintenance
    from cre_maintenance_validation_context import qualify_ready_release

    monkeypatch.setattr(maintenance, 'validate_maintenance_release', lambda *_args, **_kwargs: None)
    packet = {'programs': {'native_git': '/git'}, 'roots': {'development': '/repo'}, 'nested': {'value': 1}}
    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={}, recheck=lambda: None)
    proof = qualify_ready_release(b'raw', packet, evidence)
    for raw, current, items in ((b'other', packet, evidence),
                                 (b'raw', copy.deepcopy(packet), evidence),
                                 (b'raw', packet, SimpleNamespace(inputs={}, original={}, original_payloads={}))):
        with pytest.raises(HandoffError):
            proof.recheck(raw, current, items)
    proof.git._witnesses[('/repo', ('rev-parse', 'x'))] = (1, 'fake')
    with pytest.raises(HandoffError, match='witness'):
        proof.recheck(b'raw', packet, evidence)


def test_ready_proof_rechecks_nested_file_and_archive(tmp_path, monkeypatch):
    import cre_runtime_maintenance as maintenance
    import cre_runtime_maintenance_build as build
    from cre_code_handoff_evidence import Evidence, sha
    from cre_code_handoff_git import read_file
    from cre_maintenance_validation_context import qualify_ready_release

    prior = tmp_path / 'prior'
    prior.write_bytes(b'original prior')
    data = [b'original archive']
    monkeypatch.setattr(build, '_source_archive', lambda *_args: data[0])
    def validate(_packet, evidence, *, git):
        evidence.original_payloads[str(prior)] = read_file(prior)
        git.record_archive('/repo', {'sha': 'a' * 40}, 'api', sha(data[0]))
    monkeypatch.setattr(maintenance, 'validate_maintenance_release', validate)
    packet = {'programs': {'native_git': '/git'}, 'roots': {'development': '/repo'}}
    evidence = Evidence({'inputs': []}, read_file)
    proof = qualify_ready_release(b'raw', packet, evidence)
    proof.recheck(b'raw', packet, evidence)
    prior.write_bytes(b'changed prior')
    with pytest.raises(HandoffError, match='payload changed'):
        proof.recheck(b'raw', packet, evidence)
    prior.write_bytes(b'original prior')
    data[0] = b'changed archive'
    with pytest.raises(HandoffError, match='source archive changed'):
        proof.recheck(b'raw', packet, evidence)


@pytest.mark.parametrize('args', [('rev-parse', 'HEAD'), ('rev-parse', 'refs/remotes/origin/main'),
                                  ('status', '--porcelain=v1'), ('show', 'HEAD:file')])
def test_ready_recorder_refuses_mutable_git_queries(args):
    from cre_maintenance_validation_context import ReadyRecordingGit

    git = ReadyRecordingGit('/git', '/repo')
    with pytest.raises(HandoffError, match='immutable objects'):
        git.command('/repo', *args)


def test_ready_recorder_replays_real_git_objects_and_refuses_replacement(tmp_path):
    from cre_maintenance_validation_context import ReadyRecordingGit

    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True).stdout.decode().strip()
    git('init', '-q')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (repo / 'file').write_text('one')
    git('add', 'file')
    git('commit', '-qm', 'one')
    old = git('rev-parse', 'HEAD')
    (repo / 'file').write_text('two')
    git('commit', '-qam', 'two')
    new = git('rev-parse', 'HEAD')
    recorder = ReadyRecordingGit('/unused/git', str(repo))
    recorder.command(repo, 'rev-parse', old + '^{tree}')
    recorder.command(repo, 'show', old + ':file')
    recorder.command(repo, 'ls-tree', '-rz', '--full-tree', old)
    recorder.command(repo, 'merge-base', '--is-ancestor', old, new)
    recorder.recheck()
    git('replace', old, new)
    with pytest.raises(HandoffError, match='Git witness changed'):
        recorder.recheck()


def test_default_ready_reader_still_performs_full_validation(monkeypatch):
    import cre_code_restoration_evidence as restoration
    import cre_runtime_maintenance as maintenance
    from cre_runtime_maintenance_transition import validate_maintenance_ready

    packet = {'roots': {'native': '/native', 'runtime': '/runtime', 'tooling': '/tooling', 'development': '/dev'},
              'schema': 'cre-runtime-maintenance-v1', 'old': {}, 'new': {}}
    raw = json.dumps(packet).encode()
    ready = {'schema': 'cre-runtime-maintenance-ready-v1', 'old': {}, 'new': {},
             'release_reference': {'path': '/release'}, 'recovery_required_at_publication': True}
    from cre_code_handoff_evidence import sha
    ready['release_sha256'] = sha(raw)
    monkeypatch.setattr(restoration, 'retained_reference', lambda *_: raw)
    def full(*_args, **_kwargs):
        raise ValueError('full release validation called')
    monkeypatch.setattr(maintenance, 'validate_maintenance_release', full)
    with pytest.raises(ValueError, match='full release validation called'):
        validate_maintenance_ready(packet, object(), ready, None, release_raw=raw, release_path='/release')


def test_full_ready_replay_is_not_added_to_regular_finite_recheck(monkeypatch):
    import cre_runtime_maintenance as maintenance
    from cre_finite_handoff import _QUALIFIED, FiniteProof
    from cre_maintenance_validation_context import qualify_ready_release

    monkeypatch.setattr(maintenance, 'validate_maintenance_release', lambda *_args, **_kwargs: None)
    packet = {'programs': {'native_git': '/git'}, 'roots': {'development': '/repo'}}
    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={}, recheck=lambda: None)
    ready = qualify_ready_release(b'raw', packet, evidence)
    calls = []
    monkeypatch.setattr(ready, 'recheck', lambda *_args, **_kwargs: calls.append('ready'))
    stack = SimpleNamespace(recheck=lambda: calls.append('stack'))
    proof = FiniteProof(b'raw', packet, evidence, stack, None, ready=ready, _token=_QUALIFIED)
    proof.recheck(b'raw', packet, evidence)
    assert calls == ['stack']
    proof.recheck_ready(b'raw', packet, evidence)
    assert calls == ['stack', 'ready']


def test_ready_reader_refuses_fake_finite_context():
    from cre_runtime_maintenance_transition import validate_maintenance_ready

    with pytest.raises(HandoffError, match='finite ready qualification differs'):
        validate_maintenance_ready({}, object(), {}, None, release_raw=b'raw', release_path='/release',
                                   finite=SimpleNamespace(recheck_ready=lambda *_: None))


def test_changed_ready_log_prevents_release_start(continued_ready_case, tmp_path, monkeypatch):  # noqa: F811
    from test_cre_hold_prefix_full_join import build_hold_successor
    from test_cre_maintenance_integrated import run_actual_apply_steps

    import cre_finite_handoff as finite
    import cre_runtime_maintenance as maintenance

    case = build_hold_successor(continued_ready_case, tmp_path, monkeypatch)
    case.evidence._loaded_finite = case.evidence._loaded_maintenance
    prior_auth = json.loads(case.authorization.read_bytes())
    review = Path(prior_auth['review']['path'])
    case.authorization.unlink()
    observation_dir = tmp_path / 'finite-observation'
    case.observation = observation_dir / 'observation.json'
    validate = maintenance.validate_maintenance_ready

    def tamper(*args, **kwargs):
        validate(*args, **kwargs)
        path = Path(args[2]['commands'][0]['log']['path'])
        path.write_bytes(b'changed after ready validation')

    monkeypatch.setattr(maintenance, 'validate_maintenance_ready', tamper)
    def drive(args, git, modules):
        args.observation_output, args.observation = str(observation_dir), str(case.observation)
        args.review = str(review)
        args.reviewer = prior_auth['review']['reviewer']
        args.approval_ref = prior_auth['review']['approval_ref']
        finite.run_finite(args, case.release_raw, case.packet, case.evidence, git, modules)

    with pytest.raises(HandoffError):
        run_actual_apply_steps(case, tmp_path, monkeypatch, False, with_ipc=True, drive=drive)
    assert not (case.output / 'release-start.json').exists()
    assert not (case.output / 'terminal.json').exists()
