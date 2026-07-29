import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join, dirname } from "node:path";
import {
  normalizePersonName,
  newmarkState,
  newmarkGalleryUrls,
  newmarkExtraUrls,
  newmarkSalePrice,
  newmarkCoverageTruncated,
  assertNewmarkAlgoliaInventoryPage,
  classifyNewmarkPeopleLookup,
  newmarkPeopleFailure,
  newmarkPeopleListingFields,
  srcNewmark,
} from "../../../sources/newmark.js";
import { firecrawl } from "../../../lib/scrape.js";

// ---------------------------------------------------------------------------
// Existing helper tests (unchanged)
// ---------------------------------------------------------------------------

test("normalizePersonName lowercases and collapses whitespace", () => {
  assert.equal(normalizePersonName("  Jane   Q.  Public "), "jane q. public");
  assert.equal(normalizePersonName(null), null);
  assert.equal(normalizePersonName(""), null);
});

test("newmarkState prefers explicit state fields", () => {
  assert.equal(newmarkState({ state: "Texas" }), "Texas");
  assert.equal(newmarkState({ state_code: "tx" }), "TX");
});

test("newmarkState infers DC from Washington zip", () => {
  assert.equal(newmarkState({ city: "Washington", zip: "20005" }), "DC");
});

test("newmarkState returns null when no signal", () => {
  assert.equal(newmarkState({ city: "Chicago" }), null);
});

test("newmarkGalleryUrls keeps the FULL thumbnail gallery (no truncation)", () => {
  const hit = {
    thumbnails: [
      { url: "/img/a.jpg" },
      { url: "/img/b.jpg" },
      { url: "https://cdn.example.com/c.jpg" },
    ],
  };
  const urls = newmarkGalleryUrls(hit);
  assert.equal(urls.length, 3);
  assert.ok(urls.includes("https://www.nmrk.com/img/a.jpg"));
  assert.ok(urls.includes("https://www.nmrk.com/img/b.jpg"));
  assert.ok(urls.includes("https://cdn.example.com/c.jpg"));
});

test("Newmark finite caps report truncation against Algolia nbHits", () => {
  assert.equal(newmarkCoverageTruncated(2, 2, 1), true);
  assert.equal(newmarkCoverageTruncated(2, 2, 2), false);
  assert.equal(
    newmarkCoverageTruncated(2, 2, Number.POSITIVE_INFINITY),
    false
  );
  assert.equal(newmarkCoverageTruncated(2, 1, Number.POSITIVE_INFINITY), true);
});

test("newmarkGalleryUrls dedupes and tolerates an empty/garbage hit", () => {
  assert.deepEqual(newmarkGalleryUrls({}), []);
  assert.deepEqual(newmarkGalleryUrls({ thumbnails: [{ url: "/x.jpg" }, { url: "/x.jpg" }] }), [
    "https://www.nmrk.com/x.jpg",
  ]);
});

test("newmarkExtraUrls collects candidate media and document fields", () => {
  const { media, docs } = newmarkExtraUrls({
    virtualTourUrl: "https://my.matterport.com/show/?m=abc",
    video_url: "https://vimeo.com/123",
    brochureUrl: "https://cdn.example.com/om.pdf",
    irrelevant: "not a url",
  });
  assert.ok(media.includes("https://my.matterport.com/show/?m=abc"));
  assert.ok(media.includes("https://vimeo.com/123"));
  assert.ok(docs.includes("https://cdn.example.com/om.pdf"));
});

test("newmarkCoverageTruncated fails closed when Algolia partitions under-recover nbHits", () => {
  assert.equal(newmarkCoverageTruncated(1500, 1000, Infinity), true);
  assert.equal(newmarkCoverageTruncated(1500, 1500, Infinity), false);
  assert.equal(newmarkCoverageTruncated(1500, 100, 100), true);
  assert.equal(newmarkCoverageTruncated(1500, 1500, Infinity, true), true);
});

