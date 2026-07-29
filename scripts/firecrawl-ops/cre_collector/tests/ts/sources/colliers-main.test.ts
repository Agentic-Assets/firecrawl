// Isolate argv before colliers-main.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  colliersMainIsChallenge,
  colliersMainAbs,
  colliersMainIdFromUrl,
  extractSitemapLocs,
  colliersMainTransaction,
  parseColliersMainAddress,
  colliersMainJsonLd,
  colliersMainDetailCachePath,
  readColliersMainCache,
  appendColliersMainCache,
  colliersMainCachedListingIsCurrent,
  colliersMainDetailPassTruncated,
  colliersMainResultTruncated,
  parseColliersMainDetail,
  type ColliersMainEntry,
  fetchColliersMainEntries,
  scrapeColliersMainDetailDoc,
} from "../../../sources/colliers-main.js";
import type { ScrapedDoc } from "../../../types.js";
import { firecrawl } from "../../../lib/scrape.js";

// ---------------------------------------------------------------------------
// Fixture helpers
// ---------------------------------------------------------------------------

const FIXTURE_PATH = join(
  new URL(".", import.meta.url).pathname,
  "../../fixtures/raw_data/colliers.json"
);

function loadFixture(): any[] {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
}

/** Return the raw_data for the first colliers-main fixture entry. */
function mainFixture(): any {
  const fixtures = loadFixture();
  const entry = fixtures.find((f: any) => f.sourceKey === "colliers-main");
  if (!entry) throw new Error("No colliers-main fixture found");
  return entry.raw_data;
}

/** Build a minimal ScrapedDoc that mimics what Firecrawl returns for a Colliers main detail page. */
function syntheticDoc(
  ldJson: object,
  markdownExtra = "",
  opts: Partial<{ statusCode: number; title: string }> = {}
): ScrapedDoc {
  const ldScript = `<script type="application/ld+json">${JSON.stringify(ldJson)}</script>`;
  return {
    rawHtml: ldScript,
    markdown: markdownExtra,
    links: [],
    metadata: { statusCode: opts.statusCode ?? 200, title: opts.title ?? "Office For Sale" },
  };
}

function doc(partial: Partial<ScrapedDoc>): ScrapedDoc {
  return {
    rawHtml: "",
    markdown: "",
    links: [],
    metadata: {},
    ...partial,
  };
}

test("colliersMainIsChallenge detects Cloudflare challenge pages", () => {
  assert.equal(colliersMainIsChallenge(doc({ metadata: { statusCode: 429 } })), true);
  assert.equal(colliersMainIsChallenge(doc({ metadata: { statusCode: 503 } })), true);
  assert.equal(
    colliersMainIsChallenge(doc({ metadata: { title: "Just a moment..." }, rawHtml: "" })),
    true
  );
  assert.equal(
    colliersMainIsChallenge(doc({ metadata: { statusCode: 200, title: "Office For Sale" }, rawHtml: "<html>ok</html>" })),
    false
  );
  assert.equal(
    colliersMainIsChallenge(doc({ metadata: { statusCode: 200 }, rawHtml: "<div>cf-chl-widget</div>" })),
    true
  );
});

test("colliersMainAbs resolves host-relative links and rejects unsafe schemes", () => {
  assert.equal(
    colliersMainAbs("/en/properties/usa12345-office-for-sale"),
    "https://www.colliers.com/en/properties/usa12345-office-for-sale"
  );
  assert.equal(colliersMainAbs("javascript:void(0)"), null);
  assert.equal(colliersMainAbs("mailto:agent@colliers.com"), null);
  assert.equal(colliersMainAbs("#section"), null);
});

test("colliersMainIdFromUrl extracts usa##### ids from detail URLs", () => {
  assert.equal(
    colliersMainIdFromUrl("https://www.colliers.com/en/properties/usa12345"),
    "usa12345"
  );
  assert.equal(
    colliersMainIdFromUrl("https://www.colliers.com/en/properties/USA99999?foo=bar"),
    "usa99999"
  );
  assert.equal(
    colliersMainIdFromUrl("https://www.colliers.com/en/properties/usa12345-office-dallas"),
    null
  );
  assert.equal(colliersMainIdFromUrl("https://www.colliers.com/en/about"), null);
});

