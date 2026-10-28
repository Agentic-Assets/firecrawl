process.argv = [process.argv[0]!, process.argv[1]!];
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync, chmodSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  matchedCohortSha256, matchedListingSha256, runMatchedCapacityCell,
  runMatchedCapacityMatrix, withProductionMatchedCapture,
  type CapturedDetail, type MatchedCohort, type MatchedMember,
  type MatchedSource,
} from "../../../sources/matched-capacity.js";
import { executeMatchedWorker, prepareMarcusInventory, prepareJllInventory, readWorkerInput, writeWorkerOutput, verifyInventory } from "../../../sources/matched-capacity-worker.js";
import { jllCohortSha256 } from "../../../sources/jll-direct-benchmark.js";

const context = {
  runtimeSnapshot: { project: "synthetic-owned", apiCpuCores: 2, browserCpuCores: 2,
    apiQueueCap: 4, browserPages: 4 },
  resourceJournal: "synthetic-resource-usage.jsonl", concurrentHostWork: "unknown" as const,
};
const inventoryObservedAt = new Date(Date.now() - 1000).toISOString();
function cohort(source: MatchedSource = "marcus-millichap"): MatchedCohort {
  return { source, inventorySha256: "a".repeat(64), inventoryStartedAt: inventoryObservedAt,
    inventoryFinishedAt: inventoryObservedAt, members: Array.from({ length: 16 }, (_, index) => {
    const url = source === "jll" ? `https://property.jll.com/listings/member-${index}`
      : `https://www.marcusmillichap.com/properties/member-${index}`;
    return { sampleId: `sample-${index}`, providerId: String(index), canonicalUrl: url,
      baseRow: { id: String(index), url, sourceKey: source, name: `Property ${index}`, transactionType: "Sale", inventoryObservedAt,
        jllSearchResult: { hidePrice: false } } };
  }) };
}
function detail(member: MatchedMember, generationId: string): CapturedDetail {
  return { listing: { ...member.baseRow, detailObservedAt: new Date().toISOString(),
    freshnessProvenance: { generationId, cacheDisposition: "live", detailScope: "detail_page", method: "real_source" },
    photos: ["https://images.example/image.jpg"],
    documents: [{ url: "https://docs.example/om.pdf", docType: "om" }],
    markdown: "Complete property narrative", contactsDetailed: [{ name: "Synthetic Contact" }] },
    client: { dispatches: 1, statuses: [200], failedDispatches: 0, redirected: false, maxAgeZero: true,
      responseUrlMatches: true, bodyIdentity: member.canonicalUrl.includes("property.jll.com") ? "jll_native_property" : "canonical_body" } };
}
function cellOptions(generationId: string, concurrency = 1) {
  return { cohort: cohort(), concurrency, generationId, context,
    signal: new AbortController().signal,
    capture: async (member: MatchedMember) => detail(member, generationId) };
}

test("shared rendered/direct audit hashes the exact same original full cohort", () => {
  const sample = { ...cohort("jll"), source: "jll" as const };
  assert.equal(matchedCohortSha256(sample), jllCohortSha256(sample));
});

test("real cell scheduler reaches C1/2/4/8 with the same immutable cohort and no fidelity loss", async () => {
  const called: number[] = [];
  const cells = await runMatchedCapacityMatrix(async (width, baseline) => {
    called.push(width);
    let active = 0;
    let observedPeak = 0;
    const options = cellOptions(`generation-${width}`, width);
    const result = await runMatchedCapacityCell({ ...options, baseline, capture: async (member) => {
      active++; observedPeak = Math.max(observedPeak, active);
      await new Promise((resolve) => setTimeout(resolve, 2));
      active--;
      return detail(member, options.generationId);
    } });
    assert.equal(result.peakActiveEnrichments, observedPeak);
    assert.equal(observedPeak, width);
    return result;
  });
  assert.deepEqual(called, [1, 2, 4, 8]);
  assert.equal(cells[3]!.peakActiveEnrichments, 8); // not collect.ts's hidden cap6
  assert.equal(new Set(cells.map((value) => value.cohortSha256)).size, 1);
  assert.ok(cells.every((value) => value.rows.length === 16 && value.freshRowsPerMinute > 0));
  assert.equal(cells[3]!.context.runtimeSnapshot.browserPages, 4);
  assert.equal(cells[3]!.coverage.providerAttempts, "unknown");
  assert.match(cells[3]!.coverage.concurrency, /locally_awaited/);
  assert.ok(cells[3]!.latencyMs.p95 >= cells[3]!.latencyMs.p50);
  assert.equal(cells[3]!.latencyMs.maximum, Math.max(...cells[3]!.rows.map((row) => row.latencyMs)));
});

