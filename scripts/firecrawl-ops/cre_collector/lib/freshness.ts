import { CacheDisposition, DetailObservation } from "../types.js";

export function refreshGenerationId(): string | null {
  const value = process.env.CRE_REFRESH_GENERATION?.trim();
  return value || null;
}

export function refreshStartedAt(): string | null {
  const value = process.env.CRE_REFRESH_STARTED_AT?.trim();
  return value && Number.isFinite(Date.parse(value)) ? value : null;
}

export function requireFreshDetails(): boolean {
  return ["1", "true", "yes", "on"].includes(
    (process.env.CRE_REQUIRE_FRESH_DETAILS ?? "").toLowerCase()
  );
}

export function requireFreshPropertyDetails(): boolean {
  return ["1", "true", "yes", "on"].includes(
    (process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS ?? "").toLowerCase()
  );
}

export function generationMatches(generationId: unknown): boolean {
  const expected = refreshGenerationId();
  if (!expected) return !requireFreshDetails();
  return generationId === expected;
}

export function detailObservation(
  method: string,
  cacheDisposition: CacheDisposition,
  observedAt = new Date().toISOString(),
  extra: Partial<DetailObservation> = {}
): DetailObservation {
  return {
    observedAt,
    generationId: refreshGenerationId(),
    method,
    cacheDisposition,
    ...extra,
  };
}

export type ListingFreshnessSummary = {
  listings: number;
  inventoryObserved: number;
  detailObserved: number;
  authoritativeInventoryFeed: number;
  detailErrors: number;
  childPreservationRows: number;
  staleInventoryObservations: number;
  staleDetailObservations: number;
};

export function summarizeListingFreshness(listings: any[]): ListingFreshnessSummary {
  const boundary = refreshStartedAt();
  const boundaryMs = boundary ? Date.parse(boundary) : null;
  const stale = (value: unknown): boolean => {
    if (boundaryMs === null) return false;
    if (typeof value !== "string") return true;
    const valueMs = Date.parse(value);
    return !Number.isFinite(valueMs) || valueMs < boundaryMs;
  };
  return {
    listings: listings.length,
    inventoryObserved: listings.filter((row) => typeof row?.inventoryObservedAt === "string").length,
    detailObserved: listings.filter((row) => typeof row?.detailObservedAt === "string").length,
    authoritativeInventoryFeed: listings.filter(
      (row) => row?.freshnessProvenance?.detailScope === "authoritative_inventory_feed"
    ).length,
    detailErrors: listings.filter((row) => Boolean(row?.detailError)).length,
    childPreservationRows: listings.filter((row) => row?.preserveChildCollections === true).length,
    staleInventoryObservations: listings.filter((row) => stale(row?.inventoryObservedAt)).length,
    staleDetailObservations: listings.filter((row) => {
      if (row?.freshnessProvenance?.detailScope === "authoritative_inventory_feed") {
        return false;
      }
      if (
        row?.freshnessProvenance?.cacheDisposition === "source_revision_cache" &&
        !stale(row?.freshnessProvenance?.validatedAt)
      ) {
        return false;
      }
      return stale(row?.detailObservedAt);
    }).length,
  };
}
