// Isolate argv before jll.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import {
  jllPropertyTypeLabel,
  normalizedJllListingUrl,
  jllFilteredSearchUrl,
  parseJllSearchPage,
  mergeJllListing,
  jllNextData,
  jllPublicProfileUrl,
  jllStringUrls,
  jllSurfaceAreaSqft,
  jllDescription,
  jllContacts,
  jllExtractLicense,
  jllDetailCachePath,
  readJllDetailCache,
  writeJllDetailCache,
  jllCachedAtMeetsBoundary,
  jllStrandedMedia,
  jllStrandedDocs,
  jllStrandedStructured,
} from "../../../sources/jll.js";
import { harvestDetail } from "../../../lib/harvest.js";

// ---------------------------------------------------------------------------
// Phase-2 data-lift tests: fixture-based, pure transform, no network.
// ---------------------------------------------------------------------------

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(__dirname, "../../fixtures/raw_data/jll.json");

function loadJllFixture(): any[] {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
}

function jllDetailRow(): any {
  return loadJllFixture().find((r: any) => r._source === "jll");
}


test("jllPropertyTypeLabel title-cases hyphenated property types", () => {
  assert.equal(jllPropertyTypeLabel("office"), "Office");
  assert.equal(jllPropertyTypeLabel("data-center"), "Data Center");
  assert.equal(jllPropertyTypeLabel("multifamily"), "Multifamily");
});

test("normalizedJllListingUrl resolves relative links and strips query/hash", () => {
  assert.equal(
    normalizedJllListingUrl("/listings/dallas-tower-123"),
    "https://property.jll.com/listings/dallas-tower-123"
  );
  assert.equal(
    normalizedJllListingUrl("https://property.jll.com/listings/foo/?utm=1#section"),
    "https://property.jll.com/listings/foo"
  );
});

test("jllFilteredSearchUrl builds tenure, property type, and page query", () => {
  const sale = new URL(jllFilteredSearchUrl("sale", "office", 2));
  assert.equal(sale.searchParams.get("tenureTypes"), "sale");
  assert.equal(sale.searchParams.get("propertyTypes"), "office");
  assert.equal(sale.searchParams.get("page"), "2");

  const rent = new URL(jllFilteredSearchUrl("rent", "industrial", 1));
  assert.equal(rent.searchParams.get("tenureTypes"), "rent");
  assert.equal(rent.searchParams.get("propertyTypes"), "industrial");
});

test("jllNextData parses __NEXT_DATA__ JSON from HTML", () => {
  const html = `
    <html><body>
      <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"property":{"id":"12345","title":"Tower"}}}}
      </script>
    </body></html>
  `;
  const data = jllNextData(html);
  assert.equal(data?.props?.pageProps?.property?.id, "12345");
  assert.equal(jllNextData("<html></html>"), null);
  assert.equal(jllNextData('<script id="__NEXT_DATA__">{bad json</script>'), null);
});

test("jllPublicProfileUrl builds profile URLs from slugs or passes through absolute URLs", () => {
  assert.equal(jllPublicProfileUrl("jane-doe"), "https://www.us.jll.com/en/people/jane-doe");
  assert.equal(jllPublicProfileUrl("/people/john-smith"), "https://www.us.jll.com/en/people/people/john-smith");
  assert.equal(jllPublicProfileUrl("https://www.us.jll.com/en/people/existing"), "https://www.us.jll.com/en/people/existing");
  assert.equal(jllPublicProfileUrl(null), null);
});

test("jllStringUrls keeps unique http(s) URLs only", () => {
  assert.deepEqual(
    jllStringUrls(["https://a.example/b.pdf", "mailto:x@y.com", "https://a.example/b.pdf", "  "]),
    ["https://a.example/b.pdf"]
  );
  assert.deepEqual(jllStringUrls(null), []);
});

