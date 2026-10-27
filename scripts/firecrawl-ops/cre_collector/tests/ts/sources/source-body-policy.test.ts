import assert from "node:assert/strict";
import test from "node:test";

process.argv = [process.argv[0]!, process.argv[1]!];
const { foundryFetchText } = await import("../../../sources/foundry-commercial.js");
const { lyonStahlFetchText } = await import("../../../sources/lyon-stahl.js");
const sources = [
  { name: "Foundry", fetchText: foundryFetchText, url: "https://www.foundrycommercial.com/property/policy-fixture/", next: "/property/next-fixture/" },
  { name: "Lyon Stahl", fetchText: lyonStahlFetchText, url: "https://lyonstahl.com/properties/policy-fixture/", next: "/properties/next-fixture/" },
] as const;
const sleep = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));

for (const source of sources) {
  test(`${source.name} recovers a native abort body error distinct from signal.reason`, { timeout: 3000 }, async () => {
    let requests = 0;
    const observed: { error?: DOMException; signal?: AbortSignal | null } = {};
    const removers: (() => void)[] = [];
    try {
      const text = await source.fetchText(source.url, "html", async (_url, init) => {
        requests++;
        if (requests > 1) return new Response("<html>recovered fixture</html>");
        observed.signal = init?.signal;
        return new Response(new ReadableStream<Uint8Array>({
          start(controller) {
            const onAbort = () => {
              observed.error = new DOMException("independent native body abort", "AbortError");
              controller.error(observed.error);
            };
            observed.signal?.addEventListener("abort", onAbort, { once: true });
            removers.push(() => observed.signal?.removeEventListener("abort", onAbort));
          },
        }, { highWaterMark: 0 }));
      }, 40, 1024, 2);
      assert.equal(text, "<html>recovered fixture</html>");
      assert.equal(requests, 2);
      assert.equal(observed.signal?.aborted, true);
      assert.ok(observed.error);
      assert.notEqual(observed.error, observed.signal?.reason);
    } finally {
      for (const remove of removers) remove();
    }
  });

  for (const route of ["headers", "redirect"] as const) {
    test(`${source.name} ${route} delay consumes its existing attempt body budget`, { timeout: 3000 }, async () => {
      let streamController!: ReadableStreamDefaultController<Uint8Array>;
      let resolvePull: (() => void) | undefined;
      let cancelled = false, closedByFixture = false, requests = 0;
      const body = new ReadableStream<Uint8Array>({
        start(controller) { streamController = controller; },
        pull() { return new Promise<void>(resolve => { resolvePull = resolve; }); },
        cancel() { cancelled = true; resolvePull?.(); },
      }, { highWaterMark: 0 });
      const fallback = setTimeout(() => {
        closedByFixture = true;
        if (!cancelled) streamController.close();
        resolvePull?.();
      }, 500);
      const started = performance.now();
      let error: unknown, rejected = false, elapsed = 0, fixtureClosedAtOutcome = false;
      try {
        await source.fetchText(source.url, "html", async () => {
          requests++;
          if (route === "redirect" && requests === 1) {
            await sleep(35);
            return new Response(new ReadableStream<Uint8Array>({
              cancel: async () => { await sleep(35); },
            }, { highWaterMark: 0 }), { status: 302, headers: { location: source.next } });
          }
          await sleep(route === "headers" ? 60 : 20);
          return new Response(body, { headers: { "Content-Type": "text/html" } });
        }, 100, 1024, 1);
      } catch (caught) {
        error = caught; rejected = true;
      } finally {
        elapsed = performance.now() - started;
        fixtureClosedAtOutcome = closedByFixture;
        clearTimeout(fallback);
        if (!closedByFixture && !cancelled) streamController.close();
        resolvePull?.();
      }
      assert.equal(rejected, true);
      assert.match(String(error), /timeout|timed out|deadline/i);
      assert.equal(fixtureClosedAtOutcome, false);
      assert.ok(elapsed < 150, `attempt budget must include ${route} delay; observed ${elapsed}ms`);
      assert.equal(requests, route === "headers" ? 1 : 2);
      assert.equal(body.locked, false);
    });
  }
}

// Native ReadableStreams automatically settle cancellation. Controlled readers
// below model noncooperative transport cleanup while retaining real Responses.
function failedReaderResponse(cancelMode: "ack" | "reject" | "pending", closedMode: "resolved" | "pending") {
  const originalError = new TypeError("synthetic body transport failure", { cause: { code: "UND_ERR_BODY_TIMEOUT" } });
  let resolveClosed!: () => void, resolveCancellation!: () => void;
  let cancelled = 0, acknowledged = false;
  let ackTimer: ReturnType<typeof setTimeout> | undefined;
  const closed = closedMode === "resolved" ? Promise.resolve() : new Promise<void>(resolve => { resolveClosed = resolve; });
  const cancellation = new Promise<void>(resolve => { resolveCancellation = resolve; });
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({ start(value) { controller = value; } }, { highWaterMark: 0 });
  const response = new Response(body);
  Object.defineProperty(body, "getReader", { value: () => ({
    read: () => Promise.reject(originalError),
    closed,
    cancel() {
      cancelled++;
      if (cancelMode === "reject") return Promise.reject(new Error("different cancellation failure"));
      if (cancelMode === "ack") {
        ackTimer = setTimeout(() => { acknowledged = true; resolveCancellation(); }, 35);
      }
      return cancellation;
    },
    releaseLock() {},
  }) });
  // Always settle every synthetic promise even if an old implementation waits.
  const fallback = setTimeout(() => { resolveClosed?.(); resolveCancellation(); }, 500);
  return {
    response, cancelled: () => cancelled, acknowledged: () => acknowledged,
    cleanup() {
      clearTimeout(fallback);
      if (ackTimer) clearTimeout(ackTimer);
      resolveClosed?.(); resolveCancellation(); controller.close();
    },
  };
}

for (const source of sources) {
  test(`${source.name} waits for positive body cancellation acknowledgement before retry`, { timeout: 3000 }, async () => {
    const fixture = failedReaderResponse("ack", "pending");
    let requests = 0;
    try {
      const text = await source.fetchText(source.url, "html", async () => {
        requests++;
        if (requests === 1) return fixture.response;
        assert.equal(fixture.acknowledged(), true, "retry must wait for positive cancellation acknowledgement");
        return new Response("<html>acknowledged successor</html>");
      }, 1000, 1024, 2);
      assert.equal(text, "<html>acknowledged successor</html>");
      assert.equal(requests, 2);
      assert.equal(fixture.cancelled(), 1);
    } finally { fixture.cleanup(); }
  });

  for (const [closed, cancellation] of [
    ["resolved", "reject"], ["resolved", "pending"], ["pending", "pending"],
  ] as const) {
    test(`${source.name} ${closed} reader.closed plus ${cancellation} cancellation never proves settled read failure`, { timeout: 3000 }, async () => {
      const fixture = failedReaderResponse(cancellation, closed);
      let requests = 0, rejected = false; const started = performance.now();
      try {
        try {
          await source.fetchText(source.url, "html", async () => {
            requests++;
            return requests === 1 ? fixture.response : new Response("<html>forbidden successor</html>");
          }, 40, 1024, 2);
        } catch { rejected = true; }
        assert.ok(performance.now() - started < 250, "cleanup proof must remain bounded despite an unresolved reader.closed");
        assert.equal(requests, 1, "unacknowledged cleanup cannot dispatch a successor");
        assert.equal(rejected, true);
        assert.equal(fixture.cancelled(), 1);
      } finally { fixture.cleanup(); }
    });
  }
}
