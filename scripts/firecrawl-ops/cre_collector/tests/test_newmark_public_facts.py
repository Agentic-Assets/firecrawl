"""Newmark public facts remain bound to accepted claim and source observations."""
import copy
import json

import pytest

import cre_ingest
from cre_artifact_scope import require_canonical_artifact
from source_fields import newmark_public_facts as bridge


def field(value):
    return {'state': 'value', 'value': value}


def artifact(count=1):
    rows, observations = [], []
    for i in range(count):
        slug = f'property-{i}'
        page = f'https://my.rcm1.com/handler/modern.aspx?pv={i}'
        url = f'https://www.nmrk.com/properties/{slug}'
        data = {'name': f'Property {i}', 'addresses': {'street': '1 Main St', 'city': 'Boston', 'state': 'MA'},
                'unknown': [None, False, 0, {'empty': '', 'unknown': []}], 'bingMapKey': 'DUMMY_PRIVATE_VALUE'}
        witness = {'schema': 'newmark-nim-link-witness/v1', 'status': 'observed', 'sourceKey': 'newmark',
                   'sourcePath': 'raw_data.rawNewmarkNimRecord', 'recordId': slug, 'slug': slug, 'url': page,
                   'inventoryObservedAt': None,
                   'sourceName': field(data['name']), 'sourceProperty': {'selection': 'single',
                       'fields': {'address': field('1 Main St'), 'city': field('Boston'),
                                  'stateAbbreviation': field('MA'), 'stateDescription': {'state': 'missing'},
                                  'zip': {'state': 'missing'}, 'countryCode': {'state': 'missing'}}}}
        source = {'schema': bridge.newmark_rcm.SCHEMA, 'identity': 'unverified', 'visibility': 'unverified',
                  'sourceClock': None, 'sourceClockStatus': 'unverified', 'witness': witness, 'extraction': 'observed',
                  'match': 'matched', 'matchMethod': 'strict_json_visitor_source_candidate',
                  'numericFidelity': 'literal_only', 'parsedSerializable': True,
                  'literal': json.dumps(data), 'parsed': data}
        observations.append({'claim': {'sourceKey': 'newmark', 'externalId': slug, 'url': url, 'identity': 'unverified'},
                             'requestedPageUrl': page, 'capturedAt': '2026-10-29T00:00:00Z',
                             'newmarkRcmObservation': True, 'sourceObservation': source,
                             'scrapedDoc': {'metadata': {'statusCode': 200}}})
        rows.append({'sourceKey': 'newmark', 'id': slug, 'url': url})
    return {'runMeta': {'mode': 'enrich', 'finishedAt': '2026-10-29T00:00:00Z'},
            'sources': [{'sourceKey': 'newmark', 'retainedObservations': observations}],
            'listings': rows, 'brokers': [], 'totalListings': count}


def raw(value):
    return json.dumps(value).encode()


def test_60_source_bound_records_preserve_unknown_values_and_credentials_removed():
    original = artifact(60)
    snapshot = raw(original)
    mappings = bridge.qualified(snapshot)
    assert len(mappings) == 60
    for listing in original['listings']:
        derived = bridge.attach(listing, mappings)
        envelope = cre_ingest.to_row(derived, {}, None)['extra_facts']['cre_public_listing_facts']
        assert next(f['value'] for f in envelope['facts'] if f['label'] == 'unknown') == [None, False, 0, {'empty': '', 'unknown': []}]
        assert envelope['captureCompleteness'] == 'partial'
        assert envelope['sourceCoverage'] == 'unverified'
        assert 'DUMMY_PRIVATE_VALUE' not in json.dumps(envelope)
    assert raw(original) == snapshot


@pytest.mark.parametrize('change', ['name', 'claim', 'url', 'match', 'duplicate'])
def test_binding_conflicts_refuse_public_envelope(change):
    value = artifact()
    row = value['sources'][0]['retainedObservations'][0]
    if change == 'name':
        row['sourceObservation']['witness']['sourceName'] = field('Different')
    elif change == 'claim':
        row['claim']['externalId'] = 'different'
    elif change == 'url':
        row['requestedPageUrl'] += 'different'
    elif change == 'match':
        row['sourceObservation']['match'] = 'unresolved'
    else:
        value['sources'][0]['retainedObservations'].append(copy.deepcopy(row))
    assert bridge.qualified(raw(value)) == {}


