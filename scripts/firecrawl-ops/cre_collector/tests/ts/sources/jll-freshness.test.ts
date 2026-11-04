// AGENTIC-4175: real enrichment and freshness aggregation, synthetic documents only.
process.argv = [process.argv[0]!, process.argv[1]!];

import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { enrichJllListing, writeJllDetailCache } from "../../../sources/jll.js";
import { summarizeListingFreshness } from "../../../lib/freshness.js";
import { firecrawl } from "../../../lib/scrape.js";

const boundary = "2020-01-01T00:00:00.000Z";
const observedAt = "2020-01-01T00:00:01.123+00:00";

function sourceFixture(t: TestContext) {
  const directory = mkdtempSync(join(tmpdir(), "jll-freshness-"));
  const values: Record<string, string> = {
    JLL_DETAIL_CACHE_DIR: directory,
    JLL_DETAIL_CACHE_MIN_CACHED_AT: boundary,
    CRE_REFRESH_GENERATION: "jll-freshness-fixture",
    CRE_REFRESH_STARTED_AT: boundary,
    CRE_REQUIRE_FRESH_DETAILS: "1",
  };
  const previous = Object.keys(values).map((key) => [key, process.env[key]] as const);
  Object.assign(process.env, values);
  t.after(() => {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    rmSync(directory, { recursive: true, force: true });
  });
  t.mock.method(globalThis, "fetch", async () => {
    throw new Error("freshness test attempted network dispatch");
  });
  return async (
    transactionType: "Sale" | "Lease",
    inventoryObservedAt: unknown = observedAt,
    hiddenAt: "list" | "detail" | "none" = "detail",
    detail: "present" | "missing" | "mismatch" = "present",
  ) => {
    const url = "https://property.jll.com/listings/freshness-fixture";
    const property = {
      id: detail === "mismatch" ? "wrong-identity" : "freshness-fixture",
      pageUrl: "/listings/freshness-fixture",
      hidePrice: hiddenAt === "detail",
      title: "Synthetic property",
      description: "Asking price: $3,250,000",
      salePrice: { amount: 3250000, currency: "USD", unit: null },
      rentPrice: { amount: 32.5, currency: "USD", unit: "feet" },
    };
    const document = {
      rawHtml: `<script id="__NEXT_DATA__">${JSON.stringify({
        props: { pageProps: { property: detail === "missing" ? null : property, brokers: [] } },
      })}</script>`,
      markdown: "Asking price: $3,250,000",
      links: [],
      images: [],
    };
    // A missing cached property also exercises the existing rendered fallback;
    // the stub returns the same synthetic document without any API dispatch.
    t.mock.method(firecrawl, "scrape", async () => document);
    writeJllDetailCache(url, document);
    return enrichJllListing({
      id: "freshness-fixture",
      url,
      transactionType,
      inventoryObservedAt,
      salePriceUsd: 3250000,
      salePriceText: "$3,250,000",
      leaseRateText: "$32.5/feet",
      financials: { amount: 3250000 },
      markdown: "Asking price: $3,250,000",
      jllSearchResult: { hidePrice: hiddenAt === "list" },
    });
  };
}

for (const transaction of ["Sale", "Lease"] as const) {
  for (const hiddenAt of ["list", "detail"] as const) {
    test(`JLL ${transaction} preserves original inventory freshness when price is hidden at ${hiddenAt}`, async (t) => {
      const row = await sourceFixture(t)(transaction, observedAt, hiddenAt);
      assert.equal(row.inventoryObservedAt, observedAt);
      assert.equal(row.detailError, undefined);
      assert.deepEqual(summarizeListingFreshness([row]), {
        listings: 1,
        inventoryObserved: 1,
        detailObserved: 1,
        authoritativeInventoryFeed: 0,
        detailErrors: 0,
        childPreservationRows: 0,
        staleInventoryObservations: 0,
        staleDetailObservations: 0,
      });
      assert.equal(row.jllDetail.pricing.visibility, "withheld");
      for (const key of ["salePriceUsd", "salePriceText", "leaseRateText", "financials", "markdown", "description"]) {
        assert.equal(row[key], undefined);
      }
      assert.doesNotMatch(JSON.stringify(row), /3250000|3,250,000|32\.5/);
    });
  }

  test(`JLL ${transaction} visible pricing retains inventory freshness and economics`, async (t) => {
    const row = await sourceFixture(t)(transaction, observedAt, "none");
    assert.equal(row.inventoryObservedAt, observedAt);
    assert.equal(row.salePriceUsd, 3250000);
    assert.equal(row.jllDetail.pricing.visibility, "visible");
    assert.equal(summarizeListingFreshness([row]).staleInventoryObservations, 0);
    assert.equal(row.detailError, undefined);
  });

  for (const [label, value] of [
    ["missing", null],
    ["invalid", "not-a-timestamp"],
    ["stale", "2019-12-31T23:59:59.000Z"],
  ] as const) {
    test(`JLL ${transaction} hidden projection does not repair ${label} inventory evidence`, async (t) => {
      const row = await sourceFixture(t)(transaction, value);
      assert.equal(row.inventoryObservedAt, value === null ? undefined : value);
      assert.equal(row.detailError, undefined);
      assert.equal(summarizeListingFreshness([row]).staleInventoryObservations, 1);
    });
  }

  for (const failure of ["missing", "mismatch"] as const) {
    test(`JLL ${transaction} keeps detail failure evidence and original inventory observation (${failure})`, async (t) => {
      const row = await sourceFixture(t)(transaction, observedAt, "list", failure);
      assert.equal(row.inventoryObservedAt, observedAt);
      assert.match(row.detailError, failure === "missing" ? /missing property/ : /provider id mismatch/);
      const summary = summarizeListingFreshness([row]);
      assert.equal(summary.detailErrors, 1);
      assert.equal(summary.staleDetailObservations, 1);
      assert.equal(summary.staleInventoryObservations, 0);
      assert.doesNotMatch(JSON.stringify(row), /3250000|3,250,000|32\.5/);
    });
  }
}
