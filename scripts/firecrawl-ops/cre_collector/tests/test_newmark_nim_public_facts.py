import copy
import json

import pytest

import cre_ingest
from source_fields import newmark_nim_public_facts as bridge


def listing(tx='sale'):
    clock = '2026-10-29T00:00:00Z'
    url = 'https://www.nmrk.com/properties/example'
    return {'sourceKey': 'newmark', 'id': 'example', 'url': url, 'transactionMode': tx,
            'inventoryObservedAt': clock,
            'freshnessProvenance': {'detailScope': 'authoritative_inventory_feed',
                                    'method': 'newmark_nim_public_inventory', 'cacheDisposition': 'live'},
            'newmarkNimRawRetention': {'schema': 'newmark-nim-raw-retention-v1', 'status': 'complete',
                                       'sourceKey': 'newmark', 'binding': 'id_and_url', 'transactionMode': tx,
                                       'listingId': 'example', 'listingUrl': url, 'providerId': 'native-123',
                                       'inventoryObservedAt': clock, 'cacheDisposition': 'live'},
            'rawNewmarkNimRecord': {'id': 'native-123', 'slug': 'example', 'externalWebsiteUrl': url,
                                    'unknown': [None, False, '', 0, []], 'properties': [{'name': 'source'}]}}


def envelope(value):
    return cre_ingest.to_row(value, {}, None, allow_newmark_nim=True)['extra_facts'][bridge.SLOTS[value['transactionMode']]]


def test_source_fields_and_receipt_identity():
    value = listing()
    original = copy.deepcopy(value)
    env = envelope(value)
    assert env['codec'] == bridge.CODEC and env['transactionMode'] == 'sale'
    assert env['observedAt'] == value['inventoryObservedAt']
    assert next(f['value'] for f in env['facts'] if f['label'] == 'unknown') == [None, False, '', 0, []]
    assert not any('numericLiterals' in f for f in env['facts'])
    assert value == original


@pytest.mark.parametrize('change', ['id', 'url', 'provider', 'transaction', 'clock', 'cache', 'scope'])
def test_mismatched_receipt_is_partial_without_facts(change):
    value = listing()
    marker = value['newmarkNimRawRetention']
    if change == 'id': value['rawNewmarkNimRecord']['slug'] = 'different'; value['rawNewmarkNimRecord'].pop('externalWebsiteUrl')
    if change == 'url': marker['listingUrl'] += 'different'
    if change == 'provider': marker['providerId'] = 'different'
    if change == 'transaction': marker['transactionMode'] = 'lease'
    if change == 'clock': marker['inventoryObservedAt'] = '2026-10-28T00:00:00Z'
    if change == 'cache': marker['cacheDisposition'] = 'generation_cache'
    if change == 'scope': marker['sourceKey'] = 'srs'
    result = envelope(value)
    assert result['facts'] == [] and result['captureCompleteness'] == 'partial'


def test_independent_slots_and_other_source_rcm_are_preserved():
    value = listing()
    old = {'schema': bridge.SCHEMA, 'codec': 'newmark-rcm-enrichment-withholding/v1', 'sourceKey': 'newmark', 'facts': []}
    value['extraFacts'] = {'cre_public_listing_facts': old, bridge.SLOTS['lease']: {'previous': 'lease'}}
    result = cre_ingest.to_row(value, {}, None, allow_newmark_nim=True)['extra_facts']
    assert result['cre_public_listing_facts'] == old
    assert bridge.SLOTS['lease'] not in result
    assert result[bridge.SLOTS['sale']]['transactionMode'] == 'sale'


def test_direct_or_enrich_cannot_publish_stale_nim_but_still_redacts():
    value = listing()
    value['rawNewmarkNimRecord']['apiKey'] = 'PRIVATE_EXAMPLE'
    value['description'] = 'PRIVATE_EXAMPLE'
    value['extraFacts'] = {bridge.SLOTS['sale']: {'stale': True}, 'ordinary': 'retained'}
    row = cre_ingest.to_row(value, {}, None)
    assert 'PRIVATE_EXAMPLE' not in str(row)
    assert bridge.SLOTS['sale'] not in row['extra_facts']
    assert row['extra_facts']['ordinary'] == 'retained'


