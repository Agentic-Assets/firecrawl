import assert from "node:assert/strict";
import test from "node:test";
import { Semaphore, SemaphoreTimeoutError, withHardTimeout } from "./browser_resources";
import {
  BrowserResourceLeakError,
  PermitLease,
  awaitAllocation,
} from "./permit_lease";

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** A batch-shaped lease: an admission slot plus one page permit. */
async function batchLease() {
  const slot = new Semaphore(1);
  const pages = new Semaphore(1);
  const lease = await PermitLease.acquire(
    [
      { source: slot, countsBrowser: false },
      { source: pages, countsBrowser: true },
    ],
    () => 10,
  );
  return { slot, pages, lease };
}

test("acquire returns the permits already taken when a later gate times out", async () => {
  const slot = new Semaphore(1);
  const pages = new Semaphore(1);
  await pages.acquire();
  await assert.rejects(
    PermitLease.acquire(
      [
        { source: slot, countsBrowser: false },
        { source: pages, countsBrowser: true },
      ],
      () => 1,
    ),
    SemaphoreTimeoutError,
  );
  assert.equal(slot.getAvailablePermits(), 1);
  assert.equal(pages.getAvailablePermits(), 0);
});

test("release returns every permit exactly once", async () => {
  const { slot, pages, lease } = await batchLease();
  lease.release();
  lease.release();
  lease.quarantine("ignored after release");
  assert.equal(slot.getAvailablePermits(), 1);
  assert.equal(pages.getAvailablePermits(), 1);
});

test("quarantine keeps browser permits, returns admission permits, and is final", async () => {
  const { slot, pages, lease } = await batchLease();
  lease.quarantine("test leak");
  lease.release();
  assert.equal(lease.heldByRequest, false);
  assert.equal(slot.getAvailablePermits(), 1);
  assert.equal(pages.getAvailablePermits(), 0);
  assert.throws(() => lease.add(() => {}, true), /no longer held/);
});

test("tryAcquire never waits and takes nothing when no permit is free", () => {
  const pages = new Semaphore(1);
  const first = PermitLease.tryAcquire(pages);
  assert.ok(first);
  assert.equal(PermitLease.tryAcquire(pages), null);
  first.release();
  assert.equal(pages.getAvailablePermits(), 1);
});

test("an in-time allocation failure leaves the lease with the request; a leak quarantines it", async () => {
  const plain = await batchLease();
  await assert.rejects(
    awaitAllocation(
      plain.lease,
      async () => {
        throw new Error("newContext failed");
      },
      (allocation) => allocation,
      async () => {},
    ),
    /newContext failed/,
  );
  assert.equal(plain.lease.heldByRequest, true);

  const leaked = await batchLease();
  await assert.rejects(
    awaitAllocation(
      leaked.lease,
      async () => {
        throw new BrowserResourceLeakError(new Error("close failed"));
      },
      (allocation) => allocation,
      async () => {},
    ),
    BrowserResourceLeakError,
  );
  assert.equal(leaked.slot.getAvailablePermits(), 1);
  assert.equal(leaked.pages.getAvailablePermits(), 0);
});

test("a late allocation owns the lease: closed then released, or released on a late failure", async () => {
  const resolved = await batchLease();
  const lateValue = deferred<string>();
  const closed: string[] = [];
  await assert.rejects(
    awaitAllocation(
      resolved.lease,
      () => lateValue.promise,
      (allocation) => withHardTimeout(allocation, 1, "deadline"),
      async (value) => {
        closed.push(value);
      },
    ),
    /deadline/,
  );
  assert.equal(resolved.lease.heldByRequest, false);
  assert.equal(resolved.pages.getAvailablePermits(), 0);
  lateValue.resolve("late-context");
  await tick();
  assert.deepEqual(closed, ["late-context"]);
  assert.equal(resolved.pages.getAvailablePermits(), 1);
  assert.equal(resolved.slot.getAvailablePermits(), 1);

  const rejected = await batchLease();
  const lateFailure = deferred<string>();
  await assert.rejects(
    awaitAllocation(
      rejected.lease,
      () => lateFailure.promise,
      (allocation) => withHardTimeout(allocation, 1, "deadline"),
      async () => {},
    ),
    /deadline/,
  );
  lateFailure.reject(new Error("browser has been closed"));
  await tick();
  assert.equal(rejected.pages.getAvailablePermits(), 1);
  assert.equal(rejected.slot.getAvailablePermits(), 1);
});

