import assert from "node:assert/strict";
import test from "node:test";
import { Semaphore } from "./browser_resources";
import {
  MAX_SCRAPE_INPUT_MS,
  parseScrapeTiming,
  PLAYWRIGHT_DEADLINE_GRACE_MS,
  runScrapeLifecycle,
  ScrapeClientGoneError,
  ScrapeDeadlineError,
} from "./scrape_lifecycle";
import { fixture, isPhase, tick } from "./helpers/lifecycle_fixture";
import { BrowserResourceLeakError } from "./permit_lease";
import { TargetDnsUnavailableError } from "./target_dns";

const never = () => new Promise<never>(() => {});

test("queued scrape expires without creating a context or stealing the next permit", async () => {
  const semaphore = new Semaphore(1);
  await semaphore.acquire();
  const { options, calls } = fixture(semaphore);
  options.deadlineAt = Date.now() + 50;
  await assert.rejects(runScrapeLifecycle(options), isPhase("admission"));
  assert.equal(semaphore.getQueueLength(), 0);
  assert.deepEqual(calls, []);
  semaphore.release();
  assert.equal(
    await runScrapeLifecycle(fixture(semaphore).options),
    "<html>property</html>",
  );
  assert.equal(semaphore.getAvailablePermits(), 1);
});

for (const stage of ["newPage", "content"] as const) {
  test(`hung ${stage} releases its permit once the context is closed`, async () => {
    const { options, context, page, calls } = fixture();
    options.deadlineAt = Date.now() + 50;
    if (stage === "newPage") context.newPage = never;
    else page.content = never;
    await assert.rejects(runScrapeLifecycle(options), isPhase("work"));
    assert.ok(calls.includes("close-context"));
    assert.equal(options.semaphore.getAvailablePermits(), 1);
    assert.equal(
      await runScrapeLifecycle(fixture(options.semaphore).options),
      "<html>property</html>",
    );
    assert.equal(options.semaphore.getAvailablePermits(), 1);
  });
}

test("a context resolving after the deadline owns the permit until it is closed", async () => {
  const { options, context, calls } = fixture();
  let resolveContext!: (value: typeof context) => void;
  options.createContext = () =>
    new Promise((resolve) => {
      resolveContext = resolve;
    });
  options.deadlineAt = Date.now() + 50;
  await assert.rejects(runScrapeLifecycle(options), isPhase("work"));
  // Capacity is not handed out while the late allocation is still in flight.
  assert.equal(options.semaphore.getAvailablePermits(), 0);
  resolveContext(context);
  await tick();
  assert.deepEqual(calls, ["close-context"]);
  assert.equal(options.semaphore.getAvailablePermits(), 1);
});

test("a context rejecting after the deadline releases the permit exactly once", async () => {
  const { options } = fixture();
  let rejectContext!: (error: Error) => void;
  options.createContext = () =>
    new Promise((_, reject) => {
      rejectContext = reject;
    });
  options.deadlineAt = Date.now() + 50;
  await assert.rejects(runScrapeLifecycle(options), isPhase("work"));
  assert.equal(options.semaphore.getAvailablePermits(), 0);
  rejectContext(new Error("browser closed"));
  await tick();
  assert.equal(options.semaphore.getAvailablePermits(), 1);
});

test("a late context whose close cannot be confirmed stays quarantined", async () => {
  const { options, context } = fixture();
  let resolveContext!: (value: typeof context) => void;
  options.createContext = () =>
    new Promise((resolve) => {
      resolveContext = resolve;
    });
  options.closeContext = never;
  options.deadlineAt = Date.now() + 50;
  await assert.rejects(runScrapeLifecycle(options), ScrapeDeadlineError);
  resolveContext(context);
  await tick(20);
  assert.equal(options.semaphore.getAvailablePermits(), 0);
});

test("a late context rejecting with an unconfirmed partial close stays quarantined", async () => {
  const { options } = fixture();
  let rejectContext!: (error: Error) => void;
  options.createContext = () =>
    new Promise((_, reject) => {
      rejectContext = reject;
    });
  options.deadlineAt = Date.now() + 50;
  await assert.rejects(runScrapeLifecycle(options), ScrapeDeadlineError);
  rejectContext(new BrowserResourceLeakError(new Error("setup failed")));
  await tick();
  assert.equal(options.semaphore.getAvailablePermits(), 0);
});

test("an in-time context failure releases the permit; an unconfirmed partial close does not", async () => {
  const ok = fixture();
  ok.options.createContext = async () => {
    throw new Error("newContext failed");
  };
  await assert.rejects(runScrapeLifecycle(ok.options), /newContext failed/);
  assert.equal(ok.options.semaphore.getAvailablePermits(), 1);

  const leaked = fixture();
  leaked.options.createContext = async () => {
    throw new BrowserResourceLeakError(new Error("setup failed"));
  };
  await assert.rejects(
    runScrapeLifecycle(leaked.options),
    BrowserResourceLeakError,
  );
  assert.equal(leaked.options.semaphore.getAvailablePermits(), 0);
});

