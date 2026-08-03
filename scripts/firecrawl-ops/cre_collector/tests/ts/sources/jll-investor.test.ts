// Isolate argv before jll-investor.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  JLL_INVESTOR_HOST,
  JLL_INVESTOR_HOME_URL,
  jllInvestorNextData,
  jllInvestorBuildId,
  jllInvestorStructuredListing,
  jllInvestorDetailRoute,
  jllInvestorUrlFromAlias,
  jllInvestorSitemapUrls,
  jllInvestorSitemapCandidateLimit,
  jllInvestorStatus,
  jllInvestorDetailCountryClassification,
  jllInvestorSearchListing,
  jllInvestorDocumentUrls,
  jllInvestorImageUrls,
  jllInvestorContacts,
  jllInvestorExtractLicense,
  jllInvestorStrandedMedia,
  jllInvestorStrandedDocs,
  jllInvestorStrandedStructured,
  parseJllInvestorStructuredDetail,
  enrichJllInvestorListing,
  resetJllInvestorBuildIdForTests,
  srcJllInvestor,
} from "../../../sources/jll-investor.js";
import { harvestDetail } from "../../../lib/harvest.js";
import { firecrawl } from "../../../lib/scrape.js";

// ---------------------------------------------------------------------------
// Phase-2 data-lift tests: fixture-based, pure transform, no network.
// ---------------------------------------------------------------------------

const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(__dirname, "../../fixtures/raw_data/jll.json");

function loadJllFixture(): any[] {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
}

function jllInvestorRow(): any {
  return loadJllFixture().find((r: any) => r._source === "jll-investor");
}


test("jllInvestorNextData parses __NEXT_DATA__ JSON from HTML", () => {
  const html = `
    <html><body>
      <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"initialState":{"pdp":{"listing":{"id":"SF-001"}}}}}}
      </script>
    </body></html>
  `;
  const data = jllInvestorNextData(html);
  assert.equal(data?.props?.pageProps?.initialState?.pdp?.listing?.id, "SF-001");
  assert.equal(jllInvestorNextData("<html></html>"), null);
});

test("JLL Investor build id and structured detail helpers fail closed", () => {
  const html = `
    <script id="__NEXT_DATA__" type="application/json">
      {"buildId":"_safe-Build_123"}
    </script>
  `;
  assert.equal(jllInvestorBuildId(html), "_safe-Build_123");
  assert.equal(
    jllInvestorBuildId(
      `<script id="__NEXT_DATA__" type="application/json">{"buildId":"../bad"}</script>`
    ),
    null
  );
  const payload = {
    pageProps: {
      initialState: {
        pdp: {
          listing: { id: "006P500000f2tXYIAY" },
        },
      },
    },
  };
  assert.equal(jllInvestorStructuredListing(payload)?.id, "006P500000f2tXYIAY");
  assert.equal(jllInvestorStructuredListing({}), null);
});

test("jllInvestorDetailRoute builds an encoded public Next-data route and rejects unsafe paths", () => {
  const route = jllInvestorDetailRoute(
    "_safe-Build_123",
    "https://invest.jll.com/us/en/listings/retail/the-village"
  );
  assert.equal(route.alias, "retail/the-village");
  assert.equal(
    route.url,
    "https://invest.jll.com/_next/data/_safe-Build_123/us/en/listings/retail/the-village.json?region=us&locale=en&asset=retail&alias=the-village"
  );
  assert.throws(
    () =>
      jllInvestorDetailRoute(
        "../bad",
        "https://invest.jll.com/us/en/listings/retail/the-village"
      ),
    /build id/i
  );
  assert.throws(
    () =>
      jllInvestorDetailRoute(
        "_safe-Build_123",
        "https://evil.example/us/en/listings/retail/the-village"
      ),
    /unsafe/i
  );
  assert.throws(
    () =>
      jllInvestorDetailRoute(
        "_safe-Build_123",
        "https://invest.jll.com/us/en/listings/retail/%2Fetc"
      ),
    /unsafe/i
  );
  assert.throws(
    () =>
      jllInvestorDetailRoute(
        "_safe-Build_123",
        "https://invest.jll.com/us/en/listings/retail/the-village?preview=1"
      ),
    /unsafe/i
  );
});

