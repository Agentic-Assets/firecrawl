// AGENTIC-2900: bounded no-write capacity cells, separate from the governed C10
// and 128-detail benchmark contracts. The foreground coordinator owns the CRE
// lock, host guards, process deadline, runtime admission and remote settlement.
import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";
import { lstatSync, readdirSync, readFileSync } from "node:fs";
import { isAbsolute } from "node:path";
import { performance } from "node:perf_hooks";
import * as cheerio from "cheerio";
import type { ScrapedDoc } from "../types.js";

export const MATCHED_WIDTHS = [1, 2, 4, 8] as const;
export const MATCHED_FIDELITY_SCHEMA = "cre-matched-semantic-v2" as const;
export type MatchedSource = "jll" | "marcus-millichap";
export type MatchedMember = {
  sampleId: string;
  providerId: string;
  canonicalUrl: string;
  // Keep the complete fresh enumeration row, including price-withholding controls.
  baseRow: Record<string, unknown>;
};
export type MatchedCohort = {
  source: MatchedSource;
  inventorySha256: string;
  inventoryStartedAt: string;
  inventoryFinishedAt: string;
  members: MatchedMember[];
};
export type ClientEvidence = {
  dispatches: number;
  statuses: number[];
  failedDispatches: number;
  redirected: boolean;
  maxAgeZero: boolean;
  responseUrlMatches: boolean;
  bodyIdentity: "unverified" | "canonical_body" | "jll_native_property";
};
export type CapturedDetail = {
  listing: Record<string, any>;
  client: ClientEvidence;
  brokers?: unknown[];
  // This is the source-written cache record, with its original flattened
  // observation fields. Reading it is not a second live acquisition.
  doc?: ScrapedDoc & { detailObservedAt?: string; generationId?: string; cachedAt?: string };
};
export type MatchedCapture = (
  member: MatchedMember,
  signal: AbortSignal,
) => Promise<CapturedDetail>;
export type TrialContext = {
  // Actual bounded, body-free snapshots supplied by the foreground coordinator;
  // these are evidence references, not authority to transition a runtime.
  runtimeSnapshot: Record<string, unknown>;
  resourceJournal: string;
  concurrentHostWork: "observed" | "unknown";
};
export class MatchedCapacityError extends Error {
  constructor(readonly category: string) {
    super(`matched capacity rejected: ${category}`);
  }
}
const reject = (category: string): never => { throw new MatchedCapacityError(category); };
const digest = (value: unknown): string => createHash("sha256").update(JSON.stringify(value)).digest("hex");
const sorted = (value: any): any => {
  if (Array.isArray(value)) return value.map(sorted);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, sorted(value[key])]));
  }
  return value;
};

export function matchedCohortSha256(cohort: MatchedCohort): string {
  const inventoryStart = Date.parse(cohort.inventoryStartedAt);
  const inventoryEnd = Date.parse(cohort.inventoryFinishedAt);
  if (!/^[a-f0-9]{64}$/.test(cohort.inventorySha256)
      || !["jll", "marcus-millichap"].includes(cohort.source)
      || !Number.isFinite(inventoryStart) || !Number.isFinite(inventoryEnd)
      || inventoryEnd < inventoryStart || inventoryEnd > Date.now() + 1000
      || cohort.members.length < 8 || cohort.members.length > (cohort.source === "jll" ? 24 : 128)) reject("cohort_shape");
  const ids = new Set<string>();
  const urls = new Set<string>();
  const samples = new Set<string>();
  for (const item of cohort.members) {
    let url: URL;
    try { url = new URL(item.canonicalUrl); } catch { return reject("cohort_url"); }
    const host = cohort.source === "jll" ? "property.jll.com" : "www.marcusmillichap.com";
    if (url.protocol !== "https:" || url.hostname !== host || url.port
        || url.username || url.password || url.hash || url.search
        || !(cohort.source === "jll" ? /^\/listings\/[^/]+\/?$/ : /^\/properties\/[^/]+(?:\/[^/]+)*\/?$/).test(url.pathname)) reject("cohort_url");
    if (!item.sampleId || !item.providerId || !item.baseRow
        || String(item.baseRow.id) !== item.providerId || item.baseRow.url !== item.canonicalUrl
        || ids.has(item.providerId) || urls.has(item.canonicalUrl) || samples.has(item.sampleId)) reject("cohort_identity");
    const observed = Date.parse(String(item.baseRow.inventoryObservedAt));
    if (!Number.isFinite(observed) || observed < inventoryStart || observed > inventoryEnd) reject("inventory_observation");
    ids.add(item.providerId); urls.add(item.canonicalUrl); samples.add(item.sampleId);
  }
  return digest(sorted(cohort));
}

