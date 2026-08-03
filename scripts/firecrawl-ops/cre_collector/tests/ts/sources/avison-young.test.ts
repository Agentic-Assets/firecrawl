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
  avisonYoungTruncated,
  isAvisonYoungPropertyPhoto,
  avisonYoungNameSlug,
  isAvisonYoungUsCompatible,
  avisonYoungTransactions,
  avisonYoungMatchesTx,
  avisonYoungTransactionType,
  avisonYoungSizeText,
  avisonYoungLeaseRateText,
  avisonYoungContact,
  avisonYoungEntityItems,
  avisonYoungTeamFeedState,
  decodeAvisonYoungCloudflareEmail,
  avisonYoungMailtoEmail,
  extractAvisonYoungDetailContacts,
  mergeAvisonYoungContacts,
  extractAvisonYoungUrls,
  extractAvisonYoungDocuments,
  extractAvisonYoungPhotos,
  extractAvisonYoungJsonLd,
  extractAvisonYoungContactUrls,
  enrichAvisonYoungContacts,
  harvestAvisonYoung,
  avisonYoungLongestMarkdown,
  avisonYoungBaseListing,
  avisonYoungSelectedProviderIds,
  assertAvisonYoungOutputIdentity,
  assertAvisonYoungDetailDoc,
  assertAvisonYoungStrictFeed,
  fetchAvisonYoungDirectDoc,
  fetchAvisonYoungDirectDocWithRetry,
  isAvisonYoungDirectDetailUrl,
  isPublicAvisonYoungAddress,
  enrichAvisonYoungListing,
  srcAvisonYoung,
} from "../../../sources/avison-young.js";
import { firecrawl } from "../../../lib/scrape.js";

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

