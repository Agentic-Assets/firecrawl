import { strict as assert } from "node:assert";
import { test } from "node:test";
import { spawnSync } from "node:child_process";
import { resolveEnricher } from "../../../lib/enrich.js";
import { createNewmarkPublishedDetailEnricher } from "../../../sources/newmark-published-enrich.js";

const literal = '{"name":"Example Property","addresses":{"street":"10 Main Street","city":"Example","state":"FL","zip":"12345"},"number":1.0000000000000001}';
const f = (value: string) => ({state:"value" as const,value});
const missing = {state:"missing" as const};
const item = { sourceKey:"newmark", externalId:"example", url:"https://www.nmrk.com/properties/example",
  sourcePublishedDetail:{status:"observed" as const,schema:"newmark-nim-link-witness/v1" as const,sourceKey:"newmark" as const,
    sourcePath:"raw_data.rawNewmarkNimRecord" as const,recordId:"native-id",slug:"example",
    url:"https://my.rcm1.com/handler/modern.aspx?pv=opaque",inventoryObservedAt:null, sourceName:f("Example Property"),
    sourceProperty:{selection:"single" as const,fields:{address:f("10 Main Street"),city:f("Example"),stateAbbreviation:f("FL"),stateDescription:missing,zip:f("12345"),countryCode:missing}}} };

test("published page is observed with exact original literal and never completes a claim", async () => {
  const called: string[]=[];
  const enricher=createNewmarkPublishedDetailEnricher(async url=>{called.push(url);return {rawHtml:`<script>var data = ${literal}</script>`,markdown:"",links:[]};},()=>"2026-10-29T12:00:00Z");
  const result=await enricher.enrichWithEvidence!([item]); assert.deepEqual(result.listings,[]); assert.deepEqual(called,[item.sourcePublishedDetail.url]);
  const observation=result.retainedObservations[0] as any;
  assert.equal(observation.claim.url,item.url);assert.equal(observation.requestedPageUrl,item.sourcePublishedDetail.url);
  assert.equal(observation.newmarkRcmObservation,true);
  assert.equal(observation.sourceObservation.literal,literal);assert.equal(observation.sourceObservation.match,"matched");
  assert.equal(observation.sourceObservation.numericFidelity,"literal_only");
});

test("source text accepts bounded Unicode and rejects controls",async()=>{
  const unicode={...item,sourcePublishedDetail:{...item.sourcePublishedDetail,sourceName:f("Calle Muñoz")}};
  let calls=0;const e=createNewmarkPublishedDetailEnricher(async()=>{calls++;return {rawHtml:`<script>var data = ${literal}</script>`,markdown:"",links:[]};});
  await e.enrichWithEvidence!([unicode]);assert.equal(calls,1);
  await e.enrichWithEvidence!([{...unicode,sourcePublishedDetail:{...unicode.sourcePublishedDetail,sourceName:f("bad\u0085control")}}]);assert.equal(calls,1);
});

test("unresolved claim never fetches; mismatched content is retained but not promoted",async()=>{
  let calls=0;const e=createNewmarkPublishedDetailEnricher(async()=>{calls++;return {rawHtml:`<script>var data = ${literal}</script>`,markdown:"",links:[]};});
  const unresolved=await e.enrichWithEvidence!([{...item,externalId:"different"}]);assert.equal(calls,0);assert.deepEqual(unresolved.listings,[]);
  const mismatched={...item,sourcePublishedDetail:{...item.sourcePublishedDetail,sourceName:f("Wrong")}};
  const result=await e.enrichWithEvidence!([mismatched]);assert.equal(calls,1);assert.deepEqual(result.listings,[]);
  assert.equal((result.retainedObservations[0] as any).sourceObservation.match,"unresolved");
});

test("only a matched successful response with substantive safe fields emits the unchanged claim",async()=>{
  const data=JSON.parse(literal); data.photos=[{type:"image",imageUrl:"/image.jpg"}];
  const html=`<script>var data = ${JSON.stringify(data)}</script>`;
  const okay=createNewmarkPublishedDetailEnricher(async()=>({rawHtml:html,markdown:"",links:[],metadata:{statusCode:200}}));
  const yes=await okay.enrichWithEvidence!([item]);assert.equal(yes.listings.length,1);
  assert.equal(yes.listings[0].id,item.externalId);assert.equal(yes.listings[0].url,item.url);assert.equal(yes.listings[0].canonicalUrl,item.url);
  assert.equal(yes.listings[0].preserveChildCollections,true);assert.equal(yes.listings[0].photos.length,1);
  const bad=createNewmarkPublishedDetailEnricher(async()=>({rawHtml:html,markdown:"",links:[],metadata:{statusCode:404}}));
  const no=await bad.enrichWithEvidence!([item]);assert.equal(no.listings.length,0);assert.equal((no.retainedObservations[0] as any).sourceObservation.match,"matched");
});

