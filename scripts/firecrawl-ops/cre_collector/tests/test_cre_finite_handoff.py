"""Finite reviewed handoff preserves qualification before fresh admission."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from cre_code_handoff_evidence import HandoffError


def test_qualification_precedes_observe_authorize_and_apply(monkeypatch, tmp_path):
    import cre_code_handoff as handoff
    import cre_finite_handoff as finite
    import cre_runtime_maintenance as maintenance

    calls = []
    args = SimpleNamespace(observation_output=str(tmp_path / "observation"),
                           observation=str(tmp_path / "observation" / "observation.json"),
                           authorization=str(tmp_path / "authorization"), output=str(tmp_path / "operation"))
    packet = {"schema": "cre-runtime-maintenance-v1"}
    class Proof:
        def extend(self, *_args): pass
    monkeypatch.setattr(finite, "prepare_finite", lambda *_: calls.append("prepare") or Proof())
    def observe(*_args):
        calls.append("observe")
        (tmp_path / "observation").mkdir()
        (tmp_path / "observation" / "observation.json").write_text(json.dumps({
            "release_sha256": finite.sha(b"release"), "tasks": []}))
    def authorize(*_args):
        calls.append("authorize")
        (tmp_path / "body").write_bytes(b"review")
        (tmp_path / "review").write_text(json.dumps({"body": {"path": str(tmp_path / "body"),
            "bytes": 6, "sha256": finite.sha(b"review")}}))
        review_raw = (tmp_path / "review").read_bytes()
        (tmp_path / "authorization").write_text(json.dumps({"observation_sha256": finite.sha(
            (tmp_path / "observation" / "observation.json").read_bytes()), "review": {
                "path": str(tmp_path / "review"), "sha256": finite.sha(review_raw)}}))
    monkeypatch.setattr(finite, "observe_finite", observe)
    monkeypatch.setattr(finite, "authorize_finite", authorize)
    monkeypatch.setattr(finite, "apply_finite", lambda *_: calls.append("apply"))
    monkeypatch.setattr(maintenance, "validate_maintenance_observation", lambda *_: None)
    monkeypatch.setattr(maintenance, "validate_maintenance_authorization", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "check_boundary_times", lambda *_args: None)
    finite.run_finite(args, b"release", packet, SimpleNamespace(original_payloads={}), object(), ())
    assert calls == ["prepare", "observe", "authorize", "apply"]


def test_failed_qualification_never_observes_or_applies(monkeypatch, tmp_path):
    import cre_finite_handoff as finite

    args = SimpleNamespace(observation_output=str(tmp_path / "observation"),
                           observation=str(tmp_path / "observation" / "observation.json"),
                           authorization=str(tmp_path / "authorization"), output=str(tmp_path / "operation"))
    monkeypatch.setattr(finite, "prepare_finite", lambda *_: (_ for _ in ()).throw(ValueError("refused")))
    monkeypatch.setattr(finite, "observe_finite", lambda *_: pytest.fail("observation reached"))
    with pytest.raises(ValueError, match="refused"):
        finite.run_finite(args, b"release", {"schema": "cre-runtime-maintenance-v1"}, object(), object(), ())


def test_preparation_refuses_unloaded_evidence():
    from cre_finite_handoff import FiniteProof, prepare_finite

    with pytest.raises(HandoffError, match='fully loaded release'):
        prepare_finite(object(), b'release', {'schema': 'cre-runtime-maintenance-v1'}, object(), object(), ())
    with pytest.raises(HandoffError, match='full qualification'):
        FiniteProof(b'release', {}, object(), None, None)


def test_owner_requires_exact_finite_argument_set():
    from pathlib import Path

    from cre_code_handoff import validate_owner_command

    script = Path('/tool/cre_code_handoff.py')
    command = [b'python3', str(script).encode(), b'--maintenance-packet', b'/release', b'--apply',
               b'--finite-reviewed', b'--observation-output', b'/observation',
               b'--observation', b'/observation/observation.json', b'--authorization', b'/auth',
               b'--output', b'/operation', b'--review', b'/review', b'--reviewer', b'peer',
               b'--approval-ref', b'issue']
    expected = {'--maintenance-packet': '/release', '--observation': '/observation/observation.json',
                '--authorization': '/auth', '--output': '/operation'}
    validate_owner_command(command, script, maintenance_inputs=expected)
    with pytest.raises(HandoffError):
        validate_owner_command(command + [b'--unknown'], script, maintenance_inputs=expected)
    with pytest.raises(HandoffError):
        validate_owner_command(command + [b'--finite-reviewed'], script, maintenance_inputs=expected)
    other = command.copy()
    other[other.index(b'--observation-output') + 1] = b'/foreign'
    with pytest.raises(HandoffError):
        validate_owner_command(other, script, maintenance_inputs=expected)


def test_finite_proof_refuses_unadmitted_or_changed_nested_bytes():
    from cre_finite_handoff import _QUALIFIED, FiniteProof

    packet = {"schema": "cre-runtime-maintenance-v1"}
    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={'/prior': b'original'}, recheck=lambda: None)
    replay = SimpleNamespace(recheck=lambda: None)
    proof = FiniteProof(b'release', packet, evidence, replay, None, _token=_QUALIFIED)
    evidence.original_payloads['/observed'] = b'fresh xml'
    proof.extend(b'release', packet, evidence, {'/observed'})
    proof.recheck(b'release', packet, evidence)
    evidence.original_payloads['/other'] = b'unreviewed'
    with pytest.raises(HandoffError, match='outside admitted inputs'):
        proof.extend(b'release', packet, evidence, {'/observed'})
    del evidence.original_payloads['/other']
    evidence.original_payloads['/prior'] = b'changed'
    with pytest.raises(HandoffError, match='outside admitted inputs'):
        proof.extend(b'release', packet, evidence, set())


def test_restoration_refuses_observation_expiring_during_admission(tmp_path, monkeypatch):
    import cre_code_handoff as handoff

    start = datetime.now(UTC)
    current = [start]
    release, observation, authorization = (tmp_path / name for name in ('release', 'observation', 'authorization'))
    release.write_bytes(b'{}')
    observation.write_text(json.dumps({'observed_at': start.isoformat(), 'qualified_at': start.isoformat()}))
    authorization.write_text(json.dumps({'authorized_at': start.isoformat(),
                                         'expires_at': (start + timedelta(seconds=299)).isoformat(),
                                         'operation_output': str(tmp_path / 'operation')}))
    args = SimpleNamespace(release=str(release), observation=str(observation), authorization=str(authorization),
                           output=str(tmp_path / 'operation'))
    monkeypatch.setattr(handoff, 'now', lambda: current[0])
    monkeypatch.setattr(handoff, 'validate_restoration_authorization', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, 'restoration_enable_boundary',
                        lambda *_args: current.__setitem__(0, start + timedelta(seconds=300)))
    with pytest.raises(HandoffError, match='expired'):
        handoff.apply_restoration(args, b'{}', {}, object(), object(), (object(),))
    assert not (tmp_path / 'operation').exists()


def test_finite_proof_refuses_new_reference_added_during_recheck():
    from cre_finite_handoff import _QUALIFIED, FiniteProof

    packet = {"schema": "cre-runtime-maintenance-v1"}
    evidence = SimpleNamespace(inputs={}, original={}, original_payloads={})
    evidence.recheck = lambda: evidence.original_payloads.update({'/late': b'unreviewed'})
    proof = FiniteProof(b'release', packet, evidence, None, None, _token=_QUALIFIED)
    with pytest.raises(HandoffError, match='outside admitted inputs'):
        proof.extend(b'release', packet, evidence, set())
