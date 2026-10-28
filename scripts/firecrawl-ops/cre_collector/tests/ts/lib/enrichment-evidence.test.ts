process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { createGenericEnricher, groupEnrichItems, runEnrichGroups, type EnrichItem } from "../../../lib/enrich.js";
import { boundEnrichmentObservations, captureEnrichmentEvidence } from "../../../lib/enrichment-evidence/index.js";
import { RetainedSourceError } from "../../../lib/source-observations.js";
import type { ScrapedDoc } from "../../../types.js";

const clock = () => "2026-10-28T12:00:00.000Z";
const doc = (html: string): ScrapedDoc => ({ rawHtml: html, markdown: "Original *markdown*", links: [],
  images: [], attributes: [], metadata: { unknown: null, valid: false, empty: [] } });

test("78 generic claims retain native page observations without inventing listings", async () => {
  const items: EnrichItem[] = Array.from({ length: 78 }, (_, index) => ({ sourceKey: "generic-source",
    externalId: `listing-${index}`, url: `https://example.test/${index}` }));
  const requests: string[] = [];
  const enricher = createGenericEnricher(async (url) => {
    requests.push(url);
    return doc('<h1>Original label</h1><script type="application/json">{"unknown":false}</script><a href="/file">Brochure</a>');
  }, clock);
  const result = await runEnrichGroups(groupEnrichItems(items), () => ({ enricher, label: "generic" }), () => "Company");
  assert.equal(requests.length, 78);
  assert.deepEqual(result.listings, []);
  const observations = result.sources[0].retainedObservations;
  assert.equal(observations.length, 78);
  assert.deepEqual(observations[0].scrapedDoc.metadata, { unknown: null, valid: false, empty: [] });
  assert.equal(observations[0].scrapedDoc.markdown, "Original *markdown*");
  assert.equal(observations[0].pageContext.scripts[0].text, '{"unknown":false}');
  assert.equal(observations[0].pageContext.links[0].text, "Brochure");
  assert.equal(observations[0].capturedAt, clock());
  assert.equal(observations[0].sourceFreshness, "unverified");
});

test("same URL claims retain both transaction witnesses with one scrape per claim", async () => {
  const items: EnrichItem[] = [
    { sourceKey: "generic-source", externalId: "a", url: "https://example.test/shared", transaction: "sale" },
    { sourceKey: "generic-source", externalId: "b", url: "https://example.test/shared", transaction: "lease" },
  ];
  let count = 0;
  const enricher = createGenericEnricher(async () => {
    count++;
    return doc('<script type="application/ld+json">{"@type":"Product","name":"Native name","extra":{"unmapped":false}}</script>');
  }, clock);
  const result = await runEnrichGroups(groupEnrichItems(items), () => ({ enricher, label: "generic" }), () => "Company");
  assert.equal(count, 2);
  assert.equal(result.listings.length, 2);
  assert.deepEqual(result.sources[0].retainedObservations.map((entry: any) => entry.claim.transaction), ["sale", "lease"]);
  assert.deepEqual(result.sources[0].retainedObservations.map((entry: any) => entry.claim.externalId), ["a", "b"]);
  assert.match(result.sources[0].retainedObservations[0].pageContext.scripts[0].text, /"unmapped":false/);
});

test("failure and oversized returned fields remain explicit without source details", async () => {
  const item: EnrichItem = { sourceKey: "generic-source", externalId: "x", url: "https://example.test/x" };
  const observed = captureEnrichmentEvidence(item, doc("x".repeat(300_000)), clock());
  assert.equal((observed.omissions as Record<string, string>).rawHtml, "field_byte_limit");
  assert.equal((observed.scrapedDoc as Record<string, unknown>).markdown, "Original *markdown*");
  const enricher = createGenericEnricher(async () => { throw new Error("private failure detail"); }, clock);
  const result = await enricher.enrichWithEvidence!([item]);
  assert.deepEqual(result.listings, []);
  assert.equal((result.retainedObservations[0] as any).failure, "scrape_failed");
  assert.doesNotMatch(JSON.stringify(result.retainedObservations), /private failure detail/);
});

test("oversized aggregate refuses with a retained prefix", () => {
  const values = Array.from({ length: 10 }, (_, index) => ({ index, data: "x".repeat(1024 * 1024) }));
  assert.throws(() => boundEnrichmentObservations(values), (error: unknown) =>
    error instanceof RetainedSourceError && error.retainedObservations.length > 0 && error.retainedObservations.length < values.length);
});

test("empty JSON-LD remains evidence only; zero-valued display price is substantive", async () => {
  const item: EnrichItem = { sourceKey: "generic-source", externalId: "x", url: "https://example.test/x" };
  const empty = createGenericEnricher(async () => doc('<script type="application/ld+json">{"@type":"Product"}</script>'), clock);
  const emptyResult = await empty.enrichWithEvidence!([item]);
  assert.deepEqual(emptyResult.listings, []);
  assert.equal(emptyResult.retainedObservations.length, 1);
  const zero = createGenericEnricher(async () => doc('<script type="application/ld+json">{"@type":"Offer","price":0}</script>'), clock);
  const zeroResult = await zero.enrichWithEvidence!([item]);
  assert.equal(zeroResult.listings.length, 1);
  assert.equal(zeroResult.listings[0].salePriceText, "0");
});

test("non-JSON native fields are omitted explicitly instead of altered", () => {
  const item: EnrichItem = { sourceKey: "generic-source", externalId: "x", url: "https://example.test/x" };
  for (const value of [NaN, Infinity, -Infinity, undefined, () => 1, Symbol("x"), 1n, -0]) {
    const observed = captureEnrichmentEvidence(item, { ...doc(""), metadata: { nested: value } }, clock());
    assert.equal((observed.omissions as Record<string, string>).metadata, "non_json_value");
    assert.equal(Object.prototype.hasOwnProperty.call(observed.scrapedDoc, "metadata"), false);
  }
  const observed = captureEnrichmentEvidence(item, { ...doc(""), metadata: { nested: [null, false, 0, []] } }, clock());
  assert.deepEqual((observed.scrapedDoc as Record<string, any>).metadata, { nested: [null, false, 0, []] });
  assert.equal(observed.pageContextClockOrigin, "collector_after_scrape_return_not_provider_freshness");
});