def test_precision_witness_and_guard_remain_intact():
    value = artifact()
    source = value['sources'][0]['retainedObservations'][0]['sourceObservation']
    source['literal'] = source['literal'][:-1] + ', "precise": 0.10000000000000000001}'
    source['parsed']['precise'] = 0.1
    mapping = bridge.qualified(raw(value))
    fact = next(f for f in next(iter(mapping.values()))['facts'] if f['label'] == 'precise')
    assert fact['value'] is None and fact['numericLiterals'] == {'': '0.10000000000000000001'}
    projected, _, _ = bridge.newmark_rcm.prepare(raw(value), source_key='newmark')
    with pytest.raises(ValueError):
        require_canonical_artifact(projected)


def test_overlong_value_is_explicitly_omitted_and_siblings_preserved():
    value = artifact()
    source = value['sources'][0]['retainedObservations'][0]['sourceObservation']
    data = json.loads(source['literal'])
    data.update(big='x' * 16385, good='present')
    source['literal'] = json.dumps(data)
    source['parsed'] = data
    envelope = next(iter(bridge.qualified(raw(value)).values()))
    assert 'big' not in [f['label'] for f in envelope['facts']]
    assert 'good' in [f['label'] for f in envelope['facts']]
    assert any(o['reason'] == 'input_limit' for o in envelope['omissions'])


def test_real_ingest_cli_dry_run_stages_projected_facts(tmp_path):
    import os
    import re
    import subprocess
    import sys

    input_path = tmp_path / 'artifact.json'
    input_path.write_bytes(raw(artifact(2)))
    result = subprocess.run([sys.executable, 'cre_ingest.py', '--in', str(input_path), '--dry-run'],
                            text=True, capture_output=True, env={**os.environ, 'TMPDIR': str(tmp_path)}, check=True)
    sql_path = re.search(r'^sql: (.+?) \(', result.stderr, re.M)
    assert sql_path is not None
    text = open(sql_path.group(1)).read()
    assert text.count('cre_public_listing_facts') == 4  # listing raw payload and staged extra facts
    assert 'newmark-rcm-enrichment-withholding/v1' in text


@pytest.mark.parametrize('key', ['api&#75;ey', 'api&#x4b;ey', 'api%4bey', 'api%4bey%ZZ'])
def test_encoded_credential_keys_do_not_reach_canonical_facts(key):
    value = artifact()
    source = value['sources'][0]['retainedObservations'][0]['sourceObservation']
    data = json.loads(source['literal'])
    data['nested'] = {'safe': 'business', key: 'EXAMPLE_PRIVATE_MATERIAL', 'copy': 'EXAMPLE_PRIVATE_MATERIAL'}
    source['literal'] = json.dumps(data)
    source['parsed'] = data
    envelope = next(iter(bridge.qualified(raw(value)).values()))
    text = json.dumps(envelope)
    assert 'EXAMPLE_PRIVATE_MATERIAL' not in text and key not in text
    nested = next(f['value'] for f in envelope['facts'] if f['label'] == 'nested')
    assert nested['safe'] == 'business' and nested['copy'] is None
    assert envelope['captureCompleteness'] == 'partial'


def test_nested_json_credential_label_drops_only_encoded_string():
    value = artifact()
    source = value['sources'][0]['retainedObservations'][0]['sourceObservation']
    data = json.loads(source['literal'])
    data['nested'] = [json.dumps({'api&#75;ey': 'EXAMPLE_PRIVATE_MATERIAL'}), 'safe', 'EXAMPLE_PRIVATE_MATERIAL']
    source['literal'] = json.dumps(data)
    source['parsed'] = data
    envelope = next(iter(bridge.qualified(raw(value)).values()))
    nested = next(f['value'] for f in envelope['facts'] if f['label'] == 'nested')
    assert nested == [None, 'safe', None]
    assert 'EXAMPLE_PRIVATE_MATERIAL' not in json.dumps(envelope)


def test_clean_encoded_business_label_is_preserved():
    value = artifact()
    source = value['sources'][0]['retainedObservations'][0]['sourceObservation']
    data = json.loads(source['literal'])
    data['retail&#32;space'] = 'available'
    source['literal'] = json.dumps(data)
    source['parsed'] = data
    envelope = next(iter(bridge.qualified(raw(value)).values()))
    assert any(f['label'] == 'retail&#32;space' and f['value'] == 'available' for f in envelope['facts'])


