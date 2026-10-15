import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { srcBuildout, buildoutDetailIframeUrl, buildoutCache, buildoutFailureCache, buildoutPageCachePath } from "../../../sources/buildout.js";
import { REGISTERED_BUILDOUT_FIRMS } from "../../../sources/buildout-registry.js";
import { firecrawl } from "../../../lib/scrape.js";
import type { BuildoutCurrentInventoryProof } from "../../../types.js";

const firm = REGISTERED_BUILDOUT_FIRMS[0];
const opts = { ...firm.inventoryOpts, recoveryPasses: 0, recoveryCooldownMs: 0, jsonAttempts: 1, jsonBackoffMs: 0 };
const keys = ["CRE_REFRESH_GENERATION", "CRE_REQUIRE_FRESH_DETAILS", "CRE_SCRAPE_MAX_ATTEMPTS", "BUILDOUT_CACHE_DIR", "BUILDOUT_PAGE_START", "BUILDOUT_PAGE_END", "BUILDOUT_REFRESH_PAGE_CACHE", "BUILDOUT_USE_PAGE_CACHE", "BUILDOUT_CACHE_ONLY", "BUILDOUT_ASSEMBLE_FROM_CACHE", "BUILDOUT_PAGE_JITTER_MS"];
function row(id: number, extra: Record<string, unknown> = {}) {
  return { id, show_link: `${firm.listingsPage}?propertyId=${id}-sale`, sale: true, closed: false, under_contract: false, also_for_sale_or_lease: false, ...extra };
}
function pages(rows: any[], limit = 2) {
  return Array.from({ length: Math.max(1, Math.ceil(rows.length / limit)) }, (_, index) => ({ inventory: rows.slice(index * limit, (index + 1) * limit), meta: { total: rows.length, limit } }));
}
async function fixture(run: (state: { requests: string[]; setPages: (pages: any[]) => void; cacheDir: string }) => Promise<void>) {
  const prior = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
  const oldFetch = globalThis.fetch;
  const oldScrape = firecrawl.scrape;
  const cacheDir = mkdtempSync(join(tmpdir(), "buildout-proof-"));
  const requests: string[] = [];
  let responses = pages([row(1), row(2, { sale: false }), row(3, { closed: true })]);
  try {
    for (const key of keys) delete process.env[key];
    Object.assign(process.env, { CRE_REFRESH_GENERATION: "buildout-proof-generation", CRE_REQUIRE_FRESH_DETAILS: "1", CRE_SCRAPE_MAX_ATTEMPTS: "1", BUILDOUT_CACHE_DIR: cacheDir });
    buildoutCache.clear(); buildoutFailureCache.clear();
    globalThis.fetch = async (input) => {
      const url = String(input); requests.push(url);
      return new Response(JSON.stringify(responses[Number(new URL(url).searchParams.get("page"))]), { status: 200 });
    };
    (firecrawl as any).scrape = async () => { throw new Error("unexpected fallback"); };
    await run({ requests, setPages: (value) => { responses = value; }, cacheDir });
  } finally {
    globalThis.fetch = oldFetch; (firecrawl as any).scrape = oldScrape;
    for (const key of keys) { if (prior[key] === undefined) delete process.env[key]; else process.env[key] = prior[key]; }
    buildoutCache.clear(); buildoutFailureCache.clear(); rmSync(cacheDir, { recursive: true, force: true });
  }
}
function collect(tx: "sale" | "lease" = "sale", max = Infinity, overrides = {}) {
  return srcBuildout(firm.company, firm.pluginKey, firm.listingsPage, tx, max, false, { ...opts, ...overrides });
}
function proof(value: Awaited<ReturnType<typeof collect>>) {
  assert.equal(value.currentInventoryProof?.schema, "buildout-current-inventory-v1");
  return value.currentInventoryProof as BuildoutCurrentInventoryProof;
}