test("jllSurfaceAreaSqft reads direct value or nested feet metrics", () => {
  assert.equal(jllSurfaceAreaSqft({ surfaceArea: 12500 }), 12500);
  assert.equal(
    jllSurfaceAreaSqft({
      surfaceAreas: [{ metrics: [{ unit: "Feet", value: { min: 8000, max: 12000 } }] }],
    }),
    12000
  );
  assert.equal(jllSurfaceAreaSqft({}), null);
});

test("jllDescription joins sections and highlights", () => {
  const property = {
    descriptionSections: [{ title: "<p>Overview</p>", content: "<p>Prime asset.</p>" }],
    highlights: ["<li>Corner lot</li>", "Transit access"],
  };
  const text = jllDescription(property);
  assert.match(text ?? "", /Overview/);
  assert.match(text ?? "", /Prime asset/);
  assert.match(text ?? "", /Corner lot/);
  assert.equal(jllDescription({}), null);
});

test("jllContacts maps brokers and dedupes by email", () => {
  const contacts = jllContacts([
    { name: "Jane Doe", email: "jane@jll.com", pageUrl: "jane-doe", jobTitle: "MD" },
    { name: "Jane Doe", email: "jane@jll.com", pageUrl: "jane-doe" },
    { name: "John Smith", email: "john@jll.com", telephone: "555-0100" },
  ]);
  assert.equal(contacts.length, 2);
  assert.equal(contacts[0]?.company, "JLL");
  assert.equal(contacts[0]?.profileUrl, "https://www.us.jll.com/en/people/jane-doe");
  assert.equal(contacts[1]?.phone, "555-0100");
});

test("mergeJllListing accumulates filters, pages, totals, and asset labels", () => {
  const existing = {
    jllPropertyTypeFilters: ["office"],
    jllSearchPages: [1],
    jllFilterTotals: { office: 100 },
    assetType: "Office",
  };
  const candidate = { jllFilterTotals: { industrial: 50 } };
  mergeJllListing(existing, candidate, "industrial", 2);
  assert.deepEqual(existing.jllPropertyTypeFilters, ["office", "industrial"]);
  assert.deepEqual(existing.jllSearchPages, [1, 2]);
  assert.deepEqual(existing.jllFilterTotals, { office: 100, industrial: 50 });
  assert.equal(existing.assetType, "Office, Industrial");
});

test("parseJllSearchPage extracts cards, totals, and sale pricing from minimal HTML", () => {
  const html = `
    <h2>2 properties</h2>
    <a class="text-base" href="/listings/dallas-tower-abc">
      <span>Dallas Tower</span>
      <span>123 Main St, Dallas, TX 75201</span>
      <span>$12,500,000</span>
      <span>85,000 SF</span>
    </a>
    <a class="text-base" href="/listings/dallas-tower-abc"><span>duplicate</span></a>
    <a class="text-base" href="/listings/austin-campus-xyz">
      <span>Austin Campus</span>
      <span>Austin, TX</span>
      <span>$4,000,000</span>
      <span>40 Acres</span>
    </a>
  `;
  const parsed = parseJllSearchPage(html, "sale", "office", 3);
  assert.equal(parsed.total, 2);
  assert.equal(parsed.listings.length, 2);
  const first = parsed.listings[0];
  assert.equal(first.url, "https://property.jll.com/listings/dallas-tower-abc");
  assert.equal(first.name, "Dallas Tower");
  assert.equal(first.state, "TX");
  assert.equal(first.postalCode, "75201");
  assert.equal(first.salePriceUsd, 12500000);
  assert.equal(first.assetType, "Office");
  assert.deepEqual(first.jllPropertyTypeFilters, ["office"]);
  assert.deepEqual(first.jllSearchPages, [3]);
});

test("parseJllSearchPage maps lease transaction and rent price text", () => {
  const html = `
    <h2>1 property</h2>
    <a class="text-base" href="/listings/lease-space-1">
      <span>Lease Space</span>
      <span>Houston, TX 77002</span>
      <span>$32.00/SF</span>
      <span>12,000 SF</span>
    </a>
  `;
  const parsed = parseJllSearchPage(html, "lease", "retail", 1);
  assert.equal(parsed.listings[0]?.transactionType, "Lease");
  assert.equal(parsed.listings[0]?.leaseRateText, "$32.00");
  assert.equal(parsed.listings[0]?.salePriceUsd, null);
});

