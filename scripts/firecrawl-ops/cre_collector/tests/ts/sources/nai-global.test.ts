// Isolate argv before nai-global.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import {
  naiLocation,
  naiImageUrls,
  naiDocumentUrls,
  naiPriceText,
  naiSizeText,
  naiListingStatus,
  naiListingFromFeed,
  NAI_LISTING_URL_BASE,
} from "../../../sources/nai-global.js";

test("naiLocation parses Infabode location path", () => {
  assert.deepEqual(naiLocation([{ path: "Dallas, TX, United States" }]), {
    city: "Dallas",
    state: "TX",
    country: "United States",
  });
  assert.deepEqual(naiLocation([{ name: "Austin, TX, US" }]), {
    city: "Austin",
    state: "TX",
    country: "US",
  });
  assert.deepEqual(naiLocation([]), { city: null, state: null, country: "US" });
});

test("naiImageUrls dedupes detail and feed images", () => {
  const row = { postImages: [{ url: "https://cdn.example.com/a.jpg" }] };
  const detail = {
    postImages: [
      { url: "https://cdn.example.com/a.jpg" },
      { url: "https://cdn.example.com/b.jpg" },
    ],
  };
  assert.deepEqual(naiImageUrls(row, detail), [
    "https://cdn.example.com/a.jpg",
    "https://cdn.example.com/b.jpg",
  ]);
  assert.deepEqual(naiImageUrls(row, null), ["https://cdn.example.com/a.jpg"]);
  assert.deepEqual(naiImageUrls({}, {}), []);
});

test("naiDocumentUrls collects document and preview URLs", () => {
  assert.deepEqual(
    naiDocumentUrls({
      urlDocument: "https://cdn.example.com/om.pdf",
      documentPreview: "https://cdn.example.com/preview.pdf",
    }),
    ["https://cdn.example.com/om.pdf", "https://cdn.example.com/preview.pdf"]
  );
  assert.deepEqual(naiDocumentUrls({ urlDocument: "not-a-url" }), []);
});

test("naiPriceText formats currency and bare values", () => {
  assert.equal(naiPriceText({ price: 1250000, currency: "USD" }), "USD 1250000");
  assert.equal(naiPriceText({ price: 42 }), "42");
  assert.equal(naiPriceText({ price: null }), null);
  assert.equal(naiPriceText({}), null);
});

test("naiSizeText joins building, range, and land size", () => {
  assert.equal(
    naiSizeText({ sizeTotal: 25000, sizeRangeL: 1000, sizeRangeH: 5000, landSize: 2.5 }),
    "25000 SF; 1000-5000 SF; 2.5 acres land"
  );
  assert.equal(naiSizeText({ sizeTotal: 0 }), null);
  assert.equal(naiSizeText({}), null);
});

test("naiListingStatus normalizes scalar and array values", () => {
  assert.equal(naiListingStatus({ listingStatus: "FOR_SALE_ON_MARKET" }), "FOR_SALE_ON_MARKET");
  assert.equal(
    naiListingStatus({ listingStatus: ["FOR_SALE_ON_MARKET", "ACTIVE"] }),
    "FOR_SALE_ON_MARKET,ACTIVE"
  );
  assert.equal(naiListingStatus({ listingStatus: [] }), null);
  assert.equal(naiListingStatus({}), null);
});

test("naiListingFromFeed maps sale row with detail enrichment", () => {
  const row = {
    id: 12345,
    title: "Feed Title",
    summary: "Feed summary",
    publishedAt: "2026-06-01T12:00:00Z",
    contentType: { id: 4, name: "For Sale" },
    source: { name: "NAI Dallas" },
    locations: [{ path: "Dallas, TX, United States" }],
    postImages: [{ url: "https://cdn.example.com/feed.jpg" }],
  };
  const detail = {
    title: "Detail Title",
    summary: "Detail summary",
    content: "<p>Full HTML description</p>",
    price: 5000000,
    currency: "USD",
    sizeTotal: 40000,
    listingStatus: "FOR_SALE_ON_MARKET",
    contactEmail: "broker@example.com",
    urlDocument: "https://cdn.example.com/om.pdf",
    urlOriginal: "https://nai.example.com/listing",
    locations: [{ path: "Dallas, TX, United States", geometry: { coordinates: [-96.8, 32.78] } }],
    postImages: [{ url: "https://cdn.example.com/detail.jpg" }],
    source: { name: "NAI Dallas", socialLinks: ["https://linkedin.com/nai"] },
    tags: ["office"],
  };

  const listing = naiListingFromFeed(row, "sale", detail, null);

  assert.equal(listing.id, "infabode:12345");
  assert.equal(listing.name, "Detail Title");
  assert.equal(listing.transactionType, "Sale");
  assert.equal(listing.city, "Dallas");
  assert.equal(listing.state, "TX");
  assert.equal(listing.salePriceText, "USD 5000000");
  assert.equal(listing.leaseRateText, null);
  assert.equal(listing.sizeText, "40000 SF");
  assert.equal(listing.latitude, 32.78);
  assert.equal(listing.longitude, -96.8);
  assert.equal(listing.url, `${NAI_LISTING_URL_BASE}/12345`);
  assert.equal(listing.listingStatus, "FOR_SALE_ON_MARKET");
  assert.equal(listing.contactsDetailed.length, 1);
  assert.equal(listing.contactsDetailed[0].email, "broker@example.com");
  assert.equal(listing.brochures.length, 1);
  assert.ok(listing.photos.includes("https://cdn.example.com/detail.jpg"));
  assert.equal(listing.detailError, undefined);
});

test("naiListingFromFeed omits children on detailError", () => {
  const row = { id: 99, title: "Stub", publishedAt: "2026-06-01" };
  const listing = naiListingFromFeed(row, "lease", null, "timeout");

  assert.equal(listing.transactionType, "Lease");
  assert.equal(listing.detailError, "timeout");
  assert.deepEqual(listing.brochures, []);
  assert.deepEqual(listing.contactsDetailed, []);
});
