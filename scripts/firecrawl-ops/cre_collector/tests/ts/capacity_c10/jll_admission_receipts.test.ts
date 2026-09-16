import assert from "node:assert/strict";
import test from "node:test";

import {
  C10ReceiptError,
  RecordingReceiptStore,
  SourceBoundOneShotTransport,
  allowlistedCards,
  type DirectProviderTransport,
  type ReceiptBinding,
  type RequestCard,
  type TransportResponse,
} from "../../../capacity_c10/receipts/index.js";
import { collectJllAdmissionReceipts, sealJllAdmissionManifest } from "../../../capacity_c10/receipts/strict_detail/jll_admission.js";
import { jllEnumerationCard } from "../../../capacity_c10/receipts/strict_detail/jll.js";
import { MemoryReceiptStore } from "./receipt_test_store.js";

const binding: ReceiptBinding = {
  planSha256: "a".repeat(64), cohortSha256: "b".repeat(64), policySha256: "c".repeat(64),
  sourceSha256: "d".repeat(64), armSha256: "e".repeat(64), implementationSha256: "f".repeat(64),
};

class FixtureTransport implements DirectProviderTransport {
  async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    const index = Number(card.url.split("member-").at(-1));
    const body = card.id === "jll-enumeration-0"
      ? JSON.stringify({ data: { properties: { count: 16, items: Array.from({ length: 16 }, (_, i) => ({ id: String(i + 1), pageUrl: `/listings/member-${i + 1}` })) } } })
      : `<script id="__NEXT_DATA__">${JSON.stringify({ props: { pageProps: { property: { id: String(index), pageUrl: `https://property.jll.com/listings/member-${index}`, images: [] } } } })}</script>`;
    return { status: 200, finalUrl: card.url, redirectCount: 0, elapsedMs: 1, challengeDetected: false, body: Buffer.from(body), contentType: card.id === "jll-enumeration-0" ? "application/json" : "text/html", providerAttempts: 1, cacheMode: "no-store" };
  }
}

function members(count = 16) {
  return Array.from({ length: count }, (_, index) => ({ key: `jll-${index + 1}`, providerId: String(index + 1), canonicalUrl: `https://property.jll.com/listings/member-${index + 1}` }));
}

test("JLL admission collector drives one fixed card plus exactly sixteen members", async () => {
  const memory = new MemoryReceiptStore();
  const store = new RecordingReceiptStore(memory);
  const transport = new SourceBoundOneShotTransport("jll", binding, allowlistedCards("jll", [jllEnumerationCard({ transaction: "sale", propertyType: "office", page: 1 })]), store, new FixtureTransport());
  const result = await collectJllAdmissionReceipts({ transport });
  assert.equal(result.memberReceipts.length, 16);
  assert.equal(result.enumeration.stage, "enumeration");
  assert.match(result.receiptSetSha256, /^[a-f0-9]{64}$/);
  assert.ok(result.artifacts.length >= 17);
  const manifest = await sealJllAdmissionManifest({ transport }, "/private/receipt-root", "d".repeat(64), result);
  const value = memory.jsonFor(manifest.sha256) as { member_receipts: unknown[]; artifacts: unknown[] };
  assert.equal(value.member_receipts.length, 16);
  assert.ok(value.artifacts.length >= 17);
});

test("JLL admission collector ignores controller-selected members", async () => {
  const store = new RecordingReceiptStore(new MemoryReceiptStore());
  const transport = new SourceBoundOneShotTransport("jll", binding, allowlistedCards("jll", [jllEnumerationCard({ transaction: "sale", propertyType: "office", page: 1 })]), store, new FixtureTransport());
  const result = await collectJllAdmissionReceipts({ transport });
  assert.equal(result.members[0]?.canonicalUrl, "https://property.jll.com/listings/member-1");
});