test("capture cannot mutate the bound identity or reported runtime snapshot", async () => {
  const options = cellOptions("mutated");
  const runtime = structuredClone(context);
  await assert.rejects(runMatchedCapacityCell({ ...options, context: runtime, capture: async (member) => {
    member.providerId = "changed"; member.baseRow.id = "changed";
    runtime.runtimeSnapshot.browserPages = 128;
    return detail(member, "mutated");
  } }), /detail_identity_or_quality/);
  const stable = await runMatchedCapacityCell({ ...cellOptions("stable"), context: runtime,
    capture: async (member) => { runtime.runtimeSnapshot.browserPages = 999; return detail(member, "stable"); },
  });
  assert.equal(stable.context.runtimeSnapshot.browserPages, 128);
});

test("a detail failure aborts in-flight peers, stops new dispatches and stops the matrix before C4", async () => {
  const widths: number[] = [];
  let launches = 0;
  let abortedPeers = 0;
  await assert.rejects(runMatchedCapacityMatrix(async (width, baseline) => {
    widths.push(width);
    const options = cellOptions(`generation-${width}`, width);
    return runMatchedCapacityCell({ ...options, baseline, capture: async (member, signal) => {
      if (width === 1) return detail(member, options.generationId);
      launches++;
      if (member.sampleId === "sample-0") throw new Error("private source/body must stay out of error");
      await new Promise((_, reject) => signal.addEventListener("abort", () => {
        abortedPeers++; reject(new Error("aborted"));
      }, { once: true }));
      return detail(member, options.generationId);
    } });
  }), /capture_failure/);
  assert.deepEqual(widths, [1, 2]);
  assert.equal(launches, 2);
  assert.equal(abortedPeers, 1);
});

for (const [label, mutate, category] of [
  ["wrong identity", (value: CapturedDetail) => { value.listing.id = "wrong"; }, "detail_identity_or_quality"],
  ["missing URL", (value: CapturedDetail) => { delete value.listing.url; }, "detail_identity_or_quality"],
  ["thin preservation row", (value: CapturedDetail) => { value.listing.preserveChildCollections = true; }, "detail_identity_or_quality"],
  ["stale observation", (value: CapturedDetail) => { value.listing.detailObservedAt = "2000-01-01T00:00:00Z"; }, "detail_freshness"],
  ["future observation", (value: CapturedDetail) => { value.listing.detailObservedAt = "2100-01-01T00:00:00Z"; }, "detail_freshness"],
  ["reused generation", (value: CapturedDetail) => { value.listing.freshnessProvenance.generationId = "old"; }, "detail_freshness"],
  ["cache hit", (value: CapturedDetail) => { value.listing.freshnessProvenance.cacheDisposition = "generation_cache"; }, "detail_freshness"],
  ["429", (value: CapturedDetail) => { value.client.statuses = [429]; }, "client_reliability"],
  ["5xx", (value: CapturedDetail) => { value.client.statuses = [503]; }, "client_reliability"],
  ["non200 success", (value: CapturedDetail) => { value.client.statuses = [201]; }, "client_reliability"],
  ["redirected body", (value: CapturedDetail) => { value.client.redirected = true; }, "client_reliability"],
  ["response URL mismatch", (value: CapturedDetail) => { value.client.responseUrlMatches = false; }, "client_reliability"],
  ["copied base identity without body proof", (value: CapturedDetail) => { value.client.bodyIdentity = "unverified"; }, "client_reliability"],
  ["invalid status evidence", (value: CapturedDetail) => { value.client.statuses = [NaN]; }, "client_reliability"],
  ["client retry", (value: CapturedDetail) => { value.client.dispatches = 2; value.client.statuses = [502, 200]; }, "client_reliability"],
] as const) {
  test(`rejects ${label} before scheduling another item`, async () => {
    let calls = 0;
    const options = cellOptions("fault-generation");
    await assert.rejects(runMatchedCapacityCell({ ...options, capture: async (member) => {
      calls++;
      const value = detail(member, options.generationId); mutate(value); return value;
    } }), new RegExp(category));
    assert.equal(calls, 1);
  });
}

