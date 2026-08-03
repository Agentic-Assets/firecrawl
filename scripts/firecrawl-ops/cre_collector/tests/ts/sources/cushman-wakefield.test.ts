// Isolate argv before cushman-wakefield.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import type { ScrapedDoc } from "../../../types.js";
import {
  CUSHMAN_API_BASE,
  CUSHMAN_HOST,
  canonicalCushmanUrl,
  canonicalCushmanAssetUrl,
  cushmanIdentityUrl,
  cushmanCanonicalIdentity,
  dedupeAssetsBestWidth,
  pmediaId,
  extractCushmanDocuments,
  extractCushmanPhotos,
  markdownLabel,
  firstNumberText,
  sqftFromText,
  acresFromText,
  cushmanSearchApiUrl,
  extractCushmanContacts,
  extractCushmanAssetUrls,
  baseCushmanListing,
  baseCushmanExtraFacts,
  cushmanUseBaseRows,
  cushmanBaseRefreshListing,
  enrichCushmanListing,
  assertCushmanDetailDoc,
  assertCushmanFreshnessMode,
  assertCushmanInventoryPage,
  assertCushmanInventoryReconciled,
  srcCushman,
} from "../../../sources/cushman-wakefield.js";
import { parseLeaseRate } from "../../../lib/parse.js";
import { firecrawl } from "../../../lib/scrape.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(__dirname, "../../fixtures/raw_data/cushman-wakefield.json");
const fixtureRows: any[] = JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));

const ASSET_BASE = "https://assets.cushmanwakefield.com";
const PMEDIA_ID = "listing-abc123";

function asset(path: string, query = ""): string {
  return `${ASSET_BASE}${path}${query}`;
}

test("canonicalCushmanUrl resolves relative paths and rewrites sitecore host", () => {
  assert.equal(
    canonicalCushmanUrl("/en/united-states/properties/lease/dallas-tower"),
    `${CUSHMAN_HOST}/en/united-states/properties/lease/dallas-tower`
  );
  assert.equal(
    canonicalCushmanUrl("https://sitecore-www.cushmanwakefield.com/property/x"),
    `${CUSHMAN_HOST}/property/x`
  );
  assert.equal(canonicalCushmanUrl("javascript:void(0)"), null);
  assert.equal(canonicalCushmanUrl(null), null);
});

test("canonicalCushmanAssetUrl keeps assets host and strips tracking params", () => {
  const raw = asset(
    `/pmedia/${PMEDIA_ID}/brochure.pdf`,
    "?sc=track&hash=deadbeef&rev=1"
  );
  assert.equal(
    canonicalCushmanAssetUrl(raw),
    asset(`/pmedia/${PMEDIA_ID}/brochure.pdf`, "?rev=1")
  );
  assert.equal(canonicalCushmanAssetUrl("https://www.cushmanwakefield.com/logo.png"), null);
});

test("dedupeAssetsBestWidth keeps the widest variant per rev key", () => {
  const narrow = asset(`/pmedia/${PMEDIA_ID}/photo.webp`, "?rev=9&w=400");
  const wide = asset(`/pmedia/${PMEDIA_ID}/photo.webp`, "?rev=9&w=1200");
  const otherRev = asset(`/pmedia/${PMEDIA_ID}/photo.webp`, "?rev=10&w=800");
  const deduped = dedupeAssetsBestWidth([narrow, wide, otherRev]);
  assert.equal(deduped.length, 2);
  assert.ok(deduped.includes(wide));
  assert.ok(deduped.includes(otherRev));
  assert.ok(!deduped.includes(narrow));
});

test("pmediaId extracts the pmedia segment id", () => {
  assert.equal(pmediaId(asset(`/pmedia/${PMEDIA_ID}/photo.webp`)), PMEDIA_ID);
  assert.equal(pmediaId("https://example.com/no-pmedia-here.jpg"), null);
});

test("extractCushmanDocuments dedupes PDF asset URLs", () => {
  const pdf = asset(`/pmedia/${PMEDIA_ID}/offering-memorandum.pdf`, "?rev=1");
  const docs = extractCushmanDocuments([pdf, pdf]);
  assert.equal(docs.length, 1);
  assert.equal(docs[0].url, pdf);
  assert.equal(docs[0].name, "offering memorandum");
});

test("extractCushmanPhotos prefers images sharing a brochure pmedia id", () => {
  const pdf = asset(`/pmedia/${PMEDIA_ID}/brochure.pdf`);
  const hero = asset(`/pmedia/${PMEDIA_ID}/hero.webp`);
  const other = asset("/pmedia/other-id/lobby.jpg");
  const people = asset("/pmedia/people/headshot.webp");
  const photos = extractCushmanPhotos([pdf, hero, other, people]);
  assert.deepEqual(photos, [hero]);
});