def test_legacy_unmarked_is_untouched():
    value = listing()
    value.pop('newmarkNimRawRetention')
    assert not (cre_ingest.to_row(value, {}, None)['extra_facts'] or {})


def test_document_reference_token_and_broker_copy_removed():
    value = listing()
    doc = 'https://files.example.test/doc?token=PRIVATE_DOCUMENT_TOKEN'
    value['rawNewmarkNimRecord']['secureDocumentsUrl'] = doc
    value['rawNewmarkNimRecord']['copy'] = 'PRIVATE_DOCUMENT_TOKEN'
    value['brokerIds'] = [0]
    pool = {0: {'name': 'PRIVATE_DOCUMENT_TOKEN', 'email': 'safe@example.test'}}
    row = cre_ingest.to_row(value, pool, None, allow_newmark_nim=True)
    assert 'PRIVATE_DOCUMENT_TOKEN' not in str(row)
    assert row['contacts'][0]['email'] == 'safe@example.test'
    assert row['extra_facts'][bridge.SLOTS['sale']]['captureCompleteness'] == 'partial'


def test_percent_identity_and_unsafe_identity():
    value = listing()
    value['rawNewmarkNimRecord']['externalWebsiteUrl'] = 'https://nmrk.com/properties/%65xample'
    value['url'] = 'https://www.nmrk.com/properties/%65xample'
    value['newmarkNimRawRetention']['listingUrl'] = value['url']
    assert envelope(value)['facts']
    value['url'] = 'https://www.nmrk.com/properties/example?api_key=SECRET_TOKEN'
    assert cre_ingest.to_row(value, {}, None, allow_newmark_nim=True) is None


def test_input_limit_is_explicit():
    value = listing()
    value['rawNewmarkNimRecord']['big'] = 'x' * 17000
    env = envelope(value)
    assert env['captureCompleteness'] == 'partial'
    assert any(item['reason'] == 'input_limit' for item in env['omissions'])


@pytest.mark.parametrize('mode', ['full', 'enrich'])
def test_actual_cli_mode_gates_inventory_slot_and_sanitizes(tmp_path, mode):
    import os
    import re
    import subprocess
    import sys

    value = listing()
    value['rawNewmarkNimRecord']['apiKey'] = 'PRIVATE_EXAMPLE_MATERIAL'
    value['description'] = 'PRIVATE_EXAMPLE_MATERIAL'
    value['extraFacts'] = {bridge.SLOTS['sale']: {'stale': True}}
    artifact = {'runMeta': {'mode': mode, 'finishedAt': '2026-10-29T00:00:00Z'},
                'sources': [{'sourceKey': 'newmark'}], 'listings': [value], 'brokers': [], 'totalListings': 1}
    path = tmp_path / 'input.json'
    original = json.dumps(artifact).encode()
    path.write_bytes(original)
    result = subprocess.run([sys.executable, 'cre_ingest.py', '--in', str(path), '--dry-run'],
                            text=True, capture_output=True, env={**os.environ, 'TMPDIR': str(tmp_path)}, check=True)
    match = re.search(r'^sql: (.+?) \(', result.stderr, re.MULTILINE)
    assert match is not None
    with open(match.group(1)) as staged:
        sql = staged.read()
    assert 'PRIVATE_EXAMPLE_MATERIAL' not in sql
    assert (bridge.SLOTS['sale'] in sql) is (mode == 'full')
    assert path.read_bytes() == original


def test_sale_and_lease_merge_preserve_each_independent_slot():
    sale = listing('sale')
    lease = listing('lease')
    a = cre_ingest.to_row(sale, {}, None, allow_newmark_nim=True)
    b = cre_ingest.to_row(lease, {}, None, allow_newmark_nim=True)
    merged = cre_ingest.merge_rows(a, b)
    assert merged['extra_facts'][bridge.SLOTS['sale']]['transactionMode'] == 'sale'
    assert merged['extra_facts'][bridge.SLOTS['lease']]['transactionMode'] == 'lease'