test("a late allocation that leaks or cannot be closed keeps its browser permit", async () => {
  const leaked = await batchLease();
  const lateLeak = deferred<string>();
  await assert.rejects(
    awaitAllocation(
      leaked.lease,
      () => lateLeak.promise,
      (allocation) => withHardTimeout(allocation, 1, "deadline"),
      async () => {},
    ),
    /deadline/,
  );
  lateLeak.reject(new BrowserResourceLeakError(new Error("close failed")));
  await tick();
  assert.equal(leaked.pages.getAvailablePermits(), 0);
  assert.equal(leaked.slot.getAvailablePermits(), 1);

  const unclosable = await batchLease();
  const lateValue = deferred<string>();
  await assert.rejects(
    awaitAllocation(
      unclosable.lease,
      () => lateValue.promise,
      (allocation) => withHardTimeout(allocation, 1, "deadline"),
      () => new Promise(() => {}),
      1,
    ),
    /deadline/,
  );
  lateValue.resolve("late-context");
  await tick(10);
  assert.equal(unclosable.pages.getAvailablePermits(), 0);
  assert.equal(unclosable.slot.getAvailablePermits(), 1);
});

test("settleByRequest closes then releases while held, and does nothing after a hand-off", async () => {
  const held = await batchLease();
  const heldCalls: string[] = [];
  assert.equal(
    await held.lease.settleByRequest(
      async () => void heldCalls.push("page"),
      async () => void heldCalls.push("context"),
    ),
    true,
  );
  assert.deepEqual(heldCalls, ["page", "context"]);
  assert.equal(held.slot.getAvailablePermits(), 1);
  assert.equal(held.pages.getAvailablePermits(), 1);

  // A late allocation owns the lease after hand-off: a caller that settles
  // anyway must not close anything or return a permit early.
  const handedOff = await batchLease();
  handedOff.lease.handOff();
  const lateCalls: string[] = [];
  assert.equal(
    await handedOff.lease.settleByRequest(
      async () => void lateCalls.push("page"),
      async () => void lateCalls.push("context"),
    ),
    false,
  );
  assert.deepEqual(lateCalls, []);
  assert.equal(handedOff.slot.getAvailablePermits(), 0);
  assert.equal(handedOff.pages.getAvailablePermits(), 0);

  // An unconfirmed close quarantines through the same path.
  const unclosable = await batchLease();
  assert.equal(
    await unclosable.lease.settleByRequest(null, () => new Promise(() => {}), 1),
    false,
  );
  assert.equal(unclosable.slot.getAvailablePermits(), 1);
  assert.equal(unclosable.pages.getAvailablePermits(), 0);
});

test("a permit whose release throws does not strand the others", async (t) => {
  const errors = t.mock.method(console, "error", () => {});
  const returned: string[] = [];
  const source = (name: string, throws = false) => ({
    source: {
      acquire: async () => {},
      release: () => {
        returned.push(name);
        if (throws) throw new Error(`${name} release failed`);
      },
    },
    countsBrowser: false,
  });

  const released = await PermitLease.acquire(
    [source("a"), source("b", true), source("c")],
    () => 1,
  );
  released.release();
  assert.deepEqual(returned, ["c", "b", "a"]);

  returned.length = 0;
  const quarantined = await PermitLease.acquire(
    [source("a"), source("b", true), source("c")],
    () => 1,
  );
  quarantined.quarantine("test leak");
  assert.deepEqual(returned, ["c", "b", "a"]);
  assert.ok(errors.mock.callCount() >= 2);
});

// AGENTIC-3767 public-API settlement regressions. Deferreds replace only browser teardown.
const flushCleanup = () => new Promise<void>((resolve) => setImmediate(resolve));

test("AGENTIC3767 concurrent settlement invokes each owned close once", async () => {
  const { lease, pages } = await batchLease();
  const pending = deferred<void>();
  let closes = 0;
  const close = () => { closes++; return pending.promise; };
  const first = lease.settleByRequest(null, close, 100);
  const second = lease.settleByRequest(null, close, 100);
  pending.resolve();
  await Promise.all([first, second]);
  assert.equal(closes, 1, "concurrent callers must share one context cleanup owner");
  assert.equal(pages.getAvailablePermits(), 1);
});

