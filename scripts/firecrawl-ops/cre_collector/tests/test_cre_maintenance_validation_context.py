"""In-memory qualification reuses facts only while inputs remain unchanged."""
import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from cre_code_handoff_evidence import HandoffError


def test_sealed_context_rechecks_bytes_and_refuses_packet_or_nested_drift():
    from cre_maintenance_validation_context import EvidenceSeal

    packet = {"schema": "test", "value": [1]}
    calls = []
    evidence = SimpleNamespace(inputs={"role": {"path": "/role"}}, original={"role": b"one"},
                               original_payloads={"/nested": b"two"}, recheck=lambda: calls.append("read"))
    sealed = EvidenceSeal(packet, evidence)
    sealed.recheck(packet, evidence)
    assert calls == ["read"]
    changed = copy.deepcopy(packet)
    changed["value"].append(2)
    with pytest.raises(HandoffError):
        sealed.recheck(changed, evidence)
    evidence.original_payloads["/nested"] = b"changed"
    with pytest.raises(HandoffError):
        sealed.recheck(packet, evidence)


def test_sealed_context_never_treats_new_evidence_paths_as_prevalidated():
    from cre_maintenance_validation_context import EvidenceSeal

    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={}, recheck=lambda: None)
    sealed = EvidenceSeal({}, evidence)
    evidence.original_payloads["/new"] = b"unreviewed"
    with pytest.raises(HandoffError):
        sealed.recheck({}, evidence)


def test_recorded_git_queries_are_reexecuted_and_drift_refuses(monkeypatch):
    from cre_code_handoff_git import Git
    from cre_maintenance_validation_context import RecordingGit

    calls = []
    current = {"bytes": b"original tree"}
    def command(_self, repo, *args, **_kwargs):
        calls.append((repo, args))
        return current["bytes"]
    monkeypatch.setattr(Git, "command", command)
    git = RecordingGit("/synthetic/git")
    assert git.command("/synthetic/repo", "rev-parse", "abc^{tree}") == b"original tree"
    git.recheck()
    assert len(calls) == 2
    current["bytes"] = b"replaced tree"
    with pytest.raises(HandoffError, match="Git witness changed"):
        git.recheck()


def test_recorded_nested_archives_are_all_regenerated(monkeypatch):
    import cre_runtime_maintenance_build as build
    from cre_code_handoff_evidence import sha
    from cre_maintenance_validation_context import RecordingGit

    values = {('a', 'api'): b'first', ('b', 'api'): b'second'}
    calls = []

    def archive(_repo, identity, service):
        key = (identity['sha'], service)
        calls.append(key)
        return values[key]

    monkeypatch.setattr(build, '_source_archive', archive)
    git = RecordingGit('/synthetic/git')
    for (identity, service), data in values.items():
        git.record_archive('/repo', {'sha': identity}, service, sha(data))
    git.recheck()
    assert set(calls) == set(values)
    values[('a', 'api')] = b'changed'
    with pytest.raises(HandoffError, match='source archive changed'):
        git.recheck()


def test_loaded_release_proof_binds_raw_packet_and_evidence_identity():
    from cre_maintenance_validation_context import LoadedMaintenance

    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={})
    packet = {"new": {"sha": "a" * 40}}
    proof = LoadedMaintenance(b"release", packet, evidence)
    proof.unchanged(b"release", packet, evidence)
    with pytest.raises(HandoffError):
        proof.unchanged(b"different", packet, evidence)
    with pytest.raises(HandoffError):
        proof.unchanged(b"release", packet, SimpleNamespace(inputs={}, original={}, original_payloads={}))


def test_loaded_admission_extends_only_unchanged_existing_evidence():
    from cre_maintenance_validation_context import LoadedMaintenance

    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={'/prior': b'prior'})
    proof = LoadedMaintenance(b'release', {}, evidence)
    evidence.original_payloads['/review'] = b'{"body":{"path":"/body"}}'
    evidence.original_payloads['/body'] = b'validated review'
    authorization = {'review': {'path': '/review'}}
    proof.extend_after_admission(b'release', {}, evidence, authorization)
    proof.unchanged(b'release', {}, evidence)
    evidence.original_payloads['/prior'] = b'changed'
    with pytest.raises(HandoffError, match='changed during admission'):
        proof.extend_after_admission(b'release', {}, evidence, authorization)
    del evidence.original_payloads['/prior']
    with pytest.raises(HandoffError, match='changed during admission'):
        proof.extend_after_admission(b'release', {}, evidence, authorization)


def test_loaded_admission_rejects_unrelated_new_evidence():
    from cre_maintenance_validation_context import LoadedMaintenance

    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={})
    proof = LoadedMaintenance(b'release', {}, evidence)
    evidence.original_payloads.update({'/review': b'{"body":{"path":"/body"}}',
                                       '/body': b'review', '/unrelated': b'other'})
    with pytest.raises(HandoffError, match='unexpected evidence'):
        proof.extend_after_admission(b'release', {}, evidence, {'review': {'path': '/review'}})


def test_loaded_admission_rejects_review_path_alias_collision():
    from cre_maintenance_validation_context import LoadedMaintenance

    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={'C:/Review.json': b'prior'})
    proof = LoadedMaintenance(b'release', {}, evidence)
    evidence.original_payloads.update({'c:/review.json': b'{"body":{"path":"/body"}}', '/body': b'review'})
    with pytest.raises(HandoffError, match='aliases'):
        proof.extend_after_admission(b'release', {}, evidence, {'review': {'path': 'c:/review.json'}})


