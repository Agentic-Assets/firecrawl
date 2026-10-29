import base64
import copy
import gzip
import json

import pytest
from source_evidence import acquisition, events, native_feed_pages, native_pages


def entry(profile, value, *, variant=None, offset=0, sequence=1):
    source = native_feed_pages.PROFILES[profile]; hanley = source == 'hanley'
    variant = variant or ('direct' if hanley else 'strict' if profile == 'kidder_bulk_post_v1' else 'standard')
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    stored = gzip.compress(raw)
    request = native_feed_pages.request_bytes(profile, offset)
    receipt = {'sourceKey': source, 'requestVariant': variant, 'requestSha256': events.sha256(request) if request else None,
               'sequence': sequence, 'page': None if hanley else offset // (12 if source == 'srs' else 50),
               'offset': None if hanley else offset, 'status': 200, 'capturedAt': '2026-10-28T00:00:00Z',
               'bodySha256': events.sha256(raw), 'bodyBytes': len(raw), 'storedSha256': events.sha256(stored), 'storedBytes': len(stored)}
    provenance = {'representation': 'collector_decoded_html_embedded_json_literal_not_wire' if hanley else 'collector_decoded_utf8_text_not_wire',
                  'utf8Decoding': 'collector_string_encoding_unobserved' if hanley else 'fatal_utf8' if profile == 'kidder_bulk_post_v1' else 'replacement_possible',
                  'httpStatusOrigin': 'direct_response', 'finalUrl': 'unobserved', 'sourceFreshness': 'unverified',
                  'identity': 'unverified', 'visibility': 'unverified', 'coverage': 'unverified'}
    data = {'schema': 'cre-native-json-feed-page/v1', 'profile': profile, 'receipt': receipt, 'storageCodec': 'gzip_base64',
            'responseBodyBase64': base64.b64encode(stored).decode(), 'provenance': provenance}
    encoded = events.encoded(data); return encoded, f'page-{sequence}-{events.sha256(encoded)}.json', source


def test_eighty_public_rows_with_private_and_unknown_rows_roundtrip_through_events():
    values = [{'id': None if i % 2 else 'duplicate', 'visibility': 'Public', 'unknown': [False, None, '', i]} for i in range(80)]
    values.extend([{'visibility': 'Private', 'secret': 'not-public'}, {'visibility': None, 'hidden': 'not-public'}])
    raw, filename, source = entry('hanley_embedded_array_v1', values)
    result, diagnostic = native_feed_pages.decode_entry(raw, filename, {}, source=source)
    assert diagnostic is None and result['records'][-2:] == [None, None]
    assert 'not-public' not in events.encoded(result).decode()
    assert not {'bodySha256', 'storedSha256', 'bodyBytes', 'storedBytes'} & result['receipt'].keys()
    plan = events.plan(result, source_key=source); events.validate_content_policy(plan); events.validate_plan(plan)
    assert native_feed_pages.reconstruct(result) == values[:80] + [None, None]


def test_decoder_retains_srs_controlled_value_and_context_without_promoting():
    values = {'properties': [{'apto_data': {'Hide_Sale_Price__c': True, 'Sale_Price__c': 99}},
                             {'apto_data': {'Hide_Sale_Price__c': None, 'Sale_Price__c': 123}, 'other': False},
                             {'unknown': [None, '', False]}], 'total': 3, 'summary': 'price context'}
    raw, filename, source = entry('srs_search_post_v1', values)
    result, _ = native_pages._decode(source, raw, filename, {}, native_format='native_json_feed_projected_v1')
    assert native_feed_pages.reconstruct(result) == values
    assert result['projection']['withheld'] == []
    assert events.validate_content_policy(events.plan(result, source_key=source)) is None
    with pytest.raises(ValueError):
        native_feed_pages.decode_entry(raw, filename.replace('page-1-', 'page-2-'), {}, source=source)
    tampered = copy.deepcopy(result); tampered['receipt']['requestSha256'] = '0' * 64
    with pytest.raises(ValueError):
        native_feed_pages.validate(tampered)


def test_kidder_escaped_credential_json_is_refused():
    raw, filename, source = entry('kidder_bulk_post_v1', {'results': [{'nested': '{"password":"hidden"}'}]})
    result, _ = native_feed_pages.decode_entry(raw, filename, {}, source=source)
    with pytest.raises(ValueError):
        events.validate_content_policy(events.plan(result, source_key=source))


def test_precise_numeric_witnesses_survive_and_private_ones_disappear():
    raw, filename, source = entry('hanley_embedded_array_v1', [{'visibility': 'Public', 'x': 1}, {'visibility': 'Private', 'x': 2}])
    # Re-encode a feed with precise literals through the same private envelope constructor.
    original = json.loads(raw); body = b'[{"visibility":"Public","x":1.0000000000000001},{"visibility":"Private","x":99999999999999999999999}]'
    stored = gzip.compress(body); original['responseBodyBase64'] = base64.b64encode(stored).decode()
    original['receipt'].update(bodySha256=events.sha256(body), bodyBytes=len(body), storedSha256=events.sha256(stored), storedBytes=len(stored))
    raw = events.encoded(original); filename = f'page-1-{events.sha256(raw)}.json'
    result, _ = native_feed_pages.decode_entry(raw, filename, {}, source=source)
    encoded = acquisition.encode_json(native_feed_pages.reconstruct(result), result['numericLiterals'])
    assert '1.0000000000000001' in encoded and '99999999999999999999999' not in encoded


