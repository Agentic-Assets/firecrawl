// AGENTIC-4185: offline semantic comparison, strict capture binding unchanged.
import test from "node:test";
import assert from "node:assert/strict";
import * as matched from "../../../sources/matched-capacity.js";
import { renderedPairListingSha256 } from "../../../sources/jll-direct-pair-worker.js";
import type { CapturedDetail, MatchedCohort, MatchedMember } from "../../../sources/matched-capacity.js";

const schema = "cre-matched-semantic-v2";
const clock = new Date(Date.now() - 1000).toISOString();
const cohort: MatchedCohort = { source: "jll", inventorySha256: "a".repeat(64), inventoryStartedAt: clock,
  inventoryFinishedAt: clock, members: Array.from({ length: 8 }, (_, n) => ({ sampleId: `sample-${n}`,
    providerId: String(n), canonicalUrl: `https://property.jll.com/listings/synthetic-${n}`,
    baseRow: { id: String(n), url: `https://property.jll.com/listings/synthetic-${n}`, sourceKey: "jll", inventoryObservedAt: clock } })) };
const context = { runtimeSnapshot: { project: "synthetic-owned" }, resourceJournal: "synthetic-private.jsonl", concurrentHostWork: "unknown" as const };
function detail(member: MatchedMember, generation: string, rawLength = 517306): CapturedDetail & { brokers: unknown[] } {
  return { listing: { ...member.baseRow, name: "Synthetic property", salePriceUsd: 1250000,
    transactionType: "Sale", description: "Complete synthetic property narrative", markdown: "Complete property markdown",
    detailObservedAt: new Date().toISOString(), freshnessProvenance: { generationId: generation, cacheDisposition: "live", detailScope: "detail_page" },
    jllDetail: { propertyId: member.providerId, hidePrice: false, scrape: { rawHtmlLength: rawLength, markdownLength: 2214, linkCount: 17 } },
    photos: ["https://images.example/first.jpg", "https://images.example/second.jpg"],
    documents: [{ url: "https://docs.example/a.pdf" }, { url: "https://docs.example/b.pdf" }],
    media: [{ url: "https://media.example/a" }, { url: "https://media.example/b" }],
    contactsDetailed: [{ name: "Synthetic A" }, { name: "Synthetic B" }], brokerIds: [0, 1],
    jllInvestorDetail: { scrape: { rawHtmlLength: 0 } }, other: { rawHtmlLength: 100 } },
    brokers: [{ name: "Synthetic A" }, { name: "Synthetic B" }],
    client: { dispatches: 1, statuses: [200], failedDispatches: 0, redirected: false, maxAgeZero: true,
      responseUrlMatches: true, bodyIdentity: "jll_native_property" } };
}
const semantic = (value: CapturedDetail): string => matched.matchedSemanticListingSha256(value);
const options = (generationId: string, concurrency = 1) => ({ cohort, generationId, concurrency, context, signal: new AbortController().signal });

// The numeric variation is the exact one-character diagnostic difference in the saved trial.
test("semantic diagnostic variation keeps both captured listings and strict paired binding unchanged", () => {
  const a = detail(cohort.members[0]!, "first"); const b = detail(cohort.members[0]!, "second", 517305);
  const before = structuredClone([a, b]);
  assert.notEqual(matched.matchedListingSha256(a), matched.matchedListingSha256(b));
  assert.equal(semantic(a), semantic(b));
  assert.equal(matched.matchedListingSha256(a), renderedPairListingSha256(a));
  assert.equal(matched.matchedListingSha256(b), renderedPairListingSha256(b));
  assert.deepEqual([a, b], before);
});

test("real cell scheduler compares semantic hashes while preserving strict hashes for paired consumers", async () => {
  const first = await matched.runMatchedCapacityCell({ ...options("first"), capture: async member => detail(member, "first") });
  const second = await matched.runMatchedCapacityCell({ ...options("second", 2), baseline: first, capture: async member => {
    await new Promise(resolve => setTimeout(resolve, 1)); return detail(member, "second", 517305);
  } });
  assert.equal(first.fidelitySchema, schema); assert.equal(second.fidelitySchema, schema);
  assert.equal(second.peakActiveEnrichments, 2);
  for (let n = 0; n < cohort.members.length; n++) {
    assert.notEqual(first.rows[n]!.fidelitySha256, second.rows[n]!.fidelitySha256);
    assert.equal(first.rows[n]!.semanticFidelitySha256, second.rows[n]!.semanticFidelitySha256);
    assert.equal(second.rows[n]!.fidelitySha256, renderedPairListingSha256(detail(cohort.members[n]!, "second", 517305)));
  }
});

for (const [label, mutate] of [
  ["price", (r: any) => r.salePriceUsd++], ["withholding", (r: any) => r.jllDetail.hidePrice = true],
  ["description", (r: any) => r.description += " changed"], ["markdown", (r: any) => r.markdown += " changed"],
  ["photo order", (r: any) => r.photos.reverse()], ["document order", (r: any) => r.documents.reverse()],
  ["media order", (r: any) => r.media.reverse()], ["contact order", (r: any) => r.contactsDetailed.reverse()],
  ["broker reference order", (r: any) => r.brokerIds.reverse()], ["markdown length", (r: any) => r.jllDetail.scrape.markdownLength++],
  ["link count", (r: any) => r.jllDetail.scrape.linkCount++], ["JLLInvestor gate", (r: any) => r.jllInvestorDetail.scrape.rawHtmlLength++],
  ["unrelated rawHtmlLength", (r: any) => r.other.rawHtmlLength++], ["native property metadata", (r: any) => r.jllDetail.propertyId = "different"],
] as const) {
  test(`semantic comparison still rejects ${label}`, async () => {
    const first = await matched.runMatchedCapacityCell({ ...options("first"), capture: async member => detail(member, "first") });
    await assert.rejects(matched.runMatchedCapacityCell({ ...options("second", 2), baseline: first, capture: async member => {
      const value = detail(member, "second", 517305); mutate(value.listing); return value;
    } }), /fidelity_regression/);
  });
}

for (const [label, mutate] of [
  ["legacy missing schema", (b: any) => delete b.fidelitySchema], ["unknown schema", (b: any) => b.fidelitySchema = "future"],
  ["missing semantic digest", (b: any) => delete b.rows[0].semanticFidelitySha256],
  ["invalid semantic digest", (b: any) => b.rows[0].semanticFidelitySha256 = "not-sha256"],
  ["missing strict digest", (b: any) => delete b.rows[0].fidelitySha256],
  ["invalid strict digest", (b: any) => b.rows[0].fidelitySha256 = "not-sha256"],
  ["incomplete cohort", (b: any) => b.rows.pop()],
  ["wrong cohort binding", (b: any) => b.cohortSha256 = "b".repeat(64)],
  ["duplicate sample", (b: any) => b.rows[0].sampleId = b.rows[1].sampleId],
  ["unknown sample", (b: any) => b.rows[0].sampleId = "outside-cohort"],
  ["wrong source", (b: any) => b.source = "marcus-millichap"],
] as const) {
  test(`baseline ${label} refuses before any capture`, async () => {
    const baseline = await matched.runMatchedCapacityCell({ ...options("first"), capture: async member => detail(member, "first") });
    mutate(baseline); let calls = 0;
    await assert.rejects(matched.runMatchedCapacityCell({ ...options("second", 2), baseline, capture: async member => {
      calls++; return detail(member, "second");
    } }), /baseline_binding/);
    assert.equal(calls, 0);
  });
}