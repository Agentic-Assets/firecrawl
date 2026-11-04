import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { CbrePartialSpool } from "../../../lib/cbre-partial-spool.js";

test("Colliers page retains provisional card fields without detail", () => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-page-"));
  try {
    const path = join(dir, "attempt.jsonl");
    const spool = new CbrePartialSpool(path, "generation", 1, undefined, "colliers");
    spool.record({ transaction: "sale", pass: 1, page: 1,
      observedAt: "2026-01-01T00:00:00Z",
      documents: [{ id: "salestracker:card:123", name: "Property", sizeText: "10 SF" }] });
    assert.equal(spool.finish().state, "sealed");
    const lines = readFileSync(path, "utf8").trim().split("\n").map(line => JSON.parse(line));
    assert.equal(lines[0].sourceKey, "colliers");
    assert.equal(lines[1].kind, "parsed_page");
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
    const colliers = `export async function srcColliers(tx,max,monitor,onPage) {
      if(tx === "lease") return {company:"Colliers",sourceUrl:"https://example.test",method:"skipped",totalAvailable:0,listings:[]};
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
        CRE_COLLIERS_PARTIAL_ATTEMPT: "1", CRE_REFRESH_GENERATION: "test-generation" },
    });
    assert.equal(run.status, 0, run.stderr);
    const artifact = JSON.parse(readFileSync(out, "utf8"));
    const failed = artifact.sources.find((source: any) => source.sourceKey === "colliers" && source.error);
    assert.match(failed.error, /synthetic page failure/);
    assert.equal(failed.partialCapture.outcome, "source_error");
    assert.equal(failed.partialCapture.coverage, "unverified");
    assert.ok(artifact.listings.some((listing: any) => listing.id === "peer"));
    assert.equal(JSON.parse(readFileSync(capture, "utf8").trim().split("\n")[1]!).documents[0].name, "Property");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
