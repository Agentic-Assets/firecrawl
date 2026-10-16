import "../../fixtures/daum-offline-deny.mjs";
import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";
import { load } from "cheerio";
import { srcDaumCommercial } from "../../../sources/daum-commercial.js";
import { observationNow } from "../../../lib/observation-clock.js";
import { daumProofPage, daumProofDetail } from "../../fixtures/daum-proof-pages.js";

function offline(t: TestContext, total = 100, allSale = false) {
  const saved = { ...process.env }, fetch = globalThis.fetch, timer = globalThis.setTimeout;
  t.after(() => {
    for (const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key];
    Object.assign(process.env, saved); globalThis.fetch = fetch; globalThis.setTimeout = timer;
  });
  Object.assign(process.env, { CRE_REQUIRE_FRESH_DETAILS: "1", CRE_REFRESH_GENERATION: t.name, CRE_REFRESH_STARTED_AT: observationNow(), DAUM_VERIFIED_SUBSET: "1" });
  globalThis.setTimeout = ((fn: any, ms?: number, ...args: any[]) => timer(fn, ms && ms > 0 && ms <= 3000 ? 0 : ms, ...args)) as typeof setTimeout;
  const state = {
    unknownInventory: new Set<number>(), unknownDetail: new Set<number>(),
    nativeAliases: new Map<number, number>(), gets: [] as number[], posts: 0,
    detailFault: (_id: number, body: string) => body,
    pageFault: (body: string) => body,
  };
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    const page = url.match(/^https:\/\/daumcommercial[.]com\/property-search\/(?:page\/(\d+)\/)?$/);
    if (init?.method === "POST" && page) {
      state.posts++;
      const $ = load(daumProofPage(total, Number(page[1] || 1), allSale));
      $(".item").each((_, item) => {
        const id = Number($(item).find("a.photo").attr("href")?.match(/Synthetic-(\d+)/)?.[1]);
        if (state.unknownInventory.has(id)) $(item).find(".listing-type").text("");
      });
      return new Response(state.pageFault($.html()));
    }
    const detail = url.match(/^https:\/\/daumcommercial[.]com\/property\/Synthetic-(\d+)\/$/);
    if (init?.method !== "GET" || !detail) throw Error("UNEXPECTED_FAKE_NO_FALLBACK");
    const id = Number(detail[1]); state.gets.push(id);
    const $ = load(daumProofDetail(id, allSale));
    if (state.unknownDetail.has(id)) $(".title-about").text("");
    if (state.nativeAliases.has(id)) $('link[rel="shortlink"]').attr("href", `https://daumcommercial.com/?p=${50000 + state.nativeAliases.get(id)!}`);
    return new Response(state.detailFault(id, $.html()));
  };
  return state;
}

// Without the opt-in branch the real strict producer rejects the first hold.
test("subset exhaustively proves 562 members and emits 560 with two typed holds", async t => {
  const state = offline(t, 562);
  state.unknownInventory = new Set([7, 293]); state.unknownDetail = new Set([7, 293]);
  const sale = await srcDaumCommercial("sale", Infinity, false);
  const lease = await srcDaumCommercial("lease", Infinity, false);
  const proof = sale.currentInventoryProof;
  assert.equal(proof?.schema, "daum-verified-subset-v1");
  if (proof?.schema !== "daum-verified-subset-v1") throw Error("missing subset proof");
  assert.equal(proof.kind, "verified_subset_with_holds");
  assert.equal(proof.total, 562); assert.equal(proof.cohort.length, 562); assert.equal(proof.details.length, 562);
  assert.deepEqual(proof.held, [["50293", "unknown_inventory_and_detail_tenure"], ["50007", "unknown_inventory_and_detail_tenure"]]);
  assert.equal(new Set([...sale.listings, ...lease.listings].map(row => row.id)).size, 560);
  assert.equal(sale.listings.length, 374); assert.equal(lease.listings.length, 372);
  assert.equal(sale.totalAvailable, 374); assert.equal(lease.totalAvailable, 372);
  assert.equal(sale.truncated, true); assert.equal(lease.truncated, true);
  assert.equal(state.posts, 14); assert.equal(state.gets.length, 562);
  assert.deepEqual(state.gets.slice(0, 4), [7, 293, 0, 1]);
  assert.deepEqual(proof.details.map(row => row[0]), proof.cohort.map(row => row[0]));
  assert.equal(proof.cohort.filter(row => row[7] === "unknown").length, 2);
  const emitted = Array.from({ length: 562 }, (_, index) => index).filter(index => index !== 7 && index !== 293);
  assert.deepEqual(sale.listings.map(row => row.id), emitted.filter(index => index % 3 !== 1).map(index => `property/Synthetic-${index}`));
  assert.deepEqual(lease.listings.map(row => row.id), emitted.filter(index => index % 3 !== 0).map(index => `property/Synthetic-${index}`));
  assert.deepEqual(proof.cohort.map(row => row[1]), proof.cohort.map(row => row[1]).sort());
  assert.deepEqual(proof.held, (lease.currentInventoryProof as typeof proof).held);
});

