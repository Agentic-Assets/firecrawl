import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  C10ReceiptError,
  SourceBoundOneShotTransport,
  allowlistedCards,
  canonicalJson,
  type DirectProviderTransport,
  type RequestCard,
  type TransportResponse,
} from "../../../capacity_c10/receipts/index.js";
import { MemoryReceiptStore } from "./receipt_test_store.js";
import {
  blockedInventoryReceiptProducers,
  inventoryReceiptProducers,
  type InventoryReceiptProducer,
  type InventorySourceKey,
} from "../../../capacity_c10/receipts/inventory.js";

const binding = Object.freeze({
  planSha256: "a".repeat(64),
  cohortSha256: "b".repeat(64),
  policySha256: "c".repeat(64),
  sourceSha256: "d".repeat(64),
  armSha256: "e".repeat(64),
  implementationSha256: "f".repeat(64),
});

function enumerationBody(sourceKey: InventorySourceKey): unknown {
  switch (sourceKey) {
    case "cbre":
      return { DocumentCount: 1, Documents: [{ "Common.PrimaryKey": "cbre-1" }] };
    case "cushman-wakefield":
      return { total_item: 1, content: [{ id: "cw-1", url: "/properties/cw-1" }] };
    case "newmark":
      return { total: 1, data: [{ id: "nm-1", slug: "newmark-one" }] };
    case "srs":
      return { total: 1, properties: [{ apto_data: { SRS_Listings_ID__c: "srs-1" }, permalink: "/properties/srs-1" }] };
    case "svn":
      return { meta: { total: 1 }, inventory: [{ id: "svn-1", show_link: "https://svn.com/properties/?propertyId=svn-1-sale" }] };
    case "lee-associates":
      return { meta: { total: 1 }, inventory: [{ id: "lee-1", show_link: "https://www.lee-associates.com/properties/?propertyId=lee-1-sale" }] };
    case "bull-realty":
      return { meta: { total: 1 }, inventory: [{ id: "bull-1", show_link: "https://www.bullrealty.com/properties/?propertyId=bull-1-sale" }] };
  }
}

class FakeDirectTransport implements DirectProviderTransport {
  readonly calls: RequestCard[] = [];

  constructor(
    private readonly sourceKey: InventorySourceKey,
    private readonly override: Partial<TransportResponse> = {},
  ) {}

  async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    this.calls.push(card);
    const payload = card.stage === "enumeration"
      ? enumerationBody(this.sourceKey)
      : { providerId: card.id, source: this.sourceKey };
    return {
      status: 200,
      finalUrl: card.url,
      redirectCount: 0,
      elapsedMs: 1,
      challengeDetected: false,
      body: Buffer.from(JSON.stringify(payload)),
      contentType: "application/json",
      providerAttempts: 1,
      cacheMode: "no-store",
      ...this.override,
    };
  }
}

async function contextFor(producer: InventoryReceiptProducer, direct?: FakeDirectTransport) {
  const store = new MemoryReceiptStore();
  const fake = direct ?? new FakeDirectTransport(producer.sourceKey);
  const transport = new SourceBoundOneShotTransport(
    producer.sourceKey,
    binding,
    allowlistedCards(producer.sourceKey, producer.initialCards),
    store,
    fake,
  );
  return { context: { transport }, fake };
}

test("all executable source producers seal fake native enumeration then member receipts", async () => {
  assert.equal(inventoryReceiptProducers.size, 7);
  for (const [sourceKey, producer] of inventoryReceiptProducers) {
    assert.equal(producer.sourceKey, sourceKey);
    assert.equal(producer.fully_verified, false);
    const { context, fake } = await contextFor(producer);
    const enumeration = await producer.produceEnumerationReceipt(context);
    const memberCard = fake.calls.find((card) => card.stage === "member");
    assert.equal(memberCard, undefined, `${sourceKey}: member graph must be frozen, not executed, by enumeration`);
    const expectedProviderId = sourceKey === "cbre" ? "cbre-1"
      : sourceKey === "cushman-wakefield" ? "cw-1"
      : sourceKey === "newmark" ? "nm-1"
      : sourceKey === "srs" ? "srs-1"
      : sourceKey === "svn" ? "svn-1"
      : sourceKey === "lee-associates" ? "lee-1" : "bull-1";
    const member = await producer.produceMemberReceipt(context, {
      key: `member-${expectedProviderId}`,
      providerId: expectedProviderId,
    });
    assert.equal(enumeration.requestAccounting.retries, 0);
    assert.equal(member.requestAccounting.retries, 0);
    assert.equal(enumeration.noWrite.cache_writes, 0);
    assert.equal(member.memberKey, `member-${expectedProviderId}`);
    assert.equal(fake.calls.length, 2, `${sourceKey}: one enumeration and one member direct attempt`);
    assert.ok(fake.calls.every((card) => card.cacheMode === "no-store"));
    assert.ok(fake.calls.every((card) => card.method === "GET" || card.method === "POST"));
    assert.equal(fake.calls.filter((card) => card.method === "POST").length, sourceKey === "newmark" || sourceKey === "srs" ? 1 : 0);
  }
});

