import assert from "node:assert/strict";
import test from "node:test";

import { isC10SuccessfulBrowserResponse } from "./c10_browser_listener";
import type { C10BrowserPageResponse } from "./c10_browser_execution";
import type { C10SidecarCard } from "./c10_browser_internal";

const enumerationRoutes = Array.from(
  { length: 16 },
  (_, index) => `https://www.us.jll.com/properties/example-${index + 1}`,
);

const enumerationCard: C10SidecarCard = {
  id: "enumeration",
  sourceKey: "jll",
  stage: "enumeration",
  method: "POST",
  url: "https://www.us.jll.com/api/graphql",
  allowedHost: "www.us.jll.com",
  headers: { accept: "application/json" },
  contentType: "application/json",
  body: "{}",
  browserBootstrapUrl: "https://www.us.jll.com/",
  cacheMode: "no-store",
  timeoutMs: 1_000,
  maxBytes: 1_024,
  bodySha256: "a".repeat(64),
  expectedMemberRoutes: enumerationRoutes,
};

const memberCard: C10SidecarCard = {
  ...enumerationCard,
  id: "member",
  stage: "member",
  method: "GET",
  url: "https://www.us.jll.com/properties/example",
  headers: { accept: "text/html" },
  contentType: null,
  body: null,
  bodySha256: null,
};

function response(
  changes: Partial<C10BrowserPageResponse> = {},
): C10BrowserPageResponse {
  return {
    status: 200,
    finalUrl: enumerationCard.url,
    redirected: false,
    contentType: "application/json; charset=utf-8",
    bodyBase64: Buffer.from(JSON.stringify({
      data: { properties: { items: enumerationRoutes.map((pageUrl) => ({ pageUrl })) } },
    })).toString("base64"),
    ...changes,
  };
}

test("C10 signs only reviewed success responses", () => {
  assert.equal(
    isC10SuccessfulBrowserResponse(enumerationCard, response(), false),
    true,
  );
  assert.equal(
    isC10SuccessfulBrowserResponse(
      memberCard,
      response({ finalUrl: memberCard.url, contentType: "text/html; charset=utf-8" }),
      false,
    ),
    true,
  );
});

test("C10 enumeration success requires current sealed membership, not a 2xx JSON transport", () => {
  for (const body of [
    { errors: [{ message: "upstream failure" }] },
    { data: { properties: { items: [] } } },
    { data: { properties: { items: [{ pageUrl: "https://www.us.jll.com/properties/other" }] } } },
  ]) {
    assert.equal(
      isC10SuccessfulBrowserResponse(
        enumerationCard,
        response({ bodyBase64: Buffer.from(JSON.stringify(body)).toString("base64") }),
        false,
      ),
      false,
    );
  }
  assert.equal(
    isC10SuccessfulBrowserResponse(
      enumerationCard,
      response({ bodyBase64: Buffer.from("not-json").toString("base64") }),
      false,
    ),
    false,
  );
});

test("C10 success gate rejects non-success, route, representation, and challenge failures", () => {
  assert.equal(
    isC10SuccessfulBrowserResponse(
      enumerationCard,
      response({ status: 302 }),
      false,
    ),
    false,
  );
  assert.equal(
    isC10SuccessfulBrowserResponse(
      enumerationCard,
      response({ finalUrl: "https://www.us.jll.com/login" }),
      false,
    ),
    false,
  );
  assert.equal(
    isC10SuccessfulBrowserResponse(
      enumerationCard,
      response({ contentType: "text/html" }),
      false,
    ),
    false,
  );
  assert.equal(
    isC10SuccessfulBrowserResponse(enumerationCard, response(), true),
    false,
  );
});
