import { AsyncLocalStorage } from "node:async_hooks";
import { randomBytes } from "node:crypto";
import {
  closeSync,
  constants as fsConstants,
  lstatSync,
  openSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { SOURCE_KEYS, SourceKey, Tx } from "../types.js";

export const PERFORMANCE_SCHEMA_VERSION = 1;
export const PERFORMANCE_KIND = "cre_scrape_performance";
export const PERFORMANCE_FLUSH_INTERVAL_MS = 10_000;
export const PERFORMANCE_MAX_SNAPSHOT_BYTES = 128 * 1024;
export const PERFORMANCE_WARNING =
  "warning: CRE performance telemetry degraded; acquisition continues";

const LATENCY_BOUNDS_MS = [100, 250, 500, 1_000, 2_500, 5_000, 10_000, 30_000, 60_000, 120_000] as const;
const MAX_STATUS_KEYS = 32;
const MAX_COUNTER = Number.MAX_SAFE_INTEGER;
const RUN_ID_PATTERN = /^(\d{4})-(\d{2})-(\d{2})T(\d{2})(\d{2})(\d{2})Z(?:-[0-9a-f]{12})?$/;
const COMMAND_ID_PATTERN = /^[0-9a-f]{32}$/;
const clientDeadlineErrors = new WeakSet<object>();

export type PerformanceErrorCategory =
  | "timeout"
  | "http_4xx"
  | "http_5xx"
  | "transport"
  | "empty_response"
  | "unknown";

export type RetryLayer = "http_helper" | "json_parse";
export type JllCacheEvent = "hit" | "miss" | "refresh_bypass";
export type LogicalScrapeKind = "raw" | "doc" | "json";

type SourceContext = {
  source: SourceKey;
  transaction: Tx;
};

export type ClientAttemptToken = {
  readonly startedMs: number;
  readonly sourceContext: SourceContext | undefined;
  completed: boolean;
};

type RequestCounts = {
  attempts_started: number;
  attempts_completed: number;
  succeeded: number;
  failed: number;
  fresh_requested: number;
};

type SourceRequestCounts = RequestCounts & SourceContext;

type SourceRunCounts = SourceContext & {
  started: number;
  completed: number;
  succeeded: number;
  failed: number;
  listings_emitted: number;
  elapsed_ms: number;
  active_started_ms: number | null;
};

type RetryCounts = {
  retry_attempts: number;
  backoff_ms: number;
  terminal_backoff_ms: number;
};

export type PerformanceFileSystem = {
  kind(path: string): "missing" | "regular" | "special";
  openExclusive(path: string): unknown;
  writeOpened(handle: unknown, contents: string): void;
  closeOpened(handle: unknown): void;
  rename(from: string, to: string): void;
  unlink(path: string): void;
};

export type PerformanceDependencies = {
  path?: string;
  runId?: string;
  commandId?: string;
  processId?: number;
  monotonicMs?: () => number;
  nowIso?: () => string;
  processResources?: () => {
    rssBytes: number;
    cpuUserMicros: number;
    cpuSystemMicros: number;
  };
  fs?: PerformanceFileSystem;
  warn?: (message: string) => void;
  flushIntervalMs?: number;
  randomHex?: () => string;
};

function safeAdd(current: number, increment = 1): number {
  if (!Number.isFinite(increment) || increment <= 0) return current;
  return Math.min(MAX_COUNTER, current + increment);
}

function emptyRequestCounts(): RequestCounts {
  return {
    attempts_started: 0,
    attempts_completed: 0,
    succeeded: 0,
    failed: 0,
    fresh_requested: 0,
  };
}

function emptyRetryCounts(): RetryCounts {
  return { retry_attempts: 0, backoff_ms: 0, terminal_backoff_ms: 0 };
}

function validRunId(value: string): boolean {
  const match = RUN_ID_PATTERN.exec(value);
  if (!match) return false;
  const [, year, month, day, hour, minute, second] = match;
  const iso = `${year}-${month}-${day}T${hour}:${minute}:${second}.000Z`;
  const parsed = new Date(iso);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString() === iso;
}

function requireMonotonic(value: number): number {
  if (!Number.isFinite(value) || value < 0) {
    throw new Error("performance monotonic clock is unavailable");
  }
  return value;
}

function finiteStatus(error: unknown): number | null {
  if (!error || typeof error !== "object") return null;
  try {
    const candidate = error as {
      status?: unknown;
      statusCode?: unknown;
      response?: { status?: unknown };
    };
    for (const value of [candidate.status, candidate.statusCode, candidate.response?.status]) {
      if (typeof value === "number" && Number.isInteger(value) && value >= 100 && value <= 599) {
        return value;
      }
    }
  } catch {
    return null;
  }
  return null;
}

export function createClientRequestDeadlineError(timeoutMs: number): Error {
  const error = new Error(`Firecrawl scrape request timed out after ${timeoutMs}ms`);
  clientDeadlineErrors.add(error);
  return error;
}

export function classifyPerformanceError(error: unknown): {
  category: PerformanceErrorCategory;
  status: number | null;
  remoteSettlementUnknown: boolean;
} {
  const status = finiteStatus(error);
  if (status !== null && status >= 500) {
    return { category: "http_5xx", status, remoteSettlementUnknown: false };
  }
  if (status !== null && status >= 400) {
    return { category: "http_4xx", status, remoteSettlementUnknown: false };
  }
  let name = "";
  let message = "";
  try {
    name =
      error && typeof error === "object" && typeof (error as { name?: unknown }).name === "string"
        ? (error as { name: string }).name.toLowerCase()
        : "";
    message =
      error && typeof error === "object" && typeof (error as { message?: unknown }).message === "string"
        ? (error as { message: string }).message.toLowerCase()
        : "";
  } catch {
    return { category: "unknown", status, remoteSettlementUnknown: false };
  }
  if (name === "aborterror" || message.includes("timed out")) {
    return {
      category: "timeout",
      status,
      remoteSettlementUnknown:
        typeof error === "object" && error !== null && clientDeadlineErrors.has(error),
    };
  }
  if (message === "empty response body" || message === "empty scraped document") {
    return { category: "empty_response", status, remoteSettlementUnknown: false };
  }
  if (error instanceof Error || typeof error === "string") {
    return { category: "transport", status, remoteSettlementUnknown: false };
  }
  return { category: "unknown", status, remoteSettlementUnknown: false };
}

function defaultFileSystem(): PerformanceFileSystem {
  return {
    kind(path) {
      try {
        return lstatSync(path).isFile() ? "regular" : "special";
      } catch (error) {
        if (
          error &&
          typeof error === "object" &&
          (error as { code?: unknown }).code === "ENOENT"
        ) {
          return "missing";
        }
        throw error;
      }
    },
    openExclusive(path) {
      const flags =
        fsConstants.O_WRONLY |
        fsConstants.O_CREAT |
        fsConstants.O_EXCL |
        (fsConstants.O_NOFOLLOW ?? 0);
      return openSync(path, flags, 0o600);
    },
    writeOpened(handle, contents) {
      if (typeof handle !== "number") throw new Error("invalid snapshot handle");
      writeFileSync(handle, contents, { encoding: "utf8" });
    },
    closeOpened(handle) {
      if (typeof handle !== "number") throw new Error("invalid snapshot handle");
      closeSync(handle);
    },
    rename: renameSync,
    unlink: unlinkSync,
  };
}

function defaultResources(): {
  rssBytes: number;
  cpuUserMicros: number;
  cpuSystemMicros: number;
} {
  const cpu = process.cpuUsage();
  return {
    rssBytes: process.memoryUsage().rss,
    cpuUserMicros: cpu.user,
    cpuSystemMicros: cpu.system,
  };
}

export class PerformanceRecorder {
  private readonly startedAt: string;
  private readonly startedMs: number;
  private readonly requests = emptyRequestCounts();
  private readonly logicalCalls: Record<LogicalScrapeKind, number> = {
    raw: 0,
    doc: 0,
    json: 0,
  };
  private readonly retries: Record<RetryLayer, RetryCounts> = {
    http_helper: emptyRetryCounts(),
    json_parse: emptyRetryCounts(),
  };
  private readonly errorCategories: Record<PerformanceErrorCategory, number> = {
    timeout: 0,
    http_4xx: 0,
    http_5xx: 0,
    transport: 0,
    empty_response: 0,
    unknown: 0,
  };
  private readonly latencyCounts = Array.from(
    { length: LATENCY_BOUNDS_MS.length + 1 },
    () => 0
  );
  private readonly statusCounts = new Map<number, number>();
  private otherValidStatuses = 0;
  private readonly sourceRequests = new Map<string, SourceRequestCounts>();
  private readonly sourceRuns = new Map<string, SourceRunCounts>();
  private activeLocallyAwaited = 0;
  private maxActiveLocallyAwaited = 0;
  private summedLatencyMs = 0;
  private timedOutRemoteSettlementUnknown = 0;
  private readonly jllCache = { hits: 0, misses: 0, refresh_bypasses: 0 };
  private maxSampledRssBytes: number | null = null;
  private degraded = false;
  private warningEmitted = false;
  private lastFlushMs = Number.NEGATIVE_INFINITY;
  private lastMonotonicMs: number;

  constructor(
    private readonly path: string,
    private readonly runId: string,
    private readonly commandId: string,
    private readonly processId: number,
    private readonly monotonicMs: () => number,
    private readonly nowIso: () => string,
    private readonly processResources: NonNullable<PerformanceDependencies["processResources"]>,
    private readonly fs: PerformanceFileSystem,
    private readonly warn: (message: string) => void,
    private readonly flushIntervalMs: number,
    private readonly randomHex: () => string
  ) {
    this.startedAt = nowIso();
    this.startedMs = requireMonotonic(monotonicMs());
    this.lastMonotonicMs = this.startedMs;
  }

  private readMonotonic(): number {
    const value = requireMonotonic(this.monotonicMs());
    if (value < this.lastMonotonicMs) {
      throw new Error("performance monotonic clock moved backwards");
    }
    this.lastMonotonicMs = value;
    return value;
  }

  private sourceKey(context: SourceContext): string {
    return `${context.source}\u0000${context.transaction}`;
  }

  private requestCountsFor(context: SourceContext): SourceRequestCounts {
    const key = this.sourceKey(context);
    let counts = this.sourceRequests.get(key);
    if (!counts) {
      counts = { ...context, ...emptyRequestCounts() };
      this.sourceRequests.set(key, counts);
    }
    return counts;
  }

  private sourceRunCountsFor(context: SourceContext): SourceRunCounts {
    const key = this.sourceKey(context);
    let counts = this.sourceRuns.get(key);
    if (!counts) {
      counts = {
        ...context,
        started: 0,
        completed: 0,
        succeeded: 0,
        failed: 0,
        listings_emitted: 0,
        elapsed_ms: 0,
        active_started_ms: null,
      };
      this.sourceRuns.set(key, counts);
    }
    return counts;
  }

  recordLogicalCall(kind: LogicalScrapeKind): void {
    this.logicalCalls[kind] = safeAdd(this.logicalCalls[kind]);
    this.maybeFlush();
  }

  recordClientAttemptStarted(freshRequested: boolean, context?: SourceContext): ClientAttemptToken {
    const startedMs = this.readMonotonic();
    this.requests.attempts_started = safeAdd(this.requests.attempts_started);
    if (freshRequested) this.requests.fresh_requested = safeAdd(this.requests.fresh_requested);
    this.activeLocallyAwaited = safeAdd(this.activeLocallyAwaited);
    this.maxActiveLocallyAwaited = Math.max(
      this.maxActiveLocallyAwaited,
      this.activeLocallyAwaited
    );
    if (context) {
      const counts = this.requestCountsFor(context);
      counts.attempts_started = safeAdd(counts.attempts_started);
      if (freshRequested) counts.fresh_requested = safeAdd(counts.fresh_requested);
    }
    const token = {
      startedMs,
      sourceContext: context,
      completed: false,
    };
    this.maybeFlush();
    return token;
  }

  recordClientAttemptCompleted(
    token: ClientAttemptToken,
    outcome: "succeeded" | "failed",
    error?: unknown
  ): void {
    if (token.completed) return;
    const elapsed = this.readMonotonic() - token.startedMs;
    token.completed = true;
    this.requests.attempts_completed = safeAdd(this.requests.attempts_completed);
    this.requests[outcome] = safeAdd(this.requests[outcome]);
    this.activeLocallyAwaited = Math.max(0, this.activeLocallyAwaited - 1);
    this.summedLatencyMs = safeAdd(this.summedLatencyMs, elapsed);
    const bucket = LATENCY_BOUNDS_MS.findIndex((bound) => elapsed <= bound);
    const bucketIndex = bucket === -1 ? LATENCY_BOUNDS_MS.length : bucket;
    this.latencyCounts[bucketIndex] = safeAdd(this.latencyCounts[bucketIndex] ?? 0);
    if (token.sourceContext) {
      const counts = this.requestCountsFor(token.sourceContext);
      counts.attempts_completed = safeAdd(counts.attempts_completed);
      counts[outcome] = safeAdd(counts[outcome]);
    }
    if (outcome === "failed") {
      const classified = classifyPerformanceError(error);
      this.errorCategories[classified.category] = safeAdd(
        this.errorCategories[classified.category]
      );
      if (classified.remoteSettlementUnknown) {
        this.timedOutRemoteSettlementUnknown = safeAdd(
          this.timedOutRemoteSettlementUnknown
        );
      }
      if (classified.status !== null) {
        const current = this.statusCounts.get(classified.status);
        if (current !== undefined || this.statusCounts.size < MAX_STATUS_KEYS) {
          this.statusCounts.set(classified.status, safeAdd(current ?? 0));
        } else {
          this.otherValidStatuses = safeAdd(this.otherValidStatuses);
        }
      }
    }
    this.maybeFlush();
  }

  recordRetry(layer: RetryLayer, backoffMs: number, willRetry: boolean): void {
    const counts = this.retries[layer];
    const boundedBackoff = Number.isFinite(backoffMs) && backoffMs > 0 ? backoffMs : 0;
    counts.backoff_ms = safeAdd(counts.backoff_ms, boundedBackoff);
    if (willRetry) {
      counts.retry_attempts = safeAdd(counts.retry_attempts);
    } else {
      counts.terminal_backoff_ms = safeAdd(
        counts.terminal_backoff_ms,
        boundedBackoff
      );
    }
    this.maybeFlush();
  }

  recordJllCache(event: JllCacheEvent): void {
    const key =
      event === "hit"
        ? "hits"
        : event === "miss"
          ? "misses"
          : "refresh_bypasses";
    this.jllCache[key] = safeAdd(this.jllCache[key]);
    this.maybeFlush();
  }

  recordSourceStarted(context: SourceContext): void {
    const startedMs = this.readMonotonic();
    const counts = this.sourceRunCountsFor(context);
    counts.started = safeAdd(counts.started);
    counts.active_started_ms = startedMs;
    this.maybeFlush();
  }

  recordSourceCompleted(
    context: SourceContext,
    outcome: "succeeded" | "failed",
    listingsEmitted?: number
  ): void {
    const counts = this.sourceRunCountsFor(context);
    const elapsed =
      counts.active_started_ms === null
        ? null
        : Math.max(
            0,
            this.readMonotonic() - counts.active_started_ms
          );
    counts.completed = safeAdd(counts.completed);
    counts[outcome] = safeAdd(counts[outcome]);
    if (outcome === "succeeded" && Number.isInteger(listingsEmitted) && listingsEmitted! >= 0) {
      counts.listings_emitted = safeAdd(counts.listings_emitted, listingsEmitted);
    }
    if (elapsed !== null) {
      counts.elapsed_ms = safeAdd(counts.elapsed_ms, elapsed);
      counts.active_started_ms = null;
    }
    this.maybeFlush();
  }

  private approximateQuantile(quantile: number): number | null {
    const total = this.latencyCounts.reduce((sum, count) => sum + count, 0);
    if (!total) return null;
    const target = Math.ceil(total * quantile);
    let cumulative = 0;
    for (let index = 0; index < this.latencyCounts.length; index++) {
      cumulative += this.latencyCounts[index] ?? 0;
      if (cumulative >= target) {
        return index < LATENCY_BOUNDS_MS.length ? LATENCY_BOUNDS_MS[index]! : null;
      }
    }
    return null;
  }

  private resourceSample(): {
    node_rss_bytes: { current: number | null; max_sampled: number | null };
    process_cpu_microseconds: {
      user_cumulative: number | null;
      system_cumulative: number | null;
    };
  } {
    const raw = this.processResources();
    const rss =
      Number.isFinite(raw.rssBytes) && raw.rssBytes >= 0 ? raw.rssBytes : null;
    const user =
      Number.isFinite(raw.cpuUserMicros) && raw.cpuUserMicros >= 0
        ? raw.cpuUserMicros
        : null;
    const system =
      Number.isFinite(raw.cpuSystemMicros) && raw.cpuSystemMicros >= 0
        ? raw.cpuSystemMicros
        : null;
    if (rss === null || user === null || system === null) this.diagnosticFailure();
    if (rss !== null) {
      this.maxSampledRssBytes = Math.max(this.maxSampledRssBytes ?? 0, rss);
    }
    return {
      node_rss_bytes: { current: rss, max_sampled: this.maxSampledRssBytes },
      process_cpu_microseconds: {
        user_cumulative: user,
        system_cumulative: system,
      },
    };
  }

  private snapshot(terminal: boolean, nowMs: number): Record<string, unknown> {
    const resources = this.resourceSample();
    const sourceRuns = [...this.sourceRuns.values()]
      .map(({ active_started_ms: _active, ...counts }) => ({ ...counts }))
      .sort((left, right) =>
        `${left.source}\u0000${left.transaction}`.localeCompare(
          `${right.source}\u0000${right.transaction}`
        )
      );
    const bySourceTransaction = [...this.sourceRequests.values()].sort((left, right) =>
      `${left.source}\u0000${left.transaction}`.localeCompare(
        `${right.source}\u0000${right.transaction}`
      )
    );
    const statusCounts = Object.fromEntries(
      [...this.statusCounts.entries()]
        .sort(([left], [right]) => left - right)
        .map(([status, count]) => [String(status), count])
    );
    return {
      schema_version: PERFORMANCE_SCHEMA_VERSION,
      kind: PERFORMANCE_KIND,
      run_id: this.runId,
      command_id: this.commandId,
      process_id: this.processId,
      started_at: this.startedAt,
      updated_at: this.nowIso(),
      terminal,
      degraded: this.degraded,
      metrics: {
        elapsed_ms: Math.max(0, nowMs - this.startedMs),
        resources,
        logical_scrape_calls: { ...this.logicalCalls },
        requests: {
          ...this.requests,
          active_locally_awaited: this.activeLocallyAwaited,
          max_active_locally_awaited: this.maxActiveLocallyAwaited,
          summed_latency_ms: this.summedLatencyMs,
          latency_histogram: {
            bounds_ms: [...LATENCY_BOUNDS_MS],
            counts: [...this.latencyCounts],
          },
          approximate_p50_ms: this.approximateQuantile(0.5),
          approximate_p95_ms: this.approximateQuantile(0.95),
          timed_out_remote_settlement_unknown:
            this.timedOutRemoteSettlementUnknown,
          retry: {
            http_helper: { ...this.retries.http_helper },
            json_parse: { ...this.retries.json_parse },
          },
          error_categories: { ...this.errorCategories },
          status_counts: statusCounts,
          other_valid_statuses: this.otherValidStatuses,
          by_source_transaction: bySourceTransaction,
        },
        source_runs: sourceRuns,
        cache: {
          jll_detail: { ...this.jllCache },
        },
      },
      coverage: {
        client_requests: "firecrawl_scrape_only",
        direct_provider_requests: "unknown_uninstrumented",
        jll_detail_cache: "instrumented",
        other_caches: "unknown_uninstrumented",
        request_concurrency: "locally_awaited_attempts_only",
        timeout_settlement: "remote_settlement_unknown_after_client_deadline",
        memory: "node_process_rss_current_and_max_sampled_not_docker_or_true_peak",
        cpu: "node_process_cpu_usage_cumulative_microseconds",
        activity: "event_driven_snapshot_may_be_stale_during_stalled_request",
        quality: "complete_quality_unknown",
      },
    };
  }

  maybeFlush(): void {
    this.flush(false);
  }

  flush(terminal: boolean): void {
    let nowMs: number;
    try {
      nowMs = this.readMonotonic();
    } catch {
      this.diagnosticFailure();
      return;
    }
    if (!terminal && nowMs - this.lastFlushMs < this.flushIntervalMs) return;
    this.lastFlushMs = nowMs;
    let tmp = "";
    let ownsTmp = false;
    let openHandle: unknown;
    try {
      if (this.fs.kind(this.path) === "special") {
        throw new Error("performance snapshot target is not a regular file");
      }
      const nonce = this.randomHex();
      if (!COMMAND_ID_PATTERN.test(nonce)) {
        throw new Error("performance snapshot nonce is invalid");
      }
      tmp = `${this.path}.${this.processId}.${nonce}.tmp`;
      const json = `${JSON.stringify(this.snapshot(terminal, nowMs))}\n`;
      if (Buffer.byteLength(json, "utf8") > PERFORMANCE_MAX_SNAPSHOT_BYTES) {
        throw new Error("performance snapshot exceeds bounded size");
      }
      openHandle = this.fs.openExclusive(tmp);
      ownsTmp = true;
      try {
        this.fs.writeOpened(openHandle, json);
      } finally {
        this.fs.closeOpened(openHandle);
      }
      openHandle = undefined;
      if (this.fs.kind(this.path) === "special") {
        throw new Error("performance snapshot target became a special file");
      }
      this.fs.rename(tmp, this.path);
      ownsTmp = false;
    } catch {
      if (ownsTmp) {
        try {
          this.fs.unlink(tmp);
        } catch {
          // Diagnostic cleanup must never affect acquisition.
        }
      }
      this.diagnosticFailure();
    }
  }

  diagnosticFailure(): void {
    this.degraded = true;
    if (this.warningEmitted) return;
    this.warningEmitted = true;
    try {
      this.warn(PERFORMANCE_WARNING);
    } catch {
      // Diagnostic warning sinks must never affect acquisition.
    }
  }
}

const sourceContext = new AsyncLocalStorage<SourceContext>();
let singleton: PerformanceRecorder | null | undefined;

export function createPerformanceRecorder(
  dependencies: PerformanceDependencies = {}
): PerformanceRecorder | undefined {
  const path = dependencies.path ?? process.env.CRE_PERFORMANCE_PATH;
  if (!path) return undefined;
  const runId = dependencies.runId ?? process.env.CRE_REFRESH_GENERATION ?? "";
  const commandId =
    dependencies.commandId ?? process.env.CRE_PERFORMANCE_COMMAND_ID ?? "";
  const warn = dependencies.warn ?? ((message: string) => console.error(message));
  const processId = dependencies.processId ?? process.pid;
  const flushIntervalMs =
    dependencies.flushIntervalMs ?? PERFORMANCE_FLUSH_INTERVAL_MS;
  if (
    !validRunId(runId) ||
    !COMMAND_ID_PATTERN.test(commandId) ||
    !Number.isInteger(processId) ||
    processId <= 0 ||
    !Number.isFinite(flushIntervalMs) ||
    flushIntervalMs < PERFORMANCE_FLUSH_INTERVAL_MS
  ) {
    try {
      warn(PERFORMANCE_WARNING);
    } catch {
      // Invalid optional telemetry cannot affect acquisition.
    }
    return undefined;
  }
  try {
    return new PerformanceRecorder(
      path,
      runId,
      commandId,
      processId,
      dependencies.monotonicMs ?? (() => performance.now()),
      dependencies.nowIso ?? (() => new Date().toISOString()),
      dependencies.processResources ?? defaultResources,
      dependencies.fs ?? defaultFileSystem(),
      warn,
      flushIntervalMs,
      dependencies.randomHex ?? (() => randomBytes(16).toString("hex"))
    );
  } catch {
    try {
      warn(PERFORMANCE_WARNING);
    } catch {
      // Invalid optional telemetry cannot affect acquisition.
    }
    return undefined;
  }
}

export function performanceRecorder(): PerformanceRecorder | undefined {
  if (singleton === undefined) {
    singleton = createPerformanceRecorder() ?? null;
  }
  return singleton ?? undefined;
}

export function setPerformanceRecorderForTests(
  recorder: PerformanceRecorder | undefined
): void {
  singleton = recorder ?? null;
}

export function resetPerformanceRecorderForTests(): void {
  singleton = undefined;
}

export async function withPerformanceSource<T>(
  source: SourceKey,
  transaction: Tx,
  operation: () => Promise<T>
): Promise<T> {
  if (!performanceRecorder()) return operation();
  if (!SOURCE_KEYS.includes(source) || !["sale", "lease"].includes(transaction)) {
    return operation();
  }
  return sourceContext.run({ source, transaction }, operation);
}

export function recordLogicalScrapeCall(kind: LogicalScrapeKind): void {
  const recorder = performanceRecorder();
  if (!recorder) return;
  try {
    recorder.recordLogicalCall(kind);
  } catch {
    recorder.diagnosticFailure();
  }
}

export function recordClientAttemptStarted(options: {
  freshRequested: boolean;
}): ClientAttemptToken | undefined {
  const recorder = performanceRecorder();
  if (!recorder) return undefined;
  try {
    return recorder.recordClientAttemptStarted(
      options.freshRequested,
      sourceContext.getStore()
    );
  } catch {
    recorder.diagnosticFailure();
    return undefined;
  }
}

export function recordClientAttemptCompleted(
  token: ClientAttemptToken | undefined,
  options: { outcome: "succeeded" | "failed"; error?: unknown }
): void {
  if (!token) return;
  const recorder = performanceRecorder();
  if (!recorder) return;
  try {
    recorder.recordClientAttemptCompleted(token, options.outcome, options.error);
  } catch {
    recorder.diagnosticFailure();
  }
}

export function recordRetry(
  layer: RetryLayer,
  backoffMs: number,
  willRetry: boolean
): void {
  const recorder = performanceRecorder();
  if (!recorder) return;
  try {
    recorder.recordRetry(layer, backoffMs, willRetry);
  } catch {
    recorder.diagnosticFailure();
  }
}

export function recordJllDetailCache(event: JllCacheEvent): void {
  const recorder = performanceRecorder();
  if (!recorder) return;
  try {
    recorder.recordJllCache(event);
  } catch {
    recorder.diagnosticFailure();
  }
}

export function recordSourceStarted(source: SourceKey, transaction: Tx): void {
  const recorder = performanceRecorder();
  if (!recorder) return;
  try {
    recorder.recordSourceStarted({ source, transaction });
  } catch {
    recorder.diagnosticFailure();
  }
}

export function recordSourceCompleted(
  source: SourceKey,
  transaction: Tx,
  options: { outcome: "succeeded" | "failed"; listingsEmitted?: number }
): void {
  const recorder = performanceRecorder();
  if (!recorder) return;
  try {
    recorder.recordSourceCompleted(
      { source, transaction },
      options.outcome,
      options.listingsEmitted
    );
  } catch {
    recorder.diagnosticFailure();
  }
}

export function flushPerformance(options: { terminal: boolean }): void {
  const recorder = performanceRecorder();
  if (!recorder) return;
  try {
    recorder.flush(options.terminal);
  } catch {
    recorder.diagnosticFailure();
  }
}
