import assert from "node:assert/strict";
import test from "node:test";
import {
  MATTHEWS_FETCH_TIMEOUT_MS,
  MATTHEWS_MAX_RESPONSE_BYTES,
  MatthewsResponseLimitError,
  matthewsFetch,
  matthewsResponseText,
  matthewsRetryableFetchFailure,
} from "../../../sources/matthews-transport.js";

function streamed(chunks: Uint8Array[], headers?: HeadersInit) {
  let reads = 0, cancellations = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      const chunk = chunks[reads++];
      if (chunk) controller.enqueue(chunk);
      else controller.close();
    },
    cancel() { cancellations++; },
  }, { highWaterMark: 0 });
  return { response: new Response(body, { headers }), body, reads: () => reads, cancellations: () => cancellations };
}

for (const header of [undefined, "1", "999999999", "invalid"]) {
  test(`Matthews actual body bytes override Content-Length ${header}`, async () => {
    const response = streamed([new TextEncoder().encode("unchanged")], header ? { "Content-Length": header } : undefined);
    assert.equal(await matthewsResponseText(response.response, new AbortController(), 1000, 9), "unchanged");
    assert.equal(response.cancellations(), 0);
    assert.equal(response.body.locked, false);
  });
}

test("Matthews exactly 32 MiB succeeds without truncation", async () => {
  const bytes = new Uint8Array(MATTHEWS_MAX_RESPONSE_BYTES).fill(97);
  bytes[bytes.length - 1] = 122;
  const response = streamed([bytes]);
  const text = await matthewsResponseText(response.response, new AbortController());
  assert.equal(text.length, bytes.length);
  assert.equal(text[0], "a"); assert.equal(text.at(-1), "z");
  assert.equal(response.cancellations(), 0);
  assert.equal(response.body.locked, false);
});

for (const chunks of [[new Uint8Array(9)], [new Uint8Array(8), new Uint8Array(1)]]) {
  test(`Matthews rejects crossing chunk in ${chunks.length}-chunk body`, async () => {
    const response = streamed(chunks, { "Content-Length": "1" });
    const controller = new AbortController();
    await assert.rejects(matthewsResponseText(response.response, controller, 1000, 8), MatthewsResponseLimitError);
    assert.equal(controller.signal.aborted, true);
    assert.equal(response.cancellations(), 1);
    assert.equal(response.reads(), chunks.length);
    assert.equal(response.body.locked, false);
  });
}

test("Matthews preserves native UTF-8 text decoding across BOM, multibyte and malformed boundaries", async () => {
  for (const bytes of [
    new Uint8Array([239,187,191,65,226,130,172,240,159,152,128]),
    new Uint8Array([65,255,226,130]),
    new Uint8Array([239,187]),
  ]) {
    const expected = await new Response(bytes).text();
    const chunks = [...bytes].map(byte => new Uint8Array([byte]));
    assert.equal(await matthewsResponseText(streamed(chunks).response, new AbortController(), 1000, bytes.length), expected);
  }
});

test("Matthews null and empty bodies retain empty text", async () => {
  for (const response of [new Response(null), new Response("")]) {
    assert.equal(await matthewsResponseText(response, new AbortController()), "");
  }
});

for (const cap of [0, -1, 0.5, NaN, Infinity, MATTHEWS_MAX_RESPONSE_BYTES + 1]) {
  test(`Matthews invalid byte limit ${cap} refuses before reader acquisition`, async () => {
    const fixture = streamed([new Uint8Array(1)]);
    await assert.rejects(matthewsResponseText(fixture.response, new AbortController(), 1000, cap), RangeError);
    assert.equal(fixture.reads(), 0); assert.equal(fixture.body.locked, false);
  });
}

test("Matthews many tiny chunks retain exact content", async () => {
  let count = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (count === 65537) controller.close();
      else { controller.enqueue(new Uint8Array([count++ % 26 + 65])); }
    },
  }, { highWaterMark: 0 });
  const result = await matthewsResponseText(new Response(body), new AbortController(), 10000, 65537);
  assert.equal(result.length, 65537);
  for (let i = 0; i < result.length; i++) assert.equal(result.charCodeAt(i), i % 26 + 65);
  assert.equal(body.locked, false);
});

