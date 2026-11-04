import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { MATTHEWS_FETCH_TIMEOUT_MS, matthewsFetch } from "../../../sources/matthews.js";

const prefix = "  matthews/http ";
let nextClock = Date.now() + 1_000_000;
async function run(t: TestContext, reply: (call: number, options: RequestInit) => Promise<Response>, url = "https://www.matthews.com/sitemap.xml?private=NEVER_LOG_QUERY", brokenLog = false, overlap = false) {
  const originalFetch = globalThis.fetch, originalTimeout = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
  const activePhaseTimers = new Set<ReturnType<typeof setTimeout>>();
  const originalNow = Date.now, originalRandom = Math.random, originalWarn = console.warn;
  let now = nextClock;
  nextClock += 10_000_000;
  const waits: number[] = [], logs: string[] = [], signals: AbortSignal[] = [];
  let calls = 0;
  Date.now = () => now;
  Math.random = () => 0.25;
  console.warn = (...args) => {
    if (brokenLog) throw new Error("synthetic logging failure");
    logs.push(args.join(" "));
  };
  globalThis.setTimeout = ((callback: (...args: any[]) => void, delay?: number, ...args: any[]) => {
    waits.push(delay ?? 0);
    if ((typeof delay === "number" && delay > MATTHEWS_FETCH_TIMEOUT_MS - 100 && delay <= MATTHEWS_FETCH_TIMEOUT_MS)) {
      const timer = originalTimeout(() => { activePhaseTimers.delete(timer); callback(...args); }, 5);
      activePhaseTimers.add(timer);
      return timer;
    }
    queueMicrotask(() => { now += delay ?? 0; callback(...args); });
    return 0 as unknown as ReturnType<typeof setTimeout>;
  }) as typeof setTimeout;
  globalThis.clearTimeout = ((timer: ReturnType<typeof setTimeout>) => {
    activePhaseTimers.delete(timer);
    originalClear(timer);
  }) as typeof clearTimeout;
  globalThis.fetch = async (_url, options) => {
    calls++;
    signals.push(options!.signal as AbortSignal);
    return reply(calls, options!);
  };
  t.after(() => {
    globalThis.fetch = originalFetch; globalThis.setTimeout = originalTimeout; globalThis.clearTimeout = originalClear;
    Date.now = originalNow; Math.random = originalRandom; console.warn = originalWarn;
  });
  let result: unknown, error: unknown;
  try { result = await (overlap ? Promise.all([matthewsFetch(url), matthewsFetch(url)]) : matthewsFetch(url)); } catch (caught) { error = caught; }
  assert.equal(activePhaseTimers.size, 0, "all phase timers settle or clear");
  const events = logs.map(line => {
    assert.ok(line.length < 512);
    assert.ok(line.startsWith(prefix));
    assert.ok(!/NEVER_LOG|https?:|password|authorization|<html|Error:/i.test(line));
    const event = JSON.parse(line.slice(prefix.length));
    assert.deepEqual(Object.keys(event).sort(), ["attempt", "cooldownMs", "elapsedMs", "maxAttempts", "nextAction", "phase", "role", "status"].sort());
    assert.ok(Number.isFinite(event.elapsedMs) && event.elapsedMs >= 0);
    return event;
  });
  return { result, error, calls, waits, events, signals };
}

