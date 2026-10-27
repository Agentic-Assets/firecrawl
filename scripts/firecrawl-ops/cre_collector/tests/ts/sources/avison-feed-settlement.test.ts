process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import test from "node:test";
import https from "node:https";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { syncBuiltinESMExports } from "node:module";
const originalRequest = https.request;
let nativeDispatch: (...args: any[]) => any = () => { throw new Error("Unexpected native request"); };
https.request = ((...args: any[]) => nativeDispatch(...args)) as typeof https.request;
syncBuiltinESMExports();
const source = await import("../../../sources/avison-young.js");
test.after(() => { https.request = originalRequest; syncBuiltinESMExports(); });
const keyHtml = `SharpLaunch.PSE.create('${"a".repeat(32)}')`;
const json = (items: unknown[]) => new Response(JSON.stringify({ items }), { headers: { "content-type": "application/json" } });

test("AY failed required feed drains its started sibling, retries uncached, and settled team timeout degrades", { concurrency: false }, async (t) => {
  const originalFetch = globalThis.fetch;
  const requests: string[] = [];
  let phase = "required-failure";
  let teamSettled = false;
  let aborted = false;
  const timers = new Set<ReturnType<typeof setTimeout>>();
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input); requests.push(url);
    if (!url.includes("pse-api.sharplaunch.com")) return new Response(keyHtml);
    const entity = new URL(url).searchParams.get("entity");
    if (phase === "required-failure") {
      if (entity === "website") return new Response("owned HTTP failure", { status: 503 });
      await new Promise<void>((resolve) => { const timer = setTimeout(() => { timers.delete(timer); teamSettled = true; resolve(); }, 60); timers.add(timer); });
      return json([{ id: "owned-team" }]);
    }
    if (entity === "website") return json([{ id: "owned-property" }]);
    return new Response(new ReadableStream<Uint8Array>({
      start(controller) {
        init?.signal?.addEventListener("abort", () => {
          aborted = true; controller.error(new DOMException("Owned cooperative team abort", "AbortError"));
        });
        const timer = setTimeout(() => { timers.delete(timer); controller.enqueue(new TextEncoder().encode('{"items":[]}')); controller.close(); }, 2000);
        timers.add(timer);
      },
    }), { headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  t.after(() => { for (const timer of timers) clearTimeout(timer); globalThis.fetch = originalFetch; });
  const started = performance.now();
  await assert.rejects(() => source.getAvisonYoungFeed(80), /HTTP 503/);
  assert.equal(teamSettled, true, "required error must wait for already-started team request");
  const failedAttemptElapsed = performance.now() - started;
  assert.equal(requests.length, 3);
  phase = "optional-timeout";
  const result = await source.getAvisonYoungFeed(80);
  assert.equal(requests.length, 6, "failed attempt must not be cached");
  assert.equal(aborted, true);
  assert.equal(result.websiteRows[0]?.id, "owned-property");
  assert.equal(result.teamFeedComplete, false);
  assert.equal(result.teamMembers.size, 0);
  assert.match(result.teamFeedReason ?? "", /deadline|timed out|abort/i);
  t.diagnostic(JSON.stringify({ failedAttemptElapsed, teamSettled, aborted, requests, degraded: !result.teamFeedComplete }));
});

test("AY settled bootstrap headers failure retains fallback", { concurrency: false }, async (t) => {
  const originalFetch = globalThis.fetch;
  let aborted = false;
  let release: (() => void) | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => await new Promise<Response>((resolve, reject) => {
    release = () => resolve(new Response(keyHtml));
    timer = setTimeout(release, 2000);
    init?.signal?.addEventListener("abort", () => { aborted = true; reject(new DOMException("Owned header abort", "AbortError")); });
  })) as typeof fetch;
  t.after(() => { if (timer) clearTimeout(timer); release?.(); globalThis.fetch = originalFetch; });
  const result = await source.fetchAvisonYoungApiKey(80);
  assert.equal(aborted, true);
  assert.equal(typeof result, "string");
  assert.notEqual(result, "a".repeat(32));
});

// Last case intentionally sets the source-local sticky transport failure.
// A dedicated test file/process keeps the latch away from other source tests.
test("AY native missing close acknowledgment blocks retry alternate and later feed", { concurrency: false }, async (t) => {
  const originalFetch = globalThis.fetch;
  let requests = 0;
  let fetches = 0;
  let closes = 0;
  let destroyed = 0;
  let release: (() => void) | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  globalThis.fetch = (async () => { fetches++; throw new Error("Forbidden later feed transport"); }) as typeof fetch;
  nativeDispatch = (_url: URL, _options: any, callback: (value: any) => void) => {
    requests++;
    const request = new EventEmitter() as any;
    const response = Object.assign(new PassThrough(), { statusCode: 200, headers: {} });
    response.on("error", () => {});
    release = () => { if (closes) return; closes++; request.emit("close"); response.destroy(); };
    timer = setTimeout(release, 2000);
    request.setTimeout = () => request;
    request.destroy = (error?: Error) => { destroyed++; queueMicrotask(() => { if (error) request.emit("error", error); response.destroy(); }); return request; };
    request.end = () => queueMicrotask(() => callback(response));
    return request;
  };
  t.after(() => { if (timer) clearTimeout(timer); release?.(); globalThis.fetch = originalFetch; });
  const url = "https://www.avisonyoung.us/properties/owned-missing-close";
  const resolver = async () => ["8.8.8.8"];
  const typed = (error: unknown) => error instanceof Error && error.name === "AvisonYoungUnsettledError";
  const started = performance.now();
  await assert.rejects(() => source.fetchAvisonYoungDirectDocWithRetry(url, (value) => source.fetchAvisonYoungDirectDoc(value, 80, resolver), 2, 0), typed);
  assert.equal(requests, 1);
  assert.equal(closes, 0);
  assert.ok(destroyed >= 1);
  await assert.rejects(() => source.fetchAvisonYoungDirectDoc("https://www.avisonyoung.us/properties/alternate", 80, resolver), typed);
  await assert.rejects(() => source.getAvisonYoungFeed(80), typed);
  assert.equal(requests, 1); assert.equal(fetches, 0);
  if (timer) clearTimeout(timer); release?.();
  await new Promise<void>((resolve) => setImmediate(resolve));
  t.diagnostic(JSON.stringify({ elapsed: performance.now() - started, requests, fetches, destroyed, closes }));
});