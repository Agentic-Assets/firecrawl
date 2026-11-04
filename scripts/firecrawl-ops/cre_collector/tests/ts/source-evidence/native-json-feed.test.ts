import { strict as assert } from 'node:assert';
import { mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { gunzipSync } from 'node:zlib';
import { test } from 'node:test';
import { NativeJsonFeedCaptureError } from '../../../lib/source-evidence/native-json-feed.js';
import { srsFetchAll, srcSrs } from '../../../sources/srs.js';
import { kidderFetchCompleteInventory, kidderFetchAll, srcKidderMathews } from '../../../sources/kidder-mathews.js';
import { srcHanley, enrichHanleyListings } from '../../../sources/hanley.js';

function readEntries(dir: string): any[] {
  return readdirSync(dir).map(name => JSON.parse(readFileSync(join(dir,name),'utf8')));
}
function body(entry: any): any {
  return JSON.parse(gunzipSync(Buffer.from(entry.responseBodyBase64,'base64')).toString('utf8'));
}
function sourceDir(root: string, name: string): string {
  const dir = join(root,name); mkdirSync(dir,{mode:0o700});
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_DIR = dir;
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SOURCE_KEY = name;
  return dir;
}

test('actual source paths retain >50 prefilter rows, typed failures stop retries, and source metadata is not overwritten', async () => {
  const root = mkdtempSync(join(tmpdir(),'native-json-feed-'));
  const originalFetch = globalThis.fetch;
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SINGLE_WRITER = '1';
  process.env.CRE_REQUIRE_FRESH_DETAILS = '1'; process.env.CRE_REFRESH_GENERATION = 'synthetic-generation';
  const rows = Array.from({length:24},(_,i) => ({permalink:`/sale/${i}`, __creInventoryCacheDisposition:'source_literal',
    apto_data:{SRS_Listings_ID__c:String(i),Name:`Synthetic ${i}`,Availability__c:'Sale',values:[false,null,'',{},[],i]}}));
  let calls = 0;
  try {
    const failedDir = sourceDir(root,'srs');
    process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_PAGES='1';
    globalThis.fetch = (async (_url: unknown, options: RequestInit) => {
      calls++; const request = JSON.parse(String(options.body)); const offset = request.query.offset;
      return new Response(JSON.stringify({total:24, properties:rows.slice(offset,offset+12), envelope:{flag:false,empty:null}}),{status:200});
    }) as typeof fetch;
    await assert.rejects(srsFetchAll(Infinity),NativeJsonFeedCaptureError);
    assert.equal(calls,2); assert.equal(readdirSync(failedDir).length,1);
    delete process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_PAGES;
    const srsDir = join(root,'srs-next'); mkdirSync(srsDir,{mode:0o700}); process.env.CRE_NATIVE_JSON_FEED_CAPTURE_DIR=srsDir;
    const first=await srsFetchAll(Infinity); assert.equal(first.items.length,24);
    const cached=await srsFetchAll(Infinity); assert.equal(cached.items[0].__creInventoryCacheDisposition,'source_literal');
    const result=await srcSrs('sale',100,false);
    assert.equal(result.listings[0].freshnessProvenance.cacheDisposition,'generation_cache');
    assert.equal(result.listings[0].rawSrs.__creInventoryCacheDisposition,'source_literal');
    const saved=readEntries(srsDir).map(body); assert.equal(saved.length,2);
    assert.deepEqual(saved[0].properties[0].apto_data.values,[false,null,'',{},[],0]);
    assert.equal(saved[0].envelope.flag,false);

    const kidderDir=sourceDir(root,'kidder-mathews');
    const kidder=Array.from({length:60},(_,i)=>({listing_key:String(i),__creInventoryCacheDisposition:'source_literal',raw:[false,null,'',{},[]]}));
    const fake: typeof fetch=async()=>new Response(JSON.stringify({startIndex:0,totalResultCount:60,currentResultCount:60,results:kidder}),{status:200});
    globalThis.fetch=fake;
    assert.equal((await kidderFetchAll(Infinity)).items.length,60);
    assert.equal((await kidderFetchAll(Infinity)).items[0].__creInventoryCacheDisposition,'source_literal');
    const mappedKidder=await srcKidderMathews('sale',100,false);
    assert.equal(mappedKidder.listings[0].freshnessProvenance.cacheDisposition,'generation_cache');
    assert.equal(mappedKidder.listings[0].rawKidder.__creInventoryCacheDisposition,'source_literal');
    assert.deepEqual(body(readEntries(kidderDir)[0]).results[0].raw,[false,null,'',{},[]]);

    const hanleyDir=sourceDir(root,'hanley');
    const hanley=Array.from({length:31},(_,i)=>({id:String(i),name:`H${i}`,visibility:i===30?'Private':'Public',dealRecordType:'Seller_Rep',extra:[false,null,'',{},[]]}));
    const html=`<script>var rethink_properties = ${JSON.stringify(hanley)};</script><div id="rethink-properties-container">${hanley.slice(0,30).map(x=>`<article data-id="${x.id}" data-result="true"></article>`).join('')}</div>`;
    globalThis.fetch=(async()=>new Response(html,{status:200})) as typeof fetch;
    const h=await srcHanley('sale',100,false); assert.equal(h.listings.length,30);
    const entry=readEntries(hanleyDir)[0]; const original=body(entry);
    assert.equal(original.length,31);assert.equal(original[30].visibility,'Private');
    assert.deepEqual(original[0].extra,[false,null,'',{},[]]);
    assert.equal(entry.provenance.representation,'collector_decoded_html_embedded_json_literal_not_wire');
    process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_BYTES='1';
    let hanleyCalls=0; globalThis.fetch=(async()=>{hanleyCalls++;return new Response(html,{status:200});}) as typeof fetch;
    await assert.rejects(enrichHanleyListings([]),NativeJsonFeedCaptureError);
    assert.equal(hanleyCalls,1, 'capture failure must not trigger fallback');
    delete process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_BYTES;
  } finally {
    globalThis.fetch=originalFetch;
    for(const key of ['DIR','SOURCE_KEY','SINGLE_WRITER','MAX_PAGES','MAX_BYTES']) delete process.env[`CRE_NATIVE_JSON_FEED_CAPTURE_${key}`];
    delete process.env.CRE_REQUIRE_FRESH_DETAILS;delete process.env.CRE_REFRESH_GENERATION;
    rmSync(root,{recursive:true,force:true});
  }
});


test('invalid strict Kidder JSON is saved before parse without a retry', async () => {
  const root=mkdtempSync(join(tmpdir(),'native-json-invalid-'));
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SINGLE_WRITER='1';
  const dir=sourceDir(root,'kidder-mathews');let calls=0;
  try {
    await assert.rejects(kidderFetchCompleteInventory(async()=>{calls++;return new Response('{broken',{status:200});}),SyntaxError);
    assert.equal(calls,1);
    const entries=readEntries(dir);assert.equal(entries.length,1);
    assert.equal(gunzipSync(Buffer.from(entries[0].responseBodyBase64,'base64')).toString('utf8'),'{broken');
  } finally {
    for(const key of ['DIR','SOURCE_KEY','SINGLE_WRITER']) delete process.env[`CRE_NATIVE_JSON_FEED_CAPTURE_${key}`];
    rmSync(root,{recursive:true,force:true});
  }
});
