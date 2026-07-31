import assert from "node:assert/strict";
import test from "node:test";
import {
  MATTHEWS_FETCH_TIMEOUT_MS,
  matthewsFetchResponse,
  matthewsFetchOptions,
  matthewsFetch,
  matthewsParsedCoverage,
  matthewsPermanentRedirectTarget,
  matthewsProviderIdentity,
  matthewsProviderNotFound,
  matthewsRetryableFetchFailure,
  matthewsResponseText,
  parseMatthewsDetail,
  matthewsTenureFromUrl,
  srcMatthews,
} from "../../../sources/matthews.js";

test("Matthews bounds a header stall even when fetch ignores abort", async (t) => {
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = globalThis.setTimeout;
  const controller = new AbortController();
  globalThis.fetch = async () => new Promise<Response>(() => {});
  globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) => {
    if (delay === 1) {
      queueMicrotask(() => callback(...args));
      return 0 as unknown as ReturnType<typeof setTimeout>;
    }
    return originalSetTimeout(callback, delay, ...args);
  }) as typeof setTimeout;
  t.after(() => {
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
  });

  await assert.rejects(
    () => matthewsFetchResponse("https://www.matthews.com/sitemap.xml", {}, controller, 1),
    /response headers timed out after 1ms/
  );
  assert.equal(controller.signal.aborted, true);
});

test("matthewsTenureFromUrl classifies leasing slugs as lease", () => {
  assert.equal(matthewsTenureFromUrl("https://www.matthews.com/properties/leasing-abc"), "lease");
  assert.equal(matthewsTenureFromUrl("https://www.matthews.com/properties/panera-bread"), "sale");
});

test("parseMatthewsDetail extracts core fields from server-rendered HTML", () => {
  const html = `
    <html>
      <head><meta property="og:image" content="https://cms.matthews.com/wp-content/uploads/photo.jpg"></head>
      <body>
        <h1 id="propertyTitle">Panera Bread</h1>
        <div id="propertyAddress">123 Main St, Tulsa, OK 74103</div>
        <div id="propertyPrice">$3,000,000</div>
        <div class="key-info-title">Cap Rate</div><div class="key-info-value">6.40%</div>
        <div class="key-info-title">Property Type</div><div class="key-info-value">Retail</div>
        <a id="agentName" href="/agents/jane">Jane Broker</a>
      </body>
    </html>`;
  const row = parseMatthewsDetail(html, "https://www.matthews.com/properties/panera-bread", "sale");
  assert.equal(row?.id, "panera-bread");
  assert.equal(row?.name, "Panera Bread");
  assert.equal(row?.transactionType, "Sale");
  assert.equal(row?.salePriceUsd, 3000000);
  assert.equal(row?.capRatePct, 6.4);
  assert.equal(row?.assetType, "Retail");
  assert.equal(row?.state, "OK");
});

test("strict Matthews parsing validates canonical provider identity and emits generation provenance", () => {
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "matthews-strict-test";
    const html = `
      <html>
        <head>
          <link rel="canonical" href="https://www.matthews.com/properties/panera-bread">
        </head>
        <body>
          <h1 id="propertyTitle">Panera Bread</h1>
          <div id="propertyAddress">123 Main St, Tulsa, OK 74103</div>
        </body>
      </html>`;
    const row = parseMatthewsDetail(
      html,
      "https://www.matthews.com/properties/panera-bread",
      "sale",
      {
        inventoryObservedAt: "2026-07-29T12:00:00.000Z",
        detailObservedAt: "2026-07-29T12:01:00.000Z",
      }
    );
    assert.equal(row?.id, "panera-bread");
    assert.equal(row?.inventoryObservedAt, "2026-07-29T12:00:00.000Z");
    assert.equal(row?.detailObservedAt, "2026-07-29T12:01:00.000Z");
    assert.equal(row?.freshnessProvenance.generationId, "matthews-strict-test");
    assert.equal(row?.freshnessProvenance.identityMethod, "canonical_property_url");

    const mismatch = parseMatthewsDetail(
      html.replace("/panera-bread", "/different-property"),
      "https://www.matthews.com/properties/panera-bread",
      "sale",
      {
        inventoryObservedAt: "2026-07-29T12:00:00.000Z",
        detailObservedAt: "2026-07-29T12:01:00.000Z",
      }
    );
    assert.equal(mismatch, null);

    const missingCanonical = parseMatthewsDetail(
      html.replace(
        '<link rel="canonical" href="https://www.matthews.com/properties/panera-bread">',
        ""
      ),
      "https://www.matthews.com/properties/panera-bread",
      "sale",
      {
        inventoryObservedAt: "2026-07-29T12:00:00.000Z",
        detailObservedAt: "2026-07-29T12:01:00.000Z",
      }
    );
    assert.equal(missingCanonical, null);
  } finally {
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
  }
});