test("extractCushmanPhotos falls back to the first image when no PDF is present", () => {
  const first = asset("/pmedia/first-id/exterior.webp");
  const second = asset("/pmedia/second-id/lobby.webp");
  const photos = extractCushmanPhotos([first, second]);
  assert.deepEqual(photos, [first]);
});

test("markdownLabel and numeric parsers extract detail-page facts", () => {
  const markdown = [
    "Building Size",
    "12,500 SF",
    "",
    "Lot Size",
    "2.5 Acres",
    "",
    "Year Built",
    "1998 / 2015",
  ].join("\n");
  assert.equal(markdownLabel(markdown, "Building Size"), "12,500 SF");
  assert.equal(markdownLabel(markdown, "Missing Label"), null);
  assert.equal(firstNumberText("Built 1998 / renovated 2015"), 1998);
  assert.equal(sqftFromText("12,500 SF total"), 12500);
  assert.equal(acresFromText("2.5 Acres"), 2.5);
  assert.equal(acresFromText("87120 SF"), 2);
});

test("cushmanSearchApiUrl builds sale and lease API endpoints", () => {
  const sale = new URL(cushmanSearchApiUrl("sale", 0));
  const lease = new URL(cushmanSearchApiUrl("lease", 200));
  assert.equal(sale.origin + sale.pathname, CUSHMAN_API_BASE);
  assert.equal(sale.searchParams.get("listing_type"), "Buy");
  assert.equal(lease.searchParams.get("listing_type"), "Lease");
  assert.equal(lease.searchParams.get("offset"), "200");
  assert.equal(sale.searchParams.get("site_country"), "US");
});

test("extractCushmanAssetUrls collects canonical asset URLs from scraped docs", () => {
  const wide = asset(`/pmedia/${PMEDIA_ID}/photo.webp`, "?rev=3&w=1600");
  const narrow = asset(`/pmedia/${PMEDIA_ID}/photo.webp`, "?rev=3&w=300&sc=x");
  const doc: ScrapedDoc = {
    rawHtml: `<a href="${narrow}">photo</a>`,
    markdown: wide,
    links: [narrow],
  };
  const urls = extractCushmanAssetUrls(doc);
  assert.deepEqual(urls, [wide]);
});

test("extractCushmanContacts merges JSON-LD people with page links", () => {
  const doc: ScrapedDoc = {
    rawHtml: `
      <script type="application/ld+json">
        {
          "@type": "RealEstateListing",
          "offeredBy": {
            "@type": "Person",
            "name": "Ada Broker",
            "jobTitle": "Executive Director",
            "telephone": "+1 214-555-0100",
            "url": "/en/people/ada-broker"
          }
        }
      </script>
      <div>
        <a href="/en/people/ada-broker">Ada Broker</a>
        <a href="/api/GetVCard?person=ada-broker">VCard</a>
        <a href="tel:+1-214-555-0199">Call</a>
      </div>
    `,
    markdown: "",
    links: [],
  };
  const contacts = extractCushmanContacts(doc);
  assert.equal(contacts.length, 1);
  assert.equal(contacts[0].name, "Ada Broker");
  assert.equal(contacts[0].title, "Executive Director");
  assert.equal(contacts[0].phone, "+1 214-555-0100");
  assert.equal(contacts[0].profileUrl, `${CUSHMAN_HOST}/en/people/ada-broker`);
  assert.match(contacts[0].vcardUrl ?? "", /GetVCard/);
});

test("baseCushmanListing maps API rows into the shared listing shape", () => {
  const row = {
    id: "cw-1001",
    nav_title: "Dallas Midtown Tower",
    property_street: "1200 Main St",
    property_city: "Dallas",
    state_or_province: "tx",
    property_postal_code: "75201",
    property_country: "US",
    property_latitude: "32.78",
    property_longitude: "-96.80",
    property_type: "Office",
    listing_status: "Active",
    image_url: asset(`/pmedia/${PMEDIA_ID}/card.webp`),
    url: "/en/united-states/properties/lease/dallas-midtown-tower",
  };
  const listing = baseCushmanListing(row, "lease");
  assert.equal(listing.id, cushmanCanonicalIdentity(row.url));
  assert.equal(listing.providerId, "cw-1001");
  assert.equal(listing.name, "Dallas Midtown Tower");
  assert.equal(listing.transactionType, "Lease");
  assert.equal(listing.state, "TX");
  assert.equal(listing.street, "1200 Main St");
  assert.equal(listing.listingStatus, "Active");
  assert.equal(listing.url, `${CUSHMAN_HOST}/en/united-states/properties/lease/dallas-midtown-tower`);
  assert.equal(listing.photos.length, 1);
  assert.match(listing.photos[0], /card\.webp/);
  assert.equal(listing.salePriceText, null);
});

