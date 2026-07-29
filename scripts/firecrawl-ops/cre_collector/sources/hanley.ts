import {
  generationMatches,
  refreshGenerationId,
  requireFreshDetails,
} from "../lib/freshness.js";
import { scrapeRaw } from "../lib/scrape.js";
import { clean, prune } from "../lib/util.js";
import { CacheDisposition, SourceResult, Tx } from "../types.js";

const HANLEY_URL = "https://hanleyinvestmentgroup.com/listings/";

type HanleyCache = {
  rows: any[];
  inventoryObservedAt: string;
  generationId: string | null;
  strictValidated: boolean;
};

export type HanleyMappingContext = {
  inventoryObservedAt?: string;
  generationId?: string | null;
  cacheDisposition?: CacheDisposition;
  strict?: boolean;
};

let hanleyCache: HanleyCache | null = null;

function numeric(value: any): number | null {
  return value != null && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
}

function hanleyChallenge(html: string): boolean {
  return /verify you are human|attention required|cf-chl-|cloudflare ray id|captcha|access denied/i.test(
    html
  );
}

function embeddedArrayAt(html: string, marker: number): any[] | null {
  const start = html.indexOf("[", marker);
  if (start < 0) return null;
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
  if (end < 0) return null;
  try {
    const parsed = JSON.parse(html.slice(start, end));
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function parseHanleyInventory(
  html: string,
  strict = requireFreshDetails()
): { rows: any[]; publicRows: any[] } {
  if (strict && hanleyChallenge(html)) {
    throw new Error("Hanley strict inventory response is a challenge page");
  }
  const assignments = [...html.matchAll(/\brethink_properties\s*=/g)];
  if (strict && assignments.length !== 1) {
    throw new Error(
      `Hanley strict inventory requires exactly one rethink_properties assignment; found ${assignments.length}`
    );
  }
  const marker = assignments[0]?.index ?? html.indexOf("rethink_properties");
  if (marker < 0) {
    if (strict) throw new Error("Hanley strict inventory is missing rethink_properties");
    return { rows: [], publicRows: [] };
  }
  const rows = embeddedArrayAt(html, marker);
  if (!rows) {
    if (strict) {
      throw new Error("Hanley strict inventory requires one complete JSON array");
    }
    return { rows: [], publicRows: [] };
  }
  if (strict && rows.length === 0) {
    throw new Error("Hanley strict inventory embedded dataset is empty");
  }
  const identities = new Set<string>();
  const publicRows: any[] = [];
  for (const [index, row] of rows.entries()) {
    if (strict && (!row || typeof row !== "object" || Array.isArray(row))) {
      throw new Error(`Hanley strict inventory row ${index} is not an object`);
    }
    const identity =
      typeof row?.id === "string"
        ? clean(row.id)
        : typeof row?.id === "number" && Number.isFinite(row.id)
          ? String(row.id)
          : null;
    if (strict && !identity) {
      throw new Error(`Hanley strict inventory row ${index} requires a nonempty provider id`);
    }
    if (identity) {
      if (strict && identities.has(identity)) {
        throw new Error(`Hanley strict inventory duplicate provider identity ${identity}`);
      }
      identities.add(identity);
    }
    const visibility = clean(row?.visibility);
    if (strict && !visibility) {
      throw new Error(`Hanley strict inventory row ${identity ?? index} requires visibility`);
    }
    if (String(visibility ?? "").toLowerCase().startsWith("public")) {
      publicRows.push(row);
    }
  }
  return { rows, publicRows };
}

export function extractRethinkProperties(html: string): any[] {
  return parseHanleyInventory(html, false).rows;
}

export function hanleyFallbackOptions(strict = requireFreshDetails()): {
  proxy: "stealth";
  waitFor: number;
  maxAge?: number;
} {
  return {
    proxy: "stealth",
    waitFor: 3000,
    ...(strict ? { maxAge: 0 } : {}),
  };
}

async function fetchHanleyHtml(strict = requireFreshDetails()): Promise<string> {
  try {
    const res = await fetch(HANLEY_URL, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125 Safari/537.36",
        Accept: "text/html",
        ...(strict ? { "Cache-Control": "no-cache" } : {}),
      },
      ...(strict ? { cache: "no-store" as const } : {}),
    });
    if (res.ok) {
      const html = await res.text();
      if (html.includes("rethink_properties")) {
        parseHanleyInventory(html, strict);
        return html;
      }
    }
  } catch {
    /* fall back to Firecrawl raw HTML */
  }
  const html = await scrapeRaw(HANLEY_URL, hanleyFallbackOptions(strict));
  if (strict) parseHanleyInventory(html, true);
  return html;
}