test("zero emitted lease with holds remains a subset scope", async t => {
  const state = offline(t, 100, true);
  state.unknownInventory.add(99); state.unknownDetail.add(99);
  const lease = await srcDaumCommercial("lease", Infinity, false);
  assert.equal(lease.listings.length, 0); assert.equal(lease.totalAvailable, 0); assert.equal(lease.truncated, true);
  assert.equal(lease.currentInventoryProof?.schema, "daum-verified-subset-v1");
});

test("mode separated success and rejection receipts never borrow", async t => {
  const state = offline(t);
  state.unknownInventory.add(99); state.unknownDetail.add(99);
  delete process.env.DAUM_VERIFIED_SUBSET;
  let rejection: unknown;
  await assert.rejects(srcDaumCommercial("sale", Infinity, false), error => { rejection = error; return true; });
  const firstPosts = state.posts;
  process.env.DAUM_VERIFIED_SUBSET = "1";
  const subset = await srcDaumCommercial("sale", Infinity, false);
  assert.equal(subset.currentInventoryProof?.schema, "daum-verified-subset-v1");
  assert.equal(state.posts, firstPosts + 4);
  const posts = state.posts;
  delete process.env.DAUM_VERIFIED_SUBSET;
  await assert.rejects(srcDaumCommercial("lease", Infinity, false), error => error === rejection);
  assert.equal(state.posts, posts, "mode switching preserves the original rejection without reacquiring");
  process.env.DAUM_VERIFIED_SUBSET = "1";
  const cached = await srcDaumCommercial("lease", Infinity, false);
  assert.equal(cached.currentInventoryProof?.schema, "daum-verified-subset-v1"); assert.equal(state.posts, posts);
  state.unknownInventory.clear(); state.unknownDetail.clear();
  process.env.CRE_REFRESH_GENERATION += "-next";
  const noHolds = await srcDaumCommercial("sale", Infinity, false);
  assert.equal(noHolds.currentInventoryProof?.schema, "daum-verified-subset-v1"); assert.equal(noHolds.truncated, false);
  assert.equal(state.posts, posts + 4);
  delete process.env.DAUM_VERIFIED_SUBSET;
  const strict = await srcDaumCommercial("sale", Infinity, false);
  assert.equal(strict.currentInventoryProof?.schema, "daum-strict-detail-v1"); assert.equal(state.posts, posts + 8);
  process.env.DAUM_VERIFIED_SUBSET = "1";
  process.env.CRE_REFRESH_STARTED_AT = observationNow();
  await srcDaumCommercial("sale", Infinity, false); assert.equal(state.posts, posts + 12);
  assert.equal((await srcDaumCommercial("sale", 1, false)).currentInventoryProof, undefined);
  assert.equal((await srcDaumCommercial("sale", 1, true)).currentInventoryProof, undefined);
});

