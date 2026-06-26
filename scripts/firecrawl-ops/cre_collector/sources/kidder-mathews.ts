import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { clean, pmap, prune } from "../lib/util.js";
import { SourceResult, Tx } from "../types.js";

const KIDDER_API = "https://services.kidder.com/search/public/listing";
const KIDDER_PAGE_SIZE = 50;

let kidderCache: any[] | null = null;

function numeric(value: any): number | null {
  return value != null && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
}

async function kidderPost(startIndex: number): Promise<any> {
  const body = JSON.stringify({ startIndex, numResults: KIDDER_PAGE_SIZE, includeAggregations: false });
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(KIDDER_API, {
        method: "POST",
        headers: {
          "Content-Type": "application/json;charset=UTF-8",
          "User-Agent": "Mozilla/5.0",
          Origin: "https://www.kidder.com",
        },
        body,
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

async function kidderFetchAll(max: number): Promise<{ items: any[]; total: number; truncated: boolean }> {
  if (kidderCache) return { items: kidderCache, total: kidderCache.length, truncated: false };
  const first = await kidderPost(0);
  const total: number = first.totalResultCount ?? 0;
  const items: any[] = [...(first.results ?? [])];
  const allPages = Math.ceil(total / KIDDER_PAGE_SIZE);
  const wantPages = Number.isFinite(max) ? Math.min(allPages, Math.ceil((max * 4) / KIDDER_PAGE_SIZE) + 1) : allPages;
  let failed = 0;
  const pageNums = Array.from({ length: Math.max(0, wantPages - 1) }, (_, index) => index + 1);
  const chunks = await pmap(pageNums, CONCURRENCY, async (page) => {
    try {
      return (await kidderPost(page * KIDDER_PAGE_SIZE)).results ?? [];
    } catch (err) {
      failed++;
      console.error(`  kidder: page ${page} failed: ${err}`);
      return [];
    }
  });
  for (const chunk of chunks) items.push(...chunk);
  if (wantPages >= allPages) kidderCache = items;
  return { items, total, truncated: failed > 0 || wantPages < allPages };
}

export function kidderTenure(row: any): { isSale: boolean; isLease: boolean } {
  const isLease = row.asking_rent_max != null || row.sublease_flg === true;
  const isSale = row.list_price != null || row.retail_investment_nnn_flg === true || !isLease;
  return { isSale, isLease };
}

export function mapKidderListing(row: any, tx: Tx): any {
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
  const photos = [row.listing_photo, row.property_photo]
    .filter((url: any) => typeof url === "string" && url.startsWith("http"))
    .slice(0, 2);
  const key = row.listing_key ?? row.property_key;
  const url = key != null ? `https://www.kidder.com/listings/${key}` : "https://www.kidder.com/properties/";
  const size = numeric(row.sf_avail);

  return prune({
    id: key != null ? String(key) : null,
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
  const { items, total, truncated } = await kidderFetchAll(max);
  const listings: any[] = [];
  for (const row of items) {
    if (listings.length >= max) break;
    const { isSale, isLease } = kidderTenure(row);
    if (tx === "sale" && !isSale) continue;
    if (tx === "lease" && !isLease) continue;
    listings.push(mapKidderListing(row, tx));
  }
  return {
    company: "Kidder Mathews",
    sourceUrl: "https://www.kidder.com/properties/",
    method: "Open Kidder backend search API, paginated direct POST",
    totalAvailable: total,
    listings,
    truncated,
  };
}
