// Isolate argv before scrape.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  lstatSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  PERFORMANCE_KIND,
  PERFORMANCE_MAX_SNAPSHOT_BYTES,
  PERFORMANCE_WARNING,
  PerformanceFileSystem,
  createClientRequestDeadlineError,
  createPerformanceRecorder,
  flushPerformance,
  performanceRecorder,
  recordClientAttemptCompleted,
  recordClientAttemptStarted,
  recordJllDetailCache,
  recordLogicalScrapeCall,
  recordRetry,
  recordSourceCompleted,
  recordSourceStarted,
  resetPerformanceRecorderForTests,
  setPerformanceRecorderForTests,
  withPerformanceSource,
} from "../../../lib/performance.js";
import { firecrawl, scrapeJson, scrapeRaw } from "../../../lib/scrape.js";
import {
  scrapeJllDetailDoc,
  writeJllDetailCache,
} from "../../../sources/jll.js";

const RUN_ID = "2026-09-13T120000Z-abcdef123456";
const COMMAND_ID = "0123456789abcdef0123456789abcdef";

class FakeFileSystem implements PerformanceFileSystem {
  readonly files = new Map<string, string>();
  readonly specialPaths = new Set<string>();
  readonly writes: string[] = [];
  readonly renames: Array<[string, string]> = [];
  failWrites = 0;

  kind(path: string): "missing" | "regular" | "special" {
    if (this.specialPaths.has(path)) return "special";
    return this.files.has(path) ? "regular" : "missing";
  }

  openExclusive(path: string): unknown {
    this.writes.push(path);
    if (this.files.has(path) || this.specialPaths.has(path)) {
      throw new Error("exclusive temporary path already exists");
    }
    this.files.set(path, "");
    return path;
  }

  writeOpened(handle: unknown, contents: string): void {
    if (typeof handle !== "string") throw new Error("invalid fake handle");
    if (this.failWrites > 0) {
      this.failWrites--;
      throw new Error("sensitive write failure /tmp/private");
    }
    this.files.set(handle, contents);
  }

  closeOpened(_handle: unknown): void {
    // The fake handle has no external resource to release.
  }

  rename(from: string, to: string): void {
    const contents = this.files.get(from);
    if (contents === undefined) throw new Error("missing temporary snapshot");
    this.renames.push([from, to]);
    this.files.set(to, contents);
    this.files.delete(from);
  }

  unlink(path: string): void {
    this.files.delete(path);
  }

  snapshot(path = "/diagnostics/performance.json"): any {
    const contents = this.files.get(path);
    assert.ok(contents, `expected snapshot at ${path}`);
    return JSON.parse(contents);
  }
}

function recorderFixture(options: {
  fs?: FakeFileSystem;
  monotonic?: { value: number };
  resources?: Array<{
    rssBytes: number;
    cpuUserMicros: number;
    cpuSystemMicros: number;
  }>;
  warnings?: string[];
} = {}) {
  const fs = options.fs ?? new FakeFileSystem();
  const monotonic = options.monotonic ?? { value: 0 };
  const warnings = options.warnings ?? [];
  const samples = options.resources ?? [
    { rssBytes: 100, cpuUserMicros: 10, cpuSystemMicros: 5 },
  ];
  let sampleIndex = 0;
  const recorder = createPerformanceRecorder({
    path: "/diagnostics/performance.json",
    runId: RUN_ID,
    commandId: COMMAND_ID,
    processId: 321,
    monotonicMs: () => monotonic.value,
    nowIso: () => new Date(Date.UTC(2026, 8, 13, 12, 0, 0) + monotonic.value).toISOString(),
    processResources: () =>
      samples[Math.min(sampleIndex++, samples.length - 1)]!,
    fs,
    warn: (message) => warnings.push(message),
    randomHex: () => "a".repeat(32),
  });
  assert.ok(recorder);
  return { fs, monotonic, recorder, warnings };
}

