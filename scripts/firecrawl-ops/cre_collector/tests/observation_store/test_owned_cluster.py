"""Deterministic failures for the isolated owned PostgreSQL fixture helper."""
import subprocess

import owned_observation_fixture_cluster as helper
import pytest


def test_initdb_timeout_does_not_start_and_retains_cluster(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(helper.subprocess, 'run', run)
    cluster = helper.OwnedCluster('/fixture/initdb', '/fixture/pg_ctl', port=1)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            cluster.__enter__()
        assert cluster.root.is_dir()
        assert len(calls) == 1 and calls[0][1]['timeout'] == 30
        assert set(calls[0][1]['env']) == {'PATH', 'HOME', 'TMPDIR', 'LC_ALL'}
    finally:
        helper.shutil.rmtree(cluster.root)


@pytest.mark.parametrize('stop_result,status_result', [(1, 0), (0, 0)])
def test_unconfirmed_stop_preserves_cluster(monkeypatch, stop_result, status_result):
    def run(command, **kwargs):
        assert kwargs['timeout'] > 0 and set(kwargs['env']) == {'PATH', 'HOME', 'TMPDIR', 'LC_ALL'}
        return subprocess.CompletedProcess(command, stop_result if command[-1] == 'stop' else status_result)
    monkeypatch.setattr(helper.subprocess, 'run', run)
    cluster = helper.OwnedCluster('/fixture/initdb', '/fixture/pg_ctl', port=1)
    cluster.start_attempted = True
    try:
        with pytest.raises(RuntimeError, match='cluster retained'):
            cluster.cleanup()
        assert cluster.root.is_dir()
    finally:
        helper.shutil.rmtree(cluster.root)
