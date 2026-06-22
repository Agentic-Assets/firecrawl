// Isolate argv before avison-young.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import type { ScrapedDoc } from "../../../types.js";
import {
  AVISON_YOUNG_CDN_BASE,
  AVISON_YOUNG_HOST,
  sharpLaunchCdnUrl,
  avisonYoungAbsoluteUrl,
  avisonYoungDetailLimit,
  isAvisonYoungPropertyPhoto,
  avisonYoungNameSlug,
  isAvisonYoungUsCompatible,
  avisonYoungTransactions,
  avisonYoungMatchesTx,
  avisonYoungTransactionType,
  avisonYoungSizeText,
  avisonYoungLeaseRateText,
  avisonYoungContact,
  extractAvisonYoungUrls,
  extractAvisonYoungDocuments,
  extractAvisonYoungPhotos,
  extractAvisonYoungJsonLd,
  extractAvisonYoungContactUrls,
  enrichAvisonYoungContacts,
  harvestAvisonYoung,
  avisonYoungLongestMarkdown,
  avisonYoungBaseListing,
} from "../../../sources/avison-young.js";

// ---------------------------------------------------------------------------
// Fixture helpers
// ---------------------------------------------------------------------------

const __filename = fileURLToPath(import.meta.url);
const __dir = dirname(__filename);
const FIXTURE_PATH = join(__dir, "../../fixtures/raw_data/avison-young.json");

/** Load the avison-young.json fixture (array of {external_id, rawSharpLaunch} blobs). */
function loadFixture(): Array<{ external_id: string; rawSharpLaunch: any }> {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
}

/** Empty team members map (no detail enrichment needed for pure scalar tests). */
function emptyTeam(): Map<string, any> {
  return new Map();
}

const DETAIL_LIMIT_ENV = "AVISON_YOUNG_DETAIL_LIMIT";

function clearDetailLimitEnv(): void {
  delete process.env[DETAIL_LIMIT_ENV];
}

function doc(rawHtml: string, links: string[] = []): ScrapedDoc {
  return { rawHtml, markdown: "", links };
}

test("sharpLaunchCdnUrl and avisonYoungAbsoluteUrl normalize CDN and site URLs", () => {
  assert.equal(sharpLaunchCdnUrl("website-42/hero.webp"), `${AVISON_YOUNG_CDN_BASE}/website-42/hero.webp`);
  assert.equal(sharpLaunchCdnUrl("https://cdn.example.com/already.jpg"), "https://cdn.example.com/already.jpg");
  assert.equal(sharpLaunchCdnUrl(null), null);
  assert.equal(
    avisonYoungAbsoluteUrl("/properties/dallas-tower", AVISON_YOUNG_HOST),
    `${AVISON_YOUNG_HOST}/properties/dallas-tower`
  );
  assert.equal(avisonYoungAbsoluteUrl("javascript:void(0)"), null);
  assert.equal(avisonYoungAbsoluteUrl("mailto:broker@example.com"), null);
});

test("avisonYoungDetailLimit honors env override and finite max", () => {
  clearDetailLimitEnv();
  assert.equal(avisonYoungDetailLimit(Number.POSITIVE_INFINITY, 12), 0);
  assert.equal(avisonYoungDetailLimit(50, 12), 12);

  process.env[DETAIL_LIMIT_ENV] = "3";
  assert.equal(avisonYoungDetailLimit(0, 12), 3);
  process.env[DETAIL_LIMIT_ENV] = "99";
  assert.equal(avisonYoungDetailLimit(50, 12), 12);
  clearDetailLimitEnv();
});