test("strict Matthews identity normalizes percent-escape spelling and sitemap trailing slashes", async (t) => {
  const sitemapUrl = "https://www.matthews.com/properties/%c2%b140k-sf-industrial-warehouse/";
  const canonicalUrl = "https://www.matthews.com/properties/%C2%B140k-sf-industrial-warehouse";
  const html = `
    <html><head><link rel="canonical" href="${canonicalUrl}"></head><body>
      <h1 id="propertyTitle">40K SF Industrial Warehouse</h1>
      <div id="propertyAddress">1 Main St, Tulsa, OK 74103</div>
    </body></html>`;
  assert.equal(
    matthewsProviderIdentity(html, sitemapUrl, true),
    "%c2%b140k-sf-industrial-warehouse"
  );
  assert.equal(parseMatthewsDetail(html, sitemapUrl, "sale", { strict: true })?.id, "%c2%b140k-sf-industrial-warehouse");

  const originalFetch = globalThis.fetch;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  let detailRequests = 0;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/sitemap.xml")) {
      return new Response(
        `<?xml version="1.0"?><urlset><url><loc>${sitemapUrl}</loc></url><url><loc>${canonicalUrl}</loc></url></urlset>`,
        { status: 200 }
      );
    }
    if (url === canonicalUrl.replace(/%C2%B1/g, "%c2%b1")) {
      detailRequests += 1;
      return new Response(html, { status: 200 });
    }
    throw new Error(`unexpected URL ${url}`);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  });

  const result = await srcMatthews("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(detailRequests, 1);
  assert.equal(result.totalAvailable, 1);
  assert.equal(result.listings.length, 1);
  assert.equal(result.listings[0].id, "%c2%b140k-sf-industrial-warehouse");
  assert.equal(result.truncated, false);

  const monitor = await srcMatthews("sale", Number.POSITIVE_INFINITY, true);
  assert.equal(monitor.listings.length, 1);
  assert.equal(monitor.listings[0].id, result.listings[0].id);
});

test("strict Matthews parsing rejects soft error shells with matching canonical identity", () => {
  const url = "https://www.matthews.com/properties/example-sale";
  const shells = [
    `
      <html>
        <head><link rel="canonical" href="${url}"></head>
        <body>
          <h1 id="propertyTitle">Page Not Found</h1>
          <div id="propertyAddress">The requested property was not found.</div>
        </body>
      </html>`,
    `
      <html>
        <head><link rel="canonical" href="${url}"></head>
        <body>
          <h1 id="propertyTitle">An Error Occurred</h1>
          <div id="propertyPrice">Please try again later.</div>
        </body>
      </html>`,
    `
      <html>
        <head><link rel="canonical" href="${url}"></head>
        <body>
          <h1 id="propertyTitle">Just a moment...</h1>
          <div id="propertyAddress">Verify you are human to continue.</div>
        </body>
      </html>`,
  ];

  for (const html of shells) {
    assert.equal(
      parseMatthewsDetail(html, url, "sale", {
        strict: true,
        inventoryObservedAt: "2026-07-29T12:00:00.000Z",
        detailObservedAt: "2026-07-29T12:01:00.000Z",
      }),
      null
    );
  }
});