test("Cushman identity is URL-derived and provider GUID rotation is provenance-only", () => {
  const url =
    "https://www.cushmanwakefield.com/en/united-states/properties/for-sale/office/tx/dallas/example-s";
  const rotated = baseCushmanListing({ id: "provider-guid-b", url }, "sale");
  const original = baseCushmanListing({ id: "provider-guid-a", url }, "sale");

  assert.equal(original.id, rotated.id);
  assert.equal(original.id, cushmanCanonicalIdentity(url));
  assert.equal(original.providerId, "provider-guid-a");
  assert.equal(rotated.providerId, "provider-guid-b");
  assert.equal(original.rawCushmanApi.id, "provider-guid-a");
  assert.equal(rotated.rawCushmanApi.id, "provider-guid-b");
});

test("Cushman identity normalizes legacy host, query, fragment, and trailing slash", () => {
  const path =
    "/en/united-states/properties/for-lease/industrial/oh/cleveland/example-l";
  const publicUrl = `https://www.cushmanwakefield.com${path}`;
  const legacyUrl = `https://sitecore-www.cushmanwakefield.com${path}/?tracking=1#top`;

  assert.equal(cushmanIdentityUrl(legacyUrl), publicUrl);
  assert.equal(
    cushmanCanonicalIdentity(legacyUrl),
    cushmanCanonicalIdentity(publicUrl)
  );
  assert.equal(cushmanCanonicalIdentity("https://example.com/property/1"), null);
});

test("Cushman identity maps the official Azure host and retains OneCap record identity", () => {
  const path =
    "/en/united-states/properties/for-sale/industrial/il/chicago/example-s";
  assert.equal(
    cushmanCanonicalIdentity(
      `https://cw-prod-gblgws-a-cm.azurewebsites.net${path}`
    ),
    cushmanCanonicalIdentity(`https://www.cushmanwakefield.com${path}`)
  );
  assert.notEqual(
    cushmanCanonicalIdentity(
      "https://onecap.cushmanwakefield.com/en/united-states/properties/for-sale/listing?recordId=a2N-A"
    ),
    cushmanCanonicalIdentity(
      "https://onecap.cushmanwakefield.com/en/united-states/properties/for-sale/listing?recordId=a2N-B"
    )
  );
  assert.equal(
    cushmanCanonicalIdentity(
      "https://onecap.cushmanwakefield.com/en/united-states/properties/for-sale/listing"
    ),
    null
  );
});

test("Cushman OneCap identity normalizes recordId whitespace and rejects duplicates", () => {
  const base =
    "https://onecap.cushmanwakefield.com/en/united-states/properties/for-sale/listing";
  assert.equal(
    cushmanCanonicalIdentity(`${base}?recordId=A+B`),
    cushmanCanonicalIdentity(`${base}?recordId=%20A%20%20B%20`)
  );
  assert.equal(
    cushmanCanonicalIdentity(`${base}?recordId=A&recordId=B`),
    null
  );
  assert.equal(
    cushmanCanonicalIdentity(`${base}?recordId=A&recordId=`),
    null
  );
  assert.equal(
    cushmanIdentityUrl(`${base}?recordId=A%7EB`),
    `${base}?recordId=A%7EB`
  );
});

test("API-base mode selection is explicit and monitor-safe", () => {
  const previousMode = process.env.CUSHMAN_DETAIL_MODE;
  try {
    delete process.env.CUSHMAN_DETAIL_MODE;
    assert.equal(cushmanUseBaseRows(false), false);
    assert.equal(cushmanUseBaseRows(true), true);
    process.env.CUSHMAN_DETAIL_MODE = "base";
    assert.equal(cushmanUseBaseRows(false), true);
  } finally {
    if (previousMode === undefined) delete process.env.CUSHMAN_DETAIL_MODE;
    else process.env.CUSHMAN_DETAIL_MODE = previousMode;
  }
});

test("API-base refresh rows preserve previously harvested child collections", () => {
  const listing = cushmanBaseRefreshListing(
    { id: "cw-base-1", url: "/en/properties/cw-base-1" },
    "sale",
    "2026-07-29T12:00:00Z",
    false
  );
  assert.equal(listing.preserveChildCollections, true);
  assert.equal(listing.freshnessProvenance.detailScope, "inventory_only");
});

