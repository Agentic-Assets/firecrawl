import assert from "node:assert/strict";
import test from "node:test";

process.argv = [process.argv[0]!, process.argv[1]!];
const { foundryFetchText } = await import("../../../sources/foundry-commercial.js");
const { lyonStahlFetchText } = await import("../../../sources/lyon-stahl.js");

for (const [name, fetchText, url] of [
  ["Foundry", foundryFetchText, "https://www.foundrycommercial.com/property/stalled-fixture/"],
  ["Lyon Stahl", lyonStahlFetchText, "https://lyonstahl.com/properties/stalled-fixture/"],
] as const) {
  test(`${name} body deadline rejects before an abort-ignoring stream eventually closes`, { timeout: 3000 }, async () => {
    let streamController!: ReadableStreamDefaultController<Uint8Array>;
    let resolvePull: (() => void) | undefined;
    let fixtureClosed = false, cancelled = 0, requests = 0;
    const observed: { signal?: AbortSignal | null } = {};
    const body = new ReadableStream<Uint8Array>({
      start(controller) { streamController = controller; },
      pull() { return new Promise<void>(resolve => { resolvePull = resolve; }); },
      cancel() { cancelled++; resolvePull?.(); },
    }, { highWaterMark: 0 });
    const response = new Response(body, { headers: { "Content-Type": "text/html" } });
    // This finite fixture owns settlement even when production ignores its abort.
    // It prevents a cancelled test or outer process timeout becoming the oracle.
    const fixtureTimer = setTimeout(() => {
      fixtureClosed = true;
      if (!cancelled) streamController.close();
      resolvePull?.();
    }, 500);
    const started = performance.now();
    let error: unknown;
    let rejected = false, closedAtSettlement = false, elapsed = 0;
    try {
      await fetchText(url, "html", async (_input, init) => {
        requests++;
        observed.signal = init?.signal;
        return response; // Intentionally ignores AbortSignal.
      }, 80, 1024, 1);
    } catch (caught) {
      rejected = true;
      error = caught;
    } finally {
      elapsed = performance.now() - started;
      closedAtSettlement = fixtureClosed;
      clearTimeout(fixtureTimer);
      if (!fixtureClosed && !cancelled) streamController.close();
      resolvePull?.();
    }
    console.log(JSON.stringify({ source: name, elapsedMs: elapsed, fixtureClosedBeforeSettlement: closedAtSettlement, requests, cancelled, aborted: observed.signal?.aborted, rejected }));
    assert.equal(closedAtSettlement, false, "the body deadline must settle before the fixture's 500ms close");
    assert.equal(rejected, true);
    assert.match(String(error), /timeout|timed out|deadline/i);
    assert.equal(observed.signal?.aborted, true);
    assert.equal(requests, 1, "the one-attempt fixture must not start a second request");
    assert.equal(body.locked, false);
  });
}
