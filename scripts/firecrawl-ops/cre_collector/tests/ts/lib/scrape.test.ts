// Isolate argv before scrape.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { setImmediate } from "node:timers/promises";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import {
  configuredScrapeMaxAttempts,
  createScrapeClient,
  firecrawl,
  parseJsonBody,
  repairUnescapedJsonStringQuotes,
  scrapeRetryDelayMs,
  scrapeRaw,
  scrapeJson,
  withRequestDeadline,
} from "../../../lib/scrape.js";

describe("scrape retry controls", () => {
  it("defaults to three attempts and accepts only the bounded opt-in", () => {
    assert.equal(configuredScrapeMaxAttempts({}), 3);
    assert.equal(
      configuredScrapeMaxAttempts({ CRE_SCRAPE_MAX_ATTEMPTS: "1" }),
      1
    );
    assert.equal(
      configuredScrapeMaxAttempts({ CRE_SCRAPE_MAX_ATTEMPTS: "3" }),
      3
    );
    assert.throws(
      () => configuredScrapeMaxAttempts({ CRE_SCRAPE_MAX_ATTEMPTS: "0" }),
      /integer from 1 through 3/
    );
    assert.throws(
      () => configuredScrapeMaxAttempts({ CRE_SCRAPE_MAX_ATTEMPTS: "4" }),
      /integer from 1 through 3/
    );
  });

  it("never schedules backoff after the terminal attempt", () => {
    assert.equal(scrapeRetryDelayMs(1, 3), 2500);
    assert.equal(scrapeRetryDelayMs(2, 3), 5000);
    assert.equal(scrapeRetryDelayMs(3, 3), null);
    assert.equal(scrapeRetryDelayMs(1, 1), null);
  });

  for (const [name, environment, expectedAttempts] of [
    ["default", {}, 3],
    ["benchmark", { CRE_SCRAPE_MAX_ATTEMPTS: "1" }, 1],
  ] as const) {
    it(`bounds ${name} HTTP attempts when the gateway returns 502`, async () => {
      let attempts = 0;
      const server = createServer((_req, res) => {
        attempts += 1;
        res.writeHead(502, { "content-type": "application/json" });
        res.end(JSON.stringify({ success: false }));
      });
      await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
      const { port } = server.address() as AddressInfo;
      const client = createScrapeClient({
        ...environment,
        FIRECRAWL_API_URL: `http://127.0.0.1:${port}`,
      });
      try {
        await assert.rejects(
          () => client.scrape("https://example.com/attempt-bound"),
          { status: 502, name: "FirecrawlSdkError" }
        );
        assert.equal(attempts, expectedAttempts);
      } finally {
        server.closeAllConnections();
        await new Promise<void>((resolve) => server.close(() => resolve()));
      }
    });
  }
});

describe("scrape freshness options", () => {
  it("forwards explicit maxAge zero and preserves the default when omitted", async () => {
    const original = firecrawl.scrape;
    const seen: any[] = [];
    (firecrawl as any).scrape = async (_url: string, options: any) => {
      seen.push(options);
      return { rawHtml: "<html>ok</html>" };
    };
    try {
      await scrapeRaw("https://example.com/fresh", { maxAge: 0 });
      await scrapeRaw("https://example.com/default");
    } finally {
      (firecrawl as any).scrape = original;
    }
    assert.equal(seen[0]?.maxAge, 0);
    assert.equal("maxAge" in seen[1], false);
  });
});

describe("withRequestDeadline", () => {
  it("returns a settled request result", async () => {
    assert.equal(await withRequestDeadline(Promise.resolve("ok"), 25), "ok");
  });

  it("rejects a request that does not settle within its client deadline", async () => {
    await assert.rejects(
      () => withRequestDeadline(new Promise<never>(() => undefined), 5),
      /timed out after 5ms/
    );
  });
});