test("strict Matthews parsing requires provider-specific property structure", () => {
  const url = "https://www.matthews.com/properties/example-sale";
  const genericPage = `
    <html>
      <head>
        <link rel="canonical" href="${url}">
        <meta property="og:image"
          content="https://cms.matthews.com/wp-content/uploads/example.jpg">
      </head>
      <body><h1>Example Sale</h1></body>
    </html>`;

  assert.equal(
    parseMatthewsDetail(genericPage, url, "sale", { strict: true }),
    null
  );
  assert.equal(
    parseMatthewsDetail(genericPage, url, "sale", { strict: false })?.name,
    "Example Sale"
  );
});

test("strict Matthews error-shell guard does not reject a property named for street number 404", () => {
  const url = "https://www.matthews.com/properties/404-main-street";
  const html = `
    <html>
      <head><link rel="canonical" href="${url}"></head>
      <body>
        <h1 id="propertyTitle">404 Main Street</h1>
        <div id="propertyAddress">404 Main Street, Tulsa, OK 74103</div>
      </body>
    </html>`;

  assert.equal(
    parseMatthewsDetail(html, url, "sale", { strict: true })?.name,
    "404 Main Street"
  );
});

test("Matthews coverage counts parse-null identity failures as truncation", () => {
  const coverage = matthewsParsedCoverage([{ id: "accepted" }, null, undefined]);
  assert.deepEqual(coverage.listings, [{ id: "accepted" }]);
  assert.equal(coverage.failures, 2);
  assert.equal(coverage.truncated, true);
});

test("Matthews excludes only its exact self-canonical provider 404 payload", () => {
  const url = "https://www.matthews.com/properties/retired-property";
  const provider404 = `
    <html><head><link rel="canonical" href="${url}"></head><body>
      <script>self.__next_f.push([1,"NEXT_REDIRECT;replace;/listings;307;"])</script>
      <script>self.__next_f.push([1,"404 - Page Not Found"])</script>
    </body></html>`;
  assert.equal(matthewsProviderNotFound(provider404, url, "sale"), true);
  assert.equal(parseMatthewsDetail(provider404, url, "sale", { strict: true }), null);

  const genericShell = provider404.replace(
    "NEXT_REDIRECT;replace;/listings;307;",
    "temporary template shell"
  );
  assert.equal(matthewsProviderNotFound(genericShell, url, "sale"), false);
  assert.equal(parseMatthewsDetail(genericShell, url, "sale", { strict: true }), null);

  const coverage = matthewsParsedCoverage([{ id: "active" }, null], 1);
  assert.deepEqual(coverage.listings, [{ id: "active" }]);
  assert.equal(coverage.providerNotFound, 1);
  assert.equal(coverage.failures, 0);
  assert.equal(coverage.truncated, false);
});

test("Matthews provider 404 exclusion rejects a mismatched canonical URL", () => {
  const requested = "https://www.matthews.com/properties/retired-property";
  const html = `
    <html><head><link rel="canonical" href="https://www.matthews.com/properties/other-property"></head>
      <body>NEXT_REDIRECT;replace;/listings;307; 404 - Page Not Found</body></html>`;
  assert.equal(matthewsProviderNotFound(html, requested, "sale"), false);
});

