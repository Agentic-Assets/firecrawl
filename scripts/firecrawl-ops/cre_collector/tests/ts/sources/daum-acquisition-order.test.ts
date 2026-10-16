import "../../fixtures/daum-offline-deny.mjs";
import test from "node:test";
import assert from "node:assert/strict";
import { load } from "cheerio";
import { srcDaumCommercial } from "../../../sources/daum-commercial.js";
import { observationNow } from "../../../lib/observation-clock.js";
import { daumProofPage, daumProofDetail } from "../../fixtures/daum-proof-pages.js";

test("strict acquisition prioritizes unknowns without changing identity, ordinals or output", async t => {
  const saved = { ...process.env }, fetch = globalThis.fetch, timer = globalThis.setTimeout;
  t.after(() => {
    for (const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key];
    Object.assign(process.env, saved); globalThis.fetch = fetch; globalThis.setTimeout = timer;
  });
  Object.assign(process.env, { CRE_REQUIRE_FRESH_DETAILS: "1", CRE_REFRESH_STARTED_AT: observationNow() });
  globalThis.setTimeout = ((fn: any, ms?: number, ...args: any[]) => timer(fn, ms && ms > 0 && ms <= 3000 ? 0 : ms, ...args)) as typeof setTimeout;
  let unknown = new Set<number>(), unresolved = new Set<number>(), gets: number[] = [], posts = 0;
  globalThis.fetch = async (input, init) => {
    const url = String(input);
    const page = url.match(/^https:\/\/daumcommercial[.]com\/property-search\/(?:page\/(\d+)\/)?$/);
    if (init?.method === "POST" && page) {
      posts++;
      const $ = load(daumProofPage(300, Number(page[1] || 1)));
      $(".item").each((_, item) => {
        const id = Number($(item).find("a.photo").attr("href")?.match(/Synthetic-(\d+)/)?.[1]);
        if (unknown.has(id)) $(item).find(".listing-type").text("");
      });
      return new Response($.html());
    }
    const detail = url.match(/^https:\/\/daumcommercial[.]com\/property\/Synthetic-(\d+)\/$/);
    if (init?.method !== "GET" || !detail) throw Error("UNEXPECTED_FAKE_NO_FALLBACK");
    const id = Number(detail[1]); gets.push(id);
    const $ = load(daumProofDetail(id));
    if (unresolved.has(id)) $(".title-about").text("");
    return new Response($.html());
  };
  await t.test("unresolved original ordinal294 fails first and both transactions share rejection", async () => {
    process.env.CRE_REFRESH_GENERATION = "order-unresolved"; unknown = new Set([293]); unresolved = new Set([293]);
    const results = await Promise.allSettled([srcDaumCommercial("sale", Infinity, false), srcDaumCommercial("lease", Infinity, false)]);
    assert.equal(results[0].status, "rejected"); assert.equal(results[1].status, "rejected");
    if (results[0].status !== "rejected" || results[1].status !== "rejected") throw Error("missing failure");
    const originalError = results[0].reason;
    assert.strictEqual(originalError, results[1].reason);
    assert.match(results[0].reason.message, /ordinal=294\/300/);
    assert.deepEqual(gets, [293]); assert.equal(posts, 8);
    await assert.rejects(srcDaumCommercial("lease", Infinity, false), error => error === originalError);
    assert.deepEqual(gets, [293]);
  });
  await t.test("new generation resolves two stable unknowns and restores full output/proof order", async () => {
    process.env.CRE_REFRESH_GENERATION = "order-resolved"; unknown = new Set([7, 293]); unresolved.clear(); gets = []; posts = 0;
    const [sale, lease] = await Promise.all([srcDaumCommercial("sale", Infinity, false), srcDaumCommercial("lease", Infinity, false)]);
    const original = Array.from({ length: 300 }, (_, i) => i);
    assert.deepEqual(gets, [7, 293, ...original.filter(i => !unknown.has(i))]); assert.equal(posts, 8);
    for (const [result, tx] of [[sale, "sale"], [lease, "lease"]] as const) {
      assert.deepEqual(result.listings.map(row => row.id), original.filter(i => tx === "sale" ? i % 3 !== 1 : i % 3 !== 0).map(i => `property/Synthetic-${i}`));
      const proof = result.currentInventoryProof as { cohort: unknown[][]; details: unknown[][] };
      assert.equal(proof.cohort.length, 300); assert.equal(proof.details.length, 300);
      assert.deepEqual(proof.details.map(row => row[0]), proof.cohort.map(row => row[0]));
    }
  });
  await t.test("all-known inventory keeps original request order", async () => {
    process.env.CRE_REFRESH_GENERATION = "order-known"; unknown.clear(); gets = [];
    await srcDaumCommercial("sale", Infinity, false);
    assert.deepEqual(gets, Array.from({ length: 300 }, (_, i) => i));
  });
});