test("strict Newmark Algolia inventory pages require coherent integer nbHits", () => {
  assert.deepEqual(
    assertNewmarkAlgoliaInventoryPage(
      { hits: [{ objectID: "a" }], nbHits: 1 },
      "test",
      true
    ).hits,
    [{ objectID: "a" }]
  );
  assert.throws(
    () => assertNewmarkAlgoliaInventoryPage({ hits: [] }, "test", true),
    /nonnegative integer nbHits/
  );
  assert.throws(
    () => assertNewmarkAlgoliaInventoryPage({ hits: [], nbHits: -1 }, "test", true),
    /nonnegative integer nbHits/
  );
  assert.throws(
    () => assertNewmarkAlgoliaInventoryPage({ hits: [], nbHits: 1.5 }, "test", true),
    /nonnegative integer nbHits/
  );
  assert.throws(
    () =>
      assertNewmarkAlgoliaInventoryPage(
        { hits: [{ objectID: "a" }, { objectID: "b" }], nbHits: 1 },
        "test",
        true
      ),
    /below returned hits/
  );
});

test("non-strict Newmark monitoring remains compatible with missing nbHits", () => {
  assert.doesNotThrow(() =>
    assertNewmarkAlgoliaInventoryPage({ hits: [] }, "monitor", false)
  );
  assert.throws(
    () => assertNewmarkAlgoliaInventoryPage({}, "monitor", false),
    /no hits array/
  );
});

test("Newmark People lookup distinguishes verified absence from malformed responses", () => {
  assert.deepEqual(
    classifyNewmarkPeopleLookup(
      { hits: [{ fullName: "Different Broker" }], nbHits: 1 },
      "Ada Broker",
      true
    ),
    { status: "verified_absent" }
  );
  assert.throws(
    () => classifyNewmarkPeopleLookup({}, "Ada Broker", true),
    /People index response has no hits array/
  );
  assert.deepEqual(
    classifyNewmarkPeopleLookup({}, "Ada Broker", false),
    {
      status: "failed",
      error: "Newmark People index response has no hits array",
    }
  );
  assert.throws(
    () =>
      classifyNewmarkPeopleLookup(
        { hits: [], nbHits: Number.NaN },
        "Ada Broker",
        true
      ),
    /incoherent hits\/nbHits/
  );
  assert.deepEqual(
    classifyNewmarkPeopleLookup(
      { hits: [], nbHits: Number.NaN },
      "Ada Broker",
      false
    ),
    {
      status: "failed",
      error: "Newmark People index response has incoherent hits/nbHits",
    }
  );
});

test("Newmark People transport failures fail strict mode but preserve non-strict children", () => {
  assert.throws(
    () => newmarkPeopleFailure(new Error("HTTP 503"), "Ada Broker", true),
    /people lookup failed for Ada Broker: HTTP 503/
  );
  assert.deepEqual(
    newmarkPeopleFailure(new Error("HTTP 503"), "Ada Broker", false),
    {
      status: "failed",
      error: "HTTP 503",
    }
  );
  assert.deepEqual(
    newmarkPeopleListingFields(
      { status: "failed", error: "HTTP 503" },
      false
    ),
    {
      preserveChildCollections: true,
      newmarkPeopleLookupStatus: "failed",
    }
  );
  assert.deepEqual(
    newmarkPeopleListingFields({ status: "verified_absent" }, false),
    {
      contactsDetailed: [],
      newmarkPeopleLookupStatus: "verified_absent",
    }
  );
});

