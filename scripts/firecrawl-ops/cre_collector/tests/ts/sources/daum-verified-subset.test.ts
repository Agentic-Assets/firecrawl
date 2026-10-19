import "../../fixtures/daum-offline-deny.mjs";
import test, { type TestContext } from "node:test";
import assert from "node:assert/strict";
import { load } from "cheerio";
import { readFileSync } from "node:fs";
import { parseDaumDetail, srcDaumCommercial } from "../../../sources/daum-commercial.js";
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

test("saved 52023 visible empty title is only a V2 identity observation, never head-title facts", () => {
  const body = readFileSync(new URL("../../fixtures/daum-52023-empty-title.html", import.meta.url), "utf8");
  const url = "https://daumcommercial.com/property/27778-avenue-hopkins-valencia-ca-91355/";
  assert.throws(() => parseDaumDetail(body, url), /omitted its property title/);
  const observed = parseDaumDetail(body, url, true);
  assert.deepEqual(observed, { postId: 52023, observedCanonicalUrl: url,
    tenure: "lease", tenureText: "Lease", emptyRequiredTitle: true });
  assert.equal("title" in observed, false);
  assert.equal("city" in observed, false);
});

// Without the opt-in branch the real strict producer rejects the first hold.
test("subset exhaustively proves 562 members and emits 560 with two typed holds", async t => {
  const state = offline(t, 562);
  const progress: string[] = [];
  t.mock.method(console, "error", (...args: unknown[]) => { progress.push(args.join(" ")); });
  state.unknownInventory = new Set([7, 293]); state.unknownDetail = new Set([7, 293]);
  const sale = await srcDaumCommercial("sale", Infinity, false);
  const lease = await srcDaumCommercial("lease", Infinity, false);
  assert.equal(progress.length, 9, "two transactions must not duplicate progress or log every member");
  assert.equal(progress.at(-1), "  daum-commercial: detail acquisition finished; checked=562/562");
  assert.doesNotMatch(progress.join("\n"), /https?:|Synthetic|verified|ingest|database|held.*accepted/i);
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

test("v2 holds an identity-proved empty visible title without erasing recognized Lease", async t => {
  const state = offline(t, 102);
  process.env.DAUM_SUBSET_POLICY = "tenure-and-empty-title-v2";
  state.unknownInventory.add(101); state.unknownDetail.add(101);
  state.detailFault = (id, body) => id === 100
    ? body.replace(/class="title-property">[^<]*</, 'class="title-property">   <') : body;
  const sale = await srcDaumCommercial("sale", Infinity, false);
  const lease = await srcDaumCommercial("lease", Infinity, false);
  const proof: any = sale.currentInventoryProof;
  assert.equal(proof.schema, "daum-verified-subset-v2");
  assert.equal(proof.subsetPolicy, "tenure-and-empty-title-v2");
  assert.deepEqual(proof.titleObservations, [["50100", 1, "empty"]]);
  assert.deepEqual(proof.held, [["50100", "empty_required_property_title"], ["50101", "unknown_inventory_and_detail_tenure"]]);
  assert.equal(proof.cohort.find((row: string[]) => row[0] === "50100")[7], "lease");
  assert.deepEqual({ ...proof, transaction: "lease" }, lease.currentInventoryProof);
  const emitted = [...sale.listings, ...lease.listings];
  assert.equal(new Set(emitted.map(row => row.daumPostId)).size, 100);
  assert.equal(emitted.some(row => ["50100", "50101"].includes(String(row.daumPostId))), false);
  assert.equal(state.gets.length, 102);
});

for (const fault of ["missing-title", "multiple-title", "missing-canonical", "duplicate-canonical", "wrong-canonical", "duplicate-shortlink", "tenure-conflict", "native-collision"] as const) {
  test(`v2 empty-title hold cannot mask ${fault}`, async t => {
    const state = offline(t, 101);
    process.env.DAUM_SUBSET_POLICY = "tenure-and-empty-title-v2";
    state.detailFault = (id, body) => {
      if (id !== 100) return body;
      const $ = load(body);
      $(".title-property").text("");
      if (fault === "missing-title") $(".title-property").remove();
      if (fault === "multiple-title") $(".title-property").clone().appendTo(".main-section");
      if (fault === "missing-canonical") $('link[rel="canonical"]').remove();
      if (fault === "duplicate-canonical") $('link[rel="canonical"]').clone().appendTo("head");
      if (fault === "wrong-canonical") $('link[rel="canonical"]').attr("href", "https://daumcommercial.com/property/Synthetic-0/");
      if (fault === "duplicate-shortlink") $('link[rel="shortlink"]').clone().appendTo("head");
      if (fault === "tenure-conflict") $(".title-about").text("Sale");
      if (fault === "native-collision") $('link[rel="shortlink"]').attr("href", "https://daumcommercial.com/?p=50000");
      return $.html();
    };
    await assert.rejects(srcDaumCommercial("sale", Infinity, false));
  });
}

test("v2 empty-title reason takes priority over unknown tenure and does not leak across policy caches", async t => {
  const state = offline(t, 101);
  state.unknownInventory.add(100); state.unknownDetail.add(100);
  state.detailFault = (id, body) => id === 100 ? body.replace(/class="title-property">[^<]*</, 'class="title-property"> <') : body;
  process.env.DAUM_SUBSET_POLICY = "tenure-and-empty-title-v2";
  const result = await srcDaumCommercial("sale", Infinity, false);
  assert.deepEqual((result.currentInventoryProof as any).held, [["50100", "empty_required_property_title"]]);
  const posts = state.posts;
  delete process.env.DAUM_SUBSET_POLICY;
  await assert.rejects(srcDaumCommercial("sale", Infinity, false), /omitted its property title/);
  assert.ok(state.posts > posts);
  delete process.env.DAUM_VERIFIED_SUBSET;
  await assert.rejects(srcDaumCommercial("sale", Infinity, false), /omitted its property title/);
  const afterStrict = state.posts;
  process.env.DAUM_SUBSET_POLICY = "tenure-and-empty-title-v2";
  await assert.rejects(srcDaumCommercial("sale", Infinity, false), /requires.*subset/i);
  assert.equal(state.posts, afterStrict, "invalid policy configuration refuses before transport");
});

test("v2 preserves the existing full parser for nonempty multiple title containers", async t => {
  const state = offline(t, 100);
  process.env.DAUM_SUBSET_POLICY = "tenure-and-empty-title-v2";
  state.detailFault = (id, body) => {
    if (id !== 0) return body;
    const $ = load(body); $(".title-property").clone().appendTo(".main-section"); return $.html();
  };
  const result = await srcDaumCommercial("sale", Infinity, false);
  assert.equal(result.currentInventoryProof?.schema, "daum-verified-subset-v2");
  assert.deepEqual((result.currentInventoryProof as any).held, []);
  assert.ok(result.listings.some(row => row.daumPostId === "50000"));
});
