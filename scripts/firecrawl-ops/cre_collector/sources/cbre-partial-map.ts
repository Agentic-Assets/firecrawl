/** Map provisional CBRE observations without admitting inventory or writing. */
import { isDeepStrictEqual } from "node:util";
import { mapCbreDocument } from "./cbre.js";

type Observation = {
  document: unknown;
  observed_at: string;
  request_transaction: "sale" | "lease";
  [key: string]: unknown;
};
type Candidate = { source_id: string; observations: Observation[] };
type Projection = {
  source_key: "cbre";
  generation: string;
  attempt: number;
  sidecar_sha256: string;
  whole_source_coverage: false;
  write_authorized: false;
  candidates: Candidate[];
  held: unknown[];
};

/**
 * The input must be independently rederived and rebound to custody and target
 * before any future write. Its request transaction is not a tenure assertion.
 * This result is intentionally not the ordinary collector artifact format.
 */
export function mapCbrePartialProjection(projection: Projection) {
  if (!projection || projection.source_key !== "cbre"
      || typeof projection.generation !== "string" || !projection.generation
      || !Number.isSafeInteger(projection.attempt) || projection.attempt < 1
      || !/^[a-f0-9]{64}$/.test(projection.sidecar_sha256)
      || projection.whole_source_coverage !== false || projection.write_authorized !== false
      || !Array.isArray(projection.candidates) || !Array.isArray(projection.held)) {
    throw new Error("invalid CBRE partial projection");
  }
  const normalized = (value: unknown): string | null => {
    if (typeof value !== "string") return null;
    return value.replace(/\s+/g, " ").trim() || null;
  };
  const tainted = new Set<string>();
  const collectIdentities = (value: unknown, seen = new WeakSet<object>()): void => {
    if (!value || typeof value !== "object" || seen.has(value)) return;
    seen.add(value);
    if (Array.isArray(value)) {
      for (const item of value) collectIdentities(item, seen);
      return;
    }
    for (const [key, item] of Object.entries(value)) {
      if (key === "source_id" || key === "Common.PrimaryKey") {
        const id = normalized(item);
        if (id) tainted.add(id);
      }
      collectIdentities(item, seen);
    }
  };
  for (const item of projection.held) collectIdentities(item);
  const counts = new Map<string, number>();
  for (const candidate of projection.candidates) {
    if (candidate && typeof candidate.source_id === "string") {
      counts.set(candidate.source_id, (counts.get(candidate.source_id) ?? 0) + 1);
    }
  }
  const validCandidate = (candidate: Candidate): boolean => {
    if (!candidate || typeof candidate.source_id !== "string"
        || !/^[A-Za-z0-9_-]+$/.test(candidate.source_id)
        || counts.get(candidate.source_id) !== 1
        || !Array.isArray(candidate.observations) || candidate.observations.length === 0) return false;
    const first = candidate.observations[0];
    if (!first || !first.document || typeof first.document !== "object" || Array.isArray(first.document)
        || (first.document as Record<string, unknown>)["Common.PrimaryKey"] !== candidate.source_id) return false;
    return candidate.observations.every((observation) => observation
      && typeof observation.observed_at === "string" && observation.observed_at.length > 0
      && ["sale", "lease"].includes(observation.request_transaction)
      && isDeepStrictEqual(observation.document, first.document));
  };
  for (const candidate of projection.candidates) {
    if (!validCandidate(candidate)) collectIdentities(candidate);
  }
  const mapped: Array<{ source_id: string; row: ReturnType<typeof mapCbreDocument>; observations: Observation[] }> = [];
  const held: unknown[] = [...projection.held];
  for (const candidate of projection.candidates) {
    try {
      if (!validCandidate(candidate) || tainted.has(candidate.source_id)) throw new Error("invalid identity");
      const first = candidate.observations[0];
      const row = mapCbreDocument(first.document, first.request_transaction, {
        observedAt: first.observed_at, generationId: projection.generation,
        method: "cbre_listings_api", cacheDisposition: "live",
      }, true);
      mapped.push({ source_id: candidate.source_id, row, observations: candidate.observations });
    } catch {
      // Preserve every observation and let later peers continue. No target
      // identity, lifecycle status, or write permission is inferred here.
      held.push({ reason: "mapping_or_identity_invalid", candidate });
    }
  }
  return { source_key: "cbre" as const, generation: projection.generation, attempt: projection.attempt,
    sidecar_sha256: projection.sidecar_sha256, whole_source_coverage: false as const,
    write_authorized: false as const, mapped, held };
}
