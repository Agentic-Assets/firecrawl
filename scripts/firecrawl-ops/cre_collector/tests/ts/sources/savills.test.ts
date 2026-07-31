import test from "node:test";
import assert from "node:assert/strict";
import {
  inferStateFromZip,
  parseSavillsUsLocation,
  savillsSqft,
  savillsImageUrls,
  savillsDocumentUrls,
  savillsListTimeoutMs,
  savillsDirectListTimeoutMs,
  savillsListHtmlIsUsable,
  savillsClearlyNonUsLocation,
  srcSavills,
} from "../../../sources/savills.js";

test("savillsListTimeoutMs is bounded with a short recovery default", () => {
  assert.equal(savillsListTimeoutMs(undefined), 30000);
  assert.equal(savillsListTimeoutMs("5000"), 10000);
  assert.equal(savillsListTimeoutMs("120000"), 90000);
  assert.equal(savillsListTimeoutMs("invalid"), 30000);
});

test("savillsDirectListTimeoutMs keeps the direct enumeration transport bounded", () => {
  assert.equal(savillsDirectListTimeoutMs(undefined), 25000);
  assert.equal(savillsDirectListTimeoutMs("1000"), 5000);
  assert.equal(savillsDirectListTimeoutMs("120000"), 60000);
  assert.equal(savillsDirectListTimeoutMs("invalid"), 25000);
});

test("savillsListHtmlIsUsable accepts only a rendered Savills list state", () => {
  const valid = [
    '<script id="__NEXT_DATA__" type="application/json">',
    JSON.stringify({
      props: {
        initialReduxState: {
          listPage: { totalItems: 3 },
          properties: { a: { ExternalPropertyID: "a" } },
        },
      },
    }),
    "</script>",
  ].join("");
  assert.equal(savillsListHtmlIsUsable(valid), true);
  assert.equal(savillsListHtmlIsUsable("<html>challenge page</html>"), false);
  assert.equal(
    savillsListHtmlIsUsable('<script id="__NEXT_DATA__" type="application/json">{"props":{}}</script>'),
    false
  );
});

function savillsPageHtml(totalItems: number, withPaging: boolean): string {
  const property = {
    ExternalPropertyID: "US-1",
    ExternalPropertyIDFormatted: "us-1",
    IsCommercial: true,
    AddressLine1: "100 Main Street",
    AddressLine2: "Dallas, TX 75201",
    PropertyTypes: [{ Caption: "Office" }],
  };
  return [
    '<script id="__NEXT_DATA__" type="application/json">',
    JSON.stringify({
      props: {
        initialReduxState: {
          listPage: withPaging
            ? {
                currentPage: 1,
                pageMap: {
                  "1": {
                    paging: { total: 1, totalItems },
                    metaData: { NextUrl: null },
                  },
                },
              }
            : {},
          properties: { "US-1": property },
        },
      },
    }),
    "</script>",
  ].join("");
}

