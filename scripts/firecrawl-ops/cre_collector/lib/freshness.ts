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
  // Provisional source-index cards are not canonical listing evidence. Keep
  // their separate accounting in the artifact/index lane rather than allowing
  // them to weaken a canonical source freshness denominator.
  const canonicalListings = listings.filter((row) => row?.inventoryOnly == null);
  const boundary = refreshStartedAt();
  const boundaryMs = boundary ? Date.parse(boundary) : null;
  const stale = (value: unknown): boolean => {
    if (boundaryMs === null) return false;
    if (typeof value !== "string") return true;
    const valueMs = Date.parse(value);
    return !Number.isFinite(valueMs) || valueMs < boundaryMs;
  };
  return {
    listings: canonicalListings.length,
    inventoryObserved: canonicalListings.filter((row) => typeof row?.inventoryObservedAt === "string").length,
    detailObserved: canonicalListings.filter((row) => typeof row?.detailObservedAt === "string").length,
    authoritativeInventoryFeed: canonicalListings.filter(
      (row) => row?.freshnessProvenance?.detailScope === "authoritative_inventory_feed"
    ).length,
    detailErrors: canonicalListings.filter((row) => Boolean(row?.detailError)).length,
    childPreservationRows: canonicalListings.filter((row) => row?.preserveChildCollections === true).length,
    staleInventoryObservations: canonicalListings.filter((row) => stale(row?.inventoryObservedAt)).length,
    staleDetailObservations: canonicalListings.filter((row) => {
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