def test_noncredential_document_link_and_source_controls_are_retained():
    value = listing()
    value['rawNewmarkNimRecord'].update(secureDocumentsUrl='https://files.example.test/public.pdf',
                                         showPrice=False, priceSummary='123', emptyArray=[])
    result = envelope(value)
    facts = {item['label']: item['value'] for item in result['facts']}
    assert facts['secureDocumentsUrl'] == 'https://files.example.test/public.pdf'
    assert facts['showPrice'] is False and facts['priceSummary'] == '123'
    assert facts['emptyArray'] == []


def test_identity_url_credential_copies_removed():
    value = listing()
    value['rawNewmarkNimRecord']['externalWebsiteUrl'] += '?api_key=PRIVATE_IDENTITY_TOKEN'
    value['rawNewmarkNimRecord']['copy'] = 'PRIVATE_IDENTITY_TOKEN'
    result = cre_ingest.to_row(value, {}, None, allow_newmark_nim=True)
    assert 'PRIVATE_IDENTITY_TOKEN' not in str(result)
    assert result['extra_facts'][bridge.SLOTS['sale']]['facts']
    assert result['extra_facts'][bridge.SLOTS['sale']]['captureCompleteness'] == 'partial'


def test_unmarked_input_cannot_inject_nim_slot():
    value = listing()
    value.pop('newmarkNimRawRetention')
    value['extraFacts'] = {bridge.SLOTS['sale']: {'forged': True}, 'ordinary': 'retained'}
    original = copy.deepcopy(value)
    result = cre_ingest.to_row(value, {}, None, allow_newmark_nim=True)['extra_facts']
    assert bridge.SLOTS['sale'] not in result
    assert result['ordinary'] == 'retained'
    assert value == original


@pytest.mark.parametrize('provider_value', [None, '', '   ', 123, 1.25])
def test_nullable_provider_id_preserves_source_value_with_canonical_identity(provider_value):
    value = listing()
    value['rawNewmarkNimRecord']['id'] = provider_value
    value['newmarkNimRawRetention']['providerId'] = None
    result = envelope(value)
    fact = next(item for item in result['facts'] if item['label'] == 'id')
    assert fact['value'] == provider_value
    assert not any('numericLiterals' in item for item in result['facts'])


def test_missing_provider_id_can_bind_canonical_identity():
    value = listing()
    value['rawNewmarkNimRecord'].pop('id')
    value['newmarkNimRawRetention']['providerId'] = None
    result = envelope(value)
    assert result['facts'] and all(item['label'] != 'id' for item in result['facts'])


def test_nullable_provider_id_mismatch_is_partial():
    value = listing()
    value['rawNewmarkNimRecord']['id'] = 123
    value['newmarkNimRawRetention']['providerId'] = '123'
    result = envelope(value)
    assert result['facts'] == [] and result['captureCompleteness'] == 'partial'


@pytest.mark.parametrize('origin', ['https://NMRK.COM:443/properties/%65xample',
                                    'https://WWW.NMRK.COM/properties/%65xample'])
def test_mapper_canonicalizes_host_and_default_port(origin):
    value = listing()
    value['rawNewmarkNimRecord']['externalWebsiteUrl'] = origin
    value['url'] = 'https://www.nmrk.com/properties/%65xample'
    value['newmarkNimRawRetention']['listingUrl'] = value['url']
    assert envelope(value)['facts']


def test_redacted_receipt_or_source_identity_cannot_attest_projection():
    value = listing()
    value['rawNewmarkNimRecord']['apiKey'] = 'native-123'
    row = cre_ingest.to_row(value, {}, None, allow_newmark_nim=True)
    env = row['extra_facts'][bridge.SLOTS['sale']]
    assert 'native-123' not in str(row)
    assert env['facts'] == [] and env['observedAt'] is None
    assert env['captureCompleteness'] == 'partial'


def test_unknown_clock_preserves_facts_with_partial_notice():
    value = listing()
    value.pop('inventoryObservedAt')
    value.pop('freshnessProvenance')
    value['newmarkNimRawRetention'].update(inventoryObservedAt=None, cacheDisposition=None)
    result = envelope(value)
    assert result['facts'] and result['observedAt'] is None
    assert result['captureCompleteness'] == 'partial'