def test_settled_capture_unknown_append_replays_exactly(tmp_path, monkeypatch):
    import hashlib

    import cre_checkpoint_refresh as refresh
    from source_evidence import automatic
    from test_automatic import SHA, setup_policy

    config, policy, path = setup_policy(tmp_path, sources=('hanley',), count=100)
    policy.update(schema_version=automatic.SCHEMA_V2, collector_projections={},
                  native_source_formats={'hanley': 'native_json_feed_projected_v1'}, max_native_pages=4)
    encoded = events.encoded(policy); path.write_bytes(encoded)
    config = automatic.configuration(str(path), hashlib.sha256(encoded).hexdigest(), config['env_file'],
                                     config['target_sha256'], SHA, ['hanley'])
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=('hanley',), page_cap=4,
                                    concurrency=1, automatic_source_evidence=config)
    attempt = {'number': 1, 'source_evidence': {'state': 'no_artifact'}}
    manifest['sources']['hanley']['attempts'].append(attempt)
    directory = native_pages.mark(run, manifest, 'hanley', attempt)
    raw, filename, _ = entry('hanley_embedded_array_v1', [{'visibility': 'Public', 'id': i} for i in range(80)])
    page = directory / filename; page.write_bytes(raw); page.chmod(0o600)
    native_pages.settle(run, manifest, 'hanley', attempt, lambda *_: None)
    state = attempt['native_source_evidence']; assert state['state'] == 'settled'
    job = state['pages'][0]; assert job['state'] == 'pending'
    expected = job['plan_sha256']; native_pages.prepare_job(run, manifest, 'hanley', attempt, job)
    job['state'] = 'outcome_unknown'
    automatic.prepare_resume(manifest, replay_unknown=True)
    calls = []; monkeypatch.setattr(automatic.cli, 'run_psql', lambda *args, **kwargs: calls.append(args))
    automatic.pump(run, manifest, lambda *_: None)
    assert len(calls) == 1 and job['state'] == 'appended' and job['plan_sha256'] == expected, job


def test_unknown_and_malformed_private_payloads_fail_closed():
    raw, _filename, source = entry('kidder_paged_post_v1', {'results': []})
    for mutation in ('bad_gzip', 'wrong_request', 'unknown_profile'):
        data = json.loads(raw)
        if mutation == 'bad_gzip':
            stored = b'not-gzip'; data['responseBodyBase64'] = base64.b64encode(stored).decode()
            data['receipt'].update(storedSha256=events.sha256(stored), storedBytes=len(stored))
        elif mutation == 'wrong_request':
            data['receipt']['requestSha256'] = '0' * 64
        else:
            data['profile'] = 'unregistered'
        changed = events.encoded(data); changed_name = f'page-1-{events.sha256(changed)}.json'
        with pytest.raises(ValueError):
            native_feed_pages.decode_entry(changed, changed_name, {}, source=source)


def test_nested_signed_url_is_refused():
    raw, filename, source = entry('kidder_bulk_post_v1', {'results': [{'data': '{"url":"https://example.test/?token=secret"}'}]})
    result, _ = native_feed_pages.decode_entry(raw, filename, {}, source=source)
    with pytest.raises(ValueError):
        events.validate_content_policy(events.plan(result, source_key=source))



@pytest.mark.parametrize('source,profile,seq,allowed', [
    ('srs', 'srs_search_post_v1', 128, True), ('srs', 'srs_search_post_v1', 129, True),
    ('srs', 'srs_search_post_v1', 256, True), ('srs', 'srs_search_post_v1', 257, False),
    ('kidder-mathews', 'kidder_bulk_post_v1', 128, True), ('kidder-mathews', 'kidder_bulk_post_v1', 129, False),
    ('hanley', 'hanley_embedded_array_v1', 128, True), ('hanley', 'hanley_embedded_array_v1', 129, False),
])
def test_feed_sequence_source_caps(source, profile, seq, allowed):
    content = [{'visibility': 'Public'}] if source == 'hanley' else {'properties' if source == 'srs' else 'results': []}
    raw, name, _ = entry(profile, content, sequence=seq)
    if allowed:
        value, _ = native_feed_pages.decode_entry(raw, name, {}, source=source)
        native_feed_pages.validate(value)
    else:
        with pytest.raises(ValueError):
            native_feed_pages.decode_entry(raw, name, {}, source=source)