for (const field of ["documents", "photos", "contactsDetailed", "markdown"] as const) {
  test(`rejects loss of ${field} against C1 even if identity and fresh row counts match`, async () => {
    const baseline = await runMatchedCapacityCell(cellOptions("baseline"));
    await assert.rejects(runMatchedCapacityCell({ ...cellOptions("candidate", 2), baseline,
      capture: async (member) => { const value = detail(member, "candidate"); delete value.listing[field]; return value; },
    }), /fidelity_regression/);
  });
}

test("broker table reordering is resolved; missing contacts remain a fidelity change", () => {
  const item = cohort().members[0]!;
  const first = detail(item, "a"); const second = detail(item, "b");
  first.listing.brokerIds = [0]; first.brokers = [{ name: "A", phone: "synthetic" }];
  second.listing.brokerIds = [1]; second.brokers = [{ name: "B" }, { name: "A", phone: "synthetic" }];
  assert.equal(matchedListingSha256(first), matchedListingSha256(second));
  second.brokers[1] = { name: "A" };
  assert.notEqual(matchedListingSha256(first), matchedListingSha256(second));
  second.listing.brokerIds = [7];
  assert.throws(() => matchedListingSha256(second), /broker_reference/);
});

test("reordering the listing's contact references remains a fidelity regression", () => {
  const first = detail(cohort().members[0]!, "a");
  first.brokers = [{ name: "Primary contact" }, { name: "Secondary contact" }];
  first.listing.brokerIds = [0, 1];
  const second = structuredClone(first);
  second.listing.brokerIds = [1, 0];
  assert.notEqual(matchedListingSha256(first), matchedListingSha256(second));
});

test("invalid cohort, width and reused baseline fail before capture", async () => {
  const invalid = cohort(); invalid.members[1] = structuredClone(invalid.members[0]!);
  assert.throws(() => matchedCohortSha256(invalid), /cohort_identity/);
  invalid.members[1] = cohort().members[1]!; invalid.members[1]!.canonicalUrl = "https://unrelated.example/item";
  assert.throws(() => matchedCohortSha256(invalid), /cohort_url/);
  let calls = 0;
  await assert.rejects(runMatchedCapacityCell({ ...cellOptions("bad", 6), capture: async (item) => {
    calls++; return detail(item, "bad");
  } }), /cell_configuration/);
  assert.equal(calls, 0);
  const baseline = await runMatchedCapacityCell(cellOptions("baseline"));
  await assert.rejects(runMatchedCapacityCell({ ...cellOptions("baseline", 2), baseline }), /baseline_binding/);
});

test("pre-aborted cell never dispatches and a falsely labeled C8 matrix fails", async () => {
  const controller = new AbortController(); controller.abort();
  let calls = 0;
  await assert.rejects(runMatchedCapacityCell({ ...cellOptions("cancelled"), signal: controller.signal,
    capture: async (item) => { calls++; return detail(item, "cancelled"); } }), /cancelled/);
  assert.equal(calls, 0);
  await assert.rejects(runMatchedCapacityMatrix(async (width, baseline) => {
    const result = await runMatchedCapacityCell({ ...cellOptions(`g${width}`, width), baseline });
    return width === 8 ? { ...result, peakActiveEnrichments: 6 } : result;
  }), /matrix_binding/);
});

async function withWorkerEnvironment<T>(source: MatchedSource, run: () => Promise<T>): Promise<T> {
  const keys = ["CRE_REFRESH_GENERATION", "CRE_REQUIRE_FRESH_DETAILS", "JLL_DETAIL_CACHE_DIR"] as const;
  const prior = keys.map((key) => process.env[key]);
  const root = mkdtempSync(join(tmpdir(), "matched-capacity-"));
  process.env.CRE_REFRESH_GENERATION = `production-${source}`;
  process.env.CRE_REQUIRE_FRESH_DETAILS = "1";
  process.env.JLL_DETAIL_CACHE_DIR = root;
  try { return await run(); } finally {
    keys.forEach((key, index) => { if (prior[index] === undefined) delete process.env[key]; else process.env[key] = prior[index]; });
    rmSync(root, { recursive: true, force: true });
  }
}

