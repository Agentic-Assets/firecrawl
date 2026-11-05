import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { createHash } from "node:crypto";
import { captureLyonSearch, projectLyonSearch } from "../../../sources/lyon-search-observation.js";
import { LYON_SEARCH_URLS, lyonStahlFetchText, lyonStahlPacedFetch } from "../../../sources/lyon-stahl.js";
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

test("retains same-URL duplicates without marking an identity conflict", () => {
  const raw = JSON.stringify([card("same"), card("same"), { ...card("same"), title: "Changed" }, card("peer")]);
  const result = projectLyonSearch([{ filter: "all", observedAt, responseText: raw }]);
  assert.deepEqual(result.candidates.map(row => row.identityConflict), [false, false]);
  assert.equal(result.candidates[0].occurrences.length, 3);
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

test("search shares the invocation gate with a detail request", async (t) => {
  const starts: number[] = [];
  const page = JSON.stringify([card("one", { sale_price: "$2.95/RSF NNN monthly" })]);
  const result = await withClock(t, async () => {
    const paced = lyonStahlPacedFetch(async input => {
      starts.push(Date.now());
      return String(input).includes("admin-ajax")
        ? new Response(page, { headers: { "content-type": "application/json" } })
        : new Response("<html></html>", { headers: { "content-type": "text/html" } });
    });
    await lyonStahlFetchText(`${base}one/`, "html", paced, 60_000, 1024, 1);
    return captureLyonSearch(paced);
  });
  assert.deepEqual(starts, [100_000, 110_000, 120_000, 130_000]);
  assert.equal(result.candidates.length, 1);
  assert.equal(result.snapshots.length, 3);
  assert.equal(result.coverage, "partial_unknown");
});

test("bounded search keeps eligible filters after a settled local failure", async (t) => {
  let requests = 0;
  const result = await withClock(t, () => captureLyonSearch(lyonStahlPacedFetch(async () => {
    requests++;
    if (requests === 2) return new Response("unavailable", { status: 503 });
    return new Response("[]", { headers: { "content-type": "application/json" } });
  })));
  assert.equal(result.coverage, "partial_unknown");
  assert.equal(result.snapshots.length, 2);
  assert.deepEqual(result.failures, [{ filter: "sale", reason: "acquisition_failed", status: 503 }]);
  assert.equal(result.snapshots[1].filter, "lease");
  assert.equal(requests, 3);
});

test("bounded search retains malformed response and continues later filters", async (t) => {
  let requests = 0;
  const result = await withClock(t, () => captureLyonSearch(lyonStahlPacedFetch(async () => {
    requests++;
    return new Response(requests === 2 ? "{broken" : "[]", { headers: { "content-type": "application/json" } });
  })));
  assert.equal(result.snapshots[1].responseText, "{broken");
  assert.deepEqual(result.failures, [{ filter: "sale", reason: "invalid_response" }]);
  assert.equal(result.snapshots[2].filter, "lease");
  assert.equal(requests, 3);
});

test("unsettled search response cleanup remains terminal and stops dispatch", async (t) => {
  let requests = 0;
  await assert.rejects(withClock(t, () => captureLyonSearch(lyonStahlPacedFetch(async () => {
    requests++;
    if (requests === 1) return new Response("[]", { headers: { "content-type": "application/json" } });
    return new Response(new ReadableStream({ cancel() { return Promise.reject(Error("unsettled")); } }), { status: 503 });
  }))), (error: any) => {
    assert.ok(error instanceof NativeEvidenceTerminalError);
    const retained = (error as NativeEvidenceTerminalError & { retainedObservations: any[] }).retainedObservations;
    assert.equal(retained[0].snapshots[0].responseText, "[]");
    assert.equal(retained[0].coverage, "partial_unknown");
    return true;
  });
  assert.equal(requests, 2);
});

for (const status of [401, 403, 429]) test(`search stops after HTTP ${status} without dispatching later filters`, async (t) => {
  let requests = 0;
  const result = await withClock(t, () => captureLyonSearch(lyonStahlPacedFetch(async () => {
    requests++;
    return requests === 2 ? new Response("denied", { status }) : new Response("[]", { headers: { "content-type": "application/json" } });
  })));
  assert.equal(requests, 2);
  assert.equal(result.stopped, true);
  assert.deepEqual(result.failures, [{ filter: "sale", reason: "access_stopped", status }]);
  assert.equal(result.snapshots[0].responseText, "[]");
});

for (const kind of ["retry-after", "redirect", "non-json"] as const) test(`search stops on ${kind} response`, async (t) => {
  let requests = 0;
  const result = await withClock(t, () => captureLyonSearch(lyonStahlPacedFetch(async () => {
    requests++;
    if (requests === 1) return new Response("[]", { headers: { "content-type": "application/json" } });
    if (kind === "retry-after") return new Response("later", { status: 503, headers: { "retry-after": "60" } });
    if (kind === "redirect") return new Response(null, { status: 302, headers: { location: LYON_SEARCH_URLS.lease } });
    return new Response("<html>challenge</html>", { headers: { "content-type": "text/html" } });
  })));
  assert.equal(requests, 2);
  assert.equal(result.stopped, true);
  assert.deepEqual(result.failures, [{ filter: "sale", reason: "access_stopped", status: kind === "retry-after" ? 503 : kind === "redirect" ? 302 : 200 }]);
});

test("search refuses a raw ungated fetch", async () => {
  await assert.rejects(captureLyonSearch(fetch), /invocation-owned paced fetch/);
});
