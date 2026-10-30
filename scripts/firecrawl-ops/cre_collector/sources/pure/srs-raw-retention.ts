import { preservesJsonValue } from "../../lib/json-value.js";

// This represents parsed JSON values, not original bytes or number lexemes.
const SRS_RAW_MAX_BYTES = 1_000_000;
export type SrsRawRetention = {
  schema: "srs-raw-retention-v1";
  status: "complete" | "omitted";
  binding: "id_and_url" | "id_only" | "url_only" | "unbound";
  listingId: string | null;
  listingUrl: string | null;
  reason?: "binding_unavailable" | "invalid_or_over_limit";
};

export function captureSrsRaw(value: unknown, listingId: string | null, listingUrl: string | null): {
  raw?: unknown; marker: SrsRawRetention;
} {
  const binding = listingId ? (listingUrl ? "id_and_url" : "id_only") : (listingUrl ? "url_only" : "unbound");
  const marker: SrsRawRetention = { schema: "srs-raw-retention-v1", status: "omitted", binding, listingId, listingUrl };
  try {
    if (!preservesJsonValue(value)) throw new Error();
    const text = JSON.stringify(value);
    if (Buffer.byteLength(text, "utf8") > SRS_RAW_MAX_BYTES) throw new Error();
    const raw = JSON.parse(text);
    return { raw, marker: { ...marker, status: "complete" } };
  } catch {
    return { marker: { ...marker, reason: "invalid_or_over_limit" } };
  }
}

// Restore after ordinary prune without altering the shared pruning contract.
export function preserveSrsRaw(listing: any, raw: unknown, marker?: SrsRawRetention): any {
  if (!listing || typeof listing !== "object" || !marker) return listing;
  const bound = (listing.id ?? null) === marker.listingId && (listing.url ?? null) === marker.listingUrl;
  if (marker.status === "complete" && bound) listing.rawSrs = raw;
  else delete listing.rawSrs;
  listing.srsRawRetention = marker.status === "complete" && !bound
    ? { ...marker, status: "omitted", reason: "binding_unavailable" } : marker;
  return listing;
}