test("inventory rejects a same-source substituted initial POST body before transport execution", async () => {
  const producer = inventoryReceiptProducers.get("newmark")!;
  const fake = new FakeDirectTransport("newmark");
  const expected = producer.initialCards[0]!;
  const substituted = {
    ...expected,
    body: canonicalJson({ ...JSON.parse(expected.body!), page: 99 }),
  };
  const store = new MemoryReceiptStore();
  const transport = new SourceBoundOneShotTransport(
    producer.sourceKey, binding, allowlistedCards(producer.sourceKey, [substituted]), store, fake,
  );
  await assert.rejects(producer.produceEnumerationReceipt({ transport }), /initial request-card set does not match source plan/);
  assert.equal(fake.calls.length, 0);
});

test("a terminal provider redirect is one attempt, with no fallback or member graph", async () => {
  const producer = inventoryReceiptProducers.get("newmark")!;
  const fake = new FakeDirectTransport("newmark", { redirectCount: 1 });
  const { context } = await contextFor(producer, fake);
  await assert.rejects(producer.produceEnumerationReceipt(context), C10ReceiptError);
  assert.equal(fake.calls.length, 1);
  assert.equal(context.transport.requestAccounting().attempts, 1);
  assert.equal(context.transport.requestAccounting().retries, 0);
  assert.equal(context.transport.requestAccounting().events[0]?.outcome, "rejected");
});

test("Deal Flow is explicitly blocked before an old GET/cards fixture can execute", async () => {
  const blocked = blockedInventoryReceiptProducers.get("cbre-dealflow");
  assert.equal(inventoryReceiptProducers.has("cbre-dealflow"), false);
  assert.equal(blocked?.executable, false);
  assert.match(blocked?.reason ?? "", /engine key.*form-urlencoded POST.*html/i);
  assert.throws(() => blocked?.refuse(), /blocked: ListingEngine/);
});

test("Buildout refuses URL aliases and invalid native show_link identities", async () => {
  const producer = inventoryReceiptProducers.get("svn")!;
  const fake = new FakeDirectTransport("svn");
  fake.execute = async (card: Readonly<RequestCard>): Promise<TransportResponse> => {
    fake.calls.push(card);
    return {
      status: 200, finalUrl: card.url, redirectCount: 0, elapsedMs: 1, challengeDetected: false,
      body: Buffer.from(JSON.stringify({ meta: { total: 1 }, inventory: [{ id: "svn-1", url: "https://svn.com/properties/legacy" }] })),
      contentType: "application/json", providerAttempts: 1, cacheMode: "no-store",
    };
  };
  const { context } = await contextFor(producer, fake);
  await assert.rejects(producer.produceEnumerationReceipt(context), C10ReceiptError);
  assert.equal(fake.calls.length, 1);
  assert.equal(context.transport.requestAccounting().events[0]?.outcome, "rejected");
});

test("Buildout refuses a native show_link without its provider propertyId", async () => {
  const producer = inventoryReceiptProducers.get("svn")!;
  const fake = new FakeDirectTransport("svn");
  fake.execute = async (card: Readonly<RequestCard>): Promise<TransportResponse> => {
    fake.calls.push(card);
    return {
      status: 200, finalUrl: card.url, redirectCount: 0, elapsedMs: 1, challengeDetected: false,
      body: Buffer.from(JSON.stringify({ meta: { total: 1 }, inventory: [{ id: "svn-1", show_link: "https://svn.com/properties/legacy" }] })),
      contentType: "application/json", providerAttempts: 1, cacheMode: "no-store",
    };
  };
  const { context } = await contextFor(producer, fake);
  await assert.rejects(producer.produceEnumerationReceipt(context), C10ReceiptError);
  assert.equal(fake.calls.length, 1);
});

test("inventory producer rejects a mismatched transport before any request", async () => {
  const producer = inventoryReceiptProducers.get("cbre")!;
  const fake = new FakeDirectTransport("cbre");
  const initial = producer.initialCards[0]!;
  const wrong = new SourceBoundOneShotTransport("wrong-source", binding, allowlistedCards("wrong-source", [{
    ...initial,
    sourceKey: "wrong-source",
  }]), new MemoryReceiptStore(), fake);
  await assert.rejects(producer.produceEnumerationReceipt({ transport: wrong }), /source binding mismatch/);
  assert.equal(fake.calls.length, 0);
});

test("inventory receipt module is isolated from collector, cache, and Firecrawl imports", async () => {
  const filename = fileURLToPath(new URL("../../../capacity_c10/receipts/inventory.ts", import.meta.url));
  const source = await readFile(filename, "utf8");
  assert.equal(/from\s+["'][^"']*(?:collect|sources|scrape|ingest|cache|firecrawl)[^"']*["']/.test(source), false);
  assert.equal(/(?:retry|fallback)\s*\(/.test(source), false);
  for (const sourceKey of ["cbre", "cbre-dealflow", "cushman-wakefield", "newmark", "srs", "svn", "lee-associates", "bull-realty"]) {
    assert.ok(source.includes(`"${sourceKey}"`));
  }
});
