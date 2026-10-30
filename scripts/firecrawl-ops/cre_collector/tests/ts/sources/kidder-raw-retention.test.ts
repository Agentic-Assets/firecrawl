import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync, existsSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { prune } from "../../../lib/util.js";
import { captureKidderRaw, preserveKidderRaw } from "../../../sources/pure/kidder-raw-retention.js";
import { mapKidderListing } from "../../../sources/kidder-mathews.js";
import { runEnrichGroups } from "../../../lib/enrich.js";

const row = { listing_key:"synthetic-id", list_price:123, __creInventoryCacheDisposition:"provider-literal",
  unknown:{ nullValue:null, falseValue:false, zero:0, empty:"", object:{}, array:[],
    positions:[null,false,0,"",{},[],{nested:null}] } };

test("source snapshot preserves parsed literals and binds mapper clock and actual cache provenance", () => {
  const source=structuredClone(row);
  const listing=mapKidderListing(source,"sale",{inventoryObservedAt:"2026-10-30T00:00:00Z",cacheDisposition:"generation_cache"});
  assert.deepEqual(listing.rawKidder,row);
  assert.equal(listing.kidderRawRetention.cacheDisposition,"generation_cache");
  assert.equal(listing.rawKidder.__creInventoryCacheDisposition,"provider-literal");
  assert.equal(listing.freshnessProvenance.cacheDisposition,"generation_cache");
  source.unknown.positions[0]="changed";
  const outer=preserveKidderRaw(prune({sourceKey:"kidder-mathews",...listing}),listing.rawKidder,listing.kidderRawRetention);
  assert.deepEqual(outer.rawKidder.unknown.positions,[null,false,0,"",{},[],{nested:null}]);
  for (const mutation of [{id:"other"},{url:"other"},{inventoryObservedAt:"other"},
    {sourceKey:"srs"},{freshnessProvenance:undefined},{freshnessProvenance:{cacheDisposition:"live"}}]) {
    const mismatch=preserveKidderRaw({...outer,...mutation},outer.rawKidder,outer.kidderRawRetention);
    assert.equal(mismatch.rawKidder,undefined); assert.equal(mismatch.kidderRawRetention.reason,"binding_mismatch");
  }
  const legacy={id:"old"}; assert.equal(preserveKidderRaw(legacy,undefined,undefined),legacy);
});

test("unbound private input and size or losslessness failure are explicit", () => {
  const missing=mapKidderListing({unknown:[null,false,0,"",[]]},"sale",{strict:false});
  assert.equal(missing.kidderRawRetention.listingId,null);
  assert.equal(missing.kidderRawRetention.cacheDisposition,null);
  assert.equal(missing.kidderRawRetention.binding,"url_only");
  assert.deepEqual(missing.rawKidder.unknown,[null,false,0,"",[]]);
  const sparse:any[] = new Array(1); (sparse as any).extra = true;
  const cycle:any={}; cycle.self=cycle;
  const getter=Object.defineProperty({},"x",{enumerable:true,get(){throw Error("executed");}});
  for(const invalid of [sparse,cycle,getter,{v:-0},{v:undefined},{v:"x".repeat(1_000_001)}]) {
    const {raw,marker}=captureKidderRaw(invalid,"id","https://example.test/",null,null);
    const retained=preserveKidderRaw({id:"id",url:"https://example.test/"},raw,marker);
    assert.equal(retained.id,"id"); assert.equal(retained.rawKidder,undefined);
    assert.equal(retained.kidderRawRetention.reason,"invalid_or_over_limit");
  }
});