describe("parseJsonBody", () => {
  it("parses plain JSON object", () => {
    assert.deepEqual(parseJsonBody('{"count":42,"ok":true}'), { count: 42, ok: true });
  });

  it("parses plain JSON array", () => {
    assert.deepEqual(parseJsonBody("[1,2,3]"), [1, 2, 3]);
  });

  it("parses HTML-wrapped JSON (Chrome viewer style)", () => {
    const wrapped =
      '<html><body><pre id="json">{"name":&quot;Tower&quot;,&quot;id&quot;:7}</pre></body></html>';
    assert.deepEqual(parseJsonBody(wrapped), { name: "Tower", id: 7 });
  });

  it("parses JSON embedded in HTML with entity escapes", () => {
    const wrapped = "<div>&lt;ignored&gt;</div>{&quot;items&quot;:[&quot;a&quot;,&quot;b&quot;]}";
    assert.deepEqual(parseJsonBody(wrapped), { items: ["a", "b"] });
  });

  it("repairs unescaped inner quotes in string values", () => {
    const broken = '{"title":"12" Main St"}';
    assert.deepEqual(parseJsonBody(broken), { title: '12" Main St' });
  });

  it("returns null for malformed JSON with no recoverable span", () => {
    assert.equal(parseJsonBody("not json at all"), null);
    assert.equal(parseJsonBody("{broken"), null);
    assert.equal(parseJsonBody(""), null);
  });
});

describe("repairUnescapedJsonStringQuotes", () => {
  it("escapes inner quotes that are not string terminators", () => {
    const input = '{"note":"said "hello" there"}';
    const repaired = repairUnescapedJsonStringQuotes(input);
    assert.equal(repaired, '{"note":"said \\"hello\\" there"}');
    assert.deepEqual(JSON.parse(repaired), { note: 'said "hello" there' });
  });

  it("leaves valid JSON string boundaries unchanged", () => {
    const input = '{"a":"x","b":"y"}';
    assert.equal(repairUnescapedJsonStringQuotes(input), input);
  });

  it("preserves already-escaped quotes", () => {
    const input = '{"msg":"already \\"fine\\""}';
    assert.equal(repairUnescapedJsonStringQuotes(input), input);
  });
});

describe("JSON parse backoff", () => {
  for (const attempts of [1, 3]) {
    it(`rejects after ${attempts} invalid bodies without a terminal wait`, async t => {
      t.mock.timers.enable({ apis: ["setTimeout"] });
      let calls = 0;
      let settled = false;
      let failure: unknown;
      const logs: string[] = [];
      t.mock.method(console, "error", (...parts: unknown[]) => logs.push(parts.join(" ")));
      t.mock.method(firecrawl, "scrape", async () => {
        calls++;
        return { rawHtml: "not JSON" };
      });
      t.mock.method(globalThis, "fetch", async () => { throw new Error("Unexpected network"); });
      const pending = scrapeJson("https://example.com/json-backoff", attempts === 1 ? { jsonAttempts: 1 } : {})
        .catch(error => { settled = true; failure = error; });
      await setImmediate();
      assert.equal(calls, 1);
      if (attempts === 3) {
        assert.equal(settled, false);
        t.mock.timers.tick(7999);
        await setImmediate();
        assert.equal(calls, 1);
        t.mock.timers.tick(1);
        await setImmediate();
        assert.equal(calls, 2);
        assert.equal(settled, false);
        t.mock.timers.tick(15999);
        await setImmediate();
        assert.equal(calls, 2);
        t.mock.timers.tick(1);
        await setImmediate();
        assert.equal(calls, 3);
      }
      assert.equal(settled, true, "no final JSON parse delay may remain pending");
      assert.match(String(failure), /contained no parseable JSON object/);
      assert.equal(logs.filter(line => line.includes("backing off")).length, attempts - 1);
      await pending;
    });
  }

  for (const successAttempt of [1, 2]) {
    it(`preserves a valid JSON result on attempt ${successAttempt}`, async t => {
      t.mock.timers.enable({ apis: ["setTimeout"] });
      let calls = 0;
      t.mock.method(console, "error", () => undefined);
      t.mock.method(firecrawl, "scrape", async () => ({
        rawHtml: ++calls === successAttempt ? '{"ok":true}' : "not JSON",
      }));
      const pending = scrapeJson("https://example.com/json-success");
      await setImmediate();
      if (successAttempt === 2) {
        assert.equal(calls, 1);
        t.mock.timers.tick(8000);
        await setImmediate();
      }
      assert.deepEqual(await pending, { ok: true });
      assert.equal(calls, successAttempt);
    });
  }

  for (const name of ["Error", "AbortError"]) {
    it(`propagates ${name} without a JSON parse retry`, async t => {
      const previous = process.env.CRE_SCRAPE_MAX_ATTEMPTS;
      process.env.CRE_SCRAPE_MAX_ATTEMPTS = "1";
      t.after(() => {
        if (previous === undefined) delete process.env.CRE_SCRAPE_MAX_ATTEMPTS;
        else process.env.CRE_SCRAPE_MAX_ATTEMPTS = previous;
      });
      t.mock.timers.enable({ apis: ["setTimeout"] });
      const failure = Object.assign(new Error("synthetic transport failure"), { name });
      let calls = 0;
      t.mock.method(console, "error", () => undefined);
      t.mock.method(firecrawl, "scrape", async () => { calls++; throw failure; });
      await assert.rejects(scrapeJson("https://example.com/json-error"), error => error === failure);
      assert.equal(calls, 1);
    });
  }
});