def test_mixed_policy_srs_256_caps_other_workers(tmp_path):
    import hashlib

    from source_evidence import automatic
    from test_automatic import SHA, setup_policy
    config, policy, path = setup_policy(tmp_path, sources=('srs', 'hanley'), count=100)
    policy.update(schema_version=automatic.SCHEMA_V2, collector_projections={},
                  native_source_formats={'srs': 'native_json_feed_projected_v1', 'hanley': 'native_json_feed_projected_v1'},
                  max_native_pages=256)
    encoded = events.encoded(policy); path.write_bytes(encoded)
    configured = automatic.configuration(str(path), hashlib.sha256(encoded).hexdigest(), config['env_file'],
                                         config['target_sha256'], SHA, ['srs', 'hanley'])
    assert native_pages.page_limit('srs', configured) == 256
    assert native_pages.page_limit('hanley', configured) == 128
    for source, expected in [('srs', '256'), ('hanley', '128')]:
        env = {}; attempt = {'number': 1, 'native_source_evidence': {'directory': f'source-evidence/native-{source}-attempt-1'}}
        native_pages.configure_worker_env(env, tmp_path, source, attempt, configured)
        assert env['CRE_NATIVE_JSON_FEED_CAPTURE_MAX_PAGES'] == expected
    policy['native_source_formats'].pop('srs'); path.write_bytes(events.encoded(policy))
    with pytest.raises(ValueError):
        automatic.configuration(str(path), events.sha256(events.encoded(policy)), config['env_file'],
                                config['target_sha256'], SHA, ['srs', 'hanley'])


def test_srs_sequence_must_fit_attempt_policy_even_when_filename_count_is_one(tmp_path):
    import hashlib

    import cre_checkpoint_refresh as refresh
    from source_evidence import automatic
    from test_automatic import SHA, setup_policy
    config, policy, path = setup_policy(tmp_path, sources=('srs',), count=100)
    policy.update(schema_version=automatic.SCHEMA_V2, collector_projections={},
                  native_source_formats={'srs': 'native_json_feed_projected_v1'}, max_native_pages=128)
    encoded = events.encoded(policy); path.write_bytes(encoded)
    configured = automatic.configuration(str(path), hashlib.sha256(encoded).hexdigest(), config['env_file'],
                                         config['target_sha256'], SHA, ['srs'])
    run = tmp_path / 'run'; run.mkdir(mode=0o700)
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=('srs',), page_cap=4,
                                    concurrency=1, automatic_source_evidence=configured)
    attempt = {'number': 1, 'source_evidence': {'state': 'no_artifact'}}
    manifest['sources']['srs']['attempts'].append(attempt)
    directory = native_pages.mark(run, manifest, 'srs', attempt)
    raw, filename, _ = entry('srs_search_post_v1', {'properties': []}, sequence=129)
    page = directory / filename; page.write_bytes(raw); page.chmod(0o600)
    native_pages.settle(run, manifest, 'srs', attempt, lambda *_: None)
    assert attempt['native_source_evidence']['state'] == 'refused'
    assert manifest['automatic_source_evidence_paused'] is True
    assert page.read_bytes() == raw


@pytest.mark.parametrize('profile,source', [('srs_search_post_v1', 'srs'), ('kidder_paged_post_v1', 'kidder-mathews')])
@pytest.mark.parametrize('value', [None, False, 0, '', [], {}, {'properties': None, 'results': None},
                                   {'renamed': [{'id': None, 'unknown': [False, '', None]}]}])
def test_unknown_response_shapes_remain_whole_unqualified_context(profile, source, value):
    raw, name, _ = entry(profile, value)
    observation, diagnostic = native_feed_pages.decode_entry(raw, name, {}, source=source)
    assert diagnostic is None and observation['arrayPointer'] is None and observation['records'] == []
    assert native_feed_pages.reconstruct(observation) == value
    native_feed_pages.validate(observation)
    plan = events.plan(observation, source_key=source)
    events.validate_content_policy(plan); events.validate_plan(plan)
    assert len(plan['events']) == 1
    changed = copy.deepcopy(observation); changed['records'] = [{'fake': 'row'}]
    with pytest.raises(ValueError):
        native_feed_pages.validate(changed)


@pytest.mark.parametrize('profile,source', [('srs_search_post_v1', 'srs'), ('kidder_paged_post_v1', 'kidder-mathews')])
def test_whole_unqualified_context_retains_numeric_and_scans_escaped_credentials(profile, source):
    raw, name, _ = entry(profile, {'unknown': 'safe'})
    data = json.loads(raw)
    body = b'{"renamed":[1.00000000000000001,null,false]}'
    stored = gzip.compress(body); data['responseBodyBase64'] = base64.b64encode(stored).decode()
    data['receipt'].update(bodySha256=events.sha256(body), bodyBytes=len(body), storedSha256=events.sha256(stored), storedBytes=len(stored))
    raw = events.encoded(data); name = f'page-1-{events.sha256(raw)}.json'
    observation, _ = native_feed_pages.decode_entry(raw, name, {}, source=source)
    assert '1.00000000000000001' in acquisition.encode_json(native_feed_pages.reconstruct(observation), observation['numericLiterals'])
    plan = events.plan(observation, source_key=source); events.validate_plan(plan); events.validate_content_policy(plan)
    raw, name, _ = entry(profile, {'unknown': '{"password":"dummy"}'})
    observation, _ = native_feed_pages.decode_entry(raw, name, {}, source=source)
    with pytest.raises(ValueError):
        events.validate_content_policy(events.plan(observation, source_key=source))