test("actual normal CLI preserves both transaction outputs and private custody", () => {
  const dir=mkdtempSync(join(tmpdir(),"kidder-retention-"));
  try {
    const preload=join(dir,"fetch.mjs"), called=join(dir,"called"), out=join(dir,"out.json");
    const rows=[row,{listing_key:"lease-id",asking_rent_max:1,unknown:[null,false,0,"",{}]}];
    writeFileSync(preload,`import {writeFileSync} from 'node:fs'; globalThis.fetch=async (url)=>{writeFileSync(${JSON.stringify(called)},'called'); if(!String(url).startsWith('https://services.kidder.com/'))throw Error('unexpected network blocked'); return new Response(JSON.stringify(${JSON.stringify({totalResultCount:2,results:rows})}),{status:200});};`,{mode:0o600});
    const env={PATH:process.env.PATH};
    const argv=["--import","tsx","--import",preload,"collect.ts","--source=kidder-mathews","--transaction=both"];
    for(const monitor of [false,true]) {
      const noOut=spawnSync(process.execPath,[...argv,...(monitor?["--monitor"]:[])],{encoding:"utf8",env});
      assert.notEqual(noOut.status,0); assert.equal(noOut.stdout,""); assert.equal(existsSync(called),false);
    }
    const claim=join(dir,"claim.json");
    writeFileSync(claim,JSON.stringify({items:[{sourceKey:"kidder-mathews",externalId:"synthetic-id",url:"https://www.kidder.com/listings/synthetic-id",transaction:"sale"}]}),{mode:0o600});
    const noEnrichOut=spawnSync(process.execPath,["--import","tsx","--import",preload,"collect.ts",`--enrich-input=${claim}`],{encoding:"utf8",env});
    assert.notEqual(noEnrichOut.status,0); assert.equal(noEnrichOut.stdout,""); assert.equal(existsSync(called),false);
    const run=(path:string)=>spawnSync(process.execPath,[...argv,`--out=${path}`],{encoding:"utf8",env});
    const ok=run(out); assert.equal(ok.status,0,ok.stderr); assert.equal(ok.stdout,""); assert.equal(statSync(out).mode&0o777,0o600);
    const artifact=JSON.parse(readFileSync(out,"utf8")); assert.equal(artifact.listings.length,2);
    for(const [i,listing] of artifact.listings.entries()) { assert.deepEqual(listing.rawKidder,rows[i]); assert.equal(listing.kidderRawRetention.status,"complete"); }
    assert.equal(artifact.listings[0].kidderRawRetention.cacheDisposition,"live");
    assert.equal(artifact.listings[1].kidderRawRetention.cacheDisposition,"generation_cache");
    assert.equal(artifact.listings[0].rawKidder.__creInventoryCacheDisposition,"provider-literal");
    const original=readFileSync(out,"utf8"); assert.notEqual(run(out).status,0); assert.equal(readFileSync(out,"utf8"),original);
    const link=join(dir,"link.json"); symlinkSync(out,link); assert.notEqual(run(link).status,0); assert.equal(readFileSync(out,"utf8"),original);
  } finally { rmSync(dir,{recursive:true,force:true}); }
});


test("targeted enrichment preserves a matching claim and omits a changed URL", async () => {
  const original=mapKidderListing(row,"sale",{strict:false});
  const groups=new Map([["kidder-mathews",[{sourceKey:"kidder-mathews",externalId:"synthetic-id",url:original.url,transaction:"sale" as const}]]]);
  const resolve=()=>({label:"fixture",enricher:{enrich:async()=>[original]}});
  const matched=await runEnrichGroups(groups,resolve,()=>"Kidder");
  assert.deepEqual(matched.listings[0].rawKidder,row);
  const wrongUrl="https://example.test/other";
  const alteredGroups=new Map([["kidder-mathews",[{sourceKey:"kidder-mathews",externalId:"synthetic-id",url:wrongUrl,transaction:"sale" as const}]]]);
  const altered=await runEnrichGroups(alteredGroups,()=>({label:"fixture",enricher:{enrich:async()=>[{...original,url:wrongUrl}]}}),()=>"Kidder");
  assert.equal(altered.listings[0].rawKidder,undefined);
  assert.equal(altered.listings[0].kidderRawRetention.reason,"binding_mismatch");
});
