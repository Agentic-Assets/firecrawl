import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { createHash } from "node:crypto";
import { captureLyonSearch, projectLyonSearch } from "../../../sources/lyon-search-observation.js";
import { LYON_SEARCH_URLS, lyonStahlFetchText } from "../../../sources/lyon-stahl.js";
import { NativeEvidenceTerminalError } from "../../../lib/source-evidence/terminal-error.js";

async function withClock<T>(t: TestContext, run: () => Promise<T>): Promise<T> {
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now: 100_000 });
  t.mock.method(performance, "now", () => Date.now());
  let settled = false;
  const result = run();
  void result.then(() => { settled = true; }, () => { settled = true; });
  for (let i = 0; !settled && i < 2000; i++) {
    await new Promise<void>(resolve => setImmediate(resolve));
    if (!settled) t.mock.timers.tick(250);
  }
  assert.ok(settled);
  return result;
}

const observedAt = "2026-11-05T06:00:00.000Z";
const base = "https://lyonstahl.com/properties/";
const card = (slug: string, fields: object = {}) => ({ permalink: `${base}${slug}/`, status: null, ...fields });

test("keeps the union, each raw occurrence and conflicting sale/lease values without inferring status", () => {
  const all = JSON.stringify([card("one", { sale_price: "$1,000,000", address: "One" }), card("all-only")]);
  const sale = JSON.stringify([card("one", { sale_price: "$900,000" }), card("sale-only")]);
  const lease = JSON.stringify([card("one", { sale_price: "$2.95/RSF NNN monthly" })]);
  const result = projectLyonSearch([
    { filter: "all", observedAt, responseText: all },
    { filter: "sale", observedAt, responseText: sale },
    { filter: "lease", observedAt, responseText: lease },
  ]);
  assert.equal(result.coverage, "partial_unknown");
  assert.equal(result.candidates.length, 3);
  assert.deepEqual(result.candidates[0].occurrences, [
    { filter: "all", index: 0 }, { filter: "sale", index: 0 }, { filter: "lease", index: 0 },
  ]);
  assert.equal(result.snapshots[2].responseText, lease);
  assert.equal(result.snapshots[2].responseTextSha256, createHash("sha256").update(lease).digest("hex"));
  assert.equal("status" in result.candidates[0], false);
  assert.equal("salePriceUsd" in result.candidates[0], false);
});

test("holds invalid card occurrences and flags duplicate identities without guessing aliases", () => {
  const raw = JSON.stringify([card("one"), card("one"), card("one").permalink, { permalink: "https://evil.example/properties/one/" }, { permalink: `${base}one` }]);
  const result = projectLyonSearch([{ filter: "all", observedAt, responseText: raw }]);
  assert.equal(result.candidates.length, 2);
  assert.ok(result.candidates.every(row => row.identityConflict));
  assert.equal(result.held.length, 2);
  assert.equal(result.snapshots[0].responseText, raw);
});

test("retains query-form source post IDs without inventing a property-path crosswalk", () => {
  const query = "https://lyonstahl.com/?post_type=properties&p=40201";
  const raw = JSON.stringify([{ permalink: query, title: "314 N Wilmington Blvd & 915 W C St", status: null },
    { permalink: "https://lyonstahl.com/?post_type=properties&p=40201&p=3" }]);
  const result = projectLyonSearch([{ filter: "all", observedAt, responseText: raw },
    { filter: "sale", observedAt, responseText: JSON.stringify([{ permalink: query }]) }]);
  assert.deepEqual(result.candidates[0].sourceIdentity, { kind: "wordpress_post", value: "40201" });
  assert.equal(result.candidates[0].url, query);
  assert.deepEqual(result.candidates[0].occurrences, [{ filter: "all", index: 0 }, { filter: "sale", index: 0 }]);
  assert.equal(result.held.length, 1);
  assert.equal(result.snapshots[0].responseText, raw);
});

test("refuses duplicate filters, invalid clocks and non-array responses", () => {
  const page = { filter: "all" as const, observedAt, responseText: "[]" };
  assert.throws(() => projectLyonSearch([page, page]));
  assert.throws(() => projectLyonSearch([{ ...page, observedAt: "invalid" }]));
  assert.throws(() => projectLyonSearch([{ ...page, responseText: "{}" }]));
});

