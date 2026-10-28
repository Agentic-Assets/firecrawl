/** Bounded, unqualified evidence from a caller-supplied enrichment result. */
import type { ScrapedDoc } from "../../types.js";
import { captureSourcePageContext } from "../source-page-context/index.js";
import { RetainedSourceError } from "../source-observations.js";

export type EnrichmentClaim = { sourceKey: string; externalId: string; url: string; transaction?: "sale" | "lease" };
const FIELD_BYTES = 256 * 1024;
const HTML_CONTEXT_BYTES = 2_000_000;
const OBSERVATION_BYTES = 2_500_000;
const GROUP_BYTES = 8 * 1024 * 1024;
const MAX_OBSERVATIONS = 10000;

function jsonBytes(value: unknown): number {
  const serialized = JSON.stringify(value);
  if (serialized === undefined) throw new Error("value is not JSON serializable");
  return Buffer.byteLength(serialized, "utf8");
}

/** Refuse values that JSON.stringify would silently transform or omit. */
function preservesJsonValue(value: unknown): boolean {
  const active = new Set<object>();
  let nodes = 0;
  const visit = (item: unknown, depth: number): boolean => {
    if (++nodes > 100000 || depth > 128) return false;
    if (item === null || typeof item === "string" || typeof item === "boolean") return true;
    if (typeof item === "number") return Number.isFinite(item) && !Object.is(item, -0);
    if (typeof item !== "object" || active.has(item)) return false;
    const array = Array.isArray(item);
    const prototype = Object.getPrototypeOf(item);
    if (array && prototype !== Array.prototype) return false;
    if (!array && prototype !== Object.prototype && prototype !== null) return false;
    active.add(item);
    try {
      const keys = Reflect.ownKeys(item);
      if (keys.some((key) => typeof key !== "string")) return false;
      if (array) {
        const values = item as unknown[];
        if (keys.some((key) => key !== "length" && !/^(?:0|[1-9]\d*)$/.test(key as string)) ||
            keys.length !== values.length + 1) return false;
        for (let index = 0; index < values.length; index++) {
          const descriptor = Object.getOwnPropertyDescriptor(item, String(index));
          if (!descriptor || !("value" in descriptor) || !descriptor.enumerable || !visit(descriptor.value, depth + 1)) return false;
        }
      } else {
        for (const key of keys as string[]) {
          const descriptor = Object.getOwnPropertyDescriptor(item, key);
          if (!descriptor || !("value" in descriptor) || !descriptor.enumerable || !visit(descriptor.value, depth + 1)) return false;
        }
      }
      return true;
    } finally { active.delete(item); }
  };
  return visit(value, 0);
}

function witness(item: EnrichmentClaim) {
  return { sourceKey: item.sourceKey, externalId: item.externalId, url: item.url,
    transaction: item.transaction ?? null, identity: "unverified" as const };
}

export function captureEnrichmentEvidence(item: EnrichmentClaim, doc: ScrapedDoc, capturedAt: string | null) {
  const fields: Record<string, unknown> = {};
  const omissions: Record<string, string> = {};
  for (const key of ["rawHtml", "markdown", "links", "images", "attributes", "metadata", "detailObservation"] as const) {
    if (!Object.prototype.hasOwnProperty.call(doc, key)) continue;
    try {
      const original = doc[key];
      if (!preservesJsonValue(original)) { omissions[key] = "non_json_value"; continue; }
      const serialized = JSON.stringify(original);
      if (serialized === undefined) omissions[key] = "non_json_value";
      else if (Buffer.byteLength(serialized, "utf8") > FIELD_BYTES) omissions[key] = "field_byte_limit";
      else fields[key] = JSON.parse(serialized);
    } catch {
      omissions[key] = "not_json_serializable";
    }
  }
  let pageContext: ReturnType<typeof captureSourcePageContext> | null = null;
  if (typeof doc.rawHtml === "string") {
    if (Buffer.byteLength(doc.rawHtml, "utf8") > HTML_CONTEXT_BYTES) omissions.pageContext = "html_byte_limit";
    else {
      try {
        pageContext = captureSourcePageContext(doc.rawHtml, { sourceUrl: item.url, detailObservedAt: capturedAt ?? undefined });
      } catch {
        omissions.pageContext = "context_capture_failed";
      }
    }
  } else omissions.pageContext = "raw_html_unavailable";
  const result = { event: "generic_enrichment_observation", schema: "cre-generic-enrichment-observation/v1",
    claim: witness(item), capturedAt, captureClock: capturedAt === null ? "unavailable" : "collector_after_scrape_return",
    pageContextClockOrigin: capturedAt === null ? "unavailable" : "collector_after_scrape_return_not_provider_freshness",
    sourceFreshness: "unverified", cacheDisposition: "unverified", pageContext, scrapedDoc: fields, omissions };
  if (jsonBytes(result) <= OBSERVATION_BYTES) return result;
  const fieldOmissions = Object.fromEntries(Object.keys(fields).map((key) => [key, "observation_byte_limit"]));
  return { ...result, pageContext: null, scrapedDoc: {},
    omissions: { ...omissions, ...fieldOmissions, ...(pageContext === null ? {} : { pageContext: "observation_byte_limit" }),
      observation: "observation_byte_limit" } };
}

export function failedEnrichmentEvidence(item: EnrichmentClaim, failure: "scrape_failed" | "evidence_capture_failed" = "scrape_failed") {
  return { event: "generic_enrichment_failure", schema: "cre-generic-enrichment-observation/v1",
    claim: witness(item), failure, sourceFreshness: "unverified", cacheDisposition: "unverified" };
}

/** Retain input order and fail visibly if even explicit omission markers cannot fit. */
export function boundEnrichmentObservations(values: readonly unknown[]): unknown[] {
  const output: unknown[] = [];
  let bytes = 2;
  for (const value of values) {
    if (output.length >= MAX_OBSERVATIONS) {
      throw new RetainedSourceError(new Error("enrichment evidence count exceeds bound"), output);
    }
    const length = jsonBytes(value) + 1;
    if (bytes + length > GROUP_BYTES) {
      throw new RetainedSourceError(new Error("enrichment evidence bytes exceed bound"), output);
    }
    bytes += length;
    output.push(value);
  }
  return output;
}

/** Evidence overflow must not discard independently mapped listing rows. */
export function limitEnrichmentObservations(values: readonly unknown[]): {
  retainedObservations: readonly unknown[]; evidenceError?: string;
} {
  try { return { retainedObservations: boundEnrichmentObservations(values) }; }
  catch (error) {
    if (!(error instanceof RetainedSourceError)) throw error;
    return { retainedObservations: error.retainedObservations, evidenceError: error.message };
  }
}