for (const phase of ["headers", "body"] as const) {
  test(`Matthews logs bounded ${phase} timeouts and keeps all six final cooldowns`, async t => {
    const r = await run(t, async () => phase === "headers" ? new Promise<Response>(() => {}) : new Response(new ReadableStream<Uint8Array>({ pull: () => new Promise<void>(() => {}) })));
    assert.equal(r.calls, 6);
    assert.match(String(r.error), new RegExp(`response ${phase === "headers" ? "headers" : "body"} timed out`));
    assert.equal(r.events.length, 6);
    assert.deepEqual(r.events.map(e => e.attempt), [1,2,3,4,5,6]);
    assert.ok(r.events.every(e => e.maxAttempts === 6 && e.phase === phase && e.role === "sitemap" && e.status === (phase === "headers" ? null : 200)));
    assert.deepEqual(r.events.map(e => e.cooldownMs), [21250,36250,51250,66250,81250,96250]);
    assert.deepEqual(r.waits.filter(ms => !(ms > MATTHEWS_FETCH_TIMEOUT_MS - 100 && ms <= MATTHEWS_FETCH_TIMEOUT_MS)), [21250,36250,51250,66250,81250,96250]);
    assert.equal(r.waits.filter(ms => (ms > MATTHEWS_FETCH_TIMEOUT_MS - 100 && ms <= MATTHEWS_FETCH_TIMEOUT_MS)).length, phase === "headers" ? 6 : 12);
    assert.ok(r.signals.every(signal => signal.aborted));
    assert.ok(r.events.slice(0,5).every(e => e.nextAction === "retry_after_cooldown"));
    assert.equal(r.events[5].nextAction, "final_cooldown_before_failure");
  });
}
for (const status of [429,403,503]) {
  test(`Matthews logs HTTP${status} without changing retry status or cooldown`, async t => {
    const r = await run(t, async () => new Response("NEVER_LOG_BODY", { status, headers: { authorization: "NEVER_LOG_HEADER", "Retry-After": "900" } }));
    assert.equal(r.calls, 6);
    assert.match(String(r.error), /throttled after retries/);
    assert.equal(r.events.length, 6);
    assert.ok(r.events.every(e => e.phase === "http" && e.status === status));
    assert.deepEqual(r.waits.filter(ms => !(ms > MATTHEWS_FETCH_TIMEOUT_MS - 100 && ms <= MATTHEWS_FETCH_TIMEOUT_MS)), [21250,36250,51250,66250,81250,96250]);
  });
}
test("Matthews terminal HTTP404 logs no retry or cooldown", async t => {
  const r = await run(t, async () => new Response("NEVER_LOG_BODY", { status:404 }));
  assert.equal(r.calls, 1); assert.match(String(r.error), /Matthews HTTP 404/);
  assert.equal(r.events.length, 1);
  assert.equal(r.events[0].phase, "http"); assert.equal(r.events[0].status,404);
  assert.equal(r.events[0].cooldownMs,0); assert.equal(r.events[0].nextAction,"terminal_failure");
  assert.equal(r.waits.length,1);
});
test("Matthews successful transport stays quiet and returns unchanged body", async t => {
  const r = await run(t, async () => new Response("original HTML"));
  assert.equal(r.error, undefined); assert.equal(r.calls,1); assert.equal(r.events.length,0);
  assert.deepEqual(r.result,{html:"original HTML",status:200,location:null});
});
for (const [url,role] of [["https://www.matthews.com/properties/example?private=NEVER_LOG_QUERY","detail"],["https://www.matthews.com/listings","inventory"],["https://unowned.test/NEVER_LOG_PATH","other"]]) {
  test(`Matthews diagnostics expose only safe ${role} role`, async t => {
    const r=await run(t, async () => new Response("NEVER_LOG_BODY",{status:404}),url);
    assert.equal(r.events.length,1);assert.equal(r.events[0].role,role);
  });
}
test("Matthews exception text never enters diagnostics and success after retry stays quiet", async t => {
  const r=await run(t, async call => {if(call===1)throw new Error("NEVER_LOG_ERROR password=secret https://private.test");return new Response("recovered");});
  assert.equal(r.calls,2);assert.equal(r.events.length,1);assert.equal(r.events[0].phase,"headers");
  assert.equal(r.events[0].status,null);assert.equal(r.events[0].nextAction,"retry_after_cooldown");
  assert.equal(r.error,undefined);
});
test("Matthews diagnostic sink failure cannot change request outcome or retries", async t => {
  const r=await run(t,async call=>call===1?new Response("blocked",{status:429}):new Response("recovered"),undefined,true);
  assert.equal(r.calls,2);assert.equal(r.error,undefined);assert.deepEqual(r.result,{html:"recovered",status:200,location:null});
});

test("Matthews overlapping successful calls retain the capped process-local pacing wait", async t => {
  const r = await run(t, async () => new Response("unchanged"), undefined, false, true);
  assert.equal(r.error, undefined); assert.equal(r.calls, 2); assert.equal(r.events.length, 0);
  assert.deepEqual(r.waits.filter(ms => !(ms > MATTHEWS_FETCH_TIMEOUT_MS - 100 && ms <= MATTHEWS_FETCH_TIMEOUT_MS)), [7000]);
  assert.equal(r.waits.filter(ms => (ms > MATTHEWS_FETCH_TIMEOUT_MS - 100 && ms <= MATTHEWS_FETCH_TIMEOUT_MS)).length, 4);
});
