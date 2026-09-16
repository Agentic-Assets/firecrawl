import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  BATCH_B_BLOCKED_SOURCES,
  FOUNDRY_C10_ENUMERATION_DEADLINE_MS,
  FOUNDRY_C10_MAX_SITEMAP_CARDS,
  SourceBoundOneShotTransport,
  allowlistedCards,
  foundryCommercialInitialCards,
  foundryCommercialReceiptProducer,
  sha256,
  type DirectProviderTransport,
  type ReceiptBinding,
  type RequestCard,
  type TransportResponse,
} from "../../../capacity_c10/receipts/index.js";
import { MemoryReceiptStore } from "./receipt_test_store.js";
import { canonicalDaumPropertyUrl, daumTenure } from "../../../sources/daum-commercial.js";
import { canonicalTranswesternUrl } from "../../../sources/transwestern.js";
import { naiPublicPostId } from "../../../sources/nai-global.js";
import { parseSavillsNextData } from "../../../sources/savills.js";
import { classifyFoundryStatus } from "../../../sources/foundry-commercial.js";

const binding: ReceiptBinding = Object.freeze({
  planSha256: "a".repeat(64), cohortSha256: "b".repeat(64), policySha256: "c".repeat(64),
  sourceSha256: "d".repeat(64), armSha256: "e".repeat(64), implementationSha256: "f".repeat(64),
});
const propertyUrl = "https://www.foundrycommercial.com/property/example/";

function response(card: Readonly<RequestCard>, body: string, overrides: Partial<TransportResponse> = {}): TransportResponse {
  return { status: 200, finalUrl: card.url, redirectCount: 0, elapsedMs: 2, challengeDetected: false, body: Buffer.from(body), contentType: "text/xml", providerAttempts: 1, cacheMode: "no-store", ...overrides };
}

class FakeFoundryTransport implements DirectProviderTransport {
  readonly cards: RequestCard[] = [];
  constructor(readonly propertyNotes: readonly string[] = ["For Sale"]) {}
  async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    this.cards.push(card);
    if (card.id === "foundry-sitemap-index") return response(card, `<sitemapindex><sitemap><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemap></sitemapindex>`);
    if (card.id === "foundry-sitemap-0") return response(card, `<urlset><url><loc>${propertyUrl}</loc></url></urlset>`);
    return response(card, `<html><head><link rel="canonical" href="${propertyUrl}"><link rel="shortlink" href="https://www.foundrycommercial.com/?p=123"></head><body><h1>Example</h1><ul class="property-notes">${this.propertyNotes.map((note) => `<li>${note}</li>`).join("")}</ul></body></html>`, { contentType: "text/html" });
  }
}

async function context(fake = new FakeFoundryTransport()) {
  const store = new MemoryReceiptStore();
  const transport = new SourceBoundOneShotTransport("foundry-commercial", binding, allowlistedCards("foundry-commercial", foundryCommercialInitialCards()), store, fake);
  return { store, transport, fake } as const;
}

test("Foundry producer derives, seals, freezes, and then verifies direct no-store member detail", async () => {
  const receiptContext = await context();
  const enumeration = await foundryCommercialReceiptProducer.produceEnumerationReceipt(receiptContext);
  const memberKey = `foundry-member-${sha256(propertyUrl).slice(0, 24)}`;
  assert.deepEqual(receiptContext.fake.cards.map((card) => card.id), ["foundry-sitemap-index", "foundry-sitemap-0"]);
  assert.equal(enumeration.stage, "enumeration");
  assert.equal(enumeration.noWrite.database_writes, 0);
  const enumerationArtifact = receiptContext.store.jsonFor(enumeration.privateArtifactSha256);
  const evidence = enumerationArtifact.evidence as Readonly<Record<string, unknown>>;
  assert.match(evidence.frozenMemberGraphArtifactSha256 as string, /^[0-9a-f]{64}$/);
  const eventBindings = evidence.enumerationEvents as readonly Readonly<Record<string, unknown>>[];
  assert.equal(eventBindings.length, 2);
  for (const event of eventBindings) {
    assert.match(event.privateEventSha256 as string, /^[0-9a-f]{64}$/);
    assert.match(event.projectionSha256 as string, /^[0-9a-f]{64}$/);
  }
  const member = await foundryCommercialReceiptProducer.produceMemberReceipt(receiptContext, { key: memberKey, providerId: "123" });
  assert.equal(member.stage, "member");
  assert.equal(receiptContext.fake.cards.length, 3);
  assert.equal(receiptContext.fake.cards[2]?.cacheMode, "no-store");
  assert.equal(receiptContext.transport.requestAccounting().retries, 0);
  assert.equal(JSON.stringify(member).includes("foundrycommercial.com"), false);
});