test("production Marcus cell calls the real detail parser at C8 with native request evidence", async () => {
  await withWorkerEnvironment("marcus-millichap", async () => {
    const original = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async (input, init) => {
      assert.ok(init?.signal);
      calls++;
      const id = String(input).match(/member-(\d+)/)![1]!;
      await new Promise((resolve) => setTimeout(resolve, 2));
      const response = new Response(`<link rel="canonical" href="${String(input)}"><main><h1>Property ${id}</h1><div class="score-hero-body"><p>1 Main St, City, TX 75001</p></div>
        <div class="mm-property-investment-overview"><p>Full source narrative</p></div>
        <div class="mm-property-price">Listing Price: $1,000,000</div>
        <div class="mm-property-documents-button"><a href="https://documents.example/deal">Offering Memorandum</a></div></main>`);
      Object.defineProperty(response, "url", { value: String(input) });
      return response;
    };
    try {
      let projected: Record<string, any> | undefined;
      const result = await withProductionMatchedCapture({ source: "marcus-millichap", generationId: "production-marcus-millichap",
        onCaptured: (_, value) => { projected = value.listing; },
      }, (capture) => runMatchedCapacityCell({ ...cellOptions("production-marcus-millichap", 8), capture }));
      assert.equal(calls, 16);
      assert.equal(result.peakActiveEnrichments, 8);
      assert.equal(projected?.salePriceUsd, 1000000);
      assert.equal(projected?.description, "Full source narrative");
      assert.equal(projected?.gatedDocuments?.[0].gated, true);
      assert.ok(result.rows.every((row) => row.client.dispatches === 1 && !row.client.maxAgeZero));
    } finally { globalThis.fetch = original; }
  });
});