def test_loaded_stack_reobserves_and_rejects_changed_live_model(monkeypatch):
    import cre_current_stack as stack
    import cre_runtime_maintenance as maintenance
    from cre_maintenance_validation_context import LoadedMaintenance

    old = {'sha': 'a' * 40, 'tree': 'b' * 40}
    before = {'sha': 'c' * 40, 'tree': 'd' * 40}
    packet = {'old': old, 'runtime_before': before, 'continuation': {'current_stack': 'model'},
              'roots': {'development': '/dev'}, 'tooling': before}
    model = {'schema': 'synthetic-model', 'value': 1}
    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={},
                               obj=lambda _role: model, recheck=lambda: None)
    evidence._loaded_maintenance = LoadedMaintenance(b'packet', packet, evidence)
    monkeypatch.setattr(stack, 'resolve_image_source', lambda *_: (b'prior', {}, object()))
    observed = [model]

    def capture(_raw, _prior, _evidence, output, **_kwargs):
        output.mkdir()
        path = output / 'model.json'
        path.write_text(json.dumps(observed[0]))
        return {'path': str(path)}

    monkeypatch.setattr(stack, 'capture_current_stack', capture)
    maintenance.recheck_continuation_stack(packet, evidence, object(), raw=b'packet')
    observed[0] = {'schema': 'synthetic-model', 'value': 2}
    with pytest.raises(HandoffError, match='fresh current stack differs'):
        maintenance.recheck_continuation_stack(packet, evidence, object(), raw=b'packet')


def test_apply_rechecks_expiry_after_slow_authorization_before_lease(tmp_path, monkeypatch):
    import cre_code_handoff as handoff
    import cre_runtime_maintenance as maintenance

    start = datetime(2026, 11, 3, tzinfo=UTC)
    current = [start]
    release = tmp_path / 'release.json'
    observation = tmp_path / 'observation.json'
    authorization = tmp_path / 'authorization.json'
    release.write_bytes(b'{}')
    observation.write_text(json.dumps({'observed_at': start.isoformat(), 'qualified_at': start.isoformat()}))
    authorization.write_text(json.dumps({'authorized_at': start.isoformat(),
                                         'expires_at': (start + timedelta(seconds=299)).isoformat()}))
    args = SimpleNamespace(release=str(release), observation=str(observation), authorization=str(authorization),
                           output=str(tmp_path / 'operation'))
    monkeypatch.setattr(handoff, 'now', lambda: current[0])
    monkeypatch.setattr(maintenance, 'validate_maintenance_release', lambda *_: None)
    monkeypatch.setattr(maintenance, 'validate_maintenance_authorization',
                        lambda *_args, **_kwargs: current.__setitem__(0, start + timedelta(seconds=300)))
    monkeypatch.setattr(maintenance, 'validate_operation_paths', lambda *_: pytest.fail('expired admission passed'))
    with pytest.raises(HandoffError, match='expired'):
        maintenance.apply_maintenance(args, b'{}', {}, object(), object(), (object(),))


def test_authorization_refuses_expiry_crossed_during_validation(tmp_path, monkeypatch):
    import cre_code_handoff as handoff
    import cre_runtime_maintenance as maintenance

    start = datetime(2026, 11, 3, tzinfo=UTC)
    current = [start]
    release, observation, review = (tmp_path / name for name in ('release', 'observation', 'review'))
    release.write_bytes(b'{}')
    observation.write_text(json.dumps({'observed_at': start.isoformat(), 'qualified_at': start.isoformat()}))
    review.write_bytes(b'review')
    args = SimpleNamespace(release=str(release), observation=str(observation), review=str(review),
                           reviewer='reviewer', approval_ref='test', output=str(tmp_path / 'operation'),
                           authorization=str(tmp_path / 'authorization'))
    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={}, recheck=lambda: None)
    monkeypatch.setattr(handoff, 'now', lambda: current[0])
    monkeypatch.setattr(maintenance, 'validate_maintenance_authorization',
                        lambda *_args, **_kwargs: current.__setitem__(0, start + timedelta(seconds=300)))
    with pytest.raises(HandoffError, match='expired'):
        maintenance.authorize_maintenance(args, b'{}', {}, evidence)
    assert not (tmp_path / 'authorization').exists()


def test_apply_rechecks_observation_age_after_slow_authorization(tmp_path, monkeypatch):
    import cre_code_handoff as handoff
    import cre_runtime_maintenance as maintenance

    start = datetime(2026, 11, 3, tzinfo=UTC)
    current = [start]
    release, observation, authorization = (tmp_path / name for name in ('release', 'observation', 'authorization'))
    release.write_bytes(b'{}')
    observation.write_text(json.dumps({'observed_at': start.isoformat(), 'qualified_at': start.isoformat()}))
    authorization.write_text(json.dumps({'authorized_at': start.isoformat(),
                                         'expires_at': (start + timedelta(seconds=500)).isoformat()}))
    args = SimpleNamespace(release=str(release), observation=str(observation), authorization=str(authorization),
                           output=str(tmp_path / 'operation'))
    monkeypatch.setattr(handoff, 'now', lambda: current[0])
    monkeypatch.setattr(maintenance, 'validate_maintenance_release', lambda *_: None)
    monkeypatch.setattr(maintenance, 'validate_maintenance_authorization',
                        lambda *_args, **_kwargs: current.__setitem__(0, start + timedelta(seconds=300)))
    monkeypatch.setattr(maintenance, 'validate_operation_paths', lambda *_: pytest.fail('expired observation passed'))
    with pytest.raises(HandoffError, match='expired'):
        maintenance.apply_maintenance(args, b'{}', {}, object(), object(), (object(),))
