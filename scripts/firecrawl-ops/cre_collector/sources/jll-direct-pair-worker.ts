// AGENTIC-2898/2900: opt-in short-lived evaluator. The coordinator owns the
// canonical lock, independent guards, deadline, runtime and remote settlement.
import { createHash } from "node:crypto";
import { mkdirSync, lstatSync, readdirSync } from "node:fs";
import { join, resolve, isAbsolute } from "node:path";
import { fileURLToPath } from "node:url";
import { fetchJllDirectDoc } from "./jll-direct.js";
import { withOfflineJllProjection } from "./jll-direct-projection.js";
import {
  auditJllCapturedPair,
  jllCohortSha256,
  jllDocumentSha256,
  jllSourceRowSha256,
  auditJllPairedDocs,
  auditJllSourceProjections,
  type JllBenchmarkCohort,
  type JllCapturedArm,
} from "./jll-direct-benchmark.js";
import type { ScrapedDoc } from "../types.js";

export interface JllPairRequest {
  schemaVersion: 1;
  kind: "cre_jll_captured_pair_request";
  renderedCellDirectory: string;
  inventoryArtifact: string;
  cohortSha256: string;
  requestedConcurrency: number;
  directGenerationId: string;
  maxInventoryAgeHours: number;
}
type PrivateIO = {
  readWorkerInput: (path: string) => any;
  writeWorkerOutput: (path: string, value: unknown) => number;
  verifyInventory: (input: any) => Promise<void>;
};
export class JllPairError extends Error {
  constructor(code: string) {
    super(code);
  }
}
const fail = (code: string): never => {
  throw new JllPairError(code);
};
const same = (a: unknown, b: unknown) =>
  jllSourceRowSha256({ value: a }) === jllSourceRowSha256({ value: b });

/** Same normalized real-listing contract used by the rendered cell summary. */
export function renderedPairListingSha256(capture: {
  listing: Record<string, any>;
  brokers: unknown[];
}): string {
  const row = structuredClone(capture.listing);
  delete row.detailObservedAt;
  if (row.freshnessProvenance) delete row.freshnessProvenance.generationId;
  if (Array.isArray(row.brokerIds))
    row.brokerIds = row.brokerIds.map((id: unknown) => {
      if (
        typeof id !== "number" ||
        !Number.isInteger(id) ||
        id < 0 ||
        !capture.brokers[id]
      )
        fail("rendered_broker_reference");
      return capture.brokers[Number(id)];
    });
  const sorted = (value: any): any =>
    Array.isArray(value)
      ? value.map(sorted)
      : value && typeof value === "object"
        ? Object.fromEntries(
            Object.keys(value)
              .sort()
              .map((key) => [key, sorted(value[key])]),
          )
        : value;
  return createHash("sha256")
    .update(JSON.stringify(sorted(row)))
    .digest("hex");
}

