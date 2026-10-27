process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import test from "node:test";
import https from "node:https";
import { syncBuiltinESMExports } from "node:module";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { performance } from "node:perf_hooks";

const nativeRequest = https.request;
let dispatch: (...args: any[]) => any = () => { throw new Error("unconfigured native HTTPS fixture"); };
// Installed before source import: its real default pinned request remains active.
https.request = ((...args: any[]) => dispatch(...args)) as typeof https.request;
syncBuiltinESMExports();
const source = await import("../../../sources/avison-young.js");
const detailUrl = "https://www.avisonyoung.us/properties/test-property";
const html = "<html><body><main><h1>Owned synthetic property</h1><p>Office building</p></main></body></html>";
const publicResolver = async () => ["8.8.8.8"];
const requestMs = 80;
const fixtureReleaseMs = 2000;
const elapsedBoundMs = 1500; // Includes a separate bounded cleanup allowance.

test.after(() => { https.request = nativeRequest; syncBuiltinESMExports(); });

type NativeMode = "ok" | "trickle" | "error" | "aborted" | "close" | "redirect";
function nativeFixture(mode: NativeMode) {
  const timers = new Set<ReturnType<typeof setTimeout>>();
  const requests: string[] = [];
  const pinned: string[] = [];
  let destroyed = 0;
  let released = false;
  let responseEvent = false;
  const later = (fn: () => void, ms: number) => {
    const timer = setTimeout(() => { timers.delete(timer); fn(); }, ms);
    timers.add(timer); return timer;
  };
  const clear = (timer: ReturnType<typeof setTimeout> | undefined) => {
    if (timer) { clearTimeout(timer); timers.delete(timer); }
  };
  dispatch = (url: URL, options: any, callback: (response: any) => void) => {
    requests.push(url.toString());
    options.lookup(url.hostname, {}, (_error: unknown, address: string) => { pinned.push(address); });
    const request = new EventEmitter() as EventEmitter & {
      setTimeout: (ms: number, cb: () => void) => unknown;
      end: () => void; destroy: (error?: Error) => unknown;
    };
    let idle: ReturnType<typeof setTimeout> | undefined;
    let timeoutMs = 0;
    let timeoutCallback = () => {};
    let ended = false;
    const response = Object.assign(new PassThrough(), { statusCode: 200, headers: {} as Record<string,string> });
    // Fixture owns the error event so a missing production handler becomes an
    // eventual semantic success/failure, rather than an uncaught process crash.
    response.on("error", () => {});
    const activity = () => { clear(idle); if (timeoutMs && !ended) idle = later(timeoutCallback, timeoutMs); };
    request.setTimeout = (ms, cb) => { timeoutMs = ms; timeoutCallback = cb; activity(); return request; };
    request.destroy = (error) => {
      if (ended) return request;
      ended = true; destroyed++; clear(idle);
      queueMicrotask(() => { if (error) request.emit("error", error); response.destroy(); request.emit("close"); });
      return request;
    };
    request.end = () => {
      later(() => {
        if (ended) return;
        if (mode === "redirect" && requests.length === 1) {
          response.statusCode = 302; response.headers.location = "/properties/redirected-property";
          callback(response); later(() => { ended = true; clear(idle); response.end(); }, 50);
          return;
        }
        callback(response);
        if (mode === "ok" || mode === "redirect") {
          later(() => { ended = true; clear(idle); response.end(html); }, mode === "ok" ? 5 : 50);
          return;
        }
        const tick = () => {
          if (ended) return;
          activity(); response.write(" "); later(tick, 20);
        };
        tick();
        if (mode !== "trickle") later(() => { responseEvent = true; response.emit(mode, ...(mode === "error" ? [new Error("owned response failure")] : [])); }, 20);
        later(() => { if (!ended) { released = true; ended = true; clear(idle); response.end(html); } }, fixtureReleaseMs);
      }, 0);
    };
    return request;
  };
  return {
    requests, pinned,
    facts: () => ({ destroyed, released, responseEvent }),
    close: () => { for (const timer of timers) clearTimeout(timer); timers.clear(); },
  };
}

for (const mode of ["trickle", "error", "aborted", "close"] as const) {
  test(`AY real pinned request handles ${mode} with bounded failure`, { concurrency: false }, async (t) => {
    const fixture = nativeFixture(mode);
    t.after(fixture.close);
    const started = performance.now();
    const outcome = await source.fetchAvisonYoungDirectDoc(detailUrl, requestMs, publicResolver)
      .then((value) => ({ value, error: null }), (error: unknown) => ({ value: null, error }));
    const elapsed = performance.now() - started;
    t.diagnostic(JSON.stringify({ mode, elapsed, ...fixture.facts(), requests: fixture.requests, pinned: fixture.pinned, rejected: outcome.error !== null }));
    assert.equal(fixture.facts().released, false);
    assert.ok(elapsed < elapsedBoundMs);
    if (mode !== "trickle") assert.equal(fixture.facts().responseEvent, true);
    assert.deepEqual(fixture.requests, [detailUrl]);
    assert.deepEqual(fixture.pinned, ["8.8.8.8"]);
    assert.equal(outcome.value, null);
    assert.ok(outcome.error instanceof Error);
  });
}

