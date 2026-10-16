process.argv = [process.argv[0]!, process.argv[1]!];
import test from 'node:test';
import assert from 'node:assert/strict';
import { srcAvisonYoung } from '../../../sources/avison-young.js';
import { firecrawl } from '../../../lib/scrape.js';

test('Avison final fresh producer suppresses hidden asking without losing rent or observation proof', async (t) => {
  const prior = { fetch:globalThis.fetch, scrape:firecrawl.scrape, env:{...process.env} };
  t.after(() => { globalThis.fetch=prior.fetch;firecrawl.scrape=prior.scrape;process.env=prior.env; });
  process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS='1';
  process.env.CRE_REFRESH_GENERATION='synthetic-visibility-generation';
  delete process.env.CRE_REQUIRE_FRESH_DETAILS;
  delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
  globalThis.fetch=async input => {
    const url=new URL(String(input));
    if(url.hostname==='www.avisonyoung.us')return new Response("<script>SharpLaunch.PSE.create('0123456789abcdef0123456789abcdef')</script>");
    assert.equal(url.hostname,'pse-api.sharplaunch.com');
    return new Response(JSON.stringify({items:url.searchParams.get('entity')==='website'?[{
      id:20,status:'active',state:'TX',transaction:['sale','lease'],name:'Synthetic Visibility Property',
      external_url:'https://www.avisonyoung.us/properties/visibility-fixture',hide_price:1,
      sale_price:20,sale_unit_price:20,total_surface_sqft:1000,availabilities_min_rent:20,
    }]:[]}));
  };
  (firecrawl as any).scrape=async(url:string)=>({
    rawHtml:`<script type="application/ld+json">{"@type":"RealEstateListing","name":"Synthetic Visibility Property","url":"${url}"}</script>`,
    markdown:'Retail frontage. Asking price $20. 20 available. Rent $20/SF/YR.',links:[],metadata:{sourceURL:url,statusCode:200},
  });
  const result=await srcAvisonYoung('sale',1,false);const row=result.listings[0];
  assert.equal(row.salePriceUsd,undefined);
  assert.equal(row.saleUnitPrice,undefined);
  assert.equal(row.rawSharpLaunch.sale_price,undefined);
  assert.equal(row.rawSharpLaunch.hide_price,1);
  assert.match(row.markdown,/Rent \$20\/SF\/YR/);
  assert.doesNotMatch(row.markdown,/asking price|20 available/i);
  assert.equal(row.leaseRateMin,20);
  assert.equal(row.freshnessProvenance.generationId,'synthetic-visibility-generation');
  assert.equal(row.detailObservedWithChildPreservation,true);
  assert.ok(row.detailObservedAt);
});

import { readFileSync } from 'node:fs';
import { avisonAskingControl, sanitizeAvisonAskingText, projectAvisonAskingValue, projectAvisonAskingPrice } from '../../../sources/pure/avison-price-visibility.js';
const shared=JSON.parse(readFileSync(new URL('../../fixtures/avison-asking-price-v1.json',import.meta.url),'utf8'));
for(const [index,vector] of shared.vectors.entries()){
  test(`shared asking visibility vector ${index+1}: ${vector.role}`,()=>{
    const evaluate=()=>vector.role==='control'
      ? avisonAskingControl(vector.input?.missing?{}:{hide_price:vector.input})
      : typeof vector.input==='string'
        ? sanitizeAvisonAskingText(vector.input,[20],vector.role)
        : projectAvisonAskingValue(vector.input,[20],vector.role==='identity'?'identity':'');
    if(typeof vector.expected==='string'&&vector.expected.startsWith('REFUSE'))assert.throws(evaluate);
    else assert.deepEqual(evaluate(),vector.expected);
  });
}
test('source-bound hidden projection protects nonprice proofs and both-order transaction unions',()=>{
 const visible={sourceKey:'avison-young',rawSharpLaunch:{hide_price:0,sale_price:20},description:'Asking price $20; Rent $20/SF/YR',salePriceUsd:20,leaseRateMin:20};
 const hidden={...visible,rawSharpLaunch:{hide_price:1,sale_price:20},detailObservedAt:'2026-10-16T22:00:00Z',freshnessProvenance:{generationId:'synthetic-20',bodySha256:'20'.repeat(32)}};
 for(const [a,b] of [[visible,hidden],[hidden,visible]]){
  const out=projectAvisonAskingPrice({sourceKey:'avison-young',primary:a,secondary_pass:b,salePriceUsd:20,leaseRateMin:20});
  assert.equal(out.salePriceUsd,undefined);assert.equal(out.leaseRateMin,20);
  assert.equal(out.avisonPriceVisibility.control,'withheld');
  assert.equal(out.primary.rawSharpLaunch.sale_price,undefined);
  assert.equal(out.secondary_pass.rawSharpLaunch.sale_price,undefined);
 }
 const out=projectAvisonAskingPrice(hidden);
 assert.equal(out.detailObservedAt,hidden.detailObservedAt);
 assert.deepEqual(out.freshnessProvenance,hidden.freshnessProvenance);
 assert.equal(out.rawSharpLaunch.hide_price,1);
 assert.equal(hidden.salePriceUsd,20,'projection must not mutate original');
 assert.throws(()=>projectAvisonAskingPrice({...hidden,sourceKey:'jll'}));
 assert.throws(()=>projectAvisonAskingPrice({...hidden,avisonPriceVisibility:{schema:'avison-asking-price-v1',control:'visible'}}));
});
test('absent and visible projections retain legacy facts except the safe marker',()=>{
 for(const raw of [{},{hide_price:0}]){
  const input={sourceKey:'avison-young',rawSharpLaunch:raw,salePriceUsd:20,newMetadata:{energyScore:20}};
  const {avisonPriceVisibility,...rest}=projectAvisonAskingPrice(input);
  assert.deepEqual(rest,input);assert.equal(avisonPriceVisibility.control,'hide_price' in raw?'visible':'absent');
 }
});