describe("observed JSON scrape context", () => {
  it("returns the Firecrawl rawHtml and metadata beside parsed plain or wrapped JSON", async t => {
    const scrapeModule = await import("../../../lib/scrape.js");
    const scrapeJsonWithObservation = (scrapeModule as any).scrapeJsonWithObservation;
    assert.equal(typeof scrapeJsonWithObservation, "function");

    const original = firecrawl.scrape;
    const observations = [
      {
        rawHtml: '{"Documents":[],"DocumentCount":0}',
        metadata: { statusCode: 200, sourceURL: "https://example.com/inventory", url: "https://example.com/final" },
      },
      {
        rawHtml: '<html><pre>{"Documents":[],"DocumentCount":0}</pre></html>',
        metadata: { statusCode: 206, sourceURL: "https://example.com/wrapped", url: "https://example.com/wrapped-final" },
      },
    ];
    let calls = 0;
    t.mock.method(firecrawl, "scrape", async () => observations[calls++]!);
    t.after(() => { (firecrawl as any).scrape = original; });

    for (const observation of observations) {
      const result = await scrapeJsonWithObservation("https://example.com/inventory");
      assert.deepEqual(result.data, { Documents: [], DocumentCount: 0 });
      assert.equal(result.response.rawHtml, observation.rawHtml);
      assert.deepEqual(result.response.metadata, observation.metadata);
      assert.equal("sha256" in result.response, false, "ordinary observations omit diagnostic digests");
    }
    assert.equal(calls, observations.length);
  });

  it("preserves the legacy parse result for an entity-encoded empty-array fragment", async t => {
    const scrapeModule = await import("../../../lib/scrape.js");
    const scrapeJsonWithObservation = (scrapeModule as any).scrapeJsonWithObservation;
    const rawHtml = '<html><pre>{&quot;Documents&quot;:[],&quot;DocumentCount&quot;:0}</pre></html>';
    const original = firecrawl.scrape;
    let calls = 0;
    t.mock.method(firecrawl, "scrape", async () => { calls++; return { rawHtml }; });
    t.after(() => { (firecrawl as any).scrape = original; });

    assert.deepEqual(await scrapeJson("https://example.com/legacy-parse"), []);
    const observed = await scrapeJsonWithObservation("https://example.com/observed-parse");
    assert.deepEqual(observed.data, []);
    assert.equal(observed.response.rawHtml, rawHtml);
    assert.equal(calls, 2);
  });
});
