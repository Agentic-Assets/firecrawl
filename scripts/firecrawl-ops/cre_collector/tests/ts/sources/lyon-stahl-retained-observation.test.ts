import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { RetainedSourceError } from "../../../lib/source-observations.js";
import { srcLyonStahl } from "../../../sources/lyon-stahl.js";

const originalUrl = "https://lyonstahl.com/properties/11863-nebraska-ave-los-angeles-ca-90025/";
const original = readFileSync(new URL("../../fixtures/lyon-stahl-product-offer.html", import.meta.url), "utf8");

async function collectWithClock(t: import("node:test").TestContext) {
  let settled = false;
  const pending = srcLyonStahl("sale", Infinity, false);
  void pending.then(() => { settled = true; }, () => { settled = true; });
  for (let tick = 0; !settled && tick < 400; tick++) {
    await new Promise<void>((resolve) => setImmediate(resolve));
    if (!settled) t.mock.timers.tick(250);
  }
  assert.ok(settled, "synthetic source must settle");
  return pending;
}

test("Lyon keeps held source context without promoting a listing", async (t) => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 100_000 });
  t.mock.method(performance, "now", () => Date.now());
  const body = original.replace(/("availability"\s*:\s*)"https:\/\/schema.org\/SoldOut"/, '$1"NewUnknownStatus"');
  const calls: string[] = [];
  const oldFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = oldFetch; });
  globalThis.fetch = async (input) => {
    const url = String(input); calls.push(url);
    if (url === "https://lyonstahl.com/sitemap.xml") return new Response('<sitemapindex><sitemap><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemap></sitemapindex>', { headers: { "content-type": "text/xml" } });
    if (url.endsWith("properties-sitemap1.xml")) return new Response(`<urlset><url><loc>${originalUrl}</loc></url></urlset>`, { headers: { "content-type": "text/xml" } });
    assert.equal(url, originalUrl);
    return new Response(body, { headers: { "content-type": "text/html" } });
  };
  const result = await collectWithClock(t);
  assert.deepEqual(result.listings, []);
  assert.equal(result.truncated, true);
  assert.equal(calls.length, 3);
  const observations = result.retainedObservations as Array<{ disposition: string; sourceUrl: string; pageContext: { schema: string }; detailObservedAt: string }>;
  assert.equal(observations.length, 1);
  assert.equal(observations[0]!.disposition, "held");
  assert.equal(observations[0]!.sourceUrl, originalUrl);
  assert.equal(observations[0]!.pageContext.schema, "cre-source-page-context/v1");
  assert.ok(observations[0]!.detailObservedAt);
});

test("Lyon duplicate native identity remains fatal and carries both unqualified parses", async (t) => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 100_000 });
  t.mock.method(performance, "now", () => Date.now());
  const other = "https://lyonstahl.com/properties/synthetic-conflict/";
  const accepted = original.replaceAll("https://schema.org/SoldOut", "https://schema.org/InStock");
  const oldFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = oldFetch; });
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url === "https://lyonstahl.com/sitemap.xml") return new Response('<sitemapindex><sitemap><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemap></sitemapindex>', { headers: { "content-type": "text/xml" } });
    if (url.endsWith("properties-sitemap1.xml")) return new Response(`<urlset><url><loc>${originalUrl}</loc></url><url><loc>${other}</loc></url></urlset>`, { headers: { "content-type": "text/xml" } });
    assert.ok(url === originalUrl || url === other);
    return new Response(url === other ? accepted.replaceAll(originalUrl, other) : accepted, { headers: { "content-type": "text/html" } });
  };
  await assert.rejects(() => collectWithClock(t), (error: unknown) => {
    assert.ok(error instanceof RetainedSourceError);
    assert.match(error.message, /duplicate provider ID/);
    assert.equal(error.retainedObservations.length, 2);
    assert.ok(error.retainedObservations.every((entry: any) => entry.schema === "lyon-stahl-identity-conflict-observation-v1"));
    return true;
  });
});

test("Lyon challenge shell never becomes a retained page observation", async (t) => {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 100_000 });
  t.mock.method(performance, "now", () => Date.now());
  const oldFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = oldFetch; });
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url === "https://lyonstahl.com/sitemap.xml") return new Response('<sitemapindex><sitemap><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemap></sitemapindex>', { headers: { "content-type": "text/xml" } });
    if (url.endsWith("properties-sitemap1.xml")) return new Response(`<urlset><url><loc>${originalUrl}</loc></url></urlset>`, { headers: { "content-type": "text/xml" } });
    assert.equal(url, originalUrl);
    return new Response(original.replace("<h1>11863 Nebraska Ave</h1>", "<h1>Access denied</h1>"), { headers: { "content-type": "text/html" } });
  };
  const result = await collectWithClock(t);
  assert.deepEqual(result.listings, []);
  assert.deepEqual(result.retainedObservations, []);
  assert.equal(result.truncated, true);
});
