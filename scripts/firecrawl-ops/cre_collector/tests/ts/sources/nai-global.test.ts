// Isolate argv before nai-global.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  naiLocation,
  naiImageUrls,
  naiDocumentUrls,
  naiPriceText,
  naiSizeText,
  naiListingStatus,
  naiIsSourceEligible,
  naiListingFromFeed,
  harvestNai,
  naiBuildingClassFromTags,
  naiGraphqlPost,
  naiFeedPageCacheKey,
  naiPageSignature,
  naiSourceIdBatches,
  NAI_LISTING_URL_BASE,
} from "../../../sources/nai-global.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(__dirname, "../../fixtures/raw_data/nai-global.json");
const fixtures: any[] = JSON.parse(readFileSync(FIXTURE_PATH, "utf-8"));

test("naiGraphqlPost deadline covers a stalled response body", async (t) => {
  const originalFetch = globalThis.fetch;
  let aborted = false;
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    init?.signal?.addEventListener("abort", () => {
      aborted = true;
    });
    return {
      ok: true,
      status: 200,
      // `fetch()` has resolved with headers, but reading the response body
      // never finishes. This is the failure mode that previously stranded the
      // source after its request timeout had already been cleared.
      text: () => new Promise<string>(() => {}),
    } as Response;
  }) as typeof fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  await assert.rejects(
    () => naiGraphqlPost("https://infabode.example/public_api", {}, "https://ab.infabode.com/nai", 20),
    /timed out after 20ms/
  );
  assert.equal(aborted, true);
});

test("NAI source batches cover each office once and namespace the page cache", () => {
  assert.deepEqual(naiSourceIdBatches([10, 11, 10, 12, 13], 2), [[10, 11], [12, 13]]);
  assert.equal(naiFeedPageCacheKey(18, [10, 11]), "10,11:18");
  assert.notEqual(naiFeedPageCacheKey(18, [10, 11]), naiFeedPageCacheKey(18, [12, 13]));
});

test("NAI page signatures detect an exact repeated provider page", () => {
  assert.equal(naiPageSignature([{ id: 10 }, { id: "11" }]), "10,11");
  assert.equal(naiPageSignature([{ id: 10 }, { title: "missing id" }]), null);
  assert.equal(naiPageSignature([]), null);
});

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

test("NAI full and monitor paths share the conservative source-eligibility rule", () => {
  assert.equal(
    naiIsSourceEligible({ contentType: { id: 4 }, listingStatus: "FOR_SALE_ON_MARKET" }, "sale"),
    true
  );
  // NAI's lease rows can use the same provider label; transaction type remains
  // the authoritative discriminator, matching the established full-path rule.
  assert.equal(
    naiIsSourceEligible({ contentType: { id: 10 }, listingStatus: "FOR_SALE_ON_MARKET" }, "lease"),
    true
  );
  assert.equal(
    naiIsSourceEligible({ contentType: { id: 4 }, listingStatus: "OFF_MARKET" }, "sale"),
    false
  );
  assert.equal(
    naiIsSourceEligible({ contentType: { id: 10 }, listingStatus: "FOR_SALE_ON_MARKET" }, "sale"),
    false
  );
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
  // Capture-everything harvest fired (clean detail): documents ride the unified
  // `documents` channel and the legacy `brochures` key is dropped to avoid a
  // duplicate insert. The urlDocument OM is classified onto documents.
  assert.equal(listing.brochures, undefined);
  assert.ok(Array.isArray(listing.documents));
  assert.ok(
    listing.documents.some((d: any) => d.url === "https://cdn.example.com/om.pdf"),
    "urlDocument OM is harvested onto the documents channel"
  );
  // urlOriginal -> external_listing link; the source socialLinks -> social link.
  assert.ok(Array.isArray(listing.links));
  assert.ok(
    listing.links.some(
      (l: any) => l.url === "https://nai.example.com/listing" && l.linkType === "external_listing"
    ),
    "urlOriginal is harvested as an external_listing link"
  );
  assert.ok(
    listing.links.some((l: any) => l.linkType === "social" && /linkedin/.test(l.url)),
    "source socialLinks are harvested as social links"
  );
  assert.ok(listing.photos.includes("https://cdn.example.com/detail.jpg"));
  // Full-page markdown is lifted from detail.content (HTML stripped to text).
  assert.ok(typeof listing.markdown === "string" && listing.markdown.length > 0);
  assert.equal(listing.detailError, undefined);
});