for (const fault of ["missing canonical", "wrong canonical", "redirect", "wrong response URL", "non200"] as const) {
  test(`real Marcus parser capture rejects ${fault} rather than trusting copied base identity`, async () => {
    await withWorkerEnvironment("marcus-millichap", async () => {
      const original = globalThis.fetch;
      let requests = 0;
      globalThis.fetch = async (input) => {
        requests++;
        const url = String(input);
        const response = new Response(`${fault === "missing canonical" ? "" : `<link rel="canonical" href="${fault === "wrong canonical" ? "https://www.marcusmillichap.com/properties/wrong" : url}">`}<main><h1>Copied base cannot prove identity</h1><div class="mm-property-investment-overview">Otherwise plausible body</div></main>`, { status: fault === "non200" ? 201 : 200 });
        Object.defineProperty(response, "url", { value: fault === "wrong response URL" ? "https://www.marcusmillichap.com/properties/wrong" : url });
        Object.defineProperty(response, "redirected", { value: fault === "redirect" });
        return response;
      };
      try {
        await assert.rejects(withProductionMatchedCapture({ source: "marcus-millichap", generationId: "production-marcus-millichap" },
          (capture) => runMatchedCapacityCell({ ...cellOptions("production-marcus-millichap"), capture })), /detail_identity_or_quality|client_reliability|capture_failure/);
        assert.equal(requests, 1);
      } finally { globalThis.fetch = original; }
    });
  });
}

for (const wallStepMs of [0, 2000]) {
test(`actual isolated worker writes strict input through a ${wallStepMs}ms forward wall step`, async () => {
  await withWorkerEnvironment("marcus-millichap", async () => {
    const root = process.env.JLL_DETAIL_CACHE_DIR!;
    const output = join(root, "output"); mkdirSync(output, { mode: 0o700 });
    const original = globalThis.fetch;
    const originalNow = Date.now;
    const previousStartedAt = process.env.CRE_REFRESH_STARTED_AT;
    const sample = cohort();
    process.env.CRE_REFRESH_STARTED_AT = sample.inventoryStartedAt;
    globalThis.fetch = async (input) => {
      const response = new Response(`<link rel="canonical" href="${String(input)}"><main><h1>Fixture property</h1><div class="mm-property-investment-overview">Complete narrative</div></main>`);
      Object.defineProperty(response, "url", { value: String(input) });
      return response;
    };
    try {
      // The observation singleton retains its original clock reference. Move
      // the raw wall boundary only; newly produced evidence keeps its domain.
      Date.now = () => originalNow() + wallStepMs;
      const inventoryArtifact = join(root, "inventory.json");
      const raw = JSON.stringify({ runMeta: { mode: "monitor", startedAt: sample.inventoryStartedAt,
        finishedAt: sample.inventoryFinishedAt }, listings: sample.members.map((member) => member.baseRow) });
      writeFileSync(inventoryArtifact, raw, { mode: 0o600 });
      sample.inventorySha256 = createHash("sha256").update(raw).digest("hex");
      const input = { cohort: sample, cohortSha256: matchedCohortSha256(sample), generationId: "production-marcus-millichap", concurrency: 1, context,
        inventoryArtifact, maxInventoryAgeHours: 2 };
      const path = join(output, "input.json");
      writeWorkerOutput(path, input);
      assert.deepEqual(readWorkerInput(path), input);
      const result = await executeMatchedWorker(input, output, new AbortController().signal);
      assert.equal(result.rows.length, 16);
      const artifact = JSON.parse(readFileSync(join(output, "collector.json"), "utf8"));
      assert.equal(artifact.runMeta.freshness.generationStartedAt, sample.inventoryStartedAt);
      assert.equal(artifact.runMeta.freshness.requireFreshDetails, true);
      assert.equal(artifact.listings.length, 16);
      assert.ok(artifact.sources.every((source: any) => source.truncated));
      assert.ok(artifact.listings.every((row: any) => row.inventoryObservedAt === inventoryObservedAt && row.freshnessProvenance.cacheDisposition === "live"));
      const dryRun = spawnSync("python3", ["cre_ingest.py", "--in", join(output, "collector.json"),
        "--dry-run", "--require-strict-freshness", "--keep-artifacts", join(output, "sql")],
        { encoding: "utf8", timeout: 15000, maxBuffer: 1024 * 1024 });
      assert.equal(dryRun.status, 0, `strict SQL dry-run rejected synthetic worker output (${dryRun.error?.name ?? "exit"})`);
      assert.throws(() => writeWorkerOutput(path, {}), /EEXIST/);
      const link = join(output, "link.json"); symlinkSync(path, link);
      assert.throws(() => readWorkerInput(link));
      chmodSync(path, 0o644);
      assert.throws(() => readWorkerInput(path), /private_input/);
    } finally {
      Date.now = originalNow;
      globalThis.fetch = original;
      if (previousStartedAt === undefined) delete process.env.CRE_REFRESH_STARTED_AT;
      else process.env.CRE_REFRESH_STARTED_AT = previousStartedAt;
    }
  });
});
}

test("forward wall step never admits an observation older than its own cell", async () => {
  const originalNow = Date.now;
  Date.now = () => originalNow() + 2000;
  try {
    const options = cellOptions("before-own-cell");
    await assert.rejects(runMatchedCapacityCell({ ...options, capture: async (member) => {
      const captured = detail(member, options.generationId);
      captured.listing.detailObservedAt = "2000-01-01T00:00:00Z";
      return captured;
    } }), /detail_freshness/);
  } finally { Date.now = originalNow; }
});

test("worker rejects forged cohort binding before importing or dispatching a source", async () => {
  await withWorkerEnvironment("marcus-millichap", async () => {
    const original = globalThis.fetch;
    let requests = 0;
    globalThis.fetch = async () => { requests++; throw new Error("blocked"); };
    try {
      await assert.rejects(executeMatchedWorker({ cohort: cohort(), cohortSha256: "f".repeat(64),
        generationId: "production-marcus-millichap", concurrency: 1, context, inventoryArtifact: "unread-before-binding", maxInventoryAgeHours: 2 },
        process.env.JLL_DETAIL_CACHE_DIR!, new AbortController().signal), /worker_binding/);
      assert.equal(requests, 0);
    } finally { globalThis.fetch = original; }
  });
});

test("bounded JLL preparation uses one real enumeration adapter and retains original response/rows/clocks", async () => {
  await withWorkerEnvironment("jll", async () => {
    const original = globalThis.fetch;
    let calls = 0;
    const body = JSON.stringify({ data: { properties: { count: 16, items: Array.from({ length: 16 }, (_, index) => ({
      id: String(index), pageUrl: `/listings/member-${index}`, title: `Synthetic ${index}`, hidePrice: false,
    })) } } });
    globalThis.fetch = async (input) => {
      calls++;
      const response = new Response(body, { headers: { "content-type": "application/json" } });
      Object.defineProperty(response, "url", { value: String(input) });
      return response;
    };
    try {
      const root = process.env.JLL_DETAIL_CACHE_DIR!;
      await prepareJllInventory({ kind: "prepare_jll", transaction: "sale", propertyType: "office", members: 16 }, root, new AbortController().signal);
      assert.equal(calls, 1);
      const inventory = readFileSync(join(root, "inventory.json"));
      const artifact = JSON.parse(inventory.toString());
      const selected = JSON.parse(readFileSync(join(root, "cohort.json"), "utf8"));
      assert.equal(artifact.nativeResponse.body, body);
      assert.equal(artifact.nativeResponse.sha256, createHash("sha256").update(body).digest("hex"));
      assert.equal(selected.cohort.inventorySha256, createHash("sha256").update(inventory).digest("hex"));
      assert.equal(selected.cohort.members.length, 16);
      assert.deepEqual(selected.cohort.members.map((member: any) => member.baseRow), artifact.listings);
      assert.equal(selected.cohortSha256, matchedCohortSha256(selected.cohort));
      assert.equal(artifact.runMeta.mode, "enumeration_only");
      assert.equal(artifact.scope.fullInventory, false);
      assert.ok(artifact.listings.every((row: any) => !row.detailObservedAt));
      const bound = { ...selected, generationId: "production-jll", concurrency: 1, context,
        inventoryArtifact: join(root, "inventory.json"), maxInventoryAgeHours: 2 };
      await verifyInventory(bound);
      artifact.listings[0].name = "Forged projected name";
      selected.cohort.members[0].baseRow.name = "Forged projected name";
      const forged = JSON.stringify(artifact);
      writeFileSync(bound.inventoryArtifact, forged, { mode: 0o600 });
      selected.cohort.inventorySha256 = createHash("sha256").update(forged).digest("hex");
      await assert.rejects(verifyInventory(bound), /native_inventory_projection/);
    } finally { globalThis.fetch = original; }
  });
});

test("bounded preparation rejects insufficient inventory without requesting another page", async () => {
  await withWorkerEnvironment("jll", async () => {
    const original = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async (input) => {
      calls++;
      const response = Response.json({ data: { properties: { count: 0, items: [] } } });
      Object.defineProperty(response, "url", { value: String(input) }); return response;
    };
    try {
      await assert.rejects(prepareJllInventory({ kind: "prepare_jll", transaction: "sale", propertyType: "office", members: 16 },
        process.env.JLL_DETAIL_CACHE_DIR!, new AbortController().signal), /preparation_incomplete/);
      assert.equal(calls, 1);
    } finally { globalThis.fetch = original; }
  });
});

test("production JLL captures one fresh real enrichment and shares its rendered document without a second request", async () => {
  await withWorkerEnvironment("jll", async () => {
    const original = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async (input, init) => {
      calls++;
      assert.ok(init?.signal);
      const body = JSON.parse(String(init?.body));
      assert.equal(body.maxAge, 0);
      const id = body.url.match(/member-(\d+)/)![1]!;
      const property = { id, pageUrl: `/listings/member-${id}`, name: `Property ${id}`, hidePrice: false,
        salePrice: { amount: 1000000, currency: "USD", unit: null },
        images: [{ url: "https://images.example/one.jpg" }], description: "Complete source narrative" };
      const response = Response.json({ success: true, data: {
        rawHtml: `<script id="__NEXT_DATA__">${JSON.stringify({ props: { pageProps: { property, brokers: [] } } })}</script>`,
        markdown: "Complete source narrative", links: [], images: [],
      } });
      Object.defineProperty(response, "url", { value: String(input) });
      return response;
    };
    try {
      let documents = 0;
      const options = { ...cellOptions("production-jll", 2), cohort: cohort("jll") };
      const result = await withProductionMatchedCapture({ source: "jll", generationId: "production-jll",
        onCaptured: (_, value) => {
          assert.match(value.doc?.rawHtml ?? "", /__NEXT_DATA__/);
          assert.equal(value.doc?.generationId, "production-jll");
          assert.equal(value.doc?.detailObservedAt, value.listing.detailObservedAt);
          documents++;
        },
      }, (capture) => runMatchedCapacityCell({ ...options, capture }));
      assert.equal(calls, 16); assert.equal(documents, 16);
      assert.ok(result.rows.every((row) => row.client.maxAgeZero));
      assert.equal(result.coverage.originCache, "unknown");
      writeFileSync(join(process.env.JLL_DETAIL_CACHE_DIR!, "already-used"), "private");
      await assert.rejects(withProductionMatchedCapture({ source: "jll", generationId: "production-jll" }, async () => {}), /worker_cache_configuration/);
      assert.equal(calls, 16);
    } finally { globalThis.fetch = original; }
  });
});

function matchedCancellationChildEnv(source: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const key of ["PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT", "WINDIR"]) {
    if (source[key] !== undefined) env[key] = source[key];
  }
  return env;
}

