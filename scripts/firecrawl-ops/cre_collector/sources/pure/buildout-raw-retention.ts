import { snapshotJsonValue } from "../../lib/json-value.js";
import { allBuildoutFeeds } from "../buildout-feeds/index.js";

const BUILDOUT_RAW_MAX_BYTES = 1_000_000;
type BuildoutScope = { sourceKey: string; feedKey: string };
export type BuildoutRawRetention = {
  schema: "buildout-raw-retention-v1";
  status: "complete" | "omitted";
  binding: "id_and_url" | "id_only" | "url_only" | "unbound";
  sourceKey: string | null;
  feedKey: string | null;
  listingId: string | null;
  listingUrl: string | null;
  inventoryObservedAt: string | null;
  reason?: "invalid_or_over_limit" | "binding_mismatch";
};

// Scope is qualified by the registry, not inferred from a token or hostname.
export function buildoutRawScope(company: string, pluginKey: string, listingsPage: string,
  cacheSlug: string | undefined): BuildoutScope | null {
  const feed = allBuildoutFeeds.find((value) => value.pluginKey === pluginKey &&
    value.feedKey === cacheSlug && value.listingsPage === listingsPage && value.company === company);
  return feed ? { sourceKey: feed.sourceKey, feedKey: feed.feedKey } : null;
}

export function captureBuildoutRaw(value: unknown, listingId: string | null, listingUrl: string | null,
  inventoryObservedAt: string | null, scope: BuildoutScope | null): { raw?: unknown; marker: BuildoutRawRetention } {
  const binding = listingId ? (listingUrl ? "id_and_url" : "id_only") : (listingUrl ? "url_only" : "unbound");
  const marker: BuildoutRawRetention = { schema: "buildout-raw-retention-v1", status: "omitted", binding,
    listingId, listingUrl, inventoryObservedAt, sourceKey: scope?.sourceKey ?? null, feedKey: scope?.feedKey ?? null };
  try {
    return { raw: snapshotJsonValue(value, BUILDOUT_RAW_MAX_BYTES), marker: { ...marker, status: "complete" } };
  } catch {
    return { marker: { ...marker, reason: "invalid_or_over_limit" } };
  }
}

/** Restore only the captured row with its mapper, clock and optional source binding intact. */
export function preserveBuildoutRaw(listing: any, raw: unknown, marker?: BuildoutRawRetention,
  sourceKey?: string): any {
  if (!listing || typeof listing !== "object" || !marker) return listing;
  const bound = (listing.id ?? null) === marker.listingId && (listing.url ?? null) === marker.listingUrl &&
    (listing.inventoryObservedAt ?? null) === marker.inventoryObservedAt &&
    (sourceKey === undefined || marker.sourceKey === null || sourceKey === marker.sourceKey);
  if (marker.status === "complete" && bound) listing.rawBuildout = raw;
  else delete listing.rawBuildout;
  listing.buildoutRawRetention = marker.status === "complete" && !bound
    ? { ...marker, status: "omitted", reason: "binding_mismatch" } : marker;
  return listing;
}