test("Foundry producer rejects an arbitrary member, cohort mismatch, and a fake non-one-shot response", async () => {
  const first = await context();
  await foundryCommercialReceiptProducer.produceEnumerationReceipt(first);
  await assert.rejects(foundryCommercialReceiptProducer.produceMemberReceipt(first, { key: "not-in-graph", providerId: "123" }), /not in the sealed graph/);
  const memberKey = `foundry-member-${sha256(propertyUrl).slice(0, 24)}`;
  await assert.rejects(foundryCommercialReceiptProducer.produceMemberReceipt(first, { key: memberKey, providerId: "wrong" }), /does not bind/);

  const fake = new FakeFoundryTransport();
  const second = await context(fake);
  await foundryCommercialReceiptProducer.produceEnumerationReceipt(second);
  const original = fake.execute.bind(fake);
  fake.execute = async (card) => ({ ...(await original(card)), providerAttempts: 2 });
  await assert.rejects(foundryCommercialReceiptProducer.produceMemberReceipt(second, { key: memberKey, providerId: "123" }), /violates/);
  assert.equal(second.transport.requestAccounting().retries, 0);
});

test("Foundry producer rejects terminal and unknown native statuses", async () => {
  const memberKey = `foundry-member-${sha256(propertyUrl).slice(0, 24)}`;
  for (const status of ["Sold", "Mystery Status"]) {
    const receiptContext = await context(new FakeFoundryTransport([status]));
    await foundryCommercialReceiptProducer.produceEnumerationReceipt(receiptContext);
    await assert.rejects(
      foundryCommercialReceiptProducer.produceMemberReceipt(receiptContext, {
        key: memberKey,
        providerId: "123",
      }),
      /projection failed without retry/,
    );
  }
});

test("Foundry producer requires explicit sale or lease tenure for generic active status", async () => {
  const memberKey = `foundry-member-${sha256(propertyUrl).slice(0, 24)}`;
  for (const notes of [["Available"], ["Coming Soon", "Office"]]) {
    const receiptContext = await context(new FakeFoundryTransport(notes));
    await foundryCommercialReceiptProducer.produceEnumerationReceipt(receiptContext);
    await assert.rejects(
      foundryCommercialReceiptProducer.produceMemberReceipt(receiptContext, {
        key: memberKey,
        providerId: "123",
      }),
      /projection failed without retry/,
    );
  }
});

test("Foundry producer accepts generic active status only with explicit provider tenure", async () => {
  const memberKey = `foundry-member-${sha256(propertyUrl).slice(0, 24)}`;
  for (const notes of [["Available", "For Lease"], ["Under Contract", "For Sale / Lease"]]) {
    const receiptContext = await context(new FakeFoundryTransport(notes));
    await foundryCommercialReceiptProducer.produceEnumerationReceipt(receiptContext);
    const member = await foundryCommercialReceiptProducer.produceMemberReceipt(receiptContext, {
      key: memberKey,
      providerId: "123",
    });
    assert.equal(member.stage, "member");
  }
});