test("strict Newmark bypasses Firecrawl cache and rejects partition pages without nbHits", async () => {
  const oldScrape = firecrawl.scrape;
  const oldFetch = globalThis.fetch;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const scrapeCalls: any[] = [];
  (firecrawl as any).scrape = async (_url: string, options: any) => {
    scrapeCalls.push(options);
    return {
      rawHtml:
        "<script>algoliaAppId='strict-app';algoliaSearchApiKey='strict-key';algoliaIndexName='strict-index';</script>",
    };
  };
  let algoliaCall = 0;
  globalThis.fetch = async () => {
    algoliaCall++;
    const body =
      algoliaCall === 1
        ? {
            hits: [{ objectID: "listing-1", slug: "listing-1" }],
            nbHits: 2,
            facets: { state: { TX: 2 } },
          }
        : { hits: [], facets: { property_types: {} } };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    await assert.rejects(
      () => srcNewmark("sale", Infinity, true),
      /nonnegative integer nbHits/
    );
    assert.equal(scrapeCalls.length, 1);
    assert.equal(scrapeCalls[0]?.maxAge, 0);
  } finally {
    (firecrawl as any).scrape = oldScrape;
    globalThis.fetch = oldFetch;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

// ---------------------------------------------------------------------------
// WS1 scalar lift: newmarkSalePrice helper
// ---------------------------------------------------------------------------

test("newmarkSalePrice parses a dollar-formatted string correctly", () => {
  assert.equal(newmarkSalePrice("$8,585,673.00"), 8585673);
});

test("newmarkSalePrice rejects 'Subject to Offer' (case-insensitive)", () => {
  assert.equal(newmarkSalePrice('"Subject to Offer"'), null);
  assert.equal(newmarkSalePrice("subject to offer"), null);
  assert.equal(newmarkSalePrice("SUBJECT TO OFFER"), null);
});

test("newmarkSalePrice rejects $0.00 placeholder", () => {
  // parseMoney returns null for zero (isFinite && v > 0 guard)
  assert.equal(newmarkSalePrice("$0.00"), null);
});

test("newmarkSalePrice returns null for non-string input", () => {
  assert.equal(newmarkSalePrice(null), null);
  assert.equal(newmarkSalePrice(undefined), null);
  assert.equal(newmarkSalePrice(0), null);
});

test("newmarkSalePrice returns null for non-numeric strings", () => {
  assert.equal(newmarkSalePrice("Contact broker"), null);
  assert.equal(newmarkSalePrice("TBD"), null);
});

// ---------------------------------------------------------------------------
// WS1 scalar lift: fixture-driven field assertions
// ---------------------------------------------------------------------------

// Resolve the fixture path relative to this test file.
const FIXTURE_PATH = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../fixtures/raw_data/newmark.json"
);

// Load and parse the fixture once.
const fixtureListings: any[] = JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));

// Helper: find a fixture entry by its id field.
function byId(id: string): any {
  const entry = fixtureListings.find((l) => l.id === id);
  if (!entry) throw new Error(`Fixture listing not found: ${id}`);
  return entry;
}

// --- Listing 1: Warehouse/Distribution sale with parseable sale_price ---

test("fixture listing 1: county emitted from top-level", () => {
  const l = byId("1751-yeager-ave-la-verne-sale");
  // county is the top-level field that mirrors rawNewmarkHit.county
  assert.equal(l.county, "Los Angeles");
});

test("fixture listing 1: submarket emitted from top-level", () => {
  const l = byId("1751-yeager-ave-la-verne-sale");
  assert.equal(l.submarket, "LA East");
});

test("fixture listing 1: market emitted from rawNewmarkHit.market", () => {
  const l = byId("1751-yeager-ave-la-verne-sale");
  // Verify the fixture has market in rawNewmarkHit
  assert.equal(l.rawNewmarkHit.market, "Los Angeles");
});

test("fixture listing 1: propertySubtype from rawNewmarkHit.property_subtype", () => {
  const l = byId("1751-yeager-ave-la-verne-sale");
  assert.equal(l.rawNewmarkHit.property_subtype, "Warehouse/Distribution");
});

test("fixture listing 1: statusBadge from rawNewmarkHit.status", () => {
  const l = byId("1751-yeager-ave-la-verne-sale");
  assert.equal(l.rawNewmarkHit.status, "For Sale");
});

test("fixture listing 1: salePriceUsd correctly parsed via newmarkSalePrice", () => {
  const l = byId("1751-yeager-ave-la-verne-sale");
  const parsed = newmarkSalePrice(l.rawNewmarkHit.sale_price);
  assert.equal(parsed, 8585673);
});

