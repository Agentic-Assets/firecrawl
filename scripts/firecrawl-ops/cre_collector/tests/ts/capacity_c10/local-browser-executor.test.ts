import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { createServer } from "node:http";
import test from "node:test";

import {
  SourceBoundOneShotTransport,
  allowlistedCards,
  createLocalC10BrowserTransport,
  canonicalJson,
  type ReceiptBinding,
} from "../../../capacity_c10/receipts/index.js";
import { MemoryReceiptStore } from "./receipt_test_store.js";

const binding: ReceiptBinding = Object.freeze({
  planSha256: "a".repeat(64), cohortSha256: "b".repeat(64), policySha256: "c".repeat(64),
  sourceSha256: "d".repeat(64), armSha256: "e".repeat(64), implementationSha256: "f".repeat(64),
});
const secret = "local-c10-test-secret-material-that-is-more-than-thirty-two-bytes";
const sign = (value: object) => createHmac("sha256", secret).update(["cre-capacity-c10-browser-evidence-v1", canonicalJson(value)].join("\u0000"), "utf8").digest("hex");

function cards() {
  return allowlistedCards("local-browser", [{
    id: "local-enumeration", sourceKey: "local-browser", stage: "enumeration", method: "POST",
    url: "https://example.test/api/listings", allowedHost: "example.test",
    headers: { accept: "application/json", "content-type": "application/json" }, contentType: "application/json",
    body: '{"page":1}', browserBootstrapUrl: "https://example.test/", cacheMode: "no-store", timeoutMs: 1_000, maxBytes: 1_024,
  }]);
}

async function fixtureServer() {
  let calls = 0;
  let requestBody = "";
  let authorization = "";
  const server = createServer(async (request, response) => {
    calls++;
    authorization = String(request.headers["x-c10-browser-authorization"] ?? "");
    for await (const chunk of request) requestBody += chunk;
    const payload = JSON.parse(requestBody) as { card: { url: string } };
    const body = Buffer.from('{"items":[]}');
    response.setHeader("content-type", "application/json");
    const evidence = {
      status: 200, finalUrl: payload.card.url, redirectCount: 0, elapsedMs: 8, challengeDetected: false,
      contentType: "application/json", bodyBase64: body.toString("base64"), jobId: "sidecar-job-1",
      pageLease: { leaseId: "sidecar-lease-1", slot: 0 }, queueMs: 2,
      proxy: { mode: "direct", proxyId: null, country: null }, engine: "playwright-service", engineAttempts: 1,
      fallbackDisabled: true, fallbackUsed: false, cacheRead: false, cacheWrite: false,
    };
    response.end(JSON.stringify({ ...evidence, evidenceSignature: sign(evidence) }));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("fixture server did not bind TCP");
  return {
    serviceUrl: `http://127.0.0.1:${address.port}`,
    calls: () => calls,
    requestBody: () => requestBody,
    authorization: () => authorization,
    close: () => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}

test("local C10 executor makes exactly one loopback sidecar invocation and seals its real attestation", async () => {
  const fixture = await fixtureServer();
  try {
    const allowlisted = cards();
    const direct = createLocalC10BrowserTransport("local-browser", binding, allowlisted, {
      armSecret: secret,
      serviceUrl: fixture.serviceUrl,
    });
    const store = new MemoryReceiptStore();
    const transport = new SourceBoundOneShotTransport("local-browser", binding, allowlisted, store, direct);
    const event = await transport.oneShot("local-enumeration", (view) => ({ providerId: "row-1", finalUrl: view.finalUrl }));
    assert.equal(fixture.calls(), 1);
    assert.match(fixture.authorization(), /^[A-Za-z0-9_-]+\.[0-9a-f]{64}$/);
    assert.match(fixture.requestBody(), /"browserBootstrapUrl":"https:\/\/example\.test\/"/);
    assert.deepEqual(transport.requestAccounting(), {
      logicalRequests: 1, attempts: 1, retries: 0, eventsSha256: transport.requestAccounting().eventsSha256,
      events: transport.requestAccounting().events,
    });
    const sealed = store.jsonFor(event.privateEventSha256);
    const evidence = (sealed.response as Record<string, unknown>).trustedBrowserEvidence as Record<string, unknown>;
    assert.equal(evidence.apiJobId, "sidecar-job-1");
    assert.equal(evidence.engineAttempts, 1);
    assert.equal(evidence.cacheRead, false);
    assert.equal(evidence.cacheWrite, false);
    assert.equal((evidence.pageLease as Record<string, unknown>).slot, 0);
  } finally {
    await fixture.close();
  }
});

test("local C10 executor refuses a non-loopback sidecar before any external request", () => {
  assert.throws(() => createLocalC10BrowserTransport("local-browser", binding, cards(), {
    armSecret: secret,
    serviceUrl: "https://browser.example.test",
  }), /loopback-only/);
});
