process.argv = [process.argv[0]!, process.argv[1]!];
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";
import { observePages, readObservationInput, validateObservationInput } from "../../../lib/page-observations/index.js";
import { reservePrivateCollectorDirectory, writeCollectorArtifact } from "../../../lib/source-evidence/artifact-output.js";
import { scrapeDoc } from "../../../lib/scrape.js";
import { createClientRequestDeadlineError } from "../../../lib/performance.js";

const item = (index: number) => ({ sourceKey: "leibsohn", url: `https://example.test/property/${index}`, context: { index, values: [null, false, ""] } });

test("serial observation retains 55 items without claiming listings or source identity", async () => {
  const items = validateObservationInput({ items: Array.from({ length: 55 }, (_, i) => item(i)) });
  let calls = 0;
  const result = await observePages(items, async () => { calls++; return { rawHtml: "", markdown: "", links: [], metadata: { statusCode: 200 } }; });
  assert.equal(calls, 55);
  assert.equal(result.parts[0].runMeta.mode, "observe");
  assert.equal(result.parts[0].runMeta.evidenceOnly, true);
  assert.deepEqual(result.parts[0].listings, []); assert.deepEqual(result.parts[0].brokers, []);
  assert.equal(result.parts[0].sources[0].retainedObservations.length, 55);
  const first = result.parts[0].sources[0].retainedObservations[0] as any;
  assert.deepEqual(first.input.context, item(0).context);
  assert.equal(first.input.identityBasis, "url");
  assert.equal(first.evidence.claim.externalId, null);
  assert.equal(first.evidence.sourceFreshness, "unverified");
  assert.deepEqual([result.parts[0].sources[0].requested, result.parts[0].sources[0].attempted, result.parts[0].sources[0].returned, result.parts[0].sources[0].failed, result.parts[0].sources[0].notAttempted], [55, 55, 55, 0, 0]);
});

test("refuses invalid and mixed input before any fetch", () => {
  for (const input of [ {items: []}, {items: [item(0), {...item(1), sourceKey: "cbre"}]},
    {items: [{...item(0), url: "http://example.test/"}]}, {items: [{...item(0), url: "https://127.0.0.1/"}]},
    {items: [{...item(0), url: "https://user@example.test/"}]}, {items: [{...item(0), url: "https://example.test:443/"}]},
    {items: [{...item(0), url: "https://example.test/#x"}]}, {items: [{...item(0), context: Infinity}]} ]) {
    assert.throws(() => validateObservationInput(input));
  }
});

test("access denial stops later items while preserving errors and context", async () => {
  const items = validateObservationInput({items: [item(0), {...item(1), sourceId: "native-id"}, item(2)]});
  let calls = 0;
  const result = await observePages(items, async () => { calls++; if (calls === 2) throw Object.assign(new Error("private error"), {status: 403}); return {rawHtml: "<p>partial</p>", markdown: "partial", links: []}; });
  const observations = result.parts[0].sources[0].retainedObservations as any[];
  assert.equal(calls, 2);
  assert.equal(observations[1].evidence.localApiStatus, 403);
  assert.equal(observations[1].evidence.claim.externalId, "native-id");
  assert.equal(observations[2].evidence.failure, "not_attempted_after_access_denial");
  assert.deepEqual([result.parts[0].sources[0].attempted, result.parts[0].sources[0].returned, result.parts[0].sources[0].failed, result.parts[0].sources[0].notAttempted, result.parts[0].sources[0].stopReason], [2, 1, 1, 1, "access_denial"]);
  assert.equal(result.parts[0].sources[0].evidenceIncomplete, true);
  assert(!JSON.stringify(result).includes("private error"));
});

test("null source ID remains explicit and input size is checked on consumed bytes", async () => {
  const items = validateObservationInput({items: [item(0), {...item(1), sourceId: null}]});
  const result = await observePages(items, async () => ({rawHtml: "<p>Partial</p>", markdown: "Partial", links: []}));
  const observations = result.parts[0].sources[0].retainedObservations as any[];
  assert.equal(Object.hasOwn(observations[0].input, "sourceId"), false);
  assert.equal(observations[1].input.sourceId, null);
  assert.equal(observations[1].input.identityBasis, "url");
  const dir = mkdtempSync(join(tmpdir(), "cre-observe-input-"));
  const path = join(dir, "input.json");
  writeFileSync(path, " ".repeat(4 * 1024 * 1024 + 1));
  assert.throws(() => readObservationInput(path));
  assert.throws(() => readObservationInput(dir));
});

