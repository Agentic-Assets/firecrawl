import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  buildoutInventoryUrl,
  buildoutRefreshPageCache,
  buildoutPageCachePolicy,
  buildoutQueryFingerprint,
  requireCompleteBuildoutInventory,
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
  readBuildoutPageCache,
  writeBuildoutPageCache,
  fetchBuildoutInventoryPage,
  enrichBuildoutDetail,
  assertBuildoutInventoryPage,
  assertBuildoutInventoryReconciled,
  buildoutInventory,
  buildoutCache,
  buildoutFailureCache,
  buildoutCapTruncated,
  BUILDOUT_STABLE_INVENTORY_SORT,
  BUILDOUT_SOURCE_INVENTORY_OPTS,
} from "../../../sources/buildout.js";
import { firecrawl } from "../../../lib/scrape.js";

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

const ENV_KEYS = [
  "BUILDOUT_PAGE_START",
  "BUILDOUT_PAGE_END",
  "BUILDOUT_CACHE_DIR",
  "BUILDOUT_REFRESH_PAGE_CACHE",
  "BUILDOUT_USE_PAGE_CACHE",
  "BUILDOUT_CACHE_ONLY",
  "BUILDOUT_ASSEMBLE_FROM_CACHE",
] as const;

function clearEnv(keys: readonly string[]): void {
  for (const key of keys) delete process.env[key];
}

test("buildoutInventoryUrl includes plugin key and page", () => {
  assert.equal(BUILDOUT_STABLE_INVENTORY_SORT, "created_at asc, id asc");
  assert.equal(
    buildoutInventoryUrl("abc123plugin", 7),
    "https://buildout.com/plugins/abc123plugin/inventory.json?page=7"
  );
  assert.equal(
    buildoutInventoryUrl("abc123plugin", 7, BUILDOUT_STABLE_INVENTORY_SORT),
    "https://buildout.com/plugins/abc123plugin/inventory.json?page=7&q%5Bs%5D%5B%5D=created_at+asc%2C+id+asc"
  );
});

test("both strict Buildout sources use the stable composite inventory sort", () => {
  assert.equal(
    BUILDOUT_SOURCE_INVENTORY_OPTS.svn.inventorySort,
    BUILDOUT_STABLE_INVENTORY_SORT
  );
  assert.equal(
    BUILDOUT_SOURCE_INVENTORY_OPTS["lee-associates"].inventorySort,
    BUILDOUT_STABLE_INVENTORY_SORT
  );
  assert.equal(BUILDOUT_SOURCE_INVENTORY_OPTS.svn.requireCompletePages, true);
  assert.equal(
    BUILDOUT_SOURCE_INVENTORY_OPTS["lee-associates"].requireCompletePages,
    true
  );
});

test("Buildout query fingerprint separates inventory sort contracts", () => {
  assert.equal(buildoutQueryFingerprint({}), '{"inventorySort":null}');
  assert.notEqual(
    buildoutQueryFingerprint({ inventorySort: "created_at asc" }),
    buildoutQueryFingerprint({ inventorySort: "created_at asc, id asc" })
  );
});