test("unconfirmed cleanup keeps the result but quarantines the permit", async () => {
  const { options } = fixture();
  options.closeContext = never;
  options.closePage = never;
  assert.equal(await runScrapeLifecycle(options), "<html>property</html>");
  assert.equal(options.semaphore.getAvailablePermits(), 0);
});

test("delivers success once, before cleanup, while still holding the permit", async () => {
  const { options } = fixture();
  let releaseClose!: () => void;
  options.closeContext = () =>
    new Promise<void>((resolve) => {
      releaseClose = resolve;
    });
  options.cleanupTimeoutMs = 500;
  let deliveryCount = 0;
  let permitsAtDelivery = -1;
  const lifecycle = runScrapeLifecycle({
    ...options,
    deliverResult: () => {
      deliveryCount++;
      permitsAtDelivery = options.semaphore.getAvailablePermits();
    },
  });
  await tick(25);
  assert.equal(deliveryCount, 1);
  assert.equal(permitsAtDelivery, 0);
  assert.equal(options.semaphore.getAvailablePermits(), 0);
  releaseClose();
  assert.equal(await lifecycle, "<html>property</html>");
  assert.equal(options.semaphore.getAvailablePermits(), 1);
  assert.equal(deliveryCount, 1);
});

test("a work failure is never delivered as a result", async () => {
  const { options } = fixture();
  let delivered = false;
  await assert.rejects(
    runScrapeLifecycle({
      ...options,
      work: async () => {
        throw new Error("navigation failed");
      },
      deliverResult: () => {
        delivered = true;
      },
    }),
    /navigation failed/,
  );
  assert.equal(delivered, false);
  assert.equal(options.semaphore.getAvailablePermits(), 1);
});

test("a late page is closed without a second permit release", async () => {
  const { options, context, page, calls } = fixture();
  let resolvePage!: (value: typeof page) => void;
  context.newPage = () =>
    new Promise((resolve) => {
      resolvePage = resolve;
    });
  options.deadlineAt = Date.now() + 50;
  await assert.rejects(runScrapeLifecycle(options), ScrapeDeadlineError);
  assert.equal(options.semaphore.getAvailablePermits(), 1);
  resolvePage(page);
  await tick();
  assert.deepEqual(calls, ["context", "close-context", "close-page"]);
  assert.equal(options.semaphore.getAvailablePermits(), 1);
});

test("validation timeout does not enter browser admission", async () => {
  const { options, calls } = fixture();
  options.prepare = never;
  options.deadlineAt = Date.now() + 50;
  await assert.rejects(runScrapeLifecycle(options), isPhase("admission"));
  assert.deepEqual(calls, []);
  assert.equal(options.semaphore.getAvailablePermits(), 1);
});

test("validation errors propagate unchanged without taking a permit", async () => {
  const { options, calls } = fixture();
  options.prepare = async () => {
    throw new TargetDnsUnavailableError();
  };
  await assert.rejects(runScrapeLifecycle(options), TargetDnsUnavailableError);
  assert.deepEqual(calls, []);
  assert.equal(options.semaphore.getAvailablePermits(), 1);
});

test("a disconnected client that wins a permit never allocates a context", async () => {
  const semaphore = new Semaphore(1);
  await semaphore.acquire();
  const { options, calls } = fixture(semaphore);
  const controller = new AbortController();
  options.deadlineAt = Date.now() + 200;
  const lifecycle = runScrapeLifecycle({
    ...options,
    signal: controller.signal,
  });
  controller.abort();
  semaphore.release();
  await assert.rejects(lifecycle, ScrapeClientGoneError);
  assert.deepEqual(calls, []);
  assert.equal(semaphore.getAvailablePermits(), 1);
});

test("a deadline passing right after the permit is granted is an admission timeout", async () => {
  const semaphore = new Semaphore(1);
  const { options, calls } = fixture(semaphore);
  options.deadlineAt = Date.now() + 30;
  const acquire = semaphore.acquire.bind(semaphore);
  semaphore.acquire = async (timeoutMs?: number) => {
    await acquire(timeoutMs);
    while (Date.now() <= options.deadlineAt) await tick(5);
  };
  await assert.rejects(runScrapeLifecycle(options), isPhase("admission"));
  assert.deepEqual(calls, []);
  assert.equal(semaphore.getAvailablePermits(), 1);
});

