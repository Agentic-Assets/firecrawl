import assert from "node:assert/strict";
import test from "node:test";
import { Semaphore } from "./browser_resources";
import { runScrapeLifecycle } from "./scrape_lifecycle";
import { ScrapeStartPacer, scrapeStartIntervalMs } from "./scrape_start_pacer";
import { fixture, isPhase, tick } from "./helpers/lifecycle_fixture";

test("global pacing spaces concurrent context allocations after admission", async () => {
  const semaphore = new Semaphore(4);
  const pacer = new ScrapeStartPacer(40);
  const starts: number[] = [];
  await Promise.all(
    Array.from({ length: 4 }, () => {
      const { options, context } = fixture(semaphore);
      options.deadlineAt = Date.now() + 1000;
      return runScrapeLifecycle({
        ...options,
        paceStart: (remaining) => {
          assert.ok(semaphore.getAvailablePermits() < 4);
          return pacer.waitForStart(remaining);
        },
        createContext: async () => {
          starts.push(performance.now());
          return context;
        },
      });
    }),
  );
  assert.equal(starts.length, 4);
  for (let index = 1; index < starts.length; index++) {
    assert.ok(
      starts[index] - starts[index - 1] >= 38,
      `start gap ${starts[index] - starts[index - 1]}ms`,
    );
  }
  assert.equal(semaphore.getAvailablePermits(), 4);
});

test("pacing deadline expires as admission without a late context", async () => {
  const pacer = new ScrapeStartPacer(200);
  await pacer.waitForStart(() => 300);
  const { options, calls } = fixture();
  options.deadlineAt = Date.now() + 50;
  await assert.rejects(
    runScrapeLifecycle({
      ...options,
      paceStart: (remaining) => pacer.waitForStart(remaining),
    }),
    isPhase("admission"),
  );
  assert.equal(options.semaphore.getAvailablePermits(), 1);
  await tick(220);
  assert.deepEqual(calls, []);
  // An idle gate has no reserved backlog: it claims synchronously.
  let remainingChecks = 0;
  await pacer.waitForStart(() => {
    remainingChecks++;
    return 100;
  });
  assert.equal(remainingChecks, 1);
});

test("default zero pacing never delays; invalid intervals fail closed", async () => {
  assert.equal(scrapeStartIntervalMs(undefined), 0);
  assert.equal(scrapeStartIntervalMs(""), 0);
  assert.equal(scrapeStartIntervalMs("5000"), 5000);
  for (const value of ["-1", "5001", "1.5", "invalid", "Infinity"]) {
    assert.throws(() => scrapeStartIntervalMs(value), /integer from 0 to 5000/);
  }
  assert.throws(() => new ScrapeStartPacer(-1), /integer from 0 to 5000/);
  const pacer = new ScrapeStartPacer(scrapeStartIntervalMs(""));
  let remainingChecks = 0;
  await Promise.all(
    Array.from({ length: 4 }, () =>
      pacer.waitForStart(() => {
        remainingChecks++;
        return 100;
      }),
    ),
  );
  assert.equal(remainingChecks, 4);
});
