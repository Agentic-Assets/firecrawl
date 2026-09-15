import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  BATCH_B_BLOCKED_SOURCES,
  PrivateReceiptStore,
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
  async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    this.cards.push(card);
    if (card.id === "foundry-sitemap-index") return response(card, `<sitemapindex><sitemap><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemap></sitemapindex>`);
    if (card.id === "foundry-sitemap-0") return response(card, `<urlset><url><loc>${propertyUrl}</loc></url></urlset>`);
    return response(card, `<html><head><link rel="canonical" href="${propertyUrl}"><link rel="shortlink" href="https://www.foundrycommercial.com/?p=123"></head><body><h1>Example</h1></body></html>`, { contentType: "text/html" });
  }
}

async function context(fake = new FakeFoundryTransport()) {
  const store = await PrivateReceiptStore.create(await mkdtemp(join(tmpdir(), "c10-foundry-")));
  const transport = new SourceBoundOneShotTransport("foundry-commercial", binding, allowlistedCards("foundry-commercial", foundryCommercialInitialCards()), store, fake);
  return { sourceKey: "foundry-commercial", binding, store, transport, fake } as const;
}

test("Foundry producer derives, seals, freezes, and then verifies direct no-store member detail", async () => {
  const receiptContext = await context();
  const enumeration = await foundryCommercialReceiptProducer.produceEnumerationReceipt(receiptContext);
  const memberKey = `foundry-member-${sha256(propertyUrl).slice(0, 24)}`;
  assert.deepEqual(receiptContext.fake.cards.map((card) => card.id), ["foundry-sitemap-index", "foundry-sitemap-0"]);
  assert.equal(enumeration.stage, "enumeration");
  assert.equal(enumeration.noWrite.database_writes, 0);
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

test("Batch B preserves reexport parity and makes every non-admitted source explicit", async () => {
  assert.deepEqual(Object.keys(BATCH_B_BLOCKED_SOURCES).sort(), ["daum-commercial", "matthews", "nai-global", "savills", "transwestern"]);
  assert.equal(naiPublicPostId(42), "42");
  assert.equal(canonicalTranswesternUrl("/property/example"), "https://transwestern.com/property/example");
  assert.equal(canonicalDaumPropertyUrl("/property/example/"), "https://daumcommercial.com/property/example/");
  assert.equal(daumTenure("Lease"), "lease");
  assert.equal(classifyFoundryStatus("For Sale").disposition, "active");
  assert.deepEqual(parseSavillsNextData('<script id="__NEXT_DATA__" type="application/json">{"ok":true}</script>'), { ok: true });
});

test("Foundry producer has no collector, cache, retry, Firecrawl, or writer import", async () => {
  const path = new URL("../../../capacity_c10/receipts/sources/batch_b.ts", import.meta.url);
  const source = await readFile(path, "utf8");
  for (const forbidden of ["foundryFetchText", "srcFoundryCommercial", "scrape", "cre_ingest", "collect.ts", "firecrawl"]) {
    assert.equal(source.includes(forbidden), false, `producer must not import ${forbidden}`);
  }
});
