"""Synthetic private RCM observation projection; no source or DB access."""
import copy
import json

import pytest

from source_evidence import collector, collector_projection, events
from source_evidence.projections import newmark_rcm


def artifact(count=1):
    literal = '{"bingMapKey":"secret-key-one","captchaKey":"secret-key-two","translation":{"languages":[{"TransPerfectAPIKey":"secret-key-three","name":"English"}],"selected":{"TransPerfectAPIKey":"secret-key-four"}},"number":1.00000000000000001,"other":[null,false,""]}'
    rows = [{'newmarkRcmObservation':True, 'claim':{'sourceKey':'newmark'},
             'sourceObservation':{'schema':newmark_rcm.SCHEMA,'literal':literal,'parsed':json.loads(literal),
                                  'parsedSerializable':True,'numericFidelity':'literal_only', 'identity':'unverified', 'visibility':'unverified',
                                  'sourceClock':None, 'sourceClockStatus':'unverified', 'extraction':'observed',
                                  'match':'unresolved', 'matchMethod':'strict_json_visitor_source_candidate'},
             'scrapedDoc':{'rawHtml':'<script>var data = '+literal+'</script>'},
             'pageContext':{'scripts':[], 'tables':{'provenance':{'inputSha256':'a'*64}}}}
            for _ in range(count)]
    return {'runMeta':{'mode':'enrich','finishedAt':'2026-10-29T12:00:00Z'},
            'sources':[{'sourceKey':'newmark','retainedObservations':rows}], 'listings':[], 'brokers':[], 'totalListings':0}


def test_projection_preserves_65_rows_and_exact_numeric_literals():
    original = artifact(65); raw = events.encoded(original)
    projected, literals, receipt = collector_projection.prepare(raw, source_key='newmark', version=newmark_rcm.VERSION)
    assert original['sources'][0]['retainedObservations'][0]['sourceObservation']['literal'].find('secret-key-one') >= 0
    assert len(receipt['withheld']) == 65 * 6
    rows = projected['sources'][0]['retainedObservations']
    assert len(rows) == 65
    for row in rows:
        assert '<script>var data = ' in row['scrapedDoc']['rawHtml']
        assert 'secret-key' not in row['scrapedDoc']['rawHtml']
        source = row['sourceObservation']
        assert '1.00000000000000001' in source['literal']
        assert source['parsed']['other'] == [None,False,'']
        assert source['projectedNumericLiterals']['/number'] == '1.00000000000000001'
        assert row['pageContext']['tables']['provenance']['inputSha256'] is None
    collector_projection.validate(projected,receipt,source_key='newmark',numeric_literals=literals)
    observation = collector.convert(projected,source_key='newmark',numeric_literals=literals,projection=receipt)
    plan = events.plan(observation,source_key='newmark'); events.validate_content_policy(plan)
    assert collector.reconstruct(observation) == projected


def test_unknown_sensitive_copy_and_missing_literal_refuse():
    value = artifact(); value['sources'][0]['retainedObservations'][0]['unexpected'] = 'secret-key-one'
    with pytest.raises(ValueError): newmark_rcm.project(value,source_key='newmark')
    value = artifact(); del value['sources'][0]['retainedObservations'][0]['sourceObservation']
    projected, _, receipt = newmark_rcm.project(value,source_key='newmark')
    assert projected['sources'][0]['retainedObservations'][0]['scrapedDoc']['rawHtml'] is None
    assert any(x['reason'] == 'unverified_content_omitted' for x in receipt['withheld'])


def test_bad_projection_receipt_rejects():
    projected, literals, receipt = newmark_rcm.prepare(events.encoded(artifact()),source_key='newmark')
    broken = copy.deepcopy(projected); broken['sources'][0]['retainedObservations'][0]['claim']['sourceKey']='other'
    with pytest.raises(ValueError): newmark_rcm.validate(broken,receipt,source_key='newmark',numeric_literals=literals)


def test_forged_receipt_path_and_unprojected_html_refused():
    projected, literals, receipt = newmark_rcm.prepare(events.encoded(artifact()),source_key='newmark')
    forged = copy.deepcopy(receipt); forged['withheld'][0]['pointer'] += '/unrelated'
    with pytest.raises(ValueError): newmark_rcm.validate(projected,forged,source_key='newmark',numeric_literals=literals)
    mixed = copy.deepcopy(projected)
    mixed['sources'][0]['retainedObservations'][0]['scrapedDoc']['rawHtml'] += '<script>var data = {"other":"bad"}</script>'
    observed = collector.convert(mixed,source_key='newmark')
    with pytest.raises(ValueError): events.validate_content_policy(events.plan(observed,source_key='newmark'))