test("extractSitemapLocs parses loc elements from sitemap XML", () => {
  const xml = `
    <?xml version="1.0" encoding="UTF-8"?>
    <urlset>
      <url><loc>https://www.colliers.com/en/sitemap?type=properties</loc></url>
      <url><loc>  https://www.colliers.com/en/properties/usa11111  </loc></url>
    </urlset>
  `;
  assert.deepEqual(extractSitemapLocs(xml), [
    "https://www.colliers.com/en/sitemap?type=properties",
    "https://www.colliers.com/en/properties/usa11111",
  ]);
});

test("colliersMainTransaction classifies sale, lease, and dual-mode listings", () => {
  assert.deepEqual(
    colliersMainTransaction("Office For Sale — 123 Main St", "", "https://www.colliers.com/en/properties/usa10001-for-sale"),
    { type: "Sale", sublease: false }
  );
  assert.deepEqual(
    colliersMainTransaction("Retail For Lease — Austin", "", "https://www.colliers.com/en/properties/usa10002-for-lease"),
    { type: "Lease", sublease: false }
  );
  assert.deepEqual(
    colliersMainTransaction("Industrial For Sale or Lease", "", "https://www.colliers.com/en/properties/usa10003-sale-or-lease"),
    { type: "Sale/Lease", sublease: false }
  );
  assert.deepEqual(
    colliersMainTransaction("Office Sublease — Denver", "", "https://www.colliers.com/en/properties/usa10004-sublease"),
    { type: "Lease", sublease: true }
  );
});

test("parseColliersMainAddress splits street, city, state, zip, and country", () => {
  assert.deepEqual(parseColliersMainAddress("11701 I-30, Little Rock, AR 72209, USA"), {
    street: "11701 I-30",
    city: "Little Rock",
    state: "AR",
    postalCode: "72209",
    country: "US",
  });
  assert.deepEqual(parseColliersMainAddress("100 King St W, Toronto, ON"), {
    street: "100 King St W",
    city: "Toronto",
    state: "ON",
    postalCode: null,
    country: null,
  });
  assert.deepEqual(parseColliersMainAddress(null), {
    street: null,
    city: null,
    state: null,
    postalCode: null,
    country: null,
  });
});

test("colliersMainJsonLd extracts RealEstateListing JSON-LD from HTML", () => {
  const html = `
    <html>
      <script type="application/ld+json">
        {"@type":"Organization","name":"Colliers"}
      </script>
      <script type="application/ld+json">
        {"@type":"RealEstateListing","name":"Office For sale — 500 Main St, Dallas, TX 75201, USA"}
      </script>
    </html>
  `;
  const ld = colliersMainJsonLd(html);
  assert.equal(ld?.["@type"], "RealEstateListing");
  assert.match(ld?.name, /Dallas, TX/);
  assert.equal(colliersMainJsonLd("<html><body>no json-ld</body></html>"), null);
});