test("isAvisonYoungPropertyPhoto accepts listing photos and rejects avatars/logos", () => {
  const property = `${AVISON_YOUNG_CDN_BASE}/website-99/gallery/hero.webp`;
  const avatar = `${AVISON_YOUNG_CDN_BASE}/150x150/media/55/headshot.jpg`;
  const logo = `${AVISON_YOUNG_CDN_BASE}/website-99/ay_logo.png`;
  const media = `${AVISON_YOUNG_CDN_BASE}/media/55/brochure.jpg`;
  assert.equal(isAvisonYoungPropertyPhoto(property), true);
  assert.equal(isAvisonYoungPropertyPhoto(avatar), false);
  assert.equal(isAvisonYoungPropertyPhoto(logo), false);
  assert.equal(isAvisonYoungPropertyPhoto(media), false);
});

test("avisonYoungNameSlug slugifies contact names", () => {
  assert.equal(avisonYoungNameSlug("Jane Q. Public"), "jane-q-public");
  assert.equal(avisonYoungNameSlug("Renée López"), "renee-lopez");
  assert.equal(avisonYoungNameSlug(null), null);
});

test("isAvisonYoungUsCompatible accepts US country or two-letter state", () => {
  assert.equal(isAvisonYoungUsCompatible({ country: "United States" }), true);
  assert.equal(isAvisonYoungUsCompatible({ country: "Canada" }), false);
  assert.equal(isAvisonYoungUsCompatible({ state: "TX" }), true);
  assert.equal(isAvisonYoungUsCompatible({ state: "Texas" }), false);
});

test("avisonYoungTransactions and transaction matchers classify rows", () => {
  const dual = { transaction: ["Sale", "Lease"] };
  const sublease = { transaction: "Sublease" };
  assert.deepEqual(avisonYoungTransactions(dual), ["sale", "lease"]);
  assert.equal(avisonYoungMatchesTx(dual, "sale"), true);
  assert.equal(avisonYoungMatchesTx(dual, "lease"), true);
  assert.equal(avisonYoungMatchesTx(sublease, "lease"), true);
  assert.equal(avisonYoungTransactionType(dual), "Sale/Lease");
  assert.equal(avisonYoungTransactionType(sublease), "Sublease");
  assert.equal(avisonYoungTransactionType({ transaction: "Lease" }), "Lease");
});

test("avisonYoungSizeText and avisonYoungLeaseRateText format feed metrics", () => {
  assert.equal(
    avisonYoungSizeText({
      total_surface_sqft: 50000,
      availabilities_min_surface_sqft: 2500,
      availabilities_max_surface_sqft: 8000,
    }),
    "50000 SF total; 2500 - 8000 SF available"
  );
  assert.equal(
    avisonYoungLeaseRateText({
      availabilities_min_rent: 28,
      availabilities_max_rent: 34,
    }),
    "$28 - $34/SF/YR"
  );
  assert.equal(avisonYoungLeaseRateText({}), null);
});

test("avisonYoungContact maps SharpLaunch team members", () => {
  const contact = avisonYoungContact({
    first_name: "Sam",
    last_name: "Rivera",
    title: "Principal",
    email: "sam.rivera@example.com",
    phone: "214-555-0101",
    media_id: 77,
    company: "Avison Young",
  });
  assert.deepEqual(contact, {
    name: "Sam Rivera",
    title: "Principal",
    email: "sam.rivera@example.com",
    phone: "214-555-0101",
    company: "Avison Young",
    avatarUrl: `${AVISON_YOUNG_CDN_BASE}/media/77`,
  });
  assert.equal(avisonYoungContact(null), null);
});

test("extractAvisonYoungUrls dedupes absolute links from HTML and doc.links", () => {
  const pageUrl = `${AVISON_YOUNG_HOST}/properties/dallas-tower`;
  const scraped = doc(
    `<a href="/files/offering.pdf">PDF</a><img src="${AVISON_YOUNG_CDN_BASE}/website-12/hero.webp">`,
    [`${AVISON_YOUNG_HOST}/properties/dallas-tower#gallery`, "mailto:skip@example.com"]
  );
  const urls = extractAvisonYoungUrls(scraped, pageUrl);
  assert.ok(urls.includes(`${AVISON_YOUNG_HOST}/files/offering.pdf`));
  assert.ok(urls.includes(`${AVISON_YOUNG_CDN_BASE}/website-12/hero.webp`));
  assert.equal(
    urls.filter((url) => url.startsWith(`${AVISON_YOUNG_HOST}/properties/dallas-tower`)).length,
    1
  );
});

