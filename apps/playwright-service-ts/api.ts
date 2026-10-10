import express, { type Express, Request, Response } from "express";
import dotenv from "dotenv";
import type { BrowserContext } from "playwright";
import { createBrowserBatchFetchHandler } from "./browser_batch_route";
import { type BrowserPool, createBrowserPool } from "./browser_context";
import { Semaphore, withHardTimeout } from "./browser_resources";
import {
  assertC10BrowserListenerConfiguration,
  createC10BrowserListener,
  readC10BrowserListenerConfig,
} from "./c10_browser_listener";
import {
  PermitLease,
  awaitAllocation,
  releaseAfterClose,
} from "./permit_lease";
import { createScrapeHandler } from "./scrape_route";
import { ScrapeStartPacer, scrapeStartIntervalMs } from "./scrape_start_pacer";
import {
  type AssertSafeTargetUrl,
  assertSafeTargetUrl,
  startSsrfProxy,
} from "./target_guard";

dotenv.config();

const readFlag = (value: string | undefined): boolean =>
  (value || "False").toUpperCase() === "TRUE";
const readPositiveInt = (value: string | undefined, fallback: number): number =>
  Math.max(1, Number.parseInt(value ?? String(fallback), 10) || fallback);

export type ServiceConfig = Readonly<{
  port: string | number;
  blockMedia: boolean;
  maxConcurrentPages: number;
  maxConcurrentBrowserBatches: number;
  allowLocalWebhooks: boolean;
  proxy: Readonly<{
    server: string | null;
    username: string | null;
    password: string | null;
    country: string | undefined;
  }>;
  /** Opt-in global spacing of /scrape context allocations (default 0 = off). */
  scrapeStartIntervalMs: number;
}>;

/** Reads the service environment once. Throws on an invalid start interval. */
export function readServiceConfig(env: NodeJS.ProcessEnv): ServiceConfig {
  return {
    port: env.PORT || 3003,
    blockMedia: readFlag(env.BLOCK_MEDIA),
    maxConcurrentPages: readPositiveInt(env.MAX_CONCURRENT_PAGES, 10),
    maxConcurrentBrowserBatches: readPositiveInt(
      env.MAX_CONCURRENT_BROWSER_BATCHES,
      1,
    ),
    allowLocalWebhooks: readFlag(env.ALLOW_LOCAL_WEBHOOKS),
    proxy: {
      server: env.PROXY_SERVER || null,
      username: env.PROXY_USERNAME || null,
      password: env.PROXY_PASSWORD || null,
      country: env.PROXY_COUNTRY,
    },
    // "" keeps the default (0) without falling back to ambient process.env.
    scrapeStartIntervalMs: scrapeStartIntervalMs(
      env.SCRAPE_START_INTERVAL_MS ?? "",
    ),
  };
}

/** Bounds each /health probe step, so a hung probe cannot pin its permit. */
const HEALTH_PROBE_TIMEOUT_MS = 10_000;

/**
 * Opens and closes one context and page under a page permit the caller
 * already holds, so the probe counts against MAX_CONCURRENT_PAGES. The
 * context is closed even when newPage or page.close fails, and a close that
 * cannot be confirmed quarantines the permit and fails the probe.
 */
async function probeBrowserContext(
  browsers: BrowserPool,
  allowLocalTargets: boolean,
  lease: PermitLease,
): Promise<void> {
  let context: BrowserContext | undefined;
  const settle = async (): Promise<boolean> => {
    // A late context allocation already owns (or quarantined) the lease.
    if (!lease.heldByRequest) return false;
    return releaseAfterClose(
      lease,
      null,
      context ? () => context!.close() : null,
      HEALTH_PROBE_TIMEOUT_MS,
    );
  };
  try {
    ({ context } = await awaitAllocation(
      lease,
      () => browsers.createContext({ allowLocalTargets }),
      (allocation) =>
        withHardTimeout(
          allocation,
          HEALTH_PROBE_TIMEOUT_MS,
          "Health probe context creation timed out",
        ),
      (late) => late.context.close(),
    ));
    const page = await withHardTimeout(
      context.newPage(),
      HEALTH_PROBE_TIMEOUT_MS,
      "Health probe page creation timed out",
      (latePage) => latePage.close(),
    );
    await withHardTimeout(
      page.close(),
      HEALTH_PROBE_TIMEOUT_MS,
      "Health probe page close timed out",
    );
  } catch (error) {
    await settle();
    throw error;
  }
  if (!(await settle())) {
    throw new Error("Health probe context close was not confirmed");
  }
}

const CLOSED_TARGET_MESSAGE = "Target page, context or browser has been closed";

/**
 * Process policy for unhandled rejections. Node 15+ exits on one by default.
 * playwright-extra's stealth shim issues CDP calls it never awaits, so a page
 * closed mid-call (a /health probe, a timed-out navigation) rejects with
 * Playwright's closed-target error and used to take the whole sidecar down.
 * That class is benign by construction (the target is already gone): log it
 * and keep serving. Anything else is rethrown, which keeps Node's default
 * crash (uncaught exception, exit code 1).
 */
export function onUnhandledRejection(reason: unknown): void {
  if (reason instanceof Error && reason.message.includes(CLOSED_TARGET_MESSAGE)) {
    console.error("Unhandled closed-target rejection (stealth shim CDP call); continuing:", reason);
    return;
  }
  throw reason;
}

