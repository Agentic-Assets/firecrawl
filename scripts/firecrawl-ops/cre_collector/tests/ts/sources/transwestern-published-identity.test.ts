import test from "node:test";
import assert from "node:assert/strict";
import { firecrawl } from "../../../lib/scrape.js";
import { srcTranswestern, transwesternDetailUrl, transwesternDetailPageIsUsable, enrichTranswesternListing } from "../../../sources/transwestern.js";
import { proveTranswesternPublishedIdentity, requireCurrentTranswesternPublishedProof } from "../../../sources/transwestern-published-identity.js";
import { requireUniqueTranswesternPlaceholderIdentity, transwesternPrimaryIdentityMatches, transwesternPublishedPlaceholderUrl } from "../../../sources/pure/transwestern-published-identity.js";

const row = {
  PageUrl: "-", BuildingName: "LaCenterra at Cinco Ranch - Phase III",
  FullAddress: "2717 Commercial Center Blvd", City: "Katy", State: "TX", ZipCode: "77494",
  PropertyTypeName: "Office", PropertySize: 72706,
};
const url = "https://transwestern.com/property/-";
const sitemap = `<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>${url}</loc></url></urlset>`;
const html = `<html><meta property="og:title" content="${row.BuildingName}"><section class="property-dtl-bnr"><h1>${row.BuildingName}</h1><span class="lct-zipe-code">${row.FullAddress} Katy, TX 77494</span></section><div class="property-description">Current property description</div></html>`;

function response(body: string, requested: string, status = 200): Response {
  const result = new Response(body, { status });
  Object.defineProperty(result, "url", { value: requested });
  return result;
}

async function withSourceMocks(
  run: (calls: Array<{ url: string; options: any }>) => Promise<void>,
  options: { sitemap?: string; html?: string; rendered?: string; feeds?: Record<string, any[]> } = {},
): Promise<void> {
  const originalFetch = globalThis.fetch;
  const originalScrape = firecrawl.scrape;
  const calls: Array<{ url: string; options: any }> = [];
  globalThis.fetch = (async (input: any, init: any) => {
    const requested = String(input);
    calls.push({ url: requested, options: init });
    return response(requested.endsWith("sitemap.xml") ? options.sitemap ?? sitemap : options.html ?? html, requested);
  }) as typeof fetch;
  (firecrawl as any).scrape = async (requested: string, init: any) => {
    calls.push({ url: requested, options: init });
    if (requested.includes("/properties?")) {
      const bucket = new URL(requested).searchParams.get("DealsType")!;
      return { rawHtml: JSON.stringify(options.feeds?.[bucket] ?? (bucket === "Lease" ? [row] : [])) };
    }
    return { rawHtml: options.rendered ?? html, markdown: "Current complete property detail", links: [] };
  };
  try { await run(calls); } finally { globalThis.fetch = originalFetch; (firecrawl as any).scrape = originalScrape; }
}

test("published placeholder requires exact sitemap membership and complete primary identity", () => {
  assert.equal(transwesternDetailUrl("-"), null);
  assert.equal(transwesternPublishedPlaceholderUrl(sitemap), url);
  assert.equal(transwesternPrimaryIdentityMatches(html, row), true);
  assert.equal(transwesternPrimaryIdentityMatches(html.replace(`<h1>${row.BuildingName}</h1>`, ""), row), false);
  assert.equal(transwesternPrimaryIdentityMatches(html.replace("Katy, TX 77494", " Katy ,  TX  77494 "), row), true);
  for (const bad of ["<urlset/>", sitemap.replace("</urlset>", `<url><loc>${url}</loc></url></urlset>`), sitemap.replace(url, "https://other.example/property/-"), `<html>${sitemap}</html>`, `<urlset><other><url><loc>${url}</loc></url></other></urlset>`]) {
    assert.throws(() => transwesternPublishedPlaceholderUrl(bad));
  }
  for (const bad of [html.replaceAll(row.BuildingName, "Different property"), html.replaceAll(row.BuildingName, `${row.BuildingName} Annex`), html.replace(row.FullAddress, "2719 Commercial Center Blvd"), html.replace("77494", "77493"), html.replace("Katy, TX", "Dallas, TX"), html.replace("Katy, TX", "Katy, CA"), html.replace('class="property-dtl-bnr"', 'class="related-property"')]) {
    assert.equal(transwesternPrimaryIdentityMatches(bad, row), false);
  }
});