test("cancellation child environment excludes unrelated credentials and runtime options", () => {
  assert.deepEqual(matchedCancellationChildEnv({ PATH: "/synthetic", HOME: "/synthetic-home",
    NODE_OPTIONS: "--require=/synthetic", CRE_TEST_SECRET: "synthetic-secret" }),
  { PATH: "/synthetic", HOME: "/synthetic-home" });
});

test("production capture forwards cell cancellation to native HTTP and restores fetch after failure", () => {
  const child = spawnSync(process.execPath, ["--import", "tsx", "tests/ts/fixtures/matched-cancellation-child.ts", "capture"],
    { encoding: "utf8", timeout: 10000, env: matchedCancellationChildEnv() });
  assert.equal(child.status, 0, `owned capture child failed: ${child.error?.message ?? child.stderr}`);
  assert.match(child.stdout, /PASS native cancellation and restoration/);
});

test("no-start timeout terminates the owned wrapper before a delayed finally can affect the next test", async () => {
  const original = globalThis.fetch;
  const nextTestFetch: typeof fetch = async () => { throw new Error("next test sentinel"); };
  globalThis.fetch = nextTestFetch;
  try {
    const child = spawnSync(process.execPath, ["--import", "tsx", "tests/ts/fixtures/matched-cancellation-child.ts", "no-start"],
      { encoding: "utf8", timeout: 10000, env: matchedCancellationChildEnv() });
    assert.equal(child.status, 0, `owned no-start child failed: ${child.error?.message ?? child.stderr}`);
    assert.match(child.stdout, /PASS bounded no-start timeout with pending wrapper/);
    await new Promise((resolve) => setTimeout(resolve, 850));
    assert.equal(globalThis.fetch, nextTestFetch);
  } finally { globalThis.fetch = original; }
});

