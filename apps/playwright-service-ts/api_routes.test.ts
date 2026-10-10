import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import test from "node:test";
import { createApp, readServiceConfig, type ServiceDeps } from "./api";
import type { BrowserPool, ContextBundle } from "./browser_context";
import { Semaphore } from "./browser_resources";
import { ScrapeResourceLeakError } from "./scrape_lifecycle";
import { TargetDnsUnavailableError } from "./target_dns";
import { InsecureConnectionError } from "./target_guard";

// Route-level coverage of the README's HTTP contract. A fake BrowserPool
// stands in for Chromium, so no browser, proxy, or DNS lookup is involved.

type FakePage = {
  goto?: () => Promise<unknown>;
};

function fakeBrowsers(
  calls: string[],
  { goto, ...pool }: Partial<BrowserPool> & FakePage = {},
): BrowserPool {
  const response = {
    status: () => 200,
    url: () => "https://property.example/",
    allHeaders: async () => ({ "content-type": "text/html" }),
    body: async () => Buffer.from(""),
  };
  const page = {
    goto: goto ?? (async () => response),
    url: () => "https://property.example/landed",
    waitForTimeout: async () => {},
    waitForSelector: async () => {},
    setExtraHTTPHeaders: async () => {},
    content: async () => "<html>property</html>",
    close: async () => {
      calls.push("close-page");
    },
  };
  const context = {
    newPage: async () => page,
    addCookies: async () => {},
    close: async () => {
      calls.push("close-context");
    },
  };
  return {
    getBrowser: () => undefined,
    initializeBrowser: async () => {
      calls.push("init");
    },
    createContext: async (options) => {
      calls.push(`context:${JSON.stringify(options)}`);
      return {
        context,
        securityState: {
          blockedNavigationRequestUrl: null,
          navigationDnsUnavailable: false,
        },
      } as unknown as ContextBundle;
    },
    close: async () => {},
    ...pool,
  };
}

