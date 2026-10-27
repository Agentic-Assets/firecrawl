process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import test from "node:test";
const source = await import("../../../sources/nai-global.js");
// Dedicated process: this deliberately leaves the source-local unsafe promise
// cached after the first transaction. No reset API or cross-test module reuse.
test("NAI unsafe discovery remains cached across sale then lease", async (t) => {
  const originalFetch = globalThis.fetch;
  const requests: string[] = [];
  let cancellations = 0;
  const widget = source.NAI_WIDGET_URL;
  const script = new URL("/_next/static/chunks/unsafe.js", widget).toString();
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = String(input); requests.push(url);
    if (url === widget) return new Response(`<script src="${script}"></script>`);
    if (url === script) return new Response(new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new Uint8Array(source.NAI_SOURCE_DISCOVERY_MAX_BODY_BYTES + 1)); },
      cancel() { cancellations++; return Promise.reject(new Error("owned unsafe cancellation")); },
    }));
    throw new Error(`No GraphQL or other transport allowed: ${url}`);
  }) as typeof fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  let firstError: unknown;
  await assert.rejects(() => source.srcNaiGlobal("sale", 1, true), (error: unknown) => {
    firstError = error; return error instanceof Error && error.name === "NaiDiscoveryUnsettledError";
  });
  assert.deepEqual(requests, [widget, script]);
  await assert.rejects(() => source.srcNaiGlobal("lease", 1, true), (error: unknown) => error === firstError);
  t.diagnostic(JSON.stringify({ requests, cancellations, sameCachedFailure: true }));
  assert.deepEqual(requests, [widget, script]);
  assert.equal(cancellations, 1);
});