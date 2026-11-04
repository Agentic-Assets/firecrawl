// AGENTIC-4357: complete-source recovery; all transport is synthetic.
process.argv = [process.argv[0]!, process.argv[1]!];
import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { srcJll, enrichJllListing, writeJllDetailCache, readJllDetailCache, jllGraphqlItemToListing, JLL_DETAIL_CONCURRENCY } from "../../../sources/jll.js";
import { firecrawl } from "../../../lib/scrape.js";
import { summarizeListingFreshness } from "../../../lib/freshness.js";
const url = "https://property.jll.com/listings/recovery-fixture";
const goodUrl = "https://property.jll.com/listings/good-fixture";
const observedAt = "2020-01-01T00:00:01.000Z";
function doc(id = "bad", pageUrl = "/listings/recovery-fixture") {
  return { rawHtml: `<script id="__NEXT_DATA__">${JSON.stringify({ props: { pageProps: { property: { id, pageUrl, title: "Synthetic target", description: "Useful property facts", hidePrice: false }, brokers: [] } } })}</script>`, markdown: "Useful property facts", links: [], images: [], metadata: { statusCode: 200, sourceURL: url } };
}
function missing(kind = "404") {
  return { rawHtml: kind === "challenge" ? "<h1>Verify you are human</h1>" : `<script id="__NEXT_DATA__">${JSON.stringify({ page: ["404", "foreign", "native-challenge"].includes(kind) ? "/404" : "/listing", props: { pageProps: {} } })}</script>`, markdown: "", links: [], images: [], metadata: { statusCode: kind === "missing" ? 200 : 404, sourceURL: kind === "foreign" ? "https://foreign.example/" : url, url } };
}
function setup(t: TestContext, extra404 = false) {
  const directory = mkdtempSync(join(tmpdir(), "jll-late-"));
  const env = { JLL_DETAIL_CACHE_DIR: directory, JLL_DETAIL_CACHE_MIN_CACHED_AT: "2020-01-01T00:00:00.000Z", CRE_REFRESH_GENERATION: "late-fixture", CRE_REFRESH_STARTED_AT: "2020-01-01T00:00:00.000Z", CRE_REQUIRE_FRESH_DETAILS: "1", CRE_SCRAPE_MAX_ATTEMPTS: "1" };
  const before = Object.keys(env).map(k => [k, process.env[k]]);
  Object.assign(process.env, env);
  t.after(() => { for (const [k, v] of before) {
    if (v === undefined)
      delete process.env[k!];
    else
      process.env[k!] = v;
  } rmSync(directory, { recursive: true, force: true }); });
  t.mock.method(globalThis, "fetch", async (_input: unknown, init: RequestInit | undefined) => {
    const body = JSON.parse(String(init?.body));
    assert.equal(body.operationName, "SearchResults");
    const items = body.variables.propertyTypes[0] === "office" ? [
      { id: "good", title: "Good", pageUrl: "/listings/good-fixture", hidePrice: false, tenureTypes: ["rent"] },
      { id: "bad", title: "Bad", pageUrl: "/listings/recovery-fixture", hidePrice: false, tenureTypes: ["rent"] },
    ] : [];
    if (extra404 && body.variables.propertyTypes[0] === "office") {
      items.push({id:"extra",title:"Extra",pageUrl:"/listings/extra",hidePrice:false,tenureTypes:["rent"]});
    }
    return Response.json({ data: { properties: { count: items.length, items } } });
  });
  const good = doc("good", "/listings/good-fixture");
  writeJllDetailCache(goodUrl, { ...good, detailObservation: { observedAt, generationId: "late-fixture", method: "jll_detail", cacheDisposition: "live" } });
  writeJllDetailCache(url, missing());
  return async () => srcJll("lease", Infinity, false);
}
test("late completed native404 recovers only failed row; good cache facts and clocks stay exact", async (t) => {
  const run = setup(t);
  const logs: string[] = [];
  t.mock.method(console, "error", (...parts: unknown[]) => logs.push(parts.join(" ")));
  const before = await enrichJllListing({ ...jllGraphqlItemToListing({ id: "good", title: "Good", pageUrl: "/listings/good-fixture", hidePrice: false, tenureTypes: ["rent"] }, "lease", "office", 1, 2), inventoryObservedAt: observedAt });
  let calls = 0;
  t.mock.method(firecrawl, "scrape", async (request: string) => { assert.equal(request, url); calls++; return calls === 1 ? missing() : doc(); });
  const result = await run();
  assert.equal(calls, 2);
  assert.deepEqual(logs.filter(line => line.includes("late detail retry")), [
    `  jll/lease: late detail retry bad ${url} (completed native404)`,
    `  jll/lease: late detail retry bad ${url}: validated`,
  ]);
  assert.equal(result.listings[1].detailError, undefined);
  const good = result.listings[0];
  assert.deepEqual({ ...good, inventoryObservedAt: observedAt }, before);
  assert.equal(good.detailObservedAt, observedAt);
  assert.equal(summarizeListingFreshness(result.listings).detailErrors, 0);
});
test("persistent native404 remains an error after exactly one late pass", async (t) => {
  const logs: string[] = [];
  t.mock.method(console, "error", (...parts: unknown[]) => logs.push(parts.join(" ")));
  const run = setup(t);
  let calls = 0;
  t.mock.method(firecrawl, "scrape", async () => { calls++; return missing(); });
  const result = await run();
  assert.equal(calls, 3); // original fallback, then the existing primary/fallback reader once.
  assert.deepEqual(logs.filter(line => line.includes("late detail retry")), [
    `  jll/lease: late detail retry bad ${url} (completed native404)`,
    `  jll/lease: late detail retry bad ${url}: failed`,
  ]);
  assert.match(result.listings[1].detailError, /missing property/);
  assert.equal(summarizeListingFreshness(result.listings).detailErrors, 1);
});
for (const kind of ["timeout", "challenge", "missing", "foreign", "wrong-id", "wrong-url"]) {
  test(`old cached404 cannot authorize late dispatch after latest ${kind}`, async (t) => {
    const run = setup(t);
    let calls = 0;
    t.mock.method(firecrawl, "scrape", async () => {
      calls++;
      if (kind === "timeout")
        throw Object.assign(new Error("request timed out"), { name: "AbortError" });
      if (kind === "wrong-id")
        return doc("other");
      if (kind === "wrong-url")
        return doc("bad", "/listings/other");
      return missing(kind);
    });
    const result = await run();
    assert.equal(calls, 1);
    assert.ok(result.listings[1].detailError);
    if (kind === "timeout")
      assert.equal(readJllDetailCache(url)?.metadata?.statusCode, 404);
  });
}
for (const flaw of ["no-generation", "stale-inventory", "missing-inventory", "missing-id", "wrong-source", "wrong-final", "non404-status", "non-native-page", "challenge-with-native-marker"]) {
  test(`completed404 eligibility excludes ${flaw}`, async (t) => {
    setup(t);
    const native404 = missing();
    if (flaw === "wrong-source")
      native404.metadata.sourceURL = "https://foreign.example/";
    if (flaw === "wrong-final")
      native404.metadata.url = `${url}-other`;
    if (flaw === "non404-status")
      native404.metadata.statusCode = 200;
    if (flaw === "non-native-page")
      native404.rawHtml = native404.rawHtml.replace("/404", "/other");
    if (flaw === "challenge-with-native-marker")
      native404.rawHtml += "<h1>Verify you are human</h1>";
    if (flaw === "no-generation")
      delete process.env.CRE_REFRESH_GENERATION;
    let eligible = 0;
    t.mock.method(firecrawl, "scrape", async () => native404);
    const row = await enrichJllListing({ id: flaw === "missing-id" ? undefined : "bad", url, inventoryObservedAt: flaw === "missing-inventory" ? undefined : flaw === "stale-inventory" ? "2019-01-01T00:00:00Z" : observedAt }, { onCompletedNative404: () => eligible++ });
    assert.ok(row.detailError);
    assert.equal(eligible, 0);
  });
}
for (const flaw of ["wrong-id", "wrong-url", "challenge"]) {
  test(`late ${flaw} still refuses the failed member`, async (t) => {
    const run = setup(t);
    let calls = 0;
    t.mock.method(firecrawl, "scrape", async () => { calls++; return calls === 1 ? missing() : flaw === "wrong-id" ? doc("other") : flaw === "wrong-url" ? doc("bad", "/listings/other") : missing("challenge"); });
    const result = await run();
    assert.ok(result.listings[1].detailError);
    assert.equal(summarizeListingFreshness(result.listings).detailErrors, 1);
    assert.equal(calls, flaw === "challenge" ? 3 : 2);
  });
}
test("cross-generation404 cache cannot supply latest eligibility after live timeout", async (t) => {
  const run = setup(t);
  process.env.CRE_REFRESH_GENERATION = "new-generation";
  let calls = 0;
  t.mock.method(firecrawl, "scrape", async () => {
    calls++;
    throw Object.assign(new Error("request timed out"), { name: "AbortError" });
  });
  const result = await run();
  assert.equal(calls, 2); // both cross-generation cached documents were refused.
  assert.equal(readJllDetailCache(url), null);
  assert.equal(summarizeListingFreshness(result.listings).detailErrors, 2);
});

