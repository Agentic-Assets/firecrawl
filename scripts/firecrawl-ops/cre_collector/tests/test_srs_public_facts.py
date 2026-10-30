import copy
import json

import pytest

import cre_ingest


def listing():
    url = 'https://www.srsre.com/properties/one'
    return {'sourceKey': 'srs', 'id': '123', 'url': url,
            'inventoryObservedAt': '2026-10-29T00:00:00Z',
            'freshnessProvenance': {'detailScope': 'authoritative_inventory_feed', 'method': 'srs_cloud_run_inventory_feed'},
            'srsRawRetention': {'schema': 'srs-raw-retention-v1', 'status': 'complete', 'listingId': '123', 'listingUrl': url},
            'rawSrs': {'permalink': '/properties/one', 'apto_data': {
                'SRS_Listings_ID__c': '123', 'Hide_Sale_Price__c': True, 'Sale_Price__c': 4500000,
                'unknown': [None, False, '', 0],
                'marketing_approver': {'Professional_Credentials_Certification__c': 'CCIM'},
                'related_brokers': [{'Professional_Credentials_Certification__c': 'SIOR'}]}}}


def envelope(value):
    return cre_ingest.to_row(value, {}, None)['extra_facts']['cre_public_listing_facts']


def test_ingest_preserves_business_fields_and_private_raw():
    value = listing()
    prior = copy.deepcopy(value)
    result = envelope(value)
    data = next(f['value'] for f in result['facts'] if f['label'] == 'apto_data')
    assert data['Sale_Price__c'] == 4500000 and data['Hide_Sale_Price__c'] is True
    assert data['unknown'] == [None, False, '', 0]
    assert data['related_brokers'][0]['Professional_Credentials_Certification__c'] == 'SIOR'
    assert data['marketing_approver']['Professional_Credentials_Certification__c'] == 'CCIM'
    assert result['observedAt'] == value['inventoryObservedAt']
    assert not any('numericLiterals' in f for f in result['facts'])
    assert value == prior


@pytest.mark.parametrize('mutation', ['identity', 'url', 'marker', 'numeric_id'])
def test_bad_binding_is_explicitly_partial(mutation):
    value = listing()
    if mutation == 'identity': value['rawSrs']['apto_data']['SRS_Listings_ID__c'] = 'other'
    if mutation == 'url': value['rawSrs']['permalink'] = '/other'
    if mutation == 'marker': value['srsRawRetention']['status'] = 'omitted'
    if mutation == 'numeric_id': value['rawSrs']['apto_data']['SRS_Listings_ID__c'] = 9007199254740992
    result = envelope(value)
    assert result['facts'] == [] and result['captureCompleteness'] == 'partial'


def test_credential_redaction_and_wrong_container():
    value = listing()
    data = value['rawSrs']['apto_data']
    data['related_brokers'] = {'0': {'Professional_Credentials_Certification__c': 'secret-value'}}
    data['api%5Fkey'] = 'token-xyz'
    data['copy'] = 'prefix token-xyz suffix'
    data['encoded'] = 'token%2Dxyz'
    result = envelope(value)
    encoded = json.dumps(result)
    assert 'secret-value' not in encoded and 'token-xyz' not in encoded and 'token%2Dxyz' not in encoded
    assert result['captureCompleteness'] == 'partial'


def test_input_limit_is_explicit():
    value = listing()
    value['rawSrs']['apto_data']['huge'] = 'x' * 17000
    result = envelope(value)
    assert result['captureCompleteness'] == 'partial'
    assert any(o['reason'] == 'input_limit' for o in result['omissions'])


def test_safe_numeric_identity_matches_mapper_string():
    value = listing()
    value['rawSrs']['apto_data']['SRS_Listings_ID__c'] = 123
    assert envelope(value)['facts']


def test_unmarked_legacy_row_does_not_supply_public_envelope():
    value = listing()
    value.pop('srsRawRetention')
    row = cre_ingest.to_row(value, {}, None)
    assert 'cre_public_listing_facts' not in (row['extra_facts'] or {})


