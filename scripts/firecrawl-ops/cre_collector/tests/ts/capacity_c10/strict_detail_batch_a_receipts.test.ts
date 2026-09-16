import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  C10ReceiptError,
  SourceBoundOneShotTransport,
  allowlistedCards,
  type DirectProviderTransport,
  type RequestCard,
  type ReceiptBinding,
  type TransportResponse,
} from "../../../capacity_c10/receipts/index.js";
import { MemoryReceiptStore } from "./receipt_test_store.js";
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
  colliersListEnumerationCard,
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
  marcusCountEnumerationCard,
  marcusMapEnumerationCard,
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
  const store = new MemoryReceiptStore();
  return {
    store,
    transport: new SourceBoundOneShotTransport(sourceKey, binding, allowlistedCards(sourceKey, cards), store, fake),
  };
}

function expansionParentCardId(store: MemoryReceiptStore, sourceKey: string, memberCardId: string): string {
  const expansionBytes = [...store.artifacts.entries()].find(([name]) =>
    name.startsWith(`graph-${sourceKey}-${memberCardId}-`),
  )?.[1];
  assert.ok(expansionBytes);
  const expansion = JSON.parse(Buffer.from(expansionBytes).toString("utf8"));
  const parent = store.jsonFor(expansion.parent.privateEventSha256);
  return (parent.card as { id: string }).id;
}

test("strict-detail rejects a same-source transport with a substituted initial host before request", async () => {
  const plan: JllReceiptPlan = {
    enumerations: [{ transaction: "sale", propertyType: "office", page: 1 }],
    members: [{ key: "jll-1", providerId: "1", canonicalUrl: "https://property.jll.com/listings/office-1" }],
    enumerationCards: [],
  };
  const expected = jllEnumerationCard(plan.enumerations[0]!, 0);
  const fake = new FixtureTransport({});
  const receiptContext = await context("jll", [{ ...expected, url: "https://alternate.example/graphql", allowedHost: "alternate.example" }], fake);
  await assert.rejects(createJllReceiptProducer(plan).produceEnumerationReceipt(receiptContext), /initial request-card set does not match source plan/);
  assert.equal(fake.cards.length, 0);
});

async function localImportGraph(entry: URL, seen = new Set<string>()): Promise<Array<{ file: string; specifier: string }>> {
  const file = fileURLToPath(entry);
  if (seen.has(file)) return [];
  seen.add(file);
  const source = await readFile(file, "utf8");
  const edges: Array<{ file: string; specifier: string }> = [];
  for (const match of source.matchAll(/\bimport(\s+type)?[\s\S]*?\bfrom\s+["']([^"']+)["']/g)) {
    if (match[1]) continue;
    const specifier = match[2]!;
    edges.push({ file, specifier });
    if (!specifier.startsWith(".")) continue;
    const target = new URL(specifier.replace(/\.js$/, ".ts"), entry);
    edges.push(...await localImportGraph(target, seen));
  }
  return edges;
}

test("strict receipt producer transitive graph excludes filesystem, cache, and scraper modules", async () => {
  const entries = ["jll", "jll_investor", "colliers", "marcus_millichap", "avison_young", "colliers_main"]
    .map((name) => new URL(`../../../capacity_c10/receipts/strict_detail/${name}.ts`, import.meta.url));
  const edges = (await Promise.all(entries.map((entry) => localImportGraph(entry)))).flat();
  const forbidden = /^(?:node:fs(?:\/|$))|(?:\.\.\/lib\/(?:scrape|performance)\.js$)/;
  assert.equal(edges.filter((edge) => forbidden.test(edge.specifier)).length, 0, JSON.stringify(edges.filter((edge) => forbidden.test(edge.specifier))));
});