test("successful lifecycle returns the complete result unchanged", async () => {
  const { options } = fixture();
  const result = {
    content: "<html>rich page</html>",
    status: 200,
    contentType: "text/html",
    headers: { "content-type": "text/html" },
  };
  assert.equal(
    await runScrapeLifecycle({ ...options, work: async () => result }),
    result,
  );
});

test("queue time consumes the work budget", async () => {
  const semaphore = new Semaphore(1);
  await semaphore.acquire();
  const { options } = fixture(semaphore);
  options.deadlineAt = Date.now() + 200;
  options.work = async () => {
    await tick(300);
    return "late";
  };
  setTimeout(() => semaphore.release(), 50);
  await assert.rejects(runScrapeLifecycle(options), isPhase("work"));
  assert.equal(semaphore.getAvailablePermits(), 1);
});

test("concurrent mixed outcomes never over-release a shared semaphore", async () => {
  const semaphore = new Semaphore(2);
  const outcomes = await Promise.allSettled(
    Array.from({ length: 8 }, (_, index) => {
      const { options, page } = fixture(semaphore);
      options.deadlineAt = Date.now() + 60;
      if (index % 3 === 0) page.content = never;
      if (index % 3 === 1) {
        options.work = async () => {
          throw new Error("boom");
        };
      }
      return runScrapeLifecycle(options);
    }),
  );
  assert.ok(outcomes.some((outcome) => outcome.status === "rejected"));
  await tick(30);
  assert.equal(semaphore.getAvailablePermits(), 2);
  assert.equal(semaphore.getQueueLength(), 0);
});

test("a Playwright timeout with the deadline grace reports a work timeout", async () => {
  for (let attempt = 0; attempt < 10; attempt++) {
    const { options } = fixture();
    options.deadlineAt = Date.now() + 30;
    await assert.rejects(
      runScrapeLifecycle({
        ...options,
        work: (_page, _context, remainingMs) =>
          new Promise<string>((_, reject) =>
            setTimeout(
              () => reject(new Error("page.goto: Timeout exceeded")),
              remainingMs() + PLAYWRIGHT_DEADLINE_GRACE_MS,
            ),
          ),
      }),
      isPhase("work"),
    );
    assert.equal(options.semaphore.getAvailablePermits(), 1);
  }
});

test("browser work failing at or after the deadline is a work timeout", async () => {
  const { options } = fixture();
  options.deadlineAt = Date.now() + 30;
  await assert.rejects(
    runScrapeLifecycle({
      ...options,
      work: async () => {
        // Starve the lifecycle timer, then fail like a racing Playwright timeout.
        while (Date.now() <= options.deadlineAt) {}
        throw new Error("page.goto: Timeout exceeded");
      },
    }),
    isPhase("work"),
  );
  assert.equal(options.semaphore.getAvailablePermits(), 1);
});

test("scrape timing caps timeout and wait_after_load below timer overflow", () => {
  assert.ok(MAX_SCRAPE_INPUT_MS < 2 ** 31 - 1);
  assert.deepEqual(parseScrapeTiming(undefined, undefined), {
    ok: true,
    timeout: 15000,
    waitAfterLoad: 0,
  });
  assert.deepEqual(parseScrapeTiming(null, null), {
    ok: true,
    timeout: 15000,
    waitAfterLoad: 0,
  });
  assert.deepEqual(parseScrapeTiming(undefined, 2000), {
    ok: true,
    timeout: 17000,
    waitAfterLoad: 2000,
  });
  // The API sends its remaining budget and caps waitFor at 60 s.
  assert.deepEqual(parseScrapeTiming(600000, 60000), {
    ok: true,
    timeout: 600000,
    waitAfterLoad: 60000,
  });
  assert.deepEqual(
    parseScrapeTiming(MAX_SCRAPE_INPUT_MS, MAX_SCRAPE_INPUT_MS),
    {
      ok: true,
      timeout: MAX_SCRAPE_INPUT_MS,
      waitAfterLoad: MAX_SCRAPE_INPUT_MS,
    },
  );
  for (const timeout of [MAX_SCRAPE_INPUT_MS + 1, 2 ** 31, 2 ** 31 - 1]) {
    const timing = parseScrapeTiming(timeout, 0);
    assert.equal(timing.ok, false, String(timeout));
    assert.match(!timing.ok ? timing.error : "", /Timeout must not exceed/);
  }
  const longWait = parseScrapeTiming(undefined, MAX_SCRAPE_INPUT_MS + 1);
  assert.equal(longWait.ok, false);
  assert.match(
    !longWait.ok ? longWait.error : "",
    /wait_after_load must not exceed/,
  );
  for (const timeout of [0, -1, "1000", Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(parseScrapeTiming(timeout, 0).ok, false, String(timeout));
  }
  for (const wait of [-1, "1000", Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(parseScrapeTiming(1000, wait).ok, false, String(wait));
  }
});