function matchedListingProjection(captured: CapturedDetail): Record<string, any> {
  const row = structuredClone(captured.listing);
  // Only acquisition clocks/generation vary between otherwise identical cells.
  // All normalized facts, prose, ordering, document/media URLs and controls stay.
  delete row.detailObservedAt;
  if (row.freshnessProvenance) delete row.freshnessProvenance.generationId;
  if (Array.isArray(row.brokerIds)) {
    row.brokerIds = row.brokerIds.map((id: unknown) => {
      if (typeof id !== "number" || !Number.isInteger(id) || !captured.brokers?.[id]) return reject("broker_reference");
      return captured.brokers[id];
    }).map(sorted);
  }
  return row;
}

// Strict capture binding stays byte-compatible with the paired audit consumer.
export function matchedListingSha256(captured: CapturedDetail): string {
  return digest(sorted(matchedListingProjection(captured)));
}

export function matchedSemanticListingSha256(captured: CapturedDetail): string {
  const row = matchedListingProjection(captured);
  // This transport diagnostic can vary without changing normalized listing facts.
  if (row.jllDetail?.scrape && typeof row.jllDetail.scrape === "object") delete row.jllDetail.scrape.rawHtmlLength;
  return digest(sorted(row));
}

export type MatchedCell = {
  fidelitySchema: typeof MATCHED_FIDELITY_SCHEMA;
  source: MatchedSource;
  cohortSha256: string;
  generationId: string;
  startedAt: string;
  finishedAt: string;
  requestedConcurrency: number;
  peakActiveEnrichments: number;
  elapsedMs: number;
  freshRowsPerMinute: number;
  latencyMs: { p50: number; p95: number; p99: number; maximum: number };
  rows: { sampleId: string; fidelitySha256: string; semanticFidelitySha256: string; latencyMs: number; client: ClientEvidence }[];
  context: TrialContext;
  coverage: {
    concurrency: string;
    providerAttempts: "unknown";
    originCache: "unknown";
    remoteSettlement: "coordinator_required";
    quality: "matched_normalized_fidelity_only; strict_ingest_dry_run_required";
  };
};