export function hanleyIsLease(row: any): boolean {
  const tags = [String(row.dealRecordType ?? ""), ...(Array.isArray(row.dealPipelineTypes) ? row.dealPipelineTypes : [])]
    .join(",")
    .toLowerCase();
  return /landlord|tenant|lease/.test(tags);
}

export function mapHanleyListing(
  row: any,
  tx: Tx,
  context: HanleyMappingContext = {}
): any {
  const strict = context.strict ?? requireFreshDetails();
  const providerIdentity =
    typeof row?.id === "string"
      ? clean(row.id)
      : typeof row?.id === "number" && Number.isFinite(row.id)
        ? String(row.id)
        : null;
  if (strict && !providerIdentity) {
    throw new Error("Hanley strict listing requires a nonempty provider id");
  }
  const isLease = hanleyIsLease(row);
  const sqft = numeric(row.propertySquareFootage) ?? numeric(row.spaceSquareFootage);
  const url = providerIdentity ? `${HANLEY_URL}?id=${providerIdentity}` : HANLEY_URL;
  return prune({
    id: providerIdentity,
    inventoryObservedAt: context.inventoryObservedAt,
    freshnessProvenance: context.inventoryObservedAt
      ? {
          detailScope: "authoritative_inventory_feed",
          generationId: context.generationId ?? refreshGenerationId(),
          method: "hanley_embedded_inventory_feed",
          cacheDisposition: context.cacheDisposition ?? "live",
        }
      : undefined,
    preserveChildCollections: true,
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
  const strict = requireFreshDetails();
  const generationId = refreshGenerationId();
  if (strict && !generationId) {
    throw new Error("Hanley strict inventory requires CRE_REFRESH_GENERATION");
  }
  let cacheDisposition: CacheDisposition = "live";
  if (
    hanleyCache &&
    strict &&
    (!hanleyCache.strictValidated || !generationMatches(hanleyCache.generationId))
  ) {
    hanleyCache = null;
  }
  if (!hanleyCache) {
    const parsed = parseHanleyInventory(await fetchHanleyHtml(strict), strict);
    if (!parsed.rows.length) throw new Error("Hanley: rethink_properties array not found or empty");
    hanleyCache = {
      rows: parsed.publicRows,
      inventoryObservedAt: new Date().toISOString(),
      generationId,
      strictValidated: strict,
    };
  } else {
    cacheDisposition = "generation_cache";
  }
  const listings: any[] = [];
  let eligible = 0;
  for (const row of hanleyCache.rows) {
    if (tx === "lease" ? !hanleyIsLease(row) : hanleyIsLease(row)) continue;
    eligible++;
    if (listings.length >= max) continue;
    listings.push(
      mapHanleyListing(row, tx, {
        strict,
        inventoryObservedAt: hanleyCache.inventoryObservedAt,
        generationId: hanleyCache.generationId,
        cacheDisposition,
      })
    );
  }
  return {
    company: "Hanley Investment Group",
    sourceUrl: HANLEY_URL,
    method: "Direct fetch of /listings/ with embedded rethink_properties JSON",
    totalAvailable: hanleyCache.rows.length,
    listings,
    truncated: listings.length < eligible,
  };
}
