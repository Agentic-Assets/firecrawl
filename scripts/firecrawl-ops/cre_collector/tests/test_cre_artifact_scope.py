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

@pytest.mark.parametrize('marker', [True, None, 0, 1, 'true', [], {}])
def test_terminal_capture_partial_never_qualifies_canonical(marker, tmp_path):
    artifact = {'runMeta': {'mode': 'full', 'terminalCaptureFailure': marker},
                'sources': [{'sourceKey': 'example', 'error': 'native_capture_failed'}],
                'listings': [{'sourceKey': 'example', 'url': 'https://example.com/1'}]}
    path = tmp_path / 'partial.json'
    path.write_text(json.dumps(artifact))
    with pytest.raises(ValueError):
        require_canonical_artifact(artifact)
    with pytest.raises(ValueError):
        cre_monitor.load_artifact_groups([str(path)])
    with pytest.raises(ValueError):
        cre_gate.count_artifacts([str(path)], quiet=True)
    with pytest.raises(ValueError):
        cre_ingest.validate_strict_artifact_freshness(artifact, require_strict_freshness=False)
    from cre_enrich import validate_enriched_artifact
    with pytest.raises(ValueError):
        validate_enriched_artifact([], artifact)
    artifact["runMeta"]["mode"] = "enrich"
    artifact["listings"] = []
    with pytest.raises(ValueError):
        validate_enriched_artifact([{ "url": "https://example.com/1", "source_key": "example" }], artifact)


def test_terminal_partial_converts_and_reconstructs_prior_evidence():
    from source_evidence import collector
    artifact = {'runMeta': {'mode': 'full', 'finishedAt': '2026-10-28T12:00:00Z',
                            'terminalCaptureFailure': True},
                'sources': [{'sourceKey': 'example', 'retainedObservations': [{'fact': None}]},
                            {'sourceKey': 'example', 'error': 'native_capture_failed'}],
                'listings': [{'sourceKey': 'example', 'url': 'https://example.com/1'}]}
    wrapped = collector.convert(artifact, source_key='example')
    assert collector.reconstruct(wrapped) == artifact


def test_parent_rejected_archive_keeps_private_partial_and_hash(tmp_path):
    import hashlib
    import os

    import cre_checkpoint_refresh as refresh

    artifact = {'runMeta': {'mode': 'full', 'finishedAt': '2026-10-28T12:00:00Z',
                            'terminalCaptureFailure': True},
                'sources': [{'sourceKey': 'example', 'error': 'native_capture_failed'}],
                'listings': [{'sourceKey': 'example', 'url': 'https://example.com/1'}]}
    path = tmp_path / 'worker.json'
    content = json.dumps(artifact).encode()
    path.write_bytes(content)
    relative, digest = refresh._archive_rejected(path, tmp_path, 'example', 1)
    archived = tmp_path / relative
    assert archived.read_bytes() == content
    assert digest == hashlib.sha256(content).hexdigest()
    assert os.stat(archived).st_mode & 0o777 == 0o600
    assert not path.exists()
