import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const hook = `import fs from "node:fs"; import { registerHooks } from "node:module";
const calls=[];
globalThis.fetch=async url=>{calls.push(String(url)); if(String(url).startsWith("https://services.kidder.com/")) return new Response("denied",{status:403}); if(String(url)==="https://kidder.com/teams/team-anthony-steck/" && process.env.KIDDER_TEAM_UNSETTLED==="1") return new Response(new ReadableStream({start(c){c.enqueue(new Uint8Array(1000001))},cancel(){return Promise.reject(new Error("unsettled"))}}),{headers:{"content-type":"text/html"}}); if(String(url)==="https://kidder.com/teams/team-anthony-steck/") return new Response('<section id="available-properties"><article class="card-post"><a class="card-post__link" href="/detail?id=1">Peer</a></article></section>',{headers:{"content-type":"text/html"}}); throw Error("unexpected request");};
const stub="data:text/javascript,"+encodeURIComponent('export async function srcNewmark(){return {company:"Synthetic",sourceUrl:"https://example.test",method:"stub",totalAvailable:1,listings:[{id:"peer",url:"https://example.test/peer"}]}}');
registerHooks({resolve(s,c,next){if(c.parentURL?.endsWith("/collect.ts") && s==="./sources/newmark.js") return {url:stub,shortCircuit:true}; return next(s,c)}});
process.on("exit",()=>fs.writeFileSync(process.env.KIDDER_CALLS,JSON.stringify(calls),{mode:0o600}));`;

for (const mixed of [false,true]) test(`403 retains private evidence and ${mixed ? "healthy peer" : "terminal status"}`,()=>{
 const dir=mkdtempSync(join(tmpdir(),"kidder-team-test-"));
 try {
  const preload=join(dir,"hook.mjs"), out=join(dir,"out.json"), calls=join(dir,"calls.json"); writeFileSync(preload,hook);
  const result=spawnSync(process.execPath,["--import","tsx","--import",preload,"collect.ts",`--source=${mixed ? "kidder-mathews,newmark" : "kidder-mathews"}`,"--transaction=both","--max-items=0",`--out=${out}`],{cwd:process.cwd(),encoding:"utf8",timeout:20000,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"team-test",KIDDER_CALLS:calls}});
  assert.equal(result.status,mixed ? 0 : 74,result.stderr);
  assert.equal(JSON.parse(readFileSync(calls,"utf8")).length,2);
  const output=JSON.parse(readFileSync(out,"utf8"));
  const source=output.sources.find((x:any)=>x.sourceKey==="kidder-mathews");
  assert.equal(source.error,"inventory HTTP 403"); assert.equal(source.retainedObservations[0].coverage,"partial_unknown");
  assert.equal(source.retainedObservations[0].cards.length,1);
  assert.equal(output.listings.filter((x:any)=>x.sourceKey==="kidder-mathews").length,0);
  if(mixed) assert.ok(output.listings.some((x:any)=>x.sourceKey==="newmark"));
  else assert.equal(output.runMeta.evidenceOnly,true);
 } finally { rmSync(dir,{recursive:true,force:true}); }
});


for (const mixed of [false,true]) test(`no output is rejected before ${mixed ? "mixed" : "single"} collection`,()=>{
 const dir=mkdtempSync(join(tmpdir(),"kidder-team-no-output-"));
 try {
  const preload=join(dir,"hook.mjs"), calls=join(dir,"calls.json"); writeFileSync(preload,hook);
  const result=spawnSync(process.execPath,["--import","tsx","--import",preload,"collect.ts",`--source=${mixed ? "kidder-mathews,newmark" : "kidder-mathews"}`,"--transaction=both"],{cwd:process.cwd(),encoding:"utf8",timeout:20000,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"team-no-output",KIDDER_CALLS:calls}});
  assert.equal(result.status,1,result.stderr); assert.match(result.stderr,/require a private --out file/);
  assert.equal(result.stdout,""); assert.deepEqual(JSON.parse(readFileSync(calls,"utf8")),[]);
 } finally {rmSync(dir,{recursive:true,force:true});}
});

test("private output collision preserves existing bytes",()=>{
 const dir=mkdtempSync(join(tmpdir(),"kidder-team-output-collision-"));
 try {
  const preload=join(dir,"hook.mjs"), out=join(dir,"out.json"), calls=join(dir,"calls.json"); writeFileSync(preload,hook); writeFileSync(out,"existing",{mode:0o600});
  const result=spawnSync(process.execPath,["--import","tsx","--import",preload,"collect.ts","--source=kidder-mathews","--transaction=both",`--out=${out}`],{cwd:process.cwd(),encoding:"utf8",timeout:20000,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"team-collision",KIDDER_CALLS:calls}});
  assert.notEqual(result.status,0); assert.equal(readFileSync(out,"utf8"),"existing"); assert.equal(result.stdout,"");
 } finally {rmSync(dir,{recursive:true,force:true});}
});