test("strict API-base rows carry authoritative inventory provenance and preserve children", () => {
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  try {
    process.env.CRE_REFRESH_GENERATION = "cushman-generation-1";
    const listing = cushmanBaseRefreshListing(
      { id: "cw-strict-1", url: "/en/properties/cw-strict-1" },
      "sale",
      "2026-07-29T12:00:00Z",
      true
    );
    assert.equal(listing.inventoryObservedAt, "2026-07-29T12:00:00Z");
    assert.equal(listing.detailObservedAt, undefined);
    assert.equal(listing.preserveChildCollections, true);
    assert.deepEqual(listing.freshnessProvenance, {
      detailScope: "authoritative_inventory_feed",
      generationId: "cushman-generation-1",
      method: "cushman_search_api",
      cacheDisposition: "live",
    });
  } finally {
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
  }
});

test("strict freshness uses the authoritative API path and requires a generation", () => {
  const oldMode = process.env.CUSHMAN_DETAIL_MODE;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    delete process.env.CRE_REFRESH_GENERATION;
    assert.throws(
      () => assertCushmanFreshnessMode(false),
      /requires CRE_REFRESH_GENERATION/
    );
    process.env.CRE_REFRESH_GENERATION = "cushman-generation-1";
    process.env.CUSHMAN_DETAIL_MODE = "full";
    assert.doesNotThrow(() => assertCushmanFreshnessMode(false));
    assert.equal(cushmanUseBaseRows(false), true);
    assert.doesNotThrow(() => assertCushmanFreshnessMode(true));
  } finally {
    if (oldMode === undefined) delete process.env.CUSHMAN_DETAIL_MODE;
    else process.env.CUSHMAN_DETAIL_MODE = oldMode;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
  }
});

test("Cushman detail admission rejects challenge, error, and wrong-property shells", () => {
  const base = {
    id: "cw-123",
    name: "Dallas Midtown Tower",
    street: "100 Main Street",
    url: `${CUSHMAN_HOST}/en/united-states/properties/lease/dallas-midtown-tower`,
  };
  assert.throws(
    () =>
      assertCushmanDetailDoc(
        {
          rawHtml: "<html><title>Just a moment...</title><div id='cf-chl-widget'></div></html>",
          markdown: "Checking your browser before accessing Cushman & Wakefield",
          links: [],
          metadata: { statusCode: 200 },
        },
        base
      ),
    /challenge or error shell/
  );
  assert.throws(
    () =>
      assertCushmanDetailDoc(
        {
          rawHtml: "<html><h1>Page not found</h1></html>",
          markdown: "404 - Page not found",
          links: [],
          metadata: { statusCode: 404 },
        },
        base
      ),
    /HTTP 404/
  );
  assert.throws(
    () =>
      assertCushmanDetailDoc(
        {
          rawHtml: "<html><h1>Chicago Industrial Center</h1></html>",
          markdown: "Chicago Industrial Center",
          links: [],
          metadata: {
            statusCode: 200,
            sourceURL:
              `${CUSHMAN_HOST}/en/united-states/properties/lease/chicago-industrial-center`,
          },
        },
        base
      ),
    /identity does not match/
  );
});

test("Cushman detail admission accepts matching listing structure", () => {
  const base = {
    id: "cw-123",
    name: "Dallas Midtown Tower",
    street: "100 Main Street",
    url: `${CUSHMAN_HOST}/en/united-states/properties/lease/dallas-midtown-tower`,
  };
  const listing = assertCushmanDetailDoc(
    {
      rawHtml: `
        <html><h1>Dallas Midtown Tower</h1>
        <script type="application/ld+json">
          {"@type":"RealEstateListing","name":"Dallas Midtown Tower",
           "url":"/en/united-states/properties/lease/dallas-midtown-tower"}
        </script></html>`,
      markdown: "# Dallas Midtown Tower\n100 Main Street",
      links: [],
      metadata: { statusCode: 200 },
    },
    base
  );
  assert.equal(listing?.name, "Dallas Midtown Tower");
});

