import copy
import json

import pytest

import cre_ingest


def listing():
    url = 'https://www.kidder.com/listings/123'
    clock = '2026-10-29T00:00:00Z'
    return {'sourceKey': 'kidder-mathews', 'id': '123', 'url': url,
            'inventoryObservedAt': clock, 'transactionMode': 'sale',
            'freshnessProvenance': {'detailScope': 'authoritative_inventory_feed',
                                    'method': 'kidder_public_inventory_feed', 'cacheDisposition': 'live'},
            'kidderRawRetention': {'schema': 'kidder-raw-retention-v1', 'status': 'complete',
                                   'sourceKey': 'kidder-mathews', 'binding': 'id_and_url',
                                   'listingId': '123', 'listingUrl': url,
                                   'inventoryObservedAt': clock, 'cacheDisposition': 'live'},
            'rawKidder': {'listing_key': 123, 'property_key': 11, 'unknown': [None, False, '', 0, []],
                          'brokers': [{'name': 'Broker', 'license': 'business'}],
                          '__creInventoryCacheDisposition': 'provider-value'}}


def envelope(value):
    return cre_ingest.to_row(value, {}, None)['extra_facts']['cre_public_listing_facts']


def test_original_names_null_false_and_provenance():
    value = listing()
    original = copy.deepcopy(value)
    env = envelope(value)
    facts = {f['label']: f['value'] for f in env['facts']}
    assert facts['unknown'] == [None, False, '', 0, []]
    assert facts['brokers'] == value['rawKidder']['brokers']
    assert facts['__creInventoryCacheDisposition'] == 'provider-value'
    assert env['observedAt'] == value['inventoryObservedAt']
    assert not any('numericLiterals' in f for f in env['facts'])
    assert value == original


@pytest.mark.parametrize('change', ['identity', 'url', 'source', 'marker', 'clock', 'disposition'])
def test_exact_binding_or_explicit_partial(change):
    value = listing()
    if change == 'identity': value['rawKidder']['listing_key'] = 124
    if change == 'url': value['kidderRawRetention']['listingUrl'] += 'other'
    if change == 'source': value['kidderRawRetention']['sourceKey'] = 'srs'
    if change == 'marker': value['kidderRawRetention']['status'] = 'omitted'
    if change == 'clock': value['kidderRawRetention']['inventoryObservedAt'] = '2026-10-28T00:00:00Z'
    if change == 'disposition': value['kidderRawRetention']['cacheDisposition'] = 'generation_cache'
    env = envelope(value)
    assert env['facts'] == [] and env['captureCompleteness'] == 'partial'


def test_unmarked_legacy_and_shared_fallback():
    value = listing()
    value.pop('kidderRawRetention')
    assert 'cre_public_listing_facts' not in (cre_ingest.to_row(value, {}, None)['extra_facts'] or {})
    value = listing()
    value.pop('id')
    value['url'] = 'https://www.kidder.com/properties/'
    value['rawKidder'].pop('listing_key')
    value['rawKidder'].pop('property_key')
    value['kidderRawRetention'].update(binding='url_only', listingId=None, listingUrl=value['url'])
    assert envelope(value)['facts'] == []


def test_property_key_fallback_is_mapper_bound():
    value = listing()
    value['rawKidder'].pop('listing_key')
    value['id'] = '11'
    value['url'] = 'https://www.kidder.com/listings/11'
    value['kidderRawRetention'].update(listingId='11', listingUrl=value['url'])
    assert envelope(value)['facts']


def test_credential_copy_and_referenced_broker_are_removed():
    value = listing()
    value['rawKidder']['apiKey'] = 'PRIVATE_EXAMPLE'
    value['rawKidder']['brokers'][0]['name'] = 'PRIVATE_EXAMPLE'
    value['brokerIds'] = [0]
    pool = {0: {'name': 'PRIVATE_EXAMPLE', 'email': 'safe@example.test'}}
    original = copy.deepcopy(pool)
    row = cre_ingest.to_row(value, pool, None)
    assert 'PRIVATE_EXAMPLE' not in str(row)
    assert row['contacts'][0]['email'] == 'safe@example.test'
    assert pool == original
    assert row['extra_facts']['cre_public_listing_facts']['captureCompleteness'] == 'partial'