test("JLL seals native GraphQL enumeration and exact canonical POST detail graph", async () => {
  const plan: JllReceiptPlan = {
    enumerations: [{ transaction: "sale", propertyType: "office", page: 1 }],
    members: [{ key: "jll-1", providerId: "1", canonicalUrl: "https://property.jll.com/listings/office-1" }],
    enumerationCards: [],
  };
  const fake = new FixtureTransport({
    "jll-enumeration-0": JSON.stringify({ data: { properties: { count: 1, items: [{
      id: "1", title: "One", images: [], address: "1 Main", propertyTypes: ["office"], tenureTypes: ["sale"],
      pageUrl: "/listings/office-1", surfaceAreas: [],
    }] } } }),
    "jll-member-0": '<script id="__NEXT_DATA__">{"props":{"pageProps":{"property":{"id":"1","pageUrl":"https://property.jll.com/listings/office-1","images":["https://asset.test/a.jpg"]}}}}</script>',
  });
  const receiptContext = await context("jll", [jllEnumerationCard(plan.enumerations[0]!, 0)], fake);
  const producer = createJllReceiptProducer(plan);
  const enumeration = await producer.produceEnumerationReceipt(receiptContext);
  const member = await producer.produceMemberReceipt(receiptContext, plan.members[0]!);
  assert.equal(enumeration.noWrite.database_writes, 0);
  assert.equal(member.requestAccounting.attempts, 2);
  assert.equal(fake.cards[0]?.method, "POST");
  assert.match(fake.cards[0]?.body ?? "", /"operationName":"SearchResults"/);
  assert.equal(fake.cards[0]?.body?.includes("office"), true);
});

test("JLL reconciles cohort members across exact filter and page strata", async () => {
  const plan: JllReceiptPlan = {
    enumerations: [
      { transaction: "sale", propertyType: "office", page: 1 },
      { transaction: "sale", propertyType: "industrial", page: 2 },
    ],
    members: [
      { key: "jll-1", providerId: "1", canonicalUrl: "https://property.jll.com/listings/office-1" },
      { key: "jll-2", providerId: "2", canonicalUrl: "https://property.jll.com/listings/industrial-2" },
    ],
    enumerationCards: [],
  };
  const fake = new FixtureTransport({
    "jll-enumeration-0": JSON.stringify({ data: { properties: { count: 2, items: [{
      id: "1", title: "One", images: [], address: "1 Main", propertyTypes: ["office"], tenureTypes: ["sale"],
      pageUrl: "/listings/office-1", surfaceAreas: [],
    }] } } }),
    "jll-enumeration-1": JSON.stringify({ data: { properties: { count: 2, items: [{
      id: "2", title: "Two", images: [], address: "2 Main", propertyTypes: ["industrial"], tenureTypes: ["sale"],
      pageUrl: "/listings/industrial-2", surfaceAreas: [],
    }] } } }),
    "jll-member-0": '<script id="__NEXT_DATA__">{"props":{"pageProps":{"property":{"id":"1","pageUrl":"https://property.jll.com/listings/office-1","images":[]}}}}</script>',
    "jll-member-1": '<script id="__NEXT_DATA__">{"props":{"pageProps":{"property":{"id":"2","pageUrl":"https://property.jll.com/listings/industrial-2","images":[]}}}}</script>',
  });
  const cards = plan.enumerations.map((slice, index) => jllEnumerationCard(slice, index));
  const receiptContext = await context("jll", cards, fake);
  const producer = createJllReceiptProducer(plan);
  await producer.produceEnumerationReceipt(receiptContext);
  assert.equal(expansionParentCardId(receiptContext.store, "jll", "jll-member-1"), "jll-enumeration-1");
  await producer.produceMemberReceipt(receiptContext, plan.members[0]!);
  await producer.produceMemberReceipt(receiptContext, plan.members[1]!);
  assert.deepEqual(
    fake.cards.map((card) => card.id),
    ["jll-enumeration-0", "jll-enumeration-1", "jll-member-0", "jll-member-1"],
  );
  assert.equal(fake.cards[1]?.body?.includes("industrial"), true);
  assert.equal(fake.cards[1]?.body?.includes('"skip":50'), true);
});