for (const mode of ["reject", "throw", "never"] as const) {
  test(`Matthews oversize survives ${mode} cancellation`, async () => {
    let calls = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) { controller.enqueue(new Uint8Array(2)); },
      cancel() {
        calls++;
        if (mode === "throw") throw new Error("synthetic cancel throw");
        if (mode === "reject") return Promise.reject(new Error("synthetic cancel rejection"));
        return new Promise<void>(() => {});
      },
    }, { highWaterMark: 0 });
    const controller = new AbortController();
    await assert.rejects(matthewsResponseText(new Response(body), controller, 1000, 1), MatthewsResponseLimitError);
    await new Promise<void>(resolve => setImmediate(resolve));
    assert.equal(calls, 1); assert.equal(controller.signal.aborted, true); assert.equal(body.locked, false);
  });
}

test("Matthews noncooperative read and cancellation cannot hold the body deadline", async () => {
  let cancels = 0;
  const body = new ReadableStream<Uint8Array>({
    pull: () => new Promise<void>(() => {}),
    cancel() { cancels++; return new Promise<void>(() => {}); },
  }, { highWaterMark: 0 });
  const controller = new AbortController();
  await assert.rejects(matthewsResponseText(new Response(body), controller, 10), /response body timed out/);
  assert.equal(controller.signal.aborted, true); assert.equal(cancels, 1); assert.equal(body.locked, false);
});

test("Matthews body deadline is absolute despite continual immediately resolved chunks", async () => {
  let reads = 0, cancels = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) { reads++; controller.enqueue(new Uint8Array(0)); },
    cancel() { cancels++; },
  }, { highWaterMark: 0 });
  await assert.rejects(matthewsResponseText(new Response(body), new AbortController(), 10), /response body timed out/);
  assert.ok(reads > 0); assert.equal(cancels, 1); assert.equal(body.locked, false);
});

test("Matthews overflow is terminal and preserves typed cause and redacted body diagnostic", async t => {
  const originalFetch = globalThis.fetch, originalWarn = console.warn, originalNow = Date.now;
  const originalTimeout = globalThis.setTimeout;
  let calls = 0;
  const logs: string[] = [], waits: number[] = [];
  Date.now = () => originalNow() + 100_000_000;
  globalThis.fetch = async () => {
    calls++;
    return streamed([new Uint8Array(MATTHEWS_MAX_RESPONSE_BYTES + 1)]).response;
  };
  console.warn = (...args) => { logs.push(args.join(" ")); };
  globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) => {
    waits.push(delay ?? 0);
    return originalTimeout(callback, delay, ...args);
  }) as typeof setTimeout;
  t.after(() => { globalThis.fetch = originalFetch; console.warn = originalWarn; Date.now = originalNow; globalThis.setTimeout = originalTimeout; });
  await assert.rejects(matthewsFetch("https://www.matthews.com/properties/PRIVATE?secret=HIDDEN"), MatthewsResponseLimitError);
  assert.equal(calls, 1); assert.equal(logs.length, 1);
  assert.ok(!/PRIVATE|HIDDEN|secret|https:|Error|byte limit/.test(logs[0]));
  const event = JSON.parse(logs[0].slice("  matthews/http ".length));
  assert.equal(event.phase, "body"); assert.equal(event.status, 200);
  assert.equal(event.cooldownMs, 0); assert.equal(event.nextAction, "terminal_failure");
  assert.ok(waits.every(delay => delay === MATTHEWS_FETCH_TIMEOUT_MS));
  assert.equal(matthewsRetryableFetchFailure(200, new MatthewsResponseLimitError(8)), false);
  assert.equal(matthewsRetryableFetchFailure(200, new Error("ordinary stream failure")), true);
});
