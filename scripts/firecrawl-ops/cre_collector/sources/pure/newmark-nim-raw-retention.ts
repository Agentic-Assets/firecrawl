import { snapshotJsonValue } from "../../lib/json-value.js";
import { clean } from "../../lib/util.js";
import type { CacheDisposition, Tx } from "../../types.js";

const MAX_RAW_BYTES = 1_000_000;
export type NewmarkNimRawRetention = {
  schema: "newmark-nim-raw-retention-v1";
  status: "complete" | "omitted";
  sourceKey: "newmark";
  binding: "id_and_url";
  transactionMode: Tx;
  listingId: string;
  listingUrl: string;
  providerId: string | null;
  inventoryObservedAt: string;
  cacheDisposition: CacheDisposition;
  reason?: "invalid_or_over_limit" | "binding_mismatch";
};

/** Snapshot parsed JSON, not original response bytes or numeric lexemes. */
export function captureNewmarkNimRaw(value: unknown, transactionMode: Tx, listingId: string,
  listingUrl: string, providerId: string | null, inventoryObservedAt: string,
  cacheDisposition: CacheDisposition): { raw?: unknown; marker: NewmarkNimRawRetention } {
  const marker: NewmarkNimRawRetention = { schema: "newmark-nim-raw-retention-v1", status: "omitted",
    sourceKey: "newmark", binding: "id_and_url", transactionMode, listingId, listingUrl, providerId, inventoryObservedAt, cacheDisposition };
  try { return { raw: snapshotJsonValue(value, MAX_RAW_BYTES), marker: { ...marker, status: "complete" } }; }
  catch { return { marker: { ...marker, reason: "invalid_or_over_limit" } }; }
}

export function preserveNewmarkNimRaw(listing: any, raw: unknown, marker?: NewmarkNimRawRetention): any {
  if (!listing || typeof listing !== "object" || !marker) return listing;
  const source = raw && typeof raw === "object" ? raw as Record<string, unknown> : undefined;
  const provenance = listing.freshnessProvenance;
  const bound = marker.sourceKey === "newmark" && marker.binding === "id_and_url" &&
    (marker.transactionMode === "sale" || marker.transactionMode === "lease") &&
    listing.id === marker.listingId && listing.url === marker.listingUrl &&
    (listing.sourceKey === undefined || listing.sourceKey === marker.sourceKey) &&
    (listing.transactionMode === undefined || listing.transactionMode === marker.transactionMode) &&
    listing.inventoryObservedAt === marker.inventoryObservedAt &&
    provenance?.detailScope === "authoritative_inventory_feed" &&
    provenance?.method === "newmark_nim_public_inventory" &&
    provenance?.cacheDisposition === marker.cacheDisposition &&
    (marker.status !== "complete" || (source !== undefined && clean(source.id) === marker.providerId));
  if (marker.status === "complete" && bound) listing.rawNewmarkNimRecord = raw;
  else delete listing.rawNewmarkNimRecord;
  listing.newmarkNimRawRetention = marker.status === "complete" && !bound
    ? { ...marker, status: "omitted", reason: "binding_mismatch" } : marker;
  return listing;
}
