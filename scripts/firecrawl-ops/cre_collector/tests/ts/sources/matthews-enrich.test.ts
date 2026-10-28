// Real targeted resolver/adapter; provider transport alone is mocked.
process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import test, { type TestContext } from "node:test";
import { createMatthewsEnricher, groupEnrichItems, resolveEnricher, runEnrichGroups, type EnrichItem } from "../../../lib/enrich.js";
import { firecrawl } from "../../../lib/scrape.js";
import { MATTHEWS_FETCH_TIMEOUT_MS } from "../../../sources/matthews.js";
import { observationNow } from "../../../lib/observation-clock.js";

const sale = "https://www.matthews.com/properties/example-sale";
const lease = "https://www.matthews.com/properties/leasing-example";
const html = (canonical: string, body = `<h1 id="propertyTitle">Example Property</h1><div id="propertyAddress">123 Main St, Tulsa, OK 74103</div><div id="propertyPrice">$3,000,000</div>`) =>
  `<html><head><link rel="canonical" href="${canonical}"></head><body>${body}</body></html>`;
function item(url = sale, externalId = "example-sale", transaction: "sale" | "lease" = "sale"): EnrichItem {
  return { sourceKey: "matthews", externalId, url, transaction };
}

async function route(t: TestContext, items: EnrichItem[], response: (url: string, options: RequestInit) => Promise<Response>) {
  // Fail before any generic Firecrawl dispatch if the source is unregistered.
  assert.equal(resolveEnricher("matthews").label, "bespoke");
  const oldFetch = globalThis.fetch;
  const oldTimeout = globalThis.setTimeout;
  const oldScrape = firecrawl.scrape;
  let fallback = 0;
  let requests = 0;
  globalThis.fetch = async (url, options) => { requests++; return response(String(url), options ?? {}); };
  (firecrawl as any).scrape = async () => { fallback++; throw new Error("Forbidden generic fallback"); };
  // Preserve bounded header/body races, accelerate only synthetic timers and
  // provider pacing/backoff. No real sockets, provider calls or real delays.
  globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) =>
    oldTimeout(callback, (typeof delay === "number" && delay > MATTHEWS_FETCH_TIMEOUT_MS - 100 && delay <= MATTHEWS_FETCH_TIMEOUT_MS) ? 2 : 0, ...args)) as typeof setTimeout;
  t.after(() => { globalThis.fetch = oldFetch; globalThis.setTimeout = oldTimeout; (firecrawl as any).scrape = oldScrape; });
  const result = await runEnrichGroups(groupEnrichItems(items), resolveEnricher, () => "Matthews");
  assert.equal(fallback, 0);
  assert.equal(result.sources[0].enricher, "bespoke");
  return { ...result, requests };
}

for (const [url, id, tx] of [[sale, "example-sale", "sale"], [lease, "leasing-example", "lease"]] as const) {
  test(`Matthews targeted ${tx} uses canonical DOM and actual detail time only`, async (t) => {
    const before = Date.parse(observationNow());
    const result = await route(t, [item(url, id, tx)], async (requested, options) => {
      assert.equal(requested, url);
      assert.equal(options.redirect, "manual");
      assert.equal(options.cache, "no-store");
      assert.equal((options.headers as Record<string, string>)["Cache-Control"], "no-cache");
      assert.ok(options.signal);
      return new Response(html(url));
    });
    assert.equal(result.requests, 1);
    assert.equal(result.listings.length, 1);
    const row = result.listings[0];
    assert.equal(row.id, id);
    assert.equal(row.url, url);
    assert.equal(row.canonicalUrl, url);
    assert.equal(row.name, "Example Property");
    assert.equal(row.transactionMode, tx);
    assert.equal(row.transactionType, tx === "sale" ? "Sale" : "Lease");
    assert.equal(tx === "sale" ? row.salePriceUsd : row.leaseRateText, tx === "sale" ? 3000000 : "$3,000,000");
    assert.ok(Date.parse(row.detailObservedAt) >= before && Date.parse(row.detailObservedAt) <= Date.parse(observationNow()));
    assert.equal(Object.hasOwn(row, "inventoryObservedAt"), false);
    assert.equal(Object.hasOwn(row, "freshnessProvenance"), false);
    assert.equal(Object.hasOwn(row, "generationId"), false);
  });
}

for (const [name, seed] of [
  ["foreign host", item("https://elsewhere.invalid/properties/example-sale")],
  ["HTTP", item(sale.replace("https:", "http:"))],
  ["userinfo", item(sale.replace("www.", "user:secret@www."))],
  ["port", item(sale.replace(".com/", ".com:444/"))],
  ["query", item(sale + "?other=1")],
  ["fragment", item(sale + "#other")],
  ["relative URL", item("/properties/example-sale")],
  ["not property", item("https://www.matthews.com/listings")],
  ["wrong queued ID", item(sale, "other-property")],
  ["folded ID", item(sale, "main:example-sale")],
  ["case changed ID", item(sale, "EXAMPLE-SALE")],
  ["wrong tenure", item(sale, "example-sale", "lease")],
] as const) {
  test(`Matthews refuses ${name} before transport`, async (t) => {
    const result = await route(t, [seed], async () => { throw new Error("Must not fetch invalid seed"); });
    assert.equal(result.requests, 0);
    assert.deepEqual(result.listings, []);
  });
}