for (const [first, second] of [[0, 3], [7, 0], [0, 7], [7, 99]]) {
  test(`all-member native collision ${first}/${second} is fatal before partition`, async t => {
    const state = offline(t);
    state.unknownInventory = new Set([7, 99]); state.unknownDetail = new Set([7, 99]);
    state.nativeAliases.set(first, second);
    await assert.rejects(srcDaumCommercial("sale", Infinity, false), /duplicate provider ID/);
  });
}

for (const fault of ["transport", "schema", "canonical", "conflict", "path"] as const) {
  test(`subset holds never mask ${fault} failure`, async t => {
    const state = offline(t);
    state.unknownInventory.add(99); state.unknownDetail.add(99);
    state.detailFault = (id, body) => {
      if (id !== 0) return body;
      if (fault === "transport") throw Error("synthetic transport refusal");
      if (fault === "schema") return body.replace(/<link rel="shortlink"[^>]+>/, "");
      if (fault === "canonical") return body.replace(/<link rel="canonical"[^>]+>/, "");
      if (fault === "conflict") return body.replace('class="title-about">Sale<', 'class="title-about">Lease<');
      return body;
    };
    if (fault === "path") state.pageFault = body => body.replaceAll("Synthetic-0/", "Synthetic-1/");
    const expected = {
      transport: /synthetic transport refusal/,
      schema: /stable WordPress shortlink ID/,
      canonical: /observed canonical identity/,
      conflict: /detail tenure lease disagrees with inventory tenure sale/,
      path: /inventory did not converge/,
    };
    await assert.rejects(srcDaumCommercial("sale", Infinity, false), expected[fault]);
  });
}


test("a rejected subset cannot poison complete success or be repaired in the same episode", async t => {
  const state = offline(t);
  state.detailFault = (id, body) => id === 0 ? body.replace(/<link rel="canonical"[^>]+>/, "") : body;
  let rejection: unknown;
  await assert.rejects(srcDaumCommercial("sale", Infinity, false), error => { rejection = error; return true; });
  state.detailFault = (_id, body) => body;
  delete process.env.DAUM_VERIFIED_SUBSET;
  assert.equal((await srcDaumCommercial("sale", Infinity, false)).currentInventoryProof?.schema, "daum-strict-detail-v1");
  const posts = state.posts;
  process.env.DAUM_VERIFIED_SUBSET = "1";
  await assert.rejects(srcDaumCommercial("lease", Infinity, false), error => error === rejection);
  assert.equal(state.posts, posts);
  process.env.CRE_REFRESH_GENERATION += "-retry";
  const success = await srcDaumCommercial("sale", Infinity, false);
  assert.equal(success.currentInventoryProof?.schema, "daum-verified-subset-v1");
  if (success.currentInventoryProof?.schema !== "daum-verified-subset-v1") throw Error("missing subset proof");
  success.currentInventoryProof.held.push(["50000", "unknown_inventory_and_detail_tenure"]);
  success.currentInventoryProof.cohort[0][0] = "tampered";
  const fresh = await srcDaumCommercial("sale", Infinity, false);
  if (fresh.currentInventoryProof?.schema !== "daum-verified-subset-v1") throw Error("missing subset proof");
  assert.deepEqual(fresh.currentInventoryProof.held, []);
  assert.equal(fresh.currentInventoryProof.cohort[0][0], "50000");
});


for (const [first, second] of [[0, 7], [7, 0], [7, 99]]) {
  test(`all-member inventory path collision ${first}/${second} is fatal before partition`, async t => {
    const state = offline(t);
    state.unknownInventory = new Set([7, 99]); state.unknownDetail = new Set([7, 99]);
    state.pageFault = body => body.replaceAll(`Synthetic-${first}/`, `Synthetic-${second}/`);
    await assert.rejects(srcDaumCommercial("sale", Infinity, false), /inventory did not converge.*duplicate URL/);
  });
}