test("Foundry uses fixed sitemap, member, card, and aggregate-deadline caps", async () => {
  const overLimit = new FakeFoundryTransport();
  overLimit.execute = async (request) => {
    overLimit.cards.push(request);
    return response(request, `<sitemapindex>${Array.from(
    { length: FOUNDRY_C10_MAX_SITEMAP_CARDS + 1 },
    (_, index) => `<sitemap><loc>https://www.foundrycommercial.com/property-sitemap${index + 1}.xml</loc></sitemap>`,
    ).join("")}</sitemapindex>`);
  };
  const capped = await context(overLimit);
  await assert.rejects(foundryCommercialReceiptProducer.produceEnumerationReceipt(capped), /projection is invalid/);
  assert.equal(overLimit.cards.length, 1);
  assert.equal(foundryCommercialInitialCards()[0]?.timeoutMs, 60_000);

  const timely = await context();
  const originalNow = Date.now;
  let reads = 0;
  Date.now = () => (reads++ === 0 ? 1 : FOUNDRY_C10_ENUMERATION_DEADLINE_MS + 2);
  try {
    await assert.rejects(foundryCommercialReceiptProducer.produceEnumerationReceipt(timely), /fixed deadline/);
  } finally {
    Date.now = originalNow;
  }
  assert.equal(timely.fake.cards.length, 1);
});

test("Foundry rejects a mismatched context before transport execution", async () => {
  const fake = new FakeFoundryTransport();
  const wrong = new SourceBoundOneShotTransport("wrong-source", binding, allowlistedCards("wrong-source", [{
    id: "wrong-enum", sourceKey: "wrong-source", stage: "enumeration", method: "GET",
    url: "https://example.test/enumeration", allowedHost: "example.test", headers: {},
    contentType: null, body: null, cacheMode: "no-store", timeoutMs: 1_000, maxBytes: 64,
  }]), new MemoryReceiptStore(), fake);
  await assert.rejects(foundryCommercialReceiptProducer.produceEnumerationReceipt({ transport: wrong }), /source binding mismatch/);
  assert.equal(fake.cards.length, 0);
});

test("Foundry rejects a same-source substituted initial card before transport execution", async () => {
  const fake = new FakeFoundryTransport();
  const expected = foundryCommercialInitialCards()[0]!;
  const transport = new SourceBoundOneShotTransport(
    "foundry-commercial", binding,
    allowlistedCards("foundry-commercial", [{ ...expected, url: `${expected.url}?substituted=1` }]),
    new MemoryReceiptStore(), fake,
  );
  await assert.rejects(foundryCommercialReceiptProducer.produceEnumerationReceipt({ transport }), /initial request-card set does not match source plan/);
  assert.equal(fake.cards.length, 0);
});

test("Batch B preserves reexport parity and makes every non-admitted source explicit", async () => {
  assert.deepEqual(Object.keys(BATCH_B_BLOCKED_SOURCES).sort(), ["daum-commercial", "matthews", "nai-global", "savills", "transwestern"]);
  assert.equal(naiPublicPostId(42), "42");
  assert.equal(canonicalTranswesternUrl("/property/example"), "https://transwestern.com/property/example");
  assert.equal(canonicalDaumPropertyUrl("/property/example/"), "https://daumcommercial.com/property/example/");
  assert.equal(daumTenure("Lease"), "lease");
  assert.equal(classifyFoundryStatus("For Sale").disposition, "active");
  assert.equal(classifyFoundryStatus("Sold").disposition, "terminal");
  assert.equal(classifyFoundryStatus("Mystery Status").disposition, "held");
  assert.deepEqual(parseSavillsNextData('<script id="__NEXT_DATA__" type="application/json">{"ok":true}</script>'), { ok: true });
});

test("Foundry producer has no collector, normal source, cache, retry, Firecrawl, or writer import", async () => {
  const path = new URL("../../../capacity_c10/receipts/sources/batch_b.ts", import.meta.url);
  const source = await readFile(path, "utf8");
  for (const forbidden of [
    "foundry-commercial.js",
    "foundryFetchText",
    "srcFoundryCommercial",
    "scrape",
    "cre_ingest",
    "collect.ts",
    "firecrawl",
  ]) {
    assert.equal(source.includes(forbidden), false, `producer must not import ${forbidden}`);
  }
});
