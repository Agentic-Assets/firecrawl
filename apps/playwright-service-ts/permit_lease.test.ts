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