test("interrupted worker leaves completed team observation in unverified attempt spool",()=>{
 const dir=mkdtempSync(join(tmpdir(),"kidder-team-interrupted-"));
 try {
  const preload=join(dir,"hook.mjs"), out=join(dir,"out.json"), calls=join(dir,"calls.json"), spool=join(dir,"attempt.jsonl");
  writeFileSync(preload,hook + `
import {syncBuiltinESMExports} from "node:module";
const originalWrite=fs.writeSync;
fs.writeSync=(fd,buffer,...args)=>{const result=originalWrite(fd,buffer,...args); if(Buffer.isBuffer(buffer) && buffer.includes(Buffer.from("kidder_team_observation"))) process.kill(process.pid,"SIGKILL"); return result}; syncBuiltinESMExports();`);
  const result=spawnSync(process.execPath,["--import","tsx","--import",preload,"collect.ts","--source=kidder-mathews","--transaction=both",`--out=${out}`],{cwd:process.cwd(),encoding:"utf8",timeout:20000,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"team-interrupted",CRE_KIDDER_MATHEWS_PARTIAL_PATH:spool,CRE_KIDDER_MATHEWS_PARTIAL_ATTEMPT:"1",KIDDER_CALLS:calls}});
  assert.equal(result.signal,"SIGKILL",result.stderr);
  const records=readFileSync(spool,"utf8").trim().split("\n").map(line=>JSON.parse(line));
  assert.equal(records[0].sourceKey,"kidder-mathews"); assert.equal(records[0].generation,"team-interrupted");
  assert.equal(records[1].kind,"kidder_team_observation"); assert.equal(records[1].documents[0].coverage,"partial_unknown");
  assert.equal(records[1].documents[0].cards.length,1); assert.throws(()=>readFileSync(out,"utf8"),/ENOENT/);
 } finally {rmSync(dir,{recursive:true,force:true});}
});

test("unwritable optional attempt spool reports custody loss but preserves terminal evidence artifact",()=>{
 const dir=mkdtempSync(join(tmpdir(),"kidder-team-spool-failure-"));
 try {
  const preload=join(dir,"hook.mjs"), out=join(dir,"out.json"), calls=join(dir,"calls.json"); writeFileSync(preload,hook);
  const result=spawnSync(process.execPath,["--import","tsx","--import",preload,"collect.ts","--source=kidder-mathews","--transaction=both",`--out=${out}`],{cwd:process.cwd(),encoding:"utf8",timeout:20000,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"team-spool-failed",CRE_KIDDER_MATHEWS_PARTIAL_PATH:join(dir,"missing","attempt.jsonl"),CRE_KIDDER_MATHEWS_PARTIAL_ATTEMPT:"1",KIDDER_CALLS:calls}});
  assert.equal(result.status,74,result.stderr); assert.match(result.stderr,/spool unavailable/);
  const source=JSON.parse(readFileSync(out,"utf8")).sources[0];
  assert.equal(source.partialCapture.state,"failed"); assert.equal(source.retainedObservations[0].cards.length,1);
 } finally {rmSync(dir,{recursive:true,force:true});}
});

test("unknown team response cleanup keeps mixed-source fence and does not claim settled 403",()=>{
 const dir=mkdtempSync(join(tmpdir(),"kidder-team-unsettled-"));
 try {
  const preload=join(dir,"hook.mjs"), out=join(dir,"out.json"), calls=join(dir,"calls.json"); writeFileSync(preload,hook);
  const result=spawnSync(process.execPath,["--import","tsx","--import",preload,"collect.ts","--source=kidder-mathews,newmark","--transaction=both",`--out=${out}`],{cwd:process.cwd(),encoding:"utf8",timeout:20000,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"team-unsettled",KIDDER_TEAM_UNSETTLED:"1",KIDDER_CALLS:calls}});
  assert.equal(result.status,73,result.stderr); assert.equal(JSON.parse(readFileSync(calls,"utf8")).length,2);
  assert.doesNotMatch(result.stderr,/collecting newmark/); assert.throws(()=>readFileSync(out,"utf8"),/ENOENT/);
 } finally {rmSync(dir,{recursive:true,force:true});}
});

test("settled team observation seals a source-bound partial receipt",()=>{
 const dir=mkdtempSync(join(tmpdir(),"kidder-team-sealed-"));
 try {
  const preload=join(dir,"hook.mjs"), out=join(dir,"out.json"), calls=join(dir,"calls.json"), spool=join(dir,"attempt.jsonl"); writeFileSync(preload,hook);
  const result=spawnSync(process.execPath,["--import","tsx","--import",preload,"collect.ts","--source=kidder-mathews","--transaction=both",`--out=${out}`],{cwd:process.cwd(),encoding:"utf8",timeout:20000,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"team-sealed",CRE_KIDDER_MATHEWS_PARTIAL_PATH:spool,CRE_KIDDER_MATHEWS_PARTIAL_ATTEMPT:"1",KIDDER_CALLS:calls}});
  assert.equal(result.status,74,result.stderr);
  const source=JSON.parse(readFileSync(out,"utf8")).sources[0]; const receipt=source.partialCapture;
  assert.equal(receipt.state,"sealed"); assert.equal(receipt.coverage,"unverified"); assert.equal(receipt.outcome,"source_error");
  assert.equal(receipt.sourceKey,"kidder-mathews"); assert.equal(receipt.generationId,"team-sealed");
  assert.equal(receipt.observationRecords,1); assert.equal(receipt.bytes,readFileSync(spool).length);
 } finally {rmSync(dir,{recursive:true,force:true});}
});
