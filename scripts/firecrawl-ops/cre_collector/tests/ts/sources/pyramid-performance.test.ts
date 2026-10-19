import assert from "node:assert/strict";
import test from "node:test";
import {
  createPerformanceRecorder, recordDirectProviderAttemptStarted,
  recordDirectProviderAttemptCompleted, resetPerformanceRecorderForTests,
  setPerformanceRecorderForTests, withPerformanceSource,
  type PerformanceFileSystem, type DirectProviderSource, type DirectProviderRequestRole,
} from "../../../lib/performance.js";
import { pyramidFetch, PYRAMID_INVENTORY_URL } from "../../../sources/pyramid-brokerage.js";

// In-memory optional recorder: no network or source output files.
function fixture(t: { after: (fn: () => void) => void }, failWrites = false) {
  const files = new Map<string, string>();
  const clock = { value: 0 };
  const warnings: string[] = [];
  const fs: PerformanceFileSystem = {
    kind: path => files.has(path) ? "regular" : "missing",
    openExclusive: path => { files.set(path, ""); return path; },
    writeOpened: (handle, text) => {
      if (failWrites) throw new Error("sensitive optional telemetry failure");
      files.set(String(handle), text);
    },
    closeOpened: () => {},
    rename: (from, to) => { files.set(to, files.get(from)!); files.delete(from); },
    unlink: path => { files.delete(path); },
  };
  const recorder = createPerformanceRecorder({
    path: "/metrics/performance.json", runId: "2026-10-19T170000Z-abcdef123456",
    commandId: "0123456789abcdef0123456789abcdef", processId: 123,
    fs, monotonicMs: () => clock.value, nowIso: () => "2026-10-19T17:00:00Z",
    processResources: () => ({ rssBytes: 1, cpuUserMicros: 0, cpuSystemMicros: 0 }),
    randomHex: () => "a".repeat(32), warn: message => { warnings.push(message); },
  });
  assert.ok(recorder);
  setPerformanceRecorderForTests(recorder);
  t.after(resetPerformanceRecorderForTests);
  const snapshot = () => { recorder.flush(false, true); return JSON.parse(files.get("/metrics/performance.json")!); };
  const rows = () => snapshot().metrics.direct_provider_requests.by_source_transaction_role as any[];
  return { recorder, clock, warnings, snapshot, rows };
}

const start = (role: DirectProviderRequestRole, source?: DirectProviderSource) =>
  recordDirectProviderAttemptStarted(role, source);
const pyramidRow = (rows: any[], role = "detail", transaction = "sale") =>
  rows.find(row => row.source === "pyramid-brokerage" && row.role === role && row.transaction === transaction);

test("direct recorder isolates explicit source, transaction and role with exactly-once tokens", async t => {
  const f = fixture(t);
  await withPerformanceSource("essex-realty", "sale", async () => {
    const token = start("detail");
    f.clock.value = 3;
    recordDirectProviderAttemptCompleted(token, { outcome: "succeeded", status: 200 });
  });
  await withPerformanceSource("pyramid-brokerage", "sale", async () => {
    const token = start("detail", "pyramid-brokerage");
    f.clock.value = 8;
    recordDirectProviderAttemptCompleted(token, { outcome: "failed", status: 502 });
    recordDirectProviderAttemptCompleted(token, { outcome: "succeeded", status: 200 });
  });
  await withPerformanceSource("pyramid-brokerage", "lease", async () => {
    recordDirectProviderAttemptCompleted(start("detail", "pyramid-brokerage"), { outcome: "succeeded", status: 200 });
  });
  const rows = f.rows();
  assert.equal(rows.length, 3);
  assert.equal(rows.find(row => row.source === "essex-realty").succeeded, 1);
  const sale = pyramidRow(rows);
  assert.deepEqual([sale.attempts_started, sale.attempts_completed, sale.failed, sale.succeeded], [1, 1, 1, 0]);
  assert.equal(sale.summed_latency_ms, 5);
  assert.equal(sale.status_counts["502"], 1);
  assert.equal(pyramidRow(rows, "detail", "lease").succeeded, 1);
});

