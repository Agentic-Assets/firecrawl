import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import {
  generationMatches,
  refreshGenerationId,
  requireFreshDetails,
} from "../lib/freshness.js";
import { clean, pmap, prune } from "../lib/util.js";
import { CacheDisposition, SourceResult, Tx } from "../types.js";

const KIDDER_API = "https://services.kidder.com/search/public/listing";
const KIDDER_PAGE_SIZE = 50;

export type KidderInventoryResult = {
  items: any[];
  total: number;
  truncated: boolean;
  inventoryObservedAt: string;
  generationId: string | null;
  strictValidated: boolean;
};

export type KidderInventoryPageEvidence = {
  total: number;
  pageSize: number;
};

export type KidderMappingContext = {
  inventoryObservedAt?: string;
  generationId?: string | null;
  cacheDisposition?: CacheDisposition;
  strict?: boolean;
};

let kidderCache: KidderInventoryResult | null = null;

function numeric(value: any): number | null {
  return value != null && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
}

function kidderProviderIdentity(row: any): string | null {
  const value = row?.listing_key;
  if (typeof value === "string") return clean(value);
  return typeof value === "number" && Number.isFinite(value) ? String(value) : null;
}

export function assertKidderInventoryPage(
  data: any,
  page: number,
  expected: KidderInventoryPageEvidence | null = null,
  strict = requireFreshDetails()
): KidderInventoryPageEvidence {
  if (!strict) {
    return {
      total: Number.isFinite(Number(data?.totalResultCount))
        ? Number(data.totalResultCount)
        : 0,
      pageSize: KIDDER_PAGE_SIZE,
    };
  }
  if (!data || typeof data !== "object" || !Array.isArray(data.results)) {
    throw new Error(`Kidder strict inventory page ${page} requires a results array`);
  }
  const total = Number(data.totalResultCount);
  if (!Number.isFinite(total) || !Number.isInteger(total) || total < 0) {
    throw new Error(
      `Kidder strict inventory page ${page} requires a valid integer totalResultCount`
    );
  }
  if (expected && total !== expected.total) {
    throw new Error(
      `Kidder strict inventory total changed from ${expected.total} to ${total} on page ${page}`
    );
  }
  const allPages = Math.max(1, Math.ceil(total / KIDDER_PAGE_SIZE));
  if (!Number.isInteger(page) || page < 0 || page >= allPages) {
    throw new Error(`Kidder strict inventory requested invalid page ${page} for total ${total}`);
  }
  const expectedRows = Math.max(
    0,
    Math.min(KIDDER_PAGE_SIZE, total - page * KIDDER_PAGE_SIZE)
  );
  if (data.results.length !== expectedRows) {
    throw new Error(
      `Kidder strict inventory page ${page} expected ${expectedRows} rows, received ${data.results.length}`
    );
  }
  return { total, pageSize: KIDDER_PAGE_SIZE };
}

export function assertKidderInventoryReconciled(
  items: any[],
  total: number,
  strict = requireFreshDetails()
): void {
  if (!strict) return;
  const identities = new Set<string>();
  for (const [index, row] of items.entries()) {
    const identity = kidderProviderIdentity(row);
    if (!identity) {
      throw new Error(`Kidder strict inventory row ${index} requires nonempty listing_key`);
    }
    if (identities.has(identity)) {
      throw new Error(`Kidder strict inventory duplicate provider identity ${identity}`);
    }
    identities.add(identity);
  }
  if (identities.size !== total) {
    throw new Error(
      `Kidder strict inventory expected ${total} unique rows, reconciled ${identities.size}`
    );
  }
}

function strictGeneration(strict: boolean): string | null {
  const generationId = refreshGenerationId();
  if (strict && !generationId) {
    throw new Error("Kidder strict inventory requires CRE_REFRESH_GENERATION");
  }
  return generationId;
}

async function kidderPost(startIndex: number, strict = requireFreshDetails()): Promise<any> {
  const body = JSON.stringify({ startIndex, numResults: KIDDER_PAGE_SIZE, includeAggregations: false });
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(KIDDER_API, {
        method: "POST",
        headers: {
          "Content-Type": "application/json;charset=UTF-8",
          "User-Agent": "Mozilla/5.0",
          Origin: "https://www.kidder.com",
          ...(strict ? { "Cache-Control": "no-cache" } : {}),
        },
        body,
        ...(strict ? { cache: "no-store" as const } : {}),
      });
      if (!res.ok) throw new Error(`Kidder API HTTP ${res.status}`);
      const data: any = await res.json();
      if (data && Array.isArray(data.results)) return data;
      throw new Error("Kidder API response missing results[]");
    } catch (err) {
      if (attempt === 3) throw err;
      await new Promise((resolve) => setTimeout(resolve, 2000 * attempt));
    }
  }
}