test("FIFO input is refused without blocking", { skip: process.platform === "win32" }, () => {
  const dir = mkdtempSync(join(tmpdir(), "cre-observe-fifo-"));
  const path = join(dir, "input.fifo");
  const made = spawnSync("mkfifo", [path]);
  assert.equal(made.status, 0);
  const probe = `const path=process.argv[1]; process.argv=[process.argv[0]]; const { readObservationInput }=await import('./lib/page-observations/index.ts'); try { readObservationInput(path); process.exitCode=1; } catch { process.exitCode=0; }`;
  const result = spawnSync(process.execPath, ["--import", "tsx", "--input-type=module", "-e", probe, path], {timeout: 3000, encoding: "utf8"});
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stderr);
});

test("invalid UTF-8 is refused before dispatch while valid replacement characters survive", () => {
  const dir = mkdtempSync(join(tmpdir(), "cre-observe-utf8-"));
  const input = join(dir, "input.json"), output = join(dir, "output"), marker = join(dir, "network-marker"), preload = join(dir, "preload.mjs");
  const prefix = Buffer.from('{"items":[{"sourceKey":"leibsohn","url":"https://example.test/","context":"');
  writeFileSync(input, Buffer.concat([prefix, Buffer.from([0xff]), Buffer.from('"}]}')]));
  writeFileSync(preload, `import {writeFileSync} from 'node:fs'; globalThis.fetch=async()=>{writeFileSync(${JSON.stringify(marker)},'called'); throw Error('unexpected');};`);
  const run = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts", "--observe-input", input, "--out", output], {encoding: "utf8"});
  assert.notEqual(run.status, 0);
  assert.throws(() => statSync(marker));
  assert.throws(() => statSync(output));
  writeFileSync(input, JSON.stringify({items: [{...item(0), context: "\ufffd"}]}));
  assert.equal(readObservationInput(input)[0].context, "\ufffd");
});

test("private output is reserved before use and refuses replacement", () => {
  const dir = mkdtempSync(join(tmpdir(), "cre-observe-"));
  const out = join(dir, "artifact.json");
  reservePrivateCollectorDirectory(out);
  assert.throws(() => reservePrivateCollectorDirectory(out));
  const file = join(out, "part.json");
  writeCollectorArtifact(file, JSON.stringify({ok: true}), true);
  assert.equal(statSync(out).mode & 0o777, 0o700);
  assert.equal(statSync(file).mode & 0o777, 0o600);
  assert.deepEqual(JSON.parse(readFileSync(file, "utf8")), {ok: true});
});

test("oversized context has an explicit omission", async () => {
  const items = validateObservationInput({items: [{...item(0), context: "x".repeat(65537)}]});
  const result = await observePages(items, async () => ({rawHtml: "", markdown: "", links: []}));
  const first = result.parts[0].sources[0].retainedObservations[0] as any;
  assert.equal(first.input.contextOmission, "context_byte_limit");
  assert.equal(Object.hasOwn(first.input, "context"), false);
});

test("singleAttempt bounds actual local API requests and retains no error text", async () => {
  const original = globalThis.fetch; let calls = 0;
  globalThis.fetch = async () => { calls++; return new Response(JSON.stringify({success: false, error: "private failure"}), {status: 502}); };
  try { await assert.rejects(scrapeDoc("https://example.test/", { singleAttempt: true, quietErrors: true })); assert.equal(calls, 1); }
  finally { globalThis.fetch = original; }
});

test("observe CLI rejects incompatible flags before dispatch", () => {
  const result = spawnSync(process.execPath, ["--import", "tsx", "collect.ts", "--observe-input", "nonexistent", "--source", "private-flag", "--out", "/tmp/unused-observe-output"], { encoding: "utf8" });
  assert.notEqual(result.status, 0);
  assert(!result.stderr.includes("private-flag"));
  assert(result.stderr.includes("observe mode requires"));
});