for (const [name, body] of [
  ["canonical mismatch", html(sale.replace("example-sale", "other-property"))],
  ["canonical missing", html(sale).replace(/<link[^>]+>/, "")],
  ["canonical tenure mismatch", html(lease)],
  ["captcha", html(sale, `<h1 id="propertyTitle">Just a moment...</h1><div id="propertyAddress">Verify you are human</div>`)],
  ["generic shell", html(sale, `<h1>Generic JSON-LD Property</h1><script type="application/ld+json">{"@type":"Product","name":"Generic"}</script>`)],
  ["provider not found", html(sale, `<script>self.__next_f.push([1,"NEXT_REDIRECT;replace;/listings;307;"])</script><script>self.__next_f.push([1,"404 - Page Not Found"])</script>`)],
] as const) {
  test(`Matthews omits ${name} without generic fallback`, async (t) => {
    const result = await route(t, [item()], async () => new Response(body));
    assert.equal(result.requests, 1);
    assert.deepEqual(result.listings, []);
  });
}

for (const status of [301, 308, 302, 307, 404]) {
  test(`Matthews targeted HTTP ${status} cannot rekey or complete an alias`, async (t) => {
    const result = await route(t, [item()], async () => new Response(null, { status, headers: { location: status === 307 ? "" : sale.replace("example-sale", "other-property") } }));
    assert.equal(result.requests, 1);
    assert.deepEqual(result.listings, []);
  });
}

for (const phase of ["headers", "body", "transport"] as const) {
  test(`Matthews targeted ${phase} failure exhausts shared bounded retries and omits`, async (t) => {
    const signals: AbortSignal[] = [];
    const result = await route(t, [item()], async (_url, options) => {
      signals.push(options.signal as AbortSignal);
      if (phase === "headers") return new Promise<Response>(() => {});
      if (phase === "transport") throw new Error("Synthetic transport error");
      return new Response(new ReadableStream<Uint8Array>({ pull: () => new Promise<void>(() => {}) }));
    });
    assert.equal(result.requests, 6);
    assert.deepEqual(result.listings, []);
    if (phase !== "transport") assert.ok(signals.every(signal => signal.aborted));
  });
}

test("Matthews normalized percent escapes keep native ID exact and original completion URL", async (t) => {
  const requested = "https://www.matthews.com/properties/%c2%b1-example/";
  const canonical = requested.slice(0, -1).replace("%c2%b1", "%C2%B1");
  const result = await route(t, [item(requested, "%c2%b1-example")], async () => new Response(html(canonical)));
  assert.equal(result.listings.length, 1);
  assert.equal(result.listings[0].id, "%c2%b1-example");
  assert.equal(result.listings[0].url, requested);
  assert.equal(result.listings[0].canonicalUrl, requested.slice(0, -1));
});

test("Matthews failed item stays omitted while a valid same-batch item completes", async (t) => {
  const result = await route(t, [item(), item(lease, "leasing-example", "lease")], async url => new Response(url === sale ? html(sale, "<h1>Access denied</h1>") : html(lease)));
  assert.equal(result.requests, 2);
  assert.equal(result.listings.length, 1);
  assert.equal(result.listings[0].id, "leasing-example");
});


test("actual Python lease claim defaults sale and cannot falsely complete Matthews detail", async (t) => {
  const cwd = fileURLToPath(new URL("../../../", import.meta.url));
  const claim = ["synthetic-lease-claim", "matthews", "leasing-example", lease, "changed", "0"];
  const converted = spawnSync("python3", ["-c", [
    "import json,sys",
    "from cre_enrich import _parse_claimed, claim_rows_to_items",
    "claimed = _parse_claimed([json.loads(sys.stdin.read())])",
    "print(json.dumps({'claimed': claimed, **claim_rows_to_items(claimed)}))",
  ].join("\n")], { cwd, input: JSON.stringify(claim), encoding: "utf8", timeout: 10_000, maxBuffer: 64 * 1024 });
  assert.equal(converted.error, undefined);
  assert.equal(converted.status, 0, converted.stderr);
  const payload = JSON.parse(converted.stdout);
  assert.equal(payload.items[0].transaction, "sale");
  assert.equal(payload.items[0].externalId, "leasing-example");
  const result = await route(t, payload.items, async () => { throw new Error("Mismatched queue tenure must not fetch"); });
  assert.equal(result.requests, 0);
  assert.deepEqual(result.listings, []);

  const partition = spawnSync("python3", ["-c", [
    "import json,sys",
    "from cre_enrich import select_done_and_retry",
    "data = json.loads(sys.stdin.read())",
    "done,retry,dead = select_done_and_retry(data['claimed'], data['listings'])",
    "print(json.dumps({'done': done, 'retry': retry, 'dead': dead}))",
  ].join("\n")], { cwd, input: JSON.stringify({ claimed: payload.claimed, listings: result.listings }), encoding: "utf8", timeout: 10_000, maxBuffer: 64 * 1024 });
  assert.equal(partition.error, undefined);
  assert.equal(partition.status, 0, partition.stderr);
  assert.deepEqual(JSON.parse(partition.stdout), { done: [], retry: ["synthetic-lease-claim"], dead: [] });
});


