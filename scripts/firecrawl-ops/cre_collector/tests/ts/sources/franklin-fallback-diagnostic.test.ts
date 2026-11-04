import test from "node:test";
import assert from "node:assert/strict";
import { firecrawl, scrapeJson } from "../../../lib/scrape.js";
import { fetchBuildoutInventoryPage } from "../../../sources/buildout.js";
import { franklinStreetBuildoutConfig } from "../../../sources/franklin-street.js";
import { allBuildoutFeeds } from "../../../sources/buildout-feeds/index.js";

test("non-JSON observations are bounded and do not change retry or success", async (t) => {
  const values = [500, "403", 99, 600, null];
  let calls = 0;
  const observed: Array<[number, number | null]> = [];
  t.mock.method(console, "error", () => undefined);
  t.mock.method(firecrawl, "scrape", async () => ({ rawHtml: "secret body", metadata: { statusCode: values[calls++], url: "secret url" } }));
  await assert.rejects(scrapeJson("https://example.invalid/secret", {
    jsonAttempts: values.length, jsonBackoffMs: 0,
    onNonJson: (attempt, status) => { observed.push([attempt, status]); if (attempt === 2) throw new Error("ignored"); },
  }), /contained no parseable JSON object/);
  assert.equal(calls, 5);
  assert.deepEqual(observed, [[1, 500], [2, null], [3, null], [4, null], [5, null]]);
});

test("successful JSON does not report non-JSON", async (t) => {
  t.mock.method(firecrawl, "scrape", async () => ({ rawHtml: "{\"ok\":true}", metadata: { statusCode: 200 } }));
  let reports = 0;
  assert.deepEqual(await scrapeJson("https://example.invalid", { onNonJson: () => { reports++; } }), { ok: true });
  assert.equal(reports, 0);
});

test("Franklin direct failure reports only sanitized fallback statuses and keeps request count", async (t) => {
  let direct = 0, fallback = 0;
  const logs: string[] = [];
  t.mock.method(console, "error", (...args: unknown[]) => { logs.push(args.join(" ")); });
  t.mock.method(globalThis, "fetch", async () => { direct++; return new Response("private body", { status: 500 }); });
  t.mock.method(firecrawl, "scrape", async () => ({ rawHtml: "private body", metadata: { statusCode: ++fallback === 1 ? 403 : "private value", url: "private url" } }));
  await assert.rejects(fetchBuildoutInventoryPage("Franklin Street", franklinStreetBuildoutConfig("sale").pluginKey, 0, {
    preferDirectJson: true, cacheSlug: "franklin-street-sale", jsonAttempts: 2, jsonBackoffMs: 0,
  }), /contained no parseable JSON object/);
  assert.equal(direct, 1); assert.equal(fallback, 2);
  const reports = logs.filter(line => line.includes("Firecrawl non-JSON"));
  assert.deepEqual(reports, [
    "  franklin-street-sale: Firecrawl non-JSON page 0 attempt 1 status 403",
    "  franklin-street-sale: Firecrawl non-JSON page 0 attempt 2 status unknown",
  ]);
  assert.ok(reports.every(line => !line.includes("private") && !line.includes("https:")));
});


test("other registered feeds and unknown plugins use bounded registry context", async (t) => {
  const logs: string[] = [];
  t.mock.method(console, "error", (...args: unknown[]) => { logs.push(args.join(" ")); });
  t.mock.method(firecrawl, "scrape", async () => ({ rawHtml: "not JSON", metadata: {} }));
  const feed = allBuildoutFeeds.find((item) => item.sourceKey === "svn")!;
  for (const plugin of [feed.pluginKey, "private-plugin-value"]) {
    await assert.rejects(fetchBuildoutInventoryPage("private company", plugin, 0, { jsonAttempts: 1 }));
  }
  assert.deepEqual(logs.filter(line => line.includes("Firecrawl non-JSON")), [
    "  svn: Firecrawl non-JSON page 0 attempt 1 status unknown",
    "  unknown: Firecrawl non-JSON page 0 attempt 1 status unknown",
  ]);
});