export async function runMatchedCapacityCell(options: {
  cohort: MatchedCohort;
  concurrency: number;
  generationId: string;
  capture: MatchedCapture;
  context: TrialContext;
  signal: AbortSignal;
  baseline?: MatchedCell;
}): Promise<MatchedCell> {
  const { cohort, concurrency, generationId, capture, signal, baseline, context } = options;
  const cohortSha256 = matchedCohortSha256(cohort);
  const members = structuredClone(cohort.members);
  const contextSnapshot = structuredClone(context);
  if (!(MATCHED_WIDTHS as readonly number[]).includes(concurrency) || !generationId) reject("cell_configuration");
  const sampleIds = new Set(members.map(member => member.sampleId));
  if (baseline && (baseline.fidelitySchema !== MATCHED_FIDELITY_SCHEMA || baseline.source !== cohort.source
      || baseline.cohortSha256 !== cohortSha256 || baseline.generationId === generationId
      || baseline.requestedConcurrency !== 1 || !Array.isArray(baseline.rows) || baseline.rows.length !== members.length
      || new Set(baseline.rows.map(row => row?.sampleId)).size !== members.length
      || baseline.rows.some(row => !row || !sampleIds.has(row.sampleId)
        || typeof row.fidelitySha256 !== "string" || !/^[0-9a-f]{64}$/.test(row.fidelitySha256)
        || typeof row.semanticFidelitySha256 !== "string" || !/^[0-9a-f]{64}$/.test(row.semanticFidelitySha256)))) reject("baseline_binding");
  if (!context.resourceJournal || !context.runtimeSnapshot || !Object.keys(context.runtimeSnapshot).length) reject("resource_evidence");
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) abort();
  const started = performance.now();
  const wallStarted = Date.now();
  let next = 0;
  let active = 0;
  let peak = 0;
  let failure: string | null = null;
  const rows: MatchedCell["rows"] = new Array(members.length);
  const baselineRows = new Map(baseline?.rows.map((row) => [row.sampleId, row.semanticFidelitySha256]));
  const fail = (category: string) => {
    failure ??= category;
    controller.abort();
  };
  try {
    await Promise.all(Array.from({ length: concurrency }, async () => {
      while (!failure && !controller.signal.aborted && next < members.length) {
        const index = next++;
        const item = members[index]!;
        const itemStarted = performance.now();
        active++; peak = Math.max(peak, active);
        try {
          const captured = structuredClone(await capture(structuredClone(item), controller.signal));
          if (failure || controller.signal.aborted) break;
          const row = captured.listing;
          const observed = Date.parse(row?.detailObservedAt);
          const client = captured.client;
          if (String(row?.id) !== item.providerId || row?.url !== item.canonicalUrl
              || row?.detailError || row?.skip || row?.preserveChildCollections) reject("detail_identity_or_quality");
          if (!Number.isFinite(observed) || observed < wallStarted || observed > Date.now() + 1000
              || row?.freshnessProvenance?.generationId !== generationId
              || row?.freshnessProvenance?.cacheDisposition !== "live"
              || row?.freshnessProvenance?.detailScope !== "detail_page") reject("detail_freshness");
          if (client.dispatches !== 1 || client.failedDispatches !== 0 || client.statuses.length !== 1
              || !Number.isInteger(client.statuses[0])
              || client.statuses[0] !== 200 || client.redirected || !client.responseUrlMatches
              || client.bodyIdentity !== (cohort.source === "jll" ? "jll_native_property" : "canonical_body")
              || (cohort.source === "jll" && !client.maxAgeZero)) reject("client_reliability");
          const fidelitySha256 = matchedListingSha256(captured);
          const semanticFidelitySha256 = matchedSemanticListingSha256(captured);
          if (baseline && baselineRows.get(item.sampleId) !== semanticFidelitySha256) reject("fidelity_regression");
          rows[index] = { sampleId: item.sampleId, fidelitySha256, semanticFidelitySha256, latencyMs: performance.now() - itemStarted, client };
        } catch (error) {
          fail(error instanceof MatchedCapacityError ? error.category : "capture_failure");
        } finally { active--; }
      }
    }));
    if (failure) reject(failure);
    if (controller.signal.aborted) reject("cancelled");
    if (rows.filter(Boolean).length !== members.length || peak !== concurrency) reject("unsaturated_or_incomplete_cell");
    const elapsedMs = performance.now() - started;
    const latencies = rows.map((row) => row.latencyMs).sort((a, b) => a - b);
    const quantile = (percent: number) => latencies[Math.ceil(latencies.length * percent) - 1]!;
    return {
      fidelitySchema: MATCHED_FIDELITY_SCHEMA, source: cohort.source, cohortSha256, generationId,
      startedAt: new Date(wallStarted).toISOString(), finishedAt: new Date().toISOString(),
      requestedConcurrency: concurrency,
      peakActiveEnrichments: peak, elapsedMs, freshRowsPerMinute: rows.length * 60000 / elapsedMs,
      latencyMs: { p50: quantile(0.5), p95: quantile(0.95), p99: quantile(0.99), maximum: latencies.at(-1)! },
      rows, context: contextSnapshot,
      coverage: { concurrency: "locally_awaited_real_enrichment; API jobs/browser pages require resource journal",
        providerAttempts: "unknown", originCache: "unknown", remoteSettlement: "coordinator_required",
        quality: "matched_normalized_fidelity_only; strict_ingest_dry_run_required" },
    };
  } finally { signal.removeEventListener("abort", abort); }
}

/** Cells must be separate owned child processes/generations/cache roots. This
 * offline coordinator never starts a process, changes Compose or writes a DB. */
export async function runMatchedCapacityMatrix(
  runCell: (concurrency: number, baseline?: MatchedCell) => Promise<MatchedCell>,
): Promise<MatchedCell[]> {
  const cells: MatchedCell[] = [];
  for (const width of MATCHED_WIDTHS) {
    const cell = await runCell(width, cells[0]);
    if (cell.requestedConcurrency !== width || cell.peakActiveEnrichments !== width
        || (cells[0] && cell.cohortSha256 !== cells[0].cohortSha256)
        || cells.some((previous) => previous.generationId === cell.generationId)) reject("matrix_binding");
    cells.push(cell);
  }
  return cells;
}

