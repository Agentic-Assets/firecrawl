import assert from "node:assert/strict";
import { request } from "node:http";
import type { AddressInfo } from "node:net";
import test from "node:test";
import { createApp, readServiceConfig } from "./api";
import type { BrowserPool, ContextBundle } from "./browser_context";
import { Semaphore } from "./browser_resources";
import { fixture, tick } from "./helpers/lifecycle_fixture";
import { runScrapeLifecycle, ScrapeClientGoneError } from "./scrape_lifecycle";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function until(predicate: () => boolean) {
  const deadline = Date.now() + 1000;
  while (!predicate()) {
    assert.ok(Date.now() < deadline, "cancellation did not settle within 1s");
    await tick(2);
  }
}

class CountedSemaphore extends Semaphore {
  releases = 0;
  override release() {
    this.releases++;
    super.release();
  }
}

async function withHttpFixture(
  work: "stalled" | "normal",
  run: (fixture: {
    url: string;
    semaphore: CountedSemaphore;
    created: () => number;
    closes: () => number;
    allowClose: () => void;
  }) => Promise<void>,
) {
  const semaphore = new CountedSemaphore(1);
  const closing = deferred<void>();
  let created = 0;
  let closes = 0;
  const page = {
    goto: async () =>
      work === "stalled"
        ? new Promise<never>(() => {})
        : {
            status: () => 200,
            allHeaders: async () => ({ "content-type": "text/html" }),
          },
    url: () => "https://fixture.example/property",
    waitForTimeout: async () => {},
    waitForSelector: async () => {},
    content: async () => "<html>fixture</html>",
    close: async () => {},
  };
  const context = {
    newPage: async () => page,
    close: async () => {
      closes++;
      if (work === "stalled") await closing.promise;
    },
  };
  const pool = {
    getBrowser: () => ({ isConnected: () => true }),
    initializeBrowser: async () => {},
    createContext: async () => {
      created++;
      return {
        context,
        securityState: {
          blockedNavigationRequestUrl: null,
          navigationDnsUnavailable: false,
        },
      } as unknown as ContextBundle;
    },
    close: async () => {},
  } as unknown as BrowserPool;
  const server = createApp({
    config: readServiceConfig({ MAX_CONCURRENT_PAGES: "1" }),
    browsers: pool,
    pageSemaphore: semaphore,
    assertSafeTargetUrl: async () => {},
  }).listen(0, "127.0.0.1");
  await new Promise((resolve) => server.once("listening", resolve));
  try {
    const { port } = server.address() as AddressInfo;
    await run({
      url: `http://127.0.0.1:${port}/scrape`,
      semaphore,
      created: () => created,
      closes: () => closes,
      allowClose: () => closing.resolve(),
    });
  } finally {
    closing.resolve();
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

function send(url: string) {
  const client = request(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
  });
  client.on("error", () => {}); // Deliberate client disconnect, not an unhandled error.
  client.end(
    JSON.stringify({ url: "https://fixture.example/property", timeout: 10000 }),
  );
  return client;
}

test(
  "HTTP disconnect removes a full-pool waiter without consuming a later permit",
  { timeout: 5000 },
  async () => {
    await withHttpFixture("stalled", async ({ url, semaphore, created }) => {
      await semaphore.acquire();
      const client = send(url);
      await until(() => semaphore.getQueueLength() === 1);
      client.destroy();
      await until(() => semaphore.getQueueLength() === 0);
      assert.equal(created(), 0);
      assert.equal(semaphore.getAvailablePermits(), 0);
      semaphore.release();
      assert.equal(semaphore.getAvailablePermits(), 1);
    });
  },
);

test(
  "HTTP disconnect interrupts stalled navigation and releases only after context closure",
  { timeout: 5000 },
  async () => {
    await withHttpFixture(
      "stalled",
      async ({ url, semaphore, created, closes, allowClose }) => {
        const client = send(url);
        await until(() => created() === 1);
        client.destroy();
        await until(() => closes() === 1);
        assert.equal(semaphore.getAvailablePermits(), 0);
        assert.equal(semaphore.releases, 0);
        allowClose();
        await until(() => semaphore.getAvailablePermits() === 1);
        assert.equal(semaphore.releases, 1);
        assert.equal(closes(), 1);
      },
    );
  },
);

test(
  "ordinary completed HTTP response is delivered and closes resources once",
  { timeout: 5000 },
  async () => {
    await withHttpFixture("normal", async ({ url, semaphore, closes }) => {
      const response = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          url: "https://fixture.example/property",
          timeout: 1000,
        }),
      });
      assert.equal(response.status, 200);
      assert.match(
        ((await response.json()) as { content: string }).content,
        /fixture/,
      );
      await until(() => semaphore.getAvailablePermits() === 1);
      assert.equal(closes(), 1);
      assert.equal(semaphore.releases, 1);
    });
  },
);

test(
  "cancelled context allocation hands off its lease until late closure",
  { timeout: 5000 },
  async () => {
    const semaphore = new CountedSemaphore(1);
    const { options, context } = fixture(semaphore);
    const allocation = deferred<typeof context>();
    const closing = deferred<void>();
    let started = false;
    let closeCalls = 0;
    const controller = new AbortController();
    const running = runScrapeLifecycle({
      ...options,
      deadlineAt: Date.now() + 10000,
      signal: controller.signal,
      createContext: () => {
        started = true;
        return allocation.promise;
      },
      closeContext: async () => {
        closeCalls++;
        await closing.promise;
      },
    });
    const rejected = assert.rejects(running, ScrapeClientGoneError);
    await until(() => started);
    controller.abort();
    await rejected;
    assert.equal(semaphore.getAvailablePermits(), 0);
    allocation.resolve(context);
    await until(() => closeCalls === 1);
    assert.equal(semaphore.getAvailablePermits(), 0);
    closing.resolve();
    await until(() => semaphore.getAvailablePermits() === 1);
    assert.equal(semaphore.releases, 1);
  },
);

test(
  "cancelled work with unconfirmed close stays quarantined and never delivers a late value",
  { timeout: 5000 },
  async () => {
    const semaphore = new CountedSemaphore(1);
    const { options } = fixture(semaphore);
    const work = deferred<string>();
    const controller = new AbortController();
    let started = false;
    let delivered = false;
    const running = runScrapeLifecycle({
      ...options,
      deadlineAt: Date.now() + 10000,
      signal: controller.signal,
      work: () => {
        started = true;
        return work.promise;
      },
      closeContext: () => new Promise<never>(() => {}),
      cleanupTimeoutMs: 10,
      deliverResult: () => {
        delivered = true;
      },
    });
    const rejected = assert.rejects(running, ScrapeClientGoneError);
    await until(() => started);
    controller.abort();
    await rejected;
    work.resolve("late");
    await tick();
    assert.equal(delivered, false);
    assert.equal(semaphore.getAvailablePermits(), 0);
    assert.equal(semaphore.releases, 0);
  },
);
