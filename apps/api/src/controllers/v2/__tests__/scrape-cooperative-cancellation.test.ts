import express from "express";
import { createServer, request, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, expect, it, vi } from "vitest";
import { scrapeController } from "../scrape";
import { processJobInternal } from "../../../services/worker/scrape-worker";
import { teamConcurrencySemaphore } from "../../../services/worker/team-semaphore";
import { AbortManager } from "../../../scraper/scrapeURL/lib/abortManager";
import { scrapeURLWithPlaywright } from "../../../scraper/scrapeURL/engines/playwright";
import { config } from "../../../config";

// Keep the real HTTP/controller/externalAbort/AbortManager/Playwright transport.
// Billing, policy and persistence do not participate in this local fault fixture.
vi.mock("../../../config", () => ({ config: { SENTRY_ENVIRONMENT: "dev" } }));
vi.mock("../scrape-alexandria", () => ({ providerScrapeController: vi.fn() }));
vi.mock("../../../search/alexandria", () => ({ discoverTools: vi.fn() }));
vi.mock("../types", () => ({
  scrapeRequestSchema: { parse: (x: unknown) => x },
}));
vi.mock("../../../lib/logger", () => {
  const logger = {
    child: () => logger,
    debug: vi.fn(),
    info: vi.fn(),
    error: vi.fn(),
    warn: vi.fn(),
  };
  return { logger };
});
vi.mock("../../../lib/otel-tracer", () => ({
  withSpan: async (_: unknown, fn: Function) => fn({}),
  setSpanAttributes: vi.fn(),
  SpanKind: {},
}));
vi.mock("../../../services/worker/nuq", () => ({}));
vi.mock("../../../lib/permissions", () => ({ checkPermissions: () => ({}) }));
vi.mock("../../../lib/safe-mode", () => ({
  applySafeMode: vi.fn(),
  resolveSafeMode: () => ({}),
  isLockdownZeroDataRetention: () => false,
}));
vi.mock("../../../lib/key-restriction", () => ({
  actionTypesOf: () => [],
  formatTypesOf: () => [],
  checkKeyFormatRestriction: () => ({ allowed: true }),
}));
vi.mock("../../../lib/exchange", () => ({
  ThirdPartyDataTermsRequiredError: class extends Error {},
}));
vi.mock("../../../services/worker/scrape-worker", () => ({
  processJobInternal: vi.fn(),
}));
vi.mock("../../../services/worker/team-semaphore", () => ({
  teamConcurrencySemaphore: { withSemaphore: vi.fn() },
}));
vi.mock("../../../lib/job-priority", () => ({ getJobPriority: () => 10 }));
vi.mock("../../../services/logging/log_job", () => ({
  logRequest: async () => {},
}));
vi.mock("../../../lib/external-request-id", () => ({
  externalRequestId: () => undefined,
}));
vi.mock("../../../lib/deployment", () => ({
  getErrorContactMessage: () => "fixture error",
}));
vi.mock("../../../lib/zdr-helpers", () => ({ getScrapeZDR: () => undefined }));
vi.mock("../../../lib/keyless", () => ({}));
vi.mock("../../../lib/keyless-credit-projection", () => ({
  projectScrapeCredits: () => 0,
}));
vi.mock("../../../lib/agent-auth-discovery", () => ({}));
vi.mock("../../../lib/threat-protection/request", () => ({
  resolveThreatProtection: () => ({}),
}));
vi.mock("../../../lib/siem-logging", () => ({}));
vi.mock("../../../lib/agent-interop", () => ({}));
vi.mock("../../../lib/alexandria-activity", () => ({}));
vi.mock("../../../services/autumn/autumn.service", () => ({
  DEFAULT_TEAM_LIMITS: { concurrency_limit: 2 },
}));
vi.mock("../../../scraper/scrapeURL/engines/fire-engine/scrape", () => ({
  fireEngineURL: "http://unused.invalid",
}));
vi.mock("../../../scraper/scrapeURL/lib/mock", () => ({ saveMock: vi.fn() }));

