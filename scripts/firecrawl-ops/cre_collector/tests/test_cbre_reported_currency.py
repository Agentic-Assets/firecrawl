"""Currency-bound numeric staging retains unqualified source price text."""
import pytest

import cre_ingest


@pytest.mark.parametrize('currency_basis, expected', [
    ('unverified_or_conflicting', None), ('source_currency_usd', 125.0),
])
def test_reported_per_square_foot_price_needs_usd_binding(currency_basis, expected):
    listing = {'sourceKey': 'cbre-dealflow', 'id': 'synthetic',
               'url': 'https://www.cbredealflow.com/property/synthetic',
               'transactionMode': 'sale', 'salePriceText': 'USD $125/sf',
               'extraFacts': {'cbre_dealflow_reported_price': {'source': 'cbre_dealflow_rcm', 'source_field': 'projectfields.value', 'reportedTextJson': '"$125/sf"', 'currencyBasis': currency_basis, 'currency': 'USD', 'symbol': '$'}}}
    row = cre_ingest.to_row(listing, {}, '2026-10-29T00:00:00Z')
    assert row['sale_price_per_sf'] == expected
    assert row['raw_data']['salePriceText'] == 'USD $125/sf'


def test_legacy_cbre_artifact_keeps_existing_per_square_foot_handling():
    listing = {'sourceKey': 'cbre-dealflow', 'id': 'synthetic',
               'url': 'https://www.cbredealflow.com/property/synthetic',
               'transactionMode': 'sale', 'salePriceText': '$125/sf'}
    assert cre_ingest.to_row(listing, {}, '2026-10-29T00:00:00Z')['sale_price_per_sf'] == 125.0


def test_non_object_extra_facts_preserve_legacy_handling():
    listing = {'sourceKey': 'cbre-dealflow', 'id': 'synthetic',
               'url': 'https://www.cbredealflow.com/property/synthetic',
               'transactionMode': 'sale', 'salePriceText': '$125/sf', 'extraFacts': ['unexpected']}
    assert cre_ingest.to_row(listing, {}, '2026-10-29T00:00:00Z')['sale_price_per_sf'] == 125.0


@pytest.mark.parametrize('currency,symbol', [('CAD', '$'), ('USD', '€'), (None, '$')])
def test_contradictory_source_currency_cannot_assert_usd(currency, symbol):
    listing = {'sourceKey': 'cbre-dealflow', 'id': 'synthetic',
               'url': 'https://www.cbredealflow.com/property/synthetic',
               'transactionMode': 'sale', 'salePriceText': '$125/sf',
               'extraFacts': {'cbre_dealflow_reported_price': {
                   'currencyBasis': 'source_currency_usd', 'currency': currency, 'symbol': symbol}}}
    assert cre_ingest.to_row(listing, {}, '2026-10-29T00:00:00Z')['sale_price_per_sf'] is None


@pytest.mark.parametrize('override', [
    {'source': 'other'}, {'source_field': 'unrelated'}, {'reportedTextJson': '"$999/sf"'},
    {'reportedTextJson': 'null'}, {'reportedTextJson': 'bad'},
])
def test_cbre_per_sf_witness_requires_exact_source_and_value(override):
    context = {'source': 'cbre_dealflow_rcm', 'source_field': 'projectfields.value',
               'reportedTextJson': '"$125/sf"', 'currencyBasis': 'source_currency_usd',
               'currency': 'USD', 'symbol': '$'}
    context.update(override)
    row = cre_ingest.to_row({'sourceKey': 'cbre-dealflow', 'id': 'synthetic',
        'url': 'https://example.test/p', 'salePriceText': 'USD $125/sf',
        'extraFacts': {'cbre_dealflow_reported_price': context}}, {}, None)
    assert row['sale_price_per_sf'] is None


def test_rcm_partial_contacts_preserve_fields_without_inventing_company():
    for source, marker in [('cbre-dealflow', 'cbre_dealflow_rcm'), ('colliers', 'colliers_sales_tracker')]:
        row = cre_ingest.to_row({'sourceKey': source, 'id': 'synthetic', 'url': 'https://example.test/p',
             'sourceCompany': 'Host brokerage', 'contactsDetailed': [
                 {'title': 'Broker', 'license': 'ABC', 'rcmContactSource': marker},
                 {'name': 'Person', 'rcmContactSource': marker}]}, {}, None)
        assert len(row['contacts']) == 1 and row['contacts'][0]['company'] is None
        assert row['contacts'][0]['isPrimary'] is True
        assert row['extra_facts']['rcm_unidentified_source_contacts'][0]['license'] == 'ABC'


@pytest.mark.parametrize('source', ['cbre-dealflow', 'colliers'])
def test_legacy_identityless_contact_does_not_create_an_unstable_child(source):
    contact = {'company': 'Reported company', 'title': 'Property contact'}
    row = cre_ingest.to_row({'sourceKey': source, 'id': 'synthetic',
        'url': 'https://example.test/p', 'sourceCompany': 'Host brokerage',
        'contactsDetailed': [contact]}, {}, None)
    assert row['contacts'] == []
    assert row['raw_data']['contactsDetailed'] == [contact]
