import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  buildoutInventoryUrl,
  envBool,
  envInt,
  buildoutCacheSlug,
  buildoutPageCachePath,
  buildoutPageWindow,
  buildoutSlugFromUrl,
  buildoutDetailIframeUrl,
  buildoutAvailableSf,
  buildoutScalarFields,
  BUILDOUT_ENRICH_CONFIG,
} from "../../../sources/buildout.js";

// Fixture path for raw_data blobs (real scrubbed listings from DB).
const __dirname = dirname(fileURLToPath(import.meta.url));
const FIXTURE_PATH = join(__dirname, "../../fixtures/raw_data/buildout.json");

type FixtureRow = {
  _source: string;
  _note: string;
  [key: string]: unknown;
};

function loadFixtures(): FixtureRow[] {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8")) as FixtureRow[];
}

const ENV_KEYS = ["BUILDOUT_PAGE_START", "BUILDOUT_PAGE_END", "BUILDOUT_CACHE_DIR"] as const;

function clearEnv(keys: readonly string[]): void {
  for (const key of keys) delete process.env[key];
}

test("buildoutInventoryUrl includes plugin key and page", () => {
  assert.equal(
    buildoutInventoryUrl("abc123plugin", 7),
    "https://buildout.com/plugins/abc123plugin/inventory.json?page=7"
  );
});

test("envBool recognizes truthy string values", () => {
  process.env.TEST_BUILDOUT_BOOL = "true";
  assert.equal(envBool("TEST_BUILDOUT_BOOL"), true);
  process.env.TEST_BUILDOUT_BOOL = "YES";
  assert.equal(envBool("TEST_BUILDOUT_BOOL"), true);
  process.env.TEST_BUILDOUT_BOOL = "0";
  assert.equal(envBool("TEST_BUILDOUT_BOOL"), false);
  delete process.env.TEST_BUILDOUT_BOOL;
});

test("envInt parses integers and rejects invalid input", () => {
  process.env.TEST_BUILDOUT_INT = "42";
  assert.equal(envInt("TEST_BUILDOUT_INT"), 42);
  process.env.TEST_BUILDOUT_INT = "-3";
  assert.equal(envInt("TEST_BUILDOUT_INT"), 0);
  process.env.TEST_BUILDOUT_INT = "nope";
  assert.equal(envInt("TEST_BUILDOUT_INT"), null);
  delete process.env.TEST_BUILDOUT_INT;
  assert.equal(envInt("TEST_BUILDOUT_INT"), null);
});

test("buildoutCacheSlug slugifies company or uses override", () => {
  assert.equal(buildoutCacheSlug("Lee & Associates", "plugin-key", {}), "lee-and-associates");
  assert.equal(buildoutCacheSlug("SVN", "plugin-key", { cacheSlug: "svn-custom" }), "svn-custom");
});

test("buildoutPageCachePath uses cache dir and padded page", () => {
  process.env.BUILDOUT_CACHE_DIR = "/tmp/buildout-cache-test";
  const path = buildoutPageCachePath("SVN", "plugin-key", 3, {});
  assert.equal(path, "/tmp/buildout-cache-test/svn/page-0003.json");
  delete process.env.BUILDOUT_CACHE_DIR;
});

test("buildoutPageWindow returns null when env unset", () => {
  clearEnv(ENV_KEYS);
  assert.equal(buildoutPageWindow(100), null);
});

test("buildoutPageWindow clamps to page bounds", () => {
  clearEnv(ENV_KEYS);
  process.env.BUILDOUT_PAGE_START = "5";
  process.env.BUILDOUT_PAGE_END = "12";
  assert.deepEqual(buildoutPageWindow(20), { start: 5, end: 12 });
  clearEnv(ENV_KEYS);
});

test("buildoutPageWindow throws on inverted range", () => {
  clearEnv(ENV_KEYS);
  process.env.BUILDOUT_PAGE_START = "10";
  process.env.BUILDOUT_PAGE_END = "2";
  assert.throws(() => buildoutPageWindow(20), /invalid Buildout page window/);
  clearEnv(ENV_KEYS);
});

// --- Tier-B detail iframe resolver (svn / lee-associates) ---

test("buildoutSlugFromUrl extracts propertyId and strips -sale/-lease suffix", () => {
  assert.equal(buildoutSlugFromUrl("https://svn.com/properties/?propertyId=rexall"), "rexall");
  assert.equal(buildoutSlugFromUrl("https://svn.com/properties/?propertyId=1614726-sale"), "1614726");
  assert.equal(buildoutSlugFromUrl("https://svn.com/properties/?propertyId=1614726-lease"), "1614726");
  // No propertyId -> null (the enricher then skips the item).
  assert.equal(buildoutSlugFromUrl("https://svn.com/properties/"), null);
  assert.equal(buildoutSlugFromUrl(null), null);
});