const servers: Server[] = [];
async function listen(server: Server) {
  servers.push(server);
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  return `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
}
function latch() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => {
    resolve = done;
  });
  return { promise, resolve };
}
afterEach(async () => {
  for (const server of servers.splice(0)) {
    server.closeAllConnections();
    await new Promise<void>(resolve => server.close(() => resolve()));
  }
  vi.clearAllMocks();
});
async function api() {
  const app = express();
  app.use(express.json());
  app.post("/v2/scrape", (req, res) => {
    Object.assign(req, { auth: { team_id: "fixture" }, acuc: { flags: null } });
    void scrapeController(req as any, res);
  });
  return listen(createServer(app));
}
function send(url: string) {
  const req = request(`${url}/v2/scrape`, {
    method: "POST",
    headers: { "content-type": "application/json" },
  });
  req.on("error", () => {}); // expected socket destruction in disconnect cases
  req.end(
    JSON.stringify({
      url: "https://fixture.example/property",
      formats: [{ type: "rawHtml" }],
      timeout: 10000,
    }),
  );
  return req;
}

it("disconnect during team admission aborts admission without creating a scrape job", async () => {
  const admitted = latch();
  const stopped = latch();
  vi.mocked(teamConcurrencySemaphore.withSemaphore).mockImplementation(
    async (_team, _id, _limit, signal) => {
      admitted.resolve();
      await new Promise<void>(resolve =>
        signal.addEventListener(
          "abort",
          () => {
            stopped.resolve();
            resolve();
          },
          { once: true },
        ),
      );
      throw signal.reason;
    },
  );
  const req = send(await api());
  await admitted.promise;
  req.destroy();
  await stopped.promise;
  expect(processJobInternal).not.toHaveBeenCalled();
}, 5000);

it("disconnect propagates through the actual controller and Playwright HTTP engine", async () => {
  const received = latch();
  const closed = latch();
  const finished = latch();
  config.PLAYWRIGHT_MICROSERVICE_URL = await listen(
    createServer(async (req, res) => {
      for await (const _ of req) {
        /* fully receive POST before waiting */
      }
      res.once("close", closed.resolve);
      received.resolve();
    }),
  );
  vi.mocked(teamConcurrencySemaphore.withSemaphore).mockImplementation(
    async (_team, _id, _limit, _signal, _timeout, work) => work(false),
  );
  let external: AbortSignal | undefined;
  vi.mocked(processJobInternal).mockImplementation(async job => {
    if (job.data.mode !== "single_urls")
      throw new Error("expected synchronous scrape");
    const abort = new AbortManager(job.data.internalOptions!.externalAbort);
    external = job.data.internalOptions!.externalAbort!.signal;
    expect(job.data.skipNuq).toBe(true);
    try {
      await scrapeURLWithPlaywright({
        url: job.data.url,
        options: {},
        mock: null,
        abort,
        logger: { child: () => ({ debug() {} }) },
      } as any);
      throw new Error("cancelled work unexpectedly completed");
    } finally {
      abort.dispose();
      finished.resolve();
    }
  });
  const req = send(await api());
  await received.promise;
  expect(
    external?.aborted,
    "ordinary request body completion is not cancellation",
  ).toBe(false);
  req.destroy();
  await Promise.all([closed.promise, finished.promise]);
  expect(external?.aborted).toBe(true);
  expect(processJobInternal).toHaveBeenCalledOnce();
}, 5000);

it("ordinary completed response keeps the in-process cancellation signal live", async () => {
  vi.mocked(teamConcurrencySemaphore.withSemaphore).mockImplementation(
    async (_team, _id, _limit, _signal, _timeout, work) => work(false),
  );
  let external: AbortSignal | undefined;
  vi.mocked(processJobInternal).mockImplementation(async job => {
    if (job.data.mode !== "single_urls")
      throw new Error("expected synchronous scrape");
    external = job.data.internalOptions!.externalAbort!.signal;
    return { rawHtml: "fixture", metadata: {} } as any;
  });
  const response = await fetch(`${await api()}/v2/scrape`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      url: "https://fixture.example/property",
      formats: [{ type: "rawHtml" }],
    }),
  });
  expect(response.status).toBe(200);
  expect(await response.json()).toMatchObject({
    success: true,
    data: { rawHtml: "fixture" },
  });
  await new Promise(resolve => setImmediate(resolve));
  expect(external?.aborted).toBe(false);
}, 5000);
