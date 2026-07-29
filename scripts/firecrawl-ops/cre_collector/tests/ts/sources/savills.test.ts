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

test("inferStateFromZip maps ZIP prefixes to states", () => {
  assert.equal(inferStateFromZip("75201"), "TX");
  assert.equal(inferStateFromZip("10001"), "NY");
  assert.equal(inferStateFromZip("90210"), "CA");
  assert.equal(inferStateFromZip("not-a-zip"), null);
  assert.equal(inferStateFromZip(null), null);
});

test("parseSavillsUsLocation parses city, state, and postal code", () => {
  assert.deepEqual(parseSavillsUsLocation("123 Main St, Dallas, TX 75201"), {
    city: "123 Main St",
    state: "TX",
    postalCode: "75201",
  });
  assert.deepEqual(parseSavillsUsLocation("Seattle, Washington 98101"), {
    city: "Seattle",
    state: "WA",
    postalCode: "98101",
  });
  assert.equal(parseSavillsUsLocation(""), null);
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
