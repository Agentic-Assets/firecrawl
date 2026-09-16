import assert from "node:assert/strict";
import test from "node:test";

import type { Page } from "playwright";

import { executeC10BrowserPageFetch } from "./c10_browser_execution";
import type { C10SidecarCard } from "./c10_browser_internal";

const card: C10SidecarCard = {
  id: "card-1",
  sourceKey: "source-1",
  stage: "enumeration",
  method: "GET",
  url: "https://fixture.test/api/list",
  allowedHost: "fixture.test",
  headers: {},
  contentType: null,
  body: null,
  browserBootstrapUrl: "https://fixture.test/",
  cacheMode: "no-store",
  timeoutMs: 1_000,
  maxBytes: 1_024,
  bodySha256: null,
  expectedMemberRoutes: Array.from(
    { length: 16 },
    (_, index) => `https://fixture.test/listings/member-${index + 1}`,
  ),
};

test("C10 executes one reviewed page operation with cache disabled", async () => {
  let headers = 0;
  let bootstrap = 0;
  let browserOperation = 0;
  const page = {
    async setExtraHTTPHeaders(value: Record<string, string>) {
      headers += 1;
      assert.equal(value["cache-control"], "no-store, no-cache, max-age=0");
      assert.equal(value.pragma, "no-cache");
    },
    async goto(url: string) {
      bootstrap += 1;
      assert.equal(url, card.browserBootstrapUrl);
      return { status: () => 200 };
    },
    url: () => card.browserBootstrapUrl,
    async evaluate() {
      browserOperation += 1;
      return {
        status: 200,
        finalUrl: card.url,
        redirected: false,
        contentType: "application/json",
        bodyBase64: Buffer.from('{"items":[]}').toString("base64"),
      };
    },
  } as unknown as Page;

  const response = await executeC10BrowserPageFetch(page, card);
  assert.equal(headers, 1);
  assert.equal(bootstrap, 1);
  assert.equal(browserOperation, 1);
  assert.equal(response.status, 200);
  assert.equal(response.finalUrl, card.url);
});

test("C10 rejects a browser operation that leaves the reviewed host", async () => {
  const page = {
    async setExtraHTTPHeaders() {},
    async goto() { return { status: () => 200 }; },
    url: () => card.browserBootstrapUrl,
    async evaluate() {
      return {
        status: 200,
        finalUrl: "https://unreviewed.test/api/list",
        redirected: true,
        contentType: "application/json",
        bodyBase64: "",
      };
    },
  } as unknown as Page;
  await assert.rejects(() => executeC10BrowserPageFetch(page, card), /reviewed origin/);
});
