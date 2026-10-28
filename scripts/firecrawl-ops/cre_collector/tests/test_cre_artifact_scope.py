import json

import pytest

import cre_gate
import cre_ingest
import cre_monitor
from cre_artifact_scope import require_canonical_artifact


@pytest.mark.parametrize('marker', [True, None, 0, 1, 'true', 'false', [], {}])
def test_evidence_scope_cannot_be_coerced_into_canonical_input(marker):
    data = {'runMeta': {'mode': 'full', 'evidenceOnly': marker}, 'sources': [], 'listings': []}
    with pytest.raises(ValueError, match='not a canonical'):
        require_canonical_artifact(data)


def test_declared_purpose_is_refused_even_if_boolean_marker_was_removed():
    with pytest.raises(ValueError):
        require_canonical_artifact({'runMeta': {'mode': 'full', 'evidencePurpose':
                                                'private_unqualified_source_observations'}})


def test_mixed_source_context_does_not_promote_observations_or_replace_other_gates():
    data = {'runMeta': {'mode': 'full', 'evidenceOnly': False},
            'sources': [{'sourceKey': 'foundry-commercial', 'truncated': True,
                         'retainedObservations': [{'status': None, 'propertyNotes': []}]}],
            'listings': []}
    require_canonical_artifact(data)
    assert data['listings'] == [] and data['sources'][0]['truncated'] is True


def test_evidence_only_artifact_is_refused_by_monitor_and_gate(tmp_path):
    artifact = {'runMeta': {'mode': 'full', 'evidenceOnly': True},
                'sources': [{'sourceKey': 'foundry-commercial', 'truncated': True,
                             'retainedObservations': [{'status': None}]}], 'listings': []}
    path = tmp_path / 'partial.json'
    path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError):
        cre_monitor.load_artifact_groups([str(path)])
    with pytest.raises(ValueError):
        cre_gate.count_artifacts([str(path)], quiet=True)
    with pytest.raises(ValueError):
        cre_ingest.validate_strict_artifact_freshness(artifact, require_strict_freshness=False)
