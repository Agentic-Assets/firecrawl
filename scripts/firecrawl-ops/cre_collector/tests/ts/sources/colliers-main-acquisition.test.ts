
import assert from "node:assert/strict";
import test from "node:test";
import {mkdtempSync,readFileSync,writeFileSync,rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {spawnSync} from "node:child_process";
import {fileURLToPath} from "node:url";

const root=fileURLToPath(new URL("../../../",import.meta.url));
const preload=String.raw`
import fs from 'node:fs';
const mode=process.env.MAIN_OFFLINE_MODE;
const ids=['usa10001','usa10002','usa10003'];
const urls=ids.map(id=>'https://www.colliers.com/en/properties/synthetic/'+id);
const calls={scrape:0,batch:0,waits:[]};
const real=globalThis.setTimeout;
globalThis.setTimeout=(fn,ms,...args)=>{if([2500,5000,15000,30000].includes(ms)){calls.waits.push(ms);return real(fn,0,...args);}return real(fn,ms,...args);};
globalThis.fetch=async(input)=>{
 const url=String(input);
 if(url.endsWith('/v2/scrape')){
  calls.scrape++;
  const rawHtml=mode==='discovery-failure'?'not a sitemap':'<urlset>'+urls.map(url=>'<url><loc>'+url+'</loc></url>').join('')+'</urlset>';
  return new Response(JSON.stringify({success:true,data:{rawHtml}}));
 }
 if(url.endsWith('/browser-batch-fetch')){
  calls.batch++;
  if(mode==='bootstrap-failure')return new Response(JSON.stringify({error:'Bootstrap returned HTTP429',bootstrapStatus:429}),{status:502});
  const results=ids.map((id,i)=>({clickUri:urls[i],raw:{propertyz32xid:id.toUpperCase(),propertyz32xtitle:'Synthetic '+id,forz32xsale:i===1?'0':'1',forz32xlease:i===0?'0':'1',propertyz32xstatus:'Available'}}));
  return new Response(JSON.stringify({bootstrapStatus:200,responses:[{status:200,body:JSON.stringify({totalCount:3,results})}]}));
 }
 throw Error('Unexpected offline endpoint');
};
process.on('exit',()=>fs.writeFileSync(process.env.MAIN_OFFLINE_CALLS,JSON.stringify(calls),{flag:'wx'}));
`;

function run(mode:string) {
 const dir=mkdtempSync(join(tmpdir(),"cre-main-acquisition-"));
 try {
  const loader=join(dir,"fake.mts"),output=join(dir,"artifact.json"),calls=join(dir,"calls.json");
  writeFileSync(loader,preload);
  const env=Object.fromEntries(["PATH","HOME","LANG"].filter(k=>process.env[k]).map(k=>[k,process.env[k]!]));
  Object.assign(env,{COLLIERS_MAIN_COVEO_ENABLE:"1",CRE_REFRESH_GENERATION:"offline-main",CRE_REFRESH_STARTED_AT:new Date().toISOString(),CRE_REQUIRE_FRESH_DETAILS:"1",MAIN_OFFLINE_MODE:mode,MAIN_OFFLINE_CALLS:calls});
  const child=spawnSync(process.execPath,["--import","tsx","--import",loader,"collect.ts","--source","colliers-main","--transaction","both","--max-items","0","--out",output],{cwd:root,env,encoding:"utf8",timeout:15000});
  assert.equal(child.error,undefined);
  return {status:child.status,stderr:child.stderr,calls:JSON.parse(readFileSync(calls,"utf8")),artifact:child.status===0?JSON.parse(readFileSync(output,"utf8")):null};
 } finally {rmSync(dir,{recursive:true,force:true});}
}

test("actual Main collector shares an exhausted bootstrap failure across both transactions",()=>{
 const x=run("bootstrap-failure");
 assert.equal(x.status,1,x.stderr);
 assert.equal(x.calls.scrape,1);
 assert.equal(x.calls.batch,3);
 assert.deepEqual(x.calls.waits,[15000,30000]);
 assert.match(x.stderr,/colliers-main\/sale FAILED/);
 assert.match(x.stderr,/colliers-main\/lease FAILED/);
});
test("actual Main collector shares failed discovery before any batch starts",()=>{
 const x=run("discovery-failure");
 assert.equal(x.status,1,x.stderr);
 assert.equal(x.calls.scrape,3);
 assert.equal(x.calls.batch,0);
 assert.deepEqual(x.calls.waits,[2500,5000]);
});
test("actual Main collector shares complete inventory with unchanged transaction filters",()=>{
 for(let process=0;process<2;process++){
  const x=run("success");
  assert.equal(x.status,0,x.stderr);
  assert.equal(x.calls.scrape,1);
  assert.equal(x.calls.batch,1);
  assert.deepEqual(x.artifact.sources.map((s:any)=>[s.transaction,s.listingsCollected,s.totalAvailableOnSource,s.truncated]),[["sale",2,3,false],["lease",2,3,false]]);
  assert.deepEqual(x.artifact.listings.map((r:any)=>[r.id,r.transactionMode]),[["usa10001","sale"],["usa10003","sale"],["usa10002","lease"],["usa10003","lease"]]);
 }
});

test("Main retains the exact shared rejection while a fresh generation or max can acquire again",()=>{
 const dir=mkdtempSync(join(tmpdir(),"cre-main-rejection-"));
 try {
  const loader=join(dir,"fake.mts"),calls=join(dir,"calls.json");
  writeFileSync(loader,preload);
  const env=Object.fromEntries(["PATH","HOME","LANG"].filter(k=>process.env[k]).map(k=>[k,process.env[k]!]));
  Object.assign(env,{COLLIERS_MAIN_COVEO_ENABLE:"1",CRE_REFRESH_GENERATION:"offline-main-one",CRE_REFRESH_STARTED_AT:new Date().toISOString(),CRE_REQUIRE_FRESH_DETAILS:"1",MAIN_OFFLINE_MODE:"bootstrap-failure",MAIN_OFFLINE_CALLS:calls});
  const script = `
    import assert from 'node:assert/strict';
    import {colliersMainEnrichAll} from './sources/colliers-main.ts';
    const first=colliersMainEnrichAll(Infinity);
    const second=colliersMainEnrichAll(Infinity);
    assert.equal(first,second);
    const outcomes=await Promise.allSettled([first,second]);
    assert.equal(outcomes[0].status,'rejected');
    assert.equal(outcomes[1].status,'rejected');
    assert.equal(outcomes[0].reason,outcomes[1].reason);
    await assert.rejects(colliersMainEnrichAll(Infinity),e=>e===outcomes[0].reason);
    process.env.CRE_REFRESH_GENERATION='offline-main-two';
    const fresh=colliersMainEnrichAll(Infinity);
    assert.notEqual(fresh,first);
    await assert.rejects(fresh,e=>e!==outcomes[0].reason);
    await assert.rejects(colliersMainEnrichAll(1));
  `;
  const child=spawnSync(process.execPath,["--import","tsx","--import",loader,"--input-type=module","-e",script],{cwd:root,env,encoding:"utf8",timeout:15000});
  assert.equal(child.error,undefined);
  assert.equal(child.status,0,child.stderr);
  const observed=JSON.parse(readFileSync(calls,"utf8"));
  assert.equal(observed.scrape,3);
  assert.equal(observed.batch,9);
 } finally {rmSync(dir,{recursive:true,force:true});}
});