test("fixture listing 1: no number_of_units in rawNewmarkHit -> units null", () => {
  const l = byId("1751-yeager-ave-la-verne-sale");
  // This listing has no number_of_units
  assert.equal(l.rawNewmarkHit.number_of_units, undefined);
});

test("fixture listing 1: canonicalUrl is the absolute nmrk.com URL", () => {
  const l = byId("1751-yeager-ave-la-verne-sale");
  assert.match(l.url, /^https:\/\/www\.nmrk\.com\/properties\//);
});

test("fixture listing 1: headline (description) is non-null", () => {
  const l = byId("1751-yeager-ave-la-verne-sale");
  assert.ok(typeof l.headline === "string" && l.headline.length > 0);
});

// --- Listing 2: 'Subject to Offer' sale_price rejection + units ---

test("fixture listing 2: salePriceUsd is null when sale_price is 'Subject to Offer'", () => {
  const l = byId("2210-melson-ave-jacksonville-sale-1642678");
  const parsed = newmarkSalePrice(l.rawNewmarkHit.sale_price);
  assert.equal(parsed, null);
});

test("fixture listing 2: number_of_units=4 present in rawNewmarkHit", () => {
  const l = byId("2210-melson-ave-jacksonville-sale-1642678");
  assert.equal(l.rawNewmarkHit.number_of_units, 4);
});

test("fixture listing 2: county is Duval (no submarket in this listing)", () => {
  const l = byId("2210-melson-ave-jacksonville-sale-1642678");
  assert.equal(l.county, "Duval");
  assert.equal(l.submarket, undefined);
});

// --- Listing 3: Lease listing with market + submarket from rawNewmarkHit ---

test("fixture listing 3: statusBadge is 'For Lease'", () => {
  const l = byId("100-west-lexington-street-baltimore-lease");
  assert.equal(l.rawNewmarkHit.status, "For Lease");
});

test("fixture listing 3: market from rawNewmarkHit.market", () => {
  const l = byId("100-west-lexington-street-baltimore-lease");
  assert.equal(l.rawNewmarkHit.market, "Baltimore");
});

test("fixture listing 3: submarket from top-level and rawNewmarkHit.submarket", () => {
  const l = byId("100-west-lexington-street-baltimore-lease");
  assert.equal(l.submarket, "CBD Baltimore");
  assert.equal(l.rawNewmarkHit.submarket, "CBD Baltimore");
});

test("fixture listing 3: propertySubtype is Street Retail", () => {
  const l = byId("100-west-lexington-street-baltimore-lease");
  assert.equal(l.rawNewmarkHit.property_subtype, "Street Retail");
});

test("fixture listing 3: no sale_price field -> newmarkSalePrice returns null", () => {
  const l = byId("100-west-lexington-street-baltimore-lease");
  const parsed = newmarkSalePrice(l.rawNewmarkHit.sale_price);
  assert.equal(parsed, null);
});

test("fixture listing 3: url is absolute nmrk.com listing URL", () => {
  const l = byId("100-west-lexington-street-baltimore-lease");
  assert.match(l.url, /^https:\/\/www\.nmrk\.com\/properties\//);
});

// --- Absent-field / null-safety assertions ---

test("newmarkSalePrice does not throw on empty string", () => {
  assert.doesNotThrow(() => newmarkSalePrice(""));
  assert.equal(newmarkSalePrice(""), null);
});

test("fixture all listings: every rawNewmarkHit has a status field (100% coverage)", () => {
  for (const l of fixtureListings) {
    assert.ok(
      typeof l.rawNewmarkHit?.status === "string" && l.rawNewmarkHit.status.length > 0,
      `Listing ${l.id} missing rawNewmarkHit.status`
    );
  }
});

test("fixture all listings: every rawNewmarkHit has a property_subtype field (100% coverage)", () => {
  for (const l of fixtureListings) {
    assert.ok(
      typeof l.rawNewmarkHit?.property_subtype === "string",
      `Listing ${l.id} missing rawNewmarkHit.property_subtype`
    );
  }
});