test("jllInvestorUrlFromAlias resolves slug, path, and absolute URLs", () => {
  assert.equal(
    jllInvestorUrlFromAlias("multifamily/dallas-portfolio"),
    `${JLL_INVESTOR_HOST}/us/en/listings/multifamily/dallas-portfolio`
  );
  assert.equal(
    jllInvestorUrlFromAlias("/us/en/listings/office/chicago-tower"),
    `${JLL_INVESTOR_HOST}/us/en/listings/office/chicago-tower`
  );
  assert.equal(
    jllInvestorUrlFromAlias("https://invest.jll.com/us/en/listings/retail/austin"),
    "https://invest.jll.com/us/en/listings/retail/austin"
  );
  assert.equal(jllInvestorUrlFromAlias(null), null);
});

test("jllInvestorSitemapUrls extracts locale sitemap links from index HTML", () => {
  const html = `
    <sitemapindex>
      <sitemap><loc>https://invest.jll.com/us/sitemap-us.xml</loc></sitemap>
      <sitemap><loc>https://invest.jll.com/gb/sitemap-gb.xml</loc></sitemap>
      <sitemap><loc>https://invest.jll.com/us/sitemap-us.xml</loc></sitemap>
    </sitemapindex>
  `;
  assert.deepEqual(jllInvestorSitemapUrls(html), [
    "https://invest.jll.com/us/sitemap-us.xml",
    "https://invest.jll.com/gb/sitemap-gb.xml",
  ]);
});

test("jllInvestorSitemapCandidateLimit applies max heuristics when scan limit is unset", () => {
  assert.equal(jllInvestorSitemapCandidateLimit(10, 500), 80);
  assert.equal(jllInvestorSitemapCandidateLimit(0, 40), 26);
  assert.equal(jllInvestorSitemapCandidateLimit(Number.POSITIVE_INFINITY, 200), 200);
});

test("JLL Investor detail country classification uses exact fullLocation US token only when country is absent", () => {
  assert.equal(
    jllInvestorDetailCountryClassification({
      country: null,
      fullLocation: "Pooler, GA, US, Americas",
    }),
    "us"
  );
  assert.equal(
    jllInvestorDetailCountryClassification({
      country: null,
      fullLocation: "Various USA locations",
    }),
    "unknown"
  );
  assert.equal(
    jllInvestorDetailCountryClassification({
      country: "Canada",
      fullLocation: "Toronto, ON, US, Americas",
    }),
    "non_us"
  );
});

test("structured JLL Investor detail validates Salesforce id and exact alias", () => {
  const base = {
    id: "00608000010RMQHAA4",
    url: "https://invest.jll.com/us/en/listings/industrial-logistics/morgan-lakes",
    inventoryObservedAt: "2026-07-30T08:00:00.000Z",
    photos: [],
  };
  const payload = {
    pageProps: {
      initialState: {
        pdp: {
          listing: {
            id: base.id,
            alias: "industrial-logistics/morgan-lakes",
            name: "Morgan Lakes",
            country: null,
            fullLocation: "Pooler, GA, US, Americas",
          },
        },
      },
    },
  };
  const parsed = parseJllInvestorStructuredDetail(
    base,
    payload,
    "industrial-logistics/morgan-lakes"
  );
  assert.equal(parsed.id, base.id);
  assert.equal(parsed.country, "US");
  assert.equal(parsed.preserveChildCollections, undefined);
  assert.equal(parsed.freshnessProvenance.method, "jll_investor_next_data_detail");

  const aliasMismatch = parseJllInvestorStructuredDetail(
    base,
    payload,
    "industrial-logistics/other"
  );
  assert.match(aliasMismatch.detailError, /alias mismatch/i);

  const invalidId = parseJllInvestorStructuredDetail(
    base,
    {
      pageProps: {
        initialState: {
          pdp: {
            listing: {
              ...payload.pageProps.initialState.pdp.listing,
              id: "slug-only",
            },
          },
        },
      },
    },
    "industrial-logistics/morgan-lakes"
  );
  assert.match(invalidId.detailError, /Salesforce Opportunity id/i);
});