test("Buildout complete inventory receipt", async (t) => {
  await t.test("mixed full-feed partitions share genuine pages and parsed JSON digests", () => fixture(async ({ requests }) => {
    const sale = await collect(); const lease = await collect("lease");
    const a = proof(sale), b = proof(lease);
    assert.equal(requests.length, 2); assert.equal(a.total, 3); assert.equal(a.limit, 2);
    assert.deepEqual(a.pages.map((p) => p.index), [0, 1]); assert.deepEqual(a.pages, b.pages);
    assert.equal(sale.listings.length, 1); assert.equal(lease.listings.length, 1);
    assert.equal(a.pages.flatMap((p) => p.rows).filter((r) => r.closed).length, 1);
    assert.equal(a.sourceKey, firm.sourceKey); assert.equal(a.pluginKey, firm.pluginKey);
    assert.equal(b.transaction, "lease"); assert.equal(a.pages[0].transport, "direct_json");
    assert.equal(a.pages[0].endpoint, requests[0]); assert.equal(new URL(requests[0]).searchParams.get("q[s][]"), a.sort);
    assert.equal(a.pages[0].parsedJsonSha256, createHash("sha256").update(JSON.stringify(pages([row(1), row(2, { sale: false }), row(3, { closed: true })])[0])).digest("hex"));
    assert.ok(Number.isFinite(Date.parse(a.pages[0].observedAt)));
  }));
  await t.test("mocked HTTP reaches the real collector artifact with both full-feed proofs", () => fixture(async ({ cacheDir }) => {
    const mockPath = join(cacheDir, "mock-provider.mjs");
    const outPath = join(cacheDir, "artifact.json");
    const payloads = pages([row(1), row(2, { sale: false }), row(3, { closed: true })]);
    writeFileSync(mockPath, `const pages = ${JSON.stringify(payloads)};
      globalThis.fetch = async (input) => {
        const url = new URL(String(input));
        if (url.hostname !== "buildout.com") throw new Error("unexpected network request");
        return new Response(JSON.stringify(pages[Number(url.searchParams.get("page"))]), {status:200});
      };`);
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", pathToFileURL(mockPath).href,
      "collect.ts", `--source=${firm.sourceKey}`, "--transaction=both", `--out=${outPath}`], {
      cwd: fileURLToPath(new URL("../../../", import.meta.url)),
      env: process.env, encoding: "utf8", timeout: 30000,
    });
    assert.equal(child.status, 0, child.stderr);
    const artifact = JSON.parse(readFileSync(outPath, "utf8"));
    assert.equal(artifact.sources.length, 2);
    assert.deepEqual(artifact.sources.map((entry: any) => entry.currentInventoryProof.transaction), ["sale", "lease"]);
    assert.equal(artifact.sources[0].currentInventoryProof.total, 3);
    assert.equal(artifact.listings.length, 2);
    assert.ok(artifact.listings.every((listing: any) => listing.sourceKey === firm.sourceKey));
  }));
  await t.test("complete sale feed proves a truly empty lease partition", () => fixture(async ({ setPages }) => {
    setPages(pages([row(1)])); const lease = await collect("lease");
    assert.equal(lease.listings.length, 0); assert.equal(proof(lease).total, 1); assert.equal(lease.truncated, false);
  }));
  await t.test("zero-row feed retains its observed terminal page", () => fixture(async ({ setPages }) => {
    setPages(pages([])); const result = await collect(); assert.equal(proof(result).pages[0].rows.length, 0); assert.equal(proof(result).total, 0);
  }));
  await t.test("malformed lifecycle flags omit proof while preserving legacy rows", () => fixture(async ({ setPages }) => {
    for (const field of ["sale", "closed", "under_contract", "also_for_sale_or_lease"]) {
      for (const value of [undefined, "false", null]) {
        buildoutCache.clear(); setPages(pages([row(1, { [field]: value })]));
        const result = await collect("lease"); assert.equal(result.currentInventoryProof, undefined, field);
      }
    }
  }));
  await t.test("wrong host or missing URL cannot carry owned listing evidence", () => fixture(async ({ setPages }) => {
    for (const show_link of ["https://attacker.example/?propertyId=1", null, "bad-url"]) {
      buildoutCache.clear(); setPages(pages([row(1, { show_link })])); const result = await collect();
      assert.equal(result.currentInventoryProof, undefined); assert.equal(result.listings.length, 1);
    }
  }));
  await t.test("capped partition has no complete proof", () => fixture(async ({ setPages }) => {
    setPages(pages([row(1), row(2)])); const result = await collect("sale", 1);
    assert.equal(result.truncated, true); assert.equal(result.currentInventoryProof, undefined);
  }));
  await t.test("under-contract and dual-mode facts survive without lifecycle activation", () => fixture(async ({ setPages }) => {
    setPages(pages([row(1, { under_contract: true, also_for_sale_or_lease: true })]));
    const result = await collect(); const raw = proof(result).pages[0].rows[0];
    assert.equal(raw.underContract, true); assert.equal(raw.alsoForSaleOrLease, true);
    assert.equal(result.listings[0].underContract, true); assert.equal(result.listings[0].transactionType, "Sale/Lease");
  }));
  await t.test("unstable sort cannot claim the complete-inventory contract", () => fixture(async () => {
    const result = await collect("sale", Infinity, { inventorySort: undefined });
    assert.equal(result.currentInventoryProof, undefined); assert.equal(result.listings.length, 1);
  }));
  await t.test("ordinary collection cannot label cached Firecrawl responses as strict fresh proof", () => fixture(async () => {
    delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    const result = await collect(); assert.equal(result.currentInventoryProof, undefined);
    assert.equal(result.listings.length, 1);
  }));
  await t.test("equal-count duplicate native identities fail closed", () => fixture(async ({ setPages }) => {
    setPages(pages([row(1), row(1)])); await assert.rejects(collect(), /duplicate inventory identity/);
  }));
  await t.test("short pages and changed declared totals fail before proof", () => fixture(async ({ setPages }) => {
    const data = pages([row(1), row(2), row(3)]); data[1].meta.total = 4;
    setPages(data); await assert.rejects(collect(), /inventory pages failed/);
  }));
  await t.test("partial page windows never produce artifact", () => fixture(async () => {
    process.env.BUILDOUT_PAGE_START = "0"; process.env.BUILDOUT_PAGE_END = "0";
    await assert.rejects(collect(), /refusing partial listing artifact/);
  }));
  await t.test("generation cache preserves original clocks, digest and transport", () => fixture(async ({ requests }) => {
    const before = proof(await collect()); buildoutCache.clear(); process.env.BUILDOUT_USE_PAGE_CACHE = "1";
    const after = proof(await collect()); assert.equal(requests.length, 2);
    assert.equal(after.pages[0].observedAt, before.pages[0].observedAt); assert.equal(after.pages[0].parsedJsonSha256, before.pages[0].parsedJsonSha256);
    assert.equal(after.pages[0].cacheDisposition, "generation_cache"); assert.equal(after.pages[0].transport, "direct_json");
  }));
  await t.test("old cache and changed retained payload cannot acquire proof", () => fixture(async () => {
    await collect(); buildoutCache.clear(); process.env.BUILDOUT_USE_PAGE_CACHE = "1";
    const path = buildoutPageCachePath(firm.company, firm.pluginKey, 0, opts);
    const cached = JSON.parse(readFileSync(path, "utf8")); delete cached.observation;
    writeFileSync(path, JSON.stringify(cached)); assert.equal((await collect()).currentInventoryProof, undefined);
  }));
  await t.test("tampered cached payload cannot reuse its old digest", () => fixture(async () => {
    await collect(); buildoutCache.clear(); process.env.BUILDOUT_USE_PAGE_CACHE = "1";
    const path = buildoutPageCachePath(firm.company, firm.pluginKey, 0, opts);
    const cached = JSON.parse(readFileSync(path, "utf8")); cached.data.inventory[0].display_name = "altered";
    writeFileSync(path, JSON.stringify(cached)); assert.equal((await collect()).currentInventoryProof, undefined);
  }));
  await t.test("wrong plugin or query fingerprint forces live reobservation", () => fixture(async ({ requests }) => {
    for (const key of ["pluginKey", "queryFingerprint"]) {
      await collect(); buildoutCache.clear(); process.env.BUILDOUT_USE_PAGE_CACHE = "1";
      const path = buildoutPageCachePath(firm.company, firm.pluginKey, 0, opts);
      const cached = JSON.parse(readFileSync(path, "utf8")); cached[key] = "wrong";
      writeFileSync(path, JSON.stringify(cached)); const before = requests.length;
      assert.ok(proof(await collect())); assert.equal(requests.length, before + 1);
      buildoutCache.clear();
    }
  }));
  await t.test("a different generation reobserves provider", () => fixture(async ({ requests }) => {
    await collect(); process.env.CRE_REFRESH_GENERATION = "new-generation"; process.env.BUILDOUT_USE_PAGE_CACHE = "1";
    assert.equal(proof(await collect()).generationId, "new-generation"); assert.equal(requests.length, 4);
  }));
  await t.test("Firecrawl fallback has truthful parsed-response provenance and maxAge zero", () => fixture(async ({ setPages }) => {
    const data = pages([row(1)]); setPages(data);
    globalThis.fetch = async () => { throw new Error("direct unavailable"); };
    const calls: any[] = [];
    (firecrawl as any).scrape = async (_url: string, options: any) => { calls.push(options); return { rawHtml: JSON.stringify(data[0]) }; };
    const result = proof(await collect()); assert.equal(result.pages[0].transport, "firecrawl_json"); assert.equal(calls[0].maxAge, 0);
  }));
  await t.test("direct recovery retains genuine observation and persists proof", () => fixture(async ({ requests }) => {
    let directPageOne = 0; const original = globalThis.fetch;
    globalThis.fetch = async (input, options) => {
      if (new URL(String(input)).searchParams.get("page") === "1" && directPageOne++ === 0) throw new Error("transient");
      return original(input, options);
    };
    const result = proof(await collect("sale", Infinity, { recoveryPasses: 1 }));
    assert.equal(result.pages[1].transport, "direct_json"); assert.ok(result.pages[1].observedAt); assert.equal(requests.length, 2);
  }));
});