test("strict-detail producer isolates nested routes and source settings from caller mutation", async () => {
  const plan: JllReceiptPlan = {
    enumerations: [{ transaction: "sale", propertyType: "office", page: 1 }],
    members: [{ key: "jll-1", providerId: "1", canonicalUrl: "https://property.jll.com/listings/office-1" }],
    enumerationCards: [],
  };
  const initialCard = jllEnumerationCard(plan.enumerations[0]!, 0);
  const fake = new FixtureTransport({
    "jll-enumeration-0": JSON.stringify({ data: { properties: { count: 1, items: [{
      id: "1", title: "One", images: [], address: "1 Main", propertyTypes: ["office"], tenureTypes: ["sale"],
      pageUrl: "/listings/office-1", surfaceAreas: [],
    }] } } }),
    "jll-member-0": '<script id="__NEXT_DATA__">{"props":{"pageProps":{"property":{"id":"1","pageUrl":"https://property.jll.com/listings/office-1","images":[]}}}}</script>',
  });
  const receiptContext = await context("jll", [initialCard], fake);
  const producer = createJllReceiptProducer(plan);
  (plan.enumerations[0] as { page: number }).page = 99;
  (plan.members[0] as { providerId: string }).providerId = "mutated";
  (plan.members[0] as { canonicalUrl: string }).canonicalUrl = "https://property.jll.com/listings/mutated";

  await producer.produceEnumerationReceipt(receiptContext);
  await producer.produceMemberReceipt(receiptContext, { key: "jll-1", providerId: "1" });
  assert.equal(fake.cards[0]?.body, initialCard.body);
  assert.equal(fake.cards[1]?.url, "https://property.jll.com/listings/office-1");
});

test("JLL Investor binds a native search build id to the one-shot structured detail route", async () => {
  const plan: JllInvestorReceiptPlan = {
    pages: [1],
    members: [{ key: "investor-2", providerId: "006000000000000001", canonicalUrl: "https://invest.jll.com/us/en/listings/office/two" }],
    enumerationCards: [],
  };
  const search = { buildId: "build_1", props: { pageProps: { initialState: { advancedSearch: {
    filters: [{ key: "location", value: "United States", label: "United States", type: "collection" }],
    count: 1, searchPage: 1, listings: [{ id: "006000000000000001", alias: "office/two" }],
  } } } } };
  const fake = new FixtureTransport({
    "jll-investor-enumeration-0": `<script id="__NEXT_DATA__">${JSON.stringify(search)}</script>`,
    "jll-investor-member-0": JSON.stringify({ pageProps: { initialState: { pdp: { listing: { id: "006000000000000001", alias: "office/two", images: [] } } } } }),
  });
  const receiptContext = await context("jll-investor", [jllInvestorEnumerationCard(1, 0)], fake);
  const producer = createJllInvestorReceiptProducer(plan);
  await producer.produceEnumerationReceipt(receiptContext);
  await producer.produceMemberReceipt(receiptContext, plan.members[0]!);
  assert.match(fake.cards[1]?.url ?? "", /_next\/data\/build_1/);
  assert.equal(fake.cards[1]?.method, "GET");
});