test("buildoutDetailIframeUrl composes the Buildout iframe content URL per source", () => {
  const svn = buildoutDetailIframeUrl("svn", "https://svn.com/properties/?propertyId=rexall");
  assert.equal(
    svn,
    `https://buildout.com/plugins/${BUILDOUT_ENRICH_CONFIG.svn.pluginKey}/svn.com/inventory/rexall?pluginId=0&iframe=true&embedded=true`
  );
  const lee = buildoutDetailIframeUrl(
    "lee-associates",
    "https://www.lee-associates.com/properties/?propertyId=1614726-sale"
  );
  assert.equal(
    lee,
    `https://buildout.com/plugins/${BUILDOUT_ENRICH_CONFIG["lee-associates"].pluginKey}/www.lee-associates.com/inventory/1614726?pluginId=0&iframe=true&embedded=true`
  );
  // Unknown source key or missing slug -> null (no iframe URL to scrape).
  assert.equal(buildoutDetailIframeUrl("unknown", "https://x/?propertyId=a"), null);
  assert.equal(buildoutDetailIframeUrl("svn", "https://svn.com/properties/"), null);
});

test("buildoutAvailableSf parses single and range available-SF attributes", () => {
  assert.deepEqual(buildoutAvailableSf("4,750 SF"), {
    availableSf: 4750,
    minDivisibleSf: 4750,
    maxDivisibleSf: 4750,
  });
  assert.deepEqual(buildoutAvailableSf("175 - 2,396 SF"), {
    availableSf: 175,
    minDivisibleSf: 175,
    maxDivisibleSf: 2396,
  });
  // No SF unit / empty -> nothing lifted.
  assert.deepEqual(buildoutAvailableSf("Contact broker"), {});
  assert.deepEqual(buildoutAvailableSf(null), {});
});

// ---------------------------------------------------------------------------
// Phase-2 scalar lift: buildoutScalarFields
// ---------------------------------------------------------------------------

test("buildoutScalarFields: fixture loads without throwing", () => {
  // Smoke: the fixture file parses and has the expected rows.
  const fixtures = loadFixtures();
  assert.ok(fixtures.length >= 5, "expected at least 5 fixture rows");
  assert.ok(fixtures.some((f) => f._source === "svn"), "SVN rows present");
  assert.ok(fixtures.some((f) => f._source === "lee-associates"), "Lee rows present");
});

test("buildoutScalarFields: SVN lease with NNN lease rate emits leaseRateMin/Type", () => {
  // Fixture 0: SVN lease, "$35 SF/yr (NNN)"
  const fixtures = loadFixtures();
  const row = fixtures.find(
    (f) => f._source === "svn" && (f.leaseRateText as string | undefined) === "$35 SF/yr (NNN)"
  );
  assert.ok(row, "SVN NNN lease fixture row found");
  const result = buildoutScalarFields({
    url: row!.url as string,
    leaseRateText: row!.leaseRateText as string,
    sizeText: row!.sizeText as string,
    salePriceText: null,
    underContract: false,
    transactionMode: "lease",
  });
  // leaseRateMin = 35, type = "nnn"; max is null (single value)
  assert.equal(result.leaseRateMin, 35);
  assert.equal(result.leaseRateMax, null);
  assert.equal(result.leaseRateType, "nnn");
  // sizeText "12,700 SF Bldg" has no acres -> lotSf is null
  assert.equal(result.lotSf, null);
  // statusBadge is null (not under contract)
  assert.equal(result.statusBadge, null);
  // canonicalUrl is the listing URL
  assert.equal(result.canonicalUrl, row!.url as string);
  // salePricePsfGuard is false (lease row, no sale price text)
  assert.equal(result.salePricePsfGuard, false);
});

test("buildoutScalarFields: SVN sale with underContract=true emits statusBadge", () => {
  // Fixture 1: SVN sale, underContract=true
  const fixtures = loadFixtures();
  const row = fixtures.find(
    (f) => f._source === "svn" && f.underContract === true
  );
  assert.ok(row, "SVN underContract fixture row found");
  const result = buildoutScalarFields({
    url: row!.url as string,
    leaseRateText: null,
    sizeText: row!.sizeText as string,
    salePriceText: row!.salePriceText as string | null,
    underContract: true,
    transactionMode: "sale",
  });
  assert.equal(result.statusBadge, "under_contract");
  // No lease rate text -> all null
  assert.equal(result.leaseRateMin, null);
  assert.equal(result.leaseRateMax, null);
  assert.equal(result.leaseRateType, null);
  // "3,009 SF" -> no acres -> lotSf null
  assert.equal(result.lotSf, null);
});

test("buildoutScalarFields: SVN acreage sizeText routes to lotSf, not sizeSf", () => {
  // Fixture 2: SVN sale, sizeText "9.15 Acres"
  const fixtures = loadFixtures();
  const row = fixtures.find(
    (f) => f._source === "svn" && typeof f.sizeText === "string" && (f.sizeText as string).includes("Acres")
  );
  assert.ok(row, "SVN acres fixture row found");
  const result = buildoutScalarFields({
    url: row!.url as string,
    leaseRateText: null,
    sizeText: row!.sizeText as string,
    salePriceText: null,
    underContract: false,
  });
  // 9.15 acres * 43560 = 398574 SF
  assert.ok(result.lotSf !== null, "lotSf should be set for an Acres sizeText");
  assert.ok(Math.abs(result.lotSf! - 9.15 * 43560) < 1, `lotSf should be ~${9.15 * 43560}, got ${result.lotSf}`);
  // statusBadge null (not under contract)
  assert.equal(result.statusBadge, null);
});

