import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { prune } from "../../../lib/util.js";
import { mapNewmarkNimListing } from "../../../sources/newmark.js";
import { captureNewmarkNimRaw, preserveNewmarkNimRaw } from "../../../sources/pure/newmark-nim-raw-retention.js";

const row = {id:"provider-id",slug:"synthetic-listing",properties:[{countryCode:"US"}],
  unknown:{n:null,f:false,z:0,s:"",o:{},a:[],positions:[null,false,0,"",{},[],{nested:null}]}};
const clock="2026-10-30T00:00:00Z";

test("mapping and outer collector retain parsed source and bind all provenance", () => {
  const source=structuredClone(row); const listing=mapNewmarkNimListing(source,"sale",clock,false)!;
  assert.deepEqual(listing.rawNewmarkNimRecord,row);
  assert.equal(listing.newmarkNimRawRetention.providerId,"provider-id");
  assert.equal(listing.newmarkNimRawRetention.binding,"id_and_url");
  source.unknown.positions[0]="changed";
  const outer=preserveNewmarkNimRaw(prune({sourceKey:"newmark",transactionMode:"sale",...listing}),listing.rawNewmarkNimRecord,listing.newmarkNimRawRetention);
  assert.deepEqual(outer.rawNewmarkNimRecord.unknown.positions,[null,false,0,"",{},[],{nested:null}]);
  for (const mutation of [{sourceKey:"srs"},{transactionMode:"lease"},{id:"other"},{url:"https://example.test/other"},
    {inventoryObservedAt:"other"},{freshnessProvenance:{...outer.freshnessProvenance,cacheDisposition:"generation_cache"}},
    {freshnessProvenance:{...outer.freshnessProvenance,method:"other"}}]) {
    const changed=preserveNewmarkNimRaw({...outer,...mutation},outer.rawNewmarkNimRecord,outer.newmarkNimRawRetention);
    assert.equal(changed.rawNewmarkNimRecord,undefined); assert.equal(changed.newmarkNimRawRetention.reason,"binding_mismatch");
  }
  const wrongRecord={...outer.rawNewmarkNimRecord,id:"other"};
  assert.equal(preserveNewmarkNimRaw({...outer},wrongRecord,outer.newmarkNimRawRetention).newmarkNimRawRetention.reason,"binding_mismatch");
  for (const marker of [{...outer.newmarkNimRawRetention,providerId:"other"},
    {...outer.newmarkNimRawRetention,binding:"unbound"}]) {
    assert.equal(preserveNewmarkNimRaw({...outer},outer.rawNewmarkNimRecord,marker).newmarkNimRawRetention.reason,"binding_mismatch");
  }
  const numericProvider = mapNewmarkNimListing({...row,id:42},"sale",clock,false)!;
  assert.equal(numericProvider.newmarkNimRawRetention.providerId,null);
  assert.equal(numericProvider.rawNewmarkNimRecord.id,42);
  const legacy={id:"old"}; assert.equal(preserveNewmarkNimRaw(legacy,undefined,undefined),legacy);
});

test("invalid or oversized raw values are omitted without dropping the listing", () => {
  const sparse:any[]=new Array(1); (sparse as any).extra=true;
  const cycle:any={};cycle.self=cycle;
  const getter=Object.defineProperty({},"x",{enumerable:true,get(){throw Error("accessed");}});
  for(const invalid of [sparse,cycle,getter,{v:-0},{v:undefined},{v:"x".repeat(1_000_001)}]) {
    const {raw,marker}=captureNewmarkNimRaw(invalid,"sale","id","https://www.nmrk.com/properties/id","provider",clock,"live");
    const listing={id:"id",url:"https://www.nmrk.com/properties/id",inventoryObservedAt:clock,freshnessProvenance:{detailScope:"authoritative_inventory_feed",method:"newmark_nim_public_inventory",cacheDisposition:"live"}};
    const result=preserveNewmarkNimRaw(listing,raw,marker);
    assert.equal(result.id,"id");assert.equal((result as any).rawNewmarkNimRecord,undefined);
    assert.equal((result as any).newmarkNimRawRetention.reason,"invalid_or_over_limit");
  }
});

test("normal and monitor CLI require private exclusive output before acquisition", () => {
  const dir=mkdtempSync(join(tmpdir(),"newmark-retention-"));
  try {
    const preload=join(dir,"fetch.mjs"),called=join(dir,"called"),out=join(dir,"out.json");
    writeFileSync(preload,`import {writeFileSync} from 'node:fs';globalThis.fetch=async(url)=>{writeFileSync(${JSON.stringify(called)},'called');if(!String(url).startsWith('https://api-public.nim.nmrk.com/'))throw Error('unexpected network blocked');return new Response(JSON.stringify(${JSON.stringify({total:1,data:[row]})}),{status:200});};`,{mode:0o600});
    const env={PATH:process.env.PATH};
    const argv=["--import","tsx","--import",preload,"collect.ts","--source=newmark","--transaction=sale"];
    for(const monitor of [false,true]) {
      const result=spawnSync(process.execPath,[...argv,...(monitor?["--monitor"]:[])],{encoding:"utf8",env});
      assert.notEqual(result.status,0);assert.equal(result.stdout,"");assert.equal(existsSync(called),false);
    }
    const run=(path:string)=>spawnSync(process.execPath,[...argv,`--out=${path}`],{encoding:"utf8",env});
    const success=run(out);assert.equal(success.status,0,success.stderr);assert.equal(success.stdout,"");
    assert.equal(statSync(out).mode&0o777,0o600);
    const artifact=JSON.parse(readFileSync(out,"utf8"));assert.equal(artifact.listings.length,1);
    assert.deepEqual(artifact.listings[0].rawNewmarkNimRecord,row);
    assert.equal(artifact.listings[0].newmarkNimRawRetention.transactionMode,"sale");
    const original=readFileSync(out,"utf8");assert.notEqual(run(out).status,0);assert.equal(readFileSync(out,"utf8"),original);
    const link=join(dir,"link.json");symlinkSync(out,link);assert.notEqual(run(link).status,0);assert.equal(readFileSync(out,"utf8"),original);
  } finally {rmSync(dir,{recursive:true,force:true});}
});
