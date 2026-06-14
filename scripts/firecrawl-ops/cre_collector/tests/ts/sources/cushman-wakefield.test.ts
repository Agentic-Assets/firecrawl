// Isolate argv before cushman-wakefield.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
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
} from "../../../sources/cushman-wakefield.js";

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
