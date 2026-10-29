process.argv = [process.argv[0]!, process.argv[1]!];
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { spawnSync } from "node:child_process";
import { observePages, readObservationInput, validateObservationInput } from "../../../lib/page-observations/index.js";
import { finishPrivateCollectorArtifact, reservePrivateCollectorArtifact } from "../../../lib/source-evidence/artifact-output.js";
import { scrapeDoc } from "../../../lib/scrape.js";
import { createClientRequestDeadlineError } from "../../../lib/performance.js";

const item = (index: number) => ({ sourceKey: "leibsohn", url: `https://example.test/property/${index}`, context: { index, values: [null, false, ""] } });

test("serial observation retains 55 items without claiming listings or source identity", async () => {
  const items = validateObservationInput({ items: Array.from({ length: 55 }, (_, i) => item(i)) });
  let calls = 0;
  const result = await observePages(items, async () => { calls++; return { rawHtml: "", markdown: "", links: [], metadata: { statusCode: 200 } }; });
  assert.equal(calls, 55);
  assert.equal(result.runMeta.mode, "observe");
  assert.equal(result.runMeta.evidenceOnly, true);
  assert.deepEqual(result.listings, []); assert.deepEqual(result.brokers, []);
  assert.equal(result.sources[0].retainedObservations.length, 55);
  const first = result.sources[0].retainedObservations[0] as any;
  assert.deepEqual(first.input.context, item(0).context);
  assert.equal(first.input.identityBasis, "url");
  assert.equal(first.evidence.claim.externalId, null);
  assert.equal(first.evidence.sourceFreshness, "unverified");
  assert.deepEqual([result.sources[0].requested, result.sources[0].attempted, result.sources[0].returned, result.sources[0].failed, result.sources[0].notAttempted], [55, 55, 55, 0, 0]);
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
  const observations = result.sources[0].retainedObservations as any[];
  assert.equal(calls, 2);
  assert.equal(observations[1].evidence.localApiStatus, 403);
  assert.equal(observations[1].evidence.claim.externalId, "native-id");
  assert.equal(observations[2].evidence.failure, "not_attempted_after_access_denial");
  assert.deepEqual([result.sources[0].attempted, result.sources[0].returned, result.sources[0].failed, result.sources[0].notAttempted, result.sources[0].stopReason], [2, 1, 1, 1, "access_denial"]);
  assert.equal(result.sources[0].evidenceIncomplete, true);
  assert(!JSON.stringify(result).includes("private error"));
});

test("null source ID remains explicit and input size is checked on consumed bytes", async () => {
  const items = validateObservationInput({items: [item(0), {...item(1), sourceId: null}]});
  const result = await observePages(items, async () => ({rawHtml: "<p>Partial</p>", markdown: "Partial", links: []}));
  const observations = result.sources[0].retainedObservations as any[];
  assert.equal(Object.hasOwn(observations[0].input, "sourceId"), false);
  assert.equal(observations[1].input.sourceId, null);
  assert.equal(observations[1].input.identityBasis, "url");
  const dir = mkdtempSync(join(tmpdir(), "cre-observe-input-"));
  const path = join(dir, "input.json");
  writeFileSync(path, " ".repeat(4 * 1024 * 1024 + 1));
  assert.throws(() => readObservationInput(path));
  assert.throws(() => readObservationInput(dir));
});

test("private output is reserved before use and refuses replacement", () => {
  const dir = mkdtempSync(join(tmpdir(), "cre-observe-"));
  const out = join(dir, "artifact.json");
  const fd = reservePrivateCollectorArtifact(out);
  assert.throws(() => reservePrivateCollectorArtifact(out));
  finishPrivateCollectorArtifact(fd, JSON.stringify({ ok: true }));
  assert.equal(statSync(out).mode & 0o777, 0o600);
  assert.deepEqual(JSON.parse(readFileSync(out, "utf8")), {ok: true});
});

test("oversized context has an explicit omission", async () => {
  const items = validateObservationInput({items: [{...item(0), context: "x".repeat(65537)}]});
  const result = await observePages(items, async () => ({rawHtml: "", markdown: "", links: []}));
  const first = result.sources[0].retainedObservations[0] as any;
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
  writeFileSync(preload, `globalThis.fetch = async () => new Response(JSON.stringify({success:true,data:{rawHtml:'<p>Partial</p>',markdown:'Partial',links:[],metadata:{statusCode:200}}}),{status:200});`);
  const result = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts", "--observe-input", input, "--out", output], {encoding: "utf8"});
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, "");
  const artifact = JSON.parse(readFileSync(output, "utf8"));
  assert.equal(artifact.sources[0].retainedObservations.length, 55);
  assert.equal(artifact.runMeta.mode, "observe");
  assert.equal(artifact.totalListings, 0);
  assert.equal(statSync(output).mode & 0o777, 0o600);
  const python = `import json,sys\nfrom source_evidence import collector\nfrom cre_artifact_scope import require_canonical_artifact\nfrom cre_enrich import validate_enriched_artifact\na=json.load(open(sys.argv[1])); w=collector.convert(a,source_key='leibsohn'); assert collector.reconstruct(w)==a\nfor f in (lambda: require_canonical_artifact(a), lambda: validate_enriched_artifact([],a)):\n try: f()\n except ValueError: continue\n raise AssertionError('unexpected canonical admission')\n`;
  const check = spawnSync("python3", ["-c", python, output], {encoding: "utf8"});
  assert.equal(check.status, 0, check.stderr);
});

test("metadata denial halts later requests, but 404 continues", async () => {
  const items = validateObservationInput({items: [item(0), item(1), item(2)]});
  let calls = 0;
  const result = await observePages(items, async () => ({rawHtml: "", markdown: "", links: [], metadata: {statusCode: ++calls === 1 ? 404 : 429}}));
  assert.equal(calls, 2);
  const observations = result.sources[0].retainedObservations as any[];
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
  const observations = result.sources[0].retainedObservations as any[];
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
  const observations = result.sources[0].retainedObservations as any[];
  assert.equal(calls, 2);
  assert.equal(result.sources[0].stopReason, "possible_challenge_header");
  assert.equal(result.sources[0].evidenceIncomplete, true);
  assert.equal(observations[1].evidence.scrapedDoc.metadata.statusCode, 200);
  assert.equal(observations[2].evidence.failure, "not_attempted_after_possible_challenge_header");
});
