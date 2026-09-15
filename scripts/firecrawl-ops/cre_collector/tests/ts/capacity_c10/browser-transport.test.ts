import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  BrowserTransport,
  C10ReceiptError,
  C10_REVIEWED_BROWSER_ENGINE,
  SourceBoundOneShotTransport,
  allowlistedCards,
  canonicalSha256,
  sha256,
  type BrowserExecutionInstruction,
  type CoordinatorArmGate,
  type CoordinatorArmToken,
  type InternalBrowserExecutor,
  type ReceiptBinding,
  type RequestCard,
  type TransportResponse,
} from "../../../capacity_c10/receipts/index.js";
import { MemoryReceiptStore } from "./receipt_test_store.js";

const binding: ReceiptBinding = Object.freeze({
  planSha256: "a".repeat(64), cohortSha256: "b".repeat(64), policySha256: "c".repeat(64),
  sourceSha256: "d".repeat(64), armSha256: "e".repeat(64), implementationSha256: "f".repeat(64),
});

const token: CoordinatorArmToken = Object.freeze({
  sourceKey: "browser-source", armSha256: binding.armSha256, tokenId: "coordinator-arm-1", tokenSha256: "9".repeat(64),
});

function cards() {
  return allowlistedCards("browser-source", [{
    id: "browser-enumeration", sourceKey: "browser-source", stage: "enumeration", method: "POST",
    url: "https://example.test/api/listings", allowedHost: "example.test",
    headers: { accept: "application/json", "content-type": "application/json" }, contentType: "application/json",
    body: '{"page":1}', cacheMode: "no-store", timeoutMs: 1_000, maxBytes: 1_024,
  }]);
}

function browserResponse(card: Readonly<RequestCard>, overrides: Partial<TransportResponse> = {}): TransportResponse {
  const body = Buffer.from('{"items":[]}');
  const response = {
    status: 200, finalUrl: card.url, redirectCount: 0, elapsedMs: 12, challengeDetected: false, body,
    contentType: "application/json", providerAttempts: 1, cacheMode: "no-store" as const,
  };
  return {
    ...response,
    trustedBrowserEvidence: {
      schemaVersion: 1 as const,
      kind: "cre_capacity_c10_browser_execution_evidence" as const,
      engine: C10_REVIEWED_BROWSER_ENGINE,
      engineAttempts: 1 as const,
      fallbackDisabled: true as const,
      fallbackUsed: false as const,
      pageLease: { leaseId: "lease-17", slot: 2 },
      apiJobId: "job-81",
      coordinatorArmTokenSha256: token.tokenSha256,
      requestSha256: canonicalSha256(card),
      requestBodySha256: card.bodySha256,
      rawResponseSha256: sha256(body),
      cacheRead: false as const,
      cacheWrite: false as const,
      queueMs: 3,
      source: { status: response.status, finalUrl: response.finalUrl, contentType: response.contentType, proxy: { mode: "isolated", proxyId: "proxy-1", country: "US" } },
    },
    ...overrides,
  };
}

class FakeArmGate implements CoordinatorArmGate {
  readonly consumptions: { readonly token: CoordinatorArmToken; readonly cardSha256: string }[] = [];
  async consume(armToken: Readonly<CoordinatorArmToken>, cardSha256: string): Promise<void> {
    this.consumptions.push({ token: armToken, cardSha256 });
  }
}

class FakeBrowserExecutor implements InternalBrowserExecutor {
  readonly instructions: BrowserExecutionInstruction[] = [];
  constructor(private readonly result: (card: Readonly<RequestCard>) => TransportResponse) {}
  async execute(instruction: Readonly<BrowserExecutionInstruction>): Promise<TransportResponse> {
    this.instructions.push(instruction);
    return this.result(instruction.card);
  }
}

function subject(result = browserResponse) {
  const allowlisted = cards();
  const gate = new FakeArmGate();
  const executor = new FakeBrowserExecutor(result);
  const browser = new BrowserTransport("browser-source", binding, allowlisted, token, gate, executor);
  const store = new MemoryReceiptStore();
  const receiptTransport = new SourceBoundOneShotTransport("browser-source", binding, allowlisted, store, browser);
  return { allowlisted, browser, executor, gate, receiptTransport, store } as const;
}

