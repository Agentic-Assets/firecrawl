// Isolate argv before avison-young.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
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
} from "../../../sources/avison-young.js";

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