test("explicit source mismatch never borrows another source transaction", async t => {
  const f = fixture(t);
  await withPerformanceSource("essex-realty", "lease", async () => {
    recordDirectProviderAttemptCompleted(start("detail", "pyramid-brokerage"), { outcome: "succeeded" });
  });
  await withPerformanceSource("pyramid-brokerage", "sale", async () => {
    recordDirectProviderAttemptCompleted(start("detail"), { outcome: "succeeded" });
  });
  assert.equal(pyramidRow(f.rows(), "detail", "unknown").succeeded, 1);
  assert.equal(f.rows().find(row => row.source === "essex-realty").transaction, "unknown");
});

test("direct telemetry finite source-role matrix and truthful coverage", t => {
  const f = fixture(t);
  for (const source of ["essex-realty", "pyramid-brokerage"] as const) {
    for (const role of source === "essex-realty" ? ["current_archive", "closed_archive", "sitemap", "detail"] as const : ["current_archive", "detail"] as const) {
      recordDirectProviderAttemptCompleted(start(role, source), { outcome: "succeeded" });
    }
  }
  assert.equal(start("sitemap", "pyramid-brokerage"), undefined);
  assert.equal(start("detail", "unknown-adapter" as unknown as DirectProviderSource), undefined);
  const snapshot = f.snapshot();
  assert.equal(f.rows().length, 6);
  assert.equal(snapshot.coverage.direct_provider_requests, "essex_pyramid_only_other_direct_sources_unknown");
  assert.equal(snapshot.coverage.direct_provider_request_sources["pyramid-brokerage"], "instrumented");
  assert.equal(snapshot.coverage.direct_provider_request_sources.other_direct_adapters, "unknown_uninstrumented");
});

test("Pyramid measures one redirect attempt through completed bounded body", async t => {
  const f = fixture(t);
  let finishBody!: () => void;
  let bodyRead!: () => void;
  const reading = new Promise<void>(resolve => { bodyRead = resolve; });
  const body = new ReadableStream<Uint8Array>({
    pull: async controller => {
      bodyRead();
      await new Promise<void>(resolve => { finishBody = resolve; });
      controller.enqueue(new TextEncoder().encode("retained body")); controller.close();
    },
  });
  let calls = 0;
  const request = withPerformanceSource("pyramid-brokerage", "sale", () => pyramidFetch(
    "https://www.pyramidbrokerage.com/listings/private-id/", async () => {
      calls++;
      if (calls === 1) return new Response(null, { status: 302, headers: { location: "/listings/final-private-id/" } });
      return new Response(body, { status: 200 });
    }, 1000, 1));
  await reading;
  const active = pyramidRow(f.rows());
  // Settle fake transport even when this assertion is RED.
  f.clock.value = 37; finishBody();
  assert.equal((await request).body, "retained body");
  assert.ok(active);
  assert.deepEqual([active.attempts_started, active.attempts_completed, active.active_locally_awaited], [1, 0, 1]);
  const done = pyramidRow(f.rows());
  assert.equal(calls, 2);
  assert.deepEqual([done.attempts_started, done.attempts_completed, done.succeeded, done.active_locally_awaited], [1, 1, 1, 0]);
  assert.equal(done.summed_latency_ms, 37);
  assert.deepEqual(done.status_counts, { "200": 1 });
  const encoded = JSON.stringify(f.snapshot());
  for (const sensitive of ["private-id", "retained body", "pyramidbrokerage.com"]) assert.ok(!encoded.includes(sensitive));
});

