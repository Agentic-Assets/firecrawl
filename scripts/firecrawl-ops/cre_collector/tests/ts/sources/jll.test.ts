// Isolate argv before jll.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
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
  jllDetailCachePath,
  readJllDetailCache,
  writeJllDetailCache,
} from "../../../sources/jll.js";

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
      metadata: { title: "Cache Test" },
    };
    writeJllDetailCache(url, doc);
    assert.ok(existsSync(path));

    const cached = readJllDetailCache(url);
    assert.equal(cached?.rawHtml, doc.rawHtml);
    assert.equal(cached?.markdown, doc.markdown);
    assert.deepEqual(cached?.links, doc.links);

    const onDisk = JSON.parse(readFileSync(path, "utf8"));
    assert.equal(onDisk.url, normalizedJllListingUrl(url));
    assert.ok(onDisk.cachedAt);
  } finally {
    if (prev === undefined) delete process.env.JLL_DETAIL_CACHE_DIR;
    else process.env.JLL_DETAIL_CACHE_DIR = prev;
    rmSync(cacheDir, { recursive: true, force: true });
  }
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
