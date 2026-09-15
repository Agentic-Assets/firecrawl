import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  C10ReceiptError,
  PrivateReceiptStore,
  SourceBoundOneShotTransport,
  allowlistedCards,
  type DirectProviderTransport,
  type RequestCard,
  type ReceiptBinding,
  type TransportResponse,
} from "../../../capacity_c10/receipts/index.js";
import {
  AVISON_YOUNG_C10_BLOCKER,
  AvisonYoungBlockedReceiptProducer,
} from "../../../capacity_c10/receipts/strict_detail/avison_young.js";
import {
  COLLIERS_MAIN_C10_BLOCKER,
  ColliersMainBlockedReceiptProducer,
} from "../../../capacity_c10/receipts/strict_detail/colliers_main.js";
import {
  createColliersReceiptProducer,
  colliersMapEnumerationCard,
  type ColliersReceiptPlan,
} from "../../../capacity_c10/receipts/strict_detail/colliers.js";
import {
  createJllInvestorReceiptProducer,
  jllInvestorEnumerationCard,
  type JllInvestorReceiptPlan,
} from "../../../capacity_c10/receipts/strict_detail/jll_investor.js";
import {
  createJllReceiptProducer,
  jllEnumerationCard,
  type JllReceiptPlan,
} from "../../../capacity_c10/receipts/strict_detail/jll.js";
import {
  createMarcusReceiptProducer,
  marcusEnumerationCard,
  type MarcusReceiptPlan,
} from "../../../capacity_c10/receipts/strict_detail/marcus_millichap.js";

const binding: ReceiptBinding = Object.freeze({
  planSha256: "a".repeat(64),
  cohortSha256: "b".repeat(64),
  policySha256: "c".repeat(64),
  sourceSha256: "d".repeat(64),
  armSha256: "e".repeat(64),
  implementationSha256: "f".repeat(64),
});

class FixtureTransport implements DirectProviderTransport {
  readonly cards: RequestCard[] = [];
  constructor(private readonly bodies: Readonly<Record<string, string>>) {}

  async execute(card: Readonly<RequestCard>): Promise<TransportResponse> {
    this.cards.push(card);
    const body = this.bodies[card.id];
    if (body === undefined) throw new Error(`unexpected card ${card.id}`);
    return {
      status: 200,
      finalUrl: card.url,
      redirectCount: 0,
      elapsedMs: 7,
      challengeDetected: false,
      body: Buffer.from(body),
      contentType: card.id.includes("member") || card.id.includes("enumeration") ? "application/json" : "text/html",
      providerAttempts: 1,
      cacheMode: "no-store",
    };
  }
}

async function context(sourceKey: string, cards: readonly any[], fake: FixtureTransport) {
  const root = await mkdtemp(join(tmpdir(), "c10-wave4-"));
  const store = await PrivateReceiptStore.create(root);
  return {
    sourceKey,
    binding,
    store,
    transport: new SourceBoundOneShotTransport(sourceKey, binding, allowlistedCards(sourceKey, cards), store, fake),
  };
}

test("JLL seals native GraphQL enumeration and exact canonical POST detail graph", async () => {
  const plan: JllReceiptPlan = {
    transaction: "sale", propertyType: "office", page: 1,
    members: [{ key: "jll-1", providerId: "1", canonicalUrl: "https://property.jll.com/listings/office-1" }],
    enumerationCards: [],
  };
  const fake = new FixtureTransport({
    "jll-enumeration": JSON.stringify({ data: { properties: { count: 1, items: [{
      id: "1", title: "One", images: [], address: "1 Main", propertyTypes: ["office"], tenureTypes: ["sale"],
      pageUrl: "/listings/office-1", surfaceAreas: [],
    }] } } }),
    "jll-member-0": '<script id="__NEXT_DATA__">{"props":{"pageProps":{"property":{"id":"1","pageUrl":"https://property.jll.com/listings/office-1","images":["https://asset.test/a.jpg"]}}}}</script>',
  });
  const receiptContext = await context("jll", [jllEnumerationCard(plan)], fake);
  const producer = createJllReceiptProducer(plan);
  const enumeration = await producer.produceEnumerationReceipt(receiptContext);
  const member = await producer.produceMemberReceipt(receiptContext, plan.members[0]!);
  assert.equal(enumeration.noWrite.database_writes, 0);
  assert.equal(member.requestAccounting.attempts, 2);
  assert.equal(fake.cards[0]?.method, "POST");
  assert.match(fake.cards[0]?.body ?? "", /"operationName":"SearchResults"/);
  assert.equal(fake.cards[0]?.body?.includes("office"), true);
});