test("Cushman rejected detail shells never stamp freshness or empty children", async () => {
  const oldScrape = firecrawl.scrape;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  (firecrawl as any).scrape = async () => ({
    rawHtml: "<html><title>Just a moment...</title><div id='cf-chl-widget'></div></html>",
    markdown: "Checking your browser",
    links: [],
    metadata: { statusCode: 200 },
  });
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    const listing = await enrichCushmanListing(
      {
        id: "cw-123",
        nav_title: "Dallas Midtown Tower",
        url: "/en/united-states/properties/lease/dallas-midtown-tower",
      },
      "lease",
      "2026-07-29T12:00:00Z"
    );
    assert.match(listing.detailError, /challenge or error shell/);
    assert.equal(listing.detailObservedAt, undefined);
    assert.equal(listing.contactsDetailed, undefined);
    assert.equal(listing.documents, undefined);
  } finally {
    (firecrawl as any).scrape = oldScrape;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("strict Cushman authoritative inventory bypasses cache and never renders details", async () => {
  const oldScrape = firecrawl.scrape;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const oldMode = process.env.CUSHMAN_DETAIL_MODE;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  const calls: any[] = [];
  (firecrawl as any).scrape = async (url: string, options: any) => {
    calls.push(options);
    if (url.includes("/api/properties/search")) {
      return {
        rawHtml: JSON.stringify({
          total_item: 1,
          content: [
            {
              id: "cw-strict-1",
              url: "/en/united-states/properties/invest/cw-strict-1",
              nav_title: "Strict Property",
              property_street: "100 Main Street",
              property_city: "Dallas",
              state_or_province: "TX",
            },
          ],
        }),
      };
    }
    return {
      rawHtml: "<html><h1>Strict Property</h1></html>",
      markdown: "Strict Property",
      links: [],
    };
  };
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "cushman-generation-1";
    process.env.CUSHMAN_DETAIL_MODE = "full";
    const result = await srcCushman("sale", 1, false);
    assert.equal(result.listings.length, 1);
    assert.equal(calls.length, 1);
    assert.ok(calls.every((options) => options.maxAge === 0));
    assert.equal(
      result.listings[0].freshnessProvenance.detailScope,
      "authoritative_inventory_feed"
    );
    assert.equal(
      result.listings[0].freshnessProvenance.generationId,
      "cushman-generation-1"
    );
    assert.equal(result.listings[0].preserveChildCollections, true);
    assert.equal(result.listings[0].detailObservedAt, undefined);
  } finally {
    (firecrawl as any).scrape = oldScrape;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
    if (oldMode === undefined) delete process.env.CUSHMAN_DETAIL_MODE;
    else process.env.CUSHMAN_DETAIL_MODE = oldMode;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
  }
});

test("strict Cushman inventory validates every page shape, total, cardinality, and identity", () => {
  const firstRows = Array.from({ length: 100 }, (_, index) => ({
    id: `cw-${index}`,
    url: `/en/united-states/properties/invest/cw-${index}`,
  }));
  const first = assertCushmanInventoryPage(
    { total_item: 101, content: firstRows },
    0,
    null,
    true
  );
  assert.equal(first.total, 101);
  assert.doesNotThrow(() =>
    assertCushmanInventoryPage(
      {
        total_item: 101,
        content: [
          {
            id: "cw-100",
            url: "/en/united-states/properties/invest/cw-100",
          },
        ],
      },
      100,
      first,
      true
    )
  );

  assert.throws(
    () => assertCushmanInventoryPage({ total_item: 1 }, 0, null, true),
    /requires a content array/
  );
  for (const total of [Number.NaN, -1, 1.5, "not-a-number"]) {
    assert.throws(
      () =>
        assertCushmanInventoryPage(
          { total_item: total, content: [] },
          0,
          null,
          true
        ),
      /valid integer total/
    );
  }
  assert.throws(
    () =>
      assertCushmanInventoryPage(
        { total_item: 102, content: [{ id: "cw-100" }, { id: "cw-101" }] },
        100,
        first,
        true
      ),
    /total changed/
  );
  assert.throws(
    () =>
      assertCushmanInventoryPage(
        { total_item: 101, content: [] },
        100,
        first,
        true
      ),
    /expected 1 rows/
  );
  assert.throws(
    () =>
      assertCushmanInventoryPage(
        { total_item: 1, content: [{ url: "/property/missing-id" }] },
        0,
        null,
        true
      ),
    /requires a nonempty provider id/
  );
  assert.throws(
    () =>
      assertCushmanInventoryPage(
        {
          total_item: 2,
          content: [
            {
              id: "duplicate",
              url: "/en/united-states/properties/invest/duplicate-a",
            },
            {
              id: "duplicate",
              url: "/en/united-states/properties/invest/duplicate-b",
            },
          ],
        },
        0,
        null,
        true
      ),
    /duplicate provider identity/
  );
  for (const row of [
    { id: "missing-url" },
    { id: "wrong-host", url: "https://example.com/property/1" },
    {
      id: "onecap-without-record",
      url: "https://onecap.cushmanwakefield.com/en/united-states/properties/listing",
    },
  ]) {
    assert.throws(
      () =>
        assertCushmanInventoryPage(
          { total_item: 1, content: [row] },
          0,
          null,
          true
        ),
      /requires a canonical URL-v1 identity/
    );
  }
});

test("strict Cushman inventory rejects repeated pages and requires exact aggregate coverage", () => {
  assert.throws(
    () =>
      assertCushmanInventoryReconciled(
        [
          {
            id: "cw-1",
            url: "/en/united-states/properties/invest/cw-1",
          },
          {
            id: "cw-1",
            url: "/en/united-states/properties/invest/cw-1-repeat",
          },
        ],
        2,
        true
      ),
    /duplicate provider identity/
  );
  assert.throws(
    () =>
      assertCushmanInventoryReconciled(
        [
          {
            id: "cw-1",
            url: "/en/united-states/properties/invest/cw-1",
          },
        ],
        2,
        true
      ),
    /expected 2 unique rows/
  );
  assert.doesNotThrow(() =>
    assertCushmanInventoryReconciled(
      [
        {
          id: "cw-1",
          url: "/en/united-states/properties/invest/cw-1",
        },
        {
          id: "cw-2",
          url: "/en/united-states/properties/invest/cw-2",
        },
      ],
      2,
      true
    )
  );
  assert.throws(
    () =>
      assertCushmanInventoryReconciled(
        [{ id: "cw-1", url: "https://example.com/property/1" }],
        1,
        true
      ),
    /requires a canonical URL-v1 identity/
  );
  assert.doesNotThrow(() =>
    assertCushmanInventoryPage(
      { total_item: "bad", content: [{ url: "/legacy-fallback" }] },
      0,
      null,
      false
    )
  );
});

test("strict Cushman collection fully enumerates before applying a finite output cap", async () => {
  const oldScrape = firecrawl.scrape;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const oldMode = process.env.CUSHMAN_DETAIL_MODE;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  const inventoryCalls: string[] = [];
  const firstRows = Array.from({ length: 100 }, (_, index) => ({
    id: `cw-${index}`,
    url: `/en/united-states/properties/invest/cw-${index}`,
    nav_title: `Property ${index}`,
  }));
  (firecrawl as any).scrape = async (url: string) => {
    if (!url.includes("/api/properties/search")) {
      throw new Error("detail fanout must not begin before inventory reconciliation");
    }
    inventoryCalls.push(url);
    const offset = Number(new URL(url).searchParams.get("offset"));
    return {
      rawHtml: JSON.stringify({
        total_item: 101,
        content: offset === 0 ? firstRows : [{ ...firstRows[0] }],
      }),
    };
  };
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "cushman-generation-1";
    process.env.CUSHMAN_DETAIL_MODE = "full";
    await assert.rejects(
      () => srcCushman("sale", 1, false),
      /duplicate provider identity/
    );
    assert.equal(inventoryCalls.length, 2);
  } finally {
    (firecrawl as any).scrape = oldScrape;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
    if (oldMode === undefined) delete process.env.CUSHMAN_DETAIL_MODE;
    else process.env.CUSHMAN_DETAIL_MODE = oldMode;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
  }
});

