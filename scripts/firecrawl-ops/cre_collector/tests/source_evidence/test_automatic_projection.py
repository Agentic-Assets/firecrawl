"""Projected evidence seals contain no copies or commitments to removed values."""
import hashlib
import json

import pytest
from source_evidence import automatic, events, map_projection
from test_automatic import SHA, artifact, setup_policy, state


def projected_policy(tmp_path):
    config, policy, path = setup_policy(tmp_path)
    policy.update(schema_version=automatic.SCHEMA_V2,
                  collector_projections={'faris-lee': map_projection.VERSION},
                  native_source_formats={}, max_native_pages=128)
    raw = events.encoded(policy)
    path.write_bytes(raw)
    config = automatic.configuration(str(path), hashlib.sha256(raw).hexdigest(), config['env_file'],
                                     config['target_sha256'], SHA, ['faris-lee'])
    return config


@pytest.mark.parametrize("source", ["cushman-wakefield", "nai-global", "svn"])
def test_shared_map_sources_require_exact_explicit_policy_version(tmp_path, source):
    config, policy, path = setup_policy(tmp_path, sources=(source,))
    policy.update(schema_version=automatic.SCHEMA_V2,
                  collector_projections={source: map_projection.version_for(source)},
                  native_source_formats={}, max_native_pages=128)
    raw = events.encoded(policy)
    path.write_bytes(raw)
    accepted = automatic.configuration(str(path), hashlib.sha256(raw).hexdigest(), config['env_file'],
                                       config['target_sha256'], SHA, [source])
    assert accepted['collector_projections'] == {source: 'google-map-tile-query/v1'}
    policy['collector_projections'][source] = map_projection.VERSION
    raw = events.encoded(policy)
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        automatic.configuration(str(path), hashlib.sha256(raw).hexdigest(), config['env_file'],
                                config['target_sha256'], SHA, [source])


def test_projection_policy_and_exact_replay_preserve_seventy_eight_records(tmp_path, monkeypatch):
    config = projected_policy(tmp_path)
    obj = artifact()
    for row in obj['listings']:
        row['photos'] = ['https://maps.googleapis.com/maps/vt?pb=visible&key=APIKEY12&token=42']
    run, source, manifest, attempt = state(tmp_path, config, obj)
    automatic.seal(run, manifest, 'faris-lee', attempt, source)
    saved = attempt['source_evidence']
    assert saved['state'] == 'pending'
    assert saved['input_representation'] == 'projected_collector_observation_v1'
    sealed = (run / saved['input_path']).read_bytes()
    assert b'APIKEY12' not in sealed and b'&token=42' not in sealed
    assert hashlib.sha256(source.read_bytes()).hexdigest().encode() not in sealed
    wrapper = json.loads(sealed)
    projection = wrapper['provenance']['projection']
    assert len(projection['replacements']) == 78
    assert 'originalRawSha256' not in projection and 'originalCanonicalSha256' not in projection
    assert all(row['photos'] == ['https://maps.googleapis.com/maps/vt?pb=visible'] for row in wrapper['records'])
    assert len(events.plan(wrapper, source_key='faris-lee')['events']) == 79
    calls = []
    monkeypatch.setattr(automatic.cli, 'run_psql', lambda *a, **k: calls.append(a))
    automatic.pump(run, manifest, lambda *_: None)
    assert saved['state'] == 'appended' and len(calls) == 1
    saved['state'] = 'pending'
    automatic.pump(run, manifest, lambda *_: None)
    assert saved['state'] == 'appended' and len(calls) == 2


def test_legacy_policy_refuses_credential_bearing_capture(tmp_path):
    config, _, _ = setup_policy(tmp_path)
    obj = artifact()
    obj['listings'][0]['photos'] = ['https://maps.googleapis.com/maps/vt?key=APIKEY12&token=42']
    run, source, manifest, attempt = state(tmp_path, config, obj)
    automatic.seal(run, manifest, 'faris-lee', attempt, source)
    assert attempt['source_evidence']['state'] == 'refused'
    assert not (run / 'source-evidence').exists()


def test_projected_input_hash_and_representation_are_required(tmp_path, monkeypatch):
    config = projected_policy(tmp_path)
    run, source, manifest, attempt = state(tmp_path, config, artifact())
    automatic.seal(run, manifest, 'faris-lee', attempt, source)
    saved = attempt['source_evidence']
    saved['input_representation'] = 'original'
    monkeypatch.setattr(automatic.cli, 'run_psql', lambda *a, **k: pytest.fail('database contacted'))
    automatic.pump(run, manifest, lambda *_: None)
    assert saved['state'] == 'refused'
