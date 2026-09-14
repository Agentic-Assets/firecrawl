// Isolate argv before scrape.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  configuredScrapeMaxAttempts,
  createScrapeClient,
  firecrawl,
  parseJsonBody,
  repairUnescapedJsonStringQuotes,
  scrapeRetryDelayMs,
  scrapeRaw,
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
    it(`bounds ${name} SDK HTTP attempts even when the gateway returns 502`, async () => {
      const client = createScrapeClient(environment);
      // Stub only the pinned SDK's HTTP boundary; its real retry loop still runs.
      const transport = (client as unknown as {
        http: {
          instance: { request: () => Promise<never> };
          sleep: () => Promise<void>;
        };
      }).http;
      const failure = Object.assign(new Error("synthetic gateway failure"), {
        response: { status: 502 },
      });
      let attempts = 0;
      transport.instance.request = async () => {
        attempts += 1;
        throw failure;
      };
      transport.sleep = async () => undefined;

      await assert.rejects(
        () => client.scrape("https://example.com/sdk-attempt-bound"),
        /synthetic gateway failure/
      );
      assert.equal(attempts, expectedAttempts);
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
