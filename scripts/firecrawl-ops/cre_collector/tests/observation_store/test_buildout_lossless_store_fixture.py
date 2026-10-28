"""Synthetic fixture-only planner: never provider or production data."""
import hashlib
import json
import subprocess

import buildout_lossless_store_fixture as fixture
import pytest

import buildout_lossless_cache_evidence as lossless


def raw_fixture(count=84):
    row = b'{"id":null,"size":1.00e+02,"size":-0,"index_attributes":[["API Key","DUMMY_SECRET_SENTINEL"],["Space","1"],["Space","2"]],"nested":{"unknown":false,"unknown":null}}'
    return b'{"syntheticFixture":true,"data":{"inventory":[' + b','.join([row] * count) + b']}}'


def test_lossless_fixture_binds_records_and_does_not_transfer_original():
    original = raw_fixture()
    evidence, _ = lossless.build(original)
    result = fixture.plan(original=original, evidence=evidence, capture_id='capture')
    data = json.loads(result['p_artifact_bytes'])
    assert data['records'] == result['p_observations'] and len(data['records']) == 85
    assert result['p_original_sha256'] == hashlib.sha256(original).hexdigest()
    assert result['p_provenance']['quarantinedItems'] == 84
    assert result['p_assertions']['source'] is None
    assert original not in result.values()
    assert b'DUMMY_SECRET_SENTINEL' not in result['p_artifact_bytes']
    row = data['records'][0]['evidence']
    values = [entry['value']['token'] for entry in row['entries'] if entry.get('key') == 'size']
    assert values == ['1.00e+02', '-0']
    assert data['records'][0]['identityResolution'] == 'unresolved'


def test_rejects_tamper_nonfixture_and_ambiguous_inventory():
    original = raw_fixture(1)
    evidence, _ = lossless.build(original)
    with pytest.raises(ValueError, match='differs'):
        fixture.plan(original=original, evidence=evidence + b' ', capture_id='capture')
    no_marker = b'{"data":{"inventory":[]}}'
    with pytest.raises(ValueError, match='marker'):
        fixture.plan(original=no_marker, evidence=lossless.build(no_marker)[0], capture_id='capture')
    ambiguous = b'{"syntheticFixture":true,"data":{"inventory":[],"inventory":[]}}'
    result = fixture.plan(original=ambiguous, evidence=lossless.build(ambiguous)[0], capture_id='capture')
    assert len(result['p_observations']) == 1
    assert result['p_observations'][0]['inventoryResolution'] == 'ambiguous_or_missing'
    assert result['p_provenance']['sourceCompleteness'] == 'unverified'


def test_failed_cluster_start_stops_and_retains_directory(monkeypatch):
    import owned_observation_fixture_cluster as runner

    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[-1] == 'start':
            raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(command, 3 if command[-1] == 'status' else 0)

    monkeypatch.setattr(runner.subprocess, 'run', fake_run)
    cluster = runner.OwnedCluster('/fake/initdb', '/fake/pg_ctl', port=55447)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            cluster.__enter__()
        assert cluster.root.is_dir()
        assert [call[0][-1] for call in calls][-2:] == ['stop', 'status']
        assert all(call[1]['timeout'] and set(call[1]['env']) == {'PATH', 'HOME', 'TMPDIR', 'LC_ALL'} for call in calls)
    finally:
        runner.shutil.rmtree(cluster.root)


def test_uncertain_cluster_stop_keeps_directory(monkeypatch):
    import owned_observation_fixture_cluster as runner

    def fake_run(command, **kwargs):
        if command[-1] == 'status':
            raise subprocess.TimeoutExpired(command, 10)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, 'run', fake_run)
    cluster = runner.OwnedCluster('/fake/initdb', '/fake/pg_ctl', port=55447)
    cluster.start_attempted = True
    try:
        with pytest.raises(RuntimeError, match='cluster retained'):
            cluster.cleanup()
        assert cluster.root.is_dir()
    finally:
        runner.shutil.rmtree(cluster.root)