test("a deliberate JLL cache miss reaches the actual scrape method and its blocked fetch transport", async () => {
  await withWorkerEnvironment("jll", async () => {
    const { firecrawl } = await import("../../../lib/scrape.js");
    const originalMethod = firecrawl.scrape;
    const originalFetch = globalThis.fetch;
    const priorAttempts = process.env.CRE_SCRAPE_MAX_ATTEMPTS;
    process.env.CRE_SCRAPE_MAX_ATTEMPTS = "1";
    let methods = 0;
    let interceptedDispatches = 0;
    let capturedFailure: CapturedDetail | undefined;
    firecrawl.scrape = async (...args) => { methods++; return originalMethod.apply(firecrawl, args); };
    // This replacement never delegates to a socket/SDK transport. On the old
    // SDK-based source, the expected interception count below would remain0.
    globalThis.fetch = async () => { interceptedDispatches++; throw new Error("blocked synthetic transport"); };
    try {
      await assert.rejects(withProductionMatchedCapture({ source: "jll", generationId: "production-jll",
        onCaptured: (_, value) => { capturedFailure = value; },
      }, (capture) => runMatchedCapacityCell({ ...cellOptions("production-jll"), cohort: cohort("jll"), capture })),
      /detail_identity_or_quality/);
      assert.equal(methods, 1);
      assert.equal(interceptedDispatches, 1);
      assert.equal(capturedFailure?.client.failedDispatches, 1);
      assert.equal(capturedFailure?.doc, undefined);
    } finally {
      firecrawl.scrape = originalMethod; globalThis.fetch = originalFetch;
      if (priorAttempts === undefined) delete process.env.CRE_SCRAPE_MAX_ATTEMPTS;
      else process.env.CRE_SCRAPE_MAX_ATTEMPTS = priorAttempts;
    }
  });
});


