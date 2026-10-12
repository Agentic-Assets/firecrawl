/** AGENTIC-2898 observational harness, never a C10 receipt or transport grant.
 * The foreground caller owns the canonical lock, host guards and capped runtime.
 * No provider, API, cache, filesystem or database connection is opened here.
 */
import { createHash } from "node:crypto";
import { harvestDetail } from "../lib/harvest.js";
import { stripHtmlText } from "../lib/html.js";
import type { ScrapedDoc } from "../types.js";
import type { OfflineJllProjection } from "./jll-direct-projection.js";
import { assertJllDirectUrl } from "./jll-direct.js";
import { jllNextData, normalizedJllListingUrl } from "./pure/jll-receipt.js";

export interface JllBenchmarkMember {
  sampleId: string;
  baseRow: Record<string, unknown>;
  providerId: string;
  canonicalUrl: string;
}
export interface JllBenchmarkCohort {
  source: "jll";
  inventorySha256: string;
  inventoryStartedAt: string;
  inventoryFinishedAt: string;
  members: readonly JllBenchmarkMember[];
}
export type JllBenchmarkTransport = "direct" | "rendered";
export interface JllArmContext {
  armId: string;
  clientConcurrency: number;
  startedAt: string;
}
export interface JllBenchmarkCapture {
  doc: ScrapedDoc;
  /** Output of the real source enricher, provenance retained without relabeling. */
  projection?: OfflineJllProjection;
  /** SDK/client attempts; not a claim about internal browser provider retries. */
  attempts: number;
  cacheDisposition: "live";
}
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value)
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}
const digest = (value: string) =>
  createHash("sha256").update(value).digest("hex");
export const jllDocumentSha256 = (doc: ScrapedDoc) => digest(canonical(doc));
export const jllSourceRowSha256 = (row: Record<string, unknown>) =>
  digest(canonical(row));
function propertyData(member: JllBenchmarkMember, doc: ScrapedDoc) {
  if (
    doc.metadata?.statusCode !== 200 ||
    normalizedJllListingUrl(String(doc.metadata?.sourceURL ?? "")) !==
      member.canonicalUrl
  ) {
    throw new Error("detail_response_identity_unverified");
  }
  const next = jllNextData(doc.rawHtml) as {
    props?: {
      pageProps?: {
        property?: Record<string, unknown>;
        brokers?: unknown;
        relativeUrl?: unknown;
      };
    };
  } | null;
  const page = next?.props?.pageProps;
  const property = page?.property;
  const observedUrl = property?.pageUrl ?? page?.relativeUrl;
  if (
    !property ||
    String(property.id ?? "").trim() !== member.providerId ||
    typeof observedUrl !== "string" ||
    normalizedJllListingUrl(observedUrl) !== member.canonicalUrl
  ) {
    throw new Error("detail_primary_identity_mismatch");
  }
  return { property, brokers: page?.brokers ?? null };
}
/** Conservative ordered check: retain case, punctuation and Markdown markers.
 * Only link destinations, HTML markup and whitespace are normalized. Formatting
 * differences can fail this audit; they require examination, not exemptions.
 */
