import { snapshotJsonValue } from "../../lib/json-value.js";
import type { CacheDisposition } from "../../types.js";

const MAX_RAW_BYTES = 1_000_000;
export type KidderRawRetention = {
  schema: "kidder-raw-retention-v1";
  status: "complete" | "omitted";
  sourceKey: "kidder-mathews";
  binding: "id_and_url" | "id_only" | "url_only" | "unbound";
  listingId: string | null;
  listingUrl: string | null;
  inventoryObservedAt: string | null;
  cacheDisposition: CacheDisposition | null;
  reason?: "invalid_or_over_limit" | "binding_mismatch";
};

/** Retain parsed values, not original HTTP bytes or numeric lexemes. */
export function captureKidderRaw(value: unknown, listingId: string | null, listingUrl: string | null,
  inventoryObservedAt: string | null, cacheDisposition: CacheDisposition | null): { raw?: unknown; marker: KidderRawRetention } {
  const binding = listingId ? (listingUrl ? "id_and_url" : "id_only") : (listingUrl ? "url_only" : "unbound");
  const marker: KidderRawRetention = { schema: "kidder-raw-retention-v1", status: "omitted",
    sourceKey: "kidder-mathews", binding, listingId, listingUrl, inventoryObservedAt, cacheDisposition };
  try {
    return { raw: snapshotJsonValue(value, MAX_RAW_BYTES), marker: { ...marker, status: "complete" } };
  } catch {
    return { marker: { ...marker, reason: "invalid_or_over_limit" } };
  }
}

export function preserveKidderRaw(listing: any, raw: unknown, marker?: KidderRawRetention): any {
  if (!listing || typeof listing !== "object" || !marker) return listing;
  const freshness = listing.freshnessProvenance;
  const bound = (listing.id ?? null) === marker.listingId && (listing.url ?? null) === marker.listingUrl &&
    (listing.inventoryObservedAt ?? null) === marker.inventoryObservedAt &&
    (marker.inventoryObservedAt === null ? freshness == null && marker.cacheDisposition === null :
      freshness != null && freshness.cacheDisposition === marker.cacheDisposition) &&
    (listing.sourceKey === undefined || listing.sourceKey === marker.sourceKey);
  if (marker.status === "complete" && bound) listing.rawKidder = raw;
  else delete listing.rawKidder;
  listing.kidderRawRetention = marker.status === "complete" && !bound
    ? { ...marker, status: "omitted", reason: "binding_mismatch" } : marker;
  return listing;
}
