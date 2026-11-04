"""Synthetic, opt-in Newmark claim witness carrier; no provider access."""
import json

import pytest

from cre_enrich import _parse_claimed, build_claim_sql, claim_rows_to_items
from newmark_claim_witness import parse


def test_legacy_and_targeted_claim_sql_unchanged_without_opt_in():
    assert 'rawNewmarkNimRecord' not in build_claim_sql(50)
    assert 'rawNewmarkNimRecord' not in build_claim_sql(50, source='matthews', targeted=True)
    with pytest.raises(ValueError):
        build_claim_sql(50, source='matthews', targeted=True, source_detail_witness=True)
    assert "q.source_key = 'newmark'" in build_claim_sql(50, source_detail_witness=True)


def test_many_claims_preserve_source_identity_and_private_witness_only():
    witness = {'status':'observed', 'schema':'newmark-nim-link-witness/v1', 'sourceKey':'newmark',
               'sourcePath':'raw_data.rawNewmarkNimRecord', 'recordId': 'abc123', 'slug':'property',
               'url':'https://my.rcm1.com/handler/modern.aspx?pv=Fixture123', 'inventoryObservedAt': None,
               'sourceName': {'state':'missing'}, 'sourceProperty': {'selection':'unresolved', 'fields':{}}}
    rows = [(str(i), 'newmark', 'property', 'https://www.nmrk.com/properties/property', '', '0', '', json.dumps(witness))
            for i in range(70)]
    rows.append(('71', 'other', 'o', 'https://example.com', '', '0'))
    claims = claim_rows_to_items(_parse_claimed(rows, source_detail_witness=True))['items']
    assert len(claims) == 71
    assert all(c['externalId'] == 'property' and c['sourcePublishedDetail'] == witness for c in claims[:70])
    assert 'sourcePublishedDetail' not in claims[-1]
    assert all('secureDocumentsUrl' not in json.dumps(c) for c in claims)


@pytest.mark.parametrize('payload', ['{', json.dumps({'url':'https://example.test/?token=dummy-secret'}),
    json.dumps({'status':'observed','unexpected':'dummy-secret'}), 'x' * 4097])
def test_malformed_witness_yields_body_free_diagnostic(payload):
    result = parse(payload)
    assert result == {'status':'unresolved', 'reason':'invalid_carrier'}
    assert 'dummy-secret' not in json.dumps(result)


def test_numeric_native_id_is_not_coerced_to_a_witness():
    value = {'status':'observed', 'schema':'newmark-nim-link-witness/v1', 'sourceKey':'newmark',
             'sourcePath':'raw_data.rawNewmarkNimRecord', 'recordId': 123, 'slug':'property',
             'url':'https://my.rcm1.com/handler/modern.aspx?pv=Fixture123', 'inventoryObservedAt': None,
               'sourceName': {'state':'missing'}, 'sourceProperty': {'selection':'unresolved', 'fields':{}}}
    assert parse(json.dumps(value)) == {'status':'unresolved', 'reason':'invalid_carrier'}


def test_property_witness_preserves_missing_null_and_exact_literals():
    value = {'status':'observed', 'schema':'newmark-nim-link-witness/v1', 'sourceKey':'newmark',
             'sourcePath':'raw_data.rawNewmarkNimRecord', 'recordId':'record', 'slug':'property',
             'url':'https://my.rcm1.com/handler/modern.aspx?pv=Fixture123', 'inventoryObservedAt': None,
             'sourceName': {'state':'value','value':'Property 1'},
             'sourceProperty': {'selection':'single','fields': {
                 'address': {'state':'value','value':'123 Main St'}, 'city': {'state':'missing'},
                 'stateAbbreviation': {'state':'null'}, 'stateDescription': {'state':'unresolved'},
                 'zip': {'state':'value','value':''}, 'countryCode': {'state':'value','value':'US'}}}}
    assert parse(json.dumps(value)) == value
    value['sourceName'] = {'state':'value', 'value':'Token Industrial'}
    value['sourceProperty']['fields']['address'] = {'state':'value','value':'Calle Muñoz 123'}
    assert parse(json.dumps(value, ensure_ascii=False)) == value
    value['sourceProperty']['fields']['address'] = {'state':'value','value':'bad\u0001control'}
    assert parse(json.dumps(value)) == {'status':'unresolved','reason':'invalid_carrier'}


