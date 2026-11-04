"""SRS API price currency must not be inferred by canonical staging."""
import json
import os
import subprocess

from cre_ingest import to_row


def test_actual_srs_mapper_to_ingest_preserves_amount_without_usd():
    script = r'''
import {mapSrsListing} from './sources/srs.ts';
const inputs = [
  {id:'positive', price:1500000, flag:true, tx:'sale'},
  {id:'zero', price:0, flag:false, tx:'sale'},
  {id:'text', price:'123456', flag:true, tx:'sale'},
  {id:'null', price:null, flag:true, tx:'sale'},
  {id:'lease', price:123456, flag:true, tx:'lease'},
];
const rows = inputs.map(x => {
  const listing = mapSrsListing({id:x.id,permalink:`/properties/${x.tx}/${x.id}`,
    apto_data:{SRS_Listings_ID__c:x.id,Availability__c:x.tx==='sale'?'Sale':'Lease',
      Sale_Price__c:x.price,Hide_Sale_Price__c:x.flag}},x.tx);
  return {...listing,sourceKey:'srs',transactionMode:x.tx};
});
process.stdout.write(JSON.stringify(rows));
'''
    proc = subprocess.run(['node', '--import', 'tsx', '--input-type=module', '-e', script],
                          capture_output=True, text=True, check=True, timeout=30,
                          env={**os.environ, 'CRE_REQUIRE_FRESH_DETAILS': '0'})
    listings = json.loads(proc.stdout)
    rows = [to_row(item, {}, '2026-10-29T12:00:00Z') for item in listings]
    assert all(row['sale_price_usd'] is None for row in rows)
    assert rows[0]['raw_data']['salePriceText'] == '1,500,000'
    assert rows[1]['raw_data']['salePriceText'] == '0'
    assert rows[2]['raw_data']['salePriceText'] == '123,456'
    assert 'salePriceText' not in rows[3]['raw_data']
    assert 'salePriceText' not in rows[4]['raw_data']
    assert rows[0]['extra_facts']['srs_reported_sale_price']['reported_number'] == 1500000
    assert rows[1]['extra_facts']['srs_reported_sale_price']['reported_number'] == 0
    assert rows[2]['extra_facts']['srs_reported_sale_price']['reported_text'] == '123456'
    assert rows[3]['extra_facts']['srs_reported_sale_price']['price_state'] == 'null'


def test_other_source_existing_usd_normalization_is_unchanged():
    listing = {'sourceKey':'kidder-mathews', 'id':'x', 'url':'https://example.test/x',
               'transactionMode':'sale', 'salePriceUsd':500000, 'salePriceText':'500,000'}
    assert to_row(listing, {}, '2026-10-29T12:00:00Z')['sale_price_usd'] == 500000


def test_nai_existing_text_recovery_is_unchanged():
    listing = {'sourceKey':'nai-global', 'id':'nai-1', 'url':'https://example.test/nai',
               'transactionMode':'sale', 'salePriceText':'POUND 500,000'}
    assert to_row(listing, {}, '2026-10-29T12:00:00Z')['sale_price_usd'] == 500000