test("strict Cushman collection admits exact complete API coverage before capping output", async () => {
  const oldScrape = firecrawl.scrape;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const oldMode = process.env.CUSHMAN_DETAIL_MODE;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  const inventoryCalls: string[] = [];
  const firstRows = Array.from({ length: 100 }, (_, index) => ({
    id: `cw-${index}`,
    url: `/en/united-states/properties/invest/cw-${index}`,
    nav_title: `Property ${index}`,
  }));
  (firecrawl as any).scrape = async (url: string, options: any) => {
    assert.equal(options.maxAge, 0);
    assert.ok(url.includes("/api/properties/search"));
    inventoryCalls.push(url);
    const offset = Number(new URL(url).searchParams.get("offset"));
    return {
      rawHtml: JSON.stringify({
        total_item: 101,
        content:
          offset === 0
            ? firstRows
            : [
                {
                  id: "cw-100",
                  url: "/en/united-states/properties/invest/cw-100",
                  nav_title: "Property 100",
                },
              ],
      }),
    };
  };
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "cushman-generation-1";
    process.env.CUSHMAN_DETAIL_MODE = "full";
    const result = await srcCushman("sale", 1, false);
    assert.equal(inventoryCalls.length, 2);
    assert.equal(result.totalAvailable, 101);
    assert.equal(result.listings.length, 1);
    assert.equal(result.truncated, true);
    assert.match(result.method, /authoritative inventory feed/);
  } finally {
    (firecrawl as any).scrape = oldScrape;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
    if (oldMode === undefined) delete process.env.CUSHMAN_DETAIL_MODE;
    else process.env.CUSHMAN_DETAIL_MODE = oldMode;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
  }
});