test("Matthews accepts only permanent same-tenure property redirect targets", () => {
  const requested = "https://www.matthews.com/properties/dollar-general-2";
  const target = "https://www.matthews.com/properties/stnl-dollar-general-livingston-tx";
  assert.equal(
    matthewsPermanentRedirectTarget(308, "/properties/stnl-dollar-general-livingston-tx", requested, "sale"),
    target
  );
  assert.equal(matthewsPermanentRedirectTarget(301, target, requested, "sale"), target);
  assert.equal(matthewsPermanentRedirectTarget(302, target, requested, "sale"), null);
  assert.equal(matthewsPermanentRedirectTarget(307, target, requested, "sale"), null);
  assert.equal(matthewsPermanentRedirectTarget(308, "https://example.test/properties/a", requested, "sale"), null);
  assert.equal(matthewsPermanentRedirectTarget(308, "/listings", requested, "sale"), null);
  assert.equal(matthewsPermanentRedirectTarget(308, requested, requested, "sale"), null);
  assert.equal(matthewsPermanentRedirectTarget(308, "/properties/leasing-space", requested, "sale"), null);
});

test("Matthews coverage excludes a verified permanent redirect alias separately", () => {
  const coverage = matthewsParsedCoverage([{ id: "active" }, null], 0, 1);
  assert.deepEqual(coverage.listings, [{ id: "active" }]);
  assert.equal(coverage.providerNotFound, 0);
  assert.equal(coverage.permanentRedirectAliases, 1);
  assert.equal(coverage.failures, 0);
  assert.equal(coverage.truncated, false);
});

test("strict Matthews direct fetches explicitly bypass caches", () => {
  const strict = matthewsFetchOptions(true);
  assert.equal(strict.cache, "no-store");
  assert.equal((strict.headers as Record<string, string>)["Cache-Control"], "no-cache");
  assert.ok(strict.signal instanceof AbortSignal);
  assert.equal(strict.redirect, "follow");
  const compatible = matthewsFetchOptions(false);
  assert.equal(compatible.cache, undefined);
});

test("Matthews fetch options abort a stalled request at the supplied timeout", async () => {
  const options = matthewsFetchOptions(true, 1);
  assert.ok(options.signal instanceof AbortSignal);
  assert.equal(options.signal.aborted, false);
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(options.signal.aborted, true);
});

test("Matthews response body deadline aborts a stream that never completes", async () => {
  const controller = new AbortController();
  const never = { text: () => new Promise<string>(() => {}) };
  await assert.rejects(
    matthewsResponseText(never, controller, 1),
    /response body timed out/
  );
  assert.equal(controller.signal.aborted, true);
});

test("Matthews classifies a body transport failure as retryable even after HTTP 200 headers", () => {
  assert.equal(
    matthewsRetryableFetchFailure(200, new Error("Matthews response body timed out after 30000ms")),
    true
  );
  assert.equal(matthewsRetryableFetchFailure(200, null), false);
  assert.equal(matthewsRetryableFetchFailure(429, null), true);
});

test("Matthews full refresh excludes only an alias whose permanent target is in the fresh sitemap", async (t) => {
  const originalFetch = globalThis.fetch;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  const alias = "https://www.matthews.com/properties/dollar-general-2";
  const target = "https://www.matthews.com/properties/stnl-dollar-general-livingston-tx";
  const detail = `<html><head><link rel="canonical" href="${target}"></head><body><h1 id="propertyTitle">Dollar General</h1><div id="propertyAddress">123 Main St, Livingston, TX 77351</div><div id="propertyPrice">$1,000,000</div></body></html>`;
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/sitemap.xml")) {
      return new Response(`<?xml version="1.0"?><urlset><url><loc>${alias}</loc></url><url><loc>${target}</loc></url></urlset>`, { status: 200 });
    }
    if (url === alias) return new Response(null, { status: 308, headers: { location: "/properties/stnl-dollar-general-livingston-tx" } });
    if (url === target) return new Response(detail, { status: 200 });
    throw new Error(`unexpected URL ${url}`);
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
  });

  const result = await srcMatthews("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(result.totalAvailable, 1);
  assert.equal(result.listings.length, 1);
  assert.equal(result.listings[0].url, target);
  assert.equal(result.truncated, false);
  assert.match(result.note ?? "", /permanent redirects/);
});

