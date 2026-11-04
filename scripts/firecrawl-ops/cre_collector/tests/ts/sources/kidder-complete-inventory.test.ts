import assert from "node:assert/strict";
import { createServer, type RequestListener } from "node:http";
import { once } from "node:events";
import { gzipSync } from "node:zlib";
import test from "node:test";
import { observationNow } from "../../../lib/observation-clock.js";
import {
  assertKidderCompleteInventory,
  kidderFetchAll,
  kidderFetchCompleteInventory,
  KIDDER_INVENTORY_TIMEOUT_MS,
  KIDDER_MAX_RESPONSE_BYTES,
  KIDDER_STRICT_MAX_RESULTS,
} from "../../../sources/kidder-mathews.js";

function inventory(ids = ["one", "two"]): any {
  return {
    startIndex: 0,
    totalResultCount: ids.length,
    currentResultCount: ids.length,
    results: ids.map((listing_key) => ({ listing_key, property_name: listing_key })),
  };
}

async function withServer(
  listener: RequestListener,
  run: (fetchImpl: typeof fetch, url: string) => Promise<void>
): Promise<void> {
  const server = createServer(listener);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert(address && typeof address === "object");
  const url = "http://127.0.0.1:" + address.port + "/";
  const fetchImpl: typeof fetch = (_requested, options) => fetch(url, options);
  try { await run(fetchImpl, url); }
  finally {
    await new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve());
      server.closeAllConnections();
    });
  }
}

test("complete Kidder response retains independent total, exact rows and unique IDs", () => {
  assert.doesNotThrow(() => assertKidderCompleteInventory(inventory()));
  assert.doesNotThrow(() => assertKidderCompleteInventory(inventory([])));
  assert.doesNotThrow(() => assertKidderCompleteInventory(
    inventory(Array.from({ length: 8000 }, (_, index) => String(index + 1)))
  ));
  for (const total of [null, undefined, "2", true, -1, 1.5, NaN, Infinity, 8001]) {
    assert.throws(() => assertKidderCompleteInventory({ ...inventory(), totalResultCount: total }), /8000-row ceiling/);
  }
  for (const changed of [
    { results: null }, { results: [] }, { totalResultCount: 3 }, { currentResultCount: 1 },
    { currentResultCount: "2" }, { startIndex: 50 }, { startIndex: "0" },
    { results: [{ listing_key: "same" }, { listing_key: "same" }] },
    { results: [{ listing_key: "same", property_name: "A" }, { listing_key: "same", property_name: "B" }] },
    { results: [{ property_key: "fallback" }, { listing_key: "two" }] },
  ]) {
    assert.throws(() => assertKidderCompleteInventory({ ...inventory(), ...changed }));
  }
  const capped = inventory(Array.from({ length: 50 }, (_, index) => String(index)));
  assert.throws(() => assertKidderCompleteInventory({ ...capped, totalResultCount: 3104 }), /independent provider total/);
  assert.throws(() => assertKidderCompleteInventory([]), /object with a results array/);
});

test("strict request uses one frontend-sized POST without redirects or a silent cap fallback", async () => {
  let calls = 0;
  const fake: typeof fetch = async (url, options) => {
    calls++;
    assert.equal(url, "https://services.kidder.com/search/public/listing");
    assert.equal(options?.method, "POST");
    assert.deepEqual(JSON.parse(String(options?.body)), {
      startIndex: 0, numResults: 8000, includeAggregations: false,
    });
    assert.equal(options?.cache, "no-store");
    assert.equal(options?.redirect, "error");
    assert(options?.signal);
    assert.equal(new Headers(options?.headers).get("Cache-Control"), "no-cache");
    return new Response(JSON.stringify(inventory()));
  };
  assert.equal((await kidderFetchCompleteInventory(fake)).results.length, 2);
  assert.equal(calls, 1);
  assert.equal(KIDDER_STRICT_MAX_RESULTS, 8000);
  assert.equal(KIDDER_INVENTORY_TIMEOUT_MS, 30_000);
  assert.equal(KIDDER_MAX_RESPONSE_BYTES, 32 * 1024 * 1024);
  const bad: typeof fetch = async () => {
    calls++;
    return new Response(JSON.stringify({ ...inventory(), totalResultCount: 3 }));
  };
  await assert.rejects(kidderFetchCompleteInventory(bad), /independent provider total/);
  assert.equal(calls, 2, "strict failures do not retry unsorted pages or deduplicate");
});

