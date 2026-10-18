process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test, { type TestContext } from "node:test";
import { observationNow } from "../../../lib/observation-clock.js";
import { INTERRA_INVENTORY_URL, srcInterraRealty } from "../../../sources/interra-realty.js";

const generationId = "interra-proof-generation";
const digest = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");
function row(id: number, status: string): any {
  return { ID: id, post_title: `Synthetic Café ${id}`, post_content: "<p>Synthetic.</p>", permalink: `https://interrarealty.com/listing/property-${id}/`, meta: { listing_status: status, listing_address: "123 Main St", listing_city: "Chicago, IL 60622", key_details: "0" } };
}
const payload = () => ({ success: true, has_next_page: false, posts: [row(2, "available"), row(10, "under_contract"), row(31, "closed")] });
function context(t: TestContext, strict = true) {
  const keys = ["CRE_REFRESH_GENERATION", "CRE_REFRESH_STARTED_AT", "CRE_REQUIRE_FRESH_DETAILS"];
  const original = keys.map(key => process.env[key]);
  process.env.CRE_REFRESH_GENERATION = generationId;
  process.env.CRE_REFRESH_STARTED_AT = new Date(Date.now() - 1000).toISOString();
  if (strict) process.env.CRE_REQUIRE_FRESH_DETAILS = "1"; else delete process.env.CRE_REQUIRE_FRESH_DETAILS;
  t.after(() => keys.forEach((key, i) => original[i] === undefined ? delete process.env[key] : process.env[key] = original[i]));
}

test("strict Interra source retains actual terminal text hash, complete native lifecycle partition and emitted-ID binding", async t => {
  context(t); const body = JSON.stringify(payload()); let received = 0; let calls = 0;
  const fetchImpl: typeof fetch = async (url, options) => {
    assert.equal(String(url), INTERRA_INVENTORY_URL);
    assert.equal(options?.cache, "no-store"); assert.equal(options?.redirect, "error");
    calls++; received = Date.parse(observationNow()); return new Response(body);
  };
  const result = await srcInterraRealty("sale", Infinity, false, fetchImpl);
  const proof = (result as any).currentInventoryProof;
  assert.ok(proof, "strict source must retain terminal-feed proof");
  assert.equal(calls, 1);
  assert.equal(proof.schema, "interra-current-inventory-v1");
  assert.equal(proof.sourceKey, "interra-realty"); assert.equal(proof.transaction, "sale");
  assert.equal(proof.kind, "terminal_feed"); assert.equal(proof.generationId, generationId);
  assert.equal(proof.endpoint, INTERRA_INVENTORY_URL); assert.equal(proof.hasNextPage, false);
  assert.equal(proof.responseTextSha256, digest(body));
  assert.ok(Date.parse(proof.observedAt) >= received);
  assert.deepEqual(proof.nativeIdsByLifecycle, { available: ["2"], under_contract: ["10"], closed: ["31"] });
  assert.equal(proof.emittedNativeIdsSha256, digest('["10","2"]'));
  assert.deepEqual(result.listings.map(row => row.id), ["2", "10"]);
  assert.equal(result.totalAvailable, 2); assert.equal(result.truncated, false);
  for (const listing of result.listings) {
    assert.equal(listing.inventoryObservedAt, proof.observedAt);
    assert.equal(listing.detailObservedAt, proof.observedAt);
    assert.equal(listing.freshnessProvenance.generationId, generationId);
    assert.equal(listing.freshnessProvenance.detailScope, "authoritative_inventory_feed");
    assert.equal(listing.preserveChildCollections, true);
  }
});

test("strict empty lease receipt names the reviewed sale-only reason without a fake fetch or observation", async t => {
  context(t);
  const result = await srcInterraRealty("lease", Infinity, false, async () => { throw new Error("Lease must not fetch inventory"); });
  assert.deepEqual((result as any).currentInventoryProof, { schema: "interra-current-inventory-v1", sourceKey: "interra-realty", transaction: "lease", generationId, kind: "known_empty_transaction", reason: "sale_only_source", count: 0 });
  assert.deepEqual(result.listings, []); assert.equal(result.totalAvailable, 0);
});