test("browser transport submits one armed card to one reviewed engine with fallback and cache disabled", async () => {
  const context = subject();
  const event = await context.receiptTransport.oneShot("browser-enumeration", (view) => ({ providerId: "native-1", finalUrl: view.finalUrl }));
  assert.equal(context.executor.instructions.length, 1);
  assert.equal(context.gate.consumptions.length, 1);
  const instruction = context.executor.instructions[0]!;
  assert.equal(instruction.engine, C10_REVIEWED_BROWSER_ENGINE);
  assert.equal(instruction.engineAttempts, 1);
  assert.equal(instruction.fallbackDisabled, true);
  assert.equal(instruction.cacheRead, false);
  assert.equal(instruction.cacheWrite, false);
  assert.equal(instruction.cardSha256, canonicalSha256(instruction.card));
  assert.equal(context.receiptTransport.requestAccounting().attempts, 1);
  assert.equal(context.receiptTransport.requestAccounting().retries, 0);
  const privateEvent = context.store.jsonFor(event.privateEventSha256);
  const evidence = (privateEvent.response as Record<string, unknown>).trustedBrowserEvidence as Record<string, unknown>;
  assert.equal(evidence.engine, C10_REVIEWED_BROWSER_ENGINE);
  assert.equal(evidence.engineAttempts, 1);
  assert.equal(evidence.requestBodySha256, instruction.card.bodySha256);
  assert.equal(evidence.cacheRead, false);
  assert.equal(evidence.cacheWrite, false);
  assert.equal((evidence.source as Record<string, unknown>).status, 200);
});

test("browser transport fails closed for missing or contradictory browser evidence without a retry", async () => {
  const missingDirect = subject((card) => {
    const response = browserResponse(card);
    return { ...response, trustedBrowserEvidence: undefined };
  });
  await assert.rejects(
    missingDirect.browser.execute(missingDirect.allowlisted.get("browser-enumeration")!),
    /trusted browser evidence is unavailable/,
  );
  assert.equal(missingDirect.executor.instructions.length, 1);

  const missing = subject((card) => {
    const response = browserResponse(card);
    return { ...response, trustedBrowserEvidence: undefined };
  });
  await assert.rejects(missing.receiptTransport.oneShot("browser-enumeration", () => ({ ok: true })), /source-bound request failed without retry/);
  assert.equal(missing.executor.instructions.length, 1);
  assert.equal(missing.receiptTransport.requestAccounting().events[0]?.outcome, "transport_error");
  await assert.rejects(missing.receiptTransport.oneShot("browser-enumeration", () => ({ ok: true })), /already consumed/);

  const fallback = subject((card) => {
    const response = browserResponse(card);
    return {
      ...response,
      trustedBrowserEvidence: { ...response.trustedBrowserEvidence!, fallbackUsed: true },
    } as unknown as TransportResponse;
  });
  await assert.rejects(fallback.browser.execute(fallback.allowlisted.get("browser-enumeration")!), /does not prove/);
  assert.equal(fallback.executor.instructions.length, 1);
});

test("browser transport rejects unlisted cards and a mismatched coordinator arm before browser execution", async () => {
  const context = subject();
  const allowed = context.allowlisted.get("browser-enumeration")!;
  await assert.rejects(context.browser.execute({ ...allowed, id: "not-declared" }), /predeclared allowlisted/);
  assert.equal(context.gate.consumptions.length, 0);
  assert.equal(context.executor.instructions.length, 0);
  assert.throws(
    () => new BrowserTransport("browser-source", binding, context.allowlisted, { ...token, armSha256: "0".repeat(64) }, context.gate, context.executor),
    /not bound/,
  );
});

test("browser substrate has no network client or public controller surface", async () => {
  const source = await readFile(new URL("../../../capacity_c10/receipts/browser_transport.ts", import.meta.url), "utf8");
  for (const forbidden of ["fetch(", "http://", "https://", "axios", "controllers/"]) {
    assert.equal(source.includes(forbidden), false, `browser substrate must not expose ${forbidden}`);
  }
});