test("JLL Investor reconciles immutable members across exact search pages", async () => {
  const listing = (index: number) => ({
    id: `006${String(index).padStart(15, "0")}`,
    alias: `office/item-${index}`,
  });
  const firstPage = Array.from({ length: 50 }, (_, index) => listing(index + 1));
  const selected = listing(51);
  const plan: JllInvestorReceiptPlan = {
    pages: [1, 2],
    members: [{
      key: "investor-page-2",
      providerId: selected.id,
      canonicalUrl: "https://invest.jll.com/us/en/listings/office/item-51",
    }],
    enumerationCards: [],
  };
  const search = (page: number, listings: unknown[]) => ({
    buildId: "build_pages",
    props: { pageProps: { initialState: { advancedSearch: {
      filters: [{ key: "location", value: "United States", label: "United States", type: "collection" }],
      count: 51,
      searchPage: page,
      listings,
    } } } },
  });
  const fake = new FixtureTransport({
    "jll-investor-enumeration-0": `<script id="__NEXT_DATA__">${JSON.stringify(search(1, firstPage))}</script>`,
    "jll-investor-enumeration-1": `<script id="__NEXT_DATA__">${JSON.stringify(search(2, [selected]))}</script>`,
    "jll-investor-member-0": JSON.stringify({ pageProps: { initialState: { pdp: { listing: {
      id: selected.id,
      alias: selected.alias,
      images: [],
    } } } } }),
  });
  const receiptContext = await context("jll-investor", [
    jllInvestorEnumerationCard(1, 0),
    jllInvestorEnumerationCard(2, 1),
  ], fake);
  const producer = createJllInvestorReceiptProducer(plan);
  await producer.produceEnumerationReceipt(receiptContext);
  assert.equal(
    expansionParentCardId(receiptContext.store, "jll-investor", "jll-investor-member-0"),
    "jll-investor-enumeration-1",
  );
  await producer.produceMemberReceipt(receiptContext, plan.members[0]!);
  assert.deepEqual(
    fake.cards.map((card) => card.id),
    ["jll-investor-enumeration-0", "jll-investor-enumeration-1", "jll-investor-member-0"],
  );
  assert.match(fake.cards[2]?.url ?? "", /item-51\.json/);
});

test("Colliers requires sealed map/list parity before its exact SLP member request", async () => {
  const plan: ColliersReceiptPlan = {
    engineKey: "engine", slices: [{ start: 1, pageSize: 1 }],
    members: [{ key: "colliers-3", providerId: "3", detailPv: "detail-3", canonicalUrl: "https://my.rcm1.com/slp/?pv=detail-3" }],
    enumerationCards: [],
  };
  const fake = new FixtureTransport({
    "colliers-map-enumeration-0": JSON.stringify({ projectLocations: [{ ProjectId: "3", Latitude: 1, Longitude: 2 }] }),
    "colliers-list-enumeration-0": JSON.stringify({ numProjects: 1, html: '<li class="item"><a href="/slp/?pv=detail-3"></a><span class="city">A, NY</span></li>' }),
    "colliers-member-0": JSON.stringify({ ProjectSummary: { AttributeVisibility: { ProjectId: "3" }, CanonicalUrl: "https://my.rcm1.com/slp/?pv=detail-3" }, GalleryImages: [] }),
  });
  const slice = plan.slices[0]!;
  const receiptContext = await context("colliers", [
    colliersMapEnumerationCard(plan, slice, 0),
    colliersListEnumerationCard(plan, slice, 0),
  ], fake);
  const producer = createColliersReceiptProducer(plan);
  await producer.produceEnumerationReceipt(receiptContext);
  await producer.produceMemberReceipt(receiptContext, plan.members[0]!);
  assert.deepEqual(fake.cards.map((card) => card.id), ["colliers-map-enumeration-0", "colliers-list-enumeration-0", "colliers-member-0"]);
});

test("Colliers rejects a fabricated total field without native numProjects", async () => {
  const plan: ColliersReceiptPlan = {
    engineKey: "engine", slices: [{ start: 1, pageSize: 1 }],
    members: [{ key: "colliers-3", providerId: "3", detailPv: "detail-3", canonicalUrl: "https://my.rcm1.com/slp/?pv=detail-3" }],
    enumerationCards: [],
  };
  const fake = new FixtureTransport({
    "colliers-map-enumeration-0": JSON.stringify({ projectLocations: [{ ProjectId: "3", Latitude: 1, Longitude: 2 }] }),
    "colliers-list-enumeration-0": JSON.stringify({ total: 1, html: '<li class="item"><a href="/slp/?pv=detail-3"></a><span class="city">A, NY</span></li>' }),
  });
  const slice = plan.slices[0]!;
  const receiptContext = await context("colliers", [
    colliersMapEnumerationCard(plan, slice, 0),
    colliersListEnumerationCard(plan, slice, 0),
  ], fake);
  await assert.rejects(
    createColliersReceiptProducer(plan).produceEnumerationReceipt(receiptContext),
    /source response projection failed without retry/,
  );
  assert.equal(receiptContext.transport.requestAccounting().events.at(-1)?.outcome, "rejected");
});

