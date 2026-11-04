import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  fetchMarcusDetailHtml, marcusPost, enrichMarcusListing,
  prepareMarcusDetailCache, appendMarcusDetailCache, readMarcusDetailCache,
} from "../../../sources/marcus-millichap.js";
import { summarizeListingFreshness } from "../../../lib/freshness.js";
import { createPerformanceRecorder, setPerformanceRecorderForTests, resetPerformanceRecorderForTests } from "../../../lib/performance.js";

const URL = "https://www.marcusmillichap.com/properties/fixture";
const DEADLINE = 37;
const HTML = `<html><main><h1>Recovered property</h1>
<div class="score-hero-body"><p>10 Main Street, Dallas, TX 75201</p></div>
<div class="mm-property-investment-overview"><p>Full useful property description.</p></div>
<div class="mm-property-documents-button"><a href="https://public.example/om.pdf">Offering memorandum</a></div>
<img src="https://mmimageservice.azurewebsites.net/api/image/property/fixture.jpg">
<iframe src="https://www.youtube.com/embed/fixture"></iframe></main></html>`;
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };

function fakeClock(t: TestContext) {
  const original = globalThis.fetch;
  t.mock.timers.enable({ apis: ["setTimeout"] });
  t.after(() => { globalThis.fetch = original; });
}

function observe<T>(promise: Promise<T>) {
  let result: { value: T } | { error: unknown } | undefined;
  void promise.then(value => { result = { value }; }, error => { result = { error }; });
  return () => result;
}

async function advance(t: TestContext, milliseconds: number) {
  t.mock.timers.tick(milliseconds);
  await flush();
}

async function untilSettled<T>(t: TestContext, result: () => T | undefined, budget: number) {
  for (let elapsed = 0; elapsed <= budget && result() === undefined; elapsed += 10) {
    await advance(t, 10);
  }
  assert.notEqual(result(), undefined, "deadline plus finite settlement wait must return");
}

for (const stage of ["headers", "body"] as const) {
  test(`Marcus retries a settled ${stage} deadline once without overlapping transport`, async t => {
    fakeClock(t);
    let calls = 0, active = 0, maximum = 0, aborts = 0;
    const urls: string[] = [];
    globalThis.fetch = (async (url, init) => {
      urls.push(String(url)); calls++; active++; maximum = Math.max(maximum, active);
      if (calls === 2) { active--; return new Response(HTML); }
      const stalled = new Promise<string>((_, reject) => {
        init!.signal!.addEventListener("abort", () => {
          aborts++; active--; reject(new DOMException("aborted", "AbortError"));
        }, { once: true });
      });
      if (stage === "headers") return stalled.then(() => new Response("unused"));
      const response = new Response("unused"); response.text = () => stalled;
      return response;
    }) as typeof fetch;
    const result = observe(fetchMarcusDetailHtml(URL, DEADLINE));
    await flush(); await advance(t, DEADLINE); await untilSettled(t, result, 1200);
    assert.deepEqual(result(), { value: HTML });
    assert.deepEqual(urls, [URL, URL]);
    assert.equal(maximum, 1); assert.equal(aborts, 1); assert.equal(active, 0);
  });
}

test("Marcus repeated settled detail deadlines stop after exactly two attempts", async t => {
  fakeClock(t); let calls = 0;
  globalThis.fetch = ((_url, init) => {
    calls++;
    return new Promise<Response>((_, reject) => init!.signal!.addEventListener("abort",
      () => reject(new DOMException("aborted", "AbortError")), { once: true }));
  }) as typeof fetch;
  const result = observe(fetchMarcusDetailHtml(URL, DEADLINE));
  await flush(); await advance(t, DEADLINE); await advance(t, DEADLINE);
  await untilSettled(t, result, 1200);
  assert.ok(result() && "error" in result()!); assert.equal(calls, 2);
  await advance(t, 5000); assert.equal(calls, 2);
});

