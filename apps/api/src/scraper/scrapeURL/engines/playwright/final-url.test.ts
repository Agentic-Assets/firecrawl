import { describe, expect, it, vi } from "vitest";
import type { Meta } from "../..";
import { scrapeURLWithPlaywright } from ".";

const mocks = vi.hoisted(() => ({ robustFetch: vi.fn() }));

vi.mock("../../../../config", () => ({
  config: { PLAYWRIGHT_MICROSERVICE_URL: "http://sidecar.example/scrape" },
}));
vi.mock("../../lib/fetch", () => ({ robustFetch: mocks.robustFetch }));
vi.mock("@mendable/firecrawl-rs", () => ({
  getInnerJson: () => {
    throw new Error("HTML fixtures must not enter JSON conversion");
  },
}));

describe("Playwright final URL compatibility", () => {
  // Real schema parsing catches removing url from the schema as well as
  // making it mandatory, without calling a sidecar or native conversion.
  it.each([
    {
      name: "promotes an HTTP response URL over the request",
      reportedUrl: "http://property.example/landed",
      rewrittenUrl: undefined,
      expectedUrl: "http://property.example/landed",
    },
    {
      name: "promotes an HTTPS response URL over the rewritten request",
      reportedUrl: "https://property.example/landed",
      rewrittenUrl: "https://property.example/rewritten",
      expectedUrl: "https://property.example/landed",
    },
    {
      name: "falls back to the request for about:blank",
      reportedUrl: "about:blank",
      rewrittenUrl: undefined,
      expectedUrl: "https://property.example/request",
    },
    {
      name: "falls back to the rewritten request for about:blank",
      reportedUrl: "about:blank",
      rewrittenUrl: "https://property.example/rewritten",
      expectedUrl: "https://property.example/rewritten",
    },
    {
      name: "falls back to the request for a malformed URL",
      reportedUrl: "not a URL",
      rewrittenUrl: undefined,
      expectedUrl: "https://property.example/request",
    },
    {
      name: "falls back to the rewritten request for a malformed URL",
      reportedUrl: "not a URL",
      rewrittenUrl: "https://property.example/rewritten",
      expectedUrl: "https://property.example/rewritten",
    },
    {
      name: "accepts an older sidecar without url and uses the request",
      reportedUrl: undefined,
      rewrittenUrl: undefined,
      expectedUrl: "https://property.example/request",
    },
    {
      name: "accepts an older sidecar without url and uses the rewritten request",
      reportedUrl: undefined,
      rewrittenUrl: "https://property.example/rewritten",
      expectedUrl: "https://property.example/rewritten",
    },
  ])("$name", async ({ reportedUrl, rewrittenUrl, expectedUrl }) => {
    const rawFixture = {
      content: "<html>property</html>",
      pageStatusCode: 200,
      contentType: "text/html",
      ...(reportedUrl === undefined ? {} : { url: reportedUrl }),
    };
    mocks.robustFetch.mockImplementation(
      async ({ schema }: { schema: { parse(value: unknown): unknown } }) =>
        schema.parse(rawFixture),
    );
    // Only the fields read by this engine are needed at this boundary.
    const meta = {
      url: "https://property.example/request",
      rewrittenUrl,
      options: { skipTlsVerification: false },
      logger: { child: () => ({}) },
      abort: {
        scrapeTimeout: () => undefined,
        asSignal: () => undefined,
      },
      mock: null,
    } as unknown as Meta;

    expect(await scrapeURLWithPlaywright(meta)).toEqual({
      url: expectedUrl,
      html: "<html>property</html>",
      statusCode: 200,
      error: undefined,
      contentType: "text/html",
      proxyUsed: "basic",
    });
  });
});
