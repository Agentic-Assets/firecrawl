import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import test from "node:test";
import { createApp, readServiceConfig, type ServiceDeps } from "./api";
import { BROWSER_BATCH_FETCH_MAX_TOTAL_DURATION_MS } from "./browser_batch_fetch";
import type { BrowserPool, ContextBundle } from "./browser_context";
import { Semaphore } from "./browser_resources";
import { ScrapeResourceLeakError } from "./scrape_lifecycle";

// Route-level permit accounting (AGENTIC-3758). The invariant: a page permit
// is held exactly as long as a browser context may be live, and a request
// never leaves a permit (or batch slot) behind once nothing is live.

// Batch admission can consume almost all of the 240 s batch budget. Rather
// than waiting, the target check advances this file's clock (node --test
// runs each file in its own process).
const realNow = Date.now.bind(Date);
let clockOffsetMs = 0;
Date.now = () => realNow() + clockOffsetMs;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

type PageFaults = {
  newPage?: () => Promise<never>;
  closePage?: () => Promise<never>;
};

class FakeContext {
  closeCalls = 0;
  constructor(
    private readonly live: Set<FakeContext>,
    private readonly faults: PageFaults = {},
  ) {
    live.add(this);
  }
  async newPage() {
    if (this.faults.newPage) return this.faults.newPage();
    return {
      goto: async () => ({ status: () => 200 }),
      url: () => "https://property.example/start",
      waitForTimeout: async () => {},
      evaluate: async () => ({
        status: 200,
        contentType: "application/json",
        body: "{}",
      }),
      close: this.faults.closePage ?? (async () => {}),
    };
  }
  async close() {
    this.closeCalls += 1;
    this.live.delete(this);
  }
}

/** A fake pool whose next contexts can be scripted; tracks live contexts. */
function scriptedBrowsers(connected = true) {
  const live = new Set<FakeContext>();
  const next: Array<() => Promise<FakeContext>> = [];
  let created = 0;
  const bundle = (context: FakeContext) =>
    ({
      context,
      securityState: {
        blockedNavigationRequestUrl: null,
        navigationDnsUnavailable: false,
      },
    }) as unknown as ContextBundle;
  const pool: BrowserPool = {
    getBrowser: () =>
      ({ isConnected: () => connected }) as unknown as ReturnType<
        BrowserPool["getBrowser"]
      >,
    initializeBrowser: async () => {},
    createContext: async () => {
      created += 1;
      const scripted = next.shift();
      return bundle(scripted ? await scripted() : new FakeContext(live));
    },
    close: async () => {},
  };
  return { pool, live, next, created: () => created };
}

async function withApp(
  deps: Omit<ServiceDeps, "config"> & { maxConcurrentPages: number },
  run: (baseUrl: string) => Promise<void>,
): Promise<void> {
  const { maxConcurrentPages, ...rest } = deps;
  const app = createApp({
    ...rest,
    config: readServiceConfig({
      MAX_CONCURRENT_PAGES: String(maxConcurrentPages),
    }),
  });
  const server = app.listen(0, "127.0.0.1");
  await new Promise((resolve) => server.once("listening", resolve));
  try {
    const { port } = server.address() as AddressInfo;
    await run(`http://127.0.0.1:${port}`);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

const postBatch = async (baseUrl: string) => {
  const response = await fetch(`${baseUrl}/browser-batch-fetch`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      bootstrapUrl: "https://property.example/start",
      requests: [
        {
          url: "https://property.example/api",
          method: "POST",
          headers: { "content-type": "application/json" },
          body: "{}",
        },
      ],
    }),
  });
  return { status: response.status, body: await response.json() };
};

const getHealth = async (baseUrl: string) => {
  const response = await fetch(`${baseUrl}/health`);
  return { status: response.status, body: await response.json() };
};

/** A target check that spends `jumps[n]` ms of the n-th batch's budget. */
const budgetSpender = (jumps: number[]) => async () => {
  clockOffsetMs += jumps.shift() ?? 0;
};

test("bug 1 (batch): a createContext leak keeps the page permit and frees the batch slot", async () => {
  const browsers = scriptedBrowsers();
  const pageSemaphore = new Semaphore(2);
  browsers.next.push(async () => {
    throw new ScrapeResourceLeakError(new Error("context.close failed"));
  });
  await withApp(
    {
      maxConcurrentPages: 2,
      browsers: browsers.pool,
      pageSemaphore,
      assertSafeTargetUrl: async () => {},
    },
    async (baseUrl) => {
      const leaked = await postBatch(baseUrl);
      assert.equal(leaked.status, 502);
      const afterLeak = pageSemaphore.getAvailablePermits();
      // The batch slot is not browser capacity: the next batch is admitted.
      const next = await postBatch(baseUrl);
      assert.deepEqual(
        { afterLeak, nextBatch: next.status },
        { afterLeak: 1, nextBatch: 200 },
        "an unconfirmed-closed context must keep counting against page capacity",
      );
      assert.equal(pageSemaphore.getAvailablePermits(), 1);
    },
  );
});