test("AGENTIC3767 public release cannot free browser capacity during pending cleanup", async () => {
  const { lease, pages } = await batchLease();
  const pending = deferred<void>();
  const settled = lease.settleByRequest(null, () => pending.promise, 100);
  lease.release();
  const whilePending = pages.getAvailablePermits();
  pending.resolve();
  await settled;
  assert.equal(whilePending, 0, "live context must still own its browser permit");
  assert.equal(pages.getAvailablePermits(), 1);
});

test("AGENTIC3767 timeout returns admission then exact late context success releases once", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { lease, pages, slot } = await batchLease();
  const pending = deferred<void>();
  const settled = lease.settleByRequest(null, () => pending.promise, 10);
  await flushCleanup();
  t.mock.timers.tick(10);
  assert.equal(await settled, false);
  assert.equal(slot.getAvailablePermits(), 1);
  assert.equal(pages.getAvailablePermits(), 0);
  pending.resolve();
  await flushCleanup();
  assert.equal(pages.getAvailablePermits(), 1, "the exact late close confirmation must return browser capacity");
  lease.release();
  pending.resolve();
  await flushCleanup();
  assert.equal(slot.getAvailablePermits(), 1);
  assert.equal(pages.getAvailablePermits(), 1, "capacity returns exactly once");
});

test("AGENTIC3767 eventual context rejection keeps browser held without unhandled rejection", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { lease, pages, slot } = await batchLease();
  const pending = deferred<void>();
  const settled = lease.settleByRequest(null, () => pending.promise, 10);
  await flushCleanup();
  t.mock.timers.tick(10);
  assert.equal(await settled, false);
  pending.reject(new Error("synthetic late close failure"));
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(slot.getAvailablePermits(), 1);
  assert.equal(pages.getAvailablePermits(), 0);
});

test("AGENTIC3767 successful context owns hanging page and settles without waiting for page", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { lease, pages } = await batchLease();
  const pendingPage = deferred<void>();
  let result: boolean | undefined;
  const settled = lease.settleByRequest(() => pendingPage.promise, async () => {}, 10)
    .then((value) => { result = value; return value; });
  await flushCleanup();
  const beforePageTimeout = { result, permits: pages.getAvailablePermits() };
  pendingPage.resolve();
  await settled;
  assert.deepEqual(beforePageTimeout, { result: true, permits: 1 },
    "confirmed context teardown owns its page without exhausting page timeout");
});

test("AGENTIC3767 page-only late success returns capacity exactly once", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { lease, pages, slot } = await batchLease();
  const pending = deferred<void>();
  const settled = lease.settleByRequest(() => pending.promise, null, 10);
  await flushCleanup();
  t.mock.timers.tick(10);
  assert.equal(await settled, false);
  assert.equal(slot.getAvailablePermits(), 1);
  assert.equal(pages.getAvailablePermits(), 0);
  pending.resolve();
  await flushCleanup();
  assert.equal(pages.getAvailablePermits(), 1, "late page-only cleanup confirms browser resource closure");
  lease.release();
  assert.equal(pages.getAvailablePermits(), 1);
});

test("AGENTIC3767 explicit quarantine during cleanup remains final despite eventual success", async () => {
  const { lease, pages, slot } = await batchLease();
  const pending = deferred<void>();
  const settled = lease.settleByRequest(null, () => pending.promise, 100);
  lease.quarantine("synthetic explicit leak veto");
  pending.resolve();
  await settled;
  lease.release();
  assert.equal(slot.getAvailablePermits(), 1);
  assert.equal(pages.getAvailablePermits(), 0);
});

test("AGENTIC3767 control confirmed close returns both capacity classes exactly once", async () => {
  const { lease, pages, slot } = await batchLease();
  assert.equal(await lease.settleByRequest(async () => {}, async () => {}, 10), true);
  lease.release();
  assert.equal(slot.getAvailablePermits(), 1);
  assert.equal(pages.getAvailablePermits(), 1);
});

test("AGENTIC3767 control late allocation stays owned until its context closes", async () => {
  const { lease, pages, slot } = await batchLease();
  const allocation = deferred<string>();
  const closed = deferred<void>();
  await assert.rejects(awaitAllocation(lease, () => allocation.promise,
    async () => { throw new Error("synthetic request deadline"); }, () => closed.promise),
    /synthetic request deadline/);
  allocation.resolve("late context");
  await flushCleanup();
  assert.equal(pages.getAvailablePermits(), 0);
  closed.resolve();
  await flushCleanup();
  assert.equal(pages.getAvailablePermits(), 1);
  assert.equal(slot.getAvailablePermits(), 1);
});
