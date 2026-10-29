"""Synthetic native NIM evidence; no provider or database access."""
import base64
import copy
import gzip
import json

import pytest
from source_evidence import acquisition, events, native_feed_pages, native_pages
from test_native_feed_pages import entry


@pytest.mark.parametrize('profile', ['newmark_nim_sale_post_v1', 'newmark_nim_lease_post_v1'])
def test_nim_retains_80_unqualified_rows_and_exact_numbers(profile):
    rows = [b'{"id":null,"country":"US","details":{"empty":"","flag":false,"n":null,"sf":1.00000000000000001}}',
            b'{"country":"GB","id":"duplicate"}', b'{"country":null,"id":"duplicate"}'] * 27
    body = b'{"data":[' + b','.join(rows) + b'],"unknown":{"outside":false,"empty":""}}'
    raw, _, source = entry(profile, {'data': []})
    envelope = json.loads(raw); stored = gzip.compress(body)
    envelope['responseBodyBase64'] = base64.b64encode(stored).decode()
    envelope['receipt'].update(bodySha256=events.sha256(body), bodyBytes=len(body), storedSha256=events.sha256(stored), storedBytes=len(stored))
    raw = events.encoded(envelope); name = f'page-1-{events.sha256(raw)}.json'
    obs, _ = native_pages._decode(source, raw, name, {}, native_format='native_json_feed_projected_v1')
    assert len(obs['records']) == 81 and obs['records'][1]['country'] == 'GB'
    assert obs['records'][0]['id'] is None and obs['records'][0]['details']['flag'] is False
    assert obs['context']['unknown'] == {'outside': False, 'empty': ''}
    reconstructed = acquisition.encode_json(native_feed_pages.reconstruct(obs), obs['numericLiterals'])
    assert '1.00000000000000001' in reconstructed
    plan = events.plan(obs, source_key='newmark'); events.validate_plan(plan); events.validate_content_policy(plan)
    assert len(plan['events']) == 82
    assert not {'bodySha256', 'bodyBytes', 'storedSha256', 'storedBytes'} & obs['receipt'].keys()
    wrong = copy.deepcopy(obs)
    wrong['profile'] = 'newmark_nim_lease_post_v1' if profile.endswith('sale_post_v1') else 'newmark_nim_sale_post_v1'
    with pytest.raises(ValueError):
        native_feed_pages.validate(wrong)


@pytest.mark.parametrize('value', [None, False, [], {}, {'data': None}, {'renamed': [False, None]}])
def test_unrecognized_nim_shape_is_unqualified_context(value):
    raw, name, source = entry('newmark_nim_sale_post_v1', value)
    obs, _ = native_feed_pages.decode_entry(raw, name, {}, source=source)
    assert obs['records'] == [] and obs['arrayPointer'] is None
    assert native_feed_pages.reconstruct(obs) == value
    plan = events.plan(obs, source_key=source); events.validate_plan(plan); events.validate_content_policy(plan)


@pytest.mark.parametrize('payload', [{'password': 'dummy'}, {'x': '{"password":"dummy"}'},
                                     {'url': 'https://jll.com/private'}, {'url': 'https://example.test/?token=dummy'}])
def test_unsafe_nim_content_remains_refused(payload):
    raw, name, source = entry('newmark_nim_sale_post_v1', {'data': [payload]})
    obs, _ = native_feed_pages.decode_entry(raw, name, {}, source=source)
    with pytest.raises(ValueError):
        events.validate_content_policy(events.plan(obs, source_key=source))


def test_newmark_page_request_bytes_and_offset_binding():
    profile = 'newmark_nim_sale_post_v1'; raw, _, source = entry(profile, {'data': []})
    envelope = json.loads(raw); envelope['receipt'].update(page=1, offset=100,
        requestSha256=events.sha256(native_feed_pages.request_bytes(profile, 100)))
    raw = events.encoded(envelope); name = f'page-1-{events.sha256(raw)}.json'
    obs, _ = native_feed_pages.decode_entry(raw, name, {}, source=source)
    assert obs['receipt']['page'] == 1 and obs['receipt']['offset'] == 100
    obs['receipt']['offset'] = 50
    with pytest.raises(ValueError):
        native_feed_pages.validate(obs)


def test_newmark_closed_source_and_page_bounds():
    raw, name, source = entry('newmark_nim_sale_post_v1', {'data': []}, sequence=129)
    with pytest.raises(ValueError):
        native_feed_pages.decode_entry(raw, name, {}, source=source)
    raw, name, _ = entry('newmark_nim_sale_post_v1', {'data': []})
    with pytest.raises(ValueError):
        native_feed_pages.decode_entry(raw, name, {}, source='kidder-mathews')
    config = {'max_native_pages': 256, 'native_source_formats': {'newmark': 'native_json_feed_projected_v1'}}
    assert native_pages.page_limit('newmark', config) == 128


def test_nim_settled_observation_replays_unknown_before_new_work(tmp_path, monkeypatch):
    import hashlib

    import cre_checkpoint_refresh as refresh
    from source_evidence import automatic
    from test_automatic import SHA, setup_policy

    config, policy, path = setup_policy(tmp_path, sources=('newmark',), count=100)
    policy.update(schema_version=automatic.SCHEMA_V2, collector_projections={},
                  native_source_formats={'newmark': 'native_json_feed_projected_v1'}, max_native_pages=4)
    raw = events.encoded(policy); path.write_bytes(raw)
    config = automatic.configuration(str(path), hashlib.sha256(raw).hexdigest(), config['env_file'],
                                     config['target_sha256'], SHA, ['newmark'])
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=('newmark',), page_cap=4,
                                    concurrency=1, automatic_source_evidence=config)
    attempt = {'number': 1, 'source_evidence': {'state': 'no_artifact'}}
    manifest['sources']['newmark']['attempts'].append(attempt)
    directory = native_pages.mark(run, manifest, 'newmark', attempt)
    raw, name, _ = entry('newmark_nim_sale_post_v1', {'data': [{'id': None, 'state': i} for i in range(65)]})
    page = directory / name; page.write_bytes(raw); page.chmod(0o600)
    native_pages.settle(run, manifest, 'newmark', attempt, lambda *_: None)
    assert attempt['native_source_evidence']['state'] == 'settled'
    job = attempt['native_source_evidence']['pages'][0]
    plan = native_pages.prepare_job(run, manifest, 'newmark', attempt, job)
    assert len(plan['events']) == 66
    job['state'] = 'outcome_unknown'; automatic.prepare_resume(manifest, replay_unknown=True)
    writes = []; monkeypatch.setattr(automatic.cli, 'run_psql', lambda *a, **kw: writes.append(a))
    automatic.pump(run, manifest, lambda *_: None)
    assert len(writes) == 1 and job['state'] == 'appended'
    page.write_bytes(raw + b' ')
    with pytest.raises(ValueError):
        native_pages.prepare_job(run, manifest, 'newmark', attempt, job)