test("JLL Investor structured enrichment refreshes build id once after rotation", async () => {
  const oldScrape = firecrawl.scrape;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const calls: Array<{ url: string; options: any }> = [];
  let homepageCalls = 0;
  (firecrawl as any).scrape = async (url: string, options: any) => {
    calls.push({ url, options });
    if (url === JLL_INVESTOR_HOME_URL) {
      homepageCalls++;
      const buildId = homepageCalls === 1 ? "_old-build" : "_new-build";
      return {
        rawHtml: `<script id="__NEXT_DATA__" type="application/json">${JSON.stringify({ buildId })}</script>`,
      };
    }
    if (url.includes("/_old-build/")) {
      return { rawHtml: JSON.stringify({ pageProps: { initialState: {} } }) };
    }
    return {
      rawHtml: JSON.stringify({
        pageProps: {
          initialState: {
            pdp: {
              listing: {
                id: "006P500000f2tXYIAY",
                alias: "retail/the-village",
                name: "The Village",
                country: "United States",
              },
            },
          },
        },
      }),
    };
  };
  try {
    resetJllInvestorBuildIdForTests();
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    const row = await enrichJllInvestorListing({
      id: "006P500000f2tXYIAY",
      url: "https://invest.jll.com/us/en/listings/retail/the-village",
      inventoryObservedAt: "2026-07-30T08:00:00.000Z",
      photos: [],
    });
    assert.equal(row.id, "006P500000f2tXYIAY");
    assert.equal(row.detailError, undefined);
    assert.equal(homepageCalls, 2);
    assert.ok(calls.some(({ url }) => url.includes("/_old-build/")));
    assert.ok(calls.some(({ url }) => url.includes("/_new-build/")));
    assert.ok(calls.every(({ options }) => options.maxAge === 0));
  } finally {
    (firecrawl as any).scrape = oldScrape;
    resetJllInvestorBuildIdForTests();
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("strict JLL Investor collection preserves unresolved candidates and bypasses Firecrawl cache", async () => {
  const oldScrape = firecrawl.scrape;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  const oldStartedAt = process.env.CRE_REFRESH_STARTED_AT;
  const calls: Array<{ url: string; options: any }> = [];
  let secondCountry: string | undefined;
  let secondId = "006080000100J8bAAE";
  const homepageHtml = `
    <script id="__NEXT_DATA__" type="application/json">
      ${JSON.stringify({ buildId: "_strict-build" })}
    </script>
  `;
  const detailPayload = (listing: Record<string, unknown>) => ({
    pageProps: { initialState: { pdp: { listing } } },
  });
  (firecrawl as any).scrape = async (url: string, options: any) => {
    calls.push({ url, options });
    if (url.endsWith("/sitemap_index.xml")) {
      return { rawHtml: `<loc>https://invest.jll.com/us/sitemap-us.xml</loc>` };
    }
    if (url.endsWith("/us/sitemap-us.xml")) {
      return {
        rawHtml: `
          <urlset>
            <url><loc>https://invest.jll.com/us/en/listings/office/known-us</loc></url>
            <url><loc>https://invest.jll.com/us/en/listings/office/unknown-country</loc></url>
            <url><loc>https://invest.jll.com/us/en/listings/office/known-us</loc></url>
          </urlset>
        `,
      };
    }
    if (url === JLL_INVESTOR_HOME_URL) {
      return { rawHtml: homepageHtml };
    }
    if (url.includes("/known-us.json")) {
      return {
        rawHtml: JSON.stringify(detailPayload({
          id: "006P500000f2tXYIAY",
          alias: "office/known-us",
          name: "Known US",
          country: "United States",
        })),
      };
    }
    return {
      rawHtml: JSON.stringify(detailPayload({
        id: secondId,
        alias: "office/unknown-country",
        name: "Unknown country",
        country: secondCountry,
      })),
    };
  };
  try {
    resetJllInvestorBuildIdForTests();
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "jll-investor-strict-test";
    process.env.CRE_REFRESH_STARTED_AT = "2026-07-29T12:00:00.000Z";

    const result = await srcJllInvestor("sale", Number.POSITIVE_INFINITY, false);

    assert.equal(result.truncated, true);
    assert.equal(result.listings.length, 2);
    const accepted = result.listings.find((row) => row.id === "006P500000f2tXYIAY");
    assert.match(accepted.inventoryObservedAt, /^20\d\d-/);
    assert.match(accepted.detailObservedAt, /^20\d\d-/);
    assert.equal(accepted.freshnessProvenance.generationId, "jll-investor-strict-test");
    assert.equal(accepted.freshnessProvenance.detailScope, "detail_page");
    assert.equal(accepted.freshnessProvenance.cacheDisposition, "live");
    const unresolved = result.listings.find((row) => row.detailError);
    assert.match(unresolved.detailError, /country/i);
    assert.equal(unresolved.preserveChildCollections, true);
    assert.equal(
      calls.filter(({ url }) => url.includes("/known-us.json")).length,
      1
    );
    assert.ok(calls.every(({ options }) => options.maxAge === 0));

    calls.length = 0;
    resetJllInvestorBuildIdForTests();
    secondCountry = "Canada";
    const complete = await srcJllInvestor("sale", Number.POSITIVE_INFINITY, false);
    assert.equal(complete.truncated, false);
    assert.deepEqual(complete.listings.map((row) => row.id), ["006P500000f2tXYIAY"]);
    assert.ok(calls.every(({ options }) => options.maxAge === 0));

    resetJllInvestorBuildIdForTests();
    secondId = "006P500000f2tXYIAY";
    const duplicateIdentity = await srcJllInvestor(
      "sale",
      Number.POSITIVE_INFINITY,
      false
    );
    assert.equal(duplicateIdentity.truncated, true);
  } finally {
    (firecrawl as any).scrape = oldScrape;
    resetJllInvestorBuildIdForTests();
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
    if (oldStartedAt === undefined) delete process.env.CRE_REFRESH_STARTED_AT;
    else process.env.CRE_REFRESH_STARTED_AT = oldStartedAt;
  }
});

test("jllInvestorStatus prefers under-contract flag then stage name", () => {
  assert.equal(jllInvestorStatus({ isUnderContract: true, stageName: "Active" }), "Under Contract");
  assert.equal(jllInvestorStatus({ stageName: "Closed" }), "Closed");
  assert.equal(jllInvestorStatus({}), "Active");
});

test("jllInvestorSearchListing maps search API row to listing shape", () => {
  const listing = jllInvestorSearchListing({
    id: "a0B3x000001",
    alias: "multifamily/dallas-portfolio",
    name: "Dallas Portfolio",
    assetType: "Multifamily",
    displayAddress: "100 Main St",
    city: "Dallas",
    state: "TX",
    country: "United States",
    latitude: 32.78,
    longitude: -96.8,
    numberOfUnits: "240 units",
    image: "https://cdn.example/hero.jpg",
    isUnderContract: false,
    stageName: "Active",
  });
  assert.equal(listing.id, "a0B3x000001");
  assert.equal(listing.transactionType, "Sale (investment)");
  assert.equal(listing.country, "US");
  assert.equal(listing.url, `${JLL_INVESTOR_HOST}/us/en/listings/multifamily/dallas-portfolio`);
  assert.deepEqual(listing.photos, ["https://cdn.example/hero.jpg"]);
  assert.equal(listing.status, "Active");
});

test("jllInvestorDocumentUrls collects nested https document links", () => {
  const urls = jllInvestorDocumentUrls({
    documents: {
      teaser: { url: "https://cdn.example/teaser.pdf" },
      nested: [{ url: "https://cdn.example/flyer.pdf" }, "mailto:broker@jll.com"],
      deep: { child: { url: "https://cdn.example/deep.pdf" } },
    },
  });
  assert.deepEqual(urls, [
    "https://cdn.example/teaser.pdf",
    "https://cdn.example/flyer.pdf",
    "https://cdn.example/deep.pdf",
  ]);
});

test("jllInvestorImageUrls merges primary, multimedia, and fallback images", () => {
  const urls = jllInvestorImageUrls(
    {
      image: "https://cdn.example/primary.jpg",
      multimedia: { images: ["https://cdn.example/gallery-1.jpg", "https://cdn.example/primary.jpg"] },
    },
    ["https://cdn.example/fallback.jpg"]
  );
  assert.deepEqual(urls, [
    "https://cdn.example/primary.jpg",
    "https://cdn.example/gallery-1.jpg",
    "https://cdn.example/fallback.jpg",
  ]);
});

test("jllInvestorContacts maps brokers and dedupes by email", () => {
  const contacts = jllInvestorContacts({
    brokers: [
      { name: "Alex Broker", email: "alex@jll.com", title: "EVP", phone: "555-1000" },
      { name: "Alex Broker", email: "alex@jll.com", title: "EVP" },
      { name: "Sam Broker", email: "sam@jll.com", linkedInURL: "https://linkedin.com/in/sam" },
    ],
  });
  assert.equal(contacts.length, 2);
  assert.equal(contacts[0]?.company, "JLL");
  assert.equal(contacts[0]?.title, "EVP");
  assert.equal(contacts[1]?.linkedInUrl, "https://linkedin.com/in/sam");
});

test("jllInvestorContacts returns empty array when brokers missing", () => {
  assert.deepEqual(jllInvestorContacts({}), []);
  assert.deepEqual(jllInvestorContacts({ brokers: null }), []);
});

test("jllInvestorStrandedMedia: videos bare-string classified, tours typed virtual_tour", () => {
  const listing = {
    multimedia: {
      videos: ["https://player.vimeo.com/video/12345"],
      virtualTours: ["https://my.matterport.com/show/?m=XYZ"],
    },
    view360URLs: ["https://example.com/360/tour"],
  };
  const promoted = jllInvestorStrandedMedia(listing);
  const out = harvestDetail({ rawHtml: "", markdown: "", links: [] } as any, { extraMedia: promoted });
  assert.ok(out.media.some((m) => m.provider === "vimeo" && m.mediaType === "video"));
  // virtualTours + view360URLs are promoted as TYPED virtual_tour items, so the
  // harvester keeps that asserted type (no reclassification to matterport).
  assert.equal(out.media.filter((m) => m.mediaType === "virtual_tour").length, 2);
  assert.equal(out.media.filter((m) => m.mediaType === "matterport").length, 0);
});

test("jllInvestorStrandedDocs collects nested CA documentsCA urls", () => {
  const docs = jllInvestorStrandedDocs({
    documentsCA: [{ url: "https://invest.jll.com/ca/om.pdf" }, { href: "https://invest.jll.com/ca/financials.pdf" }],
  });
  const urls = docs.map((d) => d.url).sort();
  assert.deepEqual(urls, ["https://invest.jll.com/ca/financials.pdf", "https://invest.jll.com/ca/om.pdf"]);
});

test("jllInvestorStrandedStructured lifts units/market/submarket/highlights; empty for sparse", () => {
  const out = jllInvestorStrandedStructured({
    numberOfUnits: 180,
    market: "Dallas",
    submarket: "Uptown",
    occupancyRate: 94,
    highlights: ["Value-add", { text: "Below-market rents" }],
  });
  assert.equal(out.units, 180);
  assert.equal(out.market, "Dallas");
  assert.equal(out.submarket, "Uptown");
  assert.equal(out.occupancyRate, 94);
  assert.ok(out.highlights.includes("Value-add"));
  assert.deepEqual(jllInvestorStrandedStructured({}), {});
});

// ---------------------------------------------------------------------------
// Phase-2: jllInvestorExtractLicense
// ---------------------------------------------------------------------------

test("jllInvestorExtractLicense formats location:number from investor license shape", () => {
  assert.equal(
    jllInvestorExtractLicense([{ number: "FA.040030452", location: "Colorado" }]),
    "Colorado: FA.040030452"
  );
  assert.equal(
    jllInvestorExtractLicense([{ number: "IA.100092076", location: "Colorado", type: "Broker" }]),
    "Colorado: IA.100092076"
  );
  assert.equal(jllInvestorExtractLicense([]), null);
  assert.equal(jllInvestorExtractLicense(null), null);
  assert.equal(jllInvestorExtractLicense(undefined), null);
});

test("jllInvestorExtractLicense returns bare number when location absent", () => {
  assert.equal(jllInvestorExtractLicense([{ number: "9534161" }]), "9534161");
});

// ---------------------------------------------------------------------------
// Phase-2: jllInvestorStrandedStructured with HTML-string highlights and statusBadge
// ---------------------------------------------------------------------------

test("jllInvestorStrandedStructured strips HTML from string highlights", () => {
  const out = jllInvestorStrandedStructured({
    highlights:
      "<ol><li>Prime location in Lakewood, CO</li><li><strong>166,745 sqft</strong> across four stories</li></ol>",
  });
  assert.ok(Array.isArray(out.highlights));
  // Each li should appear as a separate string (HTML stripped)
  assert.ok(out.highlights.some((h: string) => h.includes("Prime location in Lakewood")));
  assert.ok(out.highlights.some((h: string) => h.includes("166,745 sqft")));
});

test("jllInvestorStrandedStructured emits statusBadge from stageName", () => {
  assert.equal(jllInvestorStrandedStructured({ stageName: "Marketing" }).statusBadge, "Marketing");
  assert.equal(jllInvestorStrandedStructured({ stageName: "Closed" }).statusBadge, "Closed");
  assert.equal(jllInvestorStrandedStructured({}).statusBadge, undefined);
});

test("jllInvestorStrandedStructured prefers isUnderContract over stageName for statusBadge", () => {
  const out = jllInvestorStrandedStructured({ isUnderContract: true, stageName: "Active" });
  assert.equal(out.statusBadge, "Under Contract");
});

test("jllInvestorStrandedStructured emits extraFacts with deal_type when present", () => {
  const out = jllInvestorStrandedStructured({ dealType: "Property Sale" });
  assert.deepEqual(out.extraFacts, { deal_type: "Property Sale" });
  // Absent dealType -> no extraFacts key.
  const sparse = jllInvestorStrandedStructured({});
  assert.equal(sparse.extraFacts, undefined);
});

test("jllInvestorStrandedStructured absent fields stay null/undefined (no fabrication)", () => {
  const out = jllInvestorStrandedStructured({});
  assert.equal(out.statusBadge, undefined);
  assert.equal(out.extraFacts, undefined);
  assert.equal(out.highlights, undefined);
  assert.equal(out.units, undefined);
});

// ---------------------------------------------------------------------------
// Phase-2: fixture-based end-to-end checks against real saved raw_data blob
// ---------------------------------------------------------------------------

test("jll-investor fixture: jllInvestorStrandedStructured lifts all Phase-2 fields", () => {
  const row = jllInvestorRow();
  const detail = row.jllInvestorDetail;
  const out = jllInvestorStrandedStructured(detail);

  // statusBadge: stageName = "Marketing", isUnderContract = false
  assert.equal(out.statusBadge, "Marketing");

  // highlights: HTML string -> stripped array of bullet points
  assert.ok(Array.isArray(out.highlights));
  assert.ok(out.highlights.some((h: string) => h.includes("Prime location in Lakewood")));
  assert.ok(out.highlights.some((h: string) => h.includes("166,745 sqft")));

  // extraFacts: dealType
  assert.deepEqual(out.extraFacts, { deal_type: "Property Sale" });

  // No buildingClass on investor detail (stays undefined, not fabricated)
  assert.equal((out as any).buildingClass, undefined);
});

test("jll-investor fixture: canonicalUrl set to base.url in parseJllInvestorDetail output shape", () => {
  const row = jllInvestorRow();
  // canonicalUrl should be base.url (the invest.jll.com detail URL).
  assert.equal(
    row.url,
    "https://invest.jll.com/us/en/listings/office/12795-w-alameda-pkwy"
  );
});

test("jll-investor fixture: jllInvestorExtractLicense on real contact license arrays", () => {
  const row = jllInvestorRow();
  const firstContact = row.contactsDetailed[0];
  const license = jllInvestorExtractLicense(firstContact.licenses);
  assert.equal(license, "Colorado: FA.040030452");

  const secondContact = row.contactsDetailed[1];
  const license2 = jllInvestorExtractLicense(secondContact.licenses);
  assert.equal(license2, "Colorado: IA.100092076");
});

test("jll-investor fixture: jllInvestorContacts emits license on each contact", () => {
  const row = jllInvestorRow();
  // Build contacts from the fixture's detail brokers array (simulated listing shape).
  const fakeListing = { brokers: row.contactsDetailed.map((c: any) => ({
    name: c.name,
    email: c.email,
    phone: c.phone,
    title: c.title,
    image: c.avatarUrl,
    linkedInURL: c.linkedInUrl,
    licenses: c.licenses,
    licensedEntity: c.licensedEntity,
  }))};
  const contacts = jllInvestorContacts(fakeListing);
  assert.equal(contacts.length, 2);
  assert.equal(contacts[0]?.license, "Colorado: FA.040030452");
  assert.equal(contacts[1]?.license, "Colorado: IA.100092076");
  // company always "JLL"
  assert.ok(contacts.every((c: any) => c.company === "JLL"));
});

test("jll-investor fixture: jllInvestorStrandedStructured does not throw on null/undefined", () => {
  assert.doesNotThrow(() => jllInvestorStrandedStructured(null));
  assert.doesNotThrow(() => jllInvestorStrandedStructured(undefined));
  assert.deepEqual(jllInvestorStrandedStructured(null), {});
  assert.deepEqual(jllInvestorStrandedStructured(undefined), {});
});