test("Savills full refresh rejects usable HTML with missing pagination metadata", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(savillsPageHtml(1, false), { status: 200 });
  try {
    await assert.rejects(
      () => srcSavills("sale", Infinity, false),
      /did not expose complete pagination metadata/
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Savills full refresh reconciles unique rows to the provider total", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(savillsPageHtml(2, true), { status: 200 });
  try {
    await assert.rejects(
      () => srcSavills("sale", Infinity, false),
      /enumerated 1 unique commercial rows but provider reported 2/
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Savills accepts a reconciled one-page provider shape whose paging total is zero", async () => {
  const html = savillsPageHtml(1, true).replace(
    '"paging":{"total":1',
    '"paging":{"total":0'
  );
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(html, { status: 200 });
  try {
    const result = await srcSavills("sale", Infinity, false);
    assert.equal(result.listings.length, 1);
    assert.equal(result.totalAvailable, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Savills follows a complete NextUrl chain when the provider omits page count", async () => {
  const first = savillsPageHtml(2, true)
    .replace('"paging":{"total":1,"totalItems":2}', '"paging":{"total":0,"totalItems":2}')
    .replace('"NextUrl":null', '"NextUrl":"/com/en/list/commercial/property-for-sale/united-states-of-america/page-2"');
  const second = savillsPageHtml(2, true)
    .replaceAll("US-1", "US-2")
    .replaceAll("us-1", "us-2")
    .replace('"paging":{"total":1,"totalItems":2}', '"paging":{"total":0,"totalItems":2}')
    .replace('"currentPage":1', '"currentPage":2');
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) =>
    new Response(String(input).endsWith("/page-2") ? second : first, { status: 200 });
  try {
    const result = await srcSavills("sale", Infinity, false);
    assert.equal(result.listings.length, 2);
    assert.equal(result.totalAvailable, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Savills rejects an address without U.S. or explicit non-U.S. evidence", async () => {
  const property = {
    ExternalPropertyID: "UNKNOWN-1",
    ExternalPropertyIDFormatted: "unknown-1",
    IsCommercial: true,
    AddressLine1: "Unknown Building",
    AddressLine2: "Unknown place",
    PropertyTypes: [{ Caption: "Office" }],
  };
  const html = [
    '<script id="__NEXT_DATA__" type="application/json">',
    JSON.stringify({
      props: {
        initialReduxState: {
          listPage: {
            currentPage: 1,
            pageMap: {
              "1": {
                paging: { total: 0, totalItems: 1 },
                metaData: { NextUrl: null },
              },
            },
          },
          properties: { "UNKNOWN-1": property },
        },
      },
    }),
    "</script>",
  ].join("");
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(html, { status: 200 });
  try {
    await assert.rejects(
      () => srcSavills("lease", Infinity, false),
      /could not classify 1 provider row/
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("Savills rejects an anomalous nominal U.S. response containing only Canadian rows", async () => {
  const property = {
    ExternalPropertyID: "CA-1",
    ExternalPropertyIDFormatted: "ca-1",
    IsCommercial: true,
    AddressLine1: "Sas Building",
    AddressLine2: "280 King St E, Toronto On M5A 1K7",
    PropertyTypes: [{ Caption: "Office" }],
  };
  const html = [
    '<script id="__NEXT_DATA__" type="application/json">',
    JSON.stringify({
      props: {
        initialReduxState: {
          listPage: {
            currentPage: 1,
            pageMap: {
              "1": {
                paging: { total: 0, totalItems: 1 },
                metaData: { NextUrl: "" },
              },
            },
          },
          properties: { "CA-1": property },
        },
      },
    }),
    "</script>",
  ].join("");
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(html, { status: 200 });
  try {
    await assert.rejects(
      () => srcSavills("sale", Infinity, false),
      /nominal U\.S\. endpoint returned only 1 explicitly non-U\.S\. row/
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("inferStateFromZip maps ZIP prefixes to states", () => {
  assert.equal(inferStateFromZip("75201"), "TX");
  assert.equal(inferStateFromZip("10001"), "NY");
  assert.equal(inferStateFromZip("90210"), "CA");
  assert.equal(inferStateFromZip("not-a-zip"), null);
  assert.equal(inferStateFromZip(null), null);
});

test("parseSavillsUsLocation parses city, state, and postal code", () => {
  assert.deepEqual(parseSavillsUsLocation("123 Main St, Dallas, TX 75201"), {
    city: "Dallas",
    state: "TX",
    postalCode: "75201",
  });
  assert.deepEqual(parseSavillsUsLocation("Seattle, Washington 98101"), {
    city: "Seattle",
    state: "WA",
    postalCode: "98101",
  });
  assert.deepEqual(parseSavillsUsLocation("Phillipsburg Nj"), {
    city: "Phillipsburg",
    state: "NJ",
    postalCode: null,
  });
  assert.deepEqual(parseSavillsUsLocation("La Jolla CA"), {
    city: "La Jolla",
    state: "CA",
    postalCode: null,
  });
  assert.deepEqual(parseSavillsUsLocation("Washington Square, New York"), {
    city: "Washington Square",
    state: "NY",
    postalCode: null,
  });
  assert.equal(parseSavillsUsLocation("Toronto On M5A 1K7"), null);
  assert.equal(parseSavillsUsLocation(""), null);
});

test("savillsClearlyNonUsLocation recognizes Canadian evidence only", () => {
  assert.equal(
    savillsClearlyNonUsLocation("280 King St E, Toronto On M5A 1K7"),
    true
  );
  assert.equal(savillsClearlyNonUsLocation("Phillipsburg Nj"), false);
  assert.equal(savillsClearlyNonUsLocation("Chicago IL"), false);
  assert.equal(savillsClearlyNonUsLocation("On Main Street"), false);
  assert.equal(savillsClearlyNonUsLocation("Unknown place"), false);
});

test("savillsSqft extracts square footage from text", () => {
  assert.equal(savillsSqft("Office (12,500 sq ft)"), 12500);
  assert.equal(savillsSqft("15,000 sqft warehouse"), 15000);
  assert.equal(savillsSqft("price on application"), null);
});

test("savillsImageUrls dedupes gallery image sizes", () => {
  const row = {
    ImagesGallery: [
      { ImageUrl_L: "https://cdn.savills.com/a-large.jpg", ImageUrl_S: "https://cdn.savills.com/a-small.jpg" },
    ],
    PropertyCardImagesGallery: [{ ImageUrl: "https://cdn.savills.com/b.jpg" }],
  };
  const urls = savillsImageUrls(row);
  assert.equal(urls.length, 3);
  assert.ok(urls.every((u) => u.startsWith("https://")));
});

test("savillsDocumentUrls classifies the floor-plan PDF distinctly from brochures", () => {
  const docs = savillsDocumentUrls({
    FloorplanPDFUrl: "https://cdn.savills.com/floorplan.pdf",
    BrochureGallery: [
      { Caption: "Marketing Brochure", ImageUrl: "https://cdn.savills.com/brochure.pdf" },
    ],
  });
  const floorplan = docs.find((d) => d.url.endsWith("floorplan.pdf"));
  const brochure = docs.find((d) => d.url.endsWith("brochure.pdf"));
  assert.equal(floorplan?.docType, "floor_plan");
  assert.equal(brochure?.docType, "brochure");
  // Non-PDF and missing urls are excluded.
  assert.equal(savillsDocumentUrls({ FloorplanPDFUrl: null, BrochureGallery: [] }).length, 0);
});