/** Load and bind every original rendered capture before starting direct load. */
export async function executeJllPairWorker(
  request: JllPairRequest,
  output: string,
  signal: AbortSignal,
  io: PrivateIO,
  acquire: (url: string, signal: AbortSignal) => Promise<ScrapedDoc> = (
    url,
    signal,
  ) => fetchJllDirectDoc(url, 15000, { signal }),
) {
  signal.throwIfAborted();
  const info = lstatSync(output);
  if (
    !info.isDirectory() ||
    info.isSymbolicLink() ||
    info.uid !== process.getuid?.() ||
    (info.mode & 0o077) !== 0 ||
    readdirSync(output).length
  )
    fail("private_empty_output");
  if (
    !isAbsolute(output) ||
    !isAbsolute(request.renderedCellDirectory ?? "") ||
    !isAbsolute(request.inventoryArtifact ?? "") ||
    request.schemaVersion !== 1 ||
    request.kind !== "cre_jll_captured_pair_request" ||
    ![1, 2, 4, 8].includes(request.requestedConcurrency) ||
    !request.directGenerationId?.trim()
  )
    fail("pair_request");
  const input = structuredClone(
    io.readWorkerInput(join(request.renderedCellDirectory, "input.json")),
  );
  const cell = structuredClone(
    io.readWorkerInput(join(request.renderedCellDirectory, "cell.json")),
  );
  const cohort = input.cohort as JllBenchmarkCohort;
  if (
    jllCohortSha256(cohort) !== request.cohortSha256 ||
    input.cohortSha256 !== request.cohortSha256 ||
    input.inventoryArtifact !== request.inventoryArtifact ||
    input.maxInventoryAgeHours !== request.maxInventoryAgeHours ||
    cell.source !== "jll" ||
    cell.cohortSha256 !== request.cohortSha256 ||
    cell.requestedConcurrency !== request.requestedConcurrency ||
    input.concurrency !== request.requestedConcurrency ||
    !cell.generationId ||
    cell.generationId !== input.generationId ||
    cell.generationId === request.directGenerationId ||
    process.env.CRE_REFRESH_GENERATION !== request.directGenerationId ||
    process.env.CRE_REFRESH_STARTED_AT !== cohort.inventoryStartedAt ||
    process.env.CRE_REQUIRE_FRESH_DETAILS !== "1" ||
    !Number.isFinite(Date.parse(cell.startedAt)) ||
    !Number.isFinite(Date.parse(cell.finishedAt)) ||
    Date.parse(cell.finishedAt) < Date.parse(cell.startedAt) ||
    Date.parse(cell.finishedAt) > Date.now() ||
    !Array.isArray(cell.rows) ||
    cell.rows.length !== cohort.members.length ||
    new Set(cell.rows.map((row: any) => row.sampleId)).size !==
      cohort.members.length
  )
    fail("rendered_cell_binding");
  await io.verifyInventory(input);
  const rendered = new Map<string, JllCapturedArm>();
  let renderedBytes = 0;
  for (const member of cohort.members) {
    const key = createHash("sha256").update(member.sampleId).digest("hex");
    const captured = structuredClone(
      io.readWorkerInput(
        join(request.renderedCellDirectory, `capture-${key}.json`),
      ),
    );
    renderedBytes += Buffer.byteLength(JSON.stringify(captured));
    if (renderedBytes > 64 * 1024 * 1024) fail("rendered_capture_size");
    const listing = captured.listing;
    const client = captured.client;
    const row = cell.rows.find((row: any) => row.sampleId === member.sampleId);
    const doc = captured.doc as ScrapedDoc & {
      detailObservedAt?: string;
      generationId?: string;
    };
    if (
      !row ||
      row.fidelitySha256 !== renderedPairListingSha256(captured) ||
      !same(captured.member, member) ||
      !same(row.client, client) ||
      client?.dispatches !== 1 ||
      client.failedDispatches !== 0 ||
      client.redirected !== false ||
      client.maxAgeZero !== true ||
      client.responseUrlMatches !== true ||
      !same(client.statuses, [200]) ||
      client.bodyIdentity !== "jll_native_property" ||
      !doc ||
      doc.generationId !== cell.generationId ||
      doc.detailObservedAt !== listing?.detailObservedAt ||
      listing?.freshnessProvenance?.generationId !== cell.generationId ||
      listing.freshnessProvenance.method !== "jll_detail" ||
      listing.freshnessProvenance.cacheDisposition !== "live" ||
      listing.detailError ||
      listing.skip ||
      listing.preserveChildCollections ||
      String(listing.id) !== member.providerId ||
      listing.url !== member.canonicalUrl ||
      !Array.isArray(captured.brokers)
    )
      fail("rendered_capture_binding");
    doc.detailObservation = {
      observedAt: doc.detailObservedAt!,
      generationId: doc.generationId!,
      method: "jll_detail",
      cacheDisposition: "live",
    };
    const arm: JllCapturedArm = {
      doc,
      attempts: 1,
      cacheDisposition: "live",
      arm: {
        cohortSha256: request.cohortSha256,
        startedAt: cell.startedAt,
        finishedAt: cell.finishedAt,
        generationId: cell.generationId,
      },
      projection: {
        kind: "live_real_source_capture",
        listing,
        brokers: captured.brokers,
        acquisition: structuredClone(doc.detailObservation),
        documentSha256: jllDocumentSha256(doc),
        baseRowSha256: jllSourceRowSha256(member.baseRow),
      },
    };
    const observed = Date.parse(doc.detailObservation.observedAt);
    if (
      !Number.isFinite(observed) ||
      observed < Date.parse(cell.startedAt) ||
      observed > Date.parse(cell.finishedAt)
    )
      fail("rendered_capture_clock");
    auditJllPairedDocs(member, doc, doc);
    auditJllSourceProjections(arm.projection!, arm.projection!);
    rendered.set(member.sampleId, arm);
  }
  signal.throwIfAborted();
  const controller = new AbortController();
  const abort = () => controller.abort(signal.reason);
  signal.addEventListener("abort", abort, { once: true });
  const startedAt = new Date().toISOString();
  const started = performance.now();
  const direct = new Map<string, ScrapedDoc>();
  let failureCode = "direct_acquisition_failed";
  let next = 0,
    active = 0,
    peak = 0,
    directBytes = 0;
  try {
    const workers = Array.from(
      { length: Math.min(request.requestedConcurrency, cohort.members.length) },
      async () => {
        while (next < cohort.members.length) {
          controller.signal.throwIfAborted();
          const member = cohort.members[next++]!;
          active++;
          peak = Math.max(peak, active);
          try {
            const doc = structuredClone(
              await acquire(member.canonicalUrl, controller.signal),
            );
            controller.signal.throwIfAborted();
            const documentAudit = auditJllPairedDocs(
              member,
              doc,
              rendered.get(member.sampleId)!.doc,
            );
            if (!documentAudit.equivalent)
              fail("pair_document_fidelity_regression");
            if (
              !Number.isInteger(doc.metadata?.providerAttempts) ||
              Number(doc.metadata?.providerAttempts) < 1 ||
              Number(doc.metadata?.providerAttempts) > 4
            )
              fail("direct_attempt_evidence");
            directBytes += Buffer.byteLength(JSON.stringify(doc));
            if (directBytes > 64 * 1024 * 1024) fail("direct_capture_size");
            direct.set(member.sampleId, doc);
          } catch (error) {
            if (!controller.signal.aborted && error instanceof JllPairError)
              failureCode = error.message;
            controller.abort();
            fail(failureCode);
          } finally {
            active--;
          }
        }
      },
    );
    const settled = await Promise.allSettled(workers);
    if (settled.some((value) => value.status === "rejected")) fail(failureCode);
  } finally {
    controller.abort();
    signal.removeEventListener("abort", abort);
  }
  const finishedAt = new Date().toISOString();
  const elapsedMs = performance.now() - started;
  signal.throwIfAborted();
  const cache = join(output, "offline-direct-cache");
  mkdirSync(cache, { mode: 0o700 });
  const beforeCache = process.env.JLL_DETAIL_CACHE_DIR;
  process.env.JLL_DETAIL_CACHE_DIR = cache;
  try {
    return await withOfflineJllProjection(async (project) => {
      const pairs = [];
      for (const member of cohort.members) {
        signal.throwIfAborted();
        const doc = direct.get(member.sampleId)!;
        const projection = await project(member, doc);
        const captured: JllCapturedArm = {
          doc,
          projection,
          attempts: 1,
          cacheDisposition: "live",
          arm: {
            cohortSha256: request.cohortSha256,
            startedAt,
            finishedAt,
            generationId: request.directGenerationId,
          },
        };
        const audit = auditJllCapturedPair({
          cohort,
          sampleId: member.sampleId,
          calibration: {
            startedAt: cell.startedAt,
            finishedAt: new Date().toISOString(),
          },
          direct: captured,
          rendered: rendered.get(member.sampleId)!,
        });
        const key = createHash("sha256").update(member.sampleId).digest("hex");
        io.writeWorkerOutput(join(output, `direct-capture-${key}.json`), {
          member,
          ...captured,
        });
        pairs.push(audit);
        if (!audit.equivalent) fail("pair_fidelity_regression");
      }
      const normalizedFinishedAt = new Date().toISOString();
      const acceptedNormalizedElapsedMs = performance.now() - started;
      const result = {
        kind: "jll_paired_transport_observation_not_admission",
        status: "paired_equivalent",
        cohortSha256: request.cohortSha256,
        requestedConcurrency: request.requestedConcurrency,
        directGenerationId: request.directGenerationId,
        renderedArm: {
          startedAt: cell.startedAt,
          finishedAt: cell.finishedAt,
          generationId: cell.generationId,
          elapsedMs: cell.elapsedMs,
          freshRowsPerMinute: cell.freshRowsPerMinute,
        },
        directArm: {
          startedAt,
          finishedAt,
          acquisitionElapsedMs: elapsedMs,
          acquisitionFreshPagesPerMinute: (direct.size * 60000) / elapsedMs,
          normalizedFinishedAt,
          acceptedNormalizedElapsedMs,
          acceptedNormalizedRowsPerMinute:
            (pairs.length * 60000) / acceptedNormalizedElapsedMs,
          peakActiveRequests: peak,
          providerSocketAttempts: [...direct.values()].reduce(
            (n, doc) => n + Number(doc.metadata?.providerAttempts ?? 0),
            0,
          ),
          clientAttempts: direct.size,
          originCacheWarmth: "unknown",
        },
        pairs,
      };
      io.writeWorkerOutput(join(output, "pair.json"), result);
      return result;
    });
  } finally {
    if (beforeCache === undefined) delete process.env.JLL_DETAIL_CACHE_DIR;
    else process.env.JLL_DETAIL_CACHE_DIR = beforeCache;
  }
}