@pytest.mark.parametrize('source', [None, 'newmark'])
@pytest.mark.parametrize('enabled', [False, True])
def test_worker_source_evidence_mode_carries_witness_only_when_selected(source, enabled, tmp_path, monkeypatch):
    from test_cre_enrich_psql import _Args, _wire_run

    import cre_enrich

    rows = [{'id':'1','source_key':'newmark','external_id':'property','url':'https://example.com/1'},
            {'id':'2','source_key':'other','external_id':'other','url':'https://example.com/2'}]
    calls = _wire_run(monkeypatch, tmp_path, claimed_rows=rows, collect_rc=1)
    prior = cre_enrich._psql_query
    def query(db_url, sql):
        values = prior(db_url, sql)
        if 'rawNewmarkNimRecord' in sql:
            witness = {'status':'observed', 'schema':'newmark-nim-link-witness/v1', 'sourceKey':'newmark',
                       'sourcePath':'raw_data.rawNewmarkNimRecord', 'recordId':'record', 'slug':'property',
                       'url':'https://my.rcm1.com/handler/modern.aspx?pv=Fixture123', 'inventoryObservedAt':None,
                       'sourceName': {'state':'missing'}, 'sourceProperty': {'selection':'unresolved','fields':{}}}
            return [tuple(row) + ('', json.dumps(witness) if row[1] == 'newmark' else '') for row in values]
        return values
    monkeypatch.setattr(cre_enrich, '_psql_query', query)
    if enabled: monkeypatch.setenv('CRE_NEWMARK_PUBLISHED_DETAIL', '1')
    else: monkeypatch.delenv('CRE_NEWMARK_PUBLISHED_DETAIL', raising=False)
    args = _Args(source=source)
    args.artifact_out = str(tmp_path / 'enriched.json')
    assert cre_enrich.run(args) == 1
    claims = json.loads((tmp_path / 'claims.json').read_text())['items']
    assert ('rawNewmarkNimRecord' in calls['query_sqls'][0]) is enabled
    assert ('sourcePublishedDetail' in claims[0]) is enabled
    assert 'sourcePublishedDetail' not in claims[1]


def test_policy_v4_is_explicit_and_legacy_policy_does_not_enable_detail(tmp_path):
    import hashlib

    from test_automatic import SHA, setup_policy

    from source_evidence import automatic, events

    config, policy, path = setup_policy(tmp_path, sources=('newmark',))
    assert 'enrichment_detail_formats' not in config
    from source_evidence.projections import newmark_rcm
    policy.update(schema_version=automatic.SCHEMA_V4, collector_projections={'newmark': newmark_rcm.VERSION}, native_source_formats={},
                  max_native_pages=128, document_text_formats={},
                  enrichment_detail_formats={'newmark': automatic.NEWMARK_DETAIL_FORMAT})
    def load(value):
        raw = events.encoded(value); path.write_bytes(raw)
        return automatic.configuration(str(path), hashlib.sha256(raw).hexdigest(), config['env_file'],
                                       config['target_sha256'], SHA, ['newmark'])
    assert load(policy)['enrichment_detail_formats'] == {'newmark': automatic.NEWMARK_DETAIL_FORMAT}
    for bad in ({'newmark':'other'}, {'other': automatic.NEWMARK_DETAIL_FORMAT}, {'newmark': None}):
        policy['enrichment_detail_formats'] = bad
        with pytest.raises(ValueError): load(policy)