test("Avison finite caps report truncation while unlimited runs do not", () => {
  assert.equal(avisonYoungTruncated(10, 10, 12), true);
  assert.equal(avisonYoungTruncated(12, 12, 12), false);
  assert.equal(
    avisonYoungTruncated(Number.POSITIVE_INFINITY, 12, 20),
    false
  );
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

test("avisonYoungEntityItems permits an empty supplemental team feed but not an empty inventory", () => {
  assert.deepEqual(avisonYoungEntityItems({ items: [] }, "team_member"), []);
  assert.throws(
    () => avisonYoungEntityItems({ items: [] }, "website"),
    /website API returned no items/
  );
  assert.deepEqual(avisonYoungEntityItems({ items: [{ id: 1 }] }, "website"), [{ id: 1 }]);
  assert.deepEqual(avisonYoungTeamFeedState([]), {
    rows: [],
    complete: false,
    reason: "team_member API returned no items",
  });
  assert.match(avisonYoungTeamFeedState([], new Error("HTTP 503")).reason ?? "", /HTTP 503/);
});

test("Avison keeps authoritative website inventory when SharpLaunch team_member is empty", async (t) => {
  // Regression: on July 29 the provider returned an empty `team_member`
  // collection while `website` still contained active listings. Treating the
  // supplemental broker collection as required caused all sale and lease rows
  // to be discarded. Full property detail remains the source for broker-card
  // enrichment, so the inventory row must be emitted in preservation mode.
  const oldFetch = globalThis.fetch;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const oldPropertyStrict = process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  const oldScrape = firecrawl.scrape;
  delete process.env.CRE_REQUIRE_FRESH_DETAILS;
  process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
  process.env.CRE_REFRESH_GENERATION = "avison-empty-team-property-detail";
  const detailCalls: Array<{ url: string; options: any }> = [];
  (firecrawl as any).scrape = async (url: string, options: any) => {
    detailCalls.push({ url, options });
    return {
      rawHtml: `<script type="application/ld+json">{"@type":"RealEstateListing","name":"Provider inventory fixture","url":"${url}"}</script>`,
      markdown: "# Provider inventory fixture\nCurrent property detail",
      links: [],
      metadata: { sourceURL: url, statusCode: 200 },
    };
  };
  t.after(() => {
    globalThis.fetch = oldFetch;
    (firecrawl as any).scrape = oldScrape;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
    if (oldPropertyStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = oldPropertyStrict;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
  });
  // Cache isolation is intentional: this test runs before any collector call
  // in this file and exercises the actual source path, not just its helper.
  // The first request is the page key and the second is website inventory;
  // replace the fetch response once the source has asked for `website`.
  let requestCount = 0;
  globalThis.fetch = async (input) => {
    requestCount += 1;
    const url = new URL(String(input));
    if (url.hostname === "www.avisonyoung.us") {
      return new Response(
        "<script>SharpLaunch.PSE.create('0123456789abcdef0123456789abcdef')</script>",
        { status: 200 }
      );
    }
    assert.equal(url.hostname, "pse-api.sharplaunch.com");
    const entity = url.searchParams.get("entity");
    if (entity === "website") {
      return new Response(
        JSON.stringify({
          items: [
            {
              id: 17,
              status: "active",
              state: "TX",
              transaction: ["sale"],
              name: "Provider inventory fixture",
              external_url: "https://www.avisonyoung.us/properties/provider-fixture",
            },
          ],
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      );
    }
    assert.equal(entity, "team_member");
    return new Response(JSON.stringify({ items: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  const result = await srcAvisonYoung("sale", 1, false);

  assert.equal(requestCount, 3);
  assert.equal(detailCalls.length, 1);
  assert.equal(detailCalls[0].options.maxAge, 0);
  assert.equal(result.listings.length, 1);
  assert.equal(result.listings[0].id, "17");
  assert.equal(result.listings[0].preserveChildCollections, true);
  assert.equal(result.listings[0].detailObservedWithChildPreservation, true);
  assert.match(result.listings[0].detailObservedAt, /^20\d\d-/);
  assert.equal(result.listings[0].freshnessProvenance.detailScope, "detail_page");
  assert.equal(result.listings[0].freshnessProvenance.cacheDisposition, "live");
  assert.match(result.note ?? "", /Supplemental broker feed degraded/);
});

test("Avison selected inventory requires unique nonempty provider IDs", () => {
  assert.deepEqual(
    avisonYoungSelectedProviderIds([{ id: 17 }, { id: " 18 " }]),
    ["17", "18"]
  );
  assert.throws(
    () => avisonYoungSelectedProviderIds([{ id: 17 }, { id: "17" }]),
    /duplicate provider id 17/
  );
  assert.throws(
    () => avisonYoungSelectedProviderIds([{ id: " " }]),
    /missing a provider id/
  );
  assert.throws(
    () => avisonYoungSelectedProviderIds([{}]),
    /missing a provider id/
  );
});

test("Avison output identities reconcile exactly to selected provider IDs", () => {
  assert.doesNotThrow(() =>
    assertAvisonYoungOutputIdentity(
      ["17", "18"],
      [{ id: "17" }, { id: "18" }]
    )
  );
  assert.throws(
    () =>
      assertAvisonYoungOutputIdentity(
        ["17", "18"],
        [{ id: "17" }, { id: "19" }]
      ),
    /identity reconciliation failed/
  );
  assert.throws(
    () => assertAvisonYoungOutputIdentity(["17"], [{ id: "17" }, { id: "17" }]),
    /identity reconciliation failed/
  );
  assert.throws(
    () => assertAvisonYoungOutputIdentity(["17"], [{ id: null }]),
    /output listing is missing a provider id/
  );
});

test("detail contacts recover broker data when the supplemental team feed is empty", () => {
  const encodedEmail = "c5abaca6adaaa9a4b6ebb5a0a9b0b6acaa85a4b3acb6aaabbcaab0aba2eba6aaa8";
  assert.equal(decodeAvisonYoungCloudflareEmail(encodedEmail), "nicholas.pelusio@avisonyoung.com");
  assert.equal(decodeAvisonYoungCloudflareEmail("not-hex"), null);
  assert.equal(
    avisonYoungMailtoEmail("mailto:frank.simpson%40avisonyoung.com?bcc=webleads%40avisonyoung.com"),
    "frank.simpson@avisonyoung.com"
  );
  assert.equal(avisonYoungMailtoEmail("mailto:first@example.com,second@example.com"), null);

  const contacts = extractAvisonYoungDetailContacts([
    {
      url: `${AVISON_YOUNG_HOST}/properties/mesa`,
      doc: doc(`
        <script type="application/ld+json">
          {"@context":"https://schema.org","@type":"RealEstateListing",
           "agent":[{"@type":"RealEstateAgent","name":"Nicholas Pelusio",
                     "image":"https://example.com/nicholas.jpg"}]}
        </script>
        <div class="team-member">
          <img src="https://example.com/nicholas.jpg">
          <h4 class="team-member__name">Nicholas Pelusio</h4>
          <div class="team-member__job">Principal</div>
          <div class="team-member__company">Avison Young</div>
          <div class="team-member__phone"><a href="tel:+16025550101">Call</a></div>
          <div class="team-member__email">
            <span class="__cf_email__" data-cfemail="${encodedEmail}">protected</span>
          </div>
        </div>
      `),
    },
  ]);
  assert.deepEqual(contacts, [
    {
      name: "Nicholas Pelusio",
      title: "Principal",
      company: "Avison Young",
      phone: "+16025550101",
      email: "nicholas.pelusio@avisonyoung.com",
      avatarUrl: "https://example.com/nicholas.jpg",
    },
  ]);
  assert.equal(
    mergeAvisonYoungContacts(
      [{ name: "Nicholas Pelusio", company: "Avison Young" }],
      contacts
    ).length,
    1
  );
  assert.equal(
    mergeAvisonYoungContacts(
      [{ name: "Alex Smith", email: "alex.one@example.com", phone: "212-555-0101" }],
      [{ name: "Alex Smith", email: "alex.two@example.com", phone: "305-555-0202" }]
    ).length,
    2
  );
  assert.equal(
    mergeAvisonYoungContacts(
      [{ name: "Alex Smith", email: "alex@example.com", phone: "212-555-0100" }],
      [{ name: "Jordan Lee", email: "jordan@example.com", phone: "212-555-0100" }]
    ).length,
    2
  );
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

test("strict-detail Avison mode rejects degraded team feeds while property-detail mode preserves contacts", () => {
  clearDetailLimitEnv();
  assert.equal(
    avisonYoungDetailLimit(Number.POSITIVE_INFINITY, 12, true),
    12
  );
  process.env[DETAIL_LIMIT_ENV] = "3";
  assert.equal(avisonYoungDetailLimit(50, 12, true), 12);
  assert.equal(avisonYoungDetailLimit(50, 12, false), 3);
  clearDetailLimitEnv();

  assert.throws(
    () =>
      assertAvisonYoungStrictFeed(
        false,
        "team_member API returned no items",
        true
      ),
    /team feed is incomplete/
  );
  assert.doesNotThrow(() =>
    assertAvisonYoungStrictFeed(false, "degraded", false)
  );
});

test("strict Avison source requires an explicit refresh generation", async (t) => {
  const originalStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const originalGeneration = process.env.CRE_REFRESH_GENERATION;
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  delete process.env.CRE_REFRESH_GENERATION;
  t.after(() => {
    if (originalStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = originalStrict;
    if (originalGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = originalGeneration;
  });
  const { srcAvisonYoung } = await import(
    "../../../sources/avison-young.js"
  );
  await assert.rejects(
    () => srcAvisonYoung("sale", 1, false),
    /requires CRE_REFRESH_GENERATION/
  );
});

test("Avison detail admission rejects challenge and wrong-property shells", () => {
  const base = {
    id: "17808",
    name: "602 N Capitol Ave",
    street: "602 N Capitol Ave",
    externalUrl:
      "https://www.avisonyoung.us/properties/602-n-capitol-ave-indianapolis-lease",
  };
  assert.throws(
    () =>
      assertAvisonYoungDetailDoc(
        doc("<html><title>Just a moment...</title><div>captcha</div></html>"),
        base.externalUrl,
        base
      ),
    /challenge or error shell/
  );
  assert.throws(
    () =>
      assertAvisonYoungDetailDoc(
        {
          ...doc("<html><h1>Unrelated Chicago Property</h1><main>Property details</main></html>"),
          metadata: {
            sourceURL:
              "https://www.avisonyoung.us/properties/unrelated-chicago-property",
          },
        },
        base.externalUrl,
        base
      ),
    /identity does not match/
  );
});

test("Avison direct detail transport captures current HTML, links, images, and identity metadata", async (t) => {
  const url =
    "https://www.avisonyoung.us/properties/602-n-capitol-ave-indianapolis-lease";
  const detail = await fetchAvisonYoungDirectDoc(
    url,
    30000,
    async () => ["93.184.216.34"],
    async (_url, address) => {
      assert.equal(address, "93.184.216.34");
      return {
        status: 200,
        location: null,
        body: `<html><main class="property-detail"><h1>602 N Capitol Ave</h1>
         <p>Property details</p><a href="/brochure.pdf">Brochure</a>
         <img src="/hero.jpg"></main></html>`,
      };
    }
  );
  assert.match(detail.rawHtml, /602 N Capitol Ave/);
  assert.match(detail.markdown, /^# 602 N Capitol Ave/m);
  assert.match(
    detail.markdown,
    /\[Brochure\]\(https:\/\/www\.avisonyoung\.us\/brochure\.pdf\)/
  );
  assert.deepEqual(detail.links, ["/brochure.pdf"]);
  assert.deepEqual(detail.images, ["/hero.jpg"]);
  assert.equal(detail.metadata?.statusCode, 200);
  assert.doesNotThrow(() =>
    assertAvisonYoungDetailDoc(detail, url, {
      id: "17808",
      name: "602 N Capitol Ave",
      street: "602 N Capitol Ave",
    })
  );
});

test("Avison strict direct detail retries only the failed page with bounded backoff", async () => {
  const waits: number[] = [];
  let calls = 0;
  const detail = await fetchAvisonYoungDirectDocWithRetry(
    "https://www.avisonyoung.us/properties/retry",
    async () => {
      calls++;
      if (calls < 3) throw new Error(`transient ${calls}`);
      return doc("<html><main><h1>Recovered property</h1></main></html>");
    },
    3,
    20,
    async (milliseconds) => {
      waits.push(milliseconds);
    }
  );

  assert.equal(calls, 3);
  assert.deepEqual(waits, [20, 40]);
  assert.match(detail.rawHtml, /Recovered property/);
});

test("Avison strict direct detail reports the terminal per-page failure", async () => {
  await assert.rejects(
    () =>
      fetchAvisonYoungDirectDocWithRetry(
        "https://www.avisonyoung.us/properties/retry",
        async () => {
          throw new Error("HTTP 503");
        },
        3,
        0
      ),
    /failed after 3 attempt\(s\): Error: HTTP 503/
  );
});

test("Avison direct detail transport rejects unsafe URLs, addresses, and redirects", async (t) => {
  assert.equal(
    isAvisonYoungDirectDetailUrl("https://www.avisonyoung.us/properties/a"),
    true
  );
  assert.equal(
    isAvisonYoungDirectDetailUrl("https://listing.sharplaunch.com"),
    true
  );
  for (const value of [
    "http://www.avisonyoung.us/properties/a",
    "https://example.com/property/a",
    "https://127.0.0.1/property/a",
    "https://user:pass@www.avisonyoung.us/property/a",
  ]) {
    assert.equal(isAvisonYoungDirectDetailUrl(value), false);
  }
  for (const address of [
    "127.0.0.1",
    "10.0.0.1",
    "169.254.1.1",
    "192.168.1.1",
    "::1",
    "fc00::1",
    "2001:db8::1",
    "::ffff:7f00:1",
    "::ffff:0a00:0001",
    "0:0:0:0:0:ffff:7f00:1",
  ]) {
    assert.equal(isPublicAvisonYoungAddress(address), false);
  }
  assert.equal(isPublicAvisonYoungAddress("93.184.216.34"), true);
  assert.equal(isPublicAvisonYoungAddress("2606:2800:220:1:248:1893:25c8:1946"), true);

  let calls = 0;
  await assert.rejects(
    () =>
      fetchAvisonYoungDirectDoc(
        "https://www.avisonyoung.us/properties/a",
        30000,
        async () => ["93.184.216.34"],
        async () => {
          calls++;
          return {
            status: 302,
            location: "https://example.com/private-target",
            body: "",
          };
        }
      ),
    /not approved/
  );
  assert.equal(calls, 1);
  await assert.rejects(
    () =>
      fetchAvisonYoungDirectDoc(
        "https://www.avisonyoung.us/properties/a",
        30000,
        async () => ["127.0.0.1"]
      ),
    /non-public address/
  );
});

test("Avison direct detail creates durable Markdown from descriptive JSON-LD when the body is empty", async () => {
  const url =
    "https://www.avisonyoung.us/properties/json-ld-only";
  const detail = await fetchAvisonYoungDirectDoc(
    url,
    30000,
    async () => ["93.184.216.34"],
    async () => ({
      status: 200,
      location: null,
      body: `<html><head><script type="application/ld+json">
        {"@type":"RealEstateListing","name":"JSON-LD Property",
         "description":"Current verified description","url":"${url}"}
      </script></head><body><main></main></body></html>`,
    })
  );
  assert.match(detail.markdown, /^# JSON-LD Property/m);
  assert.match(detail.markdown, /Current verified description/);
  assert.match(detail.markdown, /\[Source property page\]/);
});

test("partial Avison alternate failure preserves children and prior Markdown evidence", async (t) => {
  const originalTransport = process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
  process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
  t.after(() => {
    if (originalTransport === undefined) {
      delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    } else {
      process.env.AVISON_YOUNG_DETAIL_TRANSPORT = originalTransport;
    }
  });
  const listing = await enrichAvisonYoungListing(
    {
      id: "17808",
      name: "602 N Capitol Ave",
      street: "602 N Capitol Ave",
      sharpLaunchUrl: "https://stale-alias.sharplaunch.com",
      externalUrl:
        "https://www.avisonyoung.us/properties/602-n-capitol-ave-indianapolis-lease",
    },
    false,
    (url) =>
      fetchAvisonYoungDirectDoc(
        url,
        30000,
        async () => ["93.184.216.34"],
        async (requestedUrl) => ({
          status: 200,
          location: null,
          body: requestedUrl.hostname.includes("stale-alias")
            ? "<html><main><h1>Access denied</h1><p>captcha</p></main></html>"
            : `<html><main class="property-detail"><h1>602 N Capitol Ave</h1>
               <p>Property details</p><a href="/brochure.pdf">Brochure</a></main></html>`,
        })
      )
  );
  assert.equal(listing.detailError, undefined);
  assert.match(listing.detailWarning, /challenge or error shell/);
  assert.equal(listing.preserveChildCollections, true);
  assert.equal(listing.detailObservedWithChildPreservation, true);
  assert.match(listing.markdown, /^# 602 N Capitol Ave/m);
  assert.equal(listing.preserveExistingMarkdown, true);
  assert.equal(
    listing.detailScrape.markdownDisposition,
    "preserve_existing_or_insert"
  );
});

test("strict Avison detail scrape is uncached, complete, and generation-bound", async (t) => {
  const originalScrape = firecrawl.scrape;
  const originalStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const originalGeneration = process.env.CRE_REFRESH_GENERATION;
  const calls: any[] = [];
  (firecrawl as any).scrape = async (url: string, options: any) => {
    calls.push({ url, options });
    return {
      rawHtml: `
        <html><main class="property-detail"><h1>602 N Capitol Ave</h1>
        <script type="application/ld+json">
          {"@type":"RealEstateListing","name":"602 N Capitol Ave",
           "url":"${url}","description":"Current property detail"}
        </script></main></html>`,
      markdown: "# 602 N Capitol Ave\nProperty details",
      links: [],
      metadata: { sourceURL: url, statusCode: 200 },
    };
  };
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  process.env.CRE_REFRESH_GENERATION = "avison-strict-test";
  t.after(() => {
    (firecrawl as any).scrape = originalScrape;
    if (originalStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = originalStrict;
    if (originalGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = originalGeneration;
  });

  const listing = await enrichAvisonYoungListing(
    {
      id: "17808",
      name: "602 N Capitol Ave",
      street: "602 N Capitol Ave",
      contactsDetailed: [{ name: "Ada Broker" }],
      externalUrl:
        "https://www.avisonyoung.us/properties/602-n-capitol-ave-indianapolis-lease",
      inventoryObservedAt: "2026-07-29T12:00:00.000Z",
    },
    true
  );
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.maxAge, 0);
  assert.match(listing.detailObservedAt, /^20\d\d-/);
  assert.equal(listing.freshnessProvenance.generationId, "avison-strict-test");
  assert.equal(listing.freshnessProvenance.detailScope, "detail_page");
  assert.equal(listing.preserveChildCollections, undefined);
  assert.equal(listing.detailError, undefined);
});

test("degraded non-strict detail refresh preserves prior child collections", async (t) => {
  const originalScrape = firecrawl.scrape;
  const originalFreshDetails =
    process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS;
  const originalGeneration = process.env.CRE_REFRESH_GENERATION;
  const calls: any[] = [];
  (firecrawl as any).scrape = async (url: string, options: any) => {
    calls.push({ url, options });
    return {
      rawHtml: `
        <html><main class="property-detail"><h1>602 N Capitol Ave</h1>
        <script type="application/ld+json">
          {"@type":"RealEstateListing","name":"602 N Capitol Ave","url":"${url}"}
        </script></main></html>`,
      markdown: "# 602 N Capitol Ave\nProperty details",
      links: [],
      metadata: { sourceURL: url, statusCode: 200 },
    };
  };
  process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
  process.env.CRE_REFRESH_GENERATION = "avison-property-detail-test";
  t.after(() => {
    (firecrawl as any).scrape = originalScrape;
    if (originalFreshDetails === undefined) {
      delete process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS;
    } else {
      process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS =
        originalFreshDetails;
    }
    if (originalGeneration === undefined) {
      delete process.env.CRE_REFRESH_GENERATION;
    } else {
      process.env.CRE_REFRESH_GENERATION = originalGeneration;
    }
  });

  const listing = await enrichAvisonYoungListing(
    {
      id: "17808",
      name: "602 N Capitol Ave",
      street: "602 N Capitol Ave",
      externalUrl:
        "https://www.avisonyoung.us/properties/602-n-capitol-ave-indianapolis-lease",
      preserveChildCollections: true,
    },
    false
  );

  assert.equal(listing.id, "17808");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.maxAge, 0);
  assert.equal(listing.preserveChildCollections, true);
  assert.equal(listing.detailObservedWithChildPreservation, true);
  assert.match(listing.detailObservedAt, /^20\d\d-/);
  assert.equal(listing.freshnessProvenance.detailScope, "detail_page");
  assert.equal(listing.freshnessProvenance.cacheDisposition, "live");
  assert.equal(
    listing.freshnessProvenance.generationId,
    "avison-property-detail-test"
  );
  assert.equal(listing.detailError, undefined);
});

test("strict Avison fails on detail admission errors while non-strict preserves the row", async (t) => {
  const originalScrape = firecrawl.scrape;
  (firecrawl as any).scrape = async () => {
    return {
      rawHtml: "<html><h1>Service unavailable</h1></html>",
      markdown: "Service unavailable",
      links: [],
      metadata: { statusCode: 503 },
    };
  };
  t.after(() => {
    (firecrawl as any).scrape = originalScrape;
  });
  const base = {
    id: "17808",
    name: "602 N Capitol Ave",
    externalUrl:
      "https://www.avisonyoung.us/properties/602-n-capitol-ave-indianapolis-lease",
  };
  await assert.rejects(
    () => enrichAvisonYoungListing(base, true),
    /detail fetch failed/
  );
  const fallback = await enrichAvisonYoungListing(base, false);
  assert.match(fallback.detailError, /HTTP 503/);
});
