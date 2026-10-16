import "../../fixtures/daum-offline-deny.mjs";

import test from "node:test";
import assert from "node:assert/strict";
import {srcDaumCommercial} from "../../../sources/daum-commercial.js";
import {observationNow} from "../../../lib/observation-clock.js";
import {daumProofPage,daumProofDetail} from "../../fixtures/daum-proof-pages.js";

test("DAUM strict full producer binds both passes to one complete fresh native/path cohort",async(t)=>{
 const saved={...process.env};
 Object.assign(process.env,{CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"daum-proof-test",CRE_REFRESH_STARTED_AT:observationNow()});
 t.after(()=>{for(const k of Object.keys(process.env))if(!(k in saved))delete process.env[k];Object.assign(process.env,saved);});
 const calls:string[]=[];
 const originalFetch=globalThis.fetch,originalTimer=globalThis.setTimeout;
 t.after(()=>{globalThis.fetch=originalFetch;globalThis.setTimeout=originalTimer;});
 globalThis.setTimeout=((fn:any,ms?:number,...args:any[])=>originalTimer(fn,ms&&ms>0&&ms<=3000?0:ms,...args)) as typeof setTimeout;
 globalThis.fetch=async(input,init)=>{
  const url=String(input);calls.push((init?.method??"GET")+" "+url);
  if(init?.method==="POST" && /^https:\/\/daumcommercial[.]com\/property-search\/(?:page\/2\/)?$/.test(url))return new Response(daumProofPage(100,url.endsWith("/page/2/")?2:1));
  const match=url.match(/^https:\/\/daumcommercial[.]com\/property\/Synthetic-(\d+)\/$/);
  if(init?.method==="GET" && match)return new Response(daumProofDetail(Number(match[1])));
  throw new Error("UNEXPECTED_OFFLINE_URL_NO_FALLBACK");
 };
 const sale=await srcDaumCommercial("sale",Infinity,false);
 const lease=await srcDaumCommercial("lease",Infinity,false);
 assert.equal(sale.currentInventoryProof?.schema,"daum-strict-detail-v1");
 assert.equal(calls.filter(x=>x.startsWith("POST")).length,4);
 assert.equal(calls.filter(x=>x.startsWith("GET")).length,100);
 assert.equal(sale.listings.length,67);assert.equal(lease.listings.length,66);
 const sp=sale.currentInventoryProof,lp=lease.currentInventoryProof;
 if(sp?.schema!=="daum-strict-detail-v1"||lp?.schema!=="daum-strict-detail-v1")throw Error("missing typed proof");
 assert.deepEqual(sp.passes,lp.passes);
 assert.equal(sale.truncated,false);assert.equal(lease.truncated,false);
 assert.equal(sale.listings[0].id,"property/Synthetic-0");
 assert.equal(sale.listings[0].daumPostId,"50000");
 const cached=await srcDaumCommercial("sale",Infinity,false);
 assert.equal(calls.length,104);
 if(cached.currentInventoryProof?.schema!=="daum-strict-detail-v1")throw Error("missing typed proof");
 cached.currentInventoryProof.passes[0].total=1;
 const immutable=await srcDaumCommercial("sale",Infinity,false);
 assert.equal(immutable.currentInventoryProof?.schema,"daum-strict-detail-v1");
 process.env.CRE_REFRESH_STARTED_AT=observationNow();
 await srcDaumCommercial("sale",Infinity,false);
 assert.equal(calls.length,208,"a different exact start cannot reuse prior receipts");
 const limited=await srcDaumCommercial("sale",1,false);
 assert.equal(limited.currentInventoryProof,undefined);
 assert.equal(limited.listings.length,1);
 const monitor=await srcDaumCommercial("sale",1,true);
 assert.equal(monitor.currentInventoryProof,undefined);
 process.env.CRE_REFRESH_GENERATION="";
 const unbound=await srcDaumCommercial("sale",1,false);
 assert.equal(unbound.currentInventoryProof,undefined);
});

