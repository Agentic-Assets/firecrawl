import assert from "node:assert/strict";
import test from "node:test";
import { Semaphore } from "./browser_batch_fetch";
import {
  runScrapeLifecycle,
  ScrapeClientGoneError,
  ScrapeDeadlineError,
  ScrapeResourceLeakError,
} from "./scrape_lifecycle";
import { isInternalHost, TargetDnsUnavailableError } from "./target_dns";
import { ScrapeStartPacer, scrapeStartIntervalMs } from "./scrape_start_pacer";

const never = () => new Promise<never>(() => {});
const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));
const isPhase = (phase: "admission" | "work") => (error: unknown) =>
  error instanceof ScrapeDeadlineError && error.phase === phase;

function fixture(semaphore = new Semaphore(1)) {
  const calls: string[] = [];
  const page = { content: async () => "<html>property</html>" };
  const context = { newPage: async () => page };
  return {
    calls,
    page,
    context,
    options: {
      deadlineAt: Date.now() + 100,
      semaphore,
      prepare: async () => {},
      createContext: async () => {
        calls.push("context");
        return context;
      },
      createPage: (value: typeof context) => value.newPage(),
      work: (value: typeof page) => value.content(),
      closeContext: async () => {
        calls.push("close-context");
      },
      closePage: async () => {
        calls.push("close-page");
      },
      cleanupTimeoutMs: 5,
    },
  };
}

test("queued scrape expires without creating a context or stealing the next permit", async () => {
  const semaphore = new Semaphore(1);
  await semaphore.acquire();
  const { options, calls } = fixture(semaphore);
  options.deadlineAt = Date.now() + 10;
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
    options.deadlineAt = Date.now() + 10;
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
  options.deadlineAt = Date.now() + 10;
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
  options.deadlineAt = Date.now() + 10;
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
  options.deadlineAt = Date.now() + 10;
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
  options.deadlineAt = Date.now() + 10;
  await assert.rejects(runScrapeLifecycle(options), ScrapeDeadlineError);
  rejectContext(new ScrapeResourceLeakError(new Error("setup failed")));
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
    throw new ScrapeResourceLeakError(new Error("setup failed"));
  };
  await assert.rejects(
    runScrapeLifecycle(leaked.options),
    ScrapeResourceLeakError,
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
  await tick(5);
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
  options.deadlineAt = Date.now() + 10;
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
  options.deadlineAt = Date.now() + 10;
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
  options.deadlineAt = Date.now() + 80;
  options.work = async () => {
    await tick(60);
    return "late";
  };
  setTimeout(() => semaphore.release(), 40);
  await assert.rejects(runScrapeLifecycle(options), isPhase("work"));
  assert.equal(semaphore.getAvailablePermits(), 1);
});

test("concurrent mixed outcomes never over-release a shared semaphore", async () => {
  const semaphore = new Semaphore(2);
  const outcomes = await Promise.allSettled(
    Array.from({ length: 8 }, (_, index) => {
      const { options, page } = fixture(semaphore);
      options.deadlineAt = Date.now() + 30;
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
  await tick(10);
  assert.equal(semaphore.getAvailablePermits(), 2);
  assert.equal(semaphore.getQueueLength(), 0);
});

test("DNS errors and empty resolution fail closed with a distinct transient error", async () => {
  await assert.rejects(
    isInternalHost("property.example", async () => {
      throw new Error("EAI_AGAIN");
    }),
    TargetDnsUnavailableError,
  );
  await assert.rejects(
    isInternalHost("property.example", async () => []),
    TargetDnsUnavailableError,
  );
});

test("private, mapped, mixed and public answers keep SSRF classification", async () => {
  const resolverMustNotRun = async () => {
    throw new Error("literal addresses must not be resolved");
  };
  for (const literal of [
    "127.0.0.1",
    "::1",
    "10.1.2.3",
    "169.254.169.254",
    "::ffff:127.0.0.1",
    "0.0.0.0",
  ]) {
    assert.equal(
      await isInternalHost(literal, resolverMustNotRun),
      true,
      literal,
    );
  }
  assert.equal(await isInternalHost("", resolverMustNotRun), true);
  for (const answer of ["127.0.0.1", "192.168.1.10", "fd00::1", "::ffff:10.0.0.1"]) {
    assert.equal(
      await isInternalHost("property.example", async () => [
        { address: answer },
      ]),
      true,
      answer,
    );
  }
  assert.equal(
    await isInternalHost("property.example", async () => [
      { address: "8.8.8.8" },
      { address: "10.0.0.1" },
    ]),
    true,
  );
  assert.equal(
    await isInternalHost("Property.Example.", async (host) => {
      assert.equal(host, "property.example");
      return [{ address: "8.8.8.8" }];
    }),
    false,
  );
});

test("global pacing spaces concurrent context allocations after admission", async () => {
  const semaphore = new Semaphore(4);
  const pacer = new ScrapeStartPacer(15);
  const starts: number[] = [];
  await Promise.all(
    Array.from({ length: 4 }, () => {
      const { options, context } = fixture(semaphore);
      options.deadlineAt = Date.now() + 500;
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
      starts[index] - starts[index - 1] >= 14,
      `start gap ${starts[index] - starts[index - 1]}ms`,
    );
  }
  assert.equal(semaphore.getAvailablePermits(), 4);
});

test("pacing deadline expires as admission without a late context", async () => {
  const pacer = new ScrapeStartPacer(60);
  await pacer.waitForStart(() => 100);
  const { options, calls } = fixture();
  options.deadlineAt = Date.now() + 10;
  await assert.rejects(
    runScrapeLifecycle({
      ...options,
      paceStart: (remaining) => pacer.waitForStart(remaining),
    }),
    isPhase("admission"),
  );
  assert.equal(options.semaphore.getAvailablePermits(), 1);
  await tick(70);
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
