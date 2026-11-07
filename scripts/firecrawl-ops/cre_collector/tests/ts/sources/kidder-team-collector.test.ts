import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const hook = `import fs from "node:fs"; import { registerHooks } from "node:module";
const calls=[];
globalThis.fetch=async url=>{calls.push(String(url)); if(String(url).startsWith("https://services.kidder.com/")) return new Response("denied",{status:403}); if(String(url)==="https://kidder.com/teams/team-anthony-steck/") return new Response('<section id="available-properties"><article class="card-post"><a class="card-post__link" href="/detail?id=1">Peer</a></article></section>',{headers:{"content-type":"text/html"}}); throw Error("unexpected request");};
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
