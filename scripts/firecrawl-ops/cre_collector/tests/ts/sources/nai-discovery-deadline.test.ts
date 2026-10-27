process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import test from "node:test";
import { performance } from "node:perf_hooks";
import {
  discoverNaiSourceIds,
  NAI_SOURCE_IDS,
  NAI_SOURCE_DISCOVERY_MAX_BODY_BYTES,
} from "../../../sources/nai-global.js";

const widget = "https://ab.infabode.test/nai-global/listings3";
const first = "https://ab.infabode.test/_next/static/chunks/first.js";
const next = "https://ab.infabode.test/_next/static/chunks/next.js";
const valid = `[${NAI_SOURCE_IDS.map((id, index) =>
  `{id:${id},name:"${index === 0 ? "NAI Global" : `NAI Office ${index}`}",country:"United States"}`
).join(",")}]`;

for (const strict of [true, false]) {
  test(`NAI oversized discovery cancellation stalls fail closed, strict=${strict}`, async (t) => {
    const requests: string[] = [];
    let cancellations = 0;
    let released = false;
    let aborted = false;
    let release!: () => void;
    const cancellation = new Promise<void>((resolve) => { release = resolve; });
    const fallbackTimer = setTimeout(() => { released = true; release(); }, 2000);
    t.after(() => { clearTimeout(fallbackTimer); release(); });
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new Uint8Array(NAI_SOURCE_DISCOVERY_MAX_BODY_BYTES + 1)); },
      cancel() { cancellations++; return cancellation; },
    });
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      requests.push(url);
      if (url === widget) return new Response(`<script src="${first}"></script><script src="${next}"></script>`);
      if (url === first) {
        init?.signal?.addEventListener("abort", () => { aborted = true; });
        return new Response(stream);
      }
      if (url === next) return new Response(valid);
      throw new Error(`unexpected fixture request ${url}`);
    }) as typeof fetch;
    const started = performance.now();
    const outcome = await discoverNaiSourceIds({ fetchImpl, strict, widgetUrl: widget, timeoutMs: 80 })
      .then((value) => ({ value, error: null }), (error: unknown) => ({ value: null, error }));
    const elapsed = performance.now() - started;
    t.diagnostic(JSON.stringify({ strict, elapsed, requests, cancellations, released, aborted, rejected: outcome.error !== null }));
    assert.equal(cancellations, 1);
    assert.equal(aborted, true);
    assert.equal(released, false, "settles before fixture releases pending cancellation");
    assert.ok(elapsed < 1500, `80ms request plus separate bounded cleanup allowance took ${elapsed}ms`);
    assert.deepEqual(requests, [widget, first], "unsettled cancellation must not dispatch next script");
    assert.equal(outcome.value, null, "unsettled cancellation must not authorize non-strict fallback");
    assert.ok(outcome.error instanceof Error);
  });
}

test("NAI acknowledged oversized cancellation permits next eligible script", async () => {
  const requests: string[] = [];
  let cancellations = 0;
  const fetchImpl = (async (input: RequestInfo | URL) => {
    const url = String(input); requests.push(url);
    if (url === widget) return new Response(`<script src="${first}"></script><script src="${next}"></script>`);
    if (url === first) return new Response(new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new Uint8Array(NAI_SOURCE_DISCOVERY_MAX_BODY_BYTES + 1)); },
      cancel() { cancellations++; },
    }));
    if (url === next) return new Response(valid);
    throw new Error(`unexpected fixture request ${url}`);
  }) as typeof fetch;
  const result = await discoverNaiSourceIds({ fetchImpl, strict: true, widgetUrl: widget, timeoutMs: 80 });
  assert.equal(cancellations, 1);
  assert.deepEqual(requests, [widget, first, next]);
  assert.deepEqual(result.sourceIds, NAI_SOURCE_IDS);
  assert.equal(result.usedFallback, false);
});

test("NAI ordinary acknowledged HTTP failure retains documented monitor fallback", async () => {
  const requests: string[] = [];
  const fetchImpl = (async (input: RequestInfo | URL) => {
    requests.push(String(input)); return new Response("unavailable", { status: 503 });
  }) as typeof fetch;
  const result = await discoverNaiSourceIds({ fetchImpl, strict: false, widgetUrl: widget, timeoutMs: 80 });
  assert.deepEqual(requests, [widget]);
  assert.equal(result.usedFallback, true);
  assert.deepEqual(result.sourceIds, NAI_SOURCE_IDS);
});
for (const failure of ["read-error", "abort-error"] as const) {
  test(`NAI native ${failure} closure retains ordinary monitor fallback`, async (t) => {
    const originalError = failure === "abort-error"
      ? new DOMException("Owned native abort failure", "AbortError")
      : new Error("Owned native reader transport failure");
    let cancellations = 0;
    let aborted = false;
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const requests: string[] = [];
    const stream = new ReadableStream<Uint8Array>({
      start(value) { controller = value; if (failure === "read-error") value.error(originalError); },
      cancel() { cancellations++; },
    });
    const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push(String(input));
      init?.signal?.addEventListener("abort", () => { aborted = true; controller.error(originalError); });
      return new Response(stream);
    }) as typeof fetch;
    const result = await discoverNaiSourceIds({ fetchImpl, strict: false, widgetUrl: widget, timeoutMs: 80 });
    t.diagnostic(JSON.stringify({ failure, requests, cancellations, aborted, usedFallback: result.usedFallback }));
    assert.deepEqual(requests, [widget]);
    assert.equal(result.usedFallback, true);
    assert.deepEqual(result.sourceIds, NAI_SOURCE_IDS);
    // Native streams reject cancel() with their original stored read error;
    // exact reader.closed error identity proves closure despite that rejection.
    assert.equal(cancellations, 0);
    if (failure === "abort-error") assert.equal(aborted, true);
  });
}

for (const failure of ["throw", "reject"] as const) {
  test(`NAI unrelated ${failure} cancellation cannot authorize continuation or fallback`, async (t) => {
    const requests: string[] = [];
    let cancellations = 0;
    const cancelError = new Error(`Owned unrelated ${failure} cancellation failure`);
    const fetchImpl = (async (input: RequestInfo | URL) => {
      const url = String(input); requests.push(url);
      if (url === widget) return new Response(`<script src="${first}"></script><script src="${next}"></script>`);
      if (url === first) return new Response(new ReadableStream<Uint8Array>({
        start(controller) { controller.enqueue(new Uint8Array(NAI_SOURCE_DISCOVERY_MAX_BODY_BYTES + 1)); },
        cancel() { cancellations++; if (failure === "throw") throw cancelError; return Promise.reject(cancelError); },
      }));
      if (url === next) return new Response(valid);
      throw new Error(`unexpected fixture request ${url}`);
    }) as typeof fetch;
    const started = performance.now();
    const outcome = await discoverNaiSourceIds({ fetchImpl, strict: false, widgetUrl: widget, timeoutMs: 80 })
      .then((value) => ({ value, error: null }), (error: unknown) => ({ value: null, error }));
    const elapsed = performance.now() - started;
    t.diagnostic(JSON.stringify({ failure, elapsed, requests, cancellations, rejected: outcome.error !== null }));
    assert.equal(cancellations, 1);
    assert.ok(elapsed < 1500);
    assert.deepEqual(requests, [widget, first]);
    assert.equal(outcome.value, null);
    assert.ok(outcome.error instanceof Error);
  });
}