async function withApp(
  overrides: Partial<ServiceDeps>,
  run: (baseUrl: string, deps: ServiceDeps) => Promise<void>,
): Promise<void> {
  const deps: ServiceDeps = {
    config: readServiceConfig({ MAX_CONCURRENT_PAGES: "1" }),
    browsers: fakeBrowsers([]),
    pageSemaphore: new Semaphore(1),
    assertSafeTargetUrl: async () => {},
    ...overrides,
  };
  const server = createApp(deps).listen(0, "127.0.0.1");
  await new Promise((resolve) => server.once("listening", resolve));
  try {
    const { port } = server.address() as AddressInfo;
    await run(`http://127.0.0.1:${port}`, deps);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

const post = async (baseUrl: string, path: string, body: object) => {
  const response = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return { status: response.status, body: await response.json() };
};

const batchBody = (host: string) => ({
  bootstrapUrl: `https://${host}/start`,
  requests: [
    {
      url: `https://${host}/api`,
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    },
  ],
});

test("readServiceConfig applies documented defaults from the given env only", () => {
  const config = readServiceConfig({});
  assert.equal(config.port, 3003);
  assert.equal(config.maxConcurrentPages, 10);
  assert.equal(config.maxConcurrentBrowserBatches, 1);
  assert.equal(config.allowLocalWebhooks, false);
  assert.equal(config.blockMedia, false);
  assert.equal(config.scrapeStartIntervalMs, 0);
  assert.equal(config.proxy.server, null);
  assert.equal(
    readServiceConfig({ ALLOW_LOCAL_WEBHOOKS: "true" }).allowLocalWebhooks,
    true,
  );
  assert.throws(
    () => readServiceConfig({ SCRAPE_START_INTERVAL_MS: "-1" }),
    /SCRAPE_START_INTERVAL_MS must be an integer/,
  );
});

test("/scrape returns the page with its landed url and releases its permit", async () => {
  const calls: string[] = [];
  await withApp({ browsers: fakeBrowsers(calls) }, async (baseUrl, deps) => {
    const response = await post(baseUrl, "/scrape", {
      url: "https://property.example/",
      skip_tls_verification: true,
      headers: { "User-Agent": "agent/1" },
    });
    assert.deepEqual(response, {
      status: 200,
      body: {
        content: "<html>property</html>",
        pageStatusCode: 200,
        contentType: "text/html",
        url: "https://property.example/landed",
      },
    });
    assert.equal(calls[0], "init");
    assert.match(
      calls[1],
      /^context:\{"allowLocalTargets":false,"skipTlsVerification":true,"userAgent":"agent\/1","deadlineAt":\d+\}$/,
    );
    assert.deepEqual(calls.slice(2), ["close-page", "close-context"]);
    assert.equal(deps.pageSemaphore.getAvailablePermits(), 1);
  });
});

test("/scrape rejects malformed input with 400 before any browser work", async () => {
  const calls: string[] = [];
  await withApp({ browsers: fakeBrowsers(calls) }, async (baseUrl) => {
    assert.deepEqual(await post(baseUrl, "/scrape", {}), {
      status: 400,
      body: { error: "URL is required" },
    });
    assert.deepEqual(await post(baseUrl, "/scrape", { url: "not a url" }), {
      status: 400,
      body: { error: "Invalid URL" },
    });
    assert.deepEqual(
      await post(baseUrl, "/scrape", {
        url: "https://property.example/",
        timeout: 0,
      }),
      { status: 400, body: { error: "Timeout must be a positive finite number" } },
    );
  });
  assert.deepEqual(calls, []);
});

test("/scrape maps blocked and unresolvable targets to the documented contract", async () => {
  await withApp(
    {
      assertSafeTargetUrl: async () => {
        throw new TargetDnsUnavailableError();
      },
    },
    async (baseUrl) => {
      assert.deepEqual(
        await post(baseUrl, "/scrape", { url: "https://property.invalid/" }),
        {
          status: 503,
          body: {
            error: "Target DNS validation is unavailable",
            code: "TARGET_DNS_UNAVAILABLE",
          },
        },
      );
    },
  );
  const seen: Array<[string, boolean | undefined]> = [];
  await withApp(
    {
      assertSafeTargetUrl: async (url, allowLocalTargets) => {
        seen.push([url, allowLocalTargets]);
        throw new InsecureConnectionError(
          url,
          "resolves to a private/internal address",
        );
      },
    },
    async (baseUrl) => {
      assert.deepEqual(
        await post(baseUrl, "/scrape", { url: "http://127.0.0.1/" }),
        {
          status: 200,
          body: {
            content: "",
            pageStatusCode: 403,
            pageError:
              'Blocked insecure target URL "http://127.0.0.1/": resolves to a private/internal address',
          },
        },
      );
    },
  );
  // The prepare phase checks the URL with the configured local-target policy.
  assert.deepEqual(seen, [["http://127.0.0.1/", false]]);
});

test("/scrape passes ALLOW_LOCAL_WEBHOOKS to the target check", async () => {
  const seen: Array<[string, boolean | undefined]> = [];
  await withApp(
    {
      config: readServiceConfig({ ALLOW_LOCAL_WEBHOOKS: "true" }),
      assertSafeTargetUrl: async (url, allowLocalTargets) => {
        seen.push([url, allowLocalTargets]);
      },
    },
    async (baseUrl) => {
      const response = await post(baseUrl, "/scrape", {
        url: "http://127.0.0.1/",
      });
      assert.equal(response.status, 200);
    },
  );
  assert.deepEqual(seen, [["http://127.0.0.1/", true]]);
});

test("/scrape reports a saturated queue as an admission timeout", async () => {
  const pageSemaphore = new Semaphore(1);
  await pageSemaphore.acquire();
  await withApp({ pageSemaphore }, async (baseUrl) => {
    assert.deepEqual(
      await post(baseUrl, "/scrape", {
        url: "https://property.example/",
        timeout: 50,
      }),
      {
        status: 503,
        body: {
          error: "Browser scrape admission deadline exceeded",
          code: "SCRAPE_ADMISSION_TIMEOUT",
        },
      },
    );
  });
  assert.equal(pageSemaphore.getAvailablePermits(), 0);
  pageSemaphore.release();
});

test("/scrape reports a navigation past the deadline as a work timeout", async () => {
  const calls: string[] = [];
  const browsers = fakeBrowsers(calls, { goto: () => new Promise(() => {}) });
  await withApp({ browsers }, async (baseUrl, deps) => {
    assert.deepEqual(
      await post(baseUrl, "/scrape", {
        url: "https://property.example/",
        timeout: 50,
      }),
      {
        status: 504,
        body: {
          error: "Browser scrape work deadline exceeded",
          code: "SCRAPE_WORK_TIMEOUT",
        },
      },
    );
    assert.equal(deps.pageSemaphore.getAvailablePermits(), 1);
  });
});

test("/scrape keeps a leaked context's permit and reports the leak", async () => {
  const browsers = fakeBrowsers([], {
    createContext: async () => {
      throw new ScrapeResourceLeakError(new Error("close failed"));
    },
  });
  await withApp({ browsers }, async (baseUrl, deps) => {
    assert.deepEqual(
      await post(baseUrl, "/scrape", { url: "https://property.example/" }),
      {
        status: 503,
        body: {
          error: "Browser scrape resource cleanup was not confirmed",
          code: "SCRAPE_RESOURCE_LEAK",
        },
      },
    );
    assert.equal(deps.pageSemaphore.getAvailablePermits(), 0);
  });
});

test("/scrape hides unexpected failures behind a generic 500", async () => {
  const browsers = fakeBrowsers([], {
    goto: async () => {
      throw new Error("net::ERR_CONNECTION_RESET");
    },
  });
  await withApp({ browsers }, async (baseUrl) => {
    assert.deepEqual(
      await post(baseUrl, "/scrape", { url: "https://property.example/" }),
      {
        status: 500,
        body: { error: "An error occurred while fetching the page." },
      },
    );
  });
});

test("/browser-batch-fetch refuses bad input, private and unresolvable bootstraps", async () => {
  let allowLocalTargetsSeen: boolean | undefined;
  await withApp(
    {
      config: readServiceConfig({ ALLOW_LOCAL_WEBHOOKS: "true" }),
      assertSafeTargetUrl: async (url, allowLocalTargets) => {
        allowLocalTargetsSeen = allowLocalTargets;
        if (url.includes("unresolvable")) throw new TargetDnsUnavailableError();
        throw new InsecureConnectionError(
          url,
          "resolves to a private/internal address",
        );
      },
    },
    async (baseUrl) => {
      const invalid = await post(baseUrl, "/browser-batch-fetch", {});
      assert.equal(invalid.status, 400);
      assert.equal(typeof invalid.body.error, "string");

      const privateBatch = batchBody("property.example");
      assert.deepEqual(await post(baseUrl, "/browser-batch-fetch", privateBatch), {
        status: 400,
        body: {
          error:
            'Blocked insecure target URL "https://property.example/start": resolves to a private/internal address',
        },
      });
      // Batch never honors ALLOW_LOCAL_WEBHOOKS.
      assert.equal(allowLocalTargetsSeen, false);

      const unresolvable = batchBody("unresolvable.example");
      assert.deepEqual(
        await post(baseUrl, "/browser-batch-fetch", unresolvable),
        {
          status: 503,
          body: {
            error: "Target DNS validation is unavailable",
            code: "TARGET_DNS_UNAVAILABLE",
          },
        },
      );
    },
  );
});

test("/health opens and closes a context and reports page capacity", async () => {
  const calls: string[] = [];
  await withApp({ browsers: fakeBrowsers(calls) }, async (baseUrl) => {
    const response = await fetch(`${baseUrl}/health`);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), {
      status: "healthy",
      maxConcurrentPages: 1,
      activePages: 0,
    });
  });
  assert.deepEqual(calls, [
    "init",
    'context:{"allowLocalTargets":false}',
    "close-page",
    "close-context",
  ]);

  const failing = fakeBrowsers([], {
    createContext: async () => {
      throw new Error("browser unavailable");
    },
  });
  await withApp({ browsers: failing }, async (baseUrl) => {
    const response = await fetch(`${baseUrl}/health`);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), {
      status: "unhealthy",
      error: "browser unavailable",
    });
  });
});
