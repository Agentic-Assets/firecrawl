import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { allBuildoutFeeds } from "../../../sources/buildout-feeds/index.js";
import { buildoutRawScope, captureBuildoutRaw, preserveBuildoutRaw } from "../../../sources/pure/buildout-raw-retention.js";
import { prune } from "../../../lib/util.js";

const row = { id: 123, sale: true, closed: false, show_link: "https://example.test/?propertyId=123-sale",
  unknown: { nullValue: null, falseValue: false, zero: 0, empty: "", object: {}, array: [],
    positions: [null, false, 0, "", {}, [], { nested: null }] } };

test("all registered variants bind exact feed scope, never guessing mismatches", () => {
  assert.equal(allBuildoutFeeds.length, 29);
  assert.equal(new Set(allBuildoutFeeds.map(f => f.sourceKey)).size, 28);
  for (const f of allBuildoutFeeds) {
    assert.deepEqual(buildoutRawScope(f.company, f.pluginKey, f.listingsPage, f.feedKey), {sourceKey:f.sourceKey,feedKey:f.feedKey});
    assert.equal(buildoutRawScope(f.company, f.pluginKey, f.listingsPage, `${f.feedKey}-other`), null);
    assert.equal(buildoutRawScope(f.company, f.pluginKey, `${f.listingsPage}other`, f.feedKey), null);
  }
});

test("snapshot preserves values and nullable identity through outer prune", () => {
  const scope = {sourceKey:"svn",feedKey:"svn"};
  for (const [id,url] of [["123", row.show_link], ["123", null], [null, null]] as const) {
    const {raw,marker} = captureBuildoutRaw(row,id,url,null,scope);
    const listing = preserveBuildoutRaw(prune({id,url,sourceKey:"svn"}) ?? {},raw,marker,"svn");
    assert.deepEqual(listing.rawBuildout,row); assert.equal(listing.buildoutRawRetention.status,"complete");
    assert.equal(listing.buildoutRawRetention.listingId,id); assert.equal(listing.buildoutRawRetention.listingUrl,url);
    const wrong = preserveBuildoutRaw({...listing},raw,marker,"other");
    assert.equal(wrong.rawBuildout,undefined); assert.equal(wrong.buildoutRawRetention.reason,"binding_mismatch");
    const wrongClock = preserveBuildoutRaw({...listing, inventoryObservedAt:"other"},raw,marker,"svn");
    assert.equal(wrongClock.rawBuildout,undefined);
    const wrongUrl = preserveBuildoutRaw({...listing, url:"other"},raw,marker,"svn");
    assert.equal(wrongUrl.rawBuildout,undefined);
  }
  const unscoped = captureBuildoutRaw(row,"123",row.show_link,null,null);
  assert.equal(unscoped.marker.sourceKey,null); assert.equal(unscoped.marker.feedKey,null);
  assert.deepEqual(unscoped.raw,row);
});

test("bounded and invalid snapshots omit explicitly while retaining listing", () => {
  const sparse:any[] = new Array(1); (sparse as any).extra = true;
  const cycle:any = {}; cycle.self = cycle;
  const getter = Object.defineProperty({}, "x", {enumerable:true,get() { throw Error("executed"); }});
  for (const bad of [sparse, cycle, getter, {v:-0}, {v:undefined}, {v:"x".repeat(1_000_001)}]) {
    const {raw,marker} = captureBuildoutRaw(bad,"123",row.show_link,null,{sourceKey:"svn",feedKey:"svn"});
    const listing = preserveBuildoutRaw({id:"123",url:row.show_link},raw,marker,"svn");
    assert.equal(listing.id,"123"); assert.equal(listing.rawBuildout,undefined);
    assert.equal(listing.buildoutRawRetention.reason,"invalid_or_over_limit");
  }
});

test("normal CLI preserves both transaction paths and exact feed variants", () => {
  const dir = mkdtempSync(join(tmpdir(), "buildout-retention-"));
  try {
    const preload = join(dir,"fetch.mjs");
    const rows = [row, {...row,id:124,sale:false,show_link:"https://example.test/?propertyId=124-lease"}, {...row,id:125,closed:true}];
    writeFileSync(preload, `globalThis.fetch = async (url) => { if (!String(url).startsWith('https://buildout.com/')) throw Error('unexpected network blocked'); return new Response(JSON.stringify(${JSON.stringify({inventory:rows,meta:{total:3,limit:3}})}),{status:200}); };`, {mode:0o600});
    for (const source of ["svn","cawley-chicago","franklin-street"]) {
      const out=join(dir,`${source}.json`);
      const result=spawnSync(process.execPath,["--import","tsx","--import",preload,"collect.ts",`--source=${source}`,"--transaction=both",`--out=${out}`],{
        encoding:"utf8",env:{PATH:process.env.PATH,BUILDOUT_CACHE_DIR:join(dir,"cache"),BUILDOUT_PAGE_JITTER_MS:"0"}});
      assert.equal(result.status,0,result.stderr);
      const artifact=JSON.parse(readFileSync(out,"utf8")); assert.equal(artifact.listings.length,2);
      for (const [i,listing] of artifact.listings.entries()) {
        assert.deepEqual(listing.rawBuildout,rows[i]);
        assert.equal(listing.buildoutRawRetention.sourceKey,source);
        assert.equal(listing.buildoutRawRetention.feedKey,source === "franklin-street" ? `franklin-street-${i === 0 ? "sale" : "lease"}` : source);
      }
    }
  } finally { rmSync(dir,{recursive:true,force:true}); }
});


test("raw-source collection requires private exclusive output before any acquisition", () => {
  const dir = mkdtempSync(join(tmpdir(), "buildout-output-"));
  try {
    const preload = join(dir,"fetch.mjs"); const called = join(dir,"called");
    const rows = [row];
    writeFileSync(preload, `import {writeFileSync} from 'node:fs'; globalThis.fetch = async (url) => {writeFileSync(${JSON.stringify(called)},'called'); if (!String(url).startsWith('https://buildout.com/')) throw Error('unexpected network blocked'); return new Response(JSON.stringify(${JSON.stringify({inventory:rows,meta:{total:1,limit:1}})}),{status:200});};`, {mode:0o600});
    const env = {PATH:process.env.PATH,BUILDOUT_CACHE_DIR:join(dir,"cache"),BUILDOUT_PAGE_JITTER_MS:"0"};
    for (const source of ["svn","srs"]) for (const monitor of [false,true]) {
      const args=["--import","tsx","--import",preload,"collect.ts",`--source=${source}`,"--transaction=sale",...(monitor?["--monitor"]:[])];
      const result=spawnSync(process.execPath,args,{encoding:"utf8",env});
      assert.notEqual(result.status,0); assert.equal(result.stdout,""); assert.equal(existsSync(called),false);
    }
    const out = join(dir,"out.json"); const run = (path:string) => spawnSync(process.execPath,
      ["--import","tsx","--import",preload,"collect.ts","--source=svn","--transaction=sale",`--out=${path}`],{encoding:"utf8",env});
    const initial=run(out); assert.equal(initial.status,0,initial.stderr); assert.equal(initial.stdout,"");
    assert.equal(statSync(out).mode & 0o777,0o600);
    const original=readFileSync(out,"utf8"); assert.notEqual(run(out).status,0); assert.equal(readFileSync(out,"utf8"),original);
    const symlink=join(dir,"symlink.json"); symlinkSync(out,symlink);
    assert.notEqual(run(symlink).status,0); assert.equal(readFileSync(out,"utf8"),original);
  } finally { rmSync(dir,{recursive:true,force:true}); }
});