async function main() {
  const inputPath = process.env.CRE_JLL_PAIR_INPUT,
    output = process.env.CRE_JLL_PAIR_OUTPUT;
  if (!inputPath || !output) fail("pair_paths");
  // Sibling opt-in coordinator module owns bounded, owner-only file I/O and
  // native inventory replay. Dynamic import keeps this worker unused by collect.
  const modulePath = "./matched-capacity-worker.js";
  const io: PrivateIO = await import(modulePath);
  const request = io.readWorkerInput(inputPath!);
  const controller = new AbortController();
  const abort = () => controller.abort();
  process.on("SIGINT", abort);
  process.on("SIGTERM", abort);
  try {
    await executeJllPairWorker(request, output!, controller.signal, io);
  } catch (error) {
    io.writeWorkerOutput(join(output!, "pair.json"), {
      kind: "jll_paired_transport_observation_not_admission",
      status: "failed",
      failure:
        error instanceof JllPairError
          ? error.message
          : "pair_validation_or_projection_failed",
      cohortSha256: request.cohortSha256,
      requestedConcurrency: request.requestedConcurrency,
    });
    throw error;
  } finally {
    process.off("SIGINT", abort);
    process.off("SIGTERM", abort);
  }
}
if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  main().catch(() => {
    console.error("JLL paired evaluation failed; no admission granted");
    process.exitCode = 1;
  });
}