def test_missing_marker_literal_with_html_refused_in_event_plan():
    value = artifact(); del value['sources'][0]['retainedObservations'][0]['sourceObservation']
    observed = collector.convert(value,source_key='newmark')
    with pytest.raises(ValueError): events.validate_content_policy(events.plan(observed,source_key='newmark'))


def test_encoded_json_credentials_and_duplicate_literal_refuse():
    value = artifact()
    row = value['sources'][0]['retainedObservations'][0]
    row['sourceObservation']['literal'] = '{"unknown":"{\\"apiKey\\":\\"dummy\\"}"}'
    row['scrapedDoc']['rawHtml'] = '<script>var data = '+row['sourceObservation']['literal']+'</script>'
    row['sourceObservation']['parsed'] = json.loads(row['sourceObservation']['literal'])
    projected, literals, receipt = newmark_rcm.prepare(events.encoded(value),source_key='newmark')
    observed = collector.convert(projected,source_key='newmark',numeric_literals=literals,projection=receipt)
    with pytest.raises(ValueError): events.validate_content_policy(events.plan(observed,source_key='newmark'))
    row['sourceObservation']['literal'] = '{"x":1,"x":2}'
    projected, _, receipt = newmark_rcm.prepare(events.encoded(value),source_key='newmark')
    assert projected['sources'][0]['retainedObservations'][0]['sourceObservation']['literal'] is None
    assert any(x['reason'] == 'unverified_content_omitted' for x in receipt['withheld'])


def test_truncated_script_is_omitted_and_other_context_retained():
    value = artifact(); row = value['sources'][0]['retainedObservations'][0]
    row['pageContext']['scripts'] = [{'text':'{"TransPerfectAPIKey":"secret-', 'textComplete':False}, {'text':'benign','textComplete':True}]
    projected, literals, receipt = newmark_rcm.prepare(events.encoded(value),source_key='newmark')
    scripts = projected['sources'][0]['retainedObservations'][0]['pageContext']['scripts']
    assert scripts[0]['text'] is None and scripts[1]['text'] == 'benign'
    assert any(x['reason']=='contaminated_text_omitted' and x['pointer'].endswith('/scripts/0/text') for x in receipt['withheld'])
    newmark_rcm.validate(projected,receipt,source_key='newmark',numeric_literals=literals)


def test_normal_seal_projects_and_unprojected_key_refuses(tmp_path):
    import hashlib

    from test_automatic import SHA, setup_policy

    from source_evidence import automatic

    config, policy, path = setup_policy(tmp_path, sources=('newmark',), count=100)
    policy.update(schema_version=automatic.SCHEMA_V4, collector_projections={'newmark':newmark_rcm.VERSION},
                  native_source_formats={}, max_native_pages=128, document_text_formats={},
                  enrichment_detail_formats={'newmark': automatic.NEWMARK_DETAIL_FORMAT})
    raw = events.encoded(policy); path.write_bytes(raw)
    config = automatic.configuration(str(path), hashlib.sha256(raw).hexdigest(), config['env_file'],
                                     config['target_sha256'], SHA, ['newmark'])
    run = tmp_path/'run'; run.mkdir(mode=0o700)
    source = run/'capture.json'; source.write_bytes(events.encoded(artifact(1))); source.chmod(0o600)
    attempt = {'number':1}
    manifest = {'run_id':'run','collector_git_sha':SHA,'collector_git_dirty':False,
                'config':{'sources':['newmark'],'automatic_source_evidence':config},
                'sources':{'newmark':{'attempts':[attempt]}}}
    automatic.seal(run, manifest, 'newmark', attempt, source)
    assert attempt['source_evidence']['state'] == 'pending'
    plan = json.loads((run/attempt['source_evidence']['path']).read_bytes())
    assert b'secret-key-one' not in events.encoded(plan)
    raw_obs = collector.convert(artifact(), source_key='newmark')
    with pytest.raises(ValueError): events.validate_content_policy(events.plan(raw_obs, source_key='newmark'))


def test_escaped_nested_copy_of_removed_secret_refuses():
    value = artifact()
    value['sources'][0]['retainedObservations'][0]['unexpected'] = '{"copy":"secret\\u002dkey\\u002done"}'
    with pytest.raises(ValueError): newmark_rcm.project(value,source_key='newmark')