test("Marcus waits for delayed local settlement and records only a permitted replacement with zero backoff", async t => {
  fakeClock(t);
  const dir = mkdtempSync(join(tmpdir(), "marcus-retry-metrics-"));
  t.after(() => { resetPerformanceRecorderForTests(); rmSync(dir, { recursive: true, force: true }); });
  const metrics = join(dir, "metrics.json");
  const recorder = createPerformanceRecorder({ path: metrics, runId: "2026-10-19T173700Z-0123456789ab", commandId: "a".repeat(32) });
  assert.ok(recorder); setPerformanceRecorderForTests(recorder);
  const retries = () => {
    recorder.flush(false, true);
    return JSON.parse(readFileSync(metrics, "utf8")).metrics.requests.retry.http_helper;
  };
  let calls = 0, active = 0, maximum = 0;
  globalThis.fetch = ((_url, init) => {
    calls++; active++; maximum = Math.max(maximum, active);
    if (calls === 2) { active--; return Promise.resolve(new Response(HTML)); }
    return new Promise<Response>((_, reject) => init!.signal!.addEventListener("abort", () => {
      setTimeout(() => { active--; reject(new DOMException("settled", "AbortError")); }, 500);
    }, { once: true }));
  }) as typeof fetch;
  const result = observe(fetchMarcusDetailHtml(URL, DEADLINE));
  await flush(); await advance(t, DEADLINE); await advance(t, 499);
  assert.equal(calls, 1); assert.equal(active, 1);
  assert.equal(retries().retry_attempts, 0);
  await advance(t, 1); await untilSettled(t, result, 100);
  assert.deepEqual(result(), { value: HTML }); assert.equal(calls, 2); assert.equal(maximum, 1);
  assert.deepEqual(retries(),
    { retry_attempts: 1, backoff_ms: 0, terminal_backoff_ms: 0 });
});

for (const stage of ["headers", "body", "body_cancel_stalls"] as const) {
  test(`Marcus ignored-abort ${stage} remains bounded and never overlaps a retry`, async t => {
    fakeClock(t); let calls = 0;
    globalThis.fetch = (async () => {
      calls++;
      if (stage === "headers") return new Promise<Response>(() => {});
      const response = new Response(new ReadableStream({
        cancel() { if (stage === "body_cancel_stalls") return new Promise<void>(() => {}); },
      }));
      response.text = () => new Promise<string>(() => {});
      return response;
    }) as typeof fetch;
    const result = observe(fetchMarcusDetailHtml(URL, DEADLINE));
    await flush(); await advance(t, DEADLINE); await untilSettled(t, result, 1100);
    assert.ok(result() && "error" in result()!); assert.equal(calls, 1);
    await advance(t, 5000); assert.equal(calls, 1);
  });
}

test("Marcus settlement after the one-second wait cannot dispatch a late replacement", async t => {
  fakeClock(t); let resolve!: (response: Response) => void, calls = 0;
  globalThis.fetch = (() => { calls++; return new Promise<Response>(r => { resolve = r; }); }) as typeof fetch;
  const result = observe(fetchMarcusDetailHtml(URL, DEADLINE));
  await flush(); await advance(t, DEADLINE); await untilSettled(t, result, 1100);
  assert.ok(result() && "error" in result()!);
  resolve(new Response(HTML)); await flush(); await advance(t, 5000);
  assert.equal(calls, 1);
});

for (const status of [200, 403, 429]) {
  test(`Marcus late HTTP${status} headers within grace never permit a replacement`, async t => {
    fakeClock(t); let resolve!: (response: Response) => void, calls = 0;
    globalThis.fetch = (() => {
      calls++;
      if (calls > 1) return Promise.resolve(new Response(HTML));
      return new Promise<Response>(r => { resolve = r; });
    }) as typeof fetch;
    const result = observe(fetchMarcusDetailHtml(URL, DEADLINE));
    await flush(); await advance(t, DEADLINE); await advance(t, 100);
    resolve(new Response("late provider response", { status }));
    await flush(); await untilSettled(t, result, 1100);
    assert.ok(result() && "error" in result()!, "late headers must preserve the deadline refusal");
    assert.equal(calls, 1); await advance(t, 5000); assert.equal(calls, 1);
  });
}

