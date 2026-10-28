process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { captureDetailTables } from "../../../lib/detail-tables/index.js";
import { firecrawl } from "../../../lib/scrape.js";
import { groupEnrichItems, runEnrichGroups } from "../../../lib/enrich.js";
import { enrichBuildoutDetail } from "../../../sources/buildout.js";
import { REGISTERED_BUILDOUT_FIRMS } from "../../../sources/buildout-registry.js";

test("Buildout enrichment captures native tables without promoting financial fields", async () => {
  const original = firecrawl.scrape;
  const config = REGISTERED_BUILDOUT_FIRMS.find(firm => firm.sourceKey === "faris-lee")!.detailConfig!;
  const url = "https://farislee.com/listings/?propertyId=1135400-sale";
  const html = '<table><tr><th>NOI</th><td>  $100,000\nUSD  </td></tr><tr><th>Year Built</th><td>1990</td></tr></table>';
  let requested = "";
  (firecrawl as any).scrape = async (target: string) => {
    requested = target;
    return { rawHtml: html, markdown: "Detail", links: [] };
  };
  try {
    const row = await enrichBuildoutDetail("faris-lee", url, config);
    assert(requested.includes("/inventory/1135400-sale?"));
    assert.equal(row.id, "1135400");
    assert.equal(row.url, url);
    assert.equal(row.noi, undefined);
    assert.equal(row.yearBuilt, undefined);
    assert.equal(row.extraFacts.detail_tables.status, "observed");
    assert.equal(row.extraFacts.detail_tables.tables[0].rows[0].cells[1].rawText, "  $100,000\nUSD  ");
    assert.equal(row.extraFacts.detail_tables.provenance.sourceUrl, requested);
    assert.equal(row.extraFacts.detail_tables_observation_origin, "collector_result_read_time");
    assert.equal(row.extraFacts.detail_tables_attempt, undefined);
  } finally { (firecrawl as any).scrape = original; }
});

test("missing HTML becomes a separate inspectable attempt instead of a complete snapshot", async () => {
  const original = firecrawl.scrape;
  (firecrawl as any).scrape = async () => ({ rawHtml: "", markdown: "Useful description", links: [] });
  try {
    const row = await enrichBuildoutDetail("svn", "https://svn.com/properties/?propertyId=1-sale");
    assert(row);
    assert.equal(row.extraFacts.detail_tables, undefined);
    assert.equal(row.extraFacts.detail_tables_attempt.status, "failed");
    assert.equal(row.extraFacts.detail_tables_attempt.omissions.unknown, true);
  } finally { (firecrawl as any).scrape = original; }
});

test("final shared enrichment artifact preserves exact null, false, empty and positional table values", async () => {
  const url = "https://svn.com/properties/?propertyId=1-sale";
  const snapshot = captureDetailTables('<table><tr><td></td><td>0</td></tr></table>', {
    sourceUrl: "https://example.org/detail", observedAt: "2026-10-28T12:00:00Z",
  });
  const groups = groupEnrichItems([{ sourceKey: "svn", externalId: "1", url }]);
  const result = await runEnrichGroups(groups, () => ({ label: "test", enricher: {
    async enrich() { return [{ id: "1", url, extraFacts: { detail_tables: snapshot } }]; },
  } }), () => "SVN");
  assert.equal(result.listings.length, 1);
  assert.deepEqual(result.listings[0].extraFacts.detail_tables, snapshot);
  assert.equal(result.listings[0].extraFacts.detail_tables.tables[0].rows[0].cells[0].text, "");
  assert.equal(result.listings[0].extraFacts.detail_tables.tables[0].visibility.hidden, false);
  assert.equal(result.listings[0].extraFacts.detail_tables.tables[0].rows[0].cells[0].colspan, null);
});

test("invalid reserved snapshots are not emitted as pruned but apparently valid evidence", async () => {
  const url = "https://svn.com/properties/?propertyId=1-sale";
  const groups = groupEnrichItems([{ sourceKey: "svn", externalId: "1", url }]);
  const result = await runEnrichGroups(groups, () => ({ label: "test", enricher: {
    async enrich() { return [{ id: "1", url, extraFacts: {
      detail_tables: { schema: "foreign", status: "observed", flag: false },
      detail_tables_attempt: { schema: "foreign", status: "failed" },
      other_fact: "retained",
    } }]; },
  } }), () => "SVN");
  assert.equal(result.listings[0].extraFacts.detail_tables, undefined);
  assert.equal(result.listings[0].extraFacts.detail_tables_attempt, undefined);
  assert.equal(result.listings[0].extraFacts.other_fact, "retained");
});
