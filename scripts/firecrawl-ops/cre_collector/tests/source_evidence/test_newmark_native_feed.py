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


def test_newmark_projects_only_row_document_credential_references():
    secret = 'https://docs.example.com/file.pdf?token=synthetic-secret'
    public = 'https://docs.example.com/file.pdf?page=2'
    values = [secret, public, None, False, '', 17]
    rows = [{'id': index, 'secureDocumentsUrl': values[index % len(values)],
             'nested': {'note': False}, 'other': [None, '', index]}
            for index in range(84)]
    body = {'data': rows, 'unknown': {'ref': public, 'false': False, 'null': None}}
    raw, name, source = entry('newmark_nim_sale_post_v1', body)
    obs, _ = native_feed_pages.decode_entry(raw, name, {}, source=source)
    projected = native_feed_pages.reconstruct(obs)
    witnesses = obs['projection']['withheld']
    assert len(witnesses) == 14
    assert [entry['pointer'] for entry in witnesses] == [f'/data/{i}/secureDocumentsUrl' for i in range(0, 84, 6)]
    assert all(entry['reason'] == 'credential-bearing-document-reference' for entry in witnesses)
    for i, row in enumerate(projected['data']):
        assert row['secureDocumentsUrl'] == (None if i % 6 == 0 else values[i % 6])
        assert row['other'] == [None, '', i]
    assert projected['unknown'] == body['unknown']
    plan = events.plan(obs, source_key=source)
    events.validate_plan(plan); events.validate_content_policy(plan)
    serialized = events.encoded(plan)
    assert b'synthetic-secret' not in serialized
    original_entry = json.loads(raw)
    assert original_entry['receipt']['bodySha256'].encode() not in serialized
    assert original_entry['receipt']['storedSha256'].encode() not in serialized
    assert native_feed_pages.reconstruct(obs) == projected
    for pointer in ['/data/1/secureDocumentsUrl', '/data/0/nested/secureDocumentsUrl',
                    '/data/00/secureDocumentsUrl', '/other/0/secureDocumentsUrl']:
        forged = copy.deepcopy(obs)
        forged['projection']['withheld'][0]['pointer'] = pointer
        with pytest.raises((ValueError, IndexError, KeyError, TypeError)):
            native_feed_pages.validate(forged)


@pytest.mark.parametrize('row', [
    {'secureDocumentUrl': 'https://docs.example.com/file?token=synthetic-secret'},
    {'nested': {'secureDocumentsUrl': 'https://docs.example.com/file?token=synthetic-secret'}},
    {'secureDocumentsUrl': 'https://docs.example.com/file', 'other': 'https://docs.example.com/file?token=synthetic-secret'},
])
def test_unknown_credential_locations_still_refuse(row):
    raw, name, source = entry('newmark_nim_sale_post_v1', {'data': [row]})
    obs, _ = native_feed_pages.decode_entry(raw, name, {}, source=source)
    with pytest.raises(ValueError):
        events.validate_content_policy(events.plan(obs, source_key=source))


def test_newmark_exceptional_number_at_document_reference_is_preserved():
    body = b'{"data":[{"secureDocumentsUrl":1.00000000000000001}]}'
    raw, _, source = entry('newmark_nim_sale_post_v1', {'data': []})
    envelope = json.loads(raw); stored = gzip.compress(body)
    envelope['responseBodyBase64'] = base64.b64encode(stored).decode()
    envelope['receipt'].update(bodySha256=events.sha256(body), bodyBytes=len(body),
                               storedSha256=events.sha256(stored), storedBytes=len(stored))
    raw = events.encoded(envelope); name = f'page-1-{events.sha256(raw)}.json'
    obs, _ = native_feed_pages.decode_entry(raw, name, {}, source=source)
    assert obs['projection']['withheld'] == []
    assert '1.00000000000000001' in acquisition.encode_json(native_feed_pages.reconstruct(obs), obs['numericLiterals'])
    events.validate_content_policy(events.plan(obs, source_key=source))


@pytest.mark.parametrize('value', [
    'See https://docs.example/file?token=synthetic-secret for details',
    ' https://docs.example/file?token=synthetic-secret',
    'https://docs.example/file?token=synthetic-secret ',
    'https://docs.example/file?token=synthetic-secret\n',
    'https://docs.example:bad/file?token=synthetic-secret',
    'https://docs.example:65536/file?token=synthetic-secret',
])
def test_malformed_or_mixed_reference_remains_for_scanner(value):
    raw, name, source = entry('newmark_nim_sale_post_v1', {'data': [{'secureDocumentsUrl': value}]})
    obs, _ = native_feed_pages.decode_entry(raw, name, {}, source=source)
    assert obs['projection']['withheld'] == []
    assert native_feed_pages.reconstruct(obs)['data'][0]['secureDocumentsUrl'] == value
    with pytest.raises(ValueError):
        events.validate_content_policy(events.plan(obs, source_key=source))


@pytest.mark.parametrize('value', [
    'http://docs.example:8080/file?token=synthetic-secret',
    'https://docs.example:443/file?token=synthetic-secret',
    'https://user:password@docs.example/file',
])
def test_exact_credential_uri_is_withheld(value):
    raw, name, source = entry('newmark_nim_sale_post_v1', {'data': [{'secureDocumentsUrl': value}]})
    obs, _ = native_feed_pages.decode_entry(raw, name, {}, source=source)
    assert native_feed_pages.reconstruct(obs)['data'][0]['secureDocumentsUrl'] is None
    assert obs['projection']['withheld'][0]['pointer'] == '/data/0/secureDocumentsUrl'
    events.validate_content_policy(events.plan(obs, source_key=source))
