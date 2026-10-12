// AGENTIC-2898: offline audit only. Run in a dedicated foreground child.
// Cache projection is never acquisition, ingestion, or C10 admission evidence.
import { lstatSync, readdirSync } from "node:fs";
import { isAbsolute } from "node:path";
import type { ScrapedDoc, DetailObservation } from "../types.js";
import {
  auditJllPairedDocs,
  jllDocumentSha256,
  jllSourceRowSha256,
} from "./jll-direct-benchmark.js";

type Member = {
  sampleId: string;
  providerId: string;
  canonicalUrl: string;
  baseRow: Record<string, unknown>;
};
export type OfflineJllProjection = {
  kind: "offline_generation_cache_projection";
  listing: Record<string, any>;
  brokers: unknown[];
  acquisition: DetailObservation;
  documentSha256: string;
  baseRowSha256: string;
};
let active = false;

/** Uses the unchanged real source parser with dispatch forbidden. The caller
 * supplies a dedicated empty owner-only cache and generation, never live cache.
 * Inputs and outputs are private audit material, not collector artifacts.
 */
export async function withOfflineJllProjection<T>(
  run: (
    project: (member: Member, doc: ScrapedDoc) => Promise<OfflineJllProjection>,
  ) => Promise<T>,
): Promise<T> {
  const cache = process.env.JLL_DETAIL_CACHE_DIR;
  const generation = process.env.CRE_REFRESH_GENERATION;
  const started = Date.parse(process.env.CRE_REFRESH_STARTED_AT ?? "");
  if (
    active ||
    !cache ||
    !isAbsolute(cache) ||
    !generation ||
    !Number.isFinite(started) ||
    process.env.CRE_REQUIRE_FRESH_DETAILS !== "1"
  )
    throw new Error("offline_projection_configuration");
  const info = lstatSync(cache);
  if (
    !info.isDirectory() ||
    info.isSymbolicLink() ||
    info.uid !== process.getuid?.() ||
    (info.mode & 0o077) !== 0 ||
    readdirSync(cache).length
  )
    throw new Error("offline_projection_cache_not_empty_owned_directory");
  active = true;
  const originalFetch = globalThis.fetch;
  let attemptedDispatches = 0;
  const inFlight = new Set<string>();
  globalThis.fetch = async () => {
    attemptedDispatches++;
    throw new Error("offline_projection_dispatch_forbidden");
  };
  try {
    const source = await import("./jll.js");
    const broker = await import("../lib/broker.js");
    return await run(async (member, doc) => {
      member = structuredClone(member);
      doc = structuredClone(doc);
      const observation = doc.detailObservation;
      if (
        !member.sampleId ||
        String(member.baseRow.id) !== member.providerId ||
        member.baseRow.url !== member.canonicalUrl ||
        !observation ||
        observation.generationId !== generation ||
        observation.cacheDisposition !== "live" ||
        !["jll_detail", "jll_direct_candidate"].includes(observation.method) ||
        !Number.isFinite(Date.parse(observation.observedAt)) ||
        Date.parse(observation.observedAt) < started ||
        Date.parse(observation.observedAt) > Date.now() + 1000
      )
        throw new Error("offline_projection_input_binding");
      if (inFlight.has(member.canonicalUrl))
        throw new Error("offline_projection_concurrent_same_url");
      inFlight.add(member.canonicalUrl);
      try {
        // Validate full primary identity before allowing the unchanged cache parser.
        auditJllPairedDocs(member, doc, doc);
        const dispatchedBefore = attemptedDispatches;
        source.writeJllDetailCache(member.canonicalUrl, doc);
        const listing = await source.enrichJllListing(
          structuredClone(member.baseRow),
        );
        if (attemptedDispatches !== dispatchedBefore)
          throw new Error("offline_projection_dispatch_blocked");
        if (
          listing.detailError ||
          listing.skip ||
          listing.preserveChildCollections ||
          String(listing.id) !== member.providerId ||
          listing.url !== member.canonicalUrl ||
          listing.detailObservedAt !== observation.observedAt ||
          listing.freshnessProvenance?.generationId !== generation ||
          listing.freshnessProvenance?.cacheDisposition !== "generation_cache"
        )
          throw new Error("offline_projection_failed");
        return {
          kind: "offline_generation_cache_projection",
          listing,
          brokers: structuredClone(broker.brokers),
          acquisition: structuredClone(observation),
          documentSha256: jllDocumentSha256(doc),
          baseRowSha256: jllSourceRowSha256(member.baseRow),
        };
      } finally {
        inFlight.delete(member.canonicalUrl);
      }
    });
  } finally {
    globalThis.fetch = originalFetch;
    active = false;
  }
}

export { auditOfflineJllProjections } from "./jll-direct-benchmark.js";
