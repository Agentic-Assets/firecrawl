import assert from "node:assert/strict";
import test from "node:test";
import { matthewsResponseText } from "../../../sources/matthews-transport.js";

test("Matthews ignores a noncooperative read result arriving after its body timeout", { timeout: 1000 }, async () => {
  let resolveRead!: (result: ReadableStreamReadResult<Uint8Array>) => void;
  const pendingRead = new Promise<ReadableStreamReadResult<Uint8Array>>(resolve => { resolveRead = resolve; });
  let reads = 0, cancellations = 0, releases = 0, byteInspections = 0;
  // A native stream settles read() when cancelled. This controlled reader models
  // a transport that ignores both cancellation and lock release until later.
  const body = {
    getReader() {
      return {
        read() {
          reads++;
          assert.equal(reads, 1, "a timed-out body must not request another chunk");
          return pendingRead;
        },
        cancel() { cancellations++; return new Promise<void>(() => {}); },
        releaseLock() { releases++; },
      };
    },
  } as unknown as ReadableStream<Uint8Array>;
  const controller = new AbortController();

  await assert.rejects(
    matthewsResponseText({ body }, controller, 10),
    /response body timed out after 10ms/
  );
  assert.equal(controller.signal.aborted, true);
  assert.equal(cancellations, 1);
  assert.equal(releases, 1);

  const lateBytes = new Uint8Array([97]);
  Object.defineProperty(lateBytes, "byteLength", {
    get() { byteInspections++; throw new Error("late body bytes must never be inspected"); },
  });
  resolveRead({ done: false, value: lateBytes });
  await new Promise<void>(resolve => setImmediate(resolve));
  await new Promise<void>(resolve => setImmediate(resolve));
  assert.equal(byteInspections, 0);
  assert.equal(reads, 1);
  assert.equal(cancellations, 1);
  assert.equal(releases, 1);
});