test("naiListingFromFeed omits children on detailError", () => {
  const row = { id: 99, title: "Stub", publishedAt: "2026-06-01" };
  const listing = naiListingFromFeed(row, "lease", null, "timeout");

  assert.equal(listing.transactionType, "Lease");
  assert.equal(listing.detailError, "timeout");
  // Byte-identical legacy shape on a failed/absent detail: brochures kept, no
  // harvest keys added (the spread is empty when harvested is null).
  assert.deepEqual(listing.brochures, []);
  assert.equal(listing.documents, undefined);
  assert.equal(listing.media, undefined);
  assert.equal(listing.links, undefined);
  assert.deepEqual(listing.contactsDetailed, []);
});

test("naiListingFromFeed safely promotes current public-feed price and size scalars", () => {
  const row = {
    id: 1634055,
    title: "64 Front Street",
    summary: "Current public listing",
    publishedAt: "2026-07-16T13:59:35Z",
    currency: "DOLLAR",
    price: 649000,
    sizeTotal: 3040,
    landSize: 0.185,
    url: "https://infabode.com/post/1634055",
    contentType: { id: 4, name: "Sale Listings" },
    source: { name: "NAI Beverly-Hanks" },
    locations: [{ path: "Dillsboro, NC, United States" }],
    postImages: [{ url: "https://cdn.example.com/feed.jpg" }],
  };

  const listing = naiListingFromFeed(row, "sale", null, null, true);
  assert.equal(listing.salePriceText, "DOLLAR 649000");
  assert.equal(listing.salePriceUsd, 649000);
  assert.equal(listing.buildingSizeSqft, 3040);
  assert.equal(listing.lotSizeAcres, 0.185);
  assert.equal(listing.canonicalUrl, "https://infabode.com/post/1634055");
  assert.equal(listing.listingStatus, null, "feed mode must not invent a detail-only status");
  assert.equal(listing.contactsDetailed.length, 0);
  assert.equal(listing.documents, undefined);
});

test("harvestNai extracts an iframe video from detail.content and classifies docs/links", () => {
  const detail = {
    content: `<p>Great asset.</p><iframe src="https://player.vimeo.com/video/824804225"></iframe>`,
    urlDocument: "https://cdn.example.com/om.pdf",
    documentPreview: "https://cdn.example.com/preview.pdf",
    urlOriginal: "https://crexi.com/listing/55",
    postImages: [{ url: "https://cdn.example.com/g1.jpg" }, { url: "https://cdn.example.com/g2.jpg" }],
    source: { socialLinks: ["https://www.facebook.com/naidallas"] },
  };
  const out = harvestNai({}, detail);
  // iframe vimeo -> media; provider vimeo; embed preserved.
  assert.ok(out.media.some((m: any) => m.provider === "vimeo"));
  // urlDocument / documentPreview -> documents.
  assert.ok(out.documents.some((d: any) => /om\.pdf$/.test(d.url)));
  // urlOriginal crexi -> external_listing link; facebook -> social.
  assert.ok(out.links.some((l: any) => l.linkType === "external_listing" && /crexi/.test(l.url)));
  assert.ok(out.links.some((l: any) => l.linkType === "social" && /facebook/.test(l.url)));
  // full image gallery kept.
  assert.ok(out.images.includes("https://cdn.example.com/g1.jpg"));
  assert.ok(out.images.includes("https://cdn.example.com/g2.jpg"));
});

test("harvestNai returns empty arrays for an absent detail", () => {
  assert.deepEqual(harvestNai({}, null), { media: [], links: [], documents: [], images: [] });
});

