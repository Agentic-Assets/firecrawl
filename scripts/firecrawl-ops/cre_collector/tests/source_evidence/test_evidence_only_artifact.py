import copy

import pytest

from cre_artifact_scope import require_canonical_artifact
from source_evidence import collector, events


def test_partial_source_observations_remain_private_context_without_listing_promotion():
    observations = [
        {'event': 'foundry_held_observation', 'schema': 'foundry-held-observation-v1',
         'observation': {'id': index, 'status': None, 'reason': 'unknown_status',
                         'propertyNotes': [] if index % 2 else [{'label': 'Source note', 'value': False}]}}
        for index in range(78)
    ]
    artifact = {
        'runMeta': {'mode': 'full', 'finishedAt': '2026-10-28T12:00:00Z', 'evidenceOnly': True,
                    'evidencePurpose': 'private_unqualified_source_observations'},
        'sources': [{'sourceKey': 'foundry-commercial', 'truncated': True, 'listingsCollected': 0,
                     'retainedObservations': observations}],
        'listings': [],
    }
    original = copy.deepcopy(artifact)
    with pytest.raises(ValueError):
        require_canonical_artifact(artifact)
    observation = collector.convert(artifact, source_key='foundry-commercial')
    plan = events.plan(observation, source_key='foundry-commercial')
    events.validate_content_policy(plan)
    assert len(plan['events']) == 79
    assert plan['events'][0]['facts'][0]['kind'] == 'retained_capture_context_v2'
    stored_context = plan['events'][0]['facts'][0]['value']['context']
    assert stored_context['sources'][0]['retainedObservations'] == []
    assert stored_context['runMeta']['evidenceOnly'] is True
    assert [row['facts'][0]['value']['value'] for row in plan['events'][1:]] == observations
    assert all(row['facts'][0]['kind'] == 'source_observation' for row in plan['events'][1:])
    assert collector.reconstruct(observation) == artifact
    assert observation['records'] == [] and artifact == original