test("Pyramid completes failure before retry sleep and excludes backoff latency", async t => {
  const f = fixture(t);
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let calls = 0;
  const request = withPerformanceSource("pyramid-brokerage", "lease", () => pyramidFetch(PYRAMID_INVENTORY_URL, async () => {
    calls++;
    f.clock.value = calls === 1 ? 10 : 520;
    return new Response(calls === 1 ? "upstream failure" : "[]", { status: calls === 1 ? 502 : 200 });
  }, 30_000, 2));
  await new Promise<void>(resolve => setImmediate(resolve));
  const failed = pyramidRow(f.rows(), "current_archive", "lease");
  f.clock.value = 510; t.mock.timers.tick(500);
  assert.equal((await request).body, "[]");
  assert.ok(failed);
  assert.deepEqual([failed.failed, failed.attempts_completed, failed.active_locally_awaited], [1, 1, 0]);
  const done = pyramidRow(f.rows(), "current_archive", "lease");
  assert.deepEqual([done.attempts_started, done.attempts_completed, done.failed, done.succeeded], [2, 2, 1, 1]);
  assert.equal(done.summed_latency_ms, 20);
  assert.deepEqual(done.status_counts, { "200": 1, "502": 1 });
});

for (const kind of ["transport", "timeout", "body-cap", "body-stream"] as const) {
  test(`Pyramid records failed ${kind} without sensitive error text`, async t => {
    const f = fixture(t);
    await assert.rejects(withPerformanceSource("pyramid-brokerage", "sale", () => pyramidFetch(PYRAMID_INVENTORY_URL, async (_url, init) => {
      if (kind === "transport") throw new Error("private network detail");
      if (kind === "timeout") return await new Promise<Response>((_resolve, reject) => {
        init!.signal!.addEventListener("abort", () => reject(new DOMException("private abort", "AbortError")), { once: true });
      });
      if (kind === "body-cap") return new Response("oversized body", { status: 200, headers: { "content-length": "99" } });
      return new Response(new ReadableStream({ start(controller) { controller.error(new Error("private body failure")); } }), { status: 200 });
    }, 10, 1, 8)), /Pyramid request failed/);
    const row = pyramidRow(f.rows(), "current_archive");
    assert.ok(row);
    assert.deepEqual([row.attempts_started, row.attempts_completed, row.failed, row.active_locally_awaited], [1, 1, 1, 0]);
    assert.deepEqual(row.status_counts, kind.startsWith("body") ? { "200": 1 } : {});
    if (kind === "body-cap") assert.equal(row.error_categories.transport, 1);
    if (kind === "timeout") assert.equal(row.error_categories.timeout, 1);
    assert.ok(!JSON.stringify(f.snapshot()).includes("private"));
  });
}

test("Pyramid redirect followed by transport failure does not borrow redirect status", async t => {
  const f = fixture(t);
  let calls = 0;
  await assert.rejects(withPerformanceSource("pyramid-brokerage", "sale", () => pyramidFetch(
    PYRAMID_INVENTORY_URL, async () => {
      calls++;
      if (calls === 1) return new Response(null, { status: 302, headers: { location: "/listings/destination/" } });
      throw new TypeError("fetch failed private transport");
    }, 1000, 1)), /fetch failed private transport/);
  const row = pyramidRow(f.rows(), "current_archive");
  assert.equal(calls, 2);
  assert.deepEqual([row.attempts_started, row.attempts_completed, row.failed], [1, 1, 1]);
  assert.deepEqual(row.status_counts, {});
  assert.equal(row.error_categories.transport, 1);
});

test("optional telemetry write failure preserves original Pyramid output and error", async t => {
  const f = fixture(t, true);
  assert.equal((await pyramidFetch(PYRAMID_INVENTORY_URL, async () => new Response("[]"), 1000, 1)).body, "[]");
  await assert.rejects(pyramidFetch(PYRAMID_INVENTORY_URL, async () => { throw new Error("original transport sentinel"); }, 1000, 1), /original transport sentinel/);
  assert.equal(f.warnings.length, 1);
  assert.ok(!f.warnings[0]!.includes("sensitive"));
});