test('Avison monitor, base-only and degraded-detail final emissions stay hidden without new freshness',async(t)=>{
 const prior={fetch:globalThis.fetch,scrape:firecrawl.scrape,env:{...process.env}};
 t.after(()=>{globalThis.fetch=prior.fetch;firecrawl.scrape=prior.scrape;process.env=prior.env;});
 delete process.env.CRE_REQUIRE_FRESH_DETAILS;delete process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS;
 delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
 globalThis.fetch=async()=>{throw new Error('Unexpected network transport: cached synthetic feed required');};
 (firecrawl as any).scrape=async()=>{throw new Error('synthetic unavailable detail');};
 for(const [monitor,limit] of [[true,'0'],[false,'0'],[false,'1']] as const){
  process.env.AVISON_YOUNG_DETAIL_LIMIT=limit;
  const result=await srcAvisonYoung('sale',1,monitor);const row=result.listings[0];
  assert.equal(row.salePriceUsd,undefined);assert.equal(row.rawSharpLaunch.sale_price,undefined);
  assert.equal(row.leaseRateMin,20);assert.equal(row.id,'20');
  assert.equal(row.detailObservedAt,undefined);assert.equal(row.detailObservedWithChildPreservation,undefined);
  assert.equal(row.freshnessProvenance.detailScope,'inventory_only');
  assert.equal(row.avisonPriceVisibility.control,'withheld');
 }
});
test('asking projection preserves resource spans and safe nested asking metadata',()=>{
 assert.equal(sanitizeAvisonAskingText('See https://example.test/$20?id=20 and call 555-123-0020',[20]),'See https://example.test/$20?id=20 and call 555-123-0020');
 assert.deepEqual(projectAvisonAskingValue({askingDetails:{amount:20,newFact:'south-facing'},leaseRateText:'$20'},[20]),{askingDetails:{newFact:'south-facing'},leaseRateText:'$20'});
 assert.throws(()=>avisonAskingControl({hide_price:0,HidePrice:1}));
 const out=projectAvisonAskingPrice({sourceKey:'avison-young',rawSharpLaunch:{hide_price:0},avisonPriceVisibility:{schema:'avison-asking-price-v1',control:'visible'},latestInventoryObservation:{sourceKey:'avison-young',rawSharpLaunch:{hide_price:1,sale_price:20}},description:'Old detail. Asking price $20.',detailObservedAt:'2026-10-15T00:00:00Z'});
 assert.equal(out.description,'Old detail.');assert.equal(out.rawSharpLaunch.hide_price,0);
 assert.equal(out.latestInventoryObservation.rawSharpLaunch.hide_price,1);assert.equal(out.detailObservedAt,'2026-10-15T00:00:00Z');
 assert.throws(()=>projectAvisonAskingPrice({sourceKey:'avison-young',rawSharpLaunch:{hide_price:1},avisonPriceVisibility:{schema:'avison-asking-price-v1',control:'withheld',privateHint:20}}));
});

