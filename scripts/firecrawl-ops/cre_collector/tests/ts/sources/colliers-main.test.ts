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
} from "../../../sources/colliers-main.js";
import type { ScrapedDoc } from "../../../types.js";

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

test("colliersMainDetailCachePath returns durable cache location", () => {
  assert.equal(colliersMainDetailCachePath(), "out/cache/colliers-main/detail-cache.jsonl");
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
