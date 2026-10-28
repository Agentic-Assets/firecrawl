import base64
import hashlib
import json

import cre_checkpoint_refresh as refresh
import pytest
from source_evidence import (
    automatic,
    avison_pages,
    avison_response,
    events,
    native_pages,
)
from test_automatic import SHA, setup_policy


def entry(body):
    value = {"schema": "cre-avison-sharplaunch-website-page/v1",
             "receipt": {"sourceKey": "avison-young", "requestUrl": avison_response.REQUEST_URL,
                         "method": "GET", "httpOutcome": "success_status_unobserved",
                         "startedAt": "2026-10-28T10:00:00.000Z", "finishedAt": "2026-10-28T10:00:01.000Z"},
             "responseBodyBase64": base64.b64encode(body).decode(), "provenance": avison_pages.PROVENANCE}
    raw = events.encoded(value) + b'\n'
    return raw, f"page-website-{hashlib.sha256(raw).hexdigest()}.json"


def test_projected_native_keeps_unknown_addressless_duplicates_and_exact_numbers():
    body = b'{"unknown_envelope":[null,"context"],"items":[{"id":7,"unknown":0.10000000000000000001},{"id":7},null,4]}'
    raw, name = entry(body)
    observation, reason = avison_pages.decode_entry(raw, name, {})
    assert reason is None and len(observation['records']) == 4
    assert observation['numericLiterals'] == {'/items/0/unknown': '0.10000000000000000001'}
    assert avison_response.reconstruct(observation)['unknown_envelope'] == [None, 'context']
    plan = events.plan(observation, source_key='avison-young')
    events.validate_content_policy(plan)
    assert len(plan['events']) == 5 and all(row['external_id'] is None for row in plan['events'])
    assert plan['events'][0]['facts'][0]['kind'] == 'avison_native_context'
    assert 'status' not in observation['receipt'] and 'finalUrl' not in observation['receipt']


def test_removed_value_and_raw_hash_do_not_define_database_capture_identity():
    plans = []
    for amount in (123456, 987654):
        body = events.encoded({'items': [{'hide_price': 1, 'sale_price': amount, 'noi': 10000,
                                        'description': f'Useful details. Asking price ${amount}. NOI $10000.'}]})
        raw, name = entry(body)
        observation, _ = avison_pages.decode_entry(raw, name, {})
        plan = events.plan(observation, source_key='avison-young')
        events.validate_content_policy(plan)
        assert str(amount).encode() not in events.encoded(plan)
        assert hashlib.sha256(raw).hexdigest().encode() not in events.encoded(plan)
        assert hashlib.sha256(body).hexdigest().encode() not in events.encoded(plan)
        assert observation['records'][0]['noi'] == 10000
        assert 'NOI $10000.' in observation['records'][0]['description']
        plans.append(plan)
    assert plans[0] == plans[1]


@pytest.mark.parametrize('body', [b'{"error":"partial","unknown":0.10000000000000000001}',
                                  b'[null,{"hide_price":1,"sale_price":123456,"noi":9000}]'])
def test_unmapped_accessible_json_is_saved_as_context_without_listing_claim(body):
    raw, name = entry(body)
    observation, reason = avison_pages.decode_entry(raw, name, {})
    assert reason is None and observation['records'] == []
    assert observation['responseEncoding'] == 'wrapped_unmapped_json'
    assert 'unmappedResponse' in avison_response.reconstruct(observation)
    plan = events.plan(observation, source_key='avison-young')
    events.validate_content_policy(plan)
    assert len(plan['events']) == 1
    assert b'123456' not in events.encoded(plan)


@pytest.mark.parametrize('mutate', [
    lambda obj: obj['receipt'].update(status=200),
    lambda obj: obj['receipt'].update(finalUrl=avison_response.REQUEST_URL),
    lambda obj: obj['receipt'].update(request_credential='do-not-save'),
    lambda obj: obj['receipt'].update(httpOutcome='authenticated'),
    lambda obj: obj['receipt'].update(startedAt='2026-10-29T10:00:00Z'),
    lambda obj: obj.update(provenance={}),
])
def test_native_receipt_refuses_unobserved_or_sensitive_metadata(mutate):
    raw, _ = entry(b'{"items":[]}')
    value = json.loads(raw)
    mutate(value)
    changed = events.encoded(value)
    with pytest.raises(ValueError):
        avison_pages.decode_entry(changed, f'page-website-{hashlib.sha256(changed).hexdigest()}.json', {})