for(const fault of ["missingCanonical","conflictingTenure","duplicateNative","unknownBoth"] as const){
 test("DAUM strict producer refuses "+fault+" and does not cache failed cohorts",async(t)=>{
  const saved={...process.env},originalFetch=globalThis.fetch,originalTimer=globalThis.setTimeout;
  t.after(()=>{for(const k of Object.keys(process.env))if(!(k in saved))delete process.env[k];Object.assign(process.env,saved);globalThis.fetch=originalFetch;globalThis.setTimeout=originalTimer;});
  Object.assign(process.env,{CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"fault-"+fault,CRE_REFRESH_STARTED_AT:observationNow()});
  globalThis.setTimeout=((fn:any,ms?:number,...args:any[])=>originalTimer(fn,ms&&ms>0&&ms<=3000?0:ms,...args)) as typeof setTimeout;
  let posts=0,broken=true;
  globalThis.fetch=async(input,init)=>{
   const url=String(input);
   if(init?.method==="POST"&&/^https:\/\/daumcommercial[.]com\/property-search\/(?:page\/2\/)?$/.test(url)){
    posts++;
    let body=daumProofPage(100,url.endsWith("/page/2/")?2:1);
    if(broken&&fault==="unknownBoth")body=body.replaceAll('class="listing-type">Sale<','class="listing-type"><');
    return new Response(body);
   }
   const match=url.match(/^https:\/\/daumcommercial[.]com\/property\/Synthetic-(\d+)\/$/);
   if(init?.method!=="GET"||!match)throw Error("UNEXPECTED_OFFLINE_URL_NO_FALLBACK");
   const index=Number(match[1]);let body=daumProofDetail(index);
   if(broken){
    if(fault==="missingCanonical"&&index===0)body=body.replace(/<link rel="canonical"[^>]+>/,"");
    if(fault==="conflictingTenure"&&index===0)body=body.replace('class="title-about">Sale<','class="title-about">Lease<');
    if(fault==="duplicateNative")body=body.replace(/\?p=\d+/,"?p=50000");
    if(fault==="unknownBoth"&&index===0)body=body.replace('class="title-about">Sale<','class="title-about"><');
   }
   return new Response(body);
  };
  await assert.rejects(srcDaumCommercial("sale",Infinity,false));
  const failedPosts=posts;broken=false;
  const result=await srcDaumCommercial("lease",Infinity,false);
  assert.equal(result.currentInventoryProof?.schema,"daum-strict-detail-v1");
  assert.equal(posts,failedPosts+4,"failed cohorts must never supply another transaction proof");
 });
}

test("DAUM empty lease is derived from a complete all-sale universe",async(t)=>{
 const saved={...process.env},originalFetch=globalThis.fetch,originalTimer=globalThis.setTimeout;
 t.after(()=>{for(const k of Object.keys(process.env))if(!(k in saved))delete process.env[k];Object.assign(process.env,saved);globalThis.fetch=originalFetch;globalThis.setTimeout=originalTimer;});
 Object.assign(process.env,{CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"all-sale",CRE_REFRESH_STARTED_AT:observationNow()});
 globalThis.setTimeout=((fn:any,ms?:number,...args:any[])=>originalTimer(fn,ms&&ms>0&&ms<=3000?0:ms,...args)) as typeof setTimeout;
 let calls=0;
 globalThis.fetch=async(input,init)=>{
  calls++;const url=String(input);
  if(init?.method==="POST"&&/^https:\/\/daumcommercial[.]com\/property-search\/(?:page\/2\/)?$/.test(url))return new Response(daumProofPage(100,url.endsWith("/page/2/")?2:1,true));
  const match=url.match(/^https:\/\/daumcommercial[.]com\/property\/Synthetic-(\d+)\/$/);
  if(init?.method==="GET"&&match)return new Response(daumProofDetail(Number(match[1]),true));
  throw Error("UNEXPECTED_OFFLINE_URL_NO_FALLBACK");
 };
 const lease=await srcDaumCommercial("lease",Infinity,false);
 assert.equal(calls,104);assert.equal(lease.listings.length,0);
 assert.equal(lease.totalAvailable,0);assert.equal(lease.truncated,false);
 if(lease.currentInventoryProof?.schema!=="daum-strict-detail-v1")throw Error("missing typed proof");
 assert.equal(lease.currentInventoryProof.total,100);
});