for (const fault of ["none", "map_failure", "missing_identity"] as const) {
  test(`guarded Marcus preparation uses the actual monitor adapter with bounded map expansion: ${fault}`, async () => {
    await withWorkerEnvironment("marcus-millichap", async () => {
      const original = globalThis.fetch;
      const rows = Array.from({ length: 16 }, (_, i) => ({ ActivityId: String(i), DealId: String(i), PropertyName: `Synthetic ${i}` }));
      let dispatches = 0, active = 0, peak = 0;
      globalThis.fetch = async (input, init) => {
        const url = String(input); dispatches++; active++; peak = Math.max(peak, active);
        assert.equal(init?.method, "POST");
        await new Promise((resolve) => setTimeout(resolve, 2));
        let value: unknown;
        let status = 200;
        if (url.endsWith("/properties")) value = { Results: { Properties: rows.slice(0, 2), TotalCount: 16 } };
        else if (url.endsWith("/mapproperties")) value = { Results: { Properties: rows } };
        else {
          assert.ok(url.endsWith("/mappropertydetail"), "no heavy detail or unrelated request");
          const body = JSON.parse(String(init?.body));
          const id = String(body.ActivityId ?? body.activityId);
          if (fault === "map_failure" && id === "0") status = 500;
          value = { Results: { PropertyUrl: fault === "missing_identity" && id === "0" ? null : `/properties/${id}/synthetic`,
            PropertyDetail: `<div class="mm-tile" data-dealid="${id}" data-activityid="${id}"><h2>Synthetic</h2></div>` } };
        }
        const response = new Response(JSON.stringify(value), { status });
        Object.defineProperty(response, "url", { value: url });
        active--; return response;
      };
      try {
        const output = process.env.JLL_DETAIL_CACHE_DIR!;
        if (fault === "none") {
          await prepareMarcusInventory(16, output, new AbortController().signal);
          assert.equal(dispatches, 18); assert.ok(peak <= 2);
          const artifact = JSON.parse(readFileSync(join(output, "inventory.json"), "utf8"));
          assert.equal(artifact.runMeta.mode, "monitor"); assert.equal(artifact.listings.length, 16);
          assert.equal(artifact.scope.heavyDetailsRequested, false); assert.equal(artifact.scope.actualClientCap, 2);
          assert.equal(artifact.rawResponses.length, 18);
          assert.ok(artifact.listings.every((row: any) => !row.detailObservedAt));
        } else {
          await assert.rejects(prepareMarcusInventory(16, output, new AbortController().signal));
          const after = dispatches;
          await new Promise((resolve) => setTimeout(resolve, 15));
          assert.equal(dispatches, after, "all ordinary map peers settled before helper rejects");
          assert.equal(active, 0); assert.ok(peak <= 2);
        }
      } finally { globalThis.fetch = original; }
    });
  });
}



test("supplied JLL cohort cannot bypass the shared twenty-four member bound", () => {
  const value = cohort("jll");
  value.members = Array.from({ length: 25 }, (_, index) => ({ ...value.members[0]!, sampleId: `s-${index}`, providerId: String(index),
    canonicalUrl: `https://property.jll.com/listings/s-${index}`, baseRow: { ...value.members[0]!.baseRow, id: String(index), url: `https://property.jll.com/listings/s-${index}` } }));
  assert.throws(() => matchedCohortSha256(value), /cohort_shape/);
});
