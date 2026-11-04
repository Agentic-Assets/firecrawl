import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { srcSrs } from '../../../sources/srs.js';
import { srcKidderMathews } from '../../../sources/kidder-mathews.js';

test('capture-off fresh and cache provenance ignores colliding provider fields and preserves them', async () => {
  const original=globalThis.fetch;
  delete process.env.CRE_NATIVE_JSON_FEED_CAPTURE_DIR;
  process.env.CRE_REQUIRE_FRESH_DETAILS='1';process.env.CRE_REFRESH_GENERATION='private-provenance-fixture';
  let calls=0;
  try {
    globalThis.fetch=(async (url:unknown)=>{
      calls++;
      if(String(url).includes('srsre-next-')) return new Response(JSON.stringify({total:1,properties:[{
        __creInventoryCacheDisposition:'generation_cache',permalink:'/sale/fixture',
        apto_data:{SRS_Listings_ID__c:'srs-id',Name:'Example',Availability__c:'Sale'}}]}));
      return new Response(JSON.stringify({startIndex:0,totalResultCount:1,currentResultCount:1,results:[{
        listing_key:'kidder-id',property_name:'Example',__creInventoryCacheDisposition:'source_revision_cache'}]}));
    }) as typeof fetch;
    const s1=await srcSrs('sale',1,false);const s2=await srcSrs('sale',1,false);
    const k1=await srcKidderMathews('sale',1,false);const k2=await srcKidderMathews('sale',1,false);
    assert.equal(calls,2);
    for(const [first,cached,key,provider] of [[s1,s2,'rawSrs','generation_cache'],[k1,k2,'rawKidder','source_revision_cache']] as const){
      assert.equal(first.listings[0].freshnessProvenance.cacheDisposition,'live');
      assert.equal(cached.listings[0].freshnessProvenance.cacheDisposition,'generation_cache');
      assert.equal(first.listings[0][key].__creInventoryCacheDisposition,provider);
      assert.equal(cached.listings[0][key].__creInventoryCacheDisposition,provider);
      assert.equal(first.listings[0].id,cached.listings[0].id);
    }
  } finally {globalThis.fetch=original;delete process.env.CRE_REQUIRE_FRESH_DETAILS;delete process.env.CRE_REFRESH_GENERATION;}
});