test("Buildout finite caps report truncation against known eligible inventory", () => {
  assert.equal(buildoutCapTruncated(1, 1, 2), true);
  assert.equal(buildoutCapTruncated(2, 2, 2), false);
  assert.equal(buildoutCapTruncated(Number.POSITIVE_INFINITY, 2, 3), false);
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

test("Buildout page cache is admitted only within its refresh generation", () => {
  const cacheDir = mkdtempSync(join(tmpdir(), "buildout-generation-cache-"));
  const oldDir = process.env.BUILDOUT_CACHE_DIR;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  try {
    process.env.BUILDOUT_CACHE_DIR = cacheDir;
    process.env.CRE_REFRESH_GENERATION = "generation-a";
    const data = { inventory: [{ id: 1 }], meta: { total: 1, limit: 30 } };
    writeBuildoutPageCache("SVN", "plugin-key", 0, {}, data);
    assert.equal(readBuildoutPageCache("SVN", "plugin-key", 0, {})?.inventory.length, 1);
    process.env.CRE_REFRESH_GENERATION = "generation-b";
    assert.equal(readBuildoutPageCache("SVN", "plugin-key", 0, {}), null);
  } finally {
    if (oldDir === undefined) delete process.env.BUILDOUT_CACHE_DIR;
    else process.env.BUILDOUT_CACHE_DIR = oldDir;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
    rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("Buildout page cache rejects a different query contract", () => {
  const cacheDir = mkdtempSync(join(tmpdir(), "buildout-query-cache-"));
  const oldDir = process.env.BUILDOUT_CACHE_DIR;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  try {
    process.env.BUILDOUT_CACHE_DIR = cacheDir;
    process.env.CRE_REFRESH_GENERATION = "generation-query";
    const data = { inventory: [{ id: 1 }], meta: { total: 1, limit: 30 } };
    const original = { inventorySort: "created_at asc" };
    writeBuildoutPageCache("Lee", "plugin-key", 0, original, data);
    assert.ok(readBuildoutPageCache("Lee", "plugin-key", 0, original));
    assert.equal(
      readBuildoutPageCache(
        "Lee",
        "plugin-key",
        0,
        { inventorySort: "created_at asc, id asc" }
      ),
      null
    );
  } finally {
    if (oldDir === undefined) delete process.env.BUILDOUT_CACHE_DIR;
    else process.env.BUILDOUT_CACHE_DIR = oldDir;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
    rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("strict Buildout Firecrawl fallback bypasses cached responses", async () => {
  const oldScrape = firecrawl.scrape;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const calls: any[] = [];
  (firecrawl as any).scrape = async (_url: string, options: any) => {
    calls.push(options);
    return {
      rawHtml: JSON.stringify({
        inventory: [{ id: 1 }],
        meta: { total: 1, limit: 30 },
      }),
    };
  };
  try {
    clearEnv(ENV_KEYS);
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    const result = await fetchBuildoutInventoryPage(
      "SVN",
      "strict-plugin-key",
      0,
      {}
    );
    const detail = await enrichBuildoutDetail(
      "svn",
      "https://svn.com/properties/?propertyId=1-sale"
    );
    assert.equal(result.inventory.length, 1);
    assert.ok(detail);
    assert.equal(calls.length, 2);
    assert.ok(calls.every((options) => options.maxAge === 0));
  } finally {
    (firecrawl as any).scrape = oldScrape;
    clearEnv(ENV_KEYS);
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("strict Buildout inventory pages require coherent integer metadata and exact page shape", () => {
  assert.throws(
    () => assertBuildoutInventoryPage({ inventory: [{ id: 1 }] }, 0, null, true),
    /valid integer meta\.total/
  );
  assert.throws(
    () =>
      assertBuildoutInventoryPage(
        { inventory: [{ id: 1 }], meta: { total: 2, limit: 0 } },
        0,
        null,
        true
      ),
    /valid positive integer meta\.limit/
  );
  assert.throws(
    () =>
      assertBuildoutInventoryPage(
        { inventory: null, meta: { total: 1, limit: 30 } },
        0,
        null,
        true
      ),
    /inventory array/
  );
  assert.throws(
    () =>
      assertBuildoutInventoryPage(
        { inventory: [{ id: 2 }], meta: { total: 31, limit: 30 } },
        1,
        { total: 30, limit: 30 },
        true
      ),
    /metadata changed/
  );
  assert.throws(
    () =>
      assertBuildoutInventoryPage(
        { inventory: [{ id: 1 }], meta: { total: 31, limit: 30 } },
        0,
        null,
        true
      ),
    /expected 30 inventory rows/
  );
});

test("complete-page Buildout runs stay strict without the freshness environment", () => {
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  try {
    delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    assert.equal(requireCompleteBuildoutInventory({}), false);
    assert.equal(requireCompleteBuildoutInventory({ requireCompletePages: true }), true);
  } finally {
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("strict Buildout reconciliation rejects missing and duplicate identities", () => {
  assert.throws(
    () =>
      assertBuildoutInventoryReconciled(
        [{ id: 1 }, { id: 1 }],
        2,
        true
      ),
    /duplicate inventory identity/
  );
  assert.throws(
    () =>
      assertBuildoutInventoryReconciled(
        [{ id: 1 }, { display_name: "missing id" }],
        2,
        true
      ),
    /missing a stable id/
  );
  assert.throws(
    () => assertBuildoutInventoryReconciled([{ id: 1 }], 2, true),
    /reconciled 1 unique inventory rows against provider total 2/
  );
  assert.doesNotThrow(() =>
    assertBuildoutInventoryReconciled([{ id: 1 }, { id: "2" }], 2, true)
  );
});

test("complete Buildout inventory rejects repeated pinned rows instead of deduping", async () => {
  const oldFetch = globalThis.fetch;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const pluginKey = "repeat-pinned-plugin";
  const page0 = Array.from({ length: 30 }, (_, index) => ({ id: index + 1 }));
  const page1 = [
    { id: 30 },
    ...Array.from({ length: 29 }, (_, index) => ({ id: index + 31 })),
  ];
  const requestedUrls: string[] = [];
  try {
    delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    buildoutCache.clear();
    buildoutFailureCache.clear();
    globalThis.fetch = async (input) => {
      const url = String(input);
      requestedUrls.push(url);
      const page = Number(new URL(url).searchParams.get("page"));
      return new Response(
        JSON.stringify({
          inventory: page === 0 ? page0 : page1,
          meta: { total: 60, limit: 30 },
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      );
    };
    await assert.rejects(
      buildoutInventory("Pinned Feed", pluginKey, {
        preferDirectJson: true,
        requireCompletePages: true,
        inventorySort: "created_at asc, id asc",
        pageConcurrency: 1,
      }),
      /duplicate inventory identity 30/
    );
    assert.ok(requestedUrls.length >= 2);
    assert.ok(
      requestedUrls.every(
        (url) =>
          new URL(url).searchParams.get("q[s][]") ===
          "created_at asc, id asc"
      )
    );
  } finally {
    globalThis.fetch = oldFetch;
    buildoutCache.clear();
    buildoutFailureCache.clear();
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  }
});

test("complete Buildout inventory rejects an oversized page declaration early", async () => {
  const oldFetch = globalThis.fetch;
  const pluginKey = "oversized-page-plugin";
  let requests = 0;
  try {
    buildoutCache.clear();
    buildoutFailureCache.clear();
    globalThis.fetch = async () => {
      requests += 1;
      return new Response(
        JSON.stringify({
          inventory: Array.from({ length: 30 }, (_, index) => ({ id: index + 1 })),
          meta: { total: 36001, limit: 30 },
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      );
    };
    await assert.rejects(
      buildoutInventory("Oversized Feed", pluginKey, {
        preferDirectJson: true,
        requireCompletePages: true,
      }),
      /exceeding the 1200-page safety cap/
    );
    assert.equal(requests, 1);
  } finally {
    globalThis.fetch = oldFetch;
    buildoutCache.clear();
    buildoutFailureCache.clear();
  }
});

test("BUILDOUT_REFRESH_PAGE_CACHE forces a live read and refreshes the durable cache", () => {
  clearEnv(ENV_KEYS);
  process.env.BUILDOUT_REFRESH_PAGE_CACHE = "1";
  assert.equal(buildoutRefreshPageCache(), true);
  assert.deepEqual(buildoutPageCachePolicy({ usePageCache: true }), { read: false, write: true });
  clearEnv(ENV_KEYS);
});

test("Buildout page cache policy preserves cache reuse by default", () => {
  clearEnv(ENV_KEYS);
  assert.equal(buildoutRefreshPageCache(), false);
  assert.deepEqual(buildoutPageCachePolicy({ usePageCache: true }), { read: true, write: true });
  assert.deepEqual(buildoutPageCachePolicy({}), { read: false, write: false });
  clearEnv(ENV_KEYS);
});

test("Buildout fresh refresh rejects cache-only recovery modes", () => {
  clearEnv(ENV_KEYS);
  process.env.BUILDOUT_REFRESH_PAGE_CACHE = "true";
  process.env.BUILDOUT_CACHE_ONLY = "1";
  assert.throws(() => buildoutPageCachePolicy({}), /cannot be combined/);
  delete process.env.BUILDOUT_CACHE_ONLY;
  process.env.BUILDOUT_ASSEMBLE_FROM_CACHE = "1";
  assert.throws(() => buildoutPageCachePolicy({}), /cannot be combined/);
  clearEnv(ENV_KEYS);
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