test("CLI writes a private 55-item evidence artifact without network", () => {
  const dir = mkdtempSync(join(tmpdir(), "cre-observe-cli-"));
  const input = join(dir, "input.json"), output = join(dir, "output.json"), preload = join(dir, "preload.mjs");
  writeFileSync(input, JSON.stringify({items: Array.from({length: 55}, (_, i) => item(i))}));
  writeFileSync(preload, `globalThis.fetch = async () => new Response(JSON.stringify({success:true,data:{rawHtml:'<p>Partial</p>',markdown:'X'.repeat(210000),links:[],metadata:{statusCode:200}}}),{status:200});`);
  const result = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts", "--observe-input", input, "--out", output], {encoding: "utf8"});
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, "");
  const summary = JSON.parse(readFileSync(join(output, "run-summary.json"), "utf8"));
  assert(summary.parts.length > 1);
  const parts = summary.parts.map((name: string) => join(output, name));
  const artifacts = parts.map((part: string) => JSON.parse(readFileSync(part, "utf8")));
  assert.equal(artifacts.flatMap((artifact: any) => artifact.sources[0].retainedObservations).length, 55);
  assert.deepEqual(artifacts.flatMap((artifact: any) => artifact.sources[0].retainedObservations.map((entry: any) => entry.input.context.index)), Array.from({length: 55}, (_, i) => i));
  assert.equal(summary.requested, 55);
  for (const [index, artifact] of artifacts.entries()) {
    assert.equal(artifact.runMeta.mode, "observe");
    assert.equal(artifact.runMeta.countsScope, "part");
    assert.equal(artifact.totalListings, 0);
    assert.equal(statSync(parts[index]).mode & 0o777, 0o600);
    assert(statSync(parts[index]).size < 16 * 1024 * 1024);
  }
  const python = `import json,sys\nfrom source_evidence import collector\nfrom cre_artifact_scope import require_canonical_artifact\nfrom cre_enrich import validate_enriched_artifact\na=json.load(open(sys.argv[1])); w=collector.convert(a,source_key='leibsohn'); assert collector.reconstruct(w)==a\nfor f in (lambda: require_canonical_artifact(a), lambda: validate_enriched_artifact([],a)):\n try: f()\n except ValueError: continue\n raise AssertionError('unexpected canonical admission')\n`;
  for (const part of parts) {
    const check = spawnSync("python3", ["-c", python, part], {encoding: "utf8"});
    assert.equal(check.status, 0, check.stderr);
  }
  const summaryCheck = `import json,sys\nfrom cre_artifact_scope import require_canonical_artifact\nfrom cre_enrich import validate_enriched_artifact\nfrom cre_gate import count_artifacts\nfrom cre_monitor import load_artifact_groups\np=sys.argv[1]; a=json.load(open(p))\nfor f in (lambda: require_canonical_artifact(a),lambda: validate_enriched_artifact([],a),lambda: count_artifacts([p],True),lambda: load_artifact_groups([p])):\n try: f()\n except ValueError: continue\n raise AssertionError('unexpected summary admission')\n`;
  const summaryRefusal = spawnSync("python3", ["-c", summaryCheck, join(output, "run-summary.json")], {encoding: "utf8"});
  assert.equal(summaryRefusal.status, 0, summaryRefusal.stderr);
});

test("metadata denial halts later requests, but 404 continues", async () => {
  const items = validateObservationInput({items: [item(0), item(1), item(2)]});
  let calls = 0;
  const result = await observePages(items, async () => ({rawHtml: "", markdown: "", links: [], metadata: {statusCode: ++calls === 1 ? 404 : 429}}));
  assert.equal(calls, 2);
  const observations = result.parts[0].sources[0].retainedObservations as any[];
  assert.equal(observations[0].evidence.scrapedDoc.metadata.statusCode, 404);
  assert.equal(observations[1].evidence.scrapedDoc.metadata.statusCode, 429);
  assert.equal(observations[2].evidence.failure, "not_attempted_after_access_denial");
});

test("unknown remote settlement stops the remainder without erasing earlier evidence", async () => {
  const items = validateObservationInput({items: [item(0), item(1), item(2)]});
  let calls = 0;
  const result = await observePages(items, async () => {
    if (++calls === 2) throw createClientRequestDeadlineError(1);
    return {rawHtml: "<p>Partial</p>", markdown: "Partial", links: []};
  });
  const observations = result.parts[0].sources[0].retainedObservations as any[];
  assert.equal(calls, 2);
  assert.equal(observations[0].evidence.scrapedDoc.markdown, "Partial");
  assert.equal(observations[1].evidence.remoteSettlementUnknown, true);
  assert.equal(observations[2].evidence.failure, "not_attempted_after_uncertain_settlement");
});

test("exact challenge heading stops after retaining response, but contact CAPTCHA does not", async () => {
  const items = validateObservationInput({items: [item(0), item(1), item(2)]});
  let calls = 0;
  const result = await observePages(items, async () => {
    calls++;
    return calls === 1
      ? {rawHtml: '<title>Example Property</title><h1>Example Property</h1><iframe title="CAPTCHA"></iframe>', markdown: "Example Property", links: [], metadata: {statusCode: 200}}
      : {rawHtml: '<title> Just a moment... </title><h1>Please wait</h1>', markdown: "Please wait", links: [], metadata: {statusCode: 200}};
  });
  const observations = result.parts[0].sources[0].retainedObservations as any[];
  assert.equal(calls, 2);
  assert.equal(result.parts[0].sources[0].stopReason, "possible_challenge_header");
  assert.equal(result.parts[0].sources[0].evidenceIncomplete, true);
  assert.equal(observations[1].evidence.scrapedDoc.metadata.statusCode, 200);
  assert.equal(observations[2].evidence.failure, "not_attempted_after_possible_challenge_header");
});