// ---------------------------------------------------------------------------
// Phase-2 scalar field tests (naiBuildingClassFromTags + naiListingFromFeed
// new camelCase fields: canonicalUrl, salePriceUsd/leaseRateMin, highlights,
// minDivisibleSf, maxDivisibleSf, buildingClass, extraFacts).
// Uses the saved fixture blobs from tests/fixtures/raw_data/nai-global.json.
// ---------------------------------------------------------------------------

test("naiBuildingClassFromTags extracts class from BuildingClassX tags", () => {
  assert.equal(naiBuildingClassFromTags(["BuildingClassA"]), "A");
  assert.equal(naiBuildingClassFromTags(["BuildingClassB", "Parking"]), "B");
  assert.equal(naiBuildingClassFromTags(["BuildingClassC"]), "C");
  assert.equal(naiBuildingClassFromTags(["RetailAsset", "Parking"]), null);
  assert.equal(naiBuildingClassFromTags([]), null);
  assert.equal(naiBuildingClassFromTags(null), null);
  assert.equal(naiBuildingClassFromTags("not-an-array"), null);
});

test("Phase-2 sale listing: canonicalUrl, salePriceUsd (POUND->USD), buildingClass, extraFacts", () => {
  // Fixture[0]: 209 North Pacific Ave sale, currency='POUND', price=1595000, tag=BuildingClassB
  const f = fixtures[0]!;
  const listing = naiListingFromFeed(f.feedRow, "sale", f.publicPost, null);

  // canonicalUrl = publicPost.urlOriginal (== sourceWebsiteUrl)
  assert.equal(
    listing.canonicalUrl,
    "https://www.naiglobal.com/listings/?propertyId=209-north-pacific-avenue-los-angeles-sale"
  );

  // salePriceUsd: parseAmountIgnoringCurrencyLabel("POUND 1595000") = 1595000
  assert.equal(listing.salePriceUsd, 1595000);

  // leaseRateMin must NOT be set on a sale listing
  assert.equal(listing.leaseRateMin, undefined);

  // buildingClass: "BuildingClassB" tag -> "B"
  assert.equal(listing.buildingClass, "B");

  // highlights: tags array (["BuildingClassB"])
  assert.deepEqual(listing.highlights, ["BuildingClassB"]);

  // extraFacts: listing_office = 'NAI Capital Commercial'
  assert.ok(listing.extraFacts && typeof listing.extraFacts === "object");
  assert.equal(listing.extraFacts!["listing_office"], "NAI Capital Commercial");

  // statusBadge must NOT be set (contaminated listingStatus)
  assert.equal(listing.statusBadge, undefined);
});

test("Phase-2 lease listing: leaseRateMin (POUND->USD), buildingClass, minDivisibleSf/maxDivisibleSf", () => {
  // Fixture[1]: 1486 S 1100 E lease, currency='POUND', price=27 ($/SF/yr), tag=BuildingClassB
  // sizeRangeL=1500, sizeRangeH=12705
  const f = fixtures[1]!;
  const listing = naiListingFromFeed(f.feedRow, "lease", f.publicPost, null);

  // leaseRateMin: parseAmountIgnoringCurrencyLabel("POUND 27") = 27
  assert.equal(listing.leaseRateMin, 27);

  // salePriceUsd must NOT be set on a lease listing
  assert.equal(listing.salePriceUsd, undefined);

  // canonicalUrl
  assert.equal(
    listing.canonicalUrl,
    "https://www.naiglobal.com/listings/?propertyId=1486-s-1100-e-salt-lake-city-lease"
  );

  // buildingClass: "BuildingClassB" tag -> "B"
  assert.equal(listing.buildingClass, "B");

  // highlights: tags array (["BuildingClassB","Parking"])
  assert.deepEqual(listing.highlights, ["BuildingClassB", "Parking"]);

  // minDivisibleSf: sizeRangeL=1500, maxDivisibleSf: sizeRangeH=12705
  assert.equal(listing.minDivisibleSf, 1500);
  assert.equal(listing.maxDivisibleSf, 12705);

  // extraFacts: listing_office = 'NAI Excel'
  assert.ok(listing.extraFacts && typeof listing.extraFacts === "object");
  assert.equal(listing.extraFacts!["listing_office"], "NAI Excel");
});