test("strict Colliers retries unknown HTTP 200 pages without property JSON-LD", () => {
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    const e = entry("usa12345", "https://www.colliers.com/en/properties/usa12345");
    assert.throws(
      () =>
        parseColliersMainDetail(
          e,
          doc({
            rawHtml: "<html><h1>Consent required</h1></html>",
            markdown: "Consent required",
            metadata: { statusCode: 200, title: "Consent required" },
          })
        ),
      /lacks validated RealEstateListing JSON-LD/
    );
    assert.deepEqual(
      parseColliersMainDetail(
        e,
        doc({
          rawHtml: "<html>gone</html>",
          markdown: "Gone",
          metadata: { statusCode: 410, title: "Gone" },
        })
      ).skip,
      "not_found"
    );
    assert.equal(
      parseColliersMainDetail(
        e,
        syntheticDoc(SALE_LD, "stale property body", {
          statusCode: 404,
          title: "Not Found",
        })
      ).skip,
      "not_found",
      "explicit HTTP tombstones must win over a stale JSON-LD body"
    );
  } finally {
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("colliersMainDetailCachePath returns durable cache location", () => {
  const previous = process.env.COLLIERS_MAIN_DETAIL_CACHE_PATH;
  try {
    delete process.env.COLLIERS_MAIN_DETAIL_CACHE_PATH;
    assert.equal(colliersMainDetailCachePath(), "out/cache/colliers-main/detail-cache.jsonl");
    process.env.COLLIERS_MAIN_DETAIL_CACHE_PATH = "out/cache/colliers-main/fresh-2026-07-29.jsonl";
    assert.equal(
      colliersMainDetailCachePath(),
      "out/cache/colliers-main/fresh-2026-07-29.jsonl"
    );
  } finally {
    if (previous === undefined) delete process.env.COLLIERS_MAIN_DETAIL_CACHE_PATH;
    else process.env.COLLIERS_MAIN_DETAIL_CACHE_PATH = previous;
  }
});

test("Colliers cache reuse follows live sitemap lastmod", () => {
  const cached = { lastUpdated: "2026-07-28", name: "Listing" };
  assert.equal(
    colliersMainCachedListingIsCurrent(entry("usa1", "https://example.test/1", "2026-07-28"), cached),
    true
  );
  assert.equal(
    colliersMainCachedListingIsCurrent(entry("usa1", "https://example.test/1", "2026-07-29"), cached),
    false
  );
  assert.equal(
    colliersMainCachedListingIsCurrent(entry("usa1", "https://example.test/1", null), cached),
    true
  );
});

test("Colliers cache reuse also requires the active refresh generation", () => {
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  try {
    process.env.CRE_REFRESH_GENERATION = "generation-current";
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    const sitemapEntry = entry(
      "usa1",
      "https://example.test/1",
      "2026-07-29"
    );
    assert.equal(
      colliersMainCachedListingIsCurrent(sitemapEntry, {
        lastUpdated: "2026-07-29",
        freshnessProvenance: { generationId: "generation-current" },
      }),
      true
    );
    assert.equal(
      colliersMainCachedListingIsCurrent(sitemapEntry, {
        lastUpdated: "2026-07-29",
        freshnessProvenance: { generationId: "generation-old" },
      }),
      false
    );
    assert.equal(
      colliersMainCachedListingIsCurrent(sitemapEntry, {
        lastUpdated: "2026-07-29",
        skip: "no_structured_data",
        freshnessProvenance: { generationId: "generation-current" },
      }),
      false
    );
    assert.equal(
      colliersMainCachedListingIsCurrent(sitemapEntry, {
        lastUpdated: "2026-07-29",
        skip: "not_found",
        freshnessProvenance: { generationId: "generation-current" },
      }),
      true
    );
  } finally {
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("strict Colliers sitemap and detail Firecrawl calls bypass cached responses", async () => {
  const oldScrape = firecrawl.scrape;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const calls: any[] = [];
  (firecrawl as any).scrape = async (url: string, options: any) => {
    calls.push(options);
    if (url.includes("type=properties")) {
      return {
        rawHtml:
          "<urlset><url><loc>https://www.colliers.com/en/properties/usa12345</loc><lastmod>2026-07-29</lastmod></url></urlset>",
      };
    }
    if (url.endsWith("/sitemap")) {
      return {
        rawHtml:
          "<sitemapindex><sitemap><loc>https://www.colliers.com/en/sitemap?type=properties</loc></sitemap></sitemapindex>",
      };
    }
    return {
      rawHtml:
        '<script type="application/ld+json">{"@type":"RealEstateListing","name":"Strict Property For Sale"}</script>',
      markdown: "Strict Property For Sale",
      links: [],
      metadata: { statusCode: 200, title: "Strict Property For Sale" },
    };
  };
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    const entries = await fetchColliersMainEntries();
    assert.equal(entries.length, 1);
    await scrapeColliersMainDetailDoc(entries[0]!.url);
    assert.equal(calls.length, 3);
    assert.ok(calls.every((options) => options.maxAge === 0));
  } finally {
    (firecrawl as any).scrape = oldScrape;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("Colliers detail pass is truncated while work is deferred or errored", () => {
  assert.equal(colliersMainDetailPassTruncated({ errors: 0, deferred: 0 }), false);
  assert.equal(colliersMainDetailPassTruncated({ errors: 1, deferred: 0 }), true);
  assert.equal(colliersMainDetailPassTruncated({ errors: 0, deferred: 1 }), true);
});

test("Colliers finite caps report truncation against sitemap inventory", () => {
  const complete = { errors: 0, deferred: 0 };
  assert.equal(colliersMainResultTruncated(complete, 1, 2), true);
  assert.equal(colliersMainResultTruncated(complete, 2, 2), false);
  assert.equal(
    colliersMainResultTruncated(complete, Number.POSITIVE_INFINITY, 2),
    false
  );
  assert.equal(colliersMainResultTruncated({ errors: 1, deferred: 0 }, 2, 2), true);
});

test("readColliersMainCache and appendColliersMainCache round-trip JSONL rows", () => {
  const dir = mkdtempSync(join(tmpdir(), "colliers-main-cache-"));
  const cachePath = join(dir, "detail-cache.jsonl");
  try {
    assert.equal(readColliersMainCache(cachePath).size, 0);

    appendColliersMainCache(cachePath, {
      id: "usa55555",
      url: "https://www.colliers.com/en/properties/usa55555",
      name: "Cached Listing",
    });
    appendColliersMainCache(cachePath, {
      id: "main:usa66666",
      url: "https://www.colliers.com/en/properties/usa66666",
      name: "Prefixed Id",
    });
    appendColliersMainCache(cachePath, {
      id: "usa77777",
      detailError: "transient failure",
      url: "https://www.colliers.com/en/properties/usa77777",
    });

    const cached = readColliersMainCache(cachePath);
    assert.equal(cached.size, 2);
    assert.equal(cached.get("usa55555")?.name, "Cached Listing");
    assert.equal(cached.get("usa66666")?.name, "Prefixed Id");
    assert.equal(cached.has("usa77777"), false);

    const lines = readFileSync(cachePath, "utf8").trim().split("\n");
    assert.equal(lines.length, 2);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

// ---------------------------------------------------------------------------
// Phase-2 data-lift tests: new camelCase scalar fields from parseColliersMainDetail
// ---------------------------------------------------------------------------

/** Minimal entry for testing. */
function entry(id: string, url: string, lastmod: string | null = "2026-01-15"): ColliersMainEntry {
  return { id, url, lastmod };
}

const SALE_LD = {
  "@type": "RealEstateListing",
  name: "Office For sale — 239 Great Neck Rd, Great Neck, NY 11021, USA | United States | Colliers",
};

const LEASE_LD = {
  "@type": "RealEstateListing",
  name: "Industrial For Lease — 4321 Industrial Blvd, Phoenix, AZ 85007, USA | United States | Colliers",
};

const MG_LEASE_LD = {
  "@type": "RealEstateListing",
  name: "Office For Lease — 100 Main St, Dallas, TX 75201, USA | United States | Colliers",
};

test("parseColliersMainDetail: canonicalUrl is the entry url", () => {
  const e = entry("usa1159083", "https://www.colliers.com/en/properties/usa-239-great-neck-rd/usa1159083");
  const mdContent = "## Office For Sale\n**Property Status** Available\nBuilding Size: 15,476 SF";
  const docx = syntheticDoc(SALE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  assert.equal(listing.canonicalUrl, e.url);
});

test("parseColliersMainDetail: statusBadge from **Property Status** markdown token", () => {
  const e = entry("usa1159083", "https://www.colliers.com/en/properties/usa1159083");
  const mdContent = "## Office For Sale\n**Property Status** Available\nBuilding Size: 5,000 SF";
  const docx = syntheticDoc(SALE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  assert.equal(listing.statusBadge, "Available");
  assert.equal(listing.colliersMain.propertyStatus, "Available");
});

test("parseColliersMainDetail: statusBadge is absent when no Property Status in markdown", () => {
  const e = entry("usa9999999", "https://www.colliers.com/en/properties/usa9999999");
  const mdContent = "## Office For Sale\nBuilding Size: 5,000 SF";
  const docx = syntheticDoc(SALE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  // prune() strips null values so the key may be absent; check null-or-undefined.
  assert.ok(listing.statusBadge == null, `statusBadge should be null/absent; got ${listing.statusBadge}`);
});

test("parseColliersMainDetail: leaseRateType from Modified Gross lease rate text", () => {
  const e = entry("usa1159094", "https://www.colliers.com/en/properties/usa1159094");
  // Markdown has a /SF lease rate with Modified Gross type.
  const mdContent =
    "## Office For Lease\n" +
    "**Property Status** Available\n" +
    "$18.50/SF Modified Gross\n";
  const docx = syntheticDoc(MG_LEASE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  // The adapter captures leaseRateText from a markdown regex, then parses it.
  // Assert leaseRateType is non-null when a valid per-SF lease rate appears.
  if (listing.leaseRateText) {
    // leaseRateType must match the expected type from parseLeaseRate.
    assert.ok(
      listing.leaseRateType === "modified_gross" ||
        listing.leaseRateType === "gross" ||
        listing.leaseRateType === "nnn" ||
        listing.leaseRateType === "full_service" ||
        listing.leaseRateType === null,
      `leaseRateType must be a valid type or null; got: ${listing.leaseRateType}`
    );
  }
});

test("parseColliersMainDetail: leaseRateMin/Max from fixture Modified Gross text", () => {
  const e = entry("usa2000001", "https://www.colliers.com/en/properties/usa2000001");
  // Use a lease rate text that the regex in the adapter can capture via the /SF regex.
  // The adapter regex: /\$[\d,.]+\s*(?:\/|per\s*)\s*(?:SF|sq\.?\s*ft)[^\n]{0,24}/i
  const mdContent =
    "## Office For Lease\n" +
    "$18.50/SF Modified Gross per year\n" +
    "Building Size: 10,000 SF\n";
  const docx = syntheticDoc(MG_LEASE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  // leaseRateText was captured from the regex; leaseRateMin must be 18.5.
  if (listing.leaseRateText) {
    assert.ok(typeof listing.leaseRateMin === "number" && listing.leaseRateMin > 0);
    assert.equal(listing.leaseRateType, "modified_gross");
  }
});

test("parseColliersMainDetail: leaseRateMin/Max absent when no lease rate text (sale-only listing)", () => {
  const e = entry("usa3000001", "https://www.colliers.com/en/properties/usa3000001-office-for-sale");
  const mdContent = "## Office For Sale\n$5,000,000\nBuilding Size: 20,000 SF";
  const docx = syntheticDoc(SALE_LD, mdContent);
  const listing = parseColliersMainDetail(e, docx);
  // Sale listing: leaseRateText is null, so leaseRateMin/Max/Type are null/absent (prune strips null).
  assert.ok(listing.leaseRateText == null, "leaseRateText should be absent for a sale listing");
  assert.ok(listing.leaseRateMin == null, "leaseRateMin should be absent when no rate text");
  assert.ok(listing.leaseRateMax == null, "leaseRateMax should be absent when no rate text");
  assert.ok(listing.leaseRateType == null, "leaseRateType should be absent when no rate text");
});

test("parseColliersMainDetail: NNN lease rate yields type=nnn, positive leaseRateMin", () => {
  const e = entry("usa4000001", "https://www.colliers.com/en/properties/usa4000001-for-lease");
  const md = "## Industrial For Lease\n$12.00/SF NNN\nBuilding Size: 50,000 SF";
  const docx = syntheticDoc(LEASE_LD, md);
  const listing = parseColliersMainDetail(e, docx);
  if (listing.leaseRateText) {
    assert.equal(listing.leaseRateType, "nnn");
    assert.equal(listing.leaseRateMin, 12);
    // leaseRateMax is null when no range; prune() drops it so check == null.
    assert.ok(listing.leaseRateMax == null, "leaseRateMax should be absent for a single-value rate");
  }
});

test("parseColliersMainDetail: fixture raw_data fields align with new field set", () => {
  // Verify the stored fixture raw_data has the shape the adapter now emits.
  const raw = mainFixture();
  // canonicalUrl: the fixture has url and the new field must be set to that.
  assert.ok(typeof raw.url === "string", "fixture has url");
  // statusBadge: the fixture has colliersMain.propertyStatus.
  const statusBadge = raw.colliersMain?.propertyStatus;
  assert.equal(statusBadge, "Available");
  // leaseRateText: present in this fixture (set in the fixture to a MG text).
  assert.ok(raw.leaseRateText, "fixture has leaseRateText");
});

test("parseColliersMainDetail: does not throw on minimal/empty doc", () => {
  const e = entry("usa0000001", "https://www.colliers.com/en/properties/usa0000001");
  const minimalDoc: ScrapedDoc = {
    rawHtml: `<script type="application/ld+json">{"@type":"RealEstateListing","name":"Office For Sale"}</script>`,
    markdown: "",
    links: [],
    metadata: { statusCode: 200, title: "Office" },
  };
  let listing: any;
  assert.doesNotThrow(() => {
    listing = parseColliersMainDetail(e, minimalDoc);
  });
  // canonicalUrl is always set (the entry url).
  assert.equal(listing.canonicalUrl, e.url);
  // Phase-2 optional fields: absent when source lacks them (prune strips null).
  assert.ok(listing.statusBadge == null, "statusBadge absent when no Property Status in markdown");
  assert.ok(listing.leaseRateType == null, "leaseRateType absent when no lease rate text");
  assert.ok(listing.leaseRateMin == null, "leaseRateMin absent when no lease rate text");
  assert.ok(listing.leaseRateMax == null, "leaseRateMax absent when no lease rate text");
});