test("bug 2 control (batch): a context resolving after the deadline is closed and its permits released", async () => {
  const browsers = scriptedBrowsers();
  const pageSemaphore = new Semaphore(2);
  const late = deferred<FakeContext>();
  browsers.next.push(() => late.promise);
  await withApp(
    {
      maxConcurrentPages: 2,
      browsers: browsers.pool,
      pageSemaphore,
      assertSafeTargetUrl: budgetSpender([
        BROWSER_BATCH_FETCH_MAX_TOTAL_DURATION_MS - 100,
        BROWSER_BATCH_FETCH_MAX_TOTAL_DURATION_MS - 1_500,
      ]),
    },
    async (baseUrl) => {
      assert.equal((await postBatch(baseUrl)).status, 502);
      assert.equal(pageSemaphore.getAvailablePermits(), 1, "late context owns the permit");
      const lateContext = new FakeContext(browsers.live);
      late.resolve(lateContext);
      await sleep(20);
      assert.equal(lateContext.closeCalls, 1);
      assert.equal(pageSemaphore.getAvailablePermits(), 2);
      assert.equal((await postBatch(baseUrl)).status, 200);
    },
  );
});

test("bug 2 (batch): a context rejecting after the deadline releases the batch slot and page permit", async () => {
  const browsers = scriptedBrowsers();
  const pageSemaphore = new Semaphore(2);
  const late = deferred<FakeContext>();
  browsers.next.push(() => late.promise);
  await withApp(
    {
      maxConcurrentPages: 2,
      browsers: browsers.pool,
      pageSemaphore,
      assertSafeTargetUrl: budgetSpender([
        BROWSER_BATCH_FETCH_MAX_TOTAL_DURATION_MS - 100,
        BROWSER_BATCH_FETCH_MAX_TOTAL_DURATION_MS - 1_500,
      ]),
    },
    async (baseUrl) => {
      assert.equal((await postBatch(baseUrl)).status, 502);
      // browser.newContext fails after the deadline: no context ever existed.
      late.reject(
        new Error("browser.newContext: Target page, context or browser has been closed"),
      );
      await sleep(20);
      const createdBefore = browsers.created();
      const next = await postBatch(baseUrl);
      assert.deepEqual(
        {
          liveContexts: browsers.live.size,
          pagePermitsAvailable: pageSemaphore.getAvailablePermits(),
          nextBatchStatus: next.status,
          nextBatchReachedContext: browsers.created() > createdBefore,
        },
        {
          liveContexts: 0,
          pagePermitsAvailable: 2,
          nextBatchStatus: 200,
          nextBatchReachedContext: true,
        },
        "with nothing live, the batch slot and page permit must not stay held",
      );
    },
  );
});

for (const [stage, faults] of [
  [
    "newPage",
    {
      newPage: async () => {
        throw new Error("newPage failed");
      },
    },
  ],
  [
    "page.close",
    {
      closePage: async () => {
        throw new Error("page.close failed");
      },
    },
  ],
] as const) {
  test(`bug 4a: /health closes its probe context when ${stage} throws`, async () => {
    const browsers = scriptedBrowsers();
    const pageSemaphore = new Semaphore(1);
    let probe: FakeContext | undefined;
    browsers.next.push(async () => {
      probe = new FakeContext(browsers.live, faults);
      return probe;
    });
    await withApp(
      {
        maxConcurrentPages: 1,
        browsers: browsers.pool,
        pageSemaphore,
        assertSafeTargetUrl: async () => {},
      },
      async (baseUrl) => {
        const health = await getHealth(baseUrl);
        assert.equal(health.status, 503);
        assert.equal(health.body.status, "unhealthy");
        assert.deepEqual(
          { closeCalls: probe?.closeCalls, liveContexts: browsers.live.size },
          { closeCalls: 1, liveContexts: 0 },
          "the /health probe context was never closed",
        );
        assert.equal(pageSemaphore.getAvailablePermits(), 1);
      },
    );
  });
}

test("bug 4b: /health never opens a context beyond MAX_CONCURRENT_PAGES", async () => {
  const browsers = scriptedBrowsers();
  const pageSemaphore = new Semaphore(2);
  // Two in-flight scrapes hold every page permit.
  await pageSemaphore.acquire();
  await pageSemaphore.acquire();
  try {
    await withApp(
      {
        maxConcurrentPages: 2,
        browsers: browsers.pool,
        pageSemaphore,
        assertSafeTargetUrl: async () => {},
      },
      async (baseUrl) => {
        const health = await getHealth(baseUrl);
        assert.deepEqual(health, {
          status: 200,
          body: { status: "healthy", maxConcurrentPages: 2, activePages: 2 },
        });
        assert.equal(
          browsers.created(),
          0,
          "/health opened a context while every page permit was held",
        );
      },
    );
  } finally {
    pageSemaphore.release();
    pageSemaphore.release();
  }
});