test("extractAvisonYoungDocuments keeps sharplaunch PDFs only", () => {
  const pdf = `${AVISON_YOUNG_CDN_BASE}/website-12/offering.pdf?token=abc`;
  const other = "https://example.com/brochure.pdf";
  const docs = extractAvisonYoungDocuments([
    {
      url: `${AVISON_YOUNG_HOST}/properties/dallas-tower`,
      doc: doc(`<a href="${pdf}">OM</a><a href="${other}">Other</a>`),
    },
  ]);
  assert.equal(docs.length, 1);
  assert.equal(docs[0].url, pdf);
  assert.equal(docs[0].name, "offering");
});

test("extractAvisonYoungPhotos merges detail photos with filtered fallback", () => {
  const detailPhoto = `${AVISON_YOUNG_CDN_BASE}/website-15/gallery/lobby.webp`;
  const feedPhoto = `${AVISON_YOUNG_CDN_BASE}/website-15/hero.webp`;
  const logo = `${AVISON_YOUNG_CDN_BASE}/website-15/ay_logo.png`;
  const photos = extractAvisonYoungPhotos(
    [
      {
        url: `${AVISON_YOUNG_HOST}/properties/austin-campus`,
        doc: doc(`<img src="${detailPhoto}">`),
      },
    ],
    [feedPhoto, logo]
  );
  assert.deepEqual(photos, [detailPhoto, feedPhoto]);
});

test("extractAvisonYoungJsonLd returns the first RealEstateListing block", () => {
  const listing = extractAvisonYoungJsonLd([
    {
      url: `${AVISON_YOUNG_HOST}/properties/one`,
      doc: doc(`<script type="application/ld+json">{"@type":"WebPage","name":"ignored"}</script>`),
    },
    {
      url: `${AVISON_YOUNG_HOST}/properties/two`,
      doc: doc(
        `<script type="application/ld+json">{"@type":"RealEstateListing","name":"Two Commerce","datePosted":"2026-01-15"}</script>`
      ),
    },
  ]);
  assert.equal(listing?.name, "Two Commerce");
  assert.equal(listing?.datePosted, "2026-01-15");
});

test("extractAvisonYoungContactUrls collects profile and vcard links", () => {
  const pageUrl = `${AVISON_YOUNG_HOST}/properties/dallas-tower`;
  const profile = `${AVISON_YOUNG_HOST}/professionals/-/ayp/view/jane-q-public`;
  const vcard = `${AVISON_YOUNG_HOST}/api/GetVCard?person=jane-q-public`;
  const { profileLinks, vcardLinks } = extractAvisonYoungContactUrls([
    {
      url: pageUrl,
      doc: doc(`
        <a href="${profile}">Jane Q. Public</a>
        <a href="${vcard}">VCard</a>
      `),
    },
  ]);
  assert.equal(profileLinks.length, 1);
  assert.equal(profileLinks[0].slug, "jane-q-public");
  assert.equal(profileLinks[0].text, "Jane Q. Public");
  assert.deepEqual(vcardLinks, [vcard]);
});

test("avisonYoungLongestMarkdown picks the longest non-empty page markdown", () => {
  assert.equal(
    avisonYoungLongestMarkdown([
      { url: "a", doc: { rawHtml: "", markdown: "short", links: [] } },
      { url: "b", doc: { rawHtml: "", markdown: "a much longer body of text", links: [] } },
    ]),
    "a much longer body of text"
  );
  assert.equal(avisonYoungLongestMarkdown([]), undefined);
  assert.equal(
    avisonYoungLongestMarkdown([{ url: "a", doc: { rawHtml: "", markdown: "", links: [] } }]),
    undefined
  );
});