@pytest.mark.parametrize('url', [
    'https://www.srsre.com/' + 'x' * 2049,
    'https://www.srsre.com/path\nother',
    'https://www.srsre.com/path%250Aother',
    'https://www.srsre.com/path?api%255Fkey=SECRET',
    'https://www.srsre.com/path?api&#75;ey=SECRET',
])
def test_unsafe_url_never_published(url):
    value = listing()
    value['url'] = url
    value['srsRawRetention']['listingUrl'] = url
    from source_fields.srs_public_facts import attach
    value['rawSrs']['permalink'] = url.removeprefix('https://www.srsre.com')
    attached = attach(value)
    result = attached['extraFacts']['cre_public_listing_facts']
    assert attached['srsRawStaging']['canonicalUnavailable'] is True
    assert result['sourceUrl'] is None
    assert result['facts']
    assert url not in json.dumps(result)
    assert result['captureCompleteness'] == 'partial'
    assert cre_ingest.to_row(value, {}, None) is None


def test_actual_dry_run_stages_projection_and_preserves_input(tmp_path):
    import os
    import re
    import subprocess
    import sys

    value = listing()
    value['rawSrs']['apto_data']['apiKey'] = 'EXAMPLE_PRIVATE_MATERIAL'
    value['description'] = 'Copypaste EXAMPLE_PRIVATE_MATERIAL'
    value['rawSrs']['apto_data']['Description__c'] = value['description']
    artifact = {'runMeta': {'mode': 'full', 'finishedAt': '2026-10-29T00:00:00Z'},
                'sources': [{'sourceKey': 'srs'}], 'listings': [value], 'brokers': [], 'totalListings': 1}
    input_path = tmp_path / 'artifact.json'
    original = json.dumps(artifact).encode()
    input_path.write_bytes(original)
    result = subprocess.run([sys.executable, 'cre_ingest.py', '--in', str(input_path), '--dry-run'],
                            text=True, capture_output=True, env={**os.environ, 'TMPDIR': str(tmp_path)}, check=True)
    sql_path = re.search(r'^sql: (.+?) \(', result.stderr, re.MULTILINE)
    assert sql_path is not None
    with open(sql_path.group(1)) as staged:
        text = staged.read()
    assert 'cre_public_listing_facts' in text and 'srs-public-inventory/v1' in text
    assert 'EXAMPLE_PRIVATE_MATERIAL' not in text
    assert 'srsRawStaging' in text
    assert input_path.read_bytes() == original


def test_url_only_identity_uses_existing_ingest_fallback():
    value = listing()
    value.pop('id')
    value['rawSrs']['apto_data'].pop('SRS_Listings_ID__c')
    value['srsRawRetention']['listingId'] = None
    assert envelope(value)['facts']
    assert cre_ingest.to_row(value, {}, None)['external_id'].startswith('url:')


def test_id_only_projection_keeps_private_fact_without_changing_row_admission():
    from source_fields.srs_public_facts import attach
    value = listing()
    value.pop('url')
    value['rawSrs'].pop('permalink')
    value['srsRawRetention']['listingUrl'] = None
    attached = attach(value)
    result = attached['extraFacts']['cre_public_listing_facts']
    assert attached['srsRawStaging']['canonicalUnavailable'] is True
    assert result['facts'] and result['sourceUrl'] is None
    assert result['captureCompleteness'] == 'partial'
    assert cre_ingest.to_row(value, {}, None) is None


def test_no_identity_preserves_private_without_qualification():
    from source_fields.srs_public_facts import attach
    value = listing()
    value.pop('id')
    value.pop('url')
    value['rawSrs']['apto_data'].pop('SRS_Listings_ID__c')
    value['rawSrs'].pop('permalink')
    value['srsRawRetention'].update(listingId=None, listingUrl=None)
    assert attach(value)['extraFacts']['cre_public_listing_facts']['facts'] == []


def test_mapper_fallback_id_without_apto_data():
    from source_fields.srs_public_facts import attach
    value = listing()
    value['rawSrs'].pop('apto_data')
    value['rawSrs']['id'] = '123'
    assert attach(value)['extraFacts']['cre_public_listing_facts']['facts']


def test_replay_retains_partial_and_recomputes_envelope():
    from source_fields.srs_public_facts import attach
    value = listing()
    value['rawSrs']['apto_data']['apiKey'] = 'secretvalue'
    value['description'] = 'secretvalue'
    first = attach(value)
    assert first['srsRawStaging']['status'] == 'redacted'
    assert first['description'] is None
    first['extraFacts']['cre_public_listing_facts']['facts'] = [{'label': 'FORGED'}]
    second = attach(first)
    result = second['extraFacts']['cre_public_listing_facts']
    assert second['srsRawStaging']['status'] == 'redacted'
    assert result['captureCompleteness'] == 'partial'
    assert 'FORGED' not in json.dumps(result)
