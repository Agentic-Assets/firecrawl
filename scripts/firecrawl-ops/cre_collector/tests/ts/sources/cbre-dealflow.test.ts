import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  parseCbreDealflowLocation,
  listingPvFromCbreDealflowUrl,
  cbreDealflowUrl,
  extractCbreDealflowEngineKey,
  CBRE_DEALFLOW_FALLBACK_ENGINE_KEY,
  CBRE_DEALFLOW_DETAIL_ATTEMPTS,
  CBRE_DEALFLOW_INVENTORY_TIMEOUT_MS,
  CBRE_DEALFLOW_INVENTORY_MAX_ATTEMPTS,
  CBRE_DEALFLOW_INVENTORY_RETRY_AFTER_MAX_MS,
  cbreDealflowGetText,
  cbreDealflowPostJson,
  cbreDealflowRetryAfterMs,
  cbreDealflowHarvestHtml,
  cbreDealflowStrandedStructured,
  cbreDealflowNewFieldsFromRawData,
  cbreDealflowDetailUnavailableReason,
  cbreDealflowUnavailableCard,
  cbreDealflowUnlinkedCardId,
  parseCbreDealflowCards,
  enrichCbreDealflowCard,
  cbreDealflowNumProjects,
  cbreDealflowAssertPageCount,
  cbreDealflowAssertHtmlOnlyMix,
  cbreDealflowCanonicalUrl,
} from "../../../sources/cbre-dealflow.js";
import { harvestDetail } from "../../../lib/harvest.js";
import {
  createPerformanceRecorder,
  resetPerformanceRecorderForTests,
  setPerformanceRecorderForTests,
} from "../../../lib/performance.js";

// ---------------------------------------------------------------------------
// Fixture helpers
// ---------------------------------------------------------------------------

const __filename = fileURLToPath(import.meta.url);
const __dir = dirname(__filename);
const FIXTURE_PATH = join(__dir, "../../fixtures/raw_data/cbre.json");

function loadFixture(): Array<{ _comment?: string; external_id: string; sourceKey: string; raw_data: any }> {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf8"));
}

test("parseCbreDealflowLocation extracts city and state", () => {
  assert.deepEqual(parseCbreDealflowLocation("Dallas, TX"), {
    city: "Dallas",
    state: "TX",
  });
  assert.deepEqual(parseCbreDealflowLocation("Austin\u201A TX"), {
    city: "Austin",
    state: "TX",
  });
  assert.deepEqual(parseCbreDealflowLocation("Houston, TX 77002"), {
    city: "Houston",
    state: "TX",
  });
});

test("listingPvFromCbreDealflowUrl reads pv query param", () => {
  assert.equal(
    listingPvFromCbreDealflowUrl("https://www.cbredealflow.com/listing?pv=abc123token"),
    "abc123token"
  );
  assert.equal(listingPvFromCbreDealflowUrl("https://www.cbredealflow.com/listing"), null);
  assert.equal(listingPvFromCbreDealflowUrl(null), null);
});

test("cbreDealflowUrl resolves relative links and rejects unsafe schemes", () => {
  assert.equal(
    cbreDealflowUrl("/properties/us-tx-dallas"),
    "https://www.cbredealflow.com/properties/us-tx-dallas"
  );
  assert.equal(cbreDealflowUrl("javascript:alert(1)"), null);
  assert.equal(cbreDealflowUrl("mailto:broker@example.com"), null);
});

test("extractCbreDealflowEngineKey reads ListingEngine key from HTML", () => {
  const html = `
    <script>
      const engine = new ListingEngine({ key: "engine-key-from-script-012345678901234567890" });
    </script>
  `;
  assert.equal(extractCbreDealflowEngineKey(html), "engine-key-from-script-012345678901234567890");
});

test("extractCbreDealflowEngineKey falls back to pv token or default", () => {
  const html = `<a href="/x?pv=${"A".repeat(32)}">link</a>`;
  assert.equal(extractCbreDealflowEngineKey(html), "A".repeat(32));
  assert.equal(extractCbreDealflowEngineKey("<html></html>"), CBRE_DEALFLOW_FALLBACK_ENGINE_KEY);
});

test("CBRE Deal Flow inventory timeout admits slow complete provider pages", () => {
  assert.equal(CBRE_DEALFLOW_INVENTORY_TIMEOUT_MS, 120000);
  assert.ok(
    CBRE_DEALFLOW_INVENTORY_TIMEOUT_MS >= 75000,
    "inventory deadline must exceed the observed 74-second complete-page latency"
  );
});

