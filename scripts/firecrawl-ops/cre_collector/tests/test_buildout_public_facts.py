import copy
import json

import pytest

import cre_ingest
from source_fields import buildout_public_facts as bridge


def listing(source='bradford-allen', feed='bradford-allen'):
    url = 'https://broker.example/show?propertyId=42-sale'
    clock = '2026-10-29T00:00:00Z'
    mode = 'lease' if feed == 'franklin-street-lease' else 'sale'
    return {'sourceKey': source, 'transactionMode': mode, 'id': '42', 'url': url, 'inventoryObservedAt': clock,
            'freshnessProvenance': {'detailScope': 'authoritative_inventory_feed', 'method': 'buildout_inventory_feed'},
            'buildoutRawRetention': {'schema': 'buildout-raw-retention-v1', 'status': 'complete',
                                     'sourceKey': source, 'feedKey': feed, 'binding': 'id_and_url', 'listingId': '42',
                                     'listingUrl': url, 'inventoryObservedAt': clock},
            'rawBuildout': {'id': 42, 'show_link': url, 'unknown': [None, False, '', 0, [], {'n': 0.1}],
                            'pdf_url': None}}


def envelope(value):
    return cre_ingest.to_row(value, {}, None)['extra_facts']['cre_public_listing_facts']


def test_original_facts_and_stable_mapper_identity():
    value = listing()
    prior = copy.deepcopy(value)
    row = cre_ingest.to_row(value, {}, None)
    assert row['external_id'] == '42'
    result = row['extra_facts']['cre_public_listing_facts']
    assert result['sourceKey'] == 'bradford-allen' and result['codec'] == 'buildout-public-inventory/v1'
    assert next(f['value'] for f in result['facts'] if f['label'] == 'unknown') == [None, False, '', 0, [], {'n': 0.1}]
    assert not any('numericLiterals' in f for f in result['facts'])
    assert result['observedAt'] == value['inventoryObservedAt'] and value == prior


@pytest.mark.parametrize('mutation', ['source', 'feed', 'id', 'url', 'clock', 'unregistered'])
def test_exact_registry_and_identity_binding(mutation):
    value = listing()
    if mutation == 'source': value['buildoutRawRetention']['sourceKey'] = 'leibsohn'
    if mutation == 'feed': value['buildoutRawRetention']['feedKey'] = 'leibsohn'
    if mutation == 'id': value['rawBuildout']['id'] = 43
    if mutation == 'url': value['rawBuildout']['show_link'] += '-different'
    if mutation == 'clock': value['buildoutRawRetention']['inventoryObservedAt'] = '2026-10-28T00:00:00Z'
    if mutation == 'unregistered': value['buildoutRawRetention']['feedKey'] = None
    result = envelope(value)
    if mutation != 'clock': assert result['facts'] == []
    assert result['captureCompleteness'] == 'partial'


def test_franklin_variants_bound_by_registry():
    assert envelope(listing('franklin-street', 'franklin-street-sale'))['facts']
    assert envelope(listing('franklin-street', 'franklin-street-lease'))['facts']
    assert not envelope(listing('franklin-street', 'franklin-street'))['facts']


def test_unmarked_legacy_does_not_supply_public_envelope():
    value = listing()
    value.pop('buildoutRawRetention')
    assert 'cre_public_listing_facts' not in (cre_ingest.to_row(value, {}, None)['extra_facts'] or {})


def test_known_document_and_token_copies_withheld_from_all_staging():
    value = listing()
    document = 'https://buildout.com/docs/abc/123?token=EXAMPLE_SECRET_TOKEN'
    value['rawBuildout']['pdf_url'] = document
    value['brochures'] = [{'name': 'Brochure', 'url': document}]
    value['description'] = 'EXAMPLE_SECRET_TOKEN'
    value['rawBuildout']['unknown'] = {'copy': document, 'other': 'retained'}
    staged = bridge.attach(value)
    text = json.dumps(staged)
    assert 'EXAMPLE_SECRET_TOKEN' not in text
    assert staged['rawBuildout']['pdf_url'] is None
    assert staged['rawBuildout']['unknown']['other'] == 'retained'
    assert staged['buildoutRawStaging']['status'] == 'redacted'
    assert staged['extraFacts']['cre_public_listing_facts']['captureCompleteness'] == 'partial'
    assert value['rawBuildout']['pdf_url'] == document


