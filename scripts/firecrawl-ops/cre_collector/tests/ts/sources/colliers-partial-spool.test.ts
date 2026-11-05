import assert from "node:assert/strict";
import { createReadStream, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { PartialPageSpool } from "../../../lib/partial-page-spool.js";
import { selectColliersWindow } from "../../../sources/pure/colliers-partial-window.js";
import { buildColliersWindowArtifact } from "../../../sources/colliers-partial-window-worker.js";
import { createHash } from "node:crypto";

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
    const limitedCapture = join(captureDir, "attempt-2.jsonl"), limitedOut = join(dir, "limited.json");
    const limited = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
      "--source=colliers", "--transaction=both", "--max-items=203", "--page-cap=2", `--out=${limitedOut}`], {
      cwd: process.cwd(), encoding: "utf8", timeout: 40000,
      env: { ...process.env, CRE_COLLIERS_PARTIAL_PATH: limitedCapture, CRE_COLLIERS_PARTIAL_ATTEMPT: "2",
        CRE_REFRESH_GENERATION: dir.split("/").at(-1)! },
    });
    assert.equal(limited.status, 1, limited.stderr);
    const limitedRows = readFileSync(limitedCapture, "utf8").trim().split("\n").map(line => JSON.parse(line));
    assert.ok(limitedRows.some(row => row.kind === "recovery_hole" && row.start === 201
      && row.pageSize === 3 && row.reason === "page_cap"));
    assert.ok(!limitedRows.some(row => row.kind === "parsed_page" && row.start === 201));
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("regular count drift marks unexamined suffix only after settled transport", async () => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-regular-drift-"));
  try {
    for (const mode of ["drift", "retry", "denial", "rate"]) {
      const runDir = join(dir, mode), captureDir = join(runDir, "colliers-partial");
      mkdirSync(captureDir, { recursive: true, mode: 0o700 });
      const capture = join(captureDir, "attempt-1.jsonl"), out = join(runDir, "main.json"), preload = join(runDir, "fetch.mjs");
      writeFileSync(preload, `globalThis.fetch=async (input)=>{
        const u=new URL(String(input)); if(u.hostname==='sales.colliers.com')return new Response('<html></html>');
        if(!u.pathname.endsWith('GetListingsHtml')&&!u.pathname.endsWith('GetMapData'))throw new Error('unexpected detail fetch');
        const start=Number(u.searchParams.get('Start')), size=Number(u.searchParams.get('PageSize'));
        const list=u.pathname.endsWith('GetListingsHtml'); const ids=Array.from({length:size},(_,i)=>'P'+(start+i));
        const total=start===1?203:202;
        const body=list?{success:true,total,totalAvail:203,numProjects:size,html:'<ul>'+ids.map(id=>'<li class="item"><a href="/handler/modern.aspx?pv='+id+'">Property</a></li>').join('')+'</ul>'}:{success:true,total,totalAvail:203,projectLocations:ids.map(ProjectId=>({ProjectId,Latitude:30,Longitude:-90}))};
        const mode=${JSON.stringify(mode)}; const options=start===1?{}:mode==='retry'?{headers:{'Retry-After':'60'}}:mode==='denial'?{status:403}:mode==='rate'?{status:429}:{};
        return Response.json(body,options);
      };`);
      const run = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
        "--source=colliers", "--transaction=both", "--max-items=203", `--out=${out}`], {
        cwd: process.cwd(), encoding: "utf8", timeout: 30000,
        env: { ...process.env, CRE_COLLIERS_PARTIAL_PATH: capture, CRE_COLLIERS_PARTIAL_ATTEMPT: "1", CRE_REFRESH_GENERATION: mode },
      });
      assert.equal(run.status, 1, run.stderr);
      const artifact = JSON.parse(readFileSync(out, "utf8"));
      const failed = artifact.sources.find((entry: any) => entry.sourceKey === "colliers" && entry.error);
      const rows = readFileSync(capture, "utf8").trim().split("\n").map(line => JSON.parse(line));
      assert.equal(rows.filter(row => row.kind === "parsed_page").length, 1);
      assert.equal(failed.partialCapture.coverage, "unverified");
      const binding = { generation: mode, attempt: 1, startedAt: artifact.runMeta.startedAt, finishedAt: artifact.runMeta.finishedAt };
      const open = () => createReadStream(capture);
      if (mode === "drift") {
        assert.match(failed.error, /total changed during pagination/);
        assert.deepEqual(rows.filter(row => row.kind === "recovery_hole").map(row => [row.start, row.pageSize]), [[101, 100], [201, 3]]);
        const selection = await selectColliersWindow(open, async () => {}, binding, 1, 2);
        assert.equal(selection.eligible.length, 2);
        const bytes = readFileSync(capture);
        const worker = await buildColliersWindowArtifact({ ...binding, generationStartedAt: binding.startedAt,
          cursor: 1, limit: 2, sourcePath: capture, sourceBytes: bytes.length,
          sourceSha256: createHash("sha256").update(bytes).digest("hex"),
          originalSourceMainSha256: createHash("sha256").update(readFileSync(out)).digest("hex"),
          producerSha: "a".repeat(40), targetSha256: "b".repeat(64),
          evidencePath: join(captureDir, "worker-evidence"), artifactPath: join(captureDir, "worker-artifact") }, async token => {
          const detail = { ProjectSummary: { ProjectId: token } }, decodedText = JSON.stringify(detail);
          return { detail, decodedText, nativeBytes: Buffer.from(decodedText) };
        });
        const projection = JSON.parse(readFileSync(join(captureDir, "worker-artifact"), "utf8"));
        assert.equal(worker.admitted, 2);
        assert.equal(worker.proposedCursor, 3);
        assert.equal(projection.sourceOutcome, "source_error");
        assert.equal(projection.coverage, "unverified");
        const staged = spawnSync("python3", ["-B", "-c", `import sys; from pathlib import Path
from cre_checkpoint_refresh import _validate_source_artifact_local
result = _validate_source_artifact_local(Path(sys.argv[1]), "colliers", sys.argv[3],
  require_strict_freshness=True, expected_generation_id=sys.argv[2], expected_generation_started_at=sys.argv[3])
assert result["colliers_partial"] and result["staged_unique"] == 2 and result["rejected_by_ingest"] == 0`,
          join(captureDir, "worker-artifact"), mode, binding.startedAt], { cwd: process.cwd(), encoding: "utf8" });
        assert.equal(staged.status, 0, staged.stderr);
      } else {
        assert.match(failed.error, mode === "retry" ? /Retry-After/ : mode === "denial" ? /403/ : /429/);
        if (mode === "retry") {
          const stopped = rows.find(row => row.kind === "unqualified_response_pair" && row.start === 101);
          assert.equal(JSON.parse(stopped.documents[0].list).total, 202);
          assert.equal(JSON.parse(stopped.documents[0].map).total, 202);
        }
        assert.equal(rows.filter(row => row.kind === "recovery_hole").length, 0);
        await assert.rejects(selectColliersWindow(open, async () => {}, binding, 1, 2), /safely scoped/);
        const bytes = readFileSync(capture);
        let detailCalls = 0;
        await assert.rejects(buildColliersWindowArtifact({ ...binding, generationStartedAt: binding.startedAt,
          cursor: 1, limit: 2, sourcePath: capture, sourceBytes: bytes.length,
          sourceSha256: createHash("sha256").update(bytes).digest("hex"),
          originalSourceMainSha256: createHash("sha256").update(readFileSync(out)).digest("hex"),
          producerSha: "a".repeat(40), targetSha256: "b".repeat(64),
          evidencePath: join(captureDir, "worker-evidence"), artifactPath: join(captureDir, "worker-artifact") }, async () => {
          detailCalls++; throw new Error("unexpected detail fetch");
        }), /safely scoped/);
        assert.equal(detailCalls, 0);
      }
    }
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
