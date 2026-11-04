"""Retained Git witnesses preserve a distinct pre-existing runtime identity."""

import copy

import pytest

import cre_runtime_maintenance_transition as transition
from cre_code_handoff_evidence import HandoffError


def test_runtime_git_witness_uses_actual_before_without_replacing_original_a(monkeypatch):
    old = {'sha': 'a' * 40, 'tree': 'b' * 40}
    runtime = {'sha': 'c' * 40, 'tree': 'd' * 40}
    selected = {'sha': 'e' * 40, 'tree': 'f' * 40}
    packet = {'old': old, 'runtime_before': runtime, 'native_before': runtime, 'new': selected,
              'roots': {'runtime': '/runtime', 'native': '/native'}}
    git = {'schema': 'cre-runtime-maintenance-git-v1'}
    for key in ('native', 'runtime'):
        git[key] = {'before': {'root': packet['roots'][key], **runtime, 'status': '', 'tracking': runtime['sha']},
                    'after': {'root': packet['roots'][key], **selected, 'status': '', 'tracking': selected['sha']}}
    histories = []
    monkeypatch.setattr(transition, 'validate_maintenance_tracking_history',
                        lambda _git, _repo, before, tracking, new: histories.append((before, tracking, new)))
    transition.validate_maintenance_git_witness(packet, git, object(), '/source')
    assert histories == [(runtime, runtime['sha'], selected)] * 2
    assert packet['old'] == old
    forged = copy.deepcopy(git)
    forged['runtime']['before'].update(old)
    with pytest.raises(HandoffError, match='Git identity differs'):
        transition.validate_maintenance_git_witness(packet, forged, object(), '/source')


def test_legacy_git_witness_still_uses_original_identity(monkeypatch):
    old = {'sha': 'a' * 40, 'tree': 'b' * 40}
    selected = {'sha': 'c' * 40, 'tree': 'd' * 40}
    packet = {'old': old, 'native_before': old, 'new': selected,
              'roots': {'runtime': '/runtime', 'native': '/native'}}
    states = {key: {'before': {'root': packet['roots'][key], **old, 'status': '', 'tracking': old['sha']},
                    'after': {'root': packet['roots'][key], **selected, 'status': '', 'tracking': selected['sha']}}
              for key in ('native', 'runtime')}
    monkeypatch.setattr(transition, 'validate_maintenance_tracking_history', lambda *_args: None)
    transition.validate_maintenance_git_witness(packet, {'schema': 'cre-runtime-maintenance-git-v1', **states},
                                                object(), '/source')