test("CBRE Deal Flow inventory retries one transient transport failure with an unchanged request", async () => {
  const body = new URLSearchParams({ Start: "1", PageSize: "200", FilterProjectType: "Investment Sale" });
  const expectedBody = body.toString();
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const retries: any[] = [];
  let attempt = 0;
  const result = await cbreDealflowPostJson(
    "/api/AjaxEngine/GetListingsHtml?&pv=opaque-provider-token",
    body,
    {
      request: async (url, init) => {
        calls.push({ url: String(url), init: init ?? {} });
        attempt++;
        if (attempt === 1) {
          body.set("PageSize", "mutated-after-first-attempt");
          throw new Error("socket timed out");
        }
        return new Response(JSON.stringify({ success: true, total: 1, html: "<ul></ul>" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
      sleep: async (ms) => assert.equal(ms, 0),
      retryBackoffMs: 0,
      logRetry: (event) => retries.push(event),
    }
  );

  assert.deepEqual(result, { success: true, total: 1, html: "<ul></ul>" });
  assert.equal(calls.length, 2);
  assert.deepEqual(calls.map((call) => call.url), [
    "https://www.cbredealflow.com/api/AjaxEngine/GetListingsHtml?&pv=opaque-provider-token",
    "https://www.cbredealflow.com/api/AjaxEngine/GetListingsHtml?&pv=opaque-provider-token",
  ]);
  assert.ok(calls.every((call) => call.init.method === "POST" && call.init.body === expectedBody));
  assert.ok(calls.every((call) => call.init.signal instanceof AbortSignal));
  assert.deepEqual(retries, [
    { endpoint: "listings", status: 0, attempt: 1, delayMs: 0, reason: "transport" },
  ]);
  assert.equal(CBRE_DEALFLOW_INVENTORY_MAX_ATTEMPTS, 2);
});

test("CBRE Deal Flow inventory keeps recovery independent from a retry telemetry failure", async () => {
  let calls = 0;
  const result = await cbreDealflowPostJson(
    "/api/Handler/ListingEngine/GetFilters?pv=opaque-provider-token",
    new URLSearchParams({ Start: "1" }),
    {
      request: async () => {
        calls++;
        if (calls === 1) throw new Error("timeout");
        return new Response(JSON.stringify({ success: true }), { status: 200 });
      },
      sleep: async () => undefined,
      logRetry: () => { throw new Error("logging unavailable"); },
    }
  );
  assert.deepEqual(result, { success: true });
  assert.equal(calls, 2);
});

test("CBRE Deal Flow inventory saves recovered retry scheduling in existing performance telemetry", async () => {
  const files = new Map<string, string>();
  const path = "/diagnostics/cbre-retry.json";
  const recorder = createPerformanceRecorder({
    path,
    runId: "2026-09-13T120000Z-abcdef123456",
    commandId: "0123456789abcdef0123456789abcdef",
    processId: 321,
    fs: {
      kind: (candidate) => files.has(candidate) ? "regular" : "missing",
      openExclusive: (candidate) => {
        if (files.has(candidate)) throw new Error("duplicate temporary telemetry path");
        files.set(candidate, "");
        return candidate;
      },
      writeOpened: (handle, contents) => files.set(String(handle), contents),
      closeOpened: () => undefined,
      rename: (from, to) => {
        const contents = files.get(from);
        if (contents === undefined) throw new Error("missing temporary telemetry path");
        files.set(to, contents);
        files.delete(from);
      },
      unlink: (candidate) => { files.delete(candidate); },
    },
    randomHex: () => "a".repeat(32),
  });
  assert.ok(recorder);
  setPerformanceRecorderForTests(recorder);
  let calls = 0;
  try {
    await cbreDealflowPostJson(
      "/api/Handler/ListingEngine/GetFilters?pv=opaque-provider-token",
      new URLSearchParams({ Start: "1" }),
      {
        request: async () => {
          calls++;
          return calls === 1
            ? new Response("transient", { status: 503 })
            : new Response(JSON.stringify({ success: true }), { status: 200 });
        },
        sleep: async () => undefined,
      }
    );
    recorder.flush(true);
    const snapshot = JSON.parse(files.get(path) ?? "");
    assert.deepEqual(snapshot.metrics.requests.retry.http_helper, {
      retry_attempts: 1,
      backoff_ms: 1000,
      terminal_backoff_ms: 0,
    });
  } finally {
    resetPerformanceRecorderForTests();
  }
  assert.equal(calls, 2);
});

test("CBRE Deal Flow inventory honors bounded 429 Retry-After without exposing query data", async () => {
  const delays: number[] = [];
  const retries: any[] = [];
  let calls = 0;
  const result = await cbreDealflowPostJson(
    "/api/Handler/ListingEngine/GetFilters?pv=opaque-provider-token",
    new URLSearchParams({ Start: "1", PageSize: "1" }),
    {
      request: async () => {
        calls++;
        if (calls === 1) {
          return new Response("retry later", {
            status: 429,
            headers: { "retry-after": "7" },
          });
        }
        return new Response(JSON.stringify({ success: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
      sleep: async (ms) => { delays.push(ms); },
      logRetry: (event) => retries.push(event),
    }
  );

  assert.deepEqual(result, { success: true });
  assert.equal(calls, 2);
  assert.deepEqual(delays, [7000]);
  assert.deepEqual(retries, [
    { endpoint: "filters", status: 429, attempt: 1, delayMs: 7000, reason: "http" },
  ]);
  assert.equal(cbreDealflowRetryAfterMs("7", 1), 7000);
  assert.equal(cbreDealflowRetryAfterMs("1.5", 1), null);
  assert.equal(cbreDealflowRetryAfterMs("-1", 1), null);
  assert.equal(cbreDealflowRetryAfterMs("invalid", 1), null);
});

test("CBRE Deal Flow inventory accepts each HTTP-date Retry-After wire format as UTC", () => {
  const at = Date.UTC(2030, 10, 6, 8, 49, 37);
  const now = Date.UTC(2026, 0, 1, 0, 0, 0);
  for (const value of [
    "Wed, 06 Nov 2030 08:49:37 GMT",
    "Wednesday, 06-Nov-30 08:49:37 GMT",
    "Wed Nov  6 08:49:37 2030",
  ]) {
    assert.equal(cbreDealflowRetryAfterMs(value, now), at - now);
  }
  assert.equal(cbreDealflowRetryAfterMs("datejunk", now), null);
});

test("CBRE Deal Flow inventory honors an HTTP-date Retry-After for every retryable provider status", async () => {
  for (const status of [500, 502, 503, 504]) {
    let calls = 0;
    const delays: number[] = [];
    const retryAfter = status === 503 ? new Date(6_000).toUTCString() : null;
    const result = await cbreDealflowPostJson(
      "/api/AjaxEngine/GetListingsHtml?&pv=opaque-provider-token",
      new URLSearchParams({ Start: "1" }),
      {
        request: async () => {
          calls++;
          if (calls === 1) {
            return new Response("transient", {
              status,
              headers: retryAfter ? { "retry-after": retryAfter } : undefined,
            });
          }
          return new Response(JSON.stringify({ success: true, status }), { status: 200 });
        },
        sleep: async (ms) => { delays.push(ms); },
        wallNow: () => 1_000,
      }
    );
    assert.deepEqual(result, { success: true, status });
    assert.equal(calls, 2);
    assert.deepEqual(delays, [status === 503 ? 5_000 : 1_000]);
  }
});

test("CBRE Deal Flow inventory fails closed when a provider embargo exceeds the retry bound", async () => {
  let calls = 0;
  await assert.rejects(
    () => cbreDealflowPostJson(
      "/api/Handler/ListingEngine/GetFilters?pv=opaque-provider-token",
      new URLSearchParams({ Start: "1" }),
      {
        request: async () => {
          calls++;
          return new Response("retry later", {
            status: 429,
            headers: { "retry-after": String(CBRE_DEALFLOW_INVENTORY_RETRY_AFTER_MAX_MS / 1000 + 1) },
          });
        },
        sleep: async () => { throw new Error("must not sleep past provider embargo"); },
      }
    ),
    /filters HTTP 429 exceeded Retry-After bound/
  );
  assert.equal(calls, 1);
});

test("CBRE Deal Flow inventory exhausts only transient server failures with body-free errors", async () => {
  let calls = 0;
  const retries: any[] = [];
  await assert.rejects(
    () => cbreDealflowPostJson(
      "/api/AjaxEngine/GetListingsHtml?&pv=opaque-provider-token",
      new URLSearchParams({ Start: "1" }),
      {
        request: async () => {
          calls++;
          return new Response("provider body must not escape", { status: 503 });
        },
        sleep: async () => undefined,
        logRetry: (event) => retries.push(event),
      }
    ),
    (error: Error) => {
      assert.match(error.message, /listings HTTP 503 exhausted retries after 2 attempt\(s\)/);
      assert.doesNotMatch(error.message, /opaque-provider-token|provider body/);
      return true;
    }
  );
  assert.equal(calls, 2);
  assert.deepEqual(retries, [
    { endpoint: "listings", status: 503, attempt: 1, delayMs: 1000, reason: "http" },
  ]);
});

test("CBRE Deal Flow inventory retries a failed successful-response body read", async () => {
  let calls = 0;
  const result = await cbreDealflowPostJson(
    "/api/Handler/ListingEngine/GetFilters?pv=opaque-provider-token",
    new URLSearchParams({ Start: "1" }),
    {
      request: async () => {
        calls++;
        if (calls === 1) {
          return new Response(new ReadableStream({
            start(controller) {
              controller.error(new Error("provider body read failed"));
            },
          }), { status: 200 });
        }
        return new Response(JSON.stringify({ success: true }), { status: 200 });
      },
      sleep: async () => undefined,
    }
  );
  assert.deepEqual(result, { success: true });
  assert.equal(calls, 2);
});

test("CBRE Deal Flow inventory reports terminal successful-response body transport without provider data", async () => {
  await assert.rejects(
    () => cbreDealflowPostJson(
      "/api/Handler/ListingEngine/GetFilters?pv=opaque-provider-token",
      new URLSearchParams({ Start: "1" }),
      {
        request: async () => new Response(new ReadableStream({
          start(controller) {
            controller.error(new Error("provider body must not escape"));
          },
        }), { status: 200 }),
        sleep: async () => undefined,
      }
    ),
    (error: Error) => {
      assert.match(error.message, /filters HTTP 200 body transport exhausted retries after 2 attempt\(s\)/);
      assert.doesNotMatch(error.message, /opaque-provider-token|provider body/);
      return true;
    }
  );
});

test("CBRE Deal Flow inventory recalculates request timeout from its monotonic deadline", async () => {
  const originalTimeout = AbortSignal.timeout;
  const requestTimeouts: number[] = [];
  let now = 0;
  let calls = 0;
  AbortSignal.timeout = ((ms: number) => {
    requestTimeouts.push(ms);
    return originalTimeout(ms);
  }) as typeof AbortSignal.timeout;
  try {
    await cbreDealflowPostJson(
      "/api/Handler/ListingEngine/GetFilters?pv=opaque-provider-token",
      new URLSearchParams({ Start: "1" }),
      {
        request: async () => {
          calls++;
          return calls === 1
            ? new Response("transient", { status: 503 })
            : new Response(JSON.stringify({ success: true }), { status: 200 });
        },
        sleep: async (ms) => { now += ms; },
        monotonicNow: () => now,
        timeoutMs: 10_000,
        deadlineMs: 15_000,
        retryBackoffMs: 8_000,
      }
    );
  } finally {
    AbortSignal.timeout = originalTimeout;
  }
  assert.equal(calls, 2);
  assert.deepEqual(requestTimeouts, [10_000, 7_000]);
});

test("CBRE Deal Flow inventory prevents another request after elapsed retry time exceeds its deadline", async () => {
  let now = 0;
  let calls = 0;
  await assert.rejects(
    () => cbreDealflowPostJson(
      "/api/Handler/ListingEngine/GetFilters?pv=opaque-provider-token",
      new URLSearchParams({ Start: "1" }),
      {
        request: async () => {
          calls++;
          return new Response("transient", { status: 503 });
        },
        sleep: async () => { now = 1_500; },
        monotonicNow: () => now,
        timeoutMs: 1_000,
        deadlineMs: 1_500,
        retryBackoffMs: 1_000,
      }
    ),
    /filters transport exceeded retry deadline after 1 attempt\(s\)/
  );
  assert.equal(calls, 1);
});

test("CBRE Deal Flow inventory cancels transient response bodies without waiting on cleanup", async () => {
  let cancelled = 0;
  let calls = 0;
  const result = await cbreDealflowPostJson(
    "/api/AjaxEngine/GetListingsHtml?pv=opaque-provider-token",
    new URLSearchParams({ Start: "1" }),
    {
      request: async () => {
        calls++;
        if (calls === 1) {
          return new Response(new ReadableStream({
            cancel() {
              cancelled++;
              return new Promise<void>(() => undefined);
            },
          }), { status: 503 });
        }
        return new Response(JSON.stringify({ success: true }), { status: 200 });
      },
      sleep: async () => undefined,
      retryBackoffMs: 0,
    }
  );
  assert.deepEqual(result, { success: true });
  assert.equal(calls, 2);
  assert.equal(cancelled, 1);
});

test("CBRE Deal Flow inventory enforces its local retry deadline and finite option ceilings", async () => {
  let calls = 0;
  let slept = false;
  await assert.rejects(
    () => cbreDealflowPostJson(
      "/api/Handler/ListingEngine/GetFilters?pv=opaque-provider-token",
      new URLSearchParams({ Start: "1" }),
      {
        request: async () => {
          calls++;
          return new Response("transient", { status: 503 });
        },
        sleep: async () => { slept = true; },
        monotonicNow: () => 0,
        timeoutMs: 10,
        deadlineMs: 10,
        retryBackoffMs: 1000,
      }
    ),
    /filters HTTP 503 exceeded retry deadline/
  );
  assert.equal(calls, 1);
  assert.equal(slept, false);

  calls = 0;
  const delays: number[] = [];
  await assert.rejects(
    () => cbreDealflowPostJson(
      "/api/Handler/ListingEngine/GetFilters?pv=opaque-provider-token",
      new URLSearchParams({ Start: "1" }),
      {
        request: async () => {
          calls++;
          return new Response("transient", { status: 503 });
        },
        sleep: async (ms) => { delays.push(ms); },
        maxAttempts: Infinity,
        retryBackoffMs: Infinity,
        retryAfterMaxMs: Infinity,
        deadlineMs: Number.NaN,
      }
    )
  );
  assert.equal(calls, CBRE_DEALFLOW_INVENTORY_MAX_ATTEMPTS);
  assert.deepEqual(delays, [1000]);
});

test("CBRE Deal Flow inventory does not retry malformed, semantic, non-transient, or unallowlisted calls", async () => {
  const cases = [
    () => new Response("not json", { status: 200 }),
    () => new Response(JSON.stringify({ success: false }), { status: 200 }),
    () => new Response("bad request", { status: 400 }),
    () => new Response("unauthorized", { status: 401 }),
    () => new Response("forbidden", { status: 403 }),
  ];
  for (const makeResponse of cases) {
    let calls = 0;
    await assert.rejects(
      () => cbreDealflowPostJson(
        "/api/Handler/ListingEngine/GetFilters?pv=opaque-provider-token",
        new URLSearchParams({ Start: "1" }),
        {
          request: async () => {
            calls++;
            return makeResponse();
          },
          sleep: async () => { throw new Error("must not retry semantic or client failures"); },
        }
      )
    );
    assert.equal(calls, 1);
  }

  let calls = 0;
  await assert.rejects(
    () => cbreDealflowPostJson(
      "/api/AjaxEngine/Unexpected?pv=opaque-provider-token",
      new URLSearchParams({ Start: "1" }),
      { request: async () => { calls++; return new Response("{}", { status: 200 }); } }
    ),
    /non-allowlisted inventory endpoint/
  );
  assert.equal(calls, 0);
});

test("CBRE Deal Flow detail reads retry transient transport failures", async () => {
  assert.equal(CBRE_DEALFLOW_DETAIL_ATTEMPTS, 3);
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls++;
    if (calls < CBRE_DEALFLOW_DETAIL_ATTEMPTS) {
      throw new Error("transient provider timeout");
    }
    return new Response("<html>fresh detail</html>", { status: 200 });
  }) as typeof fetch;
  try {
    assert.equal(
      await cbreDealflowGetText(
        "https://www.cbredealflow.com/handler/landing.aspx?pv=test",
        CBRE_DEALFLOW_DETAIL_ATTEMPTS,
        0
      ),
      "<html>fresh detail</html>"
    );
    assert.equal(calls, CBRE_DEALFLOW_DETAIL_ATTEMPTS);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("CBRE Deal Flow exhausted linked-detail requests preserve canonical inventory", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls++;
    throw new Error("provider detail unavailable");
  }) as typeof fetch;
  try {
    const listing = await enrichCbreDealflowCard(
      {
        id: "public-card-token",
        url: "https://www.cbredealflow.com/handler/landing.aspx?pv=public-card-token",
        urlKind: "detail",
        listingPv: "public-card-token",
        name: "Current public card",
        transactionType: "Investment Sale",
        assetType: "Office",
        description: "Fresh inventory description",
        city: "Dallas",
        state: "TX",
        country: "United States",
        sizeText: "10,000 sf",
        status: "Available",
        brokerIds: [],
        contactsDetailed: [{ name: "Current Broker" }],
        brochures: [],
        photos: [],
        cbreDealflowCard: { projectType: "Investment Sale" },
      },
      "sale"
    );
    assert.equal(calls, CBRE_DEALFLOW_DETAIL_ATTEMPTS);
    assert.equal(listing.id, "public-card-token");
    assert.equal(
      listing.canonicalUrl,
      "https://www.cbredealflow.com/handler/landing.aspx?pv=public-card-token"
    );
    assert.equal(listing.detailUnavailable.reason, "detail_request_failed");
    assert.equal(listing.detailUnavailable.publicCardObserved, true);
    assert.equal(listing.detailUnavailable.publicPageObserved, undefined);
    assert.equal(listing.preserveChildCollections, true);
    assert.equal(listing.detailError, undefined);
    assert.equal(listing.name, "Current public card");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("CBRE Deal Flow unparseable linked detail remains inventory-backed", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response("<html><body>unexpected provider detail shell</body></html>", {
      status: 200,
    })) as typeof fetch;
  try {
    const listing = await enrichCbreDealflowCard(
      {
        id: "public-card-token",
        url: "https://www.cbredealflow.com/handler/landing.aspx?pv=public-card-token",
        urlKind: "detail",
        listingPv: "public-card-token",
        name: "Current public card",
        transactionType: "Investment Sale",
        assetType: "Office",
        description: null,
        city: "Dallas",
        state: "TX",
        country: "United States",
        sizeText: null,
        status: "Available",
        brokerIds: [],
        contactsDetailed: [],
        brochures: [],
        photos: [],
        cbreDealflowCard: { projectType: "Investment Sale" },
      },
      "sale"
    );
    assert.equal(listing.detailUnavailable.reason, "detail_request_failed");
    assert.equal(listing.preserveChildCollections, true);
    assert.equal(listing.detailError, undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("CBRE Deal Flow malformed linked identity still fails closed", async () => {
  await assert.rejects(
    () =>
      enrichCbreDealflowCard(
        {
          id: "public-card-token",
          url: null,
          urlKind: "detail",
          listingPv: "public-card-token",
          name: "Malformed linked card",
          transactionType: "Investment Sale",
          assetType: "Office",
          description: null,
          city: "Dallas",
          state: "TX",
          country: "United States",
          sizeText: null,
          status: "Available",
          brokerIds: [],
          contactsDetailed: [],
          brochures: [],
          photos: [],
          cbreDealflowCard: { projectType: "Investment Sale" },
        },
        "sale"
      ),
    /linked card is missing its public URL/
  );
});

test("CBRE Deal Flow structured-detail mapping failures still fail closed", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (async () =>
    new Response(
      '<html><script>var data = {"sections":1}</script></html>',
      { status: 200 }
    )) as typeof fetch;
  try {
    await assert.rejects(
      () =>
        enrichCbreDealflowCard(
          {
            id: "public-card-token",
            url: "https://www.cbredealflow.com/handler/landing.aspx?pv=public-card-token",
            urlKind: "detail",
            listingPv: "public-card-token",
            name: "Current public card",
            transactionType: "Investment Sale",
            assetType: "Office",
            description: null,
            city: "Dallas",
            state: "TX",
            country: "United States",
            sizeText: null,
            status: "Available",
            brokerIds: [],
            contactsDetailed: [],
            brochures: [],
            photos: [],
            cbreDealflowCard: { projectType: "Investment Sale" },
          },
          "sale"
        ),
      /detail mapping failed/
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("CBRE Deal Flow recognizes a current card with no configured public landing detail", () => {
  const html = `
    <div>
      The landing page executive summary has been enabled,
      but the landing page has not been setup.
    </div>
  `;
  assert.equal(cbreDealflowDetailUnavailableReason(html), "landing_not_setup");
  assert.equal(
    cbreDealflowDetailUnavailableReason(`
      <html>
        <head><title>State of Florida Surplus Lands | CBRE | Powered by LightBox</title></head>
        <body>
          <div id="ProjectNameAndAddress">
            <div id="ProjectName">State of Florida Surplus Lands</div>
          </div>
          <div id="Content">
            <div class="TabContent">
              <p>This public property landing page is available without an embedded data object.</p>
              <p>Contact the listed brokerage team for current offering information.</p>
            </div>
          </div>
        </body>
      </html>
    `, "State of Florida Surplus Lands"),
    "public_html_only"
  );
  assert.equal(
    cbreDealflowDetailUnavailableReason(`
      <html>
        <head><title>Maintenance | CBRE | Powered by LightBox</title></head>
        <body>
          <script>${"provider shell ".repeat(20)}</script>
          <p>Temporarily unavailable. Please try again later.</p>
        </body>
      </html>
    `, "Maintenance"),
    null
  );
  assert.equal(
    cbreDealflowDetailUnavailableReason(`
      <html>
        <head><title>Different Property | CBRE | Powered by LightBox</title></head>
        <body>
          <div id="ProjectNameAndAddress">
            <div id="ProjectName">Different Property</div>
          </div>
          <div id="Content">
            <div class="TabContent">
              <p>This is a substantive public property page with enough visible body text to pass the length floor.</p>
            </div>
          </div>
        </body>
      </html>
    `, "Expected Property"),
    null
  );
  assert.equal(cbreDealflowDetailUnavailableReason("<html>unexpected empty page</html>"), null);
});

test("CBRE Deal Flow HTML-only classification fails closed on a layout-wide anomaly", () => {
  const listing = (reason: string | null) => ({
    detailUnavailable: reason ? { reason } : undefined,
  });
  assert.equal(
    cbreDealflowAssertHtmlOnlyMix([
      ...Array.from({ length: 5 }, () => listing("public_html_only")),
      ...Array.from({ length: 95 }, () => listing(null)),
    ]),
    5
  );
  assert.throws(
    () =>
      cbreDealflowAssertHtmlOnlyMix([
        ...Array.from({ length: 6 }, () => listing("public_html_only")),
        ...Array.from({ length: 94 }, () => listing(null)),
      ]),
    /public_html_only anomaly/
  );
});

test("CBRE Deal Flow unavailable detail preserves prior children and keeps fresh card fields", () => {
  const row = cbreDealflowUnavailableCard(
    {
      id: "public-card-token",
      url: "https://www.cbredealflow.com/handler/landing.aspx?pv=public-card-token",
      urlKind: "detail",
      listingPv: "public-card-token",
      name: "Current public card",
      transactionType: "Investment Sale",
      assetType: "Multifamily",
      description: "Fresh card description",
      city: "New York",
      state: "NY",
      country: "United States",
      sizeText: "9,766 sf",
      status: "Available",
      brokerIds: [],
      contactsDetailed: [{ name: "Current Broker" }],
      brochures: [],
      photos: ["https://example.test/current.jpg"],
      cbreDealflowCard: { projectType: "Investment Sale" },
    },
    "landing_not_setup"
  );
  assert.equal(row.detailUnavailable.reason, "landing_not_setup");
  assert.equal(row.detailUnavailable.publicPageObserved, true);
  assert.equal(row.preserveChildCollections, true);
  assert.equal(
    row.canonicalUrl,
    "https://www.cbredealflow.com/handler/landing.aspx?pv=public-card-token"
  );
  assert.equal(row.statusBadge, "Available");
  assert.deepEqual(row.extraFacts, { project_type: "Investment Sale" });
  assert.equal(row.name, "Current public card");

  const htmlOnly = cbreDealflowUnavailableCard(
    {
      ...row,
      urlKind: "detail",
      cbreDealflowCard: { projectType: "Investment Sale" },
    },
    "public_html_only"
  );
  assert.equal(htmlOnly.detailUnavailable.publicPageObserved, true);
});

test("CBRE Deal Flow canonical URL never falls back to agreement or brochure material", () => {
  assert.equal(
    cbreDealflowCanonicalUrl({
      urlKind: "agreement",
      url: "https://www.cbredealflow.com/buyer/agreement?pv=agreement-token",
    }),
    undefined
  );
  assert.equal(
    cbreDealflowCanonicalUrl({
      urlKind: "brochure",
      url: "https://www.cbredealflow.com/files/public-brochure.pdf",
    }),
    undefined
  );
  assert.equal(
    cbreDealflowCanonicalUrl({
      urlKind: "detail",
      url: "https://www.cbredealflow.com/handler/landing.aspx?pv=property-token",
    }),
    "https://www.cbredealflow.com/handler/landing.aspx?pv=property-token"
  );
});

test("CBRE Deal Flow retains agreement-gated and unlinked provider cards", () => {
  const html = `
    <ul class="gridview">
      <li class="item">
        <div class="card">
          <div class="img"><a class="summary" href="/buyer/agreement?pv=agreement-token"><p>Agreement listing</p></a></div>
          <div class="headline">Agreement listing</div>
          <div class="location"><div class="city">Dallas, TX</div></div>
          <span class="asset">Retail</span><span class="status">Available</span>
          <div class="details">Investment Sale | 10,000 sq ft</div>
        </div>
      </li>
      <li class="item">
        <div class="card">
          <div class="img"><a class="summary"><p>Coming soon listing</p></a></div>
          <div class="headline">Coming soon listing</div>
          <div class="location"><div class="city">Austin, TX</div></div>
          <span class="asset">Industrial</span><span class="status">Coming Soon</span>
          <div class="details">Investment Sale | 20,000 sq ft</div>
        </div>
      </li>
    </ul>
  `;
  const cards = parseCbreDealflowCards(html, "sale");
  assert.equal(cards.length, 2);
  assert.equal(cards[0]?.urlKind, "agreement");
  assert.equal(cards[0]?.listingPv, "agreement-token");
  assert.equal(cards[1]?.urlKind, "unlinked");
  assert.match(cards[1]?.id ?? "", /^card:[0-9a-f]{24}$/);
  assert.equal(cards[1]?.cbreDealflowCard.cardIdentity, cards[1]?.id);
  assert.match(cards[0]?.cbreDealflowCard.cardIdentity ?? "", /^card:[0-9a-f]{24}$/);
  assert.equal(cards[1]?.url, null);
});

test("CBRE Deal Flow unlinked-card identity is deterministic and rejects nameless cards", () => {
  const fields = {
    name: "Coming Soon Listing",
    city: "Austin",
    state: "TX",
    assetType: "Industrial",
  };
  assert.equal(cbreDealflowUnlinkedCardId(fields), cbreDealflowUnlinkedCardId(fields));
  assert.notEqual(
    cbreDealflowUnlinkedCardId(fields),
    cbreDealflowUnlinkedCardId({ ...fields, city: "Dallas" })
  );
  assert.equal(cbreDealflowUnlinkedCardId({ ...fields, name: null }), null);
});

test("CBRE Deal Flow pagination count fails closed when malformed", () => {
  assert.equal(cbreDealflowNumProjects(200, 1), 200);
  assert.equal(cbreDealflowNumProjects("0", 2001), 0);
  for (const invalid of [undefined, null, "not-a-number", -1, 1.5, Number.NaN]) {
    assert.throws(() => cbreDealflowNumProjects(invalid, 1), /invalid numProjects/);
  }
  assert.equal(cbreDealflowAssertPageCount(200, 200, 1), 200);
  assert.throws(() => cbreDealflowAssertPageCount(0, 200, 1), /parity failed/);
  assert.throws(() => cbreDealflowAssertPageCount(201, 200, 1), /parity failed/);
});

test("CBRE Deal Flow classifies agreement and unlinked cards without a failing detail request", async () => {
  const base = {
    id: "card-id",
    url: "https://www.cbredealflow.com/",
    listingPv: "card-id",
    name: "Current card",
    transactionType: "Investment Sale",
    assetType: "Office",
    description: null,
    city: "Dallas",
    state: "TX",
    country: "United States",
    sizeText: null,
    status: "Available",
    brokerIds: [],
    photos: [],
    cbreDealflowCard: { projectType: "Investment Sale" },
  };
  const agreement = await enrichCbreDealflowCard(
    { ...base, urlKind: "agreement", url: "https://www.cbredealflow.com/buyer/agreement?pv=card-id" },
    "sale"
  );
  const unlinked = await enrichCbreDealflowCard(
    { ...base, id: "card:fixture", listingPv: null, urlKind: "unlinked", url: null },
    "sale"
  );
  const brochure = await enrichCbreDealflowCard(
    {
      ...base,
      urlKind: "brochure",
      url: "https://www.cbredealflow.com/buyer/brochure?pv=card-id",
      brochures: [{ name: "Public brochure", url: "https://www.cbredealflow.com/buyer/brochure?pv=card-id" }],
    },
    "sale"
  );
  assert.equal(agreement.detailUnavailable.reason, "gated_agreement");
  assert.equal(agreement.id, "card:pv:card-id");
  assert.equal(agreement.inventoryOnly.reason, "no_public_property_page");
  assert.equal(agreement.inventoryOnly.indexUrl, "https://www.cbredealflow.com/");
  assert.equal(unlinked.detailUnavailable.reason, "card_not_linked");
  assert.equal(unlinked.provisionalIdentity.historyContinuity, "not_guaranteed");
  assert.equal(unlinked.inventoryOnly.reason, "no_provider_id_or_listing_url");
  assert.equal(unlinked.inventoryOnly.indexUrl, "https://www.cbredealflow.com/");
  assert.equal(brochure.detailUnavailable.reason, "public_brochure_only");
  assert.equal(brochure.id, "card:pv:card-id");
  assert.equal(brochure.inventoryOnly.reason, "no_public_property_page");
  assert.equal(brochure.brochures.length, 1);
  assert.equal(agreement.detailUnavailable.publicCardObserved, true);
  assert.equal(agreement.detailUnavailable.publicPageObserved, undefined);
  assert.equal(unlinked.detailUnavailable.publicPageObserved, undefined);
  assert.equal(agreement.preserveChildCollections, true);
  assert.equal(unlinked.preserveChildCollections, true);
});

test("cbreDealflowHarvestHtml concatenates page html + section content fragments", () => {
  const data = {
    sections: [
      { contents: [{ content: '<iframe src="https://player.vimeo.com/video/999"></iframe>' }] },
      { contents: [{ content: "<p>no media here</p>" }] },
    ],
  };
  const html = cbreDealflowHarvestHtml("<html><body>page</body></html>", data);
  assert.match(html, /player\.vimeo\.com\/video\/999/);
  // The harvester picks the embedded iframe up as media.
  const out = harvestDetail({ rawHtml: html } as any, {});
  assert.ok(out.media.some((m) => m.provider === "vimeo"));
});

test("cbreDealflowStrandedStructured lifts caprate/noi/occupancy/year/units; empty for sparse", () => {
  const out = cbreDealflowStrandedStructured({
    projectfields: {
      caprate: "6.75%",
      noi: "$1,250,000",
      occupancy: "92%",
      yearbuilt: "1995",
      units: "150",
      zoning: "MU-1",
    },
  });
  assert.equal(out.capRatePct, 6.75);
  assert.equal(out.noi, 1250000);
  assert.equal(out.occupancyRate, 92);
  assert.equal(out.yearBuilt, 1995);
  assert.equal(out.units, 150);
  assert.equal(out.zoning, "MU-1");
  assert.deepEqual(cbreDealflowStrandedStructured({}), {});
});

// ---------------------------------------------------------------------------
// WS1: cbreDealflowNewFieldsFromRawData - fixture-driven tests
// ---------------------------------------------------------------------------

test("cbreDealflowNewFieldsFromRawData: dealflow fixture row yields statusBadge, contacts phone/title, extraFacts", () => {
  const fixture = loadFixture();
  const row = fixture.find((r) => r.external_id === "dealflow:150532")!;
  assert.ok(row, "fixture row dealflow:150532 must exist");
  const out = cbreDealflowNewFieldsFromRawData(row.raw_data);
  // statusBadge from cbreDealflowDetail.status (preferred) or card status
  assert.equal(out.statusBadge, "Available");
  // contactsDetailed with phone and title
  assert.ok(out.contactsDetailedWithPhoneAndTitle.length >= 2, "must have at least 2 contacts");
  const firstContact = out.contactsDetailedWithPhoneAndTitle[0]!;
  assert.equal(firstContact.name, "Ben Galles");
  assert.equal(firstContact.phone, "775 750 6429");
  assert.equal(firstContact.title, "Senior Vice President");
  const secondContact = out.contactsDetailedWithPhoneAndTitle[1]!;
  assert.equal(secondContact.name, "Katie Galles");
  assert.equal(secondContact.phone, "+1 775 772 6181");
  assert.equal(secondContact.title, "Senior Associate");
  // extraFacts from cbreDealflowDetail.projectType
  assert.ok(out.extraFacts !== null, "extraFacts must be non-null when projectType is present");
  assert.equal(out.extraFacts?.project_type, "Value Add");
});

test("cbreDealflowNewFieldsFromRawData: absent cbreDealflowDetail yields nulls", () => {
  const out = cbreDealflowNewFieldsFromRawData({
    status: "Available",
    contactsDetailed: [{ name: "Jane Smith", phone: "555-1234", title: null }],
  });
  // statusBadge falls back to card-level status when cbreDealflowDetail is absent
  assert.equal(out.statusBadge, "Available");
  assert.equal(out.contactsDetailedWithPhoneAndTitle[0]?.phone, "555-1234");
  assert.equal(out.contactsDetailedWithPhoneAndTitle[0]?.title, null);
  // extraFacts null when no projectType
  assert.equal(out.extraFacts, null);
});

test("cbreDealflowNewFieldsFromRawData: no status, no contacts, no projectType -> all null", () => {
  const out = cbreDealflowNewFieldsFromRawData({});
  assert.equal(out.statusBadge, null);
  assert.deepEqual(out.contactsDetailedWithPhoneAndTitle, []);
  assert.equal(out.extraFacts, null);
});

test("cbreDealflowNewFieldsFromRawData: null input does not throw", () => {
  assert.doesNotThrow(() => {
    const out = cbreDealflowNewFieldsFromRawData(null);
    assert.equal(out.statusBadge, null);
    assert.deepEqual(out.contactsDetailedWithPhoneAndTitle, []);
    assert.equal(out.extraFacts, null);
  });
});

test("cbreDealflowNewFieldsFromRawData: statusBadge is NOT written to status field (routes through OPT-IN gate)", () => {
  // statusBadge must NOT be 'status'; it is the statusBadge camelCase key that routes through
  // the OPT-IN activation gate in cre_ingest.py and never auto-activates.
  const out = cbreDealflowNewFieldsFromRawData({ cbreDealflowDetail: { status: "Sold" }, contactsDetailed: [] });
  assert.equal(out.statusBadge, "Sold");
  // Confirm the returned shape has no 'status' key (that would bypass the gate)
  assert.ok(!("status" in out), "returned shape must not contain a direct 'status' key");
});