test("buildoutScalarFields: Lee sale with per-SF salePriceText triggers DQ guard", () => {
  // Fixture 3: Lee sale, salePriceText "$12.00 /SF" (salePriceUsd=12 in DB is per-SF, not absolute)
  const fixtures = loadFixtures();
  const row = fixtures.find(
    (f) =>
      f._source === "lee-associates" &&
      typeof f.salePriceText === "string" &&
      (f.salePriceText as string).includes("/SF")
  );
  assert.ok(row, "Lee per-SF salePriceText fixture row found");
  const result = buildoutScalarFields({
    url: row!.url as string,
    leaseRateText: null,
    sizeText: row!.sizeText as string,
    salePriceText: row!.salePriceText as string,
    underContract: false,
    transactionMode: "sale",
  });
  // salePricePsfGuard must be TRUE: the value is per-SF, not an absolute price
  assert.equal(result.salePricePsfGuard, true, "isPerSfText must detect the /SF suffix");
  // lotSf: "1.88 Acres" -> ~81892.8 SF
  assert.ok(result.lotSf !== null, "lotSf should be set for 1.88 Acres Lee listing");
  assert.ok(Math.abs(result.lotSf! - 1.88 * 43560) < 1, `lotSf ~${1.88 * 43560}`);
  // No lease rate text
  assert.equal(result.leaseRateMin, null);
});

test("buildoutScalarFields: Lee lease with NNN rate emits leaseRateMin/Type", () => {
  // Fixture 4: Lee lease, leaseRateText "$12 SF/yr (NNN)"
  const fixtures = loadFixtures();
  const row = fixtures.find(
    (f) =>
      f._source === "lee-associates" &&
      typeof f.leaseRateText === "string" &&
      (f.leaseRateText as string).includes("SF/yr")
  );
  assert.ok(row, "Lee NNN lease fixture row found");
  const result = buildoutScalarFields({
    url: row!.url as string,
    leaseRateText: row!.leaseRateText as string,
    sizeText: row!.sizeText as string,
    salePriceText: null,
    underContract: false,
    transactionMode: "lease",
  });
  assert.equal(result.leaseRateMin, 12);
  assert.equal(result.leaseRateMax, null);
  assert.equal(result.leaseRateType, "nnn");
  // "11,897 SF Bldg" -> no acres -> lotSf null
  assert.equal(result.lotSf, null);
  // salePricePsfGuard false (no sale price text)
  assert.equal(result.salePricePsfGuard, false);
});

test("buildoutScalarFields: absent fields stay null, function never throws on null input", () => {
  // Defensive: all inputs null
  assert.doesNotThrow(() => {
    const result = buildoutScalarFields({
      url: null,
      leaseRateText: null,
      sizeText: null,
      salePriceText: null,
      underContract: false,
    });
    assert.equal(result.canonicalUrl, null);
    assert.equal(result.leaseRateMin, null);
    assert.equal(result.leaseRateMax, null);
    assert.equal(result.leaseRateType, null);
    assert.equal(result.lotSf, null);
    assert.equal(result.statusBadge, null);
    assert.equal(result.salePricePsfGuard, false);
  });
});

test("buildoutScalarFields: suite-size mis-range ($2.50 - 250 SF/month) produces null rate", () => {
  // Contract golden vector row 10: this Buildout-specific case must yield null min/max.
  const result = buildoutScalarFields({
    url: "https://svn.com/properties/?propertyId=test-suite-size",
    leaseRateText: "$2.50 - 250 SF/month",
    sizeText: null,
    salePriceText: null,
    underContract: false,
  });
  assert.equal(result.leaseRateMin, null, "suite-size mis-range should produce null min");
  assert.equal(result.leaseRateMax, null, "suite-size mis-range should produce null max");
});

test("buildoutScalarFields: range lease rate ($35 - 45 SF/yr NNN) emits min and max", () => {
  // Contract golden vector row 3.
  const result = buildoutScalarFields({
    url: "https://svn.com/properties/?propertyId=test-range",
    leaseRateText: "$35 - 45 SF/yr (NNN)",
    sizeText: null,
    salePriceText: null,
    underContract: false,
  });
  assert.equal(result.leaseRateMin, 35);
  assert.equal(result.leaseRateMax, 45);
  assert.equal(result.leaseRateType, "nnn");
});

test("buildoutScalarFields: salePricePsfGuard false for absolute price text", () => {
  // A real dollar amount (not per-SF) must NOT trigger the guard.
  const result = buildoutScalarFields({
    url: "https://svn.com/properties/?propertyId=test-abs",
    leaseRateText: null,
    sizeText: null,
    salePriceText: "$1,500,000",
    underContract: false,
    transactionMode: "sale",
  });
  assert.equal(result.salePricePsfGuard, false, "absolute price must not trigger per-SF guard");
});
