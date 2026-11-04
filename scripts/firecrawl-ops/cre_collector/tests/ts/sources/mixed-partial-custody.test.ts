import {test} from "node:test";
import assert from "node:assert/strict";
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,rmSync,statSync} from "node:fs";
import {spawnSync} from "node:child_process";
import {tmpdir} from "node:os";
import {join} from "node:path";

for (const key of ["cbre","colliers"] as const) test(`Franklin and ${key} preserve private custody`, () => {
  const dir=mkdtempSync(join(tmpdir(),"mixed-evidence-"));
  const out=join(dir,"output.json"), preload=join(dir,"preload.mjs"), partial=join(dir,`${key}-partial`);
  const side=join(partial,"attempt-1.jsonl");
  const stub=key==="cbre" ? 'export async function srcCbre(tx,m,n,cb){cb(1,1,[{id:"PRIVATE_PAGE"}]);throw Error("page failure")}' : 'export async function srcColliers(tx,m,n,cb){cb(1,1,[{id:"PRIVATE_PAGE"}]);throw Error("page failure")}';
  const html='<article data-listing-card="true" data-listing-id="p1">PRIVATE_FRANKLIN</article>';
  const lease={inventory:[{id:42,sale:false,closed:false,show_link:"https://www.franklinst.com/properties/?propertyId=42-lease",display_name:"Healthy lease",index_attributes:[]}],meta:{total:1,limit:100}};
  writeFileSync(preload,`import {registerHooks} from "node:module";
  const stub=${JSON.stringify("data:text/javascript,"+encodeURIComponent(stub))};
  registerHooks({resolve(s,c,n){if(c.parentURL?.endsWith("/collect.ts")&&s===${JSON.stringify(`./sources/${key}.js`)})return {url:stub,shortCircuit:true};return n(s,c)}});
  globalThis.fetch=async input=>{const u=String(input);if(u.includes("franklinst.com/properties/for-sale"))return new Response(${JSON.stringify(html)},{headers:{"content-type":"text/html"}});if(u.includes("a234450b432b2b2bebc1ace7e6f692e4489bde70"))return new Response("{}");if(u.includes("2f82fcd26667c4b0126d0084938ffa265f05fa4a"))return Response.json(${JSON.stringify(lease)});throw Error("unexpected "+u)};`);
  const env={...process.env,BUILDOUT_PAGE_JITTER_MS:"0",CRE_SCRAPE_MAX_ATTEMPTS:"1",CRE_REFRESH_GENERATION:"fixture",[`CRE_${key.toUpperCase()}_PARTIAL_PATH`]:side,[`CRE_${key.toUpperCase()}_PARTIAL_ATTEMPT`]:"1"};
  const args=["--import","tsx","--import",preload,"collect.ts",`--source=franklin-street,${key}`,"--transaction=both","--max-items=0"];
  try{
    mkdirSync(partial,{mode:0o700});writeFileSync(out,"previous");
    const run=(extra:string[])=>spawnSync(process.execPath,[...args,...extra],{cwd:process.cwd(),encoding:"utf8",timeout:20000,env});
    const existing=run([`--out=${out}`]);
    assert.equal(existing.status,1,existing.stderr);assert.match(existing.stderr,/EEXIST/);
    assert.equal(readFileSync(out,"utf8"),"previous");assert.doesNotMatch(existing.stderr,/Franklin private sidecar:/);
    assert.match(readFileSync(side,"utf8"),/PRIVATE_PAGE/);rmSync(side);
    const fresh=join(dir,"fresh.json"), result=run([`--out=${fresh}`]);
    assert.equal(result.status,0,result.stderr);assert.equal(statSync(fresh).mode&0o777,0o600);
    const data=JSON.parse(readFileSync(fresh,"utf8"));
    assert.ok(data.sources.some((x:any)=>x.sourceKey===key&&x.partialCapture?.state==="sealed"&&/page failure/.test(x.error)));
    assert.ok(data.sources.some((x:any)=>x.sourceKey==="franklin-street"&&x.retainedObservations?.length));
    assert.ok(data.listings.some((x:any)=>x.name==="Healthy lease"));
    const stdout=run([]);assert.equal(stdout.status,1,stdout.stderr);assert.equal(stdout.stdout,"");assert.match(stdout.stderr,/require a private --out file/);
  } finally {rmSync(dir,{recursive:true,force:true})}
});
