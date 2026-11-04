process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import test from "node:test";
import { performance } from "node:perf_hooks";
const source = await import("../../../sources/avison-young.js");

test("AY continuously ready body yields to its elapsed deadline before producer escape", async (t) => {
  const originalFetch = globalThis.fetch;
  let requests = 0;
  let cancellations = 0;
  let producerReleased = false;
  let chunks = 0;
  const producerReleaseAt = performance.now() + 300;
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (performance.now() >= producerReleaseAt) {
        producerReleased = true;
        controller.close();
        return;
      }
      chunks++;
      controller.enqueue(new Uint8Array(0));
    },
    cancel() { cancellations++; },
  });
  globalThis.fetch = (async () => {
    requests++;
    return new Response(stream, { headers: { "content-type": "application/json" } });
  }) as typeof fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  const started = performance.now();
  const outcome = await source.fetchAvisonYoungEntity("website", "owned-synthetic-key", 40)
    .then((value) => ({ value, error: null }), (error: unknown) => ({ value: null, error }));
  const elapsed = performance.now() - started;
  t.diagnostic(JSON.stringify({ elapsed, requests, cancellations, producerReleased, chunks, rejected: outcome.error !== null }));
  assert.equal(producerReleased, false, "elapsed deadline must stop consumption before producer escape");
  assert.ok(elapsed < 200, `40ms operation plus prompt acknowledged cleanup took ${elapsed}ms`);
  assert.equal(requests, 1);
  assert.equal(cancellations, 1);
  assert.equal(outcome.value, null);
  assert.ok(outcome.error instanceof Error);
});