def test_encoded_credential_numeric_subtree_preserves_safe_siblings_and_witnesses():
    value = artifact()
    source = value['sources'][0]['retainedObservations'][0]['sourceObservation']
    data = json.loads(source['literal'])
    data['nested'] = {'api%4bey': 0.1, 'safe': None, 'other': 0.2}
    source['literal'] = json.dumps(data).replace('0.1, "safe"', '0.10000000000000000001, "safe"')
    source['parsed'] = data
    envelope = next(iter(bridge.qualified(raw(value)).values()))
    fact = next(f for f in envelope['facts'] if f['label'] == 'nested')
    assert fact['value'] == {'safe': None, 'other': 0.2}
    assert not fact.get('numericLiterals')
    assert envelope['captureCompleteness'] == 'partial'
    assert any(o['reason'] == 'credential_key_removed' for o in envelope['omissions'])


def test_encoded_secret_copy_in_object_key_is_removed():
    from urllib.parse import quote
    value = artifact()
    source = value['sources'][0]['retainedObservations'][0]['sourceObservation']
    data = json.loads(source['literal'])
    secret = 'DUMMY PRIVATE VALUE'
    data['nested'] = {'api%4bey': secret, 'other': {quote(secret): 'should not persist', 'safe': 'business'}}
    source['literal'] = json.dumps(data)
    source['parsed'] = data
    envelope = next(iter(bridge.qualified(raw(value)).values()))
    fact = next(f for f in envelope['facts'] if f['label'] == 'nested')
    assert fact['value'] == {'other': {'safe': 'business'}}
    assert quote(secret) not in json.dumps(envelope)
    assert secret not in json.dumps(envelope)
    assert any(o['sourcePath'] is None and o['reason'] == 'credential_key_removed' for o in envelope['omissions'])


def test_numeric_credential_copy_is_removed_from_siblings():
    value = artifact()
    source = value['sources'][0]['retainedObservations'][0]['sourceObservation']
    data = json.loads(source['literal'])
    data['nested'] = {'api%4bey': 9988776655, 'copy': '9988776655', 'number': 9988776655, 'safe': 'ordinary'}
    source['literal'] = json.dumps(data)
    source['parsed'] = data
    envelope = next(iter(bridge.qualified(raw(value)).values()))
    fact = next(f for f in envelope['facts'] if f['label'] == 'nested')
    assert fact['value'] == {'copy': None, 'number': None, 'safe': 'ordinary'}
    assert '9988776655' not in json.dumps(envelope)


def test_exceptional_numeric_credential_copy_uses_exact_literal():
    value = artifact()
    source = value['sources'][0]['retainedObservations'][0]['sourceObservation']
    data = json.loads(source['literal'])
    data['nested'] = {'api%4bey': 0.1, 'copy': '0.10000000000000000001', 'safe': 'ordinary'}
    source['literal'] = json.dumps(data).replace('0.1, "copy"', '0.10000000000000000001, "copy"')
    source['parsed'] = data
    envelope = next(iter(bridge.qualified(raw(value)).values()))
    fact = next(f for f in envelope['facts'] if f['label'] == 'nested')
    assert fact['value'] == {'copy': None, 'safe': 'ordinary'}
    assert '0.10000000000000000001' not in json.dumps(envelope)


def test_known_numeric_credential_in_prose_is_omitted_with_token_boundaries():
    value = artifact()
    source = value['sources'][0]['retainedObservations'][0]['sourceObservation']
    data = json.loads(source['literal'])
    data['nested'] = {'api%4bey': 9988776655, 'copy': 'prefix 9988776655 suffix',
                      'other': 'prefix 99887766550 suffix', 'safe': 'ordinary'}
    source['literal'] = json.dumps(data)
    source['parsed'] = data
    envelope = next(iter(bridge.qualified(raw(value)).values()))
    fact = next(f for f in envelope['facts'] if f['label'] == 'nested')
    assert fact['value'] == {'copy': None, 'other': 'prefix 99887766550 suffix', 'safe': 'ordinary'}
    assert any(o['reason'] == 'unverified_content_omitted' for o in envelope['omissions'])


def test_uninspectable_large_record_retains_explicit_partial_availability():
    value = artifact()
    source = value['sources'][0]['retainedObservations'][0]['sourceObservation']
    data = json.loads(source['literal'])
    data['oversized'] = [0] * 20001
    data['safeSibling'] = 'ordinary'
    source['literal'] = json.dumps(data)
    source['parsed'] = data
    snapshot = raw(value)
    mapping = bridge.qualified(snapshot)
    staged = cre_ingest.to_row(bridge.attach(value['listings'][0], mapping), {}, None)
    envelope = staged['extra_facts']['cre_public_listing_facts']
    assert envelope['facts'] == []
    assert envelope['captureCompleteness'] == 'partial'
    assert envelope['omissions'] == [{'sourcePath': None, 'reason': 'input_limit'}]
    assert raw(value) == snapshot