test("Matthews keeps an unenumerated permanent redirect target as truncation", async (t) => {
  const originalFetch = globalThis.fetch;
  const alias = "https://www.matthews.com/properties/dollar-general-2";
  const active = "https://www.matthews.com/properties/panera-bread";
  globalThis.fetch = async (input) => {
    const url = String(input);
    if (url.endsWith("/sitemap.xml")) {
      return new Response(`<?xml version="1.0"?><urlset><url><loc>${alias}</loc></url><url><loc>${active}</loc></url></urlset>`, { status: 200 });
    }
    if (url === alias) return new Response(null, { status: 308, headers: { location: "/properties/stnl-dollar-general-livingston-tx" } });
    if (url === active) return new Response(`<html><head><link rel="canonical" href="${active}"></head><body><h1 id="propertyTitle">Panera Bread</h1><div id="propertyAddress">123 Main St, Tulsa, OK 74103</div></body></html>`, { status: 200 });
    throw new Error(`unexpected URL ${url}`);
  };
  t.after(() => { globalThis.fetch = originalFetch; });

  const result = await srcMatthews("sale", Number.POSITIVE_INFINITY, false);
  assert.equal(result.totalAvailable, 2);
  assert.equal(result.listings.length, 1);
  assert.equal(result.truncated, true);
  assert.match(result.note ?? "", /failed to fetch, parse, or validate identity/);
});

test("Matthews monitor reports finite sitemap caps as truncation", async (t) => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      `<?xml version="1.0"?>
      <urlset>
        <url><loc>https://www.matthews.com/properties/alpha-sale</loc></url>
        <url><loc>https://www.matthews.com/properties/bravo-sale</loc></url>
      </urlset>`,
      { status: 200 }
    );
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const result = await srcMatthews("sale", 1, true);
  assert.equal(result.totalAvailable, 2);
  assert.equal(result.listings.length, 1);
  assert.equal(result.truncated, true);
  assert.match(result.note ?? "", /Selected 1\/2/);
});

test("Matthews retries a failed HTTP-200 response body before admitting a later response", async (t) => {
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = globalThis.setTimeout;
  let calls = 0;
  globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) => {
    if (typeof delay === "number" && delay >= 1800 && delay < MATTHEWS_FETCH_TIMEOUT_MS) {
      queueMicrotask(() => callback(...args));
      return 0 as unknown as ReturnType<typeof setTimeout>;
    }
    return originalSetTimeout(callback, delay, ...args);
  }) as typeof setTimeout;
  globalThis.fetch = async () => {
    calls += 1;
    if (calls === 1) {
      return {
        ok: true,
        status: 200,
        headers: new Headers(),
        text: async () => {
          throw new Error("Matthews response body timed out after 30000ms");
        },
      } as unknown as Response;
    }
    return new Response("recovered", { status: 200 });
  };
  t.after(() => {
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
  });

  const response = await matthewsFetch("https://www.matthews.com/properties/retry-check");
  assert.equal(calls, 2);
  assert.equal(response.html, "recovered");
});

test("Matthews fetch retries then fails a header stall that ignores abort", async (t) => {
  const originalFetch = globalThis.fetch;
  const originalSetTimeout = globalThis.setTimeout;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return new Promise<Response>(() => {});
  };
  globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) => {
    if (typeof delay === "number" && delay >= 1800) {
      queueMicrotask(() => callback(...args));
      return 0 as unknown as ReturnType<typeof setTimeout>;
    }
    return originalSetTimeout(callback, delay, ...args);
  }) as typeof setTimeout;
  t.after(() => {
    globalThis.fetch = originalFetch;
    globalThis.setTimeout = originalSetTimeout;
  });

  await assert.rejects(
    () => matthewsFetch("https://www.matthews.com/sitemap.xml"),
    /response headers timed out after 30000ms/
  );
  assert.equal(calls, 6);
});