def test_credential_removal_and_replay():
    value = listing()
    value['rawBuildout']['api%5Fkey'] = 'EXAMPLE_CREDENTIAL'
    value['description'] = 'copy EXAMPLE_CREDENTIAL'
    stage = bridge.attach(value)
    assert 'EXAMPLE_CREDENTIAL' not in json.dumps(stage)
    stage['extraFacts']['cre_public_listing_facts']['facts'] = [{'label': 'FORGED'}]
    replay = bridge.attach(stage)
    assert replay['buildoutRawStaging']['status'] == 'redacted'
    assert replay['extraFacts']['cre_public_listing_facts']['captureCompleteness'] == 'partial'
    assert 'FORGED' not in json.dumps(replay['extraFacts'])


def test_overlimit_fact_reports_partial():
    value = listing()
    value['rawBuildout']['large'] = 'x' * 17000
    result = envelope(value)
    assert result['captureCompleteness'] == 'partial'
    assert any(item['reason'] == 'input_limit' for item in result['omissions'])


def test_missing_identity_or_url_does_not_invent_either():
    value = listing()
    value.pop('id')
    value['rawBuildout'].pop('id')
    value['buildoutRawRetention']['listingId'] = None
    value['buildoutRawRetention']['binding'] = 'url_only'
    assert envelope(value)['facts']
    value = listing()
    value.pop('url')
    value['rawBuildout'].pop('show_link')
    value['buildoutRawRetention']['listingUrl'] = None
    value['buildoutRawRetention']['binding'] = 'id_only'
    projected = bridge.attach(value)
    assert projected['extraFacts']['cre_public_listing_facts']['facts']
    assert projected['extraFacts']['cre_public_listing_facts']['sourceUrl'] is None
    assert projected['buildoutRawStaging']['canonicalUnavailable'] is True
    assert cre_ingest.to_row(value, {}, None) is None


def test_actual_dryrun_excludes_private_document_and_keeps_input(tmp_path):
    import os
    import re
    import subprocess
    import sys

    value = listing()
    document = 'https://buildout.com/docs/abc/123?token=EXAMPLE_SECRET_TOKEN'
    value['rawBuildout']['pdf_url'] = document
    value['brochures'] = [{'name': 'Brochure', 'url': document}]
    value['description'] = 'EXAMPLE_SECRET_TOKEN'
    input_path = tmp_path / 'artifact.json'
    original = json.dumps({'runMeta': {'mode': 'full', 'finishedAt': '2026-10-29T00:00:00Z'},
                           'sources': [{'sourceKey': 'bradford-allen'}], 'listings': [value],
                           'brokers': [], 'totalListings': 1}).encode()
    input_path.write_bytes(original)
    result = subprocess.run([sys.executable, 'cre_ingest.py', '--in', str(input_path), '--dry-run'],
                            text=True, capture_output=True, env={**os.environ, 'TMPDIR': str(tmp_path)}, check=True)
    sql_path = re.search(r'^sql: (.+?) \(', result.stderr, re.MULTILINE)
    assert sql_path is not None
    with open(sql_path.group(1)) as staged:
        text = staged.read()
    assert 'buildout-public-inventory/v1' in text and 'EXAMPLE_SECRET_TOKEN' not in text
    assert input_path.read_bytes() == original


def test_http_canonical_row_keeps_facts_with_partial_public_url():
    value = listing()
    value['url'] = value['url'].replace('https:', 'http:')
    value['rawBuildout']['show_link'] = value['url']
    value['buildoutRawRetention']['listingUrl'] = value['url']
    row = cre_ingest.to_row(value, {}, None)
    result = row['extra_facts']['cre_public_listing_facts']
    assert row['source_url'] == value['url']
    assert result['facts'] and result['sourceUrl'] is None
    assert result['captureCompleteness'] == 'partial'


def test_franklin_feed_is_bound_to_collection_pass():
    value = listing('franklin-street', 'franklin-street-sale')
    value['buildoutRawRetention']['feedKey'] = 'franklin-street-lease'
    assert envelope(value)['facts'] == []
    value['transactionMode'] = 'lease'
    assert envelope(value)['facts']
    value.pop('transactionMode')
    assert envelope(value)['facts'] == []


def test_broker_copies_are_sanitized_without_mutating_shared_pool():
    value = listing()
    value['rawBuildout']['apiKey'] = 'EXAMPLE_CREDENTIAL'
    value['brokerIds'] = [0]
    brokers = {0: {'name': 'EXAMPLE_CREDENTIAL', 'email': 'person@example.test'}}
    original = copy.deepcopy(brokers)
    row = cre_ingest.to_row(value, brokers, None)
    assert 'EXAMPLE_CREDENTIAL' not in str(row)
    assert row['contacts'][0]['email'] == 'person@example.test'
    assert brokers == original