test("sixty independently bound claims qualify without changing their identities or URLs",async()=>{
  const data=JSON.parse(literal);data.photos=[{type:"image",imageUrl:"/image.jpg"}];
  const e=createNewmarkPublishedDetailEnricher(async()=>({rawHtml:`<script>var data = ${JSON.stringify(data)}</script>`,markdown:"",links:[],metadata:{statusCode:200}}));
  const items=Array.from({length:60},(_,i)=>{const slug=`example-${i}`;return {...item,externalId:slug,url:`https://www.nmrk.com/properties/${slug}`,
    sourcePublishedDetail:{...item.sourcePublishedDetail,recordId:`native-${i}`,slug,url:`https://my.rcm1.com/handler/modern.aspx?pv=opaque_${i}`}};});
  const result=await e.enrichWithEvidence!(items);assert.equal(result.listings.length,60);assert.equal(result.retainedObservations.length,60);
  for(let i=0;i<items.length;i++){assert.equal(result.listings[i].id,items[i]!.externalId);assert.equal(result.listings[i].url,items[i]!.url);}
});

test("duplicate keys or source-observation overflow cannot complete a claim",async()=>{
  const duplicate=literal.replace('"name":"Example Property"', '"name":"Wrong","name":"Example Property"');
  const dup=createNewmarkPublishedDetailEnricher(async()=>({rawHtml:`<script>var data = ${duplicate}</script>`,markdown:"",links:[],metadata:{statusCode:200}}));
  const first=await dup.enrichWithEvidence!([item]);assert.equal(first.listings.length,0);
  assert.equal((first.retainedObservations[0] as any).sourceObservation.extraction,"invalid_json");
  assert.equal((first.retainedObservations[0] as any).sourceObservation.literal,duplicate);
  const large=JSON.parse(literal);large.padding="x".repeat(270000);large.photos=[{type:"image",imageUrl:"/image.jpg"}];
  const overflow=createNewmarkPublishedDetailEnricher(async()=>({rawHtml:`<script>var data = ${JSON.stringify(large)}</script>`,markdown:"",links:[],metadata:{statusCode:200}}));
  const second=await overflow.enrichWithEvidence!([item]);assert.equal(second.listings.length,0);
  assert.equal((second.retainedObservations[0] as any).omissions.sourceObservation,"field_byte_limit");
  assert.equal((second.retainedObservations[0] as any).newmarkRcmObservation,true);
});

test("group evidence overflow never completes claims whose observations were dropped",async()=>{
  const data=JSON.parse(literal);data.photos=[{type:"image",imageUrl:"/image.jpg"}];
  const e=createNewmarkPublishedDetailEnricher(async()=>({rawHtml:`<script>var data = ${JSON.stringify(data)}</script>`,markdown:"x".repeat(180000),links:[],metadata:{statusCode:200}}));
  const items=Array.from({length:50},(_,i)=>{const slug=`example-${i}`;return {...item,externalId:slug,url:`https://www.nmrk.com/properties/${slug}`,
    sourcePublishedDetail:{...item.sourcePublishedDetail,recordId:`native-${i}`,slug,url:`https://my.rcm1.com/handler/modern.aspx?pv=opaque_${i}`}};});
  const result=await e.enrichWithEvidence!(items);
  assert.ok(result.evidenceError);assert.ok(result.retainedObservations.length < items.length);
  assert.equal(result.listings.length,result.retainedObservations.length);
});

test("experimental route is explicit and default generic dispatch is preserved",()=>{
  const old=process.env.CRE_NEWMARK_PUBLISHED_DETAIL;
  try {delete process.env.CRE_NEWMARK_PUBLISHED_DETAIL;assert.equal(resolveEnricher("newmark").label,"generic");
    process.env.CRE_NEWMARK_PUBLISHED_DETAIL="1";assert.equal(resolveEnricher("newmark").label,"newmark-published-detail-observation");
  } finally {if(old===undefined) delete process.env.CRE_NEWMARK_PUBLISHED_DETAIL;else process.env.CRE_NEWMARK_PUBLISHED_DETAIL=old;}
});