test("AY public DNS time consumes the single detail deadline", { concurrency: false }, async (t) => {
  const fixture = nativeFixture("ok"); t.after(fixture.close);
  let released = false;
  let release!: () => void;
  const pending = new Promise<string[]>((resolve) => { release = () => resolve(["8.8.8.8"]); });
  const timer = setTimeout(() => { released = true; release(); }, fixtureReleaseMs);
  t.after(() => { clearTimeout(timer); release(); });
  const started = performance.now();
  const outcome = await source.fetchAvisonYoungDirectDoc(detailUrl, requestMs, () => pending)
    .then((value) => ({ value, error: null }), (error: unknown) => ({ value: null, error }));
  const elapsed = performance.now() - started;
  t.diagnostic(JSON.stringify({ elapsed, released, requests: fixture.requests, rejected: outcome.error !== null }));
  assert.equal(released, false); assert.ok(elapsed < elapsedBoundMs);
  assert.deepEqual(fixture.requests, []);
  clearTimeout(timer); release();
  await new Promise<void>((resolve) => setImmediate(resolve));
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.deepEqual(fixture.requests, [], "late DNS resolution must not dispatch HTTPS after timeout");
  assert.equal(outcome.value, null); assert.ok(outcome.error instanceof Error);
});

test("AY approved redirects share the original elapsed budget", { concurrency: false }, async (t) => {
  const fixture = nativeFixture("redirect"); t.after(fixture.close);
  const started = performance.now();
  const outcome = await source.fetchAvisonYoungDirectDoc(detailUrl, requestMs, publicResolver)
    .then((value) => ({ value, error: null }), (error: unknown) => ({ value: null, error }));
  t.diagnostic(JSON.stringify({ elapsed: performance.now() - started, ...fixture.facts(), requests: fixture.requests, rejected: outcome.error !== null }));
  assert.equal(fixture.requests.length, 2);
  assert.equal(outcome.value, null); assert.ok(outcome.error instanceof Error, "50ms+50ms responses must exhaust one 80ms budget");
});

test("AY native pinned request success and public address controls", { concurrency: false }, async (t) => {
  const fixture = nativeFixture("ok"); t.after(fixture.close);
  const result = await source.fetchAvisonYoungDirectDoc(detailUrl, 500, publicResolver);
  assert.match(result.rawHtml, /Owned synthetic property/);
  assert.deepEqual(fixture.pinned, ["8.8.8.8"]);
  await assert.rejects(() => source.fetchAvisonYoungDirectDoc(detailUrl, 500, async () => ["127.0.0.1"]), /non-public/);
  await assert.rejects(() => source.fetchAvisonYoungDirectDoc("https://unapproved.test/property", 500, publicResolver), /not approved/);
  assert.equal(fixture.requests.length, 1);
});

for (const kind of ["bootstrap", "entity"] as const) {
  test(`AY ${kind} stalled fetch body has a finite operation deadline`, { concurrency: false }, async (t) => {
    const originalFetch = globalThis.fetch;
    let released = false;
    let aborted = false;
    let cancellations = 0;
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const payload = kind === "bootstrap" ? `SharpLaunch.PSE.create('${"a".repeat(32)}')` : JSON.stringify({ items: [{ id: "owned-fixture" }] });
    const stream = new ReadableStream<Uint8Array>({ start(value) { controller = value; }, cancel() { cancellations++; } });
    const timer = setTimeout(() => { released = true; controller.enqueue(new TextEncoder().encode(payload)); controller.close(); }, fixtureReleaseMs);
    const requests: string[] = [];
    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push(String(input)); init?.signal?.addEventListener("abort", () => {
        aborted = true;
        controller.error(new DOMException("Owned abort-cooperative response body", "AbortError"));
      });
      return new Response(stream, { headers: { "content-type": kind === "bootstrap" ? "text/html" : "application/json" } });
    }) as typeof fetch;
    t.after(() => { clearTimeout(timer); globalThis.fetch = originalFetch; });
    const started = performance.now();
    // Optional timeout exercises the production acquisition budget.
    const invoke = kind === "bootstrap"
      ? () => source.fetchAvisonYoungApiKey(requestMs)
      : () => source.fetchAvisonYoungEntity("website", "owned-synthetic-key", requestMs);
    const outcome = await invoke().then((value) => ({ value, error: null }), (error: unknown) => ({ value: null, error }));
    const elapsed = performance.now() - started;
    t.diagnostic(JSON.stringify({ kind, elapsed, released, aborted, cancellations, requests, rejected: outcome.error !== null }));
    assert.equal(released, false); assert.ok(elapsed < elapsedBoundMs); assert.equal(requests.length, 1);
    assert.equal(aborted, true);
    if (kind === "entity") { assert.equal(outcome.value, null); assert.ok(outcome.error instanceof Error); }
    else { assert.equal(outcome.error, null); assert.equal(typeof outcome.value, "string"); assert.notEqual(outcome.value, "a".repeat(32)); }
  });
}