test("harvestAvisonYoung unions media/links/documents/images across detail docs", () => {
  const pageUrl = `${AVISON_YOUNG_HOST}/properties/dallas-tower`;
  const docs = [
    {
      url: pageUrl,
      // Firecrawl's `links` format surfaces page anchors (vimeo tour, loopnet
      // syndication); the gallery img rides rawHtml + the passed image list.
      doc: doc(`<img src="${AVISON_YOUNG_CDN_BASE}/website-12/hero.webp">`, [
        "https://vimeo.com/824804225",
        "https://www.loopnet.com/Listing/123",
      ]),
    },
  ];
  const sharpLaunchDocs = [{ name: "offering", url: `${AVISON_YOUNG_CDN_BASE}/website-12/offering.pdf` }];
  const photos = [`${AVISON_YOUNG_CDN_BASE}/website-12/gallery/lobby.webp`];
  const ld = { video: "https://www.youtube.com/watch?v=abc123XYZ_0" };
  const out = harvestAvisonYoung(docs, sharpLaunchDocs, photos, ld);

  // vimeo anchor -> media (provider vimeo); JSON-LD youtube video -> media.
  assert.ok(out.media.some((m) => m.provider === "vimeo"));
  assert.ok(out.media.some((m) => m.provider === "youtube"));
  // loopnet anchor -> external_listing link.
  assert.ok(out.links.some((l) => l.linkType === "external_listing" && /loopnet/.test(l.url)));
  // SharpLaunch OM pdf -> document classified as om.
  assert.ok(
    out.documents.some((d) => /offering\.pdf$/.test(d.url) && (d.docType === "om" || d.docType === "other"))
  );
  // gallery photos kept (extraImages + page img), deduped.
  assert.ok(out.images.includes(`${AVISON_YOUNG_CDN_BASE}/website-12/gallery/lobby.webp`));
  assert.ok(out.images.includes(`${AVISON_YOUNG_CDN_BASE}/website-12/hero.webp`));
});

test("harvestAvisonYoung never throws on empty input", () => {
  const out = harvestAvisonYoung([], [], [], null);
  assert.deepEqual(out, { media: [], links: [], documents: [], images: [] });
});

test("enrichAvisonYoungContacts attaches profile and vcard URLs by slug", () => {
  const pageUrl = `${AVISON_YOUNG_HOST}/properties/dallas-tower`;
  const profile = `${AVISON_YOUNG_HOST}/professionals/-/ayp/view/sam-rivera`;
  const vcard = `${AVISON_YOUNG_HOST}/api/GetVCard?person=sam-rivera`;
  const enriched = enrichAvisonYoungContacts(
    [{ name: "Sam Rivera", phone: "214-555-0101", company: "Avison Young" }],
    [
      {
        url: pageUrl,
        doc: doc(`
          <a href="${profile}">Sam Rivera</a>
          <a href="${vcard}">VCard</a>
        `),
      },
    ]
  );
  assert.equal(enriched.length, 1);
  assert.equal(enriched[0].profileUrl, profile);
  assert.equal(enriched[0].vcardUrl, vcard);
  assert.equal(enriched[0].phone, "214-555-0101");
});

// ---------------------------------------------------------------------------
// Phase-2 Data-Lift: scalar field tests using saved rawSharpLaunch fixture
// ---------------------------------------------------------------------------

test("avisonYoungBaseListing emits canonicalUrl from external_url (Indianapolis lease row)", () => {
  const fixtures = loadFixture();
  const row = fixtures.find((f) => f.external_id === "17808")!.rawSharpLaunch;
  const listing = avisonYoungBaseListing(row, emptyTeam());
  // canonicalUrl must be the public AY properties page, not the SharpLaunch micro-site
  assert.equal(
    listing.canonicalUrl,
    "https://www.avisonyoung.us/properties/602-n-capitol-ave-indianapolis-lease"
  );
  // url field is also set (existing behavior preserved)
  assert.ok(listing.url);
});

