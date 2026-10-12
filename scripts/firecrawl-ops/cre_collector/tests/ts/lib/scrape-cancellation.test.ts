// Local HTTP fault fixture is explicit: no provider/API/database is contacted.
process.argv = [process.argv[0]!, process.argv[1]!];
import assert from "node:assert/strict";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  createScrapeClient,
  firecrawl,
  scrapeRaw,
} from "../../../lib/scrape.js";
import { classifyPerformanceError } from "../../../lib/performance.js";

for (const phase of ["headers", "body"] as const) {
  test(
    `deadline aborts stalled ${phase}, does not overlap a retry, and cannot overwrite fresh output`,
    { timeout: 5000 },
    async () => {
      const directory = await mkdtemp(join(tmpdir(), "cre-cancel-"));
      const artifact = join(directory, "result.txt");
      let requests = 0;
      let active = 0;
      let peak = 0;
      let closed!: () => void;
      const firstClosed = new Promise<void>((resolve) => {
        closed = resolve;
      });
      let late!: () => void;
      const lateResponse = new Promise<void>((resolve) => {
        late = resolve;
      });
      const bodies: Record<string, unknown>[] = [];
      const authorizations: unknown[] = [];
      const server = createServer(async (req, res) => {
        requests++;
        const ordinal = requests;
        active++;
        peak = Math.max(peak, active);
        res.once("close", () => {
          active--;
          if (ordinal === 1) closed();
        });
        authorizations.push(req.headers.authorization);
        const chunks: Buffer[] = [];
        for await (const chunk of req) chunks.push(Buffer.from(chunk));
        bodies.push(JSON.parse(Buffer.concat(chunks).toString()));
        if (ordinal === 1) {
          // Complete after the replacement artifact has been written. The old
          // request must have no path back to the caller's artifact promotion.
          if (phase === "body") {
            res.writeHead(200, { "content-type": "application/json" });
            res.flushHeaders();
          }
          await lateResponse;
        }
        if (!res.headersSent)
          res.writeHead(200, { "content-type": "application/json" });
        res.end(
          JSON.stringify({
            success: true,
            data: { rawHtml: ordinal === 1 ? "old" : "fresh" },
          }),
        );
      });
      await new Promise<void>((resolve) =>
        server.listen(0, "127.0.0.1", resolve),
      );
      const { port } = server.address() as AddressInfo;
      const client = createScrapeClient({
        FIRECRAWL_API_URL: `http://127.0.0.1:${port}`,
        FIRECRAWL_API_KEY: "fixture-only",
      });
      const original = firecrawl.scrape;
      firecrawl.scrape = client.scrape.bind(client);
      try {
        await assert.rejects(
          async () => {
            const value = await scrapeRaw("https://fixture.example/old", {
              timeout: 80,
              maxAge: 0,
            });
            await writeFile(artifact, value);
          },
          (error: unknown) =>
            classifyPerformanceError(error).remoteSettlementUnknown,
        );
        await firstClosed;
        assert.equal(
          requests,
          1,
          "unknown server settlement forbids automatic retry",
        );
        await assert.rejects(readFile(artifact), { code: "ENOENT" });
        await writeFile(
          artifact,
          await scrapeRaw("https://fixture.example/fresh", {
            timeout: 1000,
            maxAge: 0,
          }),
        );
        late();
        await new Promise((resolve) => setTimeout(resolve, 20));
        assert.equal(await readFile(artifact, "utf8"), "fresh");
        assert.equal(peak, 1);
        assert.equal(requests, 2);
        assert.deepEqual(authorizations, [
          "Bearer fixture-only",
          "Bearer fixture-only",
        ]);
        assert.equal(bodies[0]!.maxAge, 0);
        assert.deepEqual(bodies[0]!.formats, ["rawHtml"]);
        assert.equal(bodies[0]!.timeout, 80);
        assert.equal("signal" in bodies[0]!, false);
      } finally {
        late();
        firecrawl.scrape = original;
        server.closeAllConnections();
        await new Promise<void>((resolve) => server.close(() => resolve()));
        await rm(directory, { recursive: true, force: true });
      }
    },
  );
}

test(
  "transport preserves typed API errors and cancellation interrupts 502 backoff",
  { timeout: 5000 },
  async () => {
    let requests = 0;
    let status = 429;
    const server = createServer((_req, res) => {
      requests++;
      res.writeHead(status, { "content-type": "application/json" });
      res.end(
        JSON.stringify({
          success: false,
          code: "SCRAPE_PDF_OCR_BACKPRESSURE",
          error: "fixture backpressure",
          details: { retryAfterSeconds: 2 },
        }),
      );
    });
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", resolve),
    );
    const client = createScrapeClient({
      FIRECRAWL_API_URL: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
    });
    try {
      await assert.rejects(
        () => client.scrape("https://fixture.example/property"),
        {
          name: "FirecrawlSdkError",
          status: 429,
          code: "SCRAPE_PDF_OCR_BACKPRESSURE",
          message: "fixture backpressure",
          details: { retryAfterSeconds: 2 },
        },
      );
      status = 502;
      const controller = new AbortController();
      const timer = setTimeout(
        () => controller.abort(new Error("fixture stop")),
        80,
      );
      try {
        await assert.rejects(
          () =>
            client.scrape(
              "https://fixture.example/property",
              {},
              { signal: controller.signal },
            ),
          { name: "AbortError" },
        );
      } finally {
        clearTimeout(timer);
      }
      await new Promise((resolve) => setTimeout(resolve, 520));
      assert.equal(
        requests,
        2,
        "aborted backoff cannot start a later HTTP attempt",
      );
    } finally {
      server.closeAllConnections();
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  },
);
