import assert from "node:assert/strict";
import test from "node:test";

process.argv = [process.argv[0]!, process.argv[1]!];
const { matthewsResponseText } = await import("../../../sources/matthews.js");

test("Matthews rejects a finite streamed body beyond 32 MiB", async () => {
  const limit = 32 * 1024 * 1024;
  let emitted = 0;
  let cancelled = 0;
  const controller = new AbortController();
  const response = new Response(new ReadableStream<Uint8Array>({
    pull(stream) {
      const remaining = limit + 1 - emitted;
      if (remaining === 0) {
        stream.close();
        return;
      }
      const bytes = new Uint8Array(Math.min(64 * 1024, remaining)).fill(97);
      emitted += bytes.byteLength;
      stream.enqueue(bytes);
    },
    cancel() { cancelled += 1; },
  }, { highWaterMark: 0 }));

  await assert.rejects(
    () => matthewsResponseText(response, controller),
    /body.*(?:limit|exceed|large|bytes)|(?:limit|exceed|large).*body/i,
    "a finite 32 MiB plus one byte response must fail closed"
  );
  assert.equal(emitted, limit + 1);
  assert.equal(controller.signal.aborted, true);
  assert.equal(cancelled, 1);
});
