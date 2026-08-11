/**
 * Minimal unit test for v2 scrape (no mocking; sanity check payload path)
 */
import { FirecrawlClient } from "../../../v2/client";

describe("v2.scrape unit", () => {
  test("constructor permits an empty key for the cloud keyless tier", () => {
    expect(() => new FirecrawlClient({ apiKey: "", apiUrl: "https://api.firecrawl.dev" })).not.toThrow();
  });
});