test("experimental CLI refuses missing private output before reading input or dispatching",()=>{
  const result=spawnSync(process.execPath,["--import","tsx","collect.ts","--enrich-input","/nonexistent-private-claim.json"],
    {cwd:process.cwd(),encoding:"utf8",timeout:15000,env:{...process.env,CRE_NEWMARK_PUBLISHED_DETAIL:"1"}});
  assert.equal(result.status,1);assert.equal(result.stdout,"");
  assert.match(result.stderr,/requires a private --out file/);
});

test('challenge and access denial stop further source dispatch with partial evidence',async()=>{
  const data=JSON.parse(literal);data.photos=[{type:'image',imageUrl:'/image.jpg'}];const html=`<script>var data = ${JSON.stringify(data)}</script>`;let calls=0;let options:any;
  const e=createNewmarkPublishedDetailEnricher(async(_url,opts)=>{options=opts;calls++;return calls===1?{rawHtml:html,markdown:'',links:[],metadata:{statusCode:200}}:{rawHtml:'<title>Checking your browser</title>'+html,markdown:'',links:[],metadata:{statusCode:200}};});
  const out=await e.enrichWithEvidence!([item,item,item]);assert.equal(calls,2);assert.equal(out.listings.length,1);assert.equal(out.retainedObservations.length,3);assert.equal((out.retainedObservations[1] as any).stopReason,'possible_challenge_header');assert.equal((out.retainedObservations[2] as any).failure,'not_attempted_after_possible_challenge_header');assert.equal(options.singleAttempt,true);assert.equal(options.quietErrors,true);
  for(const status of [401,403,429,451]){let hits=0;const d=createNewmarkPublishedDetailEnricher(async()=>{hits++;throw Object.assign(new Error('denied'),{status});});const r=await d.enrichWithEvidence!([item,item]);assert.equal(hits,1);assert.equal(r.listings.length,0);assert.equal((r.retainedObservations[1] as any).failure,'not_attempted_after_access_denial');}
  let returned=0;const d=createNewmarkPublishedDetailEnricher(async()=>{returned++;return {rawHtml:'',markdown:'',links:[],metadata:{statusCode:403}};});const r=await d.enrichWithEvidence!([item,item]);assert.equal(returned,1);assert.equal((r.retainedObservations[1] as any).failure,'not_attempted_after_access_denial');
});

test('unknown settlement stops dispatch and creates explicit not-attempted evidence',async()=>{
 let calls=0;const e=createNewmarkPublishedDetailEnricher(async()=>{calls++;throw new Error('unknown transport');});const r=await e.enrichWithEvidence!([item,item]);assert.equal(calls,1);assert.equal((r.retainedObservations[0] as any).stopReason,'uncertain_settlement');assert.equal((r.retainedObservations[1] as any).failure,'not_attempted_after_uncertain_settlement');
});

test('hidden-only text is not substantive and additive output clears synthetic transaction mode',async()=>{
 const data=JSON.parse(literal);data.projectfields={showprice:true,summary:'<span hidden>not visible</span>'};let html=`<script>var data = ${JSON.stringify(data)}</script>`;const e=createNewmarkPublishedDetailEnricher(async()=>({rawHtml:html,markdown:'',links:[],metadata:{statusCode:200}}));const none=await e.enrichWithEvidence!([item]);assert.equal(none.listings.length,0);assert.equal(none.retainedObservations.length,1);
 const {runEnrichGroups}=await import('../../../lib/enrich.js');const emptyGroup=await runEnrichGroups(new Map([['newmark',[item]]]),()=>({enricher:e,label:'test'}),()=> 'Newmark');assert.equal(emptyGroup.listings.length,0);assert.equal(emptyGroup.sources[0].retainedObservations.length,1);
 data.photos=[{type:'image',imageUrl:'/image.jpg'}];html=`<script>var data = ${JSON.stringify(data)}</script>`;const withFields=await e.enrichWithEvidence!([item]);assert.equal(withFields.listings.length,1);assert.equal(withFields.listings[0].transactionMode,null);
 const result=await runEnrichGroups(new Map([['newmark',[item]]]),()=>({enricher:e,label:'test'}),()=> 'Newmark');assert.equal(result.listings.length,1);assert.equal(Object.hasOwn(result.listings[0],'transactionMode'),false);
});
