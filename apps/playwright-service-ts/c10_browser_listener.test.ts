import assert from "node:assert/strict";
import test from "node:test";

import {
  assertC10BrowserListenerConfiguration,
  hasC10AdmissionEnumerationCandidates,
  isC10SuccessfulBrowserResponse,
  readC10BrowserListenerConfig,
} from "./c10_browser_listener";
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

test("readC10BrowserListenerConfig/assertC10BrowserListenerConfiguration: admission lane unset is null, named lane round-trips, any other value is invalid and throws", () => {
  const unset = readC10BrowserListenerConfig({});
  assert.equal(unset.admissionLane, null);
  assert.equal(unset.admissionLaneValid, true);
  assert.doesNotThrow(() => assertC10BrowserListenerConfiguration(unset));

  const named = readC10BrowserListenerConfig({
    C10_ADMISSION_LANE: "jll-canonical-url-lexicographic-v1",
  });
  assert.equal(named.admissionLane, "jll-canonical-url-lexicographic-v1");
  assert.equal(named.admissionLaneValid, true);
  assert.doesNotThrow(() => assertC10BrowserListenerConfiguration(named));

  const invalid = readC10BrowserListenerConfig({ C10_ADMISSION_LANE: "other" });
  assert.equal(invalid.admissionLane, null);
  assert.equal(invalid.admissionLaneValid, false);
  assert.throws(
    () => assertC10BrowserListenerConfiguration(invalid),
    /C10_ADMISSION_LANE is not a reviewed admission lane/,
  );
});

test("hasC10AdmissionEnumerationCandidates accepts >=16 candidates and rejects 15, an errors envelope, or non-JSON", () => {
  const bodyWith = (count: number) =>
    Buffer.from(
      JSON.stringify({
        data: {
          properties: {
            items: Array.from({ length: count }, (_, index) => ({
              pageUrl: `https://www.us.jll.com/properties/candidate-${index + 1}`,
            })),
          },
        },
      }),
    ).toString("base64");
  assert.equal(hasC10AdmissionEnumerationCandidates(bodyWith(16)), true);
  assert.equal(hasC10AdmissionEnumerationCandidates(bodyWith(15)), false);
  assert.equal(
    hasC10AdmissionEnumerationCandidates(
      Buffer.from(JSON.stringify({ errors: [{ message: "upstream failure" }] })).toString(
        "base64",
      ),
    ),
    false,
  );
  assert.equal(
    hasC10AdmissionEnumerationCandidates(Buffer.from("not-json").toString("base64")),
    false,
  );
});

test("C10 admission enumeration success accepts >=16 candidates while a strict card still requires sealed membership", () => {
  const admissionEnumerationCard: C10SidecarCard = {
    ...enumerationCard,
    id: "admission-enumeration",
    expectedMemberRoutes: null,
  };
  const candidateBody = (count: number) =>
    Buffer.from(
      JSON.stringify({
        data: {
          properties: {
            items: Array.from({ length: count }, (_, index) => ({
              pageUrl: `https://www.us.jll.com/properties/candidate-${index + 1}`,
            })),
          },
        },
      }),
    ).toString("base64");

  assert.equal(
    isC10SuccessfulBrowserResponse(
      admissionEnumerationCard,
      response({ bodyBase64: candidateBody(16) }),
      false,
    ),
    true,
  );
  assert.equal(
    isC10SuccessfulBrowserResponse(
      admissionEnumerationCard,
      response({ bodyBase64: candidateBody(15) }),
      false,
    ),
    false,
  );
  // The same sixteen non-sealed candidates do not satisfy the strict card,
  // which still requires membership in its sealed expectedMemberRoutes.
  assert.equal(
    isC10SuccessfulBrowserResponse(
      enumerationCard,
      response({ bodyBase64: candidateBody(16) }),
      false,
    ),
    false,
  );
});
