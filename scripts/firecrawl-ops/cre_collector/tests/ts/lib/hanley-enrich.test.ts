process.argv = [process.argv[0]!, process.argv[1]!];

import assert from "node:assert/strict";
import test from "node:test";
import { enrichHanleyListings } from "../../../sources/hanley.js";
import { resolveEnricher, runEnrichGroups } from "../../../lib/enrich.js";
import { firecrawl } from "../../../lib/scrape.js";

const sale = { id: "H-1", name: "Synthetic sale", visibility: "Public", dealRecordType: "Seller_Rep" };
const lease = { id: "H-2", name: "Synthetic lease", visibility: "Public", dealRecordType: "Landlord_Rep" };
const url = (id: string) => `https://hanleyinvestmentgroup.com/listings/?id=${id}`;
const claim = (id: string) => ({ sourceKey: "hanley", externalId: id, url: url(id) });

function html(rows: any[]) {
  return `<script>var rethink_properties = ${JSON.stringify(rows)};</script>` +
    `<div id="rethink-properties-container">` + rows.filter((row) => row.visibility === "Public")
      .map((row) => `<article data-id="${row.id}" data-result="true"></article>`).join("") + `</div>`;
}

async function withInventory(rows: any[], run: (calls: () => number) => Promise<void>) {
  const originalFetch = globalThis.fetch;
  const originalScrape = firecrawl.scrape;
  const generation = process.env.CRE_REFRESH_GENERATION;
  delete process.env.CRE_REFRESH_GENERATION;
  let calls = 0;
  globalThis.fetch = async (input) => {
    assert.equal(String(input), "https://hanleyinvestmentgroup.com/listings/");
    calls++;
    return new Response(html(rows), { status: 200 });
  };
  // Existing direct→rendered transport fallback remains source-backed. No API call.
  firecrawl.scrape = (async () => ({ rawHtml: html(rows) })) as typeof firecrawl.scrape;
  try { await run(() => calls); }
  finally {
    globalThis.fetch = originalFetch;
    firecrawl.scrape = originalScrape;
    if (generation === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = generation;
  }
}

test("Hanley dispatches through one complete public feed and keeps source tenure/identity", async () => {
  await withInventory([sale, lease, { ...sale, id: "private", visibility: "Private" }], async (calls) => {
    assert.equal(resolveEnricher("hanley").label, "bespoke");
    const result = await runEnrichGroups(new Map([["hanley", [claim("H-1"), claim("H-2"), claim("private"), claim("missing"), { ...claim("H-1"), url: "https://wrong.example/" }]]]), resolveEnricher, () => "Hanley Investment Group");
    assert.equal(calls(), 1);
    assert.deepEqual(result.listings.map((row) => [row.id, row.url, row.transactionType]), [["H-1", url("H-1"), "Sale"], ["H-2", url("H-2"), "Lease"]]);
    for (const row of result.listings) {
      assert.equal(row.preserveChildCollections, true);
      assert.equal(row.freshnessProvenance.detailScope, "authoritative_inventory_feed");
      assert.ok(row.inventoryObservedAt);
      assert.equal(row.detailObservedAt, undefined);
    }
  });
});

for (const [name, rows] of [
  ["duplicate public identities", [sale, sale]],
  ["public/private identity contradiction", [sale, { ...sale, visibility: "Private" }]],
  ["missing provider identity", [{ ...sale, id: "" }]],
  ["missing visibility", [{ ...sale, visibility: "" }]],
] as const) {
  test(`Hanley refuses ${name} without generic fallback`, async () => {
    await withInventory([...rows], async () => {
      await assert.rejects(enrichHanleyListings([claim("H-1")]));
      const result = await runEnrichGroups(new Map([["hanley", [claim("H-1")]]]), resolveEnricher, () => "Hanley Investment Group");
      assert.deepEqual(result.listings, []);
      assert.equal(result.sources[0].enricher, "bespoke");
      assert.ok(result.sources[0].error);
    });
  });
}
