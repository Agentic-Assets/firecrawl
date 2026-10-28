import cre_checkpoint_refresh as refresh
import pytest
from source_evidence import automatic, buildout_pages, events, native_pages
from test_automatic import SHA, setup_policy
from test_buildout_pages import entry


SOURCE = 'fortis-net-lease'


def native_run(tmp_path):
    config, policy, path = setup_policy(tmp_path, sources=(SOURCE,), count=100)
    policy.update(schema_version=automatic.SCHEMA_V2, collector_projections={},
                  native_source_formats={SOURCE: buildout_pages.FORMAT}, max_native_pages=4)
    raw = events.encoded(policy)
    path.write_bytes(raw)
    config = automatic.configuration(str(path), events.sha256(raw), config['env_file'],
                                     config['target_sha256'], SHA, [SOURCE])
    run = tmp_path / 'native-buildout'
    run.mkdir(mode=0o700)
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=(SOURCE,),
                                    page_cap=4, concurrency=1, automatic_source_evidence=config)
    attempt = {'number': 1, 'source_evidence': {'state': 'no_artifact'}}
    manifest['sources'][SOURCE]['attempts'].append(attempt)
    directory = native_pages.mark(run, manifest, SOURCE, attempt)
    return run, manifest, attempt, directory


def test_156_native_rows_survive_without_a_canonical_artifact_and_replay_exactly(tmp_path, monkeypatch):
    run, manifest, attempt, directory = native_run(tmp_path)
    for page in range(2):
        records = [{'nativeOrdinal': index, 'unknown': [None, False, ''],
                    'index_attributes': [['Original units', '4 per source unit'], ['Original units', None]]}
                   for index in range(78)]
        records[1] = records[0]  # Duplicates and address/ID absence are retained.
        records[2] = None
        body = events.encoded({'meta': {'total': 156, 'limit': 78}, 'inventory': records})
        raw, name = entry(body, source=SOURCE, page=page)
        path = directory / name
        path.write_bytes(raw)
        path.chmod(0o600)
    native_pages.settle(run, manifest, SOURCE, attempt, lambda *args: None)
    state = attempt['native_source_evidence']
    assert state['state'] == 'settled' and len(state['pages']) == 2
    plans = []
    for page in state['pages']:
        plan = native_pages.prepare_job(run, manifest, SOURCE, attempt, page)
        assert len(plan['events']) == 79
        assert plan['events'][0]['facts'][0]['kind'] == 'buildout_parsed_context'
        assert plan['events'][3]['facts'][0]['value'] is None
        plans.append(plan)
    calls = []
    monkeypatch.setattr(automatic.cli, 'run_psql', lambda *args, **kwargs: calls.append(args))
    automatic.pump(run, manifest, lambda *args: None)
    assert len(calls) == 2 and all(page['state'] == 'appended' for page in state['pages'])
    automatic.pump(run, manifest, lambda *args: None)
    assert len(calls) == 2
    assert attempt['source_evidence']['state'] == 'no_artifact'
    assert len({plan['events'][0]['capture_id'] for plan in plans}) == 2


def test_worker_environment_removes_foreign_scope_and_binds_one_source(tmp_path):
    prefix = native_pages.BUILDOUT_ENV_PREFIX
    env = {prefix + '_' + suffix: 'foreign' for suffix in
           ('DIR', 'SOURCE_KEY', 'SINGLE_WRITER', 'MAX_PAGES', 'MAX_BYTES')}
    native_pages.configure_worker_env(env, tmp_path, SOURCE, {}, {})
    assert not env
    attempt = {'number': 2, 'native_source_evidence': {
        'directory': f'source-evidence/native-{SOURCE}-attempt-2'}}
    config = {'native_source_formats': {SOURCE: buildout_pages.FORMAT}, 'max_native_pages': 4}
    native_pages.configure_worker_env(env, tmp_path, SOURCE, attempt, config)
    assert env[prefix + '_SOURCE_KEY'] == SOURCE
    assert env[prefix + '_SINGLE_WRITER'] == '1'
    assert env[prefix + '_DIR'].endswith(f'native-{SOURCE}-attempt-2')


@pytest.mark.parametrize('source', ['jll', 'unregistered'])
def test_unscoped_sources_do_not_inherit_buildout_native_authority(source):
    assert not native_pages.format_supported(source, buildout_pages.FORMAT)


@pytest.mark.parametrize('source', ['svn', 'lee-associates', 'franklin-street'])
def test_core_feeds_have_an_explicit_native_format_scope(source):
    assert native_pages.format_supported(source, buildout_pages.FORMAT)


def test_policy_accepts_only_explicit_registered_format_and_scope(tmp_path):
    config, policy, path = setup_policy(tmp_path, sources=(SOURCE,), count=100)
    policy.update(schema_version=automatic.SCHEMA_V2, collector_projections={},
                  native_source_formats={SOURCE: 'unknown_buildout_format'}, max_native_pages=4)
    raw = events.encoded(policy)
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='invalid native capture'):
        automatic.configuration(str(path), events.sha256(raw), config['env_file'],
                                config['target_sha256'], SHA, [SOURCE])