test("published identity cannot combine unrelated global heading with a different primary banner", () => {
  const wrongPrimary = html.replace(`<h1>${row.BuildingName}</h1>`, "<h1>Different primary property</h1>")
    .replace("<section", `<h1>${row.BuildingName}</h1><section`);
  assert.equal(transwesternPrimaryIdentityMatches(wrongPrimary, row), false);
  assert.equal(transwesternPrimaryIdentityMatches(html.replace("</html>", `<section class="property-dtl-bnr"><h1>${row.BuildingName}</h1></section></html>`), row), false);
  assert.equal(transwesternPrimaryIdentityMatches(html.replace(`<h1>${row.BuildingName}</h1>`, `<h1>${row.BuildingName}</h1><h1>${row.BuildingName}</h1>`), row), false);
});

test("placeholder identity collisions reject before deduplication", () => {
  requireUniqueTranswesternPlaceholderIdentity([row, { ...row, City: " KATY " }]);
  assert.throws(() => requireUniqueTranswesternPlaceholderIdentity([row, { ...row, FullAddress: "Other address" }]), /conflicting/);
  assert.throws(() => requireUniqueTranswesternPlaceholderIdentity([{ ...row, ZipCode: "" }]), /complete/);
  assert.throws(() => requireUniqueTranswesternPlaceholderIdentity([{ ...row, ZipCode: 77494 }]), /complete/);
});

test("full source preserves official slug/raw row and reuses normal rendered enrichment", async () => {
  await withSourceMocks(async (calls) => {
    const result = await srcTranswestern("lease", Infinity, false);
    assert.equal(result.totalAvailable, 1);
    assert.equal(result.truncated, false);
    const listing = result.listings[0];
    assert.equal(listing.id, "-");
    assert.equal(listing.url, url);
    assert.deepEqual(listing.rawTranswesternFeed, row);
    assert.equal(listing.detailError, undefined);
    assert.equal(listing.markdown, "Current complete property detail");
    assert.equal(listing.freshnessProvenance.detailScope, "detail_page");
    assert.ok(listing.detailObservedAt);
    assert.equal(calls.filter((call) => call.url === url).length, 2);
    const native = calls.filter((call) => call.options.redirect);
    assert.equal(native.length, 2);
    assert.ok(native.every((call) => call.options.redirect === "error" && call.options.cache === "no-store" && call.options.signal instanceof AbortSignal));
    assert.notEqual(native[0].options.signal, native[1].options.signal);
    assert.equal(calls.find((call) => call.url === url && !call.options.redirect)?.options.maxAge, 0);
  });
});

test("monitor also proves placeholder but retains inventory-only observation scope", async () => {
  await withSourceMocks(async (calls) => {
    const result = await srcTranswestern("lease", Infinity, true);
    assert.equal(result.listings[0].id, "-");
    assert.equal(result.listings[0].freshnessProvenance.detailScope, "inventory_feed");
    assert.equal(result.listings[0].detailObservedAt, undefined);
    assert.equal(calls.filter((call) => call.url === url).length, 1);
  });
});

test("full and monitor fail closed without fresh sitemap or exact usable property proof", async () => {
  for (const monitor of [false, true]) {
    for (const options of [{ sitemap: "<urlset/>" }, { sitemap: sitemap.replace("</urlset>", `<url><loc>${url}</loc></url></urlset>`) }, { html: html.replace(row.FullAddress, "Wrong street") }, { html: `${html} verify you are human` }, { html: html.replace('class="property-description"', 'class="generic"') }]) {
      await withSourceMocks(async () => {
        await assert.rejects(srcTranswestern("lease", 1, monitor));
      }, options);
    }
  }
});

test("all buckets are checked for placeholder collision before proof or cap selection", async () => {
  await withSourceMocks(async (calls) => {
    await assert.rejects(srcTranswestern("lease", 1, false), /conflicting/);
    assert.equal(calls.filter((call) => call.options.redirect).length, 0);
  }, { feeds: { Lease: [row], Sublease: [{ ...row, FullAddress: "Different street" }], "Sale or Lease": [] } });
});

test("identical placeholder across buckets deduplicates only after fresh proof", async () => {
  await withSourceMocks(async (calls) => {
    const result = await srcTranswestern("lease", Infinity, true);
    assert.equal(result.totalAvailable, 1);
    assert.equal(result.listings.length, 1);
    assert.equal(calls.filter((call) => call.options.redirect).length, 2);
  }, { feeds: { Lease: [row], Sublease: [{ ...row }], "Sale or Lease": [] } });
});

test("ordinary slugs make no exceptional native proof requests", async () => {
  await withSourceMocks(async (calls) => {
    const result = await srcTranswestern("lease", Infinity, true);
    assert.equal(result.listings[0].id, "ordinary-property");
    assert.equal(calls.filter((call) => call.options.redirect).length, 0);
  }, { feeds: { Lease: [{ ...row, PageUrl: "ordinary-property" }], Sublease: [], "Sale or Lease": [] } });
});