test('retained own controls, strict envelope presence and harmless generic metadata',()=>{
 const input={sourceKey:'avison-young',rawSharpLaunch:{hide_price:0},avisonPriceVisibility:{schema:'avison-asking-price-v1',control:'visible'},latestInventoryObservation:{sourceKey:'avison-young',rawSharpLaunch:{hide_price:1,sale_price:20},avisonPriceVisibility:{schema:'avison-asking-price-v1',control:'withheld'}},newMetadata:{energy:{value:20,unit:'kWh'}},description:'Old facts. Asking price $20.'};
 const out=projectAvisonAskingPrice(input);
 assert.equal(out.avisonPriceVisibility.control,'withheld');assert.deepEqual(out.newMetadata,input.newMetadata);assert.equal(out.description,'Old facts.');
 assert.throws(()=>projectAvisonAskingPrice({...input,avisonPriceVisibility:null}));
});

test('saved current-observation wrapper projection is idempotent without widening marker authority',()=>{
 const input={sourceKey:'avison-young',rawSharpLaunch:{hide_price:0,sale_price:20},avisonPriceVisibility:{schema:'avison-asking-price-v1',control:'visible'},latestInventoryObservation:{sourceKey:'avison-young',rawSharpLaunch:{hide_price:1,sale_price:20},avisonPriceVisibility:{schema:'avison-asking-price-v1',control:'withheld'}},description:'Old facts. Asking price $20.',detailObservedAt:'2026-10-15T00:00:00Z'};
 const once=projectAvisonAskingPrice(input);
 assert.deepEqual(projectAvisonAskingPrice(once),once);
 assert.equal(once.rawSharpLaunch.hide_price,0);assert.equal(once.detailObservedAt,input.detailObservedAt);
 assert.throws(()=>projectAvisonAskingPrice({...input,latestInventoryObservation:undefined,avisonPriceVisibility:{schema:'avison-asking-price-v1',control:'withheld'}}));
 assert.throws(()=>projectAvisonAskingPrice({...input,latestInventoryObservation:{sourceKey:'avison-young',rawSharpLaunch:{hide_price:0},avisonPriceVisibility:{schema:'avison-asking-price-v1',control:'withheld'}}}));
});

test('resource-key asking words and conflicting explicit asking basis preserve scope',()=>{
 assert.deepEqual(projectAvisonAskingValue({askingBrochureUrl:'https://www.avisonyoung.us/files/20.pdf'},[20]),{askingBrochureUrl:'https://www.avisonyoung.us/files/20.pdf'});
 assert.equal(sanitizeAvisonAskingText('Asking price $20/SF/YR',[20]),'');
 assert.equal(sanitizeAvisonAskingText('offered at $20/SF/YR',[20]),'offered at $20/SF/YR');
});

test('independently typed quantities inside asking containers retain their value',()=>{
 assert.deepEqual(projectAvisonAskingValue({askingDetails:{amount:20,newMetadata:{energy:{value:20,unit:'kWh'},certification:'LEED Gold'}}},[20]),{askingDetails:{newMetadata:{energy:{value:20,unit:'kWh'},certification:'LEED Gold'}}});
});

test('canonical operating revenue roles retain independent numeric and textual values',()=>{
 for(const amount of [20,'$20']) assert.deepEqual(projectAvisonAskingValue({grossRevenue:amount,revpar:amount},[20]),{grossRevenue:amount,revpar:amount});
 assert.deepEqual(projectAvisonAskingValue({grossRevenue:'$20. Asking price $20.',revpar:'$20. Asking price $20.'},[20]),{grossRevenue:'$20.',revpar:'$20.'});
});
test('asking notes cannot reclassify independently typed NOI and rent amounts',()=>{
 assert.deepEqual(projectAvisonAskingValue({noi:{amount:20,currency:'USD',note:'Asking price $20'},lease:{amount:20,basis:'SF/YR',note:'Asking price $20'}},[20]),{noi:{amount:20,currency:'USD'},lease:{amount:20,basis:'SF/YR'}});
 assert.deepEqual(projectAvisonAskingValue({pricing:{amount:20,note:'Asking price $20'}},[20]),{pricing:{}});
});

test('shared explicit currency grammar omits ambiguous clauses and preserves independent money and measurements',()=>{
 for(const money of ['$30','USD 30','30 USD','30 dollars','30 million']) {
  assert.equal(sanitizeAvisonAskingText(`Retail frontage. ${money} available. Zoned retail.`,[20]),'Retail frontage. Zoned retail.');
  assert.equal(sanitizeAvisonAskingText(`Rent ${money}; NOI ${money}`,[20]),`Rent ${money}; NOI ${money}`);
 }
 assert.equal(sanitizeAvisonAskingText('Area 30 million SF. Zoned retail.',[20]),'Area 30 million SF. Zoned retail.');
});

test('parking-space quantity survives without exposing the same known asking number',()=>{
 assert.equal(sanitizeAvisonAskingText('20 parking spaces. Asking price 20.',[20]),'20 parking spaces.');
 assert.equal(sanitizeAvisonAskingText('20 available. Asking price 20.',[20]),'');
});