let productionCaptureActive = false;
/** One isolated worker only. Parent owns a bounded process-group deadline;
 * AbortSignal forwarding is cooperative and is not remote-settlement evidence.
 * onCaptured can save the JLL rendered cache doc for the paired transport audit
 * without another rendered request. Never print these private row/doc bodies. */
export async function withProductionMatchedCapture<T>(options: {
  source: MatchedSource;
  generationId: string;
  onCaptured?: (member: MatchedMember, detail: CapturedDetail) => void;
}, run: (capture: MatchedCapture) => Promise<T>): Promise<T> {
  if (productionCaptureActive || process.env.CRE_REFRESH_GENERATION !== options.generationId
      || process.env.CRE_REQUIRE_FRESH_DETAILS !== "1") reject("worker_freshness_configuration");
  if (options.source === "jll") {
    const root = process.env.JLL_DETAIL_CACHE_DIR;
    if (!root || !isAbsolute(root)) reject("worker_cache_configuration");
    const info = lstatSync(root!);
    if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid?.()
        || (info.mode & 0o077) !== 0 || readdirSync(root!).length !== 0) reject("worker_cache_configuration");
  }
  const originalFetch = globalThis.fetch;
  const storage = new AsyncLocalStorage<{ signal: AbortSignal; client: ClientEvidence; member: MatchedMember }>();
  productionCaptureActive = true;
  globalThis.fetch = async (input, init) => {
    const state = storage.getStore();
    if (!state) return originalFetch(input, init);
    state.signal.throwIfAborted();
    state.client.dispatches++;
    const inherited = init?.signal ?? (input instanceof Request ? input.signal : undefined);
    const signal = inherited ? AbortSignal.any([inherited, state.signal]) : state.signal;
    if (typeof init?.body === "string") {
      try { state.client.maxAgeZero ||= JSON.parse(init.body).maxAge === 0; } catch { /* no body capture */ }
    }
    try {
      const response = await originalFetch(input, { ...init, signal });
      state.client.statuses.push(response.status);
      state.client.redirected ||= response.redirected;
      const requestedUrl = input instanceof Request ? input.url : String(input);
      state.client.responseUrlMatches &&= response.url === requestedUrl;
      if (response.redirected || response.url !== requestedUrl || response.status !== 200) reject("response_binding");
      if (options.source === "marcus-millichap") {
        // Inspect the exact body consumed by the real parser, once. The source
        // copies base.id/url, so those fields alone are not observed identity.
        const text = response.text.bind(response);
        response.text = async () => {
          const html = await text();
          if (Buffer.byteLength(html) > 4 * 1024 * 1024) reject("identity_body_size");
          const $ = cheerio.load(html);
          const canonical = $('link[rel="canonical"]').map((_, el) => $(el).attr("href")).get();
          if (canonical.length !== 1 || new URL(canonical[0]!, requestedUrl).href !== state.member.canonicalUrl) reject("canonical_body_identity");
          state.client.bodyIdentity = "canonical_body";
          return html;
        };
      }
      return response;
    } catch (error) { state.client.failedDispatches++; throw error; }
  };
  try {
    const jll = options.source === "jll" ? await import("./jll.js") : null;
    const marcus = options.source === "marcus-millichap" ? await import("./marcus-millichap.js") : null;
    const broker = await import("../lib/broker.js");
    return await run(async (member, signal) => {
      const client: ClientEvidence = { dispatches: 0, statuses: [], failedDispatches: 0, redirected: false, maxAgeZero: false,
        responseUrlMatches: true, bodyIdentity: "unverified" };
      const listing = await storage.run({ signal, client, member }, () => jll
        ? jll.enrichJllListing(structuredClone(member.baseRow))
        : marcus!.enrichMarcusListing(structuredClone(member.baseRow)));
      const detail: CapturedDetail = { listing, client, brokers: structuredClone(broker.brokers) };
      if (jll && !listing?.detailError && !listing?.skip) {
        client.bodyIdentity = "jll_native_property";
        detail.doc = JSON.parse(readFileSync(jll.jllDetailCachePath(member.canonicalUrl), "utf8"));
      }
      options.onCaptured?.(structuredClone(member), structuredClone(detail));
      return detail;
    });
  } finally { globalThis.fetch = originalFetch; productionCaptureActive = false; }
}