for (const stage of ["late_headers", "known_body"] as const) {
  test(`Marcus ${stage} with unsettled body cancellation never permits a replacement`, async t => {
    fakeClock(t);
    let calls = 0, cancellations = 0, resolveHeaders!: (response: Response) => void;
    const response = new Response("provider body");
    response.body!.cancel = () => { cancellations++; return new Promise<void>(() => {}); };
    globalThis.fetch = ((_url, init) => {
      calls++;
      if (calls > 1) return Promise.resolve(new Response(HTML));
      if (stage === "late_headers") return new Promise<Response>(r => { resolveHeaders = r; });
      response.text = () => new Promise<string>((_, reject) => init!.signal!.addEventListener("abort",
        () => reject(new DOMException("body settled", "AbortError")), { once: true }));
      return Promise.resolve(response);
    }) as typeof fetch;
    const result = observe(fetchMarcusDetailHtml(URL, DEADLINE));
    await flush(); await advance(t, DEADLINE);
    if (stage === "late_headers") { await advance(t, 100); resolveHeaders(response); await flush(); }
    await untilSettled(t, result, 1100);
    assert.ok(result() && "error" in result()!, "pending cancellation cannot be transport readiness");
    assert.equal(calls, 1); assert.equal(cancellations, 1, "cancel one body once");
    await advance(t, 5000); assert.equal(calls, 1);
  });
}

test("Marcus known-body cancellation settling within grace permits one later replacement", async t => {
  fakeClock(t);
  let calls = 0, cancellations = 0;
  globalThis.fetch = ((_url, init) => {
    calls++;
    if (calls > 1) return Promise.resolve(new Response(HTML));
    const response = new Response("provider body");
    response.body!.cancel = () => {
      cancellations++;
      return new Promise<void>(resolve => setTimeout(resolve, 500));
    };
    response.text = () => new Promise<string>((_, reject) => init!.signal!.addEventListener("abort",
      () => reject(new DOMException("body settled", "AbortError")), { once: true }));
    return Promise.resolve(response);
  }) as typeof fetch;
  const result = observe(fetchMarcusDetailHtml(URL, DEADLINE));
  await flush(); await advance(t, DEADLINE); await advance(t, 499);
  assert.equal(calls, 1); assert.equal(cancellations, 1);
  await advance(t, 1); await untilSettled(t, result, 100);
  assert.deepEqual(result(), { value: HTML }); assert.equal(calls, 2); assert.equal(cancellations, 1);
});

for (const status of [403, 404, 429, 503]) {
  test(`Marcus detail HTTP${status} is not a deadline retry`, async t => {
    fakeClock(t); let calls = 0;
    globalThis.fetch = (async () => { calls++; return new Response("refused", { status }); }) as typeof fetch;
    await assert.rejects(fetchMarcusDetailHtml(URL, DEADLINE), new RegExp(`HTTP ${status}`));
    assert.equal(calls, 1);
  });
}

test("Marcus arbitrary network error with timeout-like text is not retried", async t => {
  fakeClock(t); let calls = 0; const error = new Error("Marcus & Millichap detail timed out after 37ms");
  globalThis.fetch = (async () => { calls++; throw error; }) as typeof fetch;
  await assert.rejects(fetchMarcusDetailHtml(URL, DEADLINE), e => e === error);
  assert.equal(calls, 1);
});

test("Marcus POST timeout remains one request even when its abort settles", async t => {
  fakeClock(t); let calls = 0;
  globalThis.fetch = ((_url, init) => {
    calls++; return new Promise<Response>((_, reject) => init!.signal!.addEventListener("abort",
      () => reject(new DOMException("aborted", "AbortError")), { once: true }));
  }) as typeof fetch;
  const result = observe(marcusPost("/api/contentsearch/properties", {}, DEADLINE));
  await flush(); await advance(t, DEADLINE); await untilSettled(t, result, 1100);
  assert.ok(result() && "error" in result()!); assert.equal(calls, 1);
});

test("Marcus initial detail success keeps its original request and single attempt", async t => {
  fakeClock(t); let calls = 0;
  globalThis.fetch = (async (url, init) => {
    calls++; assert.equal(String(url), URL); assert.equal(init!.method, undefined);
    assert.deepEqual(init!.headers, { accept: "text/html,*/*", referer: "https://www.marcusmillichap.com/properties", "user-agent": "Mozilla/5.0 CRE collector" });
    return new Response(HTML);
  }) as typeof fetch;
  assert.equal(await fetchMarcusDetailHtml(URL, DEADLINE), HTML); assert.equal(calls, 1);
});

