import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync, writeSync, fsyncSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { PartialPageSpool } from "../../../lib/partial-page-spool.js";
import { fetchCbreSnapshot } from "../../../sources/cbre.js";

test("writes private page observations with exact sealed digest", () => {
  const dir = mkdtempSync(join(tmpdir(), "cbre-spool-test-"));
  try {
    const path = join(dir, "capture.jsonl");
    const spool = new PartialPageSpool(path, "run-1", 2);
    spool.record({ transaction: "sale", pass: 1, page: 1,
      observedAt: "2026-01-01T00:00:00.000Z",
      documents: [{ "Common.PrimaryKey": "abc", value: null, empty: [] }] });
    const result = spool.finish();
    assert.equal(result.state, "sealed");
    assert.equal(statSync(path).mode & 0o777, 0o600);
    const bytes = readFileSync(path);
    if (result.state !== "sealed") throw new Error();
    assert.equal(result.bytes, bytes.length);
    assert.equal(result.sha256, createHash("sha256").update(bytes).digest("hex"));
    assert.equal(result.observationRecords, 1);
    const lines = bytes.toString("utf8").trim().split("\n").map(line => JSON.parse(line));
    assert.equal(lines[1].kind, "validated_page");
    assert.deepEqual(lines[1].documents[0], { "Common.PrimaryKey": "abc", value: null, empty: [] });
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("unsafe directory and preexisting file fail without overwriting", () => {
  const dir = mkdtempSync(join(tmpdir(), "cbre-spool-test-"));
  try {
    const path = join(dir, "capture.jsonl");
    const first = new PartialPageSpool(path, "run-1", 1);
    assert.equal(first.finish().state, "sealed");
    const before = readFileSync(path);
    assert.equal(new PartialPageSpool(path, "run-1", 2).finish().state, "failed");
    assert.deepEqual(readFileSync(path), before);
    chmodSync(dir, 0o755);
    assert.equal(new PartialPageSpool(join(dir, "other"), "run-1", 3).finish().state, "failed");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("keeps validated pages when a later page fails without changing the error", async () => {
  const dir = mkdtempSync(join(tmpdir(), "cbre-spool-test-"));
  try {
    const path = join(dir, "capture.jsonl");
    const spool = new PartialPageSpool(path, "run-1", 1);
    const failure = new Error("synthetic later failure");
    await assert.rejects(() => fetchCbreSnapshot(async (page) => {
      if (page === 1) return { DocumentCount: 2, Documents: [{ "Common.PrimaryKey": "a" }] };
      throw failure;
    }, { pageSize: 1, maxPasses: 2, maxPages: 2, concurrency: 1,
      onPage: (page, pass, documents) => spool.record({ transaction: "sale", page, pass,
        observedAt: "2026-01-01T00:00:00.000Z", documents }) }), error => error === failure);
    const receipt = spool.finish();
    assert.equal(receipt.state, "sealed");
    assert.match(readFileSync(path, "utf8"), /Common.PrimaryKey/);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});


test("collector preserves failed CBRE error, sidecar and healthy sibling", async () => {
  const { spawnSync } = await import("node:child_process");
  const dir = mkdtempSync(join(tmpdir(), "cbre-spool-cli-"));
  try {
    const preload = join(dir, "preload.mjs");
    const stubCbre = `export async function srcCbre(tx,max,monitor,onPage) {
      onPage?.(1,1,[{"Common.PrimaryKey":tx+"-1","Common.Aspects":null,nullable:null}]);
      if(tx==="lease" && process.env.CBRE_TEST_SUCCESS!=="1") {
        onPage?.(2,1,[{DocumentCount:2,Documents:[{"Common.PrimaryKey":"peer"},null]}],"document_validation",[1]);
        throw new Error("synthetic lease failure");
      }
      return {company:"CBRE",sourceUrl:"https://example.test",method:"fixture",totalAvailable:1,
        listings:[{id:"sale-ok",url:"https://example.test/sale"}]}; }`;
    const stubOther = `export async function srcNewmark() {
      return {company:"Other",sourceUrl:"https://example.test",method:"fixture",totalAvailable:1,
        listings:[{id:"other-ok",url:"https://example.test/other"}]}; }`;
    writeFileSync(preload, `import {registerHooks} from "node:module";
      const c=${JSON.stringify("data:text/javascript," + encodeURIComponent(stubCbre))};
      const n=${JSON.stringify("data:text/javascript," + encodeURIComponent(stubOther))};
      const clock=${JSON.stringify("data:text/javascript," + encodeURIComponent('let n=0;export function observationNow(){if(++n===2)throw new Error("clock failed");return "2026-01-01T00:00:00.000Z"}'))};
      registerHooks({resolve(s,cxt,next){if(cxt.parentURL?.endsWith("/collect.ts")){
        if(s==="./sources/cbre.js")return {url:c,shortCircuit:true};
        if(s==="./sources/newmark.js")return {url:n,shortCircuit:true};
        if(s==="./lib/observation-clock.js" && process.env.CBRE_TEST_CLOCK_FAIL==="1")return {url:clock,shortCircuit:true};}
        return next(s,cxt);}});`);
    const run = join(dir, "fixture-run");
    mkdirSync(run, { mode: 0o700 });
    mkdirSync(join(run, "cbre-partial"), { mode: 0o700 });
    const out = join(run, "main.json"), side = join(run, "cbre-partial", "attempt-1.jsonl");
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
      "--source=cbre,newmark", "--transaction=both", `--out=${out}`],
      { cwd: process.cwd(), encoding: "utf8", timeout: 20000,
        env: { ...process.env, CRE_CBRE_PARTIAL_PATH: side, CRE_CBRE_PARTIAL_ATTEMPT: "1",
          CRE_REFRESH_GENERATION: "fixture-run" } });
    assert.equal(child.status, 0, child.stderr);
    const artifact = JSON.parse(readFileSync(out, "utf8"));
    const failure = artifact.sources.find((row: any) => row.sourceKey === "cbre" && row.error);
    assert.match(failure.error, /synthetic lease failure/);
    assert.equal(failure.partialCapture.state, "sealed");
    assert.equal(failure.partialCapture.observationRecords, 3);
    assert.ok(artifact.listings.some((row: any) => row.id === "other-ok"));
    assert.ok(artifact.listings.some((row: any) => row.id === "sale-ok"));
    assert.equal(statSync(out).mode & 0o777, 0o600);
    const pages = readFileSync(side, "utf8").trim().split("\n").map(line => JSON.parse(line));
    assert.equal(pages[1].documents[0]["Common.Aspects"], null);
    assert.equal(pages[1].transaction, "sale");
    assert.equal(pages[2].transaction, "lease");
    assert.equal(pages[3].kind, "cbre_rejected_page");
    assert.deepEqual(pages[3].rejectedDocuments, [1]);
    assert.equal(pages[3].rejectedDocumentsIndexBasis, "documents_flat_1");
    assert.equal(pages[3].qualification, "unverified_page");
    assert.equal(pages[3].documents[0].Documents[0]["Common.PrimaryKey"], "peer");
    const verify = spawnSync("python3", ["-c",
      `import json,sys; from pathlib import Path; from cre_partial_page_custody import settle; a={"number":1,"cbre_partial":{"state":"pending","path":"cbre-partial/attempt-1.jsonl"}}; settle(Path(sys.argv[1]),a,Path(sys.argv[2])); print(json.dumps(a))`,
      run, out], { cwd: process.cwd(), encoding: "utf8", timeout: 10000 });
    assert.equal(verify.status, 0, verify.stderr);
    assert.equal(JSON.parse(verify.stdout).cbre_partial.state, "sealed_local");
    const out2 = join(run, "success.json");
    const side2 = join(run, "cbre-partial", "attempt-2.jsonl");
    const success = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
      "--source=cbre", "--transaction=both", `--out=${out2}`],
      { cwd: process.cwd(), encoding: "utf8", timeout: 20000,
        env: { ...process.env, CRE_CBRE_PARTIAL_PATH: side2, CRE_CBRE_PARTIAL_ATTEMPT: "2",
          CRE_REFRESH_GENERATION: "fixture-run", CBRE_TEST_SUCCESS: "1" } });
    assert.equal(success.status, 0, success.stderr);
    const successArtifact = JSON.parse(readFileSync(out2, "utf8"));
    assert.equal(successArtifact.sources[0].partialCapture.outcome, "source_returned");
    assert.equal(successArtifact.sources[0].partialCapture.coverage, "unverified");
    assert.ok(statSync(side2).size > 0);
    const stdoutOnly = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
      "--source=cbre", "--transaction=both"],
      { cwd: process.cwd(), encoding: "utf8", timeout: 20000,
        env: { ...process.env, CRE_CBRE_PARTIAL_PATH: join(run, "cbre-partial", "no-out.jsonl"),
          CRE_CBRE_PARTIAL_ATTEMPT: "3", CRE_REFRESH_GENERATION: "fixture-run" } });
    assert.equal(stdoutOnly.status, 0, stdoutOnly.stderr);
    assert.ok(JSON.parse(stdoutOnly.stdout).listings.some((row: any) => row.id === "sale-ok"));
    const out3 = join(run, "clock-failed.json"), side3 = join(run, "cbre-partial", "attempt-3.jsonl");
    const clockFailure = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts",
      "--source=cbre,newmark", "--transaction=both", `--out=${out3}`],
      { cwd: process.cwd(), encoding: "utf8", timeout: 20000,
        env: { ...process.env, CRE_CBRE_PARTIAL_PATH: side3, CRE_CBRE_PARTIAL_ATTEMPT: "3",
          CRE_REFRESH_GENERATION: "fixture-run", CBRE_TEST_CLOCK_FAIL: "1" } });
    assert.equal(clockFailure.status, 0, clockFailure.stderr);
    const clockArtifact = JSON.parse(readFileSync(out3, "utf8"));
    assert.ok(clockArtifact.listings.some((row: any) => row.id === "other-ok"));
    const clockCbreFailure = clockArtifact.sources.find((row: any) => row.sourceKey === "cbre" && row.error);
    assert.match(clockCbreFailure.error, /synthetic lease failure/);
    assert.equal(clockCbreFailure.partialCapture.state, "failed");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});


