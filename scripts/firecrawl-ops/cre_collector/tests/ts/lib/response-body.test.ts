import assert from "node:assert/strict";
import test from "node:test";
import { readBoundedUtf8, ResponseBodyDeadlineError, ResponseBodyLimitError } from "../../../lib/response-body.js";

async function read(chunks: Uint8Array[], maxBytes: number) {
  let pulls = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      const value = chunks[pulls++];
      if (value) controller.enqueue(value); else controller.close();
    },
  }, { highWaterMark: 0 });
  const reader = new Response(body).body!.getReader();
  const controller = new AbortController();
  try {
    const value = await readBoundedUtf8(reader, { maxBytes, signal: controller.signal, deadlineAt: performance.now() + 1000 });
    assert.equal(body.locked, true, "core leaves lock ownership with caller");
    assert.equal(controller.signal.aborted, false, "core never aborts controller");
    return value;
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

test("shared body preserves native UTF-8 BOM and malformed trailing replacement", async () => {
  for (const bytes of [new Uint8Array([239,187,191,65,226,130,172,240,159,152,128]), new Uint8Array([65,255,226,130]), new Uint8Array([239,187])]) {
    const expected = await new Response(bytes).text();
    assert.equal(await read([...bytes].map(value => new Uint8Array([value])), bytes.length), expected);
  }
});

test("shared body accepts exact byte cap and rejects one extra byte without prefix success", async () => {
  assert.equal(await read([new TextEncoder().encode("exact")], 5), "exact");
  await assert.rejects(read([new Uint8Array(5), new Uint8Array(1)], 5), ResponseBodyLimitError);
});

test("shared body never acquires/cancels/releases the caller-owned reader", async () => {
  let cancels = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) { controller.enqueue(new Uint8Array(2)); },
    cancel() { cancels++; },
  }, { highWaterMark: 0 });
  const reader = new Response(body).body!.getReader();
  await assert.rejects(readBoundedUtf8(reader, { maxBytes: 1, signal: new AbortController().signal, deadlineAt: performance.now() + 1000 }), ResponseBodyLimitError);
  assert.equal(cancels, 0); assert.equal(body.locked, true);
  await reader.cancel(); reader.releaseLock(); assert.equal(cancels, 1);
});

test("shared body observes pre-abort and expired absolute deadline before any read", async () => {
  for (const preAborted of [true, false]) {
    let pulls = 0;
    const body = new ReadableStream<Uint8Array>({ pull() { pulls++; } }, { highWaterMark: 0 });
    const reader = new Response(body).body!.getReader();
    const controller = new AbortController();
    const reason = new Error("owned abort");
    if (preAborted) controller.abort(reason);
    try {
      await assert.rejects(readBoundedUtf8(reader, { maxBytes: 1, signal: controller.signal, deadlineAt: performance.now() - 1 }), preAborted ? error => error === reason : ResponseBodyDeadlineError);
      assert.equal(pulls, 0);
    } finally { await reader.cancel(); reader.releaseLock(); }
  }
});

test("shared body stops after a late read without another read or byte access", async () => {
  let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
  let byteInspections = 0;
  let pulls = 0;
  const body = new ReadableStream<Uint8Array>({
    start(controller) { streamController = controller; },
    pull() { pulls++; },
  }, { highWaterMark: 0 });
  const reader = new Response(body).body!.getReader();
  await assert.rejects(readBoundedUtf8(reader, { maxBytes: 1, signal: new AbortController().signal, deadlineAt: performance.now() + 10 }), ResponseBodyDeadlineError);
  const unexpected = { get byteLength() { byteInspections++; throw new Error("late bytes processed"); } } as Uint8Array;
  streamController!.enqueue(unexpected);
  await new Promise<void>(resolve => setImmediate(resolve));
  assert.equal(pulls, 1);
  assert.equal(byteInspections, 0);
  await reader.cancel(); reader.releaseLock();
});

test("shared body uses monotonic deadline despite a microtask-only infinite stream", async () => {
  let pulls = 0;
  const body = new ReadableStream<Uint8Array>({ pull(controller) { pulls++; controller.enqueue(new Uint8Array(0)); } }, { highWaterMark: 0 });
  const reader = new Response(body).body!.getReader();
  try {
    await assert.rejects(readBoundedUtf8(reader, { maxBytes: 1, signal: new AbortController().signal, deadlineAt: performance.now() + 10 }), ResponseBodyDeadlineError);
    assert.ok(pulls > 0);
  } finally { await reader.cancel(); reader.releaseLock(); }
});

test("shared body removes abort listener and clears timer on success and failure", async t => {
  const originalTimeout = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
  const active = new Set<ReturnType<typeof setTimeout>>();
  globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) => {
    const timer = originalTimeout(() => { active.delete(timer); callback(...args); }, delay);
    active.add(timer); return timer;
  }) as typeof setTimeout;
  globalThis.clearTimeout = ((timer: ReturnType<typeof setTimeout>) => { active.delete(timer); originalClear(timer); }) as typeof clearTimeout;
  t.after(() => { globalThis.setTimeout = originalTimeout; globalThis.clearTimeout = originalClear; });
  for (const fails of [false, true]) {
    const controller = new AbortController();
    const signal = controller.signal;
    const add = signal.addEventListener.bind(signal), remove = signal.removeEventListener.bind(signal);
    let listeners = 0;
    signal.addEventListener = ((...args: Parameters<typeof add>) => { listeners++; return add(...args); }) as typeof signal.addEventListener;
    signal.removeEventListener = ((...args: Parameters<typeof remove>) => { listeners--; return remove(...args); }) as typeof signal.removeEventListener;
    const body = new Response("ok").body!;
    const reader = new Response(body).body!.getReader();
    try {
      const result = readBoundedUtf8(reader, { maxBytes: fails ? 1 : 2, signal, deadlineAt: performance.now() + 1000 });
      if (fails) await assert.rejects(result, ResponseBodyLimitError); else assert.equal(await result, "ok");
      assert.equal(listeners, 0); assert.equal(active.size, 0);
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
  }
});