function orderedVisibleMarkdown(markdown: string): string {
  return (
    stripHtmlText(markdown.replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")) ?? ""
  )
    .replace(/\s+/gu, " ")
    .trim();
}
function tokenCounts(markdown: string): Map<string, number> {
  const visible = orderedVisibleMarkdown(markdown);
  const result = new Map<string, number>();
  for (const word of visible
    .normalize("NFKC")
    .toLowerCase()
    .match(/[\p{L}\p{N}]+/gu) ?? [])
    result.set(word, (result.get(word) ?? 0) + 1);
  return result;
}
function missing<T>(expected: T[], actual: T[]): number {
  const available = new Set(actual.map(canonical));
  return expected.filter((value) => !available.has(canonical(value))).length;
}
export function auditJllPairedDocs(
  member: JllBenchmarkMember,
  direct: ScrapedDoc,
  rendered: ScrapedDoc,
) {
  const nativeDirect = propertyData(member, direct);
  const nativeRendered = propertyData(member, rendered);
  const directAssets = harvestDetail(direct, { baseUrl: member.canonicalUrl });
  const renderedAssets = harvestDetail(rendered, {
    baseUrl: member.canonicalUrl,
  });
  const directTokens = tokenCounts(direct.markdown);
  const renderedTokens = tokenCounts(rendered.markdown);
  let missingMarkdownTokens = 0;
  for (const [word, count] of renderedTokens)
    missingMarkdownTokens += Math.max(0, count - (directTokens.get(word) ?? 0));
  const omissions = {
    documents: missing(renderedAssets.documents, directAssets.documents),
    media: missing(renderedAssets.media, directAssets.media),
    links: missing(renderedAssets.links, directAssets.links),
    images: missing(renderedAssets.images, directAssets.images),
    markdownTokens: missingMarkdownTokens,
  };
  const nativePayloadEqual =
    canonical(nativeDirect) === canonical(nativeRendered);
  const markdownOrderedVisibleEqual =
    orderedVisibleMarkdown(direct.markdown) ===
    orderedVisibleMarkdown(rendered.markdown);
  return {
    equivalent:
      nativePayloadEqual &&
      markdownOrderedVisibleEqual &&
      directTokens.size > 0 &&
      renderedTokens.size > 0 &&
      Object.values(omissions).every((value) => value === 0),
    nativePayloadEqual,
    markdownOrderedVisibleEqual,
    omissions,
    nativeDigest: {
      direct: digest(canonical(nativeDirect)),
      rendered: digest(canonical(nativeRendered)),
    },
    bodyDigest: {
      direct: digest(direct.rawHtml),
      rendered: digest(rendered.rawHtml),
    },
    markdownDigest: {
      direct: digest(direct.markdown),
      rendered: digest(rendered.markdown),
    },
    counts: {
      direct: Object.fromEntries(
        Object.entries(directAssets).map(([key, value]) => [key, value.length]),
      ),
      rendered: Object.fromEntries(
        Object.entries(renderedAssets).map(([key, value]) => [
          key,
          value.length,
        ]),
      ),
    },
  };
}

/** Hash the complete immutable source inventory, including controls/clocks. */
export function jllCohortSha256(cohort: JllBenchmarkCohort): string {
  const members = cohort.members;
  if (
    cohort.source !== "jll" ||
    !Number.isFinite(Date.parse(cohort.inventoryStartedAt)) ||
    !Number.isFinite(Date.parse(cohort.inventoryFinishedAt)) ||
    Date.parse(cohort.inventoryFinishedAt) <
      Date.parse(cohort.inventoryStartedAt) ||
    !/^[a-f0-9]{64}$/.test(cohort.inventorySha256) ||
    members.length < 1 ||
    members.length > 24
  )
    throw new Error("invalid_bounded_matrix");
  const ids = new Set<string>(),
    urls = new Set<string>(),
    samples = new Set<string>();
  for (const member of members) {
    const url = assertJllDirectUrl(member.canonicalUrl);
    if (
      !member.sampleId ||
      samples.has(member.sampleId) ||
      !member.baseRow ||
      String(member.baseRow.id) !== member.providerId ||
      typeof member.baseRow.inventoryObservedAt !== "string" ||
      !Number.isFinite(Date.parse(member.baseRow.inventoryObservedAt)) ||
      Date.parse(member.baseRow.inventoryObservedAt) <
        Date.parse(cohort.inventoryStartedAt) ||
      Date.parse(member.baseRow.inventoryObservedAt) >
        Date.parse(cohort.inventoryFinishedAt) ||
      member.baseRow.url !== member.canonicalUrl ||
      !/^[0-9]+$/.test(member.providerId) ||
      url.toString() !== member.canonicalUrl ||
      url.search ||
      url.hash ||
      ids.has(member.providerId) ||
      urls.has(member.canonicalUrl)
    )
      throw new Error("invalid_matrix_identity");
    samples.add(member.sampleId);
    ids.add(member.providerId);
    urls.add(member.canonicalUrl);
  }
  return digest(canonical(cohort));
}

export type LiveJllProjection = Omit<OfflineJllProjection, "kind"> & {
  kind: "live_real_source_capture";
};
export type JllCapturedArm = Omit<JllBenchmarkCapture, "projection"> & {
  projection?: OfflineJllProjection | LiveJllProjection;
  arm: {
    cohortSha256: string;
    startedAt: string;
    finishedAt: string;
    generationId: string;
  };
};

/** Reconcile captures from their ORIGINAL arms without acquiring or retiming
 * either document. The coordinator owns calibration and each elapsed/resource
 * measurement. This returns fidelity evidence only, never throughput/admission.
 */
export function auditJllCapturedPair(input: {
  cohort: JllBenchmarkCohort;
  sampleId: string;
  calibration: { startedAt: string; finishedAt: string };
  direct: JllCapturedArm;
  rendered: JllCapturedArm;
}) {
  const options = structuredClone(input);
  const cohortSha256 = jllCohortSha256(options.cohort);
  const member = options.cohort.members.find(
    (row) => row.sampleId === options.sampleId,
  );
  const start = Date.parse(options.calibration.startedAt),
    end = Date.parse(options.calibration.finishedAt);
  if (
    !member ||
    !Number.isFinite(start) ||
    !Number.isFinite(end) ||
    end < start ||
    end > Date.now() + 1000
  )
    throw new Error("invalid_pair_calibration");
  for (const transport of ["direct", "rendered"] as const) {
    const capture = options[transport];
    const observation = capture.doc.detailObservation;
    const armStart = Date.parse(capture.arm.startedAt),
      armEnd = Date.parse(capture.arm.finishedAt);
    const observed = Date.parse(observation?.observedAt ?? "");
    if (
      !Number.isFinite(armStart) ||
      !Number.isFinite(armEnd) ||
      armEnd < armStart ||
      armStart < start ||
      armEnd > end ||
      capture.arm.cohortSha256 !== cohortSha256 ||
      !capture.arm.generationId.trim() ||
      capture.attempts !== 1 ||
      capture.cacheDisposition !== "live" ||
      !observation ||
      observation.cacheDisposition !== "live" ||
      observation.generationId !== capture.arm.generationId ||
      observation.method !==
        (transport === "direct" ? "jll_direct_candidate" : "jll_detail") ||
      !Number.isFinite(observed) ||
      observed < armStart ||
      observed > armEnd ||
      !capture.projection ||
      canonical(capture.projection.acquisition) !== canonical(observation) ||
      capture.projection.documentSha256 !== jllDocumentSha256(capture.doc) ||
      capture.projection.baseRowSha256 !== jllSourceRowSha256(member.baseRow) ||
      String(capture.projection.listing.id) !== member.providerId ||
      capture.projection.listing.url !== member.canonicalUrl
    )
      throw new Error("pair_capture_binding_failed");
  }
  if (options.direct.arm.generationId === options.rendered.arm.generationId)
    throw new Error("pair_generation_reused");
  const documents = auditJllPairedDocs(
    member,
    options.direct.doc,
    options.rendered.doc,
  );
  const sourceProjection = auditJllSourceProjections(
    options.direct.projection!,
    options.rendered.projection!,
  );
  return {
    kind: "jll_paired_transport_observation_not_admission",
    cohortSha256,
    sampleId: member.sampleId,
    calibration: options.calibration,
    arms: { direct: options.direct.arm, rendered: options.rendered.arm },
    documents,
    sourceProjection,
    equivalent: documents.equivalent && sourceProjection.normalizedFactsEqual,
  };
}

/** All cells use identical members, no retries/cache, and stop at first regression.
 * C means client concurrency. Caller must report actual browser/queue capacity,
 * CPU/memory telemetry and other host workloads; this does not infer capacity.
 */
export async function runJllTransportMatrix(options: {
  cohort: JllBenchmarkCohort;
  capture: (
    transport: JllBenchmarkTransport,
    member: JllBenchmarkMember,
    signal: AbortSignal,
    context: JllArmContext,
  ) => Promise<JllBenchmarkCapture>;
  signal: AbortSignal;
  cellTimeoutMs?: number;
}) {
  // Snapshot the complete source rows once. Capture callbacks cannot mutate
  // source controls or identities used by later cells.
  const cohort = structuredClone(options.cohort);
  const members = cohort.members;
  const { capture, signal } = options;
  const cohortSha256 = jllCohortSha256(cohort);
  const cellTimeoutMs = options.cellTimeoutMs ?? 180000;
  if (
    !Number.isInteger(cellTimeoutMs) ||
    cellTimeoutMs < 1000 ||
    cellTimeoutMs > 300000
  )
    throw new Error("invalid_bounded_matrix");
  const cells = [];
  const usedGenerations = new Set<string>();
  for (const concurrency of [1, 2, 4, 8]) {
    const captures = new Map<JllBenchmarkTransport, JllBenchmarkCapture[]>();
    const measurements = [];
    for (const transport of concurrency === 2 || concurrency === 8
      ? (["rendered", "direct"] as const)
      : (["direct", "rendered"] as const)) {
      signal.throwIfAborted();
      const controller = new AbortController();
      const abort = () => controller.abort(signal.reason);
      signal.addEventListener("abort", abort, { once: true });
      const started = performance.now();
      const wallStarted = Date.now();
      const context: JllArmContext = {
        armId: `${transport}:c${concurrency}`,
        clientConcurrency: concurrency,
        startedAt: new Date(wallStarted).toISOString(),
      };
      let armGeneration: string | null = null;
      const rows: JllBenchmarkCapture[] = new Array(members.length);
      let next = 0;
      let completed = 0;
      const timer = setTimeout(
        () => controller.abort(new Error("matrix_cell_deadline")),
        cellTimeoutMs,
      );
      try {
        let rejectAbort!: () => void;
        const aborted = new Promise<never>((_, reject) => {
          rejectAbort = () => reject(controller.signal.reason);
          controller.signal.addEventListener("abort", rejectAbort, {
            once: true,
          });
        });
        try {
          await Promise.race([
            Promise.all(
              Array.from(
                { length: Math.min(concurrency, members.length) },
                async () => {
                  while (next < members.length) {
                    controller.signal.throwIfAborted();
                    const index = next++;
                    const row = structuredClone(
                      await capture(
                        transport,
                        structuredClone(members[index]!),
                        controller.signal,
                        { ...context },
                      ),
                    );
                    controller.signal.throwIfAborted();
                    if (row.attempts !== 1 || row.cacheDisposition !== "live")
                      throw new Error("noncold_or_retried_capture");
                    propertyData(members[index]!, row.doc);
                    const observation = row.doc.detailObservation;
                    const observed = Date.parse(observation?.observedAt ?? "");
                    const expectedMethod =
                      transport === "direct"
                        ? "jll_direct_candidate"
                        : "jll_detail";
                    if (
                      !observation ||
                      observation.cacheDisposition !== "live" ||
                      observation.method !== expectedMethod ||
                      !observation.generationId?.trim() ||
                      !Number.isFinite(observed) ||
                      observed < wallStarted ||
                      observed > Date.now() + 1000
                    )
                      throw new Error("capture_observation_not_bound_to_arm");
                    if (armGeneration === null) {
                      if (usedGenerations.has(observation.generationId))
                        throw new Error("capture_generation_reused");
                      armGeneration = observation.generationId;
                      usedGenerations.add(armGeneration);
                    } else if (armGeneration !== observation.generationId)
                      throw new Error("capture_generation_mixed");
                    if (row.projection) {
                      if (
                        canonical(row.projection.acquisition) !==
                          canonical(observation) ||
                        row.projection.documentSha256 !==
                          jllDocumentSha256(row.doc) ||
                        row.projection.baseRowSha256 !==
                          jllSourceRowSha256(members[index]!.baseRow)
                      )
                        throw new Error("projection_acquisition_mismatch");
                      if (
                        String(row.projection.listing.id) !==
                          members[index]!.providerId ||
                        row.projection.listing.url !==
                          members[index]!.canonicalUrl
                      )
                        throw new Error("projection_identity_mismatch");
                      auditOfflineJllProjections(
                        row.projection,
                        row.projection,
                      );
                    }
                    rows[index] = row;
                    completed++;
                  }
                },
              ),
            ),
            aborted,
          ]);
        } finally {
          controller.signal.removeEventListener("abort", rejectAbort);
        }
      } catch {
        controller.abort();
        cells.push({
          clientConcurrency: concurrency,
          measurements,
          audits: [],
          partial: {
            transport,
            dispatched: next,
            completed,
            elapsedMs: performance.now() - started,
          },
        });
        return {
          kind: "jll_transport_observation_not_admission",
          cohortSha256,
          complete: false,
          stoppedAtConcurrency: concurrency,
          failure: "capture_failed_or_cancelled",
          cells,
        };
      } finally {
        clearTimeout(timer);
        signal.removeEventListener("abort", abort);
        controller.abort();
      }
      const elapsedMs = performance.now() - started;
      captures.set(transport, rows);
      measurements.push({
        transport,
        clientConcurrency: concurrency,
        elapsedMs,
        freshCaptures: rows.length,
        generationId: armGeneration,
        armStartedAt: context.startedAt,
        clientAttempts: rows.length,
        clientRetries: 0,
        providerAttempts:
          transport === "direct" &&
          rows.every((row) =>
            Number.isInteger(row.doc.metadata?.providerAttempts),
          )
            ? rows.reduce(
                (sum, row) => sum + Number(row.doc.metadata?.providerAttempts),
                0,
              )
            : null,
        providerRetries: null,
        collectorCacheHits: 0,
        freshPagesPerMinute: (rows.length * 60000) / elapsedMs,
      });
    }
    const audits = members.map((member, index) => {
      const direct = captures.get("direct")![index]!;
      const rendered = captures.get("rendered")![index]!;
      const documentAudit = auditJllPairedDocs(
        member,
        direct.doc,
        rendered.doc,
      );
      const projectionAudit =
        direct.projection && rendered.projection
          ? auditOfflineJllProjections(direct.projection, rendered.projection)
          : null;
      return {
        sampleId: member.sampleId,
        providerId: member.providerId,
        canonicalUrl: member.canonicalUrl,
        ...documentAudit,
        sourceProjection: projectionAudit,
        equivalent:
          documentAudit.equivalent &&
          projectionAudit?.normalizedFactsEqual === true,
      };
    });
    cells.push({ clientConcurrency: concurrency, measurements, audits });
    if (audits.some((row) => !row.equivalent))
      return {
        kind: "jll_transport_observation_not_admission",
        cohortSha256,
        complete: false,
        stoppedAtConcurrency: concurrency,
        failure: "fidelity_regression",
        cells,
      };
  }
  return {
    kind: "jll_transport_observation_not_admission",
    cohortSha256,
    complete: true,
    failure: null,
    cells,
  };
}

/** Compare every real normalized fact, including withheld fields and Markdown.
 * Only acquisition clock/generation/method/cache metadata are removed; the
 * original acquisition and projection provenance remain in the audit result. Broker references resolve to
 * their actual records; their array order and all other array order stay exact.
 */
export function auditOfflineJllProjections(
  direct: OfflineJllProjection,
  rendered: OfflineJllProjection,
) {
  if (
    direct.kind !== "offline_generation_cache_projection" ||
    rendered.kind !== "offline_generation_cache_projection"
  )
    throw new Error("not_offline_projection");
  return auditJllSourceProjections(direct, rendered);
}

/** Compare an honest live real-enricher capture with offline parser replay. */
export function auditJllSourceProjections(
  direct: OfflineJllProjection | LiveJllProjection,
  rendered: OfflineJllProjection | LiveJllProjection,
) {
  const project = (capture: OfflineJllProjection | LiveJllProjection) => {
    const provenance = capture.listing.freshnessProvenance;
    const expected =
      capture.kind === "offline_generation_cache_projection"
        ? "generation_cache"
        : "live";
    if (
      ![
        "offline_generation_cache_projection",
        "live_real_source_capture",
      ].includes(capture.kind) ||
      provenance?.cacheDisposition !== expected ||
      (capture.kind === "live_real_source_capture" &&
        (provenance?.generationId !== capture.acquisition.generationId ||
          capture.listing.detailObservedAt !== capture.acquisition.observedAt ||
          capture.acquisition.cacheDisposition !== "live" ||
          capture.acquisition.method !== "jll_detail" ||
          provenance?.method !== "jll_detail"))
    )
      throw new Error("not_offline_projection");
    const row = structuredClone(capture.listing);
    delete row.detailObservedAt;
    delete row.freshnessProvenance.generationId;
    delete row.freshnessProvenance.method;
    delete row.freshnessProvenance.cacheDisposition;
    if (Array.isArray(row.brokerIds))
      row.brokerIds = row.brokerIds.map((id: unknown) => {
        if (
          typeof id !== "number" ||
          !Number.isInteger(id) ||
          id < 0 ||
          !capture.brokers[id]
        )
          throw new Error("invalid_projection_broker_reference");
        return capture.brokers[id];
      });
    return digest(canonical(row));
  };
  const directSha256 = project(direct),
    renderedSha256 = project(rendered);
  return {
    normalizedFactsEqual: directSha256 === renderedSha256,
    directSha256,
    renderedSha256,
    acquisition: { direct: direct.acquisition, rendered: rendered.acquisition },
    projectionProvenance: {
      direct: structuredClone(direct.listing.freshnessProvenance),
      rendered: structuredClone(rendered.listing.freshnessProvenance),
    },
  };
}