def test_foreign_forged_envelope_is_recomputed():
    value = listing()
    existing = {'schema': 'cre-public-listing-facts/v1', 'sourceKey': 'newmark',
                'codec': 'newmark-rcm/v1', 'facts': [{'label': 'forged'}]}
    value['extraFacts'] = {'cre_public_listing_facts': existing}
    original = copy.deepcopy(value)
    env = envelope(value)
    assert env['sourceKey'] == 'kidder-mathews'
    assert 'forged' not in json.dumps(env)
    assert value == original


def test_dual_pass_different_facts_survive_and_replay_deduplicates():
    a = listing()
    b = listing()
    b['transactionMode'] = 'lease'
    b['rawKidder']['unknown'] = [0, False, 'different']
    x, y = cre_ingest.to_row(a, {}, None), cre_ingest.to_row(b, {}, None)
    merged = cre_ingest.merge_rows(x, y)
    facts = merged['extra_facts']['cre_public_listing_facts']['facts']
    assert len(facts) == 2
    assert [f['value']['feedKey'] for f in facts] == [None, None]
    assert next(f['value'] for f in facts[1]['value']['facts'] if f['label'] == 'unknown') == [0, False, 'different']
    again = cre_ingest.merge_rows(merged, cre_ingest.to_row(b, {}, None))
    assert len(again['extra_facts']['cre_public_listing_facts']['facts']) == 2


def test_overlimit_is_explicit_partial():
    value = listing()
    value['rawKidder']['big'] = 'x' * 17000
    env = envelope(value)
    assert env['captureCompleteness'] == 'partial'
    assert any(item['reason'] == 'input_limit' for item in env['omissions'])


def test_unknown_clock_is_not_fresh_but_facts_retained():
    value = listing()
    value.pop('inventoryObservedAt')
    value.pop('freshnessProvenance')
    value['kidderRawRetention'].update(inventoryObservedAt=None, cacheDisposition=None)
    result = envelope(value)
    assert result['facts'] and result['observedAt'] is None
    assert result['captureCompleteness'] == 'partial'


def test_missing_receipt_provenance_or_wrong_method_refuses_facts():
    value = listing()
    value['kidderRawRetention'].pop('cacheDisposition')
    assert envelope(value)['facts'] == []
    value = listing()
    value['freshnessProvenance']['method'] = 'other'
    assert envelope(value)['facts'] == []


def test_unsafe_canonical_url_refuses_staging():
    value = listing()
    value['url'] = 'https://www.kidder.com/listings/123?api_key=PRIVATE_EXAMPLE'
    value['kidderRawRetention']['listingUrl'] = value['url']
    assert cre_ingest.to_row(value, {}, None) is None


def test_provider_cache_lookalike_does_not_supply_provenance():
    value = listing()
    value['rawKidder']['__creInventoryCacheDisposition'] = 'generation_cache'
    value['kidderRawRetention']['cacheDisposition'] = 'generation_cache'
    assert envelope(value)['facts'] == []


@pytest.mark.parametrize('value', [1.25, 9007199254740992])
def test_ambiguous_numeric_identity_is_partial(value):
    item = listing()
    item['rawKidder']['listing_key'] = value
    result = envelope(item)
    assert result['facts'] == [] and result['captureCompleteness'] == 'partial'


def test_assignment_copy_is_removed_from_source_and_broker():
    value = listing()
    value['rawKidder']['source_note'] = 'apiKey=PRIVATE_EXAMPLE_TOKEN'
    value['rawKidder']['brokers'][0]['name'] = 'PRIVATE_EXAMPLE_TOKEN'
    value['brokerIds'] = [0]
    brokers = {0: {'name': 'PRIVATE_EXAMPLE_TOKEN', 'email': 'safe@example.test'}}
    original = copy.deepcopy(value)
    row = cre_ingest.to_row(value, brokers, None)
    assert 'PRIVATE_EXAMPLE_TOKEN' not in str(row)
    assert row['contacts'][0]['email'] == 'safe@example.test'
    assert row['extra_facts']['cre_public_listing_facts']['captureCompleteness'] == 'partial'
    assert value == original