test("Hudson current-feed host owns inventory proof and detail claims exactly", () => fixture(async ({ setPages }) => {
  const hudson = REGISTERED_BUILDOUT_FIRMS.find((entry) => entry.sourceKey === "hudson-peters")!;
  const ownedUrl = "https://hudsonpeters.com/listings/?propertyId=687920-lease";
  for (const host of ["hudsonpeters.com", "www.hudsonpeters.com", "foreign.example"]) {
    buildoutCache.clear();
    const listingUrl = ownedUrl.replace("hudsonpeters.com", host);
    setPages(pages([row(968697, { show_link: listingUrl, sale: false })]));
    const result = await srcBuildout(hudson.company, hudson.pluginKey, hudson.listingsPage, "lease", Infinity, false,
      { ...hudson.inventoryOpts, recoveryPasses: 0, jsonAttempts: 1 });
    const iframe = buildoutDetailIframeUrl(hudson.sourceKey, listingUrl, hudson.detailConfig);
    if (host === "hudsonpeters.com") {
      assert.equal(proof(result).sourceKey, "hudson-peters");
      assert.equal(result.listings[0].url, ownedUrl);
      assert.equal(iframe, `https://buildout.com/plugins/${hudson.pluginKey}/hudsonpeters.com/inventory/687920?pluginId=0&iframe=true&embedded=true`);
    } else {
      assert.equal(result.currentInventoryProof, undefined, host);
      assert.equal(iframe, null, host);
    }
  }
}));