test("Phase-2 sale listing: non-BuildingClass tags -> buildingClass is absent/null", () => {
  // Fixture[2]: 5390 North Beckman Lane sale, tags=['RetailAsset'], no sizeRange
  const f = fixtures[2]!;
  const listing = naiListingFromFeed(f.feedRow, "sale", f.publicPost, null);

  // buildingClass: "RetailAsset" has no class letter -> null -> pruned to undefined
  assert.ok(listing.buildingClass == null);

  // minDivisibleSf and maxDivisibleSf: sizeRangeL/H absent -> null/undefined
  assert.ok(listing.minDivisibleSf == null);
  assert.ok(listing.maxDivisibleSf == null);

  // salePriceUsd: parseAmountIgnoringCurrencyLabel("POUND 6230416") = 6230416
  assert.equal(listing.salePriceUsd, 6230416);

  // highlights: tags=['RetailAsset'] -> emitted
  assert.deepEqual(listing.highlights, ["RetailAsset"]);

  // extraFacts: listing_office = 'NAI Excel'
  assert.equal(listing.extraFacts!["listing_office"], "NAI Excel");
});

test("Phase-2: monitor mode (detail=null) emits no new scalar fields", () => {
  // When detail=null (monitor pass), detailScalars is {} and none of the new
  // Phase-2 fields should appear.
  const row = { id: 99, title: "Monitor row", publishedAt: "2026-06-01" };
  const listing = naiListingFromFeed(row, "sale", null, null);

  assert.equal(listing.canonicalUrl, undefined);
  assert.equal(listing.salePriceUsd, undefined);
  assert.equal(listing.leaseRateMin, undefined);
  assert.equal(listing.highlights, undefined);
  assert.equal(listing.minDivisibleSf, undefined);
  assert.equal(listing.maxDivisibleSf, undefined);
  assert.equal(listing.buildingClass, undefined);
  assert.equal(listing.extraFacts, undefined);
  assert.equal(listing.statusBadge, undefined);
});

test("Phase-2: detailError=timeout emits no new scalar fields (same as detail=null)", () => {
  const row = { id: 55, title: "Error row", publishedAt: "2026-06-01" };
  const listing = naiListingFromFeed(row, "lease", null, "timeout");

  assert.equal(listing.canonicalUrl, undefined);
  assert.equal(listing.salePriceUsd, undefined);
  assert.equal(listing.leaseRateMin, undefined);
  assert.equal(listing.buildingClass, undefined);
  assert.equal(listing.extraFacts, undefined);
  assert.equal(listing.statusBadge, undefined);
  assert.equal(listing.detailError, "timeout");
});

test("Phase-2: publicPost with no price emits null/absent salePriceUsd", () => {
  const row = { id: 77, title: "No price", publishedAt: "2026-06-01", source: { name: "NAI Test" } };
  const detail = {
    id: 77,
    title: "No price",
    tags: ["BuildingClassA"],
    price: null,
    currency: null,
    locations: [{ name: "Dallas", path: "Dallas, TX, United States", geometry: { coordinates: [-96.8, 32.78] } }],
    sizeTotal: 5000,
    urlOriginal: "https://www.naiglobal.com/listings/?propertyId=test",
    listingStatus: ["FOR_SALE_ON_MARKET"],
    source: { name: "NAI Test", socialLinks: [] },
  };
  const listing = naiListingFromFeed(row, "sale", detail, null);

  // salePriceUsd: price=null -> priceText=null -> parseAmountIgnoringCurrencyLabel(null) = null
  assert.ok(listing.salePriceUsd == null);

  // canonicalUrl still set
  assert.equal(listing.canonicalUrl, "https://www.naiglobal.com/listings/?propertyId=test");

  // buildingClass: "BuildingClassA" -> "A"
  assert.equal(listing.buildingClass, "A");

  // statusBadge: never set
  assert.equal(listing.statusBadge, undefined);
});
