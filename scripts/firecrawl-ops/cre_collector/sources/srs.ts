import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { clean, pmap, prune } from "../lib/util.js";
import { SourceResult, Tx } from "../types.js";

const SRS_API = "https://srsre-next-412955565034.us-central1.run.app/api/property-search";
const SRS_PAGE_SIZE = 12;
const SRS_FILTERS = {
  availabilityType: ["sale", "lease", "investment-sale"],
  propertyType: ["retail", "industrial", "office", "land", "multifamily", "hospitality", "healthcare", "special_purpose"],
  address: null,
  tenant: "",
  ownershipType: ["fee-simple-land-building", "ground-lease-land-only", "leasehold-lease-only", "other"],
  portfolio: [] as string[],
  tenancyType: ["single-tenant", "multi-tenant", "land"],
  subType: null,
  orderDirection: "DESC",
  orderBy: "date",
  office: "",
  broker: "",
  sizeRange: { required: false },
  lotSizeRange: { required: false },
  priceRange: { required: true },
  capRateRange: { required: true },
  latLong: null,
  searchTerms: "",
};

let srsCache: any[] | null = null;

function numeric(value: any): number | null {
  return value != null && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
}

async function srsPost(page: number): Promise<any> {
  const body = JSON.stringify({
    query: { offset: SRS_PAGE_SIZE * page, pageSize: SRS_PAGE_SIZE, ...SRS_FILTERS },
    client_ip: "",
  });
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(SRS_API, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "User-Agent": "Mozilla/5.0",
          Origin: "https://www.srsre.com",
        },
        body,
      });
      if (!res.ok) throw new Error(`SRS API HTTP ${res.status}`);
      const data: any = await res.json();
      if (data && Array.isArray(data.properties)) return data;
      throw new Error("SRS API response missing properties[]");
    } catch (err) {
      if (attempt === 3) throw err;
      await new Promise((resolve) => setTimeout(resolve, 2000 * attempt));
    }
  }
}

export async function srsFetchAll(max: number): Promise<{ items: any[]; total: number; truncated: boolean }> {
  if (srsCache) return { items: srsCache, total: srsCache.length, truncated: false };
  const first = await srsPost(0);
  const total: number = first.total ?? 0;
  const items: any[] = [...(first.properties ?? [])];
  const allPages = Math.ceil(total / SRS_PAGE_SIZE);
  const wantPages = Number.isFinite(max) ? Math.min(allPages, Math.ceil((max * 4) / SRS_PAGE_SIZE) + 1) : allPages;
  let failed = 0;
  const pageNums = Array.from({ length: Math.max(0, wantPages - 1) }, (_, index) => index + 1);
  const chunks = await pmap(pageNums, CONCURRENCY, async (page) => {
    try {
      return (await srsPost(page)).properties ?? [];
    } catch (err) {
      failed++;
      console.error(`  srs: page ${page} failed: ${err}`);
      return [];
    }
  });
  for (const chunk of chunks) items.push(...chunk);
  if (wantPages >= allPages) srsCache = items;
  return { items, total, truncated: failed > 0 || wantPages < allPages };
}

export function srsTenure(row: any): { isSale: boolean; isLease: boolean } {
  const availability = String(row?.apto_data?.Availability__c ?? "").toLowerCase();
  const permalink = String(row?.permalink ?? "").toLowerCase();
  const isLease = availability.includes("lease") || /\/lease\//.test(permalink);
  const isSale = availability.includes("sale") || /\/sale\//.test(permalink) || !isLease;
  return { isSale, isLease };
}

export function mapSrsListing(row: any, tx: Tx): any {
  const data = row.apto_data ?? {};
  const { isSale } = srsTenure(row);
  const salePrice = !data.Hide_Sale_Price__c ? numeric(data.Sale_Price__c) : null;
  const photos: string[] = [];
  const listingImages = data.listing_images;
  if (Array.isArray(listingImages)) {
    for (const image of listingImages) {
      const url = typeof image === "string" ? image : image?.url ?? image?.src;
      if (typeof url === "string" && url.startsWith("http")) photos.push(url);
    }
  }
  if (!photos.length && typeof row.thumbnail === "string" && row.thumbnail.startsWith("http")) {
    photos.push(row.thumbnail);
  }

  const brokerIds = (Array.isArray(data.related_brokers) ? data.related_brokers : [])
    .map((broker: any) =>
      brokerRef({
        name: clean(broker?.name ?? broker?.Name ?? [broker?.FirstName, broker?.LastName].filter(Boolean).join(" ")),
        email: clean(broker?.email ?? broker?.Email),
        phone: clean(broker?.phone ?? broker?.Phone),
        company: "SRS Real Estate Partners",
      })
    )
    .filter((id: number | null): id is number => id !== null);
  const url = row.permalink ? `https://www.srsre.com${row.permalink}` : null;

  return prune({
    id: clean(data.SRS_Listings_ID__c) ?? clean(row.id) ?? clean(data.Id),
    name: clean(data.Name) || clean(data.Property_Address__c),
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType: clean(data.Primary_Property_Type__c),
    description: clean(data.Description__c),
    street: clean(data.Property_Address__c) ?? clean(data.Postal_Address__Street__s),
    city: clean(data.Property_City__c) ?? clean(data.Postal_Address__City__s),
    state: clean(data.Property_State__c) ?? clean(data.Postal_Address__StateCode__s),
    postalCode: data.Property_Zip__c ? String(data.Property_Zip__c).slice(0, 12) : clean(data.Postal_Address__PostalCode__s),
    country: "US",
    latitude: numeric(row.location?.lat) ?? numeric(data.Property_Latitude__c) ?? numeric(data.Latitude__c),
    longitude: numeric(row.location?.lon) ?? numeric(data.Property_Longitude__c) ?? numeric(data.Longitude__c),
    salePriceUsd: isSale ? salePrice : null,
    salePriceText: isSale && salePrice ? `$${salePrice.toLocaleString("en-US")}` : null,
    capRatePct: numeric(data.Cap_Rate__c),
    buildingSizeSqft: numeric(data.Total_Property_SF_GLA__c) ?? numeric(data.square_footage),
    lotSizeAcres: numeric(data.Total_Property_Land_Acres__c) ?? numeric(data.lot_size_acres),
    yearBuilt: numeric(data.Year_Built__c),
    sizeText: clean(row.square_feet_data),
    brokerIds,
    photos: photos.slice(0, 12),
    url,
    canonicalUrl: url,
    rawSrs: row,
  });
}

export async function srcSrs(tx: Tx, max: number, _monitor: boolean): Promise<SourceResult> {
  const { items, total, truncated } = await srsFetchAll(max);
  const listings: any[] = [];
  for (const row of items) {
    if (listings.length >= max) break;
    const { isSale, isLease } = srsTenure(row);
    if (tx === "sale" && !isSale) continue;
    if (tx === "lease" && !isLease) continue;
    listings.push(mapSrsListing(row, tx));
  }
  return {
    company: "SRS Real Estate Partners",
    sourceUrl: "https://www.srsre.com/properties",
    method: "Salesforce-backed Cloud Run search API, paginated direct POST",
    totalAvailable: total,
    listings,
    truncated,
  };
}