test("rendered detail drift is rejected after successful native admission proof", async () => {
  await withSourceMocks(async () => {
    const result = await srcTranswestern("lease", Infinity, false);
    assert.match(result.listings[0].detailError, /expected Transwestern property identity/);
  }, { rendered: html.replace(row.FullAddress, "Different street") });
});

test("proof is generation/identity/time bound and each invocation reads live again", async () => {
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  const oldStarted = process.env.CRE_REFRESH_STARTED_AT;
  process.env.CRE_REFRESH_GENERATION = "current-generation";
  process.env.CRE_REFRESH_STARTED_AT = "2026-01-01T00:00:00Z";
  try {
    await withSourceMocks(async (calls) => {
      const observedAt = new Date(Date.now() - 1000).toISOString();
      const proof = await proveTranswesternPublishedIdentity(row, transwesternDetailPageIsUsable);
      requireCurrentTranswesternPublishedProof(proof, row, observedAt);
      assert.throws(() => requireCurrentTranswesternPublishedProof(proof, { ...row, PageUrl: "ordinary-property" }, observedAt), /stale or mismatched/);
      await assert.rejects(proveTranswesternPublishedIdentity({ ...row, PageUrl: "ordinary-property" }, transwesternDetailPageIsUsable), /requires the placeholder/);
      for (const bad of [{ ...proof, generationId: "old-generation" }, { ...proof, verifiedAt: "2025-01-01T00:00:00Z" }, { ...proof, identitySha256: "wrong" }]) {
        await assert.rejects(enrichTranswesternListing(row, "Lease", "lease", true, observedAt, bad), /stale or mismatched/);
      }
      await proveTranswesternPublishedIdentity(row, transwesternDetailPageIsUsable);
      assert.equal(calls.filter((call) => call.url.endsWith("sitemap.xml")).length, 2);
    });
  } finally {
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION; else process.env.CRE_REFRESH_GENERATION = oldGeneration;
    if (oldStarted === undefined) delete process.env.CRE_REFRESH_STARTED_AT; else process.env.CRE_REFRESH_STARTED_AT = oldStarted;
  }
});

test("official proof fetch rejects transport failures, redirects and oversized bodies", async () => {
  const originalFetch = globalThis.fetch;
  try {
    for (const fake of [
      async () => { throw new DOMException("unavailable", "TimeoutError"); },
      async () => response(sitemap, "https://other.example/sitemap.xml"),
      async () => response("unavailable", "https://transwestern.com/sitemap.xml", 503),
      async () => response("x".repeat(2 * 1024 * 1024 + 1), "https://transwestern.com/sitemap.xml"),
      async (input: any) => response(String(input).endsWith("sitemap.xml") ? sitemap : "x".repeat(1024 * 1024 + 1), String(input)),
    ]) {
      globalThis.fetch = fake as typeof fetch;
      await assert.rejects(proveTranswesternPublishedIdentity(row, transwesternDetailPageIsUsable));
    }
  } finally { globalThis.fetch = originalFetch; }
});

test("native proof has a ten-second per-request deadline and shared twenty-second total", async () => {
  const originalFetch = globalThis.fetch;
  const originalTimeout = AbortSignal.timeout;
  const timers: ReturnType<typeof setTimeout>[] = [];
  try {
    for (const expireTotal of [false, true]) {
      const deadlines: number[] = [];
      AbortSignal.timeout = (milliseconds: number) => {
        deadlines.push(milliseconds);
        const controller = new AbortController();
        const expire = expireTotal ? milliseconds === 20000 : milliseconds === 10000;
        timers.push(setTimeout(() => controller.abort(new DOMException("bounded timeout", "TimeoutError")), expire ? 10 : 1000));
        return controller.signal;
      };
      globalThis.fetch = (async (input: any, init: any) => {
        if (expireTotal && String(input).endsWith("sitemap.xml")) return response(sitemap, String(input));
        return new Promise<Response>((_resolve, reject) => {
          if (init.signal.aborted) reject(init.signal.reason);
          else init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true });
        });
      }) as typeof fetch;
      await assert.rejects(proveTranswesternPublishedIdentity(row, transwesternDetailPageIsUsable), { name: "TimeoutError" });
      assert.deepEqual(deadlines, expireTotal ? [20000, 10000, 10000] : [20000, 10000]);
    }
  } finally {
    timers.forEach(clearTimeout);
    globalThis.fetch = originalFetch;
    AbortSignal.timeout = originalTimeout;
  }
});