test("Marcus recovered detail preserves rich enrichment and same-generation success cache", async t => {
  fakeClock(t); let calls = 0;
  const previous = process.env.CRE_REFRESH_GENERATION;
  process.env.CRE_REFRESH_GENERATION = "2026-10-19T173700Z-0123456789ab";
  t.after(() => { if (previous === undefined) delete process.env.CRE_REFRESH_GENERATION; else process.env.CRE_REFRESH_GENERATION = previous; });
  globalThis.fetch = ((_url, init) => {
    calls++;
    if (calls === 2) return Promise.resolve(new Response(HTML));
    return new Promise<Response>((_, reject) => init!.signal!.addEventListener("abort",
      () => reject(new DOMException("aborted", "AbortError")), { once: true }));
  }) as typeof fetch;
  const base = { id: "fixture", activityId: "activity", url: URL, inventoryObservedAt: new Date().toISOString() };
  const result = observe(enrichMarcusListing(base));
  await flush(); await advance(t, 30_000); await untilSettled(t, result, 1200);
  const row = result() && "value" in result()! ? (result() as { value: any }).value : null;
  assert.ok(row); assert.equal(row.detailError, undefined); assert.equal(calls, 2);
  assert.equal(row.name, "Recovered property"); assert.equal(row.street, "10 Main Street");
  assert.equal(row.freshnessProvenance.generationId, process.env.CRE_REFRESH_GENERATION);
  assert.equal(row.freshnessProvenance.detailScope, "detail_page");
  assert.equal(row.freshnessProvenance.cacheDisposition, "live"); assert.ok(row.detailObservedAt);
  assert.ok(row.photos.includes("https://mmimageservice.azurewebsites.net/api/image/property/fixture.jpg"));
  assert.ok(row.documents.some((d: any) => d.url === "https://public.example/om.pdf"));
  assert.ok(row.media.some((d: any) => d.url.includes("youtube.com")));
  const dir = mkdtempSync(join(tmpdir(), "marcus-recovery-cache-")); t.after(() => rmSync(dir, { recursive: true, force: true }));
  const path = join(dir, "cache.jsonl"), generation = process.env.CRE_REFRESH_GENERATION!;
  prepareMarcusDetailCache(path, generation); appendMarcusDetailCache(path, generation, base, row);
  assert.deepEqual(readMarcusDetailCache(path, generation, [base]).get("fixture"), row);
  assert.equal(readMarcusDetailCache(path, "different-generation", [base]).size, 0);
});

test("Marcus enrichment fixture is already usable with its original resources", async t => {
  fakeClock(t); let calls = 0;
  globalThis.fetch = (async () => { calls++; return new Response(HTML); }) as typeof fetch;
  const row = await enrichMarcusListing({ id: "fixture", url: URL });
  assert.equal(row.detailError, undefined); assert.equal(calls, 1);
  assert.equal(row.street, "10 Main Street");
  assert.ok(row.photos.includes("https://mmimageservice.azurewebsites.net/api/image/property/fixture.jpg"));
  assert.ok(row.documents.some((d: any) => d.url === "https://public.example/om.pdf"));
  assert.ok(row.media.some((d: any) => d.url.includes("youtube.com")));
});

test("Marcus failed detail remains an error and never enters success cache", async t => {
  fakeClock(t); let calls = 0;
  globalThis.fetch = (async () => { calls++; return new Response("captcha verify you are human"); }) as typeof fetch;
  const base = { id: "failed", activityId: "failed", url: URL };
  const row = await enrichMarcusListing(base);
  assert.ok(row.detailError); assert.equal(calls, 1);
  assert.equal(summarizeListingFreshness([row]).detailErrors, 1);
  const dir = mkdtempSync(join(tmpdir(), "marcus-failed-cache-")); t.after(() => rmSync(dir, { recursive: true, force: true }));
  const path = join(dir, "cache.jsonl"); prepareMarcusDetailCache(path, "attempt");
  appendMarcusDetailCache(path, "attempt", base, row);
  assert.equal(readMarcusDetailCache(path, "attempt", [base]).size, 0);
});

test("Marcus invalid timeout is refused before transport", async t => {
  fakeClock(t); let calls = 0;
  globalThis.fetch = (async () => { calls++; return new Response(HTML); }) as typeof fetch;
  for (const timeout of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
    await assert.rejects(fetchMarcusDetailHtml(URL, timeout), RangeError);
  }
  assert.equal(calls, 0);
});
