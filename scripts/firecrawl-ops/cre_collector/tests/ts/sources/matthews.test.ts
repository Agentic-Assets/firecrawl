import assert from "node:assert/strict";
import test from "node:test";
import {
  matthewsFetchOptions,
  matthewsParsedCoverage,
  parseMatthewsDetail,
  matthewsTenureFromUrl,
  srcMatthews,
} from "../../../sources/matthews.js";

test("matthewsTenureFromUrl classifies leasing slugs as lease", () => {
  assert.equal(matthewsTenureFromUrl("https://www.matthews.com/properties/leasing-abc"), "lease");
  assert.equal(matthewsTenureFromUrl("https://www.matthews.com/properties/panera-bread"), "sale");
});

test("parseMatthewsDetail extracts core fields from server-rendered HTML", () => {
  const html = `
    <html>
      <head><meta property="og:image" content="https://cms.matthews.com/wp-content/uploads/photo.jpg"></head>
      <body>
        <h1 id="propertyTitle">Panera Bread</h1>
        <div id="propertyAddress">123 Main St, Tulsa, OK 74103</div>
        <div id="propertyPrice">$3,000,000</div>
        <div class="key-info-title">Cap Rate</div><div class="key-info-value">6.40%</div>
        <div class="key-info-title">Property Type</div><div class="key-info-value">Retail</div>
        <a id="agentName" href="/agents/jane">Jane Broker</a>
      </body>
    </html>`;
  const row = parseMatthewsDetail(html, "https://www.matthews.com/properties/panera-bread", "sale");
  assert.equal(row?.id, "panera-bread");
  assert.equal(row?.name, "Panera Bread");
  assert.equal(row?.transactionType, "Sale");
  assert.equal(row?.salePriceUsd, 3000000);
  assert.equal(row?.capRatePct, 6.4);
  assert.equal(row?.assetType, "Retail");
  assert.equal(row?.state, "OK");
});

test("strict Matthews parsing validates canonical provider identity and emits generation provenance", () => {
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "matthews-strict-test";
    const html = `
      <html>
        <head>
          <link rel="canonical" href="https://www.matthews.com/properties/panera-bread">
        </head>
        <body>
          <h1 id="propertyTitle">Panera Bread</h1>
          <div id="propertyAddress">123 Main St, Tulsa, OK 74103</div>
        </body>
      </html>`;
    const row = parseMatthewsDetail(
      html,
      "https://www.matthews.com/properties/panera-bread",
      "sale",
      {
        inventoryObservedAt: "2026-07-29T12:00:00.000Z",
        detailObservedAt: "2026-07-29T12:01:00.000Z",
      }
    );
    assert.equal(row?.id, "panera-bread");
    assert.equal(row?.inventoryObservedAt, "2026-07-29T12:00:00.000Z");
    assert.equal(row?.detailObservedAt, "2026-07-29T12:01:00.000Z");
    assert.equal(row?.freshnessProvenance.generationId, "matthews-strict-test");
    assert.equal(row?.freshnessProvenance.identityMethod, "canonical_property_url");

    const mismatch = parseMatthewsDetail(
      html.replace("/panera-bread", "/different-property"),
      "https://www.matthews.com/properties/panera-bread",
      "sale",
      {
        inventoryObservedAt: "2026-07-29T12:00:00.000Z",
        detailObservedAt: "2026-07-29T12:01:00.000Z",
      }
    );
    assert.equal(mismatch, null);

    const missingCanonical = parseMatthewsDetail(
      html.replace(
        '<link rel="canonical" href="https://www.matthews.com/properties/panera-bread">',
        ""
      ),
      "https://www.matthews.com/properties/panera-bread",
      "sale",
      {
        inventoryObservedAt: "2026-07-29T12:00:00.000Z",
        detailObservedAt: "2026-07-29T12:01:00.000Z",
      }
    );
    assert.equal(missingCanonical, null);
  } finally {
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
  }
});

test("strict Matthews parsing rejects soft error shells with matching canonical identity", () => {
  const url = "https://www.matthews.com/properties/example-sale";
  const shells = [
    `
      <html>
        <head><link rel="canonical" href="${url}"></head>
        <body>
          <h1 id="propertyTitle">Page Not Found</h1>
          <div id="propertyAddress">The requested property was not found.</div>
        </body>
      </html>`,
    `
      <html>
        <head><link rel="canonical" href="${url}"></head>
        <body>
          <h1 id="propertyTitle">An Error Occurred</h1>
          <div id="propertyPrice">Please try again later.</div>
        </body>
      </html>`,
    `
      <html>
        <head><link rel="canonical" href="${url}"></head>
        <body>
          <h1 id="propertyTitle">Just a moment...</h1>
          <div id="propertyAddress">Verify you are human to continue.</div>
        </body>
      </html>`,
  ];

  for (const html of shells) {
    assert.equal(
      parseMatthewsDetail(html, url, "sale", {
        strict: true,
        inventoryObservedAt: "2026-07-29T12:00:00.000Z",
        detailObservedAt: "2026-07-29T12:01:00.000Z",
      }),
      null
    );
  }
});

test("strict Matthews parsing requires provider-specific property structure", () => {
  const url = "https://www.matthews.com/properties/example-sale";
  const genericPage = `
    <html>
      <head>
        <link rel="canonical" href="${url}">
        <meta property="og:image"
          content="https://cms.matthews.com/wp-content/uploads/example.jpg">
      </head>
      <body><h1>Example Sale</h1></body>
    </html>`;

  assert.equal(
    parseMatthewsDetail(genericPage, url, "sale", { strict: true }),
    null
  );
  assert.equal(
    parseMatthewsDetail(genericPage, url, "sale", { strict: false })?.name,
    "Example Sale"
  );
});

test("strict Matthews error-shell guard does not reject a property named for street number 404", () => {
  const url = "https://www.matthews.com/properties/404-main-street";
  const html = `
    <html>
      <head><link rel="canonical" href="${url}"></head>
      <body>
        <h1 id="propertyTitle">404 Main Street</h1>
        <div id="propertyAddress">404 Main Street, Tulsa, OK 74103</div>
      </body>
    </html>`;

  assert.equal(
    parseMatthewsDetail(html, url, "sale", { strict: true })?.name,
    "404 Main Street"
  );
});

test("Matthews coverage counts parse-null identity failures as truncation", () => {
  const coverage = matthewsParsedCoverage([{ id: "accepted" }, null, undefined]);
  assert.deepEqual(coverage.listings, [{ id: "accepted" }]);
  assert.equal(coverage.failures, 2);
  assert.equal(coverage.truncated, true);
});

test("strict Matthews direct fetches explicitly bypass caches", () => {
  const strict = matthewsFetchOptions(true);
  assert.equal(strict.cache, "no-store");
  assert.equal((strict.headers as Record<string, string>)["Cache-Control"], "no-cache");
  const compatible = matthewsFetchOptions(false);
  assert.equal(compatible.cache, undefined);
});

test("Matthews monitor reports finite sitemap caps as truncation", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      `<?xml version="1.0"?>
      <urlset>
        <url><loc>https://www.matthews.com/properties/alpha-sale</loc></url>
        <url><loc>https://www.matthews.com/properties/bravo-sale</loc></url>
      </urlset>`,
      { status: 200 }
    );
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const result = await srcMatthews("sale", 1, true);
  assert.equal(result.totalAvailable, 2);
  assert.equal(result.listings.length, 1);
  assert.equal(result.truncated, true);
  assert.match(result.note ?? "", /Selected 1\/2/);
});
