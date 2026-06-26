import { scrapeRaw } from "../lib/scrape.js";
import { clean, prune } from "../lib/util.js";
import { SourceResult, Tx } from "../types.js";

const HANLEY_URL = "https://hanleyinvestmentgroup.com/listings/";

let hanleyCache: any[] | null = null;

function numeric(value: any): number | null {
  return value != null && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
}

export function extractRethinkProperties(html: string): any[] {
  const marker = html.indexOf("rethink_properties");
  if (marker < 0) return [];
  const start = html.indexOf("[", marker);
  if (start < 0) return [];
  let depth = 0;
  let end = -1;
  let inString = false;
  let escaped = false;
  for (let index = start; index < html.length; index++) {
    const char = html[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
    } else if (char === '"') inString = true;
    else if (char === "[") depth++;
    else if (char === "]") {
      depth--;
      if (depth === 0) {
        end = index + 1;
        break;
      }
    }
  }
  if (end < 0) return [];
  try {
    return JSON.parse(html.slice(start, end));
  } catch {
    return [];
  }
}

async function fetchHanleyHtml(): Promise<string> {
  try {
    const res = await fetch(HANLEY_URL, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        Accept: "text/html",
      },
    });
    if (res.ok) {
      const html = await res.text();
      if (html.includes("rethink_properties")) return html;
    }
  } catch {
    /* fall back to Firecrawl raw HTML */
  }
  return scrapeRaw(HANLEY_URL, { proxy: "stealth", waitFor: 3000 });
}

export function hanleyIsLease(row: any): boolean {
  const tags = [String(row.dealRecordType ?? ""), ...(Array.isArray(row.dealPipelineTypes) ? row.dealPipelineTypes : [])]
    .join(",")
    .toLowerCase();
  return /landlord|tenant|lease/.test(tags);
}

export function mapHanleyListing(row: any, tx: Tx): any {
  const isLease = hanleyIsLease(row);
  const sqft = numeric(row.propertySquareFootage) ?? numeric(row.spaceSquareFootage);
  const url = row.id ? `${HANLEY_URL}?id=${row.id}` : HANLEY_URL;
  return prune({
    id: clean(row.id),
    name: clean(row.name) || clean(row.address),
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType: clean(row.propertyType) ?? clean(row.propertyRecordType),
    street: clean(row.address),
    city: clean(row.city),
    state: clean(row.state),
    postalCode: row.zipCode ? String(row.zipCode).slice(0, 12) : null,
    country: "US",
    latitude: numeric(row.latitude),
    longitude: numeric(row.longitude),
    salePriceUsd: !isLease ? numeric(row.salesPrice) : null,
    salePriceText: !isLease && numeric(row.salesPrice) ? `$${Number(row.salesPrice).toLocaleString("en-US")}` : null,
    leaseRateText: isLease ? clean(String(row.leaseRate ?? "")) || null : null,
    capRatePct: numeric(row.capRate),
    buildingSizeSqft: sqft,
    sizeText: sqft ? `${sqft.toLocaleString("en-US")} SF` : null,
    brokerIds: [],
    photos: typeof row.image === "string" && row.image.startsWith("http") ? [row.image] : [],
    url,
    canonicalUrl: url,
    statusBadge: clean(row.status),
    units: numeric(row.numberOfUnits),
    rawHanley: row,
  });
}

export async function srcHanley(tx: Tx, max: number, _monitor: boolean): Promise<SourceResult> {
  if (!hanleyCache) {
    const rows = extractRethinkProperties(await fetchHanleyHtml());
    if (!rows.length) throw new Error("Hanley: rethink_properties array not found or empty");
    hanleyCache = rows.filter((row) => String(row.visibility ?? "").toLowerCase().startsWith("public"));
  }
  const listings: any[] = [];
  for (const row of hanleyCache) {
    if (listings.length >= max) break;
    if (tx === "lease" ? !hanleyIsLease(row) : hanleyIsLease(row)) continue;
    listings.push(mapHanleyListing(row, tx));
  }
  return {
    company: "Hanley Investment Group",
    sourceUrl: HANLEY_URL,
    method: "Direct fetch of /listings/ with embedded rethink_properties JSON",
    totalAvailable: hanleyCache.length,
    listings,
  };
}