test("avisonYoungBaseListing emits availableSf / minDivisibleSf / maxDivisibleSf from availabilities_*_surface_sqft", () => {
  const fixtures = loadFixture();
  // Row 17808: min=max=5547
  const rowA = fixtures.find((f) => f.external_id === "17808")!.rawSharpLaunch;
  const listingA = avisonYoungBaseListing(rowA, emptyTeam());
  assert.equal(listingA.availableSf, 5547);
  assert.equal(listingA.minDivisibleSf, 5547);
  assert.equal(listingA.maxDivisibleSf, 5547);

  // Row 17952: min=52435, max=127231
  const rowB = fixtures.find((f) => f.external_id === "17952")!.rawSharpLaunch;
  const listingB = avisonYoungBaseListing(rowB, emptyTeam());
  assert.equal(listingB.availableSf, 52435);
  assert.equal(listingB.minDivisibleSf, 52435);
  assert.equal(listingB.maxDivisibleSf, 127231);
});

test("avisonYoungBaseListing emits leaseRateMin/Max for normal rates and suppresses anomalous $7500/SF/YR rate", () => {
  const fixtures = loadFixture();

  // Row 17808: $18/SF/YR -> leaseRateMin=18; leaseRateMax pruned (null -> absent key)
  const rowA = fixtures.find((f) => f.external_id === "17808")!.rawSharpLaunch;
  const listingA = avisonYoungBaseListing(rowA, emptyTeam());
  assert.equal(listingA.leaseRateMin, 18);
  // prune() drops null values, so leaseRateMax is absent (undefined) when there is no range high
  assert.equal(listingA.leaseRateMax, undefined, "no range high -> prune drops leaseRateMax");

  // Row 17952: $4.95/SF/YR (same min and max in feed) -> leaseRateMin=4.95, leaseRateMax absent
  const rowB = fixtures.find((f) => f.external_id === "17952")!.rawSharpLaunch;
  const listingB = avisonYoungBaseListing(rowB, emptyTeam());
  assert.equal(listingB.leaseRateMin, 4.95);
  assert.equal(listingB.leaseRateMax, undefined, "identical min==max -> no range -> leaseRateMax absent");

  // Row 18150: $7500/SF/YR anomaly -> MUST be rejected (>500 $/SF/yr guard) -> both absent after prune
  const rowC = fixtures.find((f) => f.external_id === "18150")!.rawSharpLaunch;
  const listingC = avisonYoungBaseListing(rowC, emptyTeam());
  assert.equal(listingC.leaseRateMin, undefined, "AY $7500/SF/YR anomaly must be rejected by >500 guard");
  assert.equal(listingC.leaseRateMax, undefined, "AY $7500/SF/YR anomaly must be rejected by >500 guard");
});

test("avisonYoungBaseListing emits submarket from rawSharpLaunch.submarket", () => {
  const fixtures = loadFixture();
  const rowA = fixtures.find((f) => f.external_id === "17808")!.rawSharpLaunch;
  assert.equal(avisonYoungBaseListing(rowA, emptyTeam()).submarket, "CBD");

  const rowB = fixtures.find((f) => f.external_id === "17952")!.rawSharpLaunch;
  assert.equal(avisonYoungBaseListing(rowB, emptyTeam()).submarket, "Earth City");
});

test("avisonYoungBaseListing emits yearBuilt from rawSharpLaunch.yearbuilt", () => {
  const fixtures = loadFixture();
  // Row 18150 has yearbuilt=1982
  const row = fixtures.find((f) => f.external_id === "18150")!.rawSharpLaunch;
  assert.equal(avisonYoungBaseListing(row, emptyTeam()).yearBuilt, 1982);
  // Row 17808 has no yearbuilt -> yearBuilt is absent (prune drops null/undefined)
  const rowNoYear = fixtures.find((f) => f.external_id === "17808")!.rawSharpLaunch;
  assert.equal(avisonYoungBaseListing(rowNoYear, emptyTeam()).yearBuilt, undefined);
});

