import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import test from "node:test";

import {
  C10ReceiptError,
  canonicalJson,
  jllBrowserCardRegistry,
  jllBrowserCohortSha256,
  runJllBrowserFidelitySmoke,
  runJllBrowserSaturationCalibration,
  withEphemeralLocalC10Sidecar,
  type JllBrowserCohort,
  type LocalBrowserFetch,
  type ReceiptBinding,
} from "../../../capacity_c10/receipts/index.js";
import { MemoryReceiptStore } from "./receipt_test_store.js";

const binding: ReceiptBinding = Object.freeze({
  planSha256: "a".repeat(64), cohortSha256: "b".repeat(64), policySha256: "c".repeat(64),
  sourceSha256: "d".repeat(64), armSha256: "e".repeat(64), implementationSha256: "f".repeat(64),
});
const secret = "jll-browser-test-secret-material-that-is-more-than-thirty-two-bytes";
const sign = (value: object) => createHmac("sha256", secret).update(["cre-capacity-c10-browser-evidence-v1", canonicalJson(value)].join("\u0000"), "utf8").digest("hex");

function cohort(): JllBrowserCohort {
  const value = {
    sourceKey: "jll" as const, transaction: "sale" as const, propertyType: "office", page: 1,
    members: Array.from({ length: 16 }, (_, index) => ({
      key: `jll-${index + 1}`, providerId: String(index + 1),
      canonicalUrl: `https://property.jll.com/listings/office-${index + 1}`,
    })),
  };
  return { ...value, cohortMemberSha256: jllBrowserCohortSha256(value) };
}

function response(card: { id: string; url: string }) {
  if (card.id === "jll-enumeration") {
    return JSON.stringify({ data: { properties: { count: 16, items: Array.from({ length: 16 }, (_, index) => ({
      id: String(index + 1), title: `Office ${index + 1}`, images: [], address: "1 Main", propertyTypes: ["office"],
      tenureTypes: ["sale"], pageUrl: `/listings/office-${index + 1}`, surfaceAreas: [],
    })) } } });
  }
  const id = card.id.replace("jll-member-", "");
  return `<script id="__NEXT_DATA__">${JSON.stringify({ props: { pageProps: { property: {
    id: String(Number(id) + 1), pageUrl: card.url, images: ["https://images.jll.test/a.jpg"],
  } } } })}</script>`;
}

function fetcher(delayMs = 0): LocalBrowserFetch {
  return async (_url, init) => {
    const card = (JSON.parse(init.body) as { card: { id: string; url: string } }).card;
    if (delayMs) await new Promise((resolve) => setTimeout(resolve, delayMs));
    const body = Buffer.from(response(card));
    return {
      ok: true, status: 200, text: async () => "",
      json: async () => {
        const evidence = {
        status: 200, finalUrl: card.url, redirectCount: 0, elapsedMs: 1, challengeDetected: false,
        contentType: card.id === "jll-enumeration" ? "application/json" : "text/html",
        bodyBase64: body.toString("base64"), jobId: `job-${card.id}`,
        pageLease: { leaseId: `lease-${card.id}`, slot: 0 }, queueMs: 0,
        proxy: { mode: "direct", proxyId: null, country: null }, engine: "playwright-service", engineAttempts: 1,
          fallbackDisabled: true, fallbackUsed: false, cacheRead: false, cacheWrite: false,
        };
        return { ...evidence, evidenceSignature: sign(evidence) };
      },
    };
  };
}

const locked = Object.freeze({ assertHeld() {} });
const options = (delayMs = 0) => ({
  armSecret: secret,
  serviceUrl: "http://127.0.0.1:3003", store: new MemoryReceiptStore(), fetcher: fetcher(delayMs), coordinatorLock: locked,
});

test("JLL browser registry freezes exact cards and one-member browser fidelity evidence", async () => {
  const selected = cohort();
  const cards = jllBrowserCardRegistry(selected);
  assert.equal(cards.size, 17);
  assert.equal(cards.get("jll-enumeration")?.browserBootstrapUrl, "https://property.jll.com/");
  assert.equal(cards.get("jll-member-0")?.bodySha256, null);
  const run = await runJllBrowserFidelitySmoke(selected, binding, "jll-1", options());
  assert.equal(run.members.length, 1);
  assert.equal(run.accounting.attempts, 2);
  assert.equal(run.accounting.retries, 0);
});

test("JLL browser rejects cohort/card drift and missing coordinator lock", async () => {
  const selected = cohort();
  const drift = { ...selected, members: [...selected.members.slice(0, 15), { ...selected.members[15]!, canonicalUrl: "https://other.test/x" }] };
  assert.throws(() => jllBrowserCardRegistry(drift), /immutable reviewed/);
  await assert.rejects(
    () => runJllBrowserFidelitySmoke(selected, binding, "jll-1", { ...options(), coordinatorLock: { assertHeld() { throw new C10ReceiptError("lock absent"); } } }),
    /lock absent/,
  );
});

test("JLL browser calibration makes sixteen one-attempt no-cache requests at the reviewed saturation", async () => {
  const run = await runJllBrowserSaturationCalibration(cohort(), binding, 4, options(2));
  assert.deepEqual(run.scheduler, { configuredConcurrency: 4, observedMaxActive: 4, scheduledMemberCount: 16 });
  assert.equal(run.members.length, 16);
  assert.equal(run.accounting.attempts, 17);
  assert.equal(run.accounting.retries, 0);
});

test("ephemeral local preflight cleans secrets after health failure and success", async () => {
  const environment: Record<string, string | undefined> = { KEEP: "yes" };
  let stopped = 0;
  const lifecycle = { async start(sidecarEnvironment: Readonly<Record<string, string>>) {
    assert.match(sidecarEnvironment.C10_BROWSER_INTERNAL_SECRET ?? "", /^[0-9a-f]{96}$/);
    return { async stop() { stopped += 1; } };
  } };
  const value = await withEphemeralLocalC10Sidecar({ serviceUrl: "http://127.0.0.1:3003", lifecycle, healthCheck: async () => true, receiptStoreAvailable: () => true, environment }, async (secret) => {
    assert.equal(environment.C10_BROWSER_ARM_SECRET, secret);
    assert.equal(environment.C10_BROWSER_INTERNAL_SECRET, secret);
    return "ok";
  });
  assert.equal(value, "ok");
  assert.equal(stopped, 1);
  assert.deepEqual(environment, { KEEP: "yes" });
  await assert.rejects(() => withEphemeralLocalC10Sidecar({ serviceUrl: "http://127.0.0.1:3003", lifecycle, healthCheck: async () => false, receiptStoreAvailable: () => true, environment }, async () => "never"), /health/);
  assert.deepEqual(environment, { KEEP: "yes" });
});