test("Matthews evidence retry only follows a validated same-source temporary redirect", async (t) => {
  const oldFetch = globalThis.fetch;
  const oldTimeout = globalThis.setTimeout;
  let location = "";
  globalThis.fetch = async () => new Response(null, { status: 307, headers: { location } });
  globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) =>
    oldTimeout(callback, (typeof delay === "number" && delay > MATTHEWS_FETCH_TIMEOUT_MS - 100 && delay <= MATTHEWS_FETCH_TIMEOUT_MS) ? 2 : 0, ...args)) as typeof setTimeout;
  t.after(() => { globalThis.fetch = oldFetch; globalThis.setTimeout = oldTimeout; });
  const called: string[] = [];
  const enricher = createMatthewsEnricher(undefined, async (url) => {
    called.push(url);
    return { rawHtml: '<script type="application/ld+json">{"@type":"Product","name":"Not canonical"}</script><a href="brochure.pdf">Brochure</a>', markdown: "native", links: [] };
  });
  for (const bad of ["", "https://evil.test/properties/other-sale", "https://user:secret@www.matthews.com/properties/other-sale", "http://www.matthews.com/properties/other-sale", "/login", "/properties/other-sale?auth=1"]) {
    location = bad;
    const result = await enricher.enrichWithEvidence!([item()]);
    assert.deepEqual(result.listings, []);
    assert.deepEqual(result.retainedObservations, []);
    assert.equal(called.length, 0);
  }
  location = "/properties/other-sale/";
  const result = await enricher.enrichWithEvidence!([item()]);
  assert.deepEqual(called, ["https://www.matthews.com/properties/other-sale/"]);
  assert.deepEqual(result.listings, []);
  const observation = result.retainedObservations[0] as any;
  assert.equal(observation.claim.url, sale);
  assert.equal(observation.primaryOutcome.reason, "temporary_redirect");
  assert.equal(observation.primaryOutcome.status, 307);
  assert.equal(observation.primaryOutcome.redirectTarget, called[0]);
  assert.equal(observation.requestedPageUrl, called[0]);
  assert.equal(observation.pageContext.sourceUrl, called[0]);
  assert.equal(new URL(observation.pageContext.links[0].href, observation.pageContext.sourceUrl).href,
    "https://www.matthews.com/properties/other-sale/brochure.pdf");
});

test("Matthews evidence keeps mixed claims separate and skips fallback for canonical rows", async () => {
  const calls: string[] = [];
  const primary: Parameters<typeof createMatthewsEnricher>[0] = async (claim, _time, observe) => {
    if (claim.externalId === "one") return { id: "one", url: claim.url };
    observe?.({ reason: "temporary_redirect", status: 307, redirectTarget: "https://www.matthews.com/properties/other-sale" });
    return null;
  };
  const enricher = createMatthewsEnricher(primary, async (url) => { calls.push(url); return { rawHtml: "partial", markdown: "", links: [] }; });
  const result = await enricher.enrichWithEvidence!([item(sale, "one"), item(sale, "two")]);
  assert.equal(result.listings.length, 1);
  assert.equal(calls.length, 1);
  assert.equal((result.retainedObservations[0] as any).claim.externalId, "two");
});

test("unqualified native HTML is retained without a second request", async (t) => {
  const result = await route(t, [item()], async () => new Response(html(sale, '<main>Property context</main>')));
  assert.equal(result.requests, 1);
  assert.deepEqual(result.listings, []);
  assert.equal(result.sources[0].retainedObservations.length, 1);
  const evidence = result.sources[0].retainedObservations[0];
  assert.equal(evidence.primaryOutcome.reason, "native_detail_unqualified");
  assert.match(evidence.scrapedDoc.rawHtml, /Property context/);
  assert.ok(evidence.capturedAt);
});

for (const status of [401, 403, 429, 451]) {
  test(`Matthews denial ${status} never triggers evidence retry`, async (t) => {
    const result = await route(t, [item()], async () => new Response(null, { status }));
    assert.deepEqual(result.listings, []);
    assert.deepEqual(result.sources[0].retainedObservations, []);
  });
}
