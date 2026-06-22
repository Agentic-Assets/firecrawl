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
} from "../../../sources/cushman-wakefield.js";
import { parseLeaseRate } from "../../../lib/parse.js";

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
  assert.equal(listing.id, "cw-1001");
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