test("Colliers reconciles immutable members across exact map/list slices", async () => {
  const plan: ColliersReceiptPlan = {
    engineKey: "engine",
    slices: [{ start: 1, pageSize: 1 }, { start: 2, pageSize: 1 }],
    members: [
      { key: "colliers-3", providerId: "3", detailPv: "detail-3", canonicalUrl: "https://my.rcm1.com/slp/?pv=detail-3" },
      { key: "colliers-4", providerId: "4", detailPv: "detail-4", canonicalUrl: "https://my.rcm1.com/slp/?pv=detail-4" },
    ],
    enumerationCards: [],
  };
  const fake = new FixtureTransport({
    "colliers-map-enumeration-0": JSON.stringify({ projectLocations: [{ ProjectId: "3" }] }),
    "colliers-list-enumeration-0": JSON.stringify({ numProjects: 1, html: '<li class="item"><a href="/slp/?pv=detail-3"></a><span>A</span></li>' }),
    "colliers-map-enumeration-1": JSON.stringify({ projectLocations: [{ ProjectId: "4" }] }),
    "colliers-list-enumeration-1": JSON.stringify({ numProjects: 1, html: '<li class="item"><a href="/slp/?pv=detail-4"></a><span>B</span></li>' }),
    "colliers-member-0": JSON.stringify({ ProjectSummary: { AttributeVisibility: { ProjectId: "3" }, CanonicalUrl: "https://my.rcm1.com/slp/?pv=detail-3" } }),
    "colliers-member-1": JSON.stringify({ ProjectSummary: { AttributeVisibility: { ProjectId: "4" }, CanonicalUrl: "https://my.rcm1.com/slp/?pv=detail-4" } }),
  });
  const cards = plan.slices.flatMap((slice, index) => [
    colliersMapEnumerationCard(plan, slice, index),
    colliersListEnumerationCard(plan, slice, index),
  ]);
  const receiptContext = await context("colliers", cards, fake);
  const producer = createColliersReceiptProducer(plan);
  await producer.produceEnumerationReceipt(receiptContext);
  assert.equal(
    expansionParentCardId(receiptContext.store, "colliers", "colliers-member-1"),
    "colliers-list-enumeration-1",
  );
  await producer.produceMemberReceipt(receiptContext, plan.members[0]!);
  await producer.produceMemberReceipt(receiptContext, plan.members[1]!);
  assert.deepEqual(
    fake.cards.map((card) => card.id),
    [
      "colliers-map-enumeration-0",
      "colliers-list-enumeration-0",
      "colliers-map-enumeration-1",
      "colliers-list-enumeration-1",
      "colliers-member-0",
      "colliers-member-1",
    ],
  );
});

test("Marcus seals canonical search and map POST bodies without retry or fallback", async () => {
  const plan: MarcusReceiptPlan = {
    members: [{ key: "marcus-4", providerId: "4", activityId: "activity-4", canonicalUrl: "https://www.marcusmillichap.com/properties/four" }],
    enumerationCards: [],
  };
  const fake = new FixtureTransport({
    "marcus-count-enumeration": JSON.stringify({ Results: { TotalCount: 2, Properties: [{ DealId: "newest-visible-only" }] } }),
    "marcus-map-enumeration": JSON.stringify({ Results: { Properties: [{ ActivityId: "activity-new" }, { ActivityId: "activity-4" }] } }),
    "marcus-member-0": JSON.stringify({ Results: { PropertyDetail: '<article data-dealid="4" data-property="four"></article>', PropertyUrl: "/properties/four" } }),
  });
  const receiptContext = await context("marcus-millichap", [marcusCountEnumerationCard(), marcusMapEnumerationCard()], fake);
  const producer = createMarcusReceiptProducer(plan);
  await producer.produceEnumerationReceipt(receiptContext);
  await producer.produceMemberReceipt(receiptContext, plan.members[0]!);
  assert.match(fake.cards[0]?.body ?? "", /"pageSize":1/);
  assert.equal(fake.cards[1]?.url, "https://www.marcusmillichap.com/api/contentsearch/mapproperties");
  assert.equal(fake.cards[2]?.body, '{"activityId":"activity-4"}');
  assert.equal(fake.cards[2]?.url, "https://www.marcusmillichap.com/api/contentsearch/mappropertydetail");
  assert.equal(receiptContext.transport.requestAccounting().retries, 0);
});