test("telemetry is inert without an opted-in path and rejects unsafe identity", () => {
  const oldPath = process.env.CRE_PERFORMANCE_PATH;
  const oldRun = process.env.CRE_REFRESH_GENERATION;
  const oldCommand = process.env.CRE_PERFORMANCE_COMMAND_ID;
  const warnings: string[] = [];
  try {
    delete process.env.CRE_PERFORMANCE_PATH;
    delete process.env.CRE_REFRESH_GENERATION;
    delete process.env.CRE_PERFORMANCE_COMMAND_ID;
    resetPerformanceRecorderForTests();
    assert.equal(performanceRecorder(), undefined);
    recordLogicalScrapeCall("raw");
    flushPerformance({ terminal: true });

    assert.equal(
      createPerformanceRecorder({
        path: "/tmp/unsafe.json",
        runId: "../not-a-run",
        commandId: "NOT-HEX",
        warn: (message) => warnings.push(message),
      }),
      undefined
    );
    assert.deepEqual(warnings, [PERFORMANCE_WARNING]);
    assert.doesNotMatch(warnings[0]!, /unsafe|not-a-run|NOT-HEX/);

    assert.equal(
      createPerformanceRecorder({
        path: "/tmp/exact.json",
        runId: ` ${RUN_ID}`,
        commandId: COMMAND_ID,
        warn: (message) => warnings.push(message),
      }),
      undefined
    );
    assert.deepEqual(warnings, [PERFORMANCE_WARNING, PERFORMANCE_WARNING]);
  } finally {
    if (oldPath === undefined) delete process.env.CRE_PERFORMANCE_PATH;
    else process.env.CRE_PERFORMANCE_PATH = oldPath;
    if (oldRun === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldRun;
    if (oldCommand === undefined) delete process.env.CRE_PERFORMANCE_COMMAND_ID;
    else process.env.CRE_PERFORMANCE_COMMAND_ID = oldCommand;
    resetPerformanceRecorderForTests();
  }
});

test("snapshot separates request, source, cache, concurrency, and sampled resources", async () => {
  const fixture = recorderFixture({
    resources: [
      { rssBytes: 100, cpuUserMicros: 10, cpuSystemMicros: 5 },
      { rssBytes: 250, cpuUserMicros: 25, cpuSystemMicros: 9 },
      { rssBytes: 150, cpuUserMicros: 30, cpuSystemMicros: 11 },
    ],
  });
  setPerformanceRecorderForTests(fixture.recorder);
  try {
    flushPerformance({ terminal: false });
    assert.equal(fixture.fs.writes.length, 1);

    recordSourceStarted("jll", "sale");
    await withPerformanceSource("jll", "sale", async () => {
      recordLogicalScrapeCall("doc");
      const first = recordClientAttemptStarted({ freshRequested: true });
      const second = recordClientAttemptStarted({ freshRequested: false });
      fixture.monotonic.value = 2_500;
      recordClientAttemptCompleted(first, { outcome: "succeeded" });
      recordClientAttemptCompleted(second, {
        outcome: "failed",
        error: Object.assign(new Error("unavailable"), { status: 503 }),
      });
    });
    recordSourceCompleted("jll", "sale", {
      outcome: "succeeded",
      listingsEmitted: 7,
    });
    recordJllDetailCache("hit");
    recordJllDetailCache("miss");
    recordJllDetailCache("refresh_bypass");
    const unattributed = recordClientAttemptStarted({ freshRequested: false });
    recordClientAttemptCompleted(unattributed, { outcome: "succeeded" });
    assert.equal(fixture.fs.writes.length, 1, "events inside ten seconds are coalesced");

    fixture.monotonic.value = 10_000;
    recordLogicalScrapeCall("raw");
    assert.equal(fixture.fs.writes.length, 2);
    flushPerformance({ terminal: true });
    assert.equal(fixture.fs.writes.length, 3, "terminal flush bypasses cadence");

    const snapshot = fixture.fs.snapshot();
    assert.equal(snapshot.schema_version, 1);
    assert.equal(snapshot.kind, PERFORMANCE_KIND);
    assert.equal(snapshot.run_id, RUN_ID);
    assert.equal(snapshot.command_id, COMMAND_ID);
    assert.equal(snapshot.process_id, 321);
    assert.equal(snapshot.terminal, true);
    assert.deepEqual(snapshot.metrics.logical_scrape_calls, { raw: 1, doc: 1, json: 0 });
    assert.deepEqual(
      {
        started: snapshot.metrics.requests.attempts_started,
        completed: snapshot.metrics.requests.attempts_completed,
        succeeded: snapshot.metrics.requests.succeeded,
        failed: snapshot.metrics.requests.failed,
        active: snapshot.metrics.requests.active_locally_awaited,
        maxActive: snapshot.metrics.requests.max_active_locally_awaited,
        fresh: snapshot.metrics.requests.fresh_requested,
      },
      { started: 3, completed: 3, succeeded: 2, failed: 1, active: 0, maxActive: 2, fresh: 1 }
    );
    assert.equal(snapshot.metrics.requests.summed_latency_ms, 5_000);
    assert.equal(snapshot.metrics.requests.approximate_p50_ms, 2_500);
    assert.equal(snapshot.metrics.requests.approximate_p95_ms, 2_500);
    assert.deepEqual(snapshot.metrics.requests.by_source_transaction, [
      {
        source: "jll",
        transaction: "sale",
        attempts_started: 2,
        attempts_completed: 2,
        succeeded: 1,
        failed: 1,
        fresh_requested: 1,
      },
    ]);
    assert.deepEqual(snapshot.metrics.source_runs, [
      {
        source: "jll",
        transaction: "sale",
        started: 1,
        completed: 1,
        succeeded: 1,
        failed: 0,
        listings_emitted: 7,
        elapsed_ms: 2_500,
      },
    ]);
    assert.deepEqual(snapshot.metrics.cache.jll_detail, {
      hits: 1,
      misses: 1,
      refresh_bypasses: 1,
    });
    assert.deepEqual(snapshot.metrics.resources.node_rss_bytes, {
      current: 150,
      max_sampled: 250,
    });
    assert.deepEqual(snapshot.metrics.resources.process_cpu_microseconds, {
      user_cumulative: 30,
      system_cumulative: 11,
    });
    assert.equal(snapshot.coverage.request_concurrency, "locally_awaited_attempts_only");
    assert.match(snapshot.coverage.memory, /not_docker_or_true_peak/);
    assert.equal(snapshot.coverage.quality, "complete_quality_unknown");
    assert.ok(
      fixture.fs.renames.every(([from]) =>
        from.endsWith(`.321.${"a".repeat(32)}.tmp`)
      )
    );
  } finally {
    resetPerformanceRecorderForTests();
  }
});

test("failure snapshots contain only finite categories and bounded statuses", () => {
  const fixture = recorderFixture();
  const secret = "token-super-secret";
  for (let status = 400; status <= 433; status++) {
    const token = fixture.recorder.recordClientAttemptStarted(false);
    fixture.recorder.recordClientAttemptCompleted(
      token,
      "failed",
      Object.assign(new Error(`https://private.example/${secret}`), { status })
    );
  }
  const timeout = fixture.recorder.recordClientAttemptStarted(false);
  fixture.recorder.recordClientAttemptCompleted(
    timeout,
    "failed",
    createClientRequestDeadlineError(1)
  );
  const otherTimeout = fixture.recorder.recordClientAttemptStarted(false);
  fixture.recorder.recordClientAttemptCompleted(
    otherTimeout,
    "failed",
    new Error(`provider timed out for ${secret}`)
  );
  fixture.recorder.flush(true);

  const snapshot = fixture.fs.snapshot();
  assert.equal(snapshot.metrics.requests.error_categories.http_4xx, 34);
  assert.equal(snapshot.metrics.requests.error_categories.timeout, 2);
  assert.equal(snapshot.metrics.requests.timed_out_remote_settlement_unknown, 1);
  assert.equal(Object.keys(snapshot.metrics.requests.status_counts).length, 32);
  assert.equal(snapshot.metrics.requests.other_valid_statuses, 2);
  const serialized = JSON.stringify(snapshot);
  assert.doesNotMatch(serialized, /private\.example|token-super-secret|\/diagnostics/);
  assert.doesNotMatch(serialized, /message|stack/);
});

test("retry layers account for retry and terminal backoff without merging layers", () => {
  const fixture = recorderFixture();
  fixture.recorder.recordRetry("http_helper", 2_500, true);
  fixture.recorder.recordRetry("http_helper", 7_500, false);
  fixture.recorder.recordRetry("json_parse", 8_000, true);
  fixture.recorder.recordRetry("json_parse", 16_000, false);
  fixture.recorder.flush(true);
  assert.deepEqual(fixture.fs.snapshot().metrics.requests.retry, {
    http_helper: {
      retry_attempts: 1,
      backoff_ms: 10_000,
      terminal_backoff_ms: 7_500,
    },
    json_parse: {
      retry_attempts: 1,
      backoff_ms: 24_000,
      terminal_backoff_ms: 16_000,
    },
  });
});

test("diagnostic write failures warn once and never escape into acquisition", () => {
  const fs = new FakeFileSystem();
  fs.failWrites = 1;
  const fixture = recorderFixture({ fs });
  assert.doesNotThrow(() => fixture.recorder.recordLogicalCall("raw"));
  assert.deepEqual(fixture.warnings, [PERFORMANCE_WARNING]);
  assert.deepEqual(
    [...fs.files.keys()].filter((path) => path.endsWith(".tmp")),
    [],
    "a temp file created before a partial write failure remains recorder-owned"
  );

  fixture.monotonic.value = 10_000;
  assert.doesNotThrow(() => fixture.recorder.recordLogicalCall("doc"));
  fixture.recorder.diagnosticFailure();
  assert.deepEqual(fixture.warnings, [PERFORMANCE_WARNING]);
  assert.equal(fixture.fs.snapshot().degraded, true);
});

test("invalid resource samples remain unavailable rather than false zero", () => {
  const fixture = recorderFixture({
    resources: [
      {
        rssBytes: Number.NaN,
        cpuUserMicros: -1,
        cpuSystemMicros: Number.POSITIVE_INFINITY,
      },
    ],
  });
  fixture.recorder.flush(true);

  const snapshot = fixture.fs.snapshot();
  assert.equal(snapshot.degraded, true);
  assert.deepEqual(snapshot.metrics.resources, {
    node_rss_bytes: { current: null, max_sampled: null },
    process_cpu_microseconds: {
      user_cumulative: null,
      system_cumulative: null,
    },
  });
  assert.deepEqual(fixture.warnings, [PERFORMANCE_WARNING]);
});

test("non-finite initial monotonic clock disables optional telemetry", () => {
  const warnings: string[] = [];
  assert.equal(
    createPerformanceRecorder({
      path: "/diagnostics/performance.json",
      runId: RUN_ID,
      commandId: COMMAND_ID,
      monotonicMs: () => Number.NaN,
      warn: (message) => warnings.push(message),
    }),
    undefined
  );
  assert.deepEqual(warnings, [PERFORMANCE_WARNING]);
});

test("backwards monotonic samples degrade without writing false elapsed time", () => {
  const fixture = recorderFixture();
  fixture.recorder.flush(false);
  fixture.monotonic.value = 10_000;
  fixture.recorder.flush(false);
  const validSnapshot = fixture.fs.snapshot();
  assert.equal(validSnapshot.metrics.elapsed_ms, 10_000);

  fixture.monotonic.value = 9_000;
  assert.doesNotThrow(() => fixture.recorder.flush(true));
  assert.equal(
    fixture.fs.snapshot().metrics.elapsed_ms,
    10_000,
    "the prior valid snapshot remains instead of asserting a smaller elapsed value"
  );
  assert.deepEqual(fixture.warnings, [PERFORMANCE_WARNING]);

  fixture.monotonic.value = 11_000;
  fixture.recorder.flush(true);
  assert.equal(fixture.fs.snapshot().metrics.elapsed_ms, 11_000);
  assert.equal(fixture.fs.snapshot().degraded, true);
});

test("snapshot output has an explicit byte cap", () => {
  const fs = new FakeFileSystem();
  const warnings: string[] = [];
  const recorder = createPerformanceRecorder({
    path: "/diagnostics/performance.json",
    runId: RUN_ID,
    commandId: COMMAND_ID,
    processId: 321,
    monotonicMs: () => 0,
    nowIso: () => "x".repeat(PERFORMANCE_MAX_SNAPSHOT_BYTES),
    fs,
    warn: (message) => warnings.push(message),
    randomHex: () => "b".repeat(32),
  });
  assert.ok(recorder);
  assert.doesNotThrow(() => recorder.flush(true));
  assert.equal(fs.files.has("/diagnostics/performance.json"), false);
  assert.deepEqual(warnings, [PERFORMANCE_WARNING]);
});

test("exclusive random temp creation refuses pre-existing symlinks and FIFOs", () => {
  for (const kind of ["symlink", "fifo"] as const) {
    const dir = mkdtempSync(join(tmpdir(), `cre-performance-temp-${kind}-`));
    const snapshotPath = join(dir, "snapshot.json");
    const victimPath = join(dir, "victim.txt");
    const nonce = kind === "symlink" ? "c".repeat(32) : "d".repeat(32);
    const tempPath = `${snapshotPath}.777.${nonce}.tmp`;
    const warnings: string[] = [];
    try {
      writeFileSync(victimPath, "victim-preserved", "utf8");
      if (kind === "symlink") {
        symlinkSync(victimPath, tempPath);
      } else {
        const made = spawnSync("mkfifo", [tempPath], { encoding: "utf8" });
        assert.equal(made.status, 0, made.stderr);
      }
      const recorder = createPerformanceRecorder({
        path: snapshotPath,
        runId: RUN_ID,
        commandId: COMMAND_ID,
        processId: 777,
        randomHex: () => nonce,
        warn: (message) => warnings.push(message),
      });
      assert.ok(recorder);
      assert.doesNotThrow(() => recorder.flush(true));
      assert.equal(readFileSync(victimPath, "utf8"), "victim-preserved");
      assert.equal(
        kind === "symlink"
          ? lstatSync(tempPath).isSymbolicLink()
          : lstatSync(tempPath).isFIFO(),
        true
      );
      assert.deepEqual(warnings, [PERFORMANCE_WARNING]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("existing snapshot symlinks and FIFOs are refused without replacement", () => {
  for (const kind of ["symlink", "fifo"] as const) {
    const dir = mkdtempSync(join(tmpdir(), `cre-performance-target-${kind}-`));
    const snapshotPath = join(dir, "snapshot.json");
    const victimPath = join(dir, "victim.txt");
    const warnings: string[] = [];
    try {
      writeFileSync(victimPath, "victim-preserved", "utf8");
      if (kind === "symlink") {
        symlinkSync(victimPath, snapshotPath);
      } else {
        const made = spawnSync("mkfifo", [snapshotPath], { encoding: "utf8" });
        assert.equal(made.status, 0, made.stderr);
      }
      const recorder = createPerformanceRecorder({
        path: snapshotPath,
        runId: RUN_ID,
        commandId: COMMAND_ID,
        processId: 778,
        randomHex: () => "e".repeat(32),
        warn: (message) => warnings.push(message),
      });
      assert.ok(recorder);
      assert.doesNotThrow(() => recorder.flush(true));
      assert.equal(readFileSync(victimPath, "utf8"), "victim-preserved");
      assert.equal(
        kind === "symlink"
          ? lstatSync(snapshotPath).isSymbolicLink()
          : lstatSync(snapshotPath).isFIFO(),
        true
      );
      assert.deepEqual(warnings, [PERFORMANCE_WARNING]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  }
});

test("diagnostic clock failures do not turn a successful scrape into a retry", async () => {
  const fs = new FakeFileSystem();
  const warnings: string[] = [];
  let clockFails = false;
  const recorder = createPerformanceRecorder({
    path: "/diagnostics/performance.json",
    runId: RUN_ID,
    commandId: COMMAND_ID,
    processId: 321,
    monotonicMs: () => {
      if (clockFails) throw new Error("diagnostic clock unavailable");
      return 0;
    },
    nowIso: () => "2026-09-13T12:00:00.000Z",
    fs,
    warn: (message) => warnings.push(message),
  });
  assert.ok(recorder);
  const originalScrape = firecrawl.scrape;
  let scrapeCalls = 0;
  setPerformanceRecorderForTests(recorder);
  (firecrawl as any).scrape = async () => {
    scrapeCalls++;
    return { rawHtml: "<html>business result</html>" };
  };
  clockFails = true;
  try {
    assert.match(await scrapeRaw("https://example.com/success"), /business result/);
    assert.equal(scrapeCalls, 1);
    assert.deepEqual(warnings, [PERFORMANCE_WARNING]);
  } finally {
    (firecrawl as any).scrape = originalScrape;
    resetPerformanceRecorderForTests();
  }
});

test("scrape timeout telemetry preserves three attempts and all existing backoffs", async () => {
  const fixture = recorderFixture();
  const originalScrape = firecrawl.scrape;
  const originalSetTimeout = globalThis.setTimeout;
  const originalConsoleError = console.error;
  const existingLogs: string[] = [];
  setPerformanceRecorderForTests(fixture.recorder);
  (firecrawl as any).scrape = () => new Promise<never>(() => undefined);
  globalThis.setTimeout = ((callback: (...args: any[]) => void, _delay?: number, ...args: any[]) =>
    originalSetTimeout(callback, 0, ...args)) as typeof setTimeout;
  console.error = (...args: unknown[]) => existingLogs.push(args.join(" "));
  try {
    await assert.rejects(
      () => scrapeRaw("https://private.example/token-super-secret", { timeout: 1, maxAge: 0 }),
      /timed out after 1ms/
    );
    fixture.recorder.flush(true);
    const requests = fixture.fs.snapshot().metrics.requests;
    assert.deepEqual(
      {
        attempts_started: requests.attempts_started,
        attempts_completed: requests.attempts_completed,
        succeeded: requests.succeeded,
        failed: requests.failed,
        fresh_requested: requests.fresh_requested,
        timeout: requests.error_categories.timeout,
        remote_unknown: requests.timed_out_remote_settlement_unknown,
      },
      {
        attempts_started: 3,
        attempts_completed: 3,
        succeeded: 0,
        failed: 3,
        fresh_requested: 3,
        timeout: 3,
        remote_unknown: 3,
      }
    );
    assert.deepEqual(requests.retry.http_helper, {
      retry_attempts: 2,
      backoff_ms: 15_000,
      terminal_backoff_ms: 7_500,
    });
    assert.equal(fixture.fs.snapshot().metrics.logical_scrape_calls.raw, 1);
    assert.equal(existingLogs.length, 3, "telemetry does not change existing retry logging");
    assert.doesNotMatch(JSON.stringify(fixture.fs.snapshot()), /private\.example|token-super-secret/);
  } finally {
    console.error = originalConsoleError;
    globalThis.setTimeout = originalSetTimeout;
    (firecrawl as any).scrape = originalScrape;
    resetPerformanceRecorderForTests();
  }
});

test("JSON parse retries remain distinct from successful Firecrawl attempts", async () => {
  const fixture = recorderFixture();
  const originalScrape = firecrawl.scrape;
  const originalSetTimeout = globalThis.setTimeout;
  const originalConsoleError = console.error;
  const bodies = ["not JSON", '{"ok":true}'];
  setPerformanceRecorderForTests(fixture.recorder);
  (firecrawl as any).scrape = async () => ({ rawHtml: bodies.shift() });
  globalThis.setTimeout = ((callback: (...args: any[]) => void, _delay?: number, ...args: any[]) =>
    originalSetTimeout(callback, 0, ...args)) as typeof setTimeout;
  console.error = () => undefined;
  try {
    assert.deepEqual(
      await scrapeJson("https://private.example/parse", {
        jsonAttempts: 2,
        jsonBackoffMs: 8_000,
      }),
      { ok: true }
    );
    fixture.recorder.flush(true);
    const snapshot = fixture.fs.snapshot();
    assert.deepEqual(snapshot.metrics.logical_scrape_calls, {
      raw: 2,
      doc: 0,
      json: 1,
    });
    assert.equal(snapshot.metrics.requests.attempts_started, 2);
    assert.equal(snapshot.metrics.requests.succeeded, 2);
    assert.deepEqual(snapshot.metrics.requests.retry.json_parse, {
      retry_attempts: 1,
      backoff_ms: 8_000,
      terminal_backoff_ms: 0,
    });
    assert.deepEqual(snapshot.metrics.requests.retry.http_helper, {
      retry_attempts: 0,
      backoff_ms: 0,
      terminal_backoff_ms: 0,
    });
  } finally {
    console.error = originalConsoleError;
    globalThis.setTimeout = originalSetTimeout;
    (firecrawl as any).scrape = originalScrape;
    resetPerformanceRecorderForTests();
  }
});

test("JLL cache telemetry observes accepted hits, attempted misses, and refresh bypasses", async () => {
  const fixture = recorderFixture();
  const cacheDir = mkdtempSync(join(tmpdir(), "jll-performance-cache-"));
  const originalScrape = firecrawl.scrape;
  const oldCacheDir = process.env.JLL_DETAIL_CACHE_DIR;
  const oldGeneration = process.env.CRE_REFRESH_GENERATION;
  const oldStrict = process.env.CRE_REQUIRE_FRESH_DETAILS;
  let scrapeCalls = 0;
  setPerformanceRecorderForTests(fixture.recorder);
  (firecrawl as any).scrape = async () => {
    scrapeCalls++;
    return { rawHtml: "<html>fresh detail</html>", markdown: "", links: [] };
  };
  try {
    process.env.JLL_DETAIL_CACHE_DIR = cacheDir;
    process.env.CRE_REFRESH_GENERATION = RUN_ID;
    delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    const hitUrl = "https://property.jll.com/listings/cache-hit";
    writeJllDetailCache(hitUrl, {
      rawHtml: "<html>cached detail</html>",
      markdown: "",
      links: [],
    });

    const hit = await scrapeJllDetailDoc(hitUrl);
    assert.match(hit.rawHtml, /cached/);
    await scrapeJllDetailDoc("https://property.jll.com/listings/cache-miss");
    await scrapeJllDetailDoc(hitUrl, { refresh: true });
    fixture.recorder.flush(true);

    assert.equal(scrapeCalls, 2);
    assert.deepEqual(fixture.fs.snapshot().metrics.cache.jll_detail, {
      hits: 1,
      misses: 1,
      refresh_bypasses: 1,
    });
    assert.equal(fixture.fs.snapshot().metrics.requests.attempts_started, 2);
  } finally {
    (firecrawl as any).scrape = originalScrape;
    if (oldCacheDir === undefined) delete process.env.JLL_DETAIL_CACHE_DIR;
    else process.env.JLL_DETAIL_CACHE_DIR = oldCacheDir;
    if (oldGeneration === undefined) delete process.env.CRE_REFRESH_GENERATION;
    else process.env.CRE_REFRESH_GENERATION = oldGeneration;
    if (oldStrict === undefined) delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    else process.env.CRE_REQUIRE_FRESH_DETAILS = oldStrict;
    resetPerformanceRecorderForTests();
    rmSync(cacheDir, { recursive: true, force: true });
  }
});