test("transport rejects malformed JSON, missing bodies, HTTP failures and byte limits", async () => {
  for (const response of [
    new Response("<html>not JSON</html>"),
    new Response(JSON.stringify({ results: [] })),
    new Response(null),
    new Response("error", { status: 500 }),
    new Response("redirect", { status: 302 }),
    new Response(new Uint8Array([0xff, 0xfe])),
  ]) {
    await assert.rejects(kidderFetchCompleteInventory(async () => response));
  }
  await assert.rejects(kidderFetchCompleteInventory(
    async () => new Response("{}", { headers: { "content-length": "1000" } }), 1000, 100
  ), /exceeds 100 bytes/);
  await assert.rejects(kidderFetchCompleteInventory(
    async () => new Response(JSON.stringify(inventory())), 1000, 10
  ), /exceeds 10 bytes/);
  for (const [timeout, bytes] of [[0, 100], [30_001, 100], [100, 0], [100, 32 * 1024 * 1024 + 1]]) {
    await assert.rejects(kidderFetchCompleteInventory(
      async () => { throw new Error("must not fetch"); }, timeout, bytes
    ), /transport bounds/);
  }
});

test("actual HTTP deadline covers delayed headers and a stalled response body", async () => {
  await withServer((_request, _response) => {}, async (fetchImpl) => {
    const started = performance.now();
    await assert.rejects(kidderFetchCompleteInventory(fetchImpl, 50));
    assert(performance.now() - started < 1000);
  });
  await withServer((_request, response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.write('{"results":[');
  }, async (fetchImpl) => {
    const started = performance.now();
    await assert.rejects(kidderFetchCompleteInventory(fetchImpl, 50));
    assert(performance.now() - started < 1000);
  });
});

test("decoded body cap applies to gzip responses and redirects never reach their destination", async () => {
  const zipped = gzipSync(JSON.stringify({ padding: "x".repeat(20_000) }));
  await withServer((_request, response) => {
    response.writeHead(200, {
      "content-type": "application/json", "content-encoding": "gzip",
      "content-length": String(zipped.length),
    });
    response.end(zipped);
  }, async (fetchImpl) => {
    assert(zipped.length < 1000);
    await assert.rejects(kidderFetchCompleteInventory(fetchImpl, 1000, 1000), /exceeds 1000 bytes/);
  });
  let destinationRequests = 0;
  await withServer((_request, response) => {
    destinationRequests++;
    response.end(JSON.stringify(inventory()));
  }, async (_destinationFetch, destination) => {
    await withServer((_request, response) => {
      response.writeHead(302, { location: destination });
      response.end();
    }, async (fetchImpl) => {
      await assert.rejects(kidderFetchCompleteInventory(fetchImpl, 1000));
    });
  });
  assert.equal(destinationRequests, 0);
});

test("legacy 50-row request stays unchanged; strict cache is generation-bound and fresh", async () => {
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  const originalFetch = globalThis.fetch;
  let calls = 0;
  let bad = false;
  globalThis.fetch = async (_url, options) => {
    calls++;
    const request = JSON.parse(String(options?.body));
    if (process.env.CRE_REQUIRE_FRESH_DETAILS === "1") assert.equal(request.numResults, 8000);
    else assert.equal(request.numResults, 50);
    return new Response(JSON.stringify(bad ? { ...inventory(), currentResultCount: 1 } : inventory()));
  };
  try {
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    delete process.env.CRE_REFRESH_GENERATION;
    await assert.rejects(kidderFetchAll(Infinity), /requires CRE_REFRESH_GENERATION/);
    assert.equal(calls, 0);
    process.env.CRE_REQUIRE_FRESH_DETAILS = "0";
    await kidderFetchAll(Infinity);
    assert.equal(calls, 1);
    process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "kidder-complete-one";
    const before = Date.parse(observationNow());
    const first = await kidderFetchAll(Infinity);
    assert.equal(calls, 2, "a legacy cache cannot satisfy strict freshness");
    assert.equal(first.strictValidated, true);
    assert.equal(first.truncated, false);
    assert.equal(first.generationId, "kidder-complete-one");
    assert(Date.parse(first.inventoryObservedAt) >= before);
    assert(Date.parse(first.inventoryObservedAt) <= Date.parse(observationNow()));
    const cached = await kidderFetchAll(Infinity);
    assert.equal(calls, 2);
    assert.equal(cached.inventoryObservedAt, first.inventoryObservedAt);
    assert.equal(cached.items[0].__creInventoryCacheDisposition, undefined);
    process.env.CRE_REFRESH_GENERATION = "kidder-complete-two";
    await kidderFetchAll(Infinity);
    assert.equal(calls, 3);
    process.env.CRE_REFRESH_GENERATION = "kidder-complete-three";
    bad = true;
    await assert.rejects(kidderFetchAll(Infinity), /independent provider total/);
    bad = false;
    await kidderFetchAll(Infinity);
    assert.equal(calls, 5, "failed proof must never be cached as complete");
  } finally {
    globalThis.fetch = originalFetch;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
  }
});