test("finite cap retains full feed IDs but binds only actual emitted IDs and remains truncated", async t => {
  context(t);
  const result = await srcInterraRealty("sale", 1, false, async () => Response.json(payload()));
  const proof = (result as any).currentInventoryProof;
  assert.ok(proof); assert.equal(result.truncated, true); assert.equal(result.listings.length, 1);
  assert.deepEqual(proof.nativeIdsByLifecycle, { available: ["2"], under_contract: ["10"], closed: ["31"] });
  assert.equal(proof.emittedNativeIdsSha256, digest('["2"]'));
});

test("partition/digest are deterministic under provider order changes without reordering emitted listings", async t => {
  context(t); const first = payload(); const second = { ...payload(), posts: [row(31, "closed"), row(10, "under_contract"), row(2, "available")] };
  const a = await srcInterraRealty("sale", Infinity, false, async () => Response.json(first));
  const b = await srcInterraRealty("sale", Infinity, false, async () => Response.json(second));
  assert.ok((a as any).currentInventoryProof);
  assert.deepEqual((a as any).currentInventoryProof.nativeIdsByLifecycle, (b as any).currentInventoryProof.nativeIdsByLifecycle);
  assert.equal((a as any).currentInventoryProof.emittedNativeIdsSha256, (b as any).currentInventoryProof.emittedNativeIdsSha256);
  assert.notEqual((a as any).currentInventoryProof.responseTextSha256, (b as any).currentInventoryProof.responseTextSha256);
  assert.deepEqual(b.listings.map(row => row.id), ["10", "2"]);
});

test("ordinary Interra mode keeps its existing shape without claiming a strict receipt", async t => {
  context(t, false);
  const result = await srcInterraRealty("sale", Infinity, false, async () => Response.json(payload()));
  const lease = await srcInterraRealty("lease", Infinity, false, async () => { throw new Error("No lease fetch"); });
  assert.equal(Object.hasOwn(result, "currentInventoryProof"), false);
  assert.equal(Object.hasOwn(lease, "currentInventoryProof"), false);
  assert.deepEqual(result.listings.map(row => row.id), ["2", "10"]);
});

const collector = fileURLToPath(new URL("../../../", import.meta.url));
function invoke(mutation?: string, monitor = false) {
  const dir = mkdtempSync(join(tmpdir(), "interra-proof-cli-"));
  const feed: any = payload();
  if (mutation === "duplicate") feed.posts[1] = row(2, "under_contract");
  if (mutation === "unknown-lifecycle") feed.posts[1].meta.listing_status = "unknown";
  if (mutation === "missing-terminal") delete feed.has_next_page;
  if (mutation === "partial") feed.has_next_page = true;
  if (mutation === "bad-url") feed.posts[0].permalink = "https://foreign.invalid/listing/identity/";
  if (mutation === "bad-id") feed.posts[0].ID = 2.5;
  const body = mutation === "malformed-json" ? "{" : JSON.stringify(feed);
  const out = join(dir, "artifact.json"), calls = join(dir, "calls.jsonl"), preload = join(dir, "preload.mts");
  writeFileSync(preload, `import { appendFileSync } from "node:fs";
globalThis.fetch = async (url, options) => {
 if (String(url) !== ${JSON.stringify(INTERRA_INVENTORY_URL)}) throw new Error("Unexpected mocked endpoint");
 appendFileSync(${JSON.stringify(calls)}, JSON.stringify({url:String(url),cache:options?.cache,redirect:options?.redirect})+"\\n");
 return new Response(${JSON.stringify(body)});
};`);
  const start = new Date(Date.now() - 1000).toISOString();
  const env = { ...process.env, CRE_REQUIRE_FRESH_DETAILS: "1", CRE_REFRESH_GENERATION: generationId, CRE_REFRESH_STARTED_AT: start };
  const child = spawnSync(process.execPath, ["--import", "tsx", "--import", preload, "collect.ts", "--source", "interra-realty", "--transaction", "both", "--max-items", mutation === "cap" ? "1" : "0", "--concurrency", "1", "--out", out, ...(monitor || mutation === "monitor" ? ["--monitor"] : [])], { cwd: collector, env, encoding: "utf8", timeout: 15_000, maxBuffer: 256 * 1024 });
  const result = { child, start, body, data: existsSync(out) ? JSON.parse(readFileSync(out, "utf8")) : null, calls: existsSync(calls) ? readFileSync(calls, "utf8").trim().split("\n").filter(Boolean).map(line => JSON.parse(line)) : [] };
  rmSync(dir, { recursive: true, force: true }); return result;
}