def test_dual_pass_preserves_whole_facts_and_deduplicates_replay():
    a = listing('franklin-street', 'franklin-street-sale')
    b = listing('franklin-street', 'franklin-street-lease')
    b['url'] = b['url'].replace('-sale', '-lease')
    b['rawBuildout']['show_link'] = b['url']
    b['buildoutRawRetention']['listingUrl'] = b['url']
    b['rawBuildout']['unknown'] = [None, False, 0, 'lease']
    b['inventoryObservedAt'] = '2026-10-29T01:00:00Z'
    b['buildoutRawRetention']['inventoryObservedAt'] = b['inventoryObservedAt']
    ar = cre_ingest.to_row(a, {}, None)
    br = cre_ingest.to_row(b, {}, None)
    merged = cre_ingest.merge_rows(ar, br)
    env = merged['extra_facts']['cre_public_listing_facts']
    assert len(env['facts']) == 2 and env['observedAt'] is None and env['sourceUrl'] is None
    values = [f['value'] for f in env['facts']]
    assert [v['feedKey'] for v in values] == ['franklin-street-sale', 'franklin-street-lease']
    assert next(f['value'] for f in values[1]['facts'] if f['label'] == 'unknown') == [None, False, 0, 'lease']
    again = cre_ingest.merge_rows(merged, cre_ingest.to_row(b, {}, None))
    assert len(again['extra_facts']['cre_public_listing_facts']['facts']) == 2
    assert 'primary' in again['raw_data'] and 'secondary_pass' in again['raw_data']


def test_inconsistent_binding_marker_is_not_qualified():
    value = listing()
    value['buildoutRawRetention']['binding'] = 'url_only'
    assert envelope(value)['facts'] == []


def test_staging_key_redaction_refuses_instead_of_using_uninspected_brokers():
    value = listing()
    value['rawBuildout']['apiKey'] = 'public'
    value['brokerIds'] = [0]
    brokers = {0: {'name': 'public', 'email': 'person@example.test'}}
    with pytest.raises(ValueError, match='credential inspection failed'):
        cre_ingest.to_row(value, brokers, None)


def test_staging_never_iterates_shared_broker_pool():
    from collections.abc import Mapping

    class Pool(Mapping):
        def __getitem__(self, key):
            if key == 0:
                return {'name': 'Broker', 'email': 'person@example.test'}
            raise KeyError(key)

        def __iter__(self):
            raise AssertionError('global broker pool iterated')

        def __len__(self):
            return 1000000

    value = listing()
    value['brokerIds'] = [0]
    row = cre_ingest.to_row(value, Pool(), None)
    assert row['contacts'][0]['name'] == 'Broker'


def test_staging_length_mismatch_refuses():
    from source_fields.public_facts import _unstage_referenced_brokers

    with pytest.raises(ValueError, match='altered'):
        _unstage_referenced_brokers({'_publicFactsStagedBrokers': []}, {0: {}}, [0])


def test_no_broker_references_do_not_create_staging_sentinel():
    value = listing()
    value['rawBuildout']['apiKey'] = 'public'
    row = cre_ingest.to_row(value, {}, None)
    assert row['extra_facts']['cre_public_listing_facts']['captureCompleteness'] == 'partial'


def test_large_dual_pass_preserves_ordered_prefix_with_limit_notice():
    a = listing('franklin-street', 'franklin-street-sale')
    b = listing('franklin-street', 'franklin-street-lease')
    b['url'] = b['url'].replace('-sale', '-lease')
    b['rawBuildout']['show_link'] = b['url']
    b['buildoutRawRetention']['listingUrl'] = b['url']
    for index in range(26):
        a['rawBuildout'][f'field_{index}'] = 'a' * 10000
        b['rawBuildout'][f'field_{index}'] = 'b' * 10000
    ar = cre_ingest.to_row(a, {}, None)
    br = cre_ingest.to_row(b, {}, None)
    assert len(json.dumps(ar['extra_facts'])) > 260000
    assert len(json.dumps(br['extra_facts'])) > 260000
    merged = cre_ingest.merge_rows(ar, br)
    envelope = merged['extra_facts']['cre_public_listing_facts']
    assert len(envelope['facts']) == 1
    assert envelope['facts'][0]['value']['feedKey'] == 'franklin-street-sale'
    assert envelope['captureCompleteness'] == 'partial'
    assert any(item['reason'] == 'input_limit' for item in envelope['omissions'])
    assert 'primary' in merged['raw_data'] and 'secondary_pass' in merged['raw_data']
    assert merged['raw_data']['secondary_pass']['rawBuildout']['field_0'] == 'b' * 10000