export async function kidderFetchAll(max: number): Promise<KidderInventoryResult> {
  const strict = requireFreshDetails();
  const generationId = strictGeneration(strict);
  if (
    kidderCache &&
    (!strict || (kidderCache.strictValidated && generationMatches(kidderCache.generationId)))
  ) {
    return {
      ...kidderCache,
      items: kidderCache.items.map((row) => ({
        ...row,
        __creInventoryCacheDisposition: "generation_cache",
      })),
    };
  }
  if (kidderCache) kidderCache = null;
  const first = await kidderPost(0, strict);
  const firstPage = assertKidderInventoryPage(first, 0, null, strict);
  const total: number = strict ? firstPage.total : first.totalResultCount ?? 0;
  const items: any[] = [...(first.results ?? [])];
  const allPages = Math.ceil(total / KIDDER_PAGE_SIZE);
  const wantPages = strict
    ? allPages
    : Number.isFinite(max)
      ? Math.min(allPages, Math.ceil((max * 4) / KIDDER_PAGE_SIZE) + 1)
      : allPages;
  let failed = 0;
  const pageNums = Array.from({ length: Math.max(0, wantPages - 1) }, (_, index) => index + 1);
  const chunks = await pmap(pageNums, CONCURRENCY, async (page) => {
    try {
      const data = await kidderPost(page * KIDDER_PAGE_SIZE, strict);
      assertKidderInventoryPage(data, page, firstPage, strict);
      return data.results ?? [];
    } catch (err) {
      if (strict) throw err;
      failed++;
      console.error(`  kidder: page ${page} failed: ${err}`);
      return [];
    }
  });
  for (const chunk of chunks) items.push(...chunk);
  assertKidderInventoryReconciled(items, total, strict);
  const result: KidderInventoryResult = {
    items,
    total,
    truncated: failed > 0 || wantPages < allPages,
    inventoryObservedAt: new Date().toISOString(),
    generationId,
    strictValidated: strict,
  };
  if (wantPages >= allPages && failed === 0) kidderCache = result;
  return result;
}

export function kidderTenure(row: any): { isSale: boolean; isLease: boolean } {
  const isLease = row.asking_rent_max != null || row.sublease_flg === true;
  const isSale = row.list_price != null || row.retail_investment_nnn_flg === true || !isLease;
  return { isSale, isLease };
}

function collectKidderPhotoUrls(value: unknown, urls: string[]): void {
  if (typeof value === "string") {
    if (/^https?:\/\//i.test(value) && !urls.includes(value)) urls.push(value);
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) collectKidderPhotoUrls(item, urls);
    return;
  }
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    for (const key of ["url", "src", "photo_url", "image_url"]) {
      collectKidderPhotoUrls(record[key], urls);
    }
  }
}

export function mapKidderListing(
  row: any,
  tx: Tx,
  context: KidderMappingContext = {}
): any {
  const strict = context.strict ?? requireFreshDetails();
  const providerIdentity = kidderProviderIdentity(row);
  if (strict && !providerIdentity) {
    throw new Error("Kidder strict listing requires nonempty listing_key");
  }
  const { isSale } = kidderTenure(row);
  const sale = isSale ? numeric(row.list_price) : null;
  const rent = numeric(row.asking_rent_max);
  const brokerIds = (Array.isArray(row.brokers) ? row.brokers : [])
    .map((broker: any) =>
      brokerRef({
        name: clean(typeof broker === "string" ? broker : broker?.name),
        company: "Kidder Mathews",
      })
    )
    .filter((id: number | null): id is number => id !== null);
  const photos: string[] = [];
  for (const value of [
    row.listing_photo,
    row.property_photo,
    row.photos,
    row.listing_photos,
    row.property_photos,
    row.images,
  ]) {
    collectKidderPhotoUrls(value, photos);
  }
  const fallbackKey =
    typeof row.property_key === "string"
      ? clean(row.property_key)
      : typeof row.property_key === "number" && Number.isFinite(row.property_key)
        ? String(row.property_key)
        : null;
  const key = providerIdentity ?? fallbackKey;
  const url = key != null ? `https://www.kidder.com/listings/${key}` : "https://www.kidder.com/properties/";
  const size = numeric(row.sf_avail);

  return prune({
    id: key != null ? String(key) : null,
    inventoryObservedAt: context.inventoryObservedAt,
    freshnessProvenance: context.inventoryObservedAt
      ? {
          detailScope: "authoritative_inventory_feed",
          generationId: context.generationId ?? refreshGenerationId(),
          method: "kidder_public_inventory_feed",
          cacheDisposition:
            context.cacheDisposition ??
            row.__creInventoryCacheDisposition ??
            "live",
        }
      : undefined,
    preserveChildCollections: true,
    name: clean(row.property_name) || clean(row.building_name) || clean(row.property_address),
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType: clean(row.use_type),
    street: clean(row.property_address),
    city: clean(row.city),
    state: clean(row.state_code),
    postalCode: row.zip_postal_code ? String(row.zip_postal_code).slice(0, 12) : null,
    country: "US",
    latitude: numeric(row.latitude),
    longitude: numeric(row.longitude),
    salePriceUsd: sale,
    salePriceText: sale ? `$${sale.toLocaleString("en-US")}` : null,
    leaseRateText: !isSale && rent != null ? `$${rent}/SF` : null,
    buildingSizeSqft: size,
    sizeText: size ? `${size.toLocaleString("en-US")} SF` : null,
    brokerIds,
    photos,
    url,
    canonicalUrl: url,
    rawKidder: row,
  });
}

export async function srcKidderMathews(tx: Tx, max: number, _monitor: boolean): Promise<SourceResult> {
  const result = await kidderFetchAll(max);
  const { items, total, inventoryObservedAt, generationId } = result;
  const listings: any[] = [];
  let eligible = 0;
  for (const row of items) {
    const { isSale, isLease } = kidderTenure(row);
    if (tx === "sale" && !isSale) continue;
    if (tx === "lease" && !isLease) continue;
    eligible++;
    if (listings.length >= max) continue;
    listings.push(
      mapKidderListing(row, tx, {
        strict: requireFreshDetails(),
        inventoryObservedAt,
        generationId,
        cacheDisposition: row.__creInventoryCacheDisposition ?? "live",
      })
    );
  }
  return {
    company: "Kidder Mathews",
    sourceUrl: "https://www.kidder.com/properties/",
    method: "Open Kidder backend search API, paginated direct POST",
    totalAvailable: total,
    listings,
    truncated: result.truncated || listings.length < eligible,
  };
}