export type ServiceDeps = Readonly<{
  config: ServiceConfig;
  browsers: BrowserPool;
  /** Shared with the C10 listener, so it is created by the caller. */
  pageSemaphore: Semaphore;
  assertSafeTargetUrl: AssertSafeTargetUrl;
}>;

/** Builds the public app without starting the browser, proxy, or listeners. */
export function createApp(deps: ServiceDeps): Express {
  const { config, browsers, pageSemaphore } = deps;
  const app = express();
  app.use("/browser-batch-fetch", express.json({ limit: "600kb" }));
  app.use(express.json());

  app.get("/health", async (req: Request, res: Response) => {
    try {
      if (!browsers.getBrowser()) {
        await browsers.initializeBrowser();
      }

      // Probe only with a free page permit, never by waiting: when every
      // permit is busy, those pages are the live load and the probe would push
      // the browser past MAX_CONCURRENT_PAGES.
      const lease = PermitLease.tryAcquire(pageSemaphore);
      if (lease) {
        await probeBrowserContext(browsers, config.allowLocalWebhooks, lease);
      } else if (!browsers.getBrowser()?.isConnected()) {
        throw new Error("Browser is not connected");
      }

      res.status(200).json({
        status: "healthy",
        maxConcurrentPages: config.maxConcurrentPages,
        activePages:
          config.maxConcurrentPages - pageSemaphore.getAvailablePermits(),
      });
    } catch (error) {
      console.error("Health check failed:", error);
      res.status(503).json({
        status: "unhealthy",
        error: error instanceof Error ? error.message : "Unknown error occurred",
      });
    }
  });

  app.post(
    "/browser-batch-fetch",
    createBrowserBatchFetchHandler({
      browsers,
      pageSemaphore,
      browserBatchSemaphore: new Semaphore(
        Math.min(config.maxConcurrentBrowserBatches, config.maxConcurrentPages),
      ),
      assertSafeTargetUrl: deps.assertSafeTargetUrl,
    }),
  );

  const scrapeStartPacer = new ScrapeStartPacer(config.scrapeStartIntervalMs);
  app.post(
    "/scrape",
    createScrapeHandler({
      browsers,
      pageSemaphore,
      paceStart:
        config.scrapeStartIntervalMs > 0
          ? (remainingMs) => scrapeStartPacer.waitForStart(remainingMs)
          : undefined,
      allowLocalTargets: config.allowLocalWebhooks,
      proxyConfigured: Boolean(config.proxy.server),
      assertSafeTargetUrl: deps.assertSafeTargetUrl,
    }),
  );

  return app;
}

/** Production entrypoint: SSRF proxy, then Chromium, then the listeners. */
function runService(): void {
  process.on("unhandledRejection", onUnhandledRejection);
  // Read synchronously so an invalid SCRAPE_START_INTERVAL_MS still fails
  // while the entry module loads, as it did when these were module constants.
  const config = readServiceConfig(process.env);
  const c10ListenerConfig = readC10BrowserListenerConfig(process.env);
  let browsers: BrowserPool | undefined;

  const start = async () => {
    // The C10 listener's only failure mode is its configuration check, so a
    // partial C10 configuration still fails before the proxy or browser starts.
    assertC10BrowserListenerConfiguration(c10ListenerConfig);
    const ssrfProxyPort = await startSsrfProxy({
      allowLocalTargets: config.allowLocalWebhooks,
      upstream: config.proxy,
    });
    const pool = createBrowserPool({
      blockMedia: config.blockMedia,
      ssrfProxyPort,
      assertSafeTargetUrl,
    });
    browsers = pool;
    const pageSemaphore = new Semaphore(config.maxConcurrentPages);
    const c10Listener = createC10BrowserListener({
      config: c10ListenerConfig,
      maxConcurrentPages: config.maxConcurrentPages,
      proxyServer: config.proxy.server,
      proxyCountry: config.proxy.country,
      pageSemaphore,
      getBrowser: pool.getBrowser,
      initializeBrowser: pool.initializeBrowser,
      createContext: pool.createContext,
      assertSafeTargetUrl,
    });
    await pool.initializeBrowser();
    const app = createApp({
      config,
      browsers: pool,
      pageSemaphore,
      assertSafeTargetUrl,
    });
    app.listen(config.port, () => {
      console.log(`Server is running on port ${config.port}`);
    });
    if (c10Listener.enabled && c10Listener.port) {
      // Docker publishes this listener only as 127.0.0.1:<host-port>; binding
      // all interfaces is required for the container port-forwarder, while the
      // signed capability and host key protect sibling-container access.
      c10Listener.app.listen(c10Listener.port, "0.0.0.0", () => {
        console.log("C10 v3 browser listener is running behind Docker loopback publication");
      });
    }
  };
  start().catch((error) => {
    console.error("Failed to start server:", error);
    process.exit(1);
  });

  process.on("SIGINT", () => {
    (browsers ? browsers.close() : Promise.resolve()).then(() => {
      console.log("Browser closed");
      process.exit(0);
    });
  });
}

// Importing this module (tests, tooling) builds nothing and binds no ports.
if (require.main === module) {
  runService();
}