test("actual collect forwards exact terminal and sale-only receipts through both strict full transaction passes", () => {
  const result = invoke(); assert.equal(result.child.status, 0, result.child.stderr);
  assert.equal(result.data.runMeta.mode, "full"); assert.deepEqual(result.data.runMeta.transactions, ["sale", "lease"]);
  assert.equal(result.data.runMeta.maxItemsPerSource, null);
  assert.equal(result.data.runMeta.freshness.generationId, generationId);
  assert.equal(result.data.runMeta.freshness.generationStartedAt, result.start);
  assert.equal(result.calls.length, 1);
  const [sale, lease] = result.data.sources;
  assert.ok(sale.currentInventoryProof);
  assert.equal(sale.currentInventoryProof.responseTextSha256, digest(result.body));
  assert.equal(sale.currentInventoryProof.generationId, generationId);
  assert.equal(sale.currentInventoryProof.emittedNativeIdsSha256, digest('["10","2"]'));
  assert.deepEqual(sale.currentInventoryProof.nativeIdsByLifecycle.closed, ["31"]);
  assert.equal(sale.listingsCollected, 2); assert.equal(sale.truncated, false);
  assert.equal(lease.currentInventoryProof.kind, "known_empty_transaction");
  assert.equal(lease.currentInventoryProof.reason, "sale_only_source");
  assert.equal(lease.currentInventoryProof.generationId, generationId);
  assert.equal(Object.hasOwn(lease.currentInventoryProof, "observedAt"), false);
  assert.equal(lease.listingsCollected, 0); assert.equal(lease.truncated, false);
  assert.equal(result.data.totalListings, 2);
  assert.deepEqual(result.data.listings.map((row: any) => row.id), ["2", "10"]);
});

for (const mutation of ["duplicate", "unknown-lifecycle", "missing-terminal", "partial", "bad-url", "bad-id", "malformed-json"]) {
  test(`actual collect retains a source error rather than creating proof for ${mutation}`, () => {
    const result = invoke(mutation);
    assert.equal(result.child.status, 1);
    assert.equal(result.data, null);
    assert.match(result.child.stderr, /interra-realty\/sale FAILED/);
    assert.match(result.child.stderr, /no listings collected/);
    assert.equal(result.calls.length, 1);
  });
}

test("actual collect cap and monitor roles remain visible and cannot be mistaken for unlimited full proof", () => {
  const capped = invoke("cap"); assert.equal(capped.child.status, 0, capped.child.stderr);
  assert.equal(capped.data.runMeta.maxItemsPerSource, 1);
  assert.equal(capped.data.sources[0].truncated, true);
  assert.equal(capped.data.sources[0].currentInventoryProof.emittedNativeIdsSha256, digest('["2"]'));
  const monitor = invoke("monitor"); assert.equal(monitor.child.status, 0, monitor.child.stderr);
  assert.equal(monitor.data.runMeta.mode, "monitor");
  assert.equal(monitor.data.listings.length, 2);
});

test("errored monitor output retains a source error with no terminal proof and cannot masquerade as full", () => {
  const result = invoke("duplicate", true);
  assert.equal(result.child.status, 0, result.child.stderr);
  assert.equal(result.data.runMeta.mode, "monitor");
  assert.ok(result.data.sources[0].error);
  assert.equal(Object.hasOwn(result.data.sources[0], "currentInventoryProof"), false);
  assert.equal(result.data.listings.length, 0);
});
