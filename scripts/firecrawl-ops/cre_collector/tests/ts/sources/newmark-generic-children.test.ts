import test from "node:test";
import assert from "node:assert/strict";
import { parseGenericJsonLd, createGenericEnricher } from "../../../lib/enrich.js";

const item = { sourceKey: "newmark", externalId: "synthetic", url: "https://example.test/property" };
const html = '<script type="application/ld+json">{"@type":"RealEstateListing","name":"Synthetic Property","description":"Current detail"}</script>';

test("Newmark generic detail preserves existing children while retaining observed details", async () => {
  const row = parseGenericJsonLd(item, html);
  assert.equal(row.preserveChildCollections, true);
  assert.equal(row.description, "Current detail");
  const enricher = createGenericEnricher(async () => ({ rawHtml: html, markdown: "", links: [] }));
  const output = await enricher.enrich([item]);
  assert.equal(output.length, 1);
  assert.equal(output[0].preserveChildCollections, true);
});
test("generic source policy does not spread to unrelated source", () => {
  assert.equal(parseGenericJsonLd({ ...item, sourceKey: "example" }, html).preserveChildCollections, undefined);
});
test("empty generic detail is not emitted as an enrichment", async () => {
  const enricher = createGenericEnricher(async () => ({ rawHtml: "<html></html>", markdown: "", links: [] }));
  assert.deepEqual(await enricher.enrich([item]), []);
});