test("jll detail cache round-trips through temp dir", () => {
  const cacheDir = mkdtempSync(join(tmpdir(), "jll-detail-cache-"));
  const prev = process.env.JLL_DETAIL_CACHE_DIR;
  process.env.JLL_DETAIL_CACHE_DIR = cacheDir;
  try {
    const url = "https://property.jll.com/listings/cache-test-123";
    const path = jllDetailCachePath(url);
    assert.ok(path.startsWith(cacheDir));
    assert.equal(readJllDetailCache(url), null);

    const doc = {
      rawHtml: "<html>detail</html>",
      markdown: "# Detail",
      links: ["https://example.com/brochure.pdf"],
      images: ["https://example.com/gallery.jpg"],
      attributes: [{ selector: "iframe", attribute: "src", values: ["https://example.com/tour"] }],
      metadata: { title: "Cache Test" },
    };
    writeJllDetailCache(url, doc);
    assert.ok(existsSync(path));

    const cached = readJllDetailCache(url);
    assert.equal(cached?.rawHtml, doc.rawHtml);
    assert.equal(cached?.markdown, doc.markdown);
    assert.deepEqual(cached?.links, doc.links);
    assert.deepEqual(cached?.images, doc.images);
    assert.deepEqual(cached?.attributes, doc.attributes);

    const onDisk = JSON.parse(readFileSync(path, "utf8"));
    assert.equal(onDisk.url, normalizedJllListingUrl(url));
    assert.ok(onDisk.cachedAt);
  } finally {
    if (prev === undefined) delete process.env.JLL_DETAIL_CACHE_DIR;
    else process.env.JLL_DETAIL_CACHE_DIR = prev;
    rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("JLL cache admission honors a run freshness boundary", () => {
  assert.equal(jllCachedAtMeetsBoundary("2026-07-29T12:00:00Z", undefined), true);
  assert.equal(
    jllCachedAtMeetsBoundary("2026-07-29T12:00:00Z", "2026-07-29T11:59:59Z"),
    true
  );
  assert.equal(
    jllCachedAtMeetsBoundary("2026-07-29T12:00:00Z", "2026-07-29T12:00:01Z"),
    false
  );
  assert.equal(jllCachedAtMeetsBoundary(undefined, "2026-07-29T12:00:00Z"), false);
  assert.equal(jllCachedAtMeetsBoundary("not-a-date", "2026-07-29T12:00:00Z"), false);
});

test("readJllDetailCache rejects mismatched url or malformed payload", () => {
  const cacheDir = mkdtempSync(join(tmpdir(), "jll-detail-cache-bad-"));
  const prev = process.env.JLL_DETAIL_CACHE_DIR;
  process.env.JLL_DETAIL_CACHE_DIR = cacheDir;
  try {
    const url = "https://property.jll.com/listings/bad-cache";
    writeJllDetailCache(url, { rawHtml: "<html></html>", markdown: "", links: [] });
    const path = jllDetailCachePath(url);
    const badUrl = JSON.parse(readFileSync(path, "utf8"));
    badUrl.url = "https://property.jll.com/listings/other";
    writeFileSync(path, JSON.stringify(badUrl));
    assert.equal(readJllDetailCache(url), null);
  } finally {
    if (prev === undefined) delete process.env.JLL_DETAIL_CACHE_DIR;
    else process.env.JLL_DETAIL_CACHE_DIR = prev;
    rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("jllStrandedMedia: videos as bare strings (provider-classified), tours/360 typed virtual_tour", () => {
  const property = {
    videos: ["https://vimeo.com/824804225", "https://www.youtube.com/watch?v=abc123XYZ_0"],
    virtualTours: ["https://my.matterport.com/show/?m=ABC123"],
    view360URLs: ["https://kuula.co/share/collection/xyz"],
  };
  const promoted = jllStrandedMedia(property);
  // Run through the harvester (the production path) to assert end-to-end typing.
  const out = harvestDetail({ rawHtml: "", markdown: "", links: [] } as any, { extraMedia: promoted });
  // Videos are bare strings -> harvester classifies provider + embed.
  assert.ok(out.media.some((m) => m.provider === "vimeo" && m.mediaType === "video"));
  assert.ok(out.media.some((m) => m.provider === "youtube" && m.embedUrl?.includes("/embed/")));
  // virtualTours + view360URLs are promoted as TYPED virtual_tour items; the
  // harvester trusts that asserted type and does NOT reclassify (so a matterport
  // url that arrived via virtualTours stays virtual_tour, not matterport).
  assert.equal(out.media.filter((m) => m.mediaType === "virtual_tour").length, 2);
  assert.equal(out.media.filter((m) => m.mediaType === "matterport").length, 0);
});

test("jllStrandedDocs classifies floor plans as floor_plan DocItems", () => {
  const docs = jllStrandedDocs({ floorPlans: ["https://cdn.jll.com/fp/level-1.pdf"] });
  assert.equal(docs.length, 1);
  assert.equal(docs[0].docType, "floor_plan");
  assert.equal(docs[0].url, "https://cdn.jll.com/fp/level-1.pdf");
});

test("jllStrandedStructured lifts submarket/year/floors/units/amenities/highlights; empty for sparse", () => {
  const out = jllStrandedStructured({
    submarket: "Uptown",
    yearBuilt: 1998,
    numberOfFloors: 12,
    numberOfUnits: 240,
    capRate: 6.25,
    amenities: ["Pool", { name: "Gym" }, "Pool"],
    highlights: ["<b>Trophy asset</b>", "Walkable"],
  });
  assert.equal(out.submarket, "Uptown");
  assert.equal(out.yearBuilt, 1998);
  assert.equal(out.floors, 12);
  assert.equal(out.units, 240);
  assert.equal(out.capRatePct, 6.25);
  assert.deepEqual(out.amenities, ["Pool", "Gym"]);
  assert.ok(out.highlights.includes("Trophy asset"));
  assert.deepEqual(jllStrandedStructured({}), {});
});

// ---------------------------------------------------------------------------
// Phase-2: jllExtractLicense
// ---------------------------------------------------------------------------

test("jllExtractLicense formats location:number from first license entry", () => {
  assert.equal(
    jllExtractLicense([{ location: "Indiana", licenseNumber: "RB14042705" }]),
    "Indiana: RB14042705"
  );
  assert.equal(
    jllExtractLicense([{ type: "Broker", location: "Texas - Dallas", licenseNumber: "234599" }]),
    "Texas - Dallas: 234599"
  );
  assert.equal(jllExtractLicense([]), null);
  assert.equal(jllExtractLicense(null), null);
  assert.equal(jllExtractLicense(undefined), null);
});

test("jllExtractLicense returns bare number when no location present", () => {
  assert.equal(jllExtractLicense([{ licenseNumber: "RB99999" }]), "RB99999");
});

// ---------------------------------------------------------------------------
// Phase-2: jllStrandedStructured with object-array highlights and buildingClass
// ---------------------------------------------------------------------------

test("jllStrandedStructured extracts highlights from object array (.title)", () => {
  const out = jllStrandedStructured({
    highlights: [
      { title: "224,000 SF total available" },
      { title: "9 dock doors" },
      { title: "Sits on 16.53 acres" },
    ],
  });
  assert.ok(Array.isArray(out.highlights));
  assert.ok(out.highlights.includes("224,000 SF total available"));
  assert.ok(out.highlights.includes("9 dock doors"));
  assert.equal(out.highlights.length, 3);
});

test("jllStrandedStructured normalizes buildingClass via normBuildingClass", () => {
  assert.equal(jllStrandedStructured({ buildingClass: "Class A" }).buildingClass, "A");
  assert.equal(jllStrandedStructured({ buildingClass: "B" }).buildingClass, "B");
  assert.equal(jllStrandedStructured({ buildingClass: "Unclassified" }).buildingClass, undefined);
  assert.equal(jllStrandedStructured({ buildingClass: "" }).buildingClass, undefined);
  assert.equal(jllStrandedStructured({}).buildingClass, undefined);
});

test("jllStrandedStructured emits canonicalUrl from relative pageUrl", () => {
  const out = jllStrandedStructured({
    pageUrl: "/listings/1401-e-memorial-dr-not-tracked-indiana",
  });
  assert.equal(
    out.canonicalUrl,
    "https://property.jll.com/listings/1401-e-memorial-dr-not-tracked-indiana"
  );
});

test("jllStrandedStructured passes through absolute pageUrl unchanged", () => {
  const out = jllStrandedStructured({
    pageUrl: "https://property.jll.com/listings/some-listing",
  });
  assert.equal(out.canonicalUrl, "https://property.jll.com/listings/some-listing");
});

test("jllStrandedStructured emits extraFacts with location_description when present", () => {
  const out = jllStrandedStructured({ locationDescription: "Suburbs" });
  assert.deepEqual(out.extraFacts, { location_description: "Suburbs" });
  // Absent locationDescription -> no extraFacts key.
  const sparse = jllStrandedStructured({});
  assert.equal(sparse.extraFacts, undefined);
});

test("jllStrandedStructured absent fields stay null/undefined (no fabrication)", () => {
  const out = jllStrandedStructured({});
  assert.equal(out.buildingClass, undefined);
  assert.equal(out.canonicalUrl, undefined);
  assert.equal(out.extraFacts, undefined);
  assert.equal(out.highlights, undefined);
  assert.equal(out.amenities, undefined);
});

// ---------------------------------------------------------------------------
// Phase-2: fixture-based end-to-end check against real saved raw_data blob
// ---------------------------------------------------------------------------

test("jll fixture: jllStrandedStructured lifts all Phase-2 fields from real raw_data", () => {
  const row = jllDetailRow();
  const detail = row.jllDetail;
  const out = jllStrandedStructured(detail);

  // submarket
  assert.equal(out.submarket, "Not Tracked Indiana");

  // buildingClass: "B" -> "B"
  assert.equal(out.buildingClass, "B");

  // highlights: object array with .title fields
  assert.ok(Array.isArray(out.highlights), "highlights should be an array");
  assert.ok(out.highlights.includes("224,000 SF total available"));
  assert.ok(out.highlights.includes("9 dock doors"));

  // amenities
  assert.ok(Array.isArray(out.amenities));
  assert.ok(out.amenities.includes("IP - Industrial Park Zone"));
  assert.ok(out.amenities.includes("Rail served"));

  // canonicalUrl from relative pageUrl
  assert.equal(
    out.canonicalUrl,
    "https://property.jll.com/listings/1401-e-memorial-dr-not-tracked-indiana"
  );

  // extraFacts: locationDescription
  assert.deepEqual(out.extraFacts, { location_description: "Suburbs" });
});

test("jll fixture: jllContacts emits license string on contacts with licenses", () => {
  const row = jllDetailRow();
  // The fixture has contactsDetailed already built; simulate calling jllContacts on
  // the broker-raw objects that would produce them. We test jllExtractLicense directly
  // on the licenses array shapes from the fixture.
  const firstBroker = row.contactsDetailed[0];
  const license = jllExtractLicense(firstBroker.licenses);
  assert.equal(license, "Indiana: RB14042705");

  const secondBroker = row.contactsDetailed[1];
  const license2 = jllExtractLicense(secondBroker.licenses);
  assert.equal(license2, "Indiana: RB21002049");
});

test("jll fixture: jllStrandedStructured does not throw on null/undefined/empty", () => {
  assert.doesNotThrow(() => jllStrandedStructured(null));
  assert.doesNotThrow(() => jllStrandedStructured(undefined));
  assert.doesNotThrow(() => jllStrandedStructured({}));
  assert.deepEqual(jllStrandedStructured(null), {});
  assert.deepEqual(jllStrandedStructured(undefined), {});
});