test("search transport uses only the three exact registered GET URLs and refuses redirects", async () => {
  for (const url of Object.values(LYON_SEARCH_URLS)) {
    const raw = await lyonStahlFetchText(url, "search", async (input, init) => {
      assert.equal(String(input), url);
      assert.equal(init?.redirect, "manual");
      return new Response("[]", { headers: { "content-type": "application/json; charset=utf-8" } });
    }, 1000, 1024, 1);
    assert.equal(raw, "[]");
  }
  let called = false;
  await assert.rejects(lyonStahlFetchText(`${LYON_SEARCH_URLS.all}&unexpected=1`, "search", async () => { called = true; return new Response(); }, 1000, 1024, 1), /unsafe/);
  assert.equal(called, false);
  await assert.rejects(lyonStahlFetchText(LYON_SEARCH_URLS.all, "search", async () => new Response(null, { status: 302, headers: { location: LYON_SEARCH_URLS.sale } }), 1000, 1024, 1), /redirect refused/);
  await assert.rejects(lyonStahlFetchText(LYON_SEARCH_URLS.all, "search", async () => new Response("[]", { headers: { "content-type": "text/html" } }), 1000, 1024, 1), /not JSON/);
});

test("search header timeout stops on unsettled fetch and safely rejects a settled abort", async () => {
  let calls = 0;
  await assert.rejects(lyonStahlFetchText(LYON_SEARCH_URLS.all, "search", async () => {
    calls++;
    return new Promise<Response>(() => {});
  }, 20, 1024, 1), error => error instanceof NativeEvidenceTerminalError && /did not settle/.test(error.message));
  assert.equal(calls, 1);
  await assert.rejects(lyonStahlFetchText(LYON_SEARCH_URLS.all, "search", async (_url, init) =>
    new Promise<Response>((_resolve, reject) => init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true })), 20, 1024, 1), /deadline exceeded/);
  let cancellations = 0;
  await assert.rejects(lyonStahlFetchText(LYON_SEARCH_URLS.all, "search", async () => {
    await new Promise(resolve => setTimeout(resolve, 35));
    return new Response(new ReadableStream({ cancel() { cancellations++; return Promise.reject(Error("unsettled")); } }), { headers: { "content-type": "application/json" } });
  }, 20, 1024, 1), error => error instanceof NativeEvidenceTerminalError && /late search response/.test(error.message));
  assert.equal(cancellations, 1);
});

test("bounded search uses one ten-second gate and keeps prior pages on settled failure", async (t) => {
  const starts: number[] = [];
  const page = JSON.stringify([card("one", { sale_price: "$2.95/RSF NNN monthly" })]);
  const result = await withClock(t, () => captureLyonSearch(async () => {
    starts.push(Date.now());
    return new Response(page, { headers: { "content-type": "application/json" } });
  }));
  assert.deepEqual(starts, [100_000, 110_000, 120_000]);
  assert.equal(result.candidates.length, 1);
  assert.equal(result.snapshots.length, 3);
  assert.equal(result.coverage, "partial_unknown");
});

test("bounded search preserves earlier pages after a settled failure and stops", async (t) => {
  let requests = 0;
  await assert.rejects(withClock(t, () => captureLyonSearch(async () => {
    requests++;
    if (requests === 2) return new Response("unavailable", { status: 503 });
    return new Response("[]", { headers: { "content-type": "application/json" } });
  })), (error: any) => {
    assert.equal(error.retainedObservations[0].coverage, "partial_unknown");
    assert.equal(error.retainedObservations[0].pages.length, 1);
    assert.equal(error.retainedObservations[0].pages[0].responseText, "[]");
    return true;
  });
  assert.equal(requests, 2);
});

test("bounded search retains the malformed response without requesting later filters", async (t) => {
  let requests = 0;
  await assert.rejects(withClock(t, () => captureLyonSearch(async () => {
    requests++;
    return new Response(requests === 1 ? "[]" : "{broken", { headers: { "content-type": "application/json" } });
  })), (error: any) => {
    assert.equal(error.retainedObservations[0].pages.length, 2);
    assert.equal(error.retainedObservations[0].pages[1].responseText, "{broken");
    return true;
  });
  assert.equal(requests, 2);
});

test("unsettled search response cleanup remains terminal and stops dispatch", async (t) => {
  let requests = 0;
  await assert.rejects(withClock(t, () => captureLyonSearch(async () => {
    requests++;
    if (requests === 1) return new Response("[]", { headers: { "content-type": "application/json" } });
    return new Response(new ReadableStream({ cancel() { return Promise.reject(Error("unsettled")); } }), { status: 503 });
  })), error => error instanceof NativeEvidenceTerminalError);
  assert.equal(requests, 2);
});