test("Marcus rejects a map detail whose native DealId does not match the selected member", async () => {
  const plan: MarcusReceiptPlan = {
    members: [{ key: "marcus-4", providerId: "4", activityId: "activity-4", canonicalUrl: "https://www.marcusmillichap.com/properties/four" }],
    enumerationCards: [],
  };
  const fake = new FixtureTransport({
    "marcus-count-enumeration": JSON.stringify({ Results: { TotalCount: 1, Properties: [{ DealId: "4" }] } }),
    "marcus-map-enumeration": JSON.stringify({ Results: { Properties: [{ ActivityId: "activity-4" }] } }),
    "marcus-member-0": JSON.stringify({ Results: { PropertyDetail: '<article data-dealid="different-deal"></article>', PropertyUrl: "/properties/four" } }),
  });
  const receiptContext = await context("marcus-millichap", [marcusCountEnumerationCard(), marcusMapEnumerationCard()], fake);
  const producer = createMarcusReceiptProducer(plan);
  await producer.produceEnumerationReceipt(receiptContext);
  await assert.rejects(
    producer.produceMemberReceipt(receiptContext, plan.members[0]!),
    /source response projection failed without retry/,
  );
  assert.equal(receiptContext.transport.requestAccounting().events.at(-1)?.outcome, "rejected");
});

test("Marcus rejects the old mapproperties envelope at the detail endpoint", async () => {
  const plan: MarcusReceiptPlan = {
    members: [{ key: "marcus-4", providerId: "4", activityId: "activity-4", canonicalUrl: "https://www.marcusmillichap.com/properties/four" }],
    enumerationCards: [],
  };
  const fake = new FixtureTransport({
    "marcus-count-enumeration": JSON.stringify({ Results: { TotalCount: 1, Properties: [{ DealId: "4" }] } }),
    "marcus-map-enumeration": JSON.stringify({ Results: { Properties: [{ ActivityId: "activity-4" }] } }),
    "marcus-member-0": JSON.stringify({ Results: { Properties: [{ ActivityId: "activity-4", PropertyUrl: "/properties/four" }] } }),
  });
  const receiptContext = await context("marcus-millichap", [marcusCountEnumerationCard(), marcusMapEnumerationCard()], fake);
  const producer = createMarcusReceiptProducer(plan);
  await producer.produceEnumerationReceipt(receiptContext);
  await assert.rejects(
    producer.produceMemberReceipt(receiptContext, plan.members[0]!),
    /source response projection failed without retry/,
  );
  assert.equal(receiptContext.transport.requestAccounting().events.at(-1)?.outcome, "rejected");
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

test("strict-detail common wrapper rejects a mismatched transport before enumeration", async () => {
  const producer = createJllReceiptProducer({
    enumerations: [{ transaction: "sale", propertyType: "office", page: 1 }],
    members: [{ key: "jll-1", providerId: "1", canonicalUrl: "https://property.jll.com/listings/office-1" }],
    enumerationCards: [],
  });
  const fake = new FixtureTransport({});
  const wrong = new SourceBoundOneShotTransport("wrong-source", binding, allowlistedCards("wrong-source", [{
    id: "wrong-enum", sourceKey: "wrong-source", stage: "enumeration", method: "GET",
    url: "https://example.test/enumeration", allowedHost: "example.test", headers: {},
    contentType: null, body: null, cacheMode: "no-store", timeoutMs: 1_000, maxBytes: 64,
  }]), new MemoryReceiptStore(), fake);
  await assert.rejects(producer.produceEnumerationReceipt({ transport: wrong }), /source binding mismatch/);
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
