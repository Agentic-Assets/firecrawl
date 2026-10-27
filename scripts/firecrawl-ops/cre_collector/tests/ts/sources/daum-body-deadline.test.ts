import test from "node:test";
import assert from "node:assert/strict";
import { DaumPoliteTransport } from "../../../sources/daum-transport.js";

const PROPERTY_URL = "https://daumcommercial.com/property/synthetic/";
const SEARCH_URL = "https://daumcommercial.com/property-search/";

test("DAUM bounds immediately ready zero-byte reads before the finite producer escape", async () => {
  const cutoff = performance.now() + 300;
  let escaped = false;
  let cancellations = 0;
  let calls = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (performance.now() >= cutoff) {
        escaped = true;
        controller.close();
      } else {
        controller.enqueue(new Uint8Array(0));
      }
    },
    cancel() { cancellations++; },
  });
  const transport = new DaumPoliteTransport(async () => {
    calls++;
    return calls === 1 ? new Response(body) : new Response("recovered");
  }, 0, 40);

  assert.equal(await transport.get(PROPERTY_URL), "recovered");
  assert.equal(escaped, false, "the body deadline must precede the independent producer escape");
  assert.equal(cancellations, 1);
  assert.equal(calls, 2);
  assert.equal(body.locked, false);
});

for (const mode of ["acknowledged", "rejected", "delayed"] as const) {
  test(`DAUM pending-body ${mode} cleanup controls retry and queued GET/POST`, async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    let releaseCancellation!: () => void;
    const cancellation = new Promise<void>(resolve => { releaseCancellation = resolve; });
    let closed = false;
    let escaped = false;
    let aborted = false;
    let cancellations = 0;
    const body = new ReadableStream<Uint8Array>({
      start(value) { controller = value; },
      cancel() {
        closed = true;
        cancellations++;
        if (mode === "rejected") return Promise.reject(new Error("synthetic cancellation refusal"));
        if (mode === "delayed") return cancellation;
      },
    });
    // Independent finite escape also exceeds the unchanged one-second cleanup allowance.
    const escape = setTimeout(() => {
      escaped = true;
      if (!closed) { closed = true; controller.close(); }
      releaseCancellation();
    }, 1_800);
    const dispatches: Array<{ url: string; method: string | undefined }> = [];
    const transport = new DaumPoliteTransport(async (input, init) => {
      dispatches.push({ url: String(input), method: init?.method });
      init?.signal?.addEventListener("abort", () => { aborted = true; }, { once: true });
      return dispatches.length === 1 ? new Response(body) : new Response("recovered");
    }, 0, 40);
    try {
      const outcomes = await Promise.allSettled([
        transport.get(PROPERTY_URL), transport.get(PROPERTY_URL), transport.post(SEARCH_URL),
      ]);
      assert.equal(escaped, false, "the request must settle before the independent body escape");
      assert.equal(cancellations, 1);
      assert.equal(aborted, true);
      assert.equal(body.locked, false);
      if (mode === "acknowledged") {
        assert.deepEqual(outcomes, Array.from({ length: 3 }, () => ({ status: "fulfilled", value: "recovered" })));
        assert.deepEqual(dispatches.map(row => row.method), ["GET", "GET", "GET", "POST"]);
      } else {
        assert.ok(outcomes.every(row => row.status === "rejected"));
        assert.equal(dispatches.length, 1, "uncertain cleanup must poison retry and queued requests");
        await assert.rejects(transport.get(PROPERTY_URL), /uncertain response-body cleanup/);
        assert.equal(dispatches.length, 1, "future calls on the same instance must remain refused");
      }
    } finally {
      clearTimeout(escape);
      if (!closed) { closed = true; controller.close(); }
      releaseCancellation();
    }
  });
}

test("DAUM body budget includes elapsed header time without resetting at the body", async () => {
  let calls = 0;
  let escaped = false;
  let closed = false;
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  let cancellations = 0;
  const body = new ReadableStream<Uint8Array>({
    start(value) { controller = value; },
    cancel() { closed = true; cancellations++; },
  });
  const escape = setTimeout(() => {
    escaped = true;
    if (!closed) { closed = true; controller.close(); }
  }, 600);
  const transport = new DaumPoliteTransport(async () => {
    calls++;
    if (calls > 1) return new Response("recovered");
    await new Promise(resolve => setTimeout(resolve, 120));
    return new Response(body);
  }, 0, 200);
  const startedAt = performance.now();
  try {
    assert.equal(await transport.get(PROPERTY_URL), "recovered");
    assert.ok(performance.now() - startedAt < 300, "headers must consume the same 200ms attempt budget");
    assert.equal(escaped, false);
    assert.equal(cancellations, 1);
    assert.equal(calls, 2);
  } finally {
    clearTimeout(escape);
    if (!closed) { closed = true; controller.close(); }
  }
});

test("DAUM deadline-induced abort closure cannot replace cancellation acknowledgement", async () => {
  const cutoff = performance.now() + 300;
  let calls = 0;
  let escaped = false;
  let body!: ReadableStream<Uint8Array>;
  const transport = new DaumPoliteTransport(async (_input, init) => {
    calls++;
    body = new ReadableStream<Uint8Array>({
      start(controller) {
        init?.signal?.addEventListener("abort", () => controller.error(init?.signal?.reason), { once: true });
      },
      pull(controller) {
        if (performance.now() >= cutoff) {
          escaped = true;
          controller.close();
        } else {
          controller.enqueue(new Uint8Array(0));
        }
      },
    });
    return new Response(body);
  }, 0, 40);

  await assert.rejects(transport.get(PROPERTY_URL), /did not settle/);
  await assert.rejects(transport.post(SEARCH_URL), /uncertain response-body cleanup/);
  assert.equal(calls, 1, "an induced abort error was not observed as the original read failure");
  assert.equal(escaped, false);
  assert.equal(body.locked, false);
});

test("DAUM reader preserves split UTF-8, BOM handling and the exact decoded-byte cap", async () => {
  const chunks = [new Uint8Array([0xef, 0xbb]), new Uint8Array([0xbf, 0xe2]), new Uint8Array([0x82, 0xac, 0xff])];
  const transport = new DaumPoliteTransport(async () => new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  })), 0, 1_000, 7);
  assert.equal(await transport.get(PROPERTY_URL), "€�");
});