test("JLL Investor binds a native search build id to the one-shot structured detail route", async () => {
  const plan: JllInvestorReceiptPlan = {
    page: 1,
    members: [{ key: "investor-2", providerId: "006000000000000001", canonicalUrl: "https://invest.jll.com/us/en/listings/office/two" }],
    enumerationCards: [],
  };
  const search = { buildId: "build_1", props: { pageProps: { initialState: { advancedSearch: {
    filters: [{ key: "location", value: "United States", label: "United States", type: "collection" }],
    count: 1, searchPage: 1, listings: [{ id: "006000000000000001", alias: "office/two" }],
  } } } } };
  const fake = new FixtureTransport({
    "jll-investor-enumeration": `<script id="__NEXT_DATA__">${JSON.stringify(search)}</script>`,
    "jll-investor-member-0": JSON.stringify({ pageProps: { initialState: { pdp: { listing: { id: "006000000000000001", alias: "office/two", images: [] } } } } }),
  });
  const receiptContext = await context("jll-investor", [jllInvestorEnumerationCard(plan)], fake);
  const producer = createJllInvestorReceiptProducer(plan);
  await producer.produceEnumerationReceipt(receiptContext);
  await producer.produceMemberReceipt(receiptContext, plan.members[0]!);
  assert.match(fake.cards[1]?.url ?? "", /_next\/data\/build_1/);
  assert.equal(fake.cards[1]?.method, "GET");
});

test("Colliers requires sealed map/list parity before its exact SLP member request", async () => {
  const plan: ColliersReceiptPlan = {
    engineKey: "engine", start: 1, pageSize: 1,
    members: [{ key: "colliers-3", providerId: "3", detailPv: "detail-3", canonicalUrl: "https://my.rcm1.com/slp/?pv=detail-3" }],
    enumerationCards: [],
  };
  const fake = new FixtureTransport({
    "colliers-map-enumeration": JSON.stringify({ projectLocations: [{ ProjectId: "3", Latitude: 1, Longitude: 2 }] }),
    "colliers-list-enumeration": JSON.stringify({ total: 1, html: '<li class="item"><a href="/slp/?pv=detail-3"></a><span class="city">A, NY</span></li>' }),
    "colliers-member-0": JSON.stringify({ ProjectSummary: { AttributeVisibility: { ProjectId: "3" }, CanonicalUrl: "https://my.rcm1.com/slp/?pv=detail-3" }, GalleryImages: [] }),
  });
  const receiptContext = await context("colliers", [colliersMapEnumerationCard(plan)], fake);
  const producer = createColliersReceiptProducer(plan);
  await producer.produceEnumerationReceipt(receiptContext);
  await producer.produceMemberReceipt(receiptContext, plan.members[0]!);
  assert.deepEqual(fake.cards.map((card) => card.id), ["colliers-map-enumeration", "colliers-list-enumeration", "colliers-member-0"]);
});

test("Marcus seals canonical search and map POST bodies without retry or fallback", async () => {
  const plan: MarcusReceiptPlan = {
    pageSize: 1,
    members: [{ key: "marcus-4", providerId: "4", activityId: "activity-4", canonicalUrl: "https://www.marcusmillichap.com/properties/four" }],
    enumerationCards: [],
  };
  const fake = new FixtureTransport({
    "marcus-enumeration": JSON.stringify({ Results: { TotalCount: 1, Properties: [{ DealId: "4", ActivityId: "activity-4", PropertyUrl: "/properties/four" }] } }),
    "marcus-member-0": JSON.stringify({ Results: { Properties: [{ ActivityId: "activity-4", PropertyUrl: "/properties/four" }] } }),
  });
  const receiptContext = await context("marcus-millichap", [marcusEnumerationCard(plan)], fake);
  const producer = createMarcusReceiptProducer(plan);
  await producer.produceEnumerationReceipt(receiptContext);
  await producer.produceMemberReceipt(receiptContext, plan.members[0]!);
  assert.match(fake.cards[0]?.body ?? "", /"pageSize":1/);
  assert.equal(fake.cards[1]?.body, '{"activityId":"activity-4"}');
  assert.equal(receiptContext.transport.requestAccounting().retries, 0);
});

test("browser-dependent source modules fail closed before any transport is invoked", async () => {
  const fake = new FixtureTransport({});
  const blockedContext = await context("jll", [{
    id: "unused", sourceKey: "jll", stage: "enumeration", method: "GET", url: "https://property.jll.com/x",
    allowedHost: "property.jll.com", headers: {}, contentType: null, body: null, cacheMode: "no-store", timeoutMs: 1, maxBytes: 1,
  }], fake);
  await assert.rejects(new AvisonYoungBlockedReceiptProducer().produceEnumerationReceipt(blockedContext), new RegExp(AVISON_YOUNG_C10_BLOCKER));
  await assert.rejects(new ColliersMainBlockedReceiptProducer().produceEnumerationReceipt(blockedContext), new RegExp(COLLIERS_MAIN_C10_BLOCKER));
  assert.equal(fake.cards.length, 0);
});

test("source receipt producers cannot import collector, cache, or normal scrape surfaces", async () => {
  const root = dirname(fileURLToPath(import.meta.url));
  const files = ["avison_young.ts", "colliers.ts", "colliers_main.ts", "jll.ts", "jll_investor.ts", "marcus_millichap.ts"];
  for (const file of files) {
    const text = await readFile(join(root, "../../../capacity_c10/receipts/strict_detail", file), "utf8");
    assert.doesNotMatch(text, /from\s+["'][^"']*(?:collect|cre_ingest|checkpoint|lib\/scrape|cache)[^"']*["']/);
    assert.doesNotMatch(text, /\b(?:srcJll|srcColliers|srcMarcus|srcAvison|scrapeDoc|scrapeRaw|fetch[A-Z]\w*WithRetry)\b/);
  }
});