for (const failure of ["timeout", "wrong-id"]) {
  test(`mixed completed404 and ${failure} skips the entire late pass`, async t => {
    const run = setup(t, true);
    const calls: string[] = [];
    t.mock.method(firecrawl, "scrape", async (request: string) => {
      calls.push(request);
      if (request === url) {
        if (failure === "timeout") {
          throw Object.assign(new Error("request timed out"), { name: "AbortError" });
        }
        return doc("other");
      }
      assert.equal(request, "https://property.jll.com/listings/extra");
      const response = missing();
      response.metadata.sourceURL = request;
      response.metadata.url = request;
      return response;
    });
    const result = await run();
    assert.equal(calls.filter(value => value === url).length, 1);
    assert.equal(calls.filter(value => value.endsWith("/extra")).length, 2);
    assert.equal(summarizeListingFreshness(result.listings).detailErrors, 2);
  });
}

test("late pass starts after all initial work, preserves order and the existing concurrency bound", async t => {
  const run = setup(t, true);
  const counts = new Map<string, number>();
  let active = 0;
  let maxActive = 0;
  t.mock.method(firecrawl, "scrape", async (request: string) => {
    const count = (counts.get(request) ?? 0) + 1;
    counts.set(request, count);
    active++;
    maxActive = Math.max(maxActive, active);
    await new Promise<void>(resolve => setImmediate(resolve));
    active--;
    const late = request === url ? count === 2 : count === 3;
    if (late) {
      assert.ok((counts.get(url) ?? 0) >= 1);
      assert.ok((counts.get("https://property.jll.com/listings/extra") ?? 0) >= 2);
      return request === url ? doc() : doc("extra", "/listings/extra");
    }
    const response = missing();
    response.metadata.sourceURL = request;
    response.metadata.url = request;
    return response;
  });
  const result = await run();
  assert.deepEqual(result.listings.map(row => row.id), ["good", "bad", "extra"]);
  assert.equal(summarizeListingFreshness(result.listings).detailErrors, 0);
  assert.equal(counts.get(url), 2);
  assert.equal(counts.get("https://property.jll.com/listings/extra"), 3);
  assert.ok(maxActive <= JLL_DETAIL_CONCURRENCY);
});

for (const phrase of ["Just a moment...", "Sorry, you have been blocked"]) {
  test(`native404-shaped explicit challenge is not retry eligible: ${phrase}`, async t => {
    setup(t);
    let eligible = 0;
    let calls = 0;
    const response = missing();
    response.rawHtml = `<title>${phrase}</title>${response.rawHtml}`;
    t.mock.method(firecrawl, "scrape", async () => {
      calls++;
      return response;
    });
    const row = await enrichJllListing(
      { id: "bad", url, inventoryObservedAt: observedAt },
      { onCompletedNative404: () => eligible++ },
    );
    assert.ok(row.detailError);
    assert.equal(calls, 1);
    assert.equal(eligible, 0);
  });
}
