import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

test("SVN source seals parsed pages and the changed response while retaining its failure", () => {
  const dir = mkdtempSync(join(tmpdir(), "svn-capture-"));
  try {
    const partial = join(dir, "svn-partial"); mkdirSync(partial, { mode: 0o700 });
    const capture = join(partial, "attempt-1.jsonl"), out = join(dir, "main.json"), preload = join(dir, "fetch.mjs");
    writeFileSync(preload, `
      const originalSetTimeout = globalThis.setTimeout;
      globalThis.setTimeout = (fn, ms, ...args) => originalSetTimeout(fn, ms === 15000 ? 0 : ms, ...args);
      globalThis.fetch = async input => {
        const url = new URL(String(input));
        if (url.hostname !== 'buildout.com') throw new Error('unexpected network request');
        const page = Number(url.searchParams.get('page'));
        const total = page === 0 ? 4 : 5;
        return Response.json({meta:{total,limit:2}, inventory:[{id:page*2+1, sale:true, closed:false},
          {id:page*2+2,sale:false,closed:false}]});
      };`);
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
      "--source=svn", "--transaction=both", `--out=${out}`], {
      cwd: process.cwd(), encoding: "utf8", timeout: 30000,
      env: { ...process.env, CRE_SVN_PARTIAL_PATH: capture, CRE_SVN_PARTIAL_ATTEMPT: "1",
        CRE_REFRESH_GENERATION: dir.split("/").at(-1)!, CRE_REQUIRE_FRESH_DETAILS: "1", BUILDOUT_PAGE_JITTER_MS: "0" },
    });
    assert.notEqual(child.status, 0, child.stderr);
    const artifact = JSON.parse(readFileSync(out, "utf8"));
    const failed = artifact.sources.find((entry: any) => entry.sourceKey === "svn" && entry.error);
    assert.match(failed.error, /inventory pages failed/);
    assert.equal(failed.partialCapture.coverage, "unverified");
    assert.equal(failed.partialCapture.outcome, "source_error");
    const rows = readFileSync(capture, "utf8").trim().split("\n").map(line => JSON.parse(line));
    const pages = rows.slice(1).filter(row => row.kind === "buildout_inventory");
    assert.ok(pages.some(row => row.documents[0].page === 0 && row.documents[0].data.meta.total === 4));
    assert.ok(pages.some(row => row.documents[0].page === 1 && row.documents[0].data.meta.total === 5));
    assert.ok(pages.every(row => row.documents[0].representation === "collector_parsed_json_not_response_bytes"));
    const summary = rows.find(row => row.kind === "buildout_summary").documents[0];
    assert.deepEqual(summary.accepted, [0]);
    assert.deepEqual(summary.failed, [1]);
    assert.equal(artifact.listings.length, 0);
    const verify = spawnSync("python3", ["-c", `import json,sys; from pathlib import Path; from cre_partial_page_custody import settle
a={"number":1,"svn_partial":{"state":"pending","path":"svn-partial/attempt-1.jsonl"}}
settle(Path(sys.argv[1]),a,Path(sys.argv[2]),"svn"); print(json.dumps(a))`, dir, out], { cwd: process.cwd(), encoding: "utf8" });
    assert.equal(verify.status, 0, verify.stderr);
    assert.equal(JSON.parse(verify.stdout).svn_partial.state, "sealed_local");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("SVN capture retains more than 128 pages without blocking a complete scan", () => {
  const dir = mkdtempSync(join(tmpdir(), "svn-capture-many-"));
  try {
    const partial = join(dir, "svn-partial"); mkdirSync(partial, { mode: 0o700 });
    const capture = join(partial, "attempt-1.jsonl"), out = join(dir, "main.json"), preload = join(dir, "fetch.mjs");
    writeFileSync(preload, `globalThis.fetch = async input => {
      const url = new URL(String(input));
      if (url.hostname !== 'buildout.com') throw new Error('unexpected network request');
      const page = Number(url.searchParams.get('page'));
      return Response.json({meta:{total:130,limit:1},inventory:[{id:page+1,sale:true,closed:false,
        show_link:'https://buildout.com/website/'+(page+1)}]});
    };`);
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
      "--source=svn", "--transaction=both", `--out=${out}`], {
      cwd: process.cwd(), encoding: "utf8", timeout: 30000,
      env: { ...process.env, CRE_SVN_PARTIAL_PATH: capture, CRE_SVN_PARTIAL_ATTEMPT: "1",
        CRE_REFRESH_GENERATION: dir.split("/").at(-1)!, CRE_REQUIRE_FRESH_DETAILS: "1", BUILDOUT_PAGE_JITTER_MS: "0" },
    });
    assert.equal(child.status, 0, child.stderr);
    const artifact = JSON.parse(readFileSync(out, "utf8"));
    const source = artifact.sources.find((entry: any) => entry.sourceKey === "svn" && entry.partialCapture);
    assert.equal(source.partialCapture.outcome, "source_returned");
    assert.equal(source.partialCapture.coverage, "unverified");
    assert.equal(source.partialCapture.observationRecords, 131);
    assert.equal(readFileSync(capture, "utf8").trim().split("\n").length, 132);
    assert.equal(artifact.listings.length, 130);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("an SVN drift capture does not discard a healthy peer", () => {
  const dir = mkdtempSync(join(tmpdir(), "svn-capture-peer-"));
  try {
    const partial = join(dir, "svn-partial"); mkdirSync(partial, { mode: 0o700 });
    const capture = join(partial, "attempt-1.jsonl"), out = join(dir, "main.json"), preload = join(dir, "fetch.mjs");
    const peer = `export async function srcNewmark() { return {company:"Other",sourceUrl:"https://example.test",method:"fixture",totalAvailable:1,listings:[{id:"peer",url:"https://example.test/peer"}]}; }`;
    writeFileSync(preload, `import {registerHooks} from 'node:module';
      const peer=${JSON.stringify("data:text/javascript," + encodeURIComponent(peer))};
      registerHooks({resolve(s,ctx,next){if(ctx.parentURL?.endsWith('/collect.ts')&&s==='./sources/newmark.js')return {url:peer,shortCircuit:true};return next(s,ctx);}});
      const timer=globalThis.setTimeout; globalThis.setTimeout=(fn,ms,...args)=>timer(fn,ms===15000?0:ms,...args);
      globalThis.fetch=async input=>{const u=new URL(String(input)); if(u.hostname!=='buildout.com')throw new Error('unexpected network');
        const page=Number(u.searchParams.get('page')); return Response.json({meta:{total:page===0?4:5,limit:2},inventory:[{id:page*2+1,sale:true},{id:page*2+2,sale:false}]});};`);
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
      "--source=svn,newmark", "--transaction=both", `--out=${out}`], {
      cwd: process.cwd(), encoding: "utf8", timeout: 30000,
      env: { ...process.env, CRE_SVN_PARTIAL_PATH: capture, CRE_SVN_PARTIAL_ATTEMPT: "1",
        CRE_REFRESH_GENERATION: dir.split("/").at(-1)!, CRE_REQUIRE_FRESH_DETAILS: "1", BUILDOUT_PAGE_JITTER_MS: "0" },
    });
    assert.equal(child.status, 0, child.stderr);
    const artifact = JSON.parse(readFileSync(out, "utf8"));
    assert.ok(artifact.listings.some((listing: any) => listing.sourceKey === "newmark" && listing.id === "peer"));
    assert.equal(artifact.sources.find((entry: any) => entry.sourceKey === "svn" && entry.partialCapture).partialCapture.outcome, "source_error");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("SVN capture records failed and unattempted page ranges", () => {
  const dir = mkdtempSync(join(tmpdir(), "svn-capture-ranges-"));
  try {
    const partial = join(dir, "svn-partial"); mkdirSync(partial, { mode: 0o700 });
    const capture = join(partial, "attempt-1.jsonl"), out = join(dir, "main.json"), preload = join(dir, "fetch.mjs");
    writeFileSync(preload, `const timer=globalThis.setTimeout; globalThis.setTimeout=(fn,ms,...args)=>timer(fn,ms===15000?0:ms,...args);
      globalThis.fetch=async input=>{const u=new URL(String(input));if(u.hostname!=='buildout.com')throw new Error('unexpected network');
        const page=Number(u.searchParams.get('page'));return Response.json({meta:{total:page===0?7:8,limit:1},inventory:[{id:page+1,sale:true}]});};`);
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
      "--source=svn", "--transaction=both", `--out=${out}`], {
      cwd: process.cwd(), encoding: "utf8", timeout: 30000,
      env: { ...process.env, CRE_SVN_PARTIAL_PATH: capture, CRE_SVN_PARTIAL_ATTEMPT: "1",
        CRE_REFRESH_GENERATION: dir.split("/").at(-1)!, CRE_REQUIRE_FRESH_DETAILS: "1", BUILDOUT_PAGE_JITTER_MS: "0" },
    });
    assert.notEqual(child.status, 0, child.stderr);
    const rows = readFileSync(capture, "utf8").trim().split("\n").map(line => JSON.parse(line));
    const summary = rows.find(row => row.kind === "buildout_summary").documents[0];
    assert.deepEqual(summary.accepted, [0]);
    assert.deepEqual(summary.failed, [1, 2, 3, 4]);
    assert.deepEqual(summary.unattempted, [5, 6]);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
