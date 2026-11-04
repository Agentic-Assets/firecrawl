import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { PartialPageSpool } from "../../../lib/partial-page-spool.js";

test("Colliers page retains provisional card fields without detail", () => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-page-"));
  try {
    const path = join(dir, "attempt.jsonl");
    const spool = new PartialPageSpool(path, "generation", 1, undefined, "colliers");
    spool.record({ transaction: "sale", pass: 1, page: 1,
      observedAt: "2026-01-01T00:00:00Z",
      documents: [{ id: "salestracker:card:123", name: "Property", sizeText: "10 SF" }] });
    spool.record({ transaction: "sale", pass: 1, page: 1, start: 1, pairAttempt: 1, kind: "unqualified_response_pair",
      observedAt: "2026-01-01T00:00:00Z", documents: [{list: "raw-list", map: "raw-map"}] });
    assert.equal(spool.finish().state, "sealed");
    const lines = readFileSync(path, "utf8").trim().split("\n").map(line => JSON.parse(line));
    assert.equal(lines[0].sourceKey, "colliers");
    assert.equal(lines[1].kind, "parsed_page");
    assert.equal(lines[2].kind, "unqualified_response_pair");
    assert.deepEqual(lines[2].documents, [{list:"raw-list", map:"raw-map"}]);
    assert.deepEqual(lines[1].documents[0], { id: "salestracker:card:123", name: "Property", sizeText: "10 SF" });
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("failed Colliers keeps its source error and healthy sibling", () => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-cli-"));
  try {
    const captureDir = join(dir, "colliers-partial");
    mkdirSync(captureDir, { mode: 0o700 });
    const capture = join(captureDir, "attempt-1.jsonl");
    const out = join(dir, "main.json");
    const preload = join(dir, "preload.mjs");
    const colliers = `export async function srcColliers(tx,max,monitor,onPage,onResponses) {
      if(tx === "lease") return {company:"Colliers",sourceUrl:"https://example.test",method:"skipped",totalAvailable:0,listings:[]};
      onResponses(1,1,1,{list:"RAW_LIST",map:"RAW_MAP"});
      onPage(1,1,[{id:"salestracker:card:123",name:"Property",inventoryOnly:{reason:"detail_not_attempted"}}]);
      throw new Error("synthetic page failure"); }`;
    const other = `export async function srcNewmark() { return {company:"Other",sourceUrl:"https://example.test",method:"fixture",totalAvailable:1,listings:[{id:"peer",url:"https://example.test/peer"}]}; }`;
    writeFileSync(preload, `import {registerHooks} from "node:module";
      const c=${JSON.stringify("data:text/javascript," + encodeURIComponent(colliers))};
      const n=${JSON.stringify("data:text/javascript," + encodeURIComponent(other))};
      registerHooks({resolve(s,ctx,next){if(ctx.parentURL?.endsWith("/collect.ts")){
        if(s==="./sources/colliers.js")return {url:c,shortCircuit:true};
        if(s==="./sources/newmark.js")return {url:n,shortCircuit:true};} return next(s,ctx);}});`);
    const run = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
      "--source=colliers,newmark", "--transaction=both", `--out=${out}`], {
      cwd: process.cwd(), encoding: "utf8", timeout: 20000,
      env: { ...process.env, CRE_COLLIERS_PARTIAL_PATH: capture,
        CRE_COLLIERS_PARTIAL_ATTEMPT: "1", CRE_REFRESH_GENERATION: dir.split("/").at(-1)! },
    });
    assert.equal(run.status, 0, run.stderr);
    const artifact = JSON.parse(readFileSync(out, "utf8"));
    const failed = artifact.sources.find((source: any) => source.sourceKey === "colliers" && source.error);
    assert.match(failed.error, /synthetic page failure/);
    assert.equal(failed.partialCapture.outcome, "source_error");
    assert.equal(failed.partialCapture.coverage, "unverified");
    assert.equal(failed.partialCapture.observationRecords, 2);
    assert.ok(artifact.listings.some((listing: any) => listing.id === "peer"));
    const records = readFileSync(capture, "utf8").trim().split("\n").map(line => JSON.parse(line));
    assert.equal(records[1].kind, "unqualified_response_pair");
    assert.equal(records[1].documents[0].list, "RAW_LIST");
    assert.equal(records[2].documents[0].name, "Property");
    const verify = spawnSync("python3", ["-c", `import json,sys; from pathlib import Path; from cre_partial_page_custody import settle; a={"number":1,"colliers_partial":{"state":"pending","path":"colliers-partial/attempt-1.jsonl"}}; settle(Path(sys.argv[1]),a,Path(sys.argv[2]),"colliers"); print(json.dumps(a))`, dir, out], {cwd:process.cwd(),encoding:"utf8"});
    assert.equal(verify.status, 0, verify.stderr);
    assert.equal(JSON.parse(verify.stdout).colliers_partial.state, "sealed_local");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("real Colliers adapter seals recovered page evidence while remaining failed", () => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-real-recovery-"));
  try {
    const captureDir = join(dir, "colliers-partial"); mkdirSync(captureDir, { mode: 0o700 });
    const capture = join(captureDir, "attempt-1.jsonl"), out = join(dir, "main.json"), preload = join(dir, "fetch.mjs");
    writeFileSync(preload, `globalThis.fetch=async (input)=>{
      const u=new URL(String(input)); if(u.hostname==='sales.colliers.com')return new Response('<html></html>');
      const start=Number(u.searchParams.get('Start')), requested=Number(u.searchParams.get('PageSize'));
      const list=u.pathname.endsWith('GetListingsHtml'); const size=requested;
      if(start!==1 && start<=101 && start+size>101 && start!==1)return Response.json({success:false,error:list?'Failure retrieving listing HTML.':'Failure retrieving map.'});
      const ids=Array.from({length:size},(_,i)=>'P'+(start+i));
      return Response.json(list?{success:true,total:203,totalAvail:203,numProjects:size,html:'<ul>'+ids.map(id=>'<li class="item"><a class="headline">Property '+id+'</a></li>').join('')+'</ul>'}:{success:true,projectLocations:ids.map(ProjectId=>({ProjectId,Latitude:30,Longitude:-90}))});
    };`);
    const run = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
      "--source=colliers", "--transaction=both", "--max-items=203", `--out=${out}`], {
      cwd: process.cwd(), encoding: "utf8", timeout: 40000,
      env: { ...process.env, CRE_COLLIERS_PARTIAL_PATH: capture, CRE_COLLIERS_PARTIAL_ATTEMPT: "1",
        CRE_REFRESH_GENERATION: dir.split("/").at(-1)! },
    });
    assert.equal(run.status, 1, run.stderr);
    const artifact = JSON.parse(readFileSync(out, "utf8"));
    const failed = artifact.sources.find((source: any) => source.sourceKey === "colliers" && source.error);
    assert.match(failed.error, /success=false/);
    assert.equal(failed.partialCapture.outcome, "source_error");
    const rows = readFileSync(capture, "utf8").trim().split("\n").map(line => JSON.parse(line));
    assert.equal(rows.filter(row => row.kind === "parsed_page").reduce((n, row) => n + row.documents.length, 0), 202);
    assert.ok(rows.some(row => row.kind === "parsed_page" && row.start === 201 && row.documents.length === 3));
    assert.deepEqual(rows.filter(row => row.kind === "recovery_hole").map(row => [row.start, row.pageSize, row.reason]), [[101, 1, "provider_failure"]]);
    assert.ok(rows.some(row => row.kind === "unqualified_response_pair" && row.start === 101 && row.pageSize === 1));
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