def test_projection_binding_and_plan_tamper_are_refused():
    raw, name = entry(b'{"items":[{"hide_price":1,"sale_price":123456}]}')
    with pytest.raises(ValueError):
        avison_pages.decode_entry(raw, name, {'originalBodySha256': 'a' * 64})
    observation, _ = avison_pages.decode_entry(raw, name, {})
    observation['projection']['changes'][0]['originalValue'] = 123456
    with pytest.raises(ValueError):
        events.plan(observation, source_key='avison-young')
    original, _ = avison_pages.decode_entry(raw, name, {})
    original['records'][0]['sale_price'] = 123456
    with pytest.raises(ValueError):
        events.plan(original, source_key='avison-young')


def test_sealed_plan_and_jsonb_key_reordering_preserve_projected_hash():
    body = b'{"z_context":1,"items":[{"z":1,"a":2,"unknown":0.10000000000000000001}],"a_context":2}'
    raw, name = entry(body)
    observation, _ = avison_pages.decode_entry(raw, name, {})
    plan = events.plan(observation, source_key='avison-young')
    sealed = json.loads(events.encoded(plan))
    events.validate_plan(sealed)
    events.validate_content_policy(sealed)
    assert sealed == plan


def test_avison_settled_capture_survives_canonical_failure_and_replays(tmp_path, monkeypatch):
    config, policy, path = setup_policy(tmp_path, sources=('avison-young',), count=100)
    policy.update(schema_version=automatic.SCHEMA_V2, collector_projections={},
                  native_source_formats={'avison-young': avison_pages.FORMAT}, max_native_pages=4)
    path.write_bytes(events.encoded(policy))
    config = automatic.configuration(str(path), events.sha256(path.read_bytes()), config['env_file'],
                                     config['target_sha256'], SHA, ['avison-young'])
    run = tmp_path / 'native-avison'
    run.mkdir(mode=0o700)
    manifest = refresh.new_manifest(run, git_sha=SHA, git_dirty=False, sources=('avison-young',),
                                    page_cap=4, concurrency=1, automatic_source_evidence=config)
    attempt = {'number': 1, 'source_evidence': {'state': 'no_artifact'}}
    manifest['sources']['avison-young']['attempts'].append(attempt)
    directory = native_pages.mark(run, manifest, 'avison-young', attempt)
    body = json.dumps({'z_context': 1, 'items': [{'z_value': i, 'id': i, 'unknown': {'array': [None, i, i]}}
                                               for i in range(78)], 'a_context': 2}).encode()
    raw, name = entry(body)
    (directory / name).write_bytes(raw)
    (directory / name).chmod(0o600)
    native_pages.settle(run, manifest, 'avison-young', attempt, lambda *_: None)
    state = attempt['native_source_evidence']
    assert state['state'] == 'settled' and len(state['pages']) == 1
    plan = native_pages.prepare_job(run, manifest, 'avison-young', attempt, state['pages'][0])
    assert len(plan['events']) == 79
    calls = []
    monkeypatch.setattr(automatic.cli, 'run_psql', lambda *a, **k: calls.append(a))
    automatic.pump(run, manifest, lambda *_: None)
    assert len(calls) == 1 and state['pages'][0]['state'] == 'appended'
    assert attempt['source_evidence']['state'] == 'no_artifact'
    # Raw-page integrity still matters privately, while its hash stays out of DB facts.
    (directory / name).write_bytes(raw + b' ')
    with pytest.raises(ValueError):
        native_pages.prepare_job(run, manifest, 'avison-young', attempt, state['pages'][0])


def test_capture_env_is_source_scoped_and_disabled_inherited_optins_are_cleared(tmp_path):
    env = {prefix + '_' + suffix: 'foreign' for prefix in native_pages.ENV_PREFIXES.values()
           for suffix in ('DIR', 'MAX_PAGES', 'MAX_BYTES', 'SINGLE_WRITER')}
    native_pages.configure_worker_env(env, tmp_path, 'avison-young', {}, {})
    assert env == {}
    attempt = {'number': 1, 'native_source_evidence': {'directory': 'source-evidence/native-avison-young-attempt-1'}}
    native_pages.configure_worker_env(env, tmp_path, 'avison-young', attempt,
                                      {'native_source_formats': {'avison-young': avison_pages.FORMAT},
                                       'max_native_pages': 4})
    assert env['CRE_AVISON_NATIVE_CAPTURE_SINGLE_WRITER'] == '1'
    assert not any(key.startswith('CRE_NAI_') for key in env)
    attempt['native_source_evidence']['directory'] = '../foreign'
    with pytest.raises(ValueError):
        native_pages.configure_worker_env(env, tmp_path, 'avison-young', attempt,
                                          {'native_source_formats': {'avison-young': avison_pages.FORMAT},
                                           'max_native_pages': 4})