@pytest.mark.parametrize('encoding', ['key', 'html', 'percent', 'base64', 'unicode', 'jshex'])
def test_removed_secret_copies_in_keys_or_encoded_fields_are_refused(encoding):
    import base64
    value = artifact(); row = value['sources'][0]['retainedObservations'][0]
    secret = 'OpaqueX9zQUjd4'
    source = row['sourceObservation']
    source['literal'] = source['literal'].replace('secret-key-one', secret)
    source['parsed']['bingMapKey'] = secret
    row['scrapedDoc']['rawHtml'] = row['scrapedDoc']['rawHtml'].replace('secret-key-one', secret)
    if encoding == 'key': row['metadata_copy'] = {secret: 'ordinary'}
    elif encoding == 'html': row['metadata_copy'] = ''.join(f'&#{ord(c)};' for c in secret)
    elif encoding == 'percent': row['metadata_copy'] = 'https://example.test/?foo=' + ''.join(f'%{ord(c):02X}' for c in secret)
    elif encoding == 'base64': row['metadata_copy'] = 'data:text/plain;base64,' + base64.b64encode(secret.encode()).decode()
    elif encoding == 'unicode': row['metadata_copy'] = 'prefix ' + ''.join(f'\\u{ord(c):04x}' for c in secret)
    else: row['metadata_copy'] = 'prefix ' + ''.join(f'\\x{ord(c):02x}' for c in secret)
    with pytest.raises(ValueError, match='credential copy'):
        newmark_rcm.prepare(events.encoded(value), source_key='newmark')


def test_source_schema_without_marker_refuses_projection_and_unprojected_plan():
    value = artifact(); row = value['sources'][0]['retainedObservations'][0]
    del row['newmarkRcmObservation']
    row['sourceObservation']['literal'] = '{"bingMapKey":"OpaqueQ17b"}'
    row['sourceObservation']['parsed'] = None
    row['scrapedDoc'] = {}; row['pageContext'] = None
    with pytest.raises(ValueError, match='marker missing'):
        newmark_rcm.prepare(events.encoded(value), source_key='newmark')
    observed = collector.convert(value, source_key='newmark')
    with pytest.raises(ValueError, match='marker missing'):
        events.validate_content_policy(events.plan(observed, source_key='newmark'))


@pytest.mark.parametrize('as_string', [False, True])
def test_unreviewed_same_key_outside_source_literal_refuses(as_string):
    value = artifact(); row = value['sources'][0]['retainedObservations'][0]
    outside = {'bingMapKey': 'DifferentOpaqueValue'}
    row['unknown'] = json.dumps(outside) if as_string else outside
    with pytest.raises(ValueError, match='unreviewed Newmark credential key'):
        newmark_rcm.prepare(events.encoded(value), source_key='newmark')


def test_original_parsed_disagreement_is_refused_before_projection():
    value = artifact(); row = value['sources'][0]['retainedObservations'][0]
    row['sourceObservation']['parsed']['unmappedOnly'] = 'extra-object-value'
    with pytest.raises(ValueError, match='parsed structure mismatch'):
        newmark_rcm.prepare(events.encoded(value), source_key='newmark')


def test_js_rounding_does_not_cause_false_parsed_mismatch():
    value = artifact(); row = value['sources'][0]['retainedObservations'][0]
    row['sourceObservation']['literal'] = row['sourceObservation']['literal'].replace('1.00000000000000001', '9007199254740993')
    row['sourceObservation']['parsed']['number'] = 9007199254740992
    projected, _, _ = newmark_rcm.prepare(events.encoded(value), source_key='newmark')
    assert '9007199254740993' in projected['sources'][0]['retainedObservations'][0]['sourceObservation']['literal']


@pytest.mark.parametrize('marker', [False, None])
def test_unknown_source_literal_cannot_hide_encoded_credentials(marker):
    value = artifact()
    row = value['sources'][0]['retainedObservations'][0]
    row.clear()
    if marker is not None:
        row['newmarkRcmObservation'] = marker
    row['sourceObservation'] = {'schema': 'future/v1', 'literal': '{"password":"DUMMY_PRIVATE_VALUE"}'}
    with pytest.raises(ValueError):
        newmark_rcm.project(value, source_key='newmark')
    observed = collector.convert(value, source_key='newmark')
    with pytest.raises(ValueError):
        events.validate_content_policy(events.plan(observed, source_key='newmark'))


def test_unknown_source_literal_clean_facts_preserved():
    value = artifact()
    row = value['sources'][0]['retainedObservations'][0]
    row.clear()
    row['sourceObservation'] = {'schema': 'future/v1', 'literal': '{"useful":[false,null,0]}', 'note': 'preserve'}
    projected, literals, receipt = newmark_rcm.project(value, source_key='newmark')
    assert projected['sources'][0]['retainedObservations'][0] == row
    newmark_rcm.validate(projected, receipt, source_key='newmark', numeric_literals=literals)
    observed = collector.convert(projected, source_key='newmark', numeric_literals=literals, projection=receipt)
    plan = events.plan(observed, source_key='newmark')
    events.validate_content_policy(plan)
    assert collector.reconstruct(observed) == projected