test("later partial write failure seals the complete prefix only", () => {
  const dir = mkdtempSync(join(tmpdir(), "cbre-spool-failure-"));
  try {
    const path = join(dir, "capture.jsonl");
    let writes = 0;
    const spool = new PartialPageSpool(path, "run-1", 1, {
      write(fd, buffer, offset, length) {
        writes++;
        if (writes === 3) return writeSync(fd, buffer, offset, Math.min(5, length));
        if (writes === 4) throw new Error("synthetic disk failure");
        return writeSync(fd, buffer, offset, length);
      },
      sync(fd) { fsyncSync(fd); },
    });
    spool.record({ transaction: "sale", pass: 1, page: 1,
      observedAt: "2026-01-01T00:00:00.000Z", documents: [{ id: "kept" }] });
    spool.record({ transaction: "sale", pass: 1, page: 2,
      observedAt: "2026-01-01T00:00:01.000Z", documents: [{ id: "unwritten" }] });
    const receipt = spool.finish();
    assert.equal(receipt.state, "sealed_partial");
    assert.equal(receipt.state === "sealed_partial" ? receipt.observationRecords : -1, 1);
    const bytes = readFileSync(path);
    assert.doesNotMatch(bytes.toString("utf8"), /unwritten/);
    assert.equal(receipt.state === "sealed_partial" ? receipt.sha256 : "",
      createHash("sha256").update(bytes).digest("hex"));
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("a failed first sync only seals prefix after a successful retry", () => {
  const dir = mkdtempSync(join(tmpdir(), "cbre-spool-sync-"));
  try {
    const path = join(dir, "capture.jsonl");
    let calls = 0;
    const spool = new PartialPageSpool(path, "run-1", 1, {
      write(fd, buffer, offset, length) { return writeSync(fd, buffer, offset, length); },
      sync(fd) { if (++calls === 1) throw new Error("synthetic sync failure"); fsyncSync(fd); },
    });
    spool.record({ transaction: "lease", pass: 1, page: 1,
      observedAt: "2026-01-01T00:00:00.000Z", documents: [{ id: "kept" }] });
    assert.equal(spool.finish().state, "sealed_partial");
    assert.equal(calls, 2);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});


test("retains rejected page response and distinguishes document from envelope failure", async () => {
  for (const [response, expected] of [
    [{ DocumentCount: 2, Documents: [{ "Common.PrimaryKey": "valid" }, null] }, "document_validation"],
    [{ DocumentCount: 3, Page: 2, Documents: [{ "Common.PrimaryKey": "valid" }, null] }, "page_envelope"],
  ] as const) {
    const dir = mkdtempSync(join(tmpdir(), "cbre-rejected-page-"));
    try {
      const path = join(dir, "capture.jsonl");
      const spool = new PartialPageSpool(path, "run-1", 1);
      await assert.rejects(() => fetchCbreSnapshot(async () => response, {
        pageSize: 2, maxPasses: 2, maxPages: 2, concurrency: 1,
        onPage: (page, pass, documents, rejection, rejectedDocuments) => spool.record({
          transaction: "sale", page, pass, observedAt: "2026-01-01T00:00:00.000Z", documents,
          ...(rejection ? { kind: "cbre_rejected_page" as const, rejection, rejectedDocuments } : {}),
        }),
      }));
      assert.equal(spool.finish().state, "sealed");
      const record = JSON.parse(readFileSync(path, "utf8").trim().split("\n")[1]);
      assert.equal(record.kind, "cbre_rejected_page");
      assert.equal(record.rejection, expected);
      assert.deepEqual(record.rejectedDocuments, expected === "document_validation" ? [1] : []);
      assert.deepEqual(record.documents, [response]);
      assert.equal(record.documents[0].Documents[0]["Common.PrimaryKey"], "valid");
      assert.equal(record.documents[0].Documents[1], null);
    } finally { rmSync(dir, { recursive: true, force: true }); }
  }
});


test("duplicate identity evidence identifies every conflicting document", async () => {
  const captured: any[] = [];
  const response = { DocumentCount: 3, Documents: [
    { "Common.PrimaryKey": "same" }, { "Common.PrimaryKey": "valid" },
    { "Common.PrimaryKey": "same" },
  ] };
  await assert.rejects(() => fetchCbreSnapshot(async () => response, {
    pageSize: 3, maxPasses: 2, maxPages: 2, concurrency: 1,
    onPage: (_page, _pass, documents, rejection, rejectedDocuments) => {
      captured.push({ documents, rejection, rejectedDocuments });
    },
  }), /duplicate Common.PrimaryKey/);
  assert.equal(captured[0].rejection, "document_validation");
  assert.deepEqual(captured[0].rejectedDocuments, [0, 2]);
  assert.deepEqual(captured[0].documents, [response]);
});

test("retention callback failure does not replace page validation error", async () => {
  await assert.rejects(() => fetchCbreSnapshot(async () => ({ DocumentCount: 1, Documents: [null] }), {
    pageSize: 1, maxPasses: 2, maxPages: 2, concurrency: 1,
    onPage: () => { throw new Error("capture failed"); },
  }), /contains a malformed document/);
});


test("rejected document indexes use the one-level flattened documents array", async () => {
  const captured: any[] = [];
  const response = { DocumentCount: 2, Documents: [[{ "Common.PrimaryKey": "valid" }, null]] };
  await assert.rejects(() => fetchCbreSnapshot(async () => response, {
    pageSize: 2, maxPasses: 2, maxPages: 2, concurrency: 1,
    onPage: (_page, _pass, documents, rejection, rejectedDocuments) => {
      captured.push({ documents, rejection, rejectedDocuments });
    },
  }), /contains a malformed document/);
  assert.deepEqual(captured[0].rejectedDocuments, [1]);
  assert.deepEqual(captured[0].documents, [response]);
});