test("avisonYoungBaseListing emits units from rawSharpLaunch.units", () => {
  const fixtures = loadFixture();
  // Row 18150 has units=1
  const rowWithUnits = fixtures.find((f) => f.external_id === "18150")!.rawSharpLaunch;
  assert.equal(avisonYoungBaseListing(rowWithUnits, emptyTeam()).units, 1);
  // Row 17808 has no units -> units absent after prune
  const rowNoUnits = fixtures.find((f) => f.external_id === "17808")!.rawSharpLaunch;
  assert.equal(avisonYoungBaseListing(rowNoUnits, emptyTeam()).units, undefined);
});

test("avisonYoungBaseListing emits salePricePerSf from rawSharpLaunch.sale_unit_price", () => {
  const fixtures = loadFixture();
  // Row 18653 has sale_unit_price=2500000
  const rowSale = fixtures.find((f) => f.external_id === "18653")!.rawSharpLaunch;
  assert.equal(avisonYoungBaseListing(rowSale, emptyTeam()).salePricePerSf, 2500000);
  // Row 17808 has no sale_unit_price -> salePricePerSf absent after prune
  const rowLease = fixtures.find((f) => f.external_id === "17808")!.rawSharpLaunch;
  assert.equal(avisonYoungBaseListing(rowLease, emptyTeam()).salePricePerSf, undefined);
});

test("avisonYoungBaseListing emits propertySubtype as first rawSharpLaunch type token", () => {
  const fixtures = loadFixture();
  // Row 17808: type=["office.office_building","office.creative_loft"] -> first token
  const rowA = fixtures.find((f) => f.external_id === "17808")!.rawSharpLaunch;
  assert.equal(avisonYoungBaseListing(rowA, emptyTeam()).propertySubtype, "office.office_building");

  // Row 17952: type=["industrial.warehouse_distribution"]
  const rowB = fixtures.find((f) => f.external_id === "17952")!.rawSharpLaunch;
  assert.equal(avisonYoungBaseListing(rowB, emptyTeam()).propertySubtype, "industrial.warehouse_distribution");
});

test("avisonYoungBaseListing buildingClass is null for AY dot-notation subtype strings (normBuildingClass yields null)", () => {
  const fixtures = loadFixture();
  // AY uses "category.subcategory" notation, not "Class A/B/C" -> buildingClass=null
  // This matches golden vector row 23: normBuildingClass("office.medical") -> null
  for (const fixture of fixtures) {
    const listing = avisonYoungBaseListing(fixture.rawSharpLaunch, emptyTeam());
    assert.equal(
      listing.buildingClass,
      undefined,
      `buildingClass must be null/absent for AY subtype "${fixture.rawSharpLaunch.type}"`
    );
  }
});

test("avisonYoungBaseListing never throws on a sparse or null-field rawSharpLaunch row", () => {
  // Minimal row: only id, state (for US compatibility check), and transaction
  const sparse = {
    id: 99999,
    state: "NY",
    transaction: ["lease"],
  };
  let listing: any;
  assert.doesNotThrow(() => {
    listing = avisonYoungBaseListing(sparse, emptyTeam());
  });
  // Absent fields stay absent after prune (not null/undefined keys present)
  assert.equal(listing.leaseRateMin, undefined);
  assert.equal(listing.leaseRateMax, undefined);
  assert.equal(listing.submarket, undefined);
  assert.equal(listing.yearBuilt, undefined);
  assert.equal(listing.units, undefined);
  assert.equal(listing.salePricePerSf, undefined);
  assert.equal(listing.buildingClass, undefined);
  assert.equal(listing.propertySubtype, undefined);
});
