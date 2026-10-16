import assert from "node:assert/strict";
import test from "node:test";
import { marcusPost, fetchMarcusDetailHtml, marcusHeaders, marcusSearchBody } from "../../../sources/marcus-millichap.js";

const POST_PATH = "/api/contentsearch/properties";
const DETAIL_URL = "https://www.marcusmillichap.com/properties/example";
const DEADLINE = 37;
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

// Exercise the real exports; only provider I/O and the deadline clock are fake.
for (const kind of ["post", "detail"] as const) {
  const invoke = () => kind === "post"
    ? marcusPost(POST_PATH, marcusSearchBody(2), DEADLINE)
    : fetchMarcusDetailHtml(DETAIL_URL, DEADLINE);
  for (const stage of ["headers", "body", "body_cancel_stalls"] as const) {
    test(`Marcus ${kind} deadline aborts stalled ${stage} even if fetch ignores abort`, async (t) => {
      const originalFetch = globalThis.fetch, originalTimer = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
      let expire: (() => void) | undefined, signal: AbortSignal | undefined;
      let calls = 0, canceled = 0, cleared = false, deadlines = 0;
      const token = {} as ReturnType<typeof setTimeout>;
      globalThis.setTimeout = ((callback: () => void, delay: number) => {
        if (delay === DEADLINE) { deadlines++; expire = callback; return token; }
        return originalTimer(callback, delay);
      }) as typeof setTimeout;
      globalThis.clearTimeout = ((value: ReturnType<typeof setTimeout>) => {
        if (value === token) cleared = true; else originalClear(value);
      }) as typeof clearTimeout;
      globalThis.fetch = (async (_url: any, init: RequestInit) => {
        calls++; signal = init.signal as AbortSignal;
        if (stage === "headers") return new Promise<Response>(() => {});
        const response = new Response(new ReadableStream({ cancel() { canceled++; if (stage === "body_cancel_stalls") return new Promise<void>(() => {}); } }));
        // A wrapper may ignore abort; the whole method promise still has a deadline.
        response.json = async () => new Promise(() => {});
        response.text = async () => new Promise(() => {});
        return response;
      }) as typeof fetch;
      t.after(() => { globalThis.fetch = originalFetch; globalThis.setTimeout = originalTimer; globalThis.clearTimeout = originalClear; });
      const outcome = invoke().then((value: unknown) => ({ state: "returned", value }), (error: Error) => ({ state: "rejected", error }));
      await flush(); expire?.();
      let watchdog: ReturnType<typeof setTimeout> | undefined;
      const result = await Promise.race([outcome, new Promise<{state: string}>(resolve => { watchdog = originalTimer(() => resolve({ state: "still_pending" }), 50); })]);
      if (watchdog) originalClear(watchdog);
      assert.equal(result.state, "rejected");
      assert.match((result as any).error.message, /Marcus.*timed out after 37ms/);
      assert.equal(signal?.aborted, true);
      assert.equal(calls, 1);
      assert.equal(cleared, true);
      assert.equal(deadlines, 1, "one deadline spans headers and body");
      if (stage !== "headers") assert.equal(canceled, 1);
    });
  }
  test(`Marcus ${kind} aborts a native-style locked response body`, async (t) => {
    const originalFetch = globalThis.fetch, originalTimer = globalThis.setTimeout;
    let expire: (() => void) | undefined, observedAbort = 0;
    globalThis.setTimeout = ((callback: () => void, delay: number) => {
      if (delay === DEADLINE) { expire = callback; return originalTimer(() => {}, 1); }
      return originalTimer(callback, delay);
    }) as typeof setTimeout;
    globalThis.fetch = (async (_url: any, init: RequestInit) => new Response(new ReadableStream({
      start(controller) { init.signal!.addEventListener("abort", () => { observedAbort++; controller.error(new Error("native body aborted")); }, { once: true }); },
    }))) as typeof fetch;
    t.after(() => { globalThis.fetch = originalFetch; globalThis.setTimeout = originalTimer; });
    const pending = invoke();
    await flush(); assert.ok(expire); expire();
    await assert.rejects(pending, /timed out/);
    assert.equal(observedAbort, 1);
  });
  test(`Marcus ${kind} cancels a late response after the header deadline`, async (t) => {
    const originalFetch = globalThis.fetch, originalTimer = globalThis.setTimeout;
    let resolveHeaders!: (response: Response) => void, expire: (() => void) | undefined, canceled = 0;
    globalThis.setTimeout = ((callback: () => void, delay: number) => {
      if (delay === DEADLINE) { expire = callback; return originalTimer(() => {}, 1); }
      return originalTimer(callback, delay);
    }) as typeof setTimeout;
    globalThis.fetch = (() => new Promise<Response>(resolve => { resolveHeaders = resolve; })) as typeof fetch;
    t.after(() => { globalThis.fetch = originalFetch; globalThis.setTimeout = originalTimer; });
    const pending = invoke();
    await flush();
    // Old code has no deadline; the separate RED stall cases already prove that.
    assert.ok(expire, "source-local deadline must be installed");
    expire();
    await assert.rejects(pending, /timed out/);
    resolveHeaders(new Response(new ReadableStream({ cancel() { canceled++; } })));
    await flush();
    assert.equal(canceled, 1);
  });
  test(`Marcus ${kind} preserves successful request shape and response, clears deadline`, async (t) => {
    const originalFetch = globalThis.fetch;
    let signal: AbortSignal | undefined, observedUrl: string | undefined, observedInit: RequestInit | undefined;
    const payload = kind === "post" ? JSON.stringify({ TotalCount: 2, Properties: [{ DealId: "native" }] }) : "<html>π property detail</html>";
    globalThis.fetch = (async (url: any, init: RequestInit) => { observedUrl = String(url); observedInit = init; signal = init.signal as AbortSignal; return new Response(payload); }) as typeof fetch;
    t.after(() => { globalThis.fetch = originalFetch; });
    const result = await invoke();
    assert.deepEqual(result, kind === "post" ? JSON.parse(payload) : payload);
    assert.equal(observedUrl, kind === "post" ? "https://www.marcusmillichap.com" + POST_PATH : DETAIL_URL);
    if (kind === "post") {
      assert.equal(observedInit?.method, "POST");
      assert.equal(observedInit?.body, JSON.stringify(marcusSearchBody(2)));
      assert.deepEqual(observedInit?.headers, marcusHeaders());
    } else assert.deepEqual(observedInit?.headers, { accept: "text/html,*/*", referer: "https://www.marcusmillichap.com/properties", "user-agent": "Mozilla/5.0 CRE collector" });
    assert.ok(signal);
    await new Promise(resolve => setTimeout(resolve, DEADLINE + 5));
    assert.equal(signal.aborted, false, "success must clear its abort deadline");
  });
  test(`Marcus ${kind} HTTP refusal cancels body without retrying`, async (t) => {
    const originalFetch = globalThis.fetch; let canceled = 0, calls = 0;
    globalThis.fetch = (async () => { calls++; return new Response(new ReadableStream({ cancel() { canceled++; } }), { status: 503 }); }) as typeof fetch;
    t.after(() => { globalThis.fetch = originalFetch; });
    await assert.rejects(invoke(), /Marcus & Millichap .*HTTP 503/);
    assert.equal(calls, 1); assert.equal(canceled, 1);
  });
}

test("Marcus JSON parse failure remains an error with one request", async (t) => {
  const originalFetch = globalThis.fetch; let calls = 0;
  globalThis.fetch = (async () => { calls++; return new Response("not-json"); }) as typeof fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  await assert.rejects(() => marcusPost(POST_PATH, {}), SyntaxError);
  assert.equal(calls, 1);
});