test("non-strict Cushman finite output caps report provider inventory truncation", async () => {
  const oldScrape = firecrawl.scrape;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const inventoryRows = [
    {
      id: "cw-1",
      url: "/en/united-states/properties/invest/cw-1",
      nav_title: "Property 1",
    },
    {
      id: "cw-2",
      url: "/en/united-states/properties/invest/cw-2",
      nav_title: "Property 2",
    },
  ];
  (firecrawl as any).scrape = async (url: string) => {
    assert.ok(url.includes("/api/properties/search"));
    return {
      rawHtml: JSON.stringify({
        total_item: inventoryRows.length,
        content: inventoryRows,
      }),
    };
  };
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "0";
    const result = await srcCushman("sale", 1, true);
    assert.equal(result.totalAvailable, 2);
    assert.equal(result.listings.length, 1);
    assert.equal(result.truncated, true);
  } finally {
    (firecrawl as any).scrape = oldScrape;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("baseCushmanListing uses sale defaults on the sale pass", () => {
  const listing = baseCushmanListing(
    {
      id: "cw-sale",
      property_street: "500 Commerce St",
      url: "/en/united-states/properties/invest/500-commerce",
    },
    "sale"
  );
  assert.equal(listing.transactionType, "Sale");
  assert.equal(listing.salePriceText, "Contact broker for pricing");
});

// ---------------------------------------------------------------------------
// Phase-2 WS1 scalar lift tests: fixture-driven (tests/fixtures/raw_data/cushman-wakefield.json)
// ---------------------------------------------------------------------------

test("fixture row 0: canonicalUrl and statusBadge are set from base API fields", () => {
  const row = fixtureRows[0]!.rawCushmanApi;
  const listing = baseCushmanListing(row, "lease");
  // canonicalUrl must be the resolved absolute URL (no sitecore- prefix)
  assert.ok(listing.canonicalUrl != null, "canonicalUrl should be set");
  assert.ok(
    (listing.canonicalUrl as string).startsWith("https://www.cushmanwakefield.com"),
    "canonicalUrl must use www host"
  );
  assert.ok(
    !(listing.canonicalUrl as string).includes("sitecore-"),
    "canonicalUrl must not include sitecore- prefix"
  );
  // statusBadge equals listingStatus
  assert.equal(listing.statusBadge, listing.listingStatus, "statusBadge must equal listingStatus");
  assert.equal(listing.listingStatus, "Available");
});

test("fixture row 0: extraFacts sublease badge is detected from attribute1 'For Sublease'", () => {
  const row = fixtureRows[0]!.rawCushmanApi;
  const listing = baseCushmanListing(row, "lease");
  assert.ok(listing.extraFacts != null, "extraFacts should be present when sublease headline");
  assert.equal((listing.extraFacts as Record<string, unknown>)["sublease"], true);
  // is_investment_property absent in this row
  assert.equal((listing.extraFacts as Record<string, unknown>)["is_investment_property"], undefined);
});

test("fixture row 1: is_investment_property is captured in extraFacts", () => {
  const row = fixtureRows[1]!.rawCushmanApi;
  // fixture row 1 has is_investment_property: true in rawCushmanApi
  const listing = baseCushmanListing(row, "lease");
  assert.ok(listing.extraFacts != null, "extraFacts should be present");
  assert.equal((listing.extraFacts as Record<string, unknown>)["is_investment_property"], true);
  // headline is 'For Lease', not sublease
  assert.equal((listing.extraFacts as Record<string, unknown>)["sublease"], undefined);
});

test("fixture row 2: extraFacts is absent when neither sublease nor is_investment_property", () => {
  const row = fixtureRows[2]!.rawCushmanApi;
  // row 2 has no is_investment_property and headline 'For Lease'
  const listing = baseCushmanListing(row, "lease");
  // prune() will have removed it if undefined; check it is null or undefined
  assert.ok(
    listing.extraFacts == null,
    "extraFacts should be absent when no sublease or investment flag"
  );
});

test("parseLeaseRate: (Annual) form yields null min (negative signal guard)", () => {
  // The frozen parser's hasNegativeSignal returns true for '(Annual)' without /SF.
  // Cushman's canonical form '$30.00 (Annual) USD' is therefore not per-SF trustable.
  const result = parseLeaseRate("$30.00 (Annual) USD");
  assert.equal(result.min, null, "Annual form without /SF must yield null min");
  assert.equal(result.max, null);
  assert.equal(result.type, null);
});

test("parseLeaseRate: per-SF form '4.50/SF USD' is parsed to a usable min", () => {
  // From real Cushman data: '4.50/SF USD' has a /SF signal but no $ prefix.
  // The parser uses Strategy 4 (single bare number) after detecting hasPerSfSignal.
  const result = parseLeaseRate("4.50/SF USD");
  assert.equal(result.min, 4.5, "per-SF bare number must be extracted as min");
  assert.equal(result.max, null);
  assert.equal(result.type, null);
});

test("parseLeaseRate: range per-SF form '16-18/SF USD' yields min=16, max=18", () => {
  const result = parseLeaseRate("16-18/SF USD");
  assert.equal(result.min, 16);
  assert.equal(result.max, 18);
  assert.equal(result.type, null);
});

test("parseLeaseRate: 'Contact us for pricing' yields all-null result without throwing", () => {
  const result = parseLeaseRate("Contact us for pricing");
  assert.equal(result.min, null);
  assert.equal(result.max, null);
  assert.equal(result.type, null);
});

test("parseLeaseRate: '$24.00/SF/YR, FSG' yields full_service type", () => {
  const result = parseLeaseRate("$24.00/SF/YR, FSG");
  assert.equal(result.min, 24);
  assert.equal(result.type, "full_service");
});

test("baseCushmanExtraFacts: 'For Sublease' headline produces sublease=true", () => {
  const ef = baseCushmanExtraFacts({ attribute1: "For Sublease" });
  assert.deepEqual(ef, { sublease: true });
});

test("baseCushmanExtraFacts: is_investment_property flag is captured", () => {
  const ef = baseCushmanExtraFacts({ attribute1: "For Lease", is_investment_property: true });
  assert.deepEqual(ef, { is_investment_property: true });
});

test("baseCushmanExtraFacts: both flags together", () => {
  const ef = baseCushmanExtraFacts({ attribute1: "For Sublease", is_investment_property: true });
  assert.deepEqual(ef, { sublease: true, is_investment_property: true });
});

test("baseCushmanExtraFacts: returns undefined when no interesting fields", () => {
  assert.equal(baseCushmanExtraFacts({ attribute1: "For Lease" }), undefined);
  assert.equal(baseCushmanExtraFacts({}), undefined);
  assert.equal(baseCushmanExtraFacts({ attribute1: null }), undefined);
});

test("baseCushmanListing: canonicalUrl is set when url is relative", () => {
  const listing = baseCushmanListing(
    { id: "cw-rel", url: "/en/united-states/properties/lease/test-prop" },
    "lease"
  );
  assert.ok(
    (listing.canonicalUrl as string)?.startsWith("https://www.cushmanwakefield.com"),
    "canonicalUrl should be absolute"
  );
  assert.equal(listing.canonicalUrl, listing.url, "canonicalUrl and url should match");
});

test("baseCushmanListing: canonicalUrl is null when no url provided", () => {
  const listing = baseCushmanListing({ id: "cw-nourl" }, "lease");
  assert.equal(listing.canonicalUrl, null);
  assert.equal(listing.url, null);
});

test("baseCushmanListing: statusBadge is null when listing_status absent", () => {
  const listing = baseCushmanListing(
    { id: "cw-nostatus", url: "/en/united-states/properties/lease/test" },
    "lease"
  );
  // prune() removes null/undefined; statusBadge is absent
  assert.ok(!("statusBadge" in listing) || listing.statusBadge == null);
});

test("fixture rows: baseCushmanListing never throws on any fixture row", () => {
  for (const fixtureRow of fixtureRows) {
    const rawRow = fixtureRow.rawCushmanApi ?? fixtureRow;
    assert.doesNotThrow(() => baseCushmanListing(rawRow, "lease"));
  }
});

test("fixture rows: contactsDetailed title is passed through when present", () => {
  // Row 0 has a contact with title 'Executive Director'
  const contacts = fixtureRows[0]!.contactsDetailed as any[];
  const withTitle = contacts.find((c: any) => c.title);
  assert.ok(withTitle != null, "At least one contact should have a title");
  assert.equal(withTitle.title, "Executive Director");
  // license is absent in Cushman contacts (not published publicly)
  assert.equal(withTitle.license, undefined);
});
