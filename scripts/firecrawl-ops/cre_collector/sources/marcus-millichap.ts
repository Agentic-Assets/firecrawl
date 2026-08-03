// sources/marcus-millichap.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { createHash, randomUUID } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { CONCURRENCY, OUT_PATH, flags } from "../lib/config.js";
import { dedupeStrings } from "../lib/html.js";
import { harvestDetail } from "../lib/harvest.js";
import {
  detailObservation,
  refreshGenerationId,
  requireFreshDetails,
} from "../lib/freshness.js";
import {
  acresToSf,
  parseLeaseRate,
  parseMoney,
  parsePercentToFraction,
  parseSizeText,
} from "../lib/parse.js";
import { DocItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { clean, moneyToNumber, num, pmap, prune } from "../lib/util.js";


// --- Marcus & Millichap: public contentsearch API + public detail pages (sale-only platform) ---

export const MARCUS_BASE = "https://www.marcusmillichap.com";
export const MARCUS_PROPERTIES_URL = `${MARCUS_BASE}/properties`;

export function marcusHeaders(): Record<string, string> {
  return {
    accept: "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/json",
    origin: MARCUS_BASE,
    referer: MARCUS_PROPERTIES_URL,
    "user-agent": "Mozilla/5.0 CRE collector",
  };
}

export function marcusSearchBody(pageSize: number): Record<string, any> {
  return {
    pageNumber: 1,
    pageSize,
    sortOrder: "DESC",
    indexFieldName: "orderdate",
    facets: [],
    rangeFacets: [],
    geoFacet: { Polygons: [], Circles: [], FieldName: "customdraw" },
    savedSearchId: null,
    allowedFacets: ["propertytype", "location", "advisors", "listingprice", "caprate"],
  };
}

export function marcusMapDetailBody(activityId: string): Record<string, any> {
  return { activityId };
}

export async function marcusPost(path: string, body: Record<string, any>): Promise<any> {
  const res = await fetch(`${MARCUS_BASE}${path}`, {
    method: "POST",
    headers: marcusHeaders(),
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Marcus & Millichap ${path} HTTP ${res.status}`);
  return res.json();
}

export function marcusUrl(href: string | null | undefined): string | null {
  const h = clean(href ?? null);
  if (!h) return null;
  try {
    return new URL(h, MARCUS_BASE).toString();
  } catch {
    return null;
  }
}

export function extractCssUrl(style: string | null | undefined): string | null {
  const match = (style ?? "").match(/url\((['"]?)(.*?)\1\)/i);
  return match ? match[2] : null;
}

export function parseMarcusLocation(location: string | null): {
  city: string | null;
  state: string | null;
  postalCode: string | null;
} {
  const m = (location ?? "").match(/^(.*?),\s*([A-Z]{2})(?:\s+(\d{5}(?:-\d{4})?))?$/);
  return {
    city: m ? clean(m[1]) : location,
    state: m ? m[2] : null,
    postalCode: m?.[3] ?? null,
  };
}

export function parseMarcusAddress(address: string | null): {
  street: string | null;
  city: string | null;
  state: string | null;
  postalCode: string | null;
} {
  const m = (address ?? "").match(/^(.*?),\s*([^,]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$/);
  return {
    street: m ? clean(m[1]) : null,
    city: m ? clean(m[2]) : null,
    state: m ? m[3] : null,
    postalCode: m ? m[4] : null,
  };
}

export function parseMarcusTileHtml(tileHtml: string | null | undefined, row: any = {}): any {
  const $ = cheerio.load(tileHtml ?? "");
  const { __creInventoryObservedAt: _inventoryObservedAt, ...rawMarcusSearchRow } = row;
  const tile = $(".mm-tile").first();
  const href = tile.find('a[href^="/properties/"], a[href*="marcusmillichap.com/properties/"]').first().attr("href");
  const location = parseMarcusLocation(clean(tile.find(".mm-location").first().text()));
  const priceText =
    clean(
      tile
        .find(".mm-listing-price, .starting-bid")
        .first()
        .text()
    )?.replace(/^Listing Price:\s*/i, "") ??
    clean(row.ListingPrice) ??
    null;
  const capRateText = clean(tile.find(".mm-cap-rate").first().text()) ?? clean(String(row.CapRate ?? ""));
  const capRate = (capRateText ?? "").match(/([0-9.]+)%?/)?.[1];
  const img = marcusUrl(tile.find('img[src*="mmimageservice"]').first().attr("src"));
  const rowUrl = marcusUrl(row.PropertyUrl);
  return prune({
    id: clean(String(row.DealId ?? tile.attr("data-dealid") ?? "")),
    activityId: clean(row.ActivityId ?? tile.attr("data-activityid")),
    propertyId: clean(String(row.PropertyId ?? "")),
    name: clean(row.PropertyName) ?? clean(tile.find("h2").first().text()),
    transactionType: "Sale",
    assetType: clean(row.PropertyType) ?? clean(tile.find("h3").first().text()),
    city: clean(row.City) ?? location.city,
    state: clean(row.StateProvince) ?? location.state,
    postalCode: clean(row.PostalCode) ?? location.postalCode,
    country: "US",
    latitude: num(Number(row.Latitude)),
    longitude: num(Number(row.Longitude)),
    salePriceUsd: moneyToNumber(priceText),
    salePriceText: priceText,
    capRatePct: capRate ? Number(capRate) : null,
    brokerIds: [],
    photos: img ? [img] : [],
    url: rowUrl ?? marcusUrl(href),
    marcusFlags: {
      newlyListed: Boolean(row.NewlyListed),
      newlyReduced: Boolean(row.NewlyReduced),
    },
    inventoryObservedAt: clean(row.__creInventoryObservedAt),
    freshnessProvenance: {
      detailScope: "inventory_feed",
      generationId: refreshGenerationId(),
      method: "marcus_mapproperties",
      cacheDisposition: "live",
    },
    rawMarcusSearchRow,
  });
}

export function parseMarcusPropertiesResponse(
  search: any,
  strict = requireFreshDetails()
): { rows: any[]; total: number | null } {
  const results = search?.Results ?? search;
  if (
    strict
    && (
      !results
      || typeof results !== "object"
      || Array.isArray(results)
      || !Array.isArray(results.Properties)
    )
  ) {
    throw new Error(
      "Marcus & Millichap properties response has no Properties array"
    );
  }
  const rows = Array.isArray(results?.Properties) ? results.Properties : [];
  const total =
    typeof results?.TotalCount === "number" ? results.TotalCount : null;
  if (
    strict
    && (
      !Number.isFinite(total)
      || !Number.isInteger(total)
      || (total as number) < 0
    )
  ) {
    throw new Error(
      "Marcus & Millichap properties response requires a finite nonnegative integer TotalCount"
    );
  }
  if (strict && (total as number) < rows.length) {
    throw new Error(
      `Marcus & Millichap properties response TotalCount ${total} is below returned rows ${rows.length}`
    );
  }
  return { rows, total };
}

export function parseMarcusMapRowsResponse(
  map: any,
  strict = requireFreshDetails()
): any[] {
  const results = map?.Results ?? map;
  if (
    strict
    && (
      !results
      || typeof results !== "object"
      || Array.isArray(results)
      || !Array.isArray(results.Properties)
    )
  ) {
    throw new Error(
      "Marcus & Millichap mapproperties response has no Properties array"
    );
  }
  const rows = Array.isArray(results?.Properties)
    ? results.Properties
    : Array.isArray(results)
      ? results
      : [];
  const seen = new Set<string>();
  return rows.filter((row: any) => {
    const activityId = clean(row.ActivityId);
    if (!activityId || seen.has(activityId)) return false;
    seen.add(activityId);
    return true;
  });
}

export async function fetchMarcusMapRows(): Promise<any[]> {
  const map = await marcusPost("/api/contentsearch/mapproperties", marcusSearchBody(1));
  return parseMarcusMapRowsResponse(map);
}

export async function fetchMarcusMapListing(mapRow: any): Promise<any | null> {
  const activityId = clean(mapRow.ActivityId);
  if (!activityId) return null;
  try {
    const detail = await marcusPost("/api/contentsearch/mappropertydetail", marcusMapDetailBody(activityId));
    const results = detail.Results ?? detail;
    return parseMarcusTileHtml(results.PropertyDetail, {
      ...mapRow,
      ActivityId: activityId,
      PropertyUrl: results.PropertyUrl,
      rawMarcusMapDetail: results,
    });
  } catch (err) {
    console.error(`  marcus-millichap/sale: map detail failed for ${activityId}: ${err}`);
    const { __creInventoryObservedAt: _inventoryObservedAt, ...rawMarcusSearchRow } = mapRow;
    return prune({
      activityId,
      latitude: num(Number(mapRow.Latitude)),
      longitude: num(Number(mapRow.Longitude)),
      country: "US",
      transactionType: "Sale",
      marcusFlags: {
        newlyListed: Boolean(mapRow.NewlyListed),
        newlyReduced: Boolean(mapRow.NewlyReduced),
      },
      inventoryObservedAt: clean(mapRow.__creInventoryObservedAt),
      freshnessProvenance: {
        detailScope: "inventory_feed",
        generationId: refreshGenerationId(),
        method: "marcus_mapproperties",
        cacheDisposition: "live",
      },
      detailError: String(err),
      rawMarcusSearchRow,
    });
  }
}

export function assertMarcusInventoryCount(
  total: number | null,
  mapRows: any[],
  strict = requireFreshDetails()
): void {
  if (
    strict
    && (
      !Number.isFinite(total)
      || !Number.isInteger(total)
      || (total as number) < 0
    )
  ) {
    throw new Error(
      "Marcus & Millichap properties response requires a finite nonnegative integer TotalCount"
    );
  }
  if (strict && mapRows.length !== total) {
    throw new Error(
      `Marcus & Millichap inventory mismatch: properties API reported ${total}, map API returned ${mapRows.length} unique ActivityIds`
    );
  }
}

export function assertMarcusMapDetails(selectedMapRows: any[], baseRows: any[]): void {
  const failedBaseRows = baseRows.filter((listing: any) => !listing?.url || listing?.detailError);
  if (!failedBaseRows.length) return;
  const sample = failedBaseRows
    .slice(0, 3)
    .map((listing: any) => listing?.activityId ?? "unknown")
    .join(", ");
  throw new Error(
    `Marcus & Millichap map detail failed for ${failedBaseRows.length}/${selectedMapRows.length} ActivityIds (${sample})`
  );
}

export function marcusCapTruncated(
  max: number,
  selected: number,
  knownInventory: number
): boolean {
  return Number.isFinite(max) && selected < knownInventory;
}

export async function fetchMarcusDetailHtml(url: string): Promise<string> {
  const res = await fetch(url, {
    headers: {
      accept: "text/html,*/*",
      referer: MARCUS_PROPERTIES_URL,
      "user-agent": "Mozilla/5.0 CRE collector",
    },
  });
  if (!res.ok) throw new Error(`Marcus & Millichap detail HTTP ${res.status}`);
  return res.text();
}

export function marcusDetailHtmlIsUsable(html: string): boolean {
  if (
    !html.trim() ||
    /captcha|access denied|verify you are human|cf-chl-/i.test(html)
  ) {
    return false;
  }
  const $ = cheerio.load(html);
  const title = clean($("h1").first().text());
  const hasPropertyBody =
    $(".score-hero-body, .specification-outer, .mm-property-investment-overview, .mm-property").length > 0;
  return !!title && hasPropertyBody;
}

export function extractMarcusDetailImages($: cheerio.CheerioAPI, seed: string[]): string[] {
  const urls: Array<string | null> = [...seed];
  $('img[src*="mmimageservice.azurewebsites.net/api/image/property"]').each((_, el) => {
    urls.push(marcusUrl($(el).attr("src")));
  });
  $('[style*="mmimageservice.azurewebsites.net/api/image/property"]').each((_, el) => {
    urls.push(marcusUrl(extractCssUrl($(el).attr("style"))));
  });
  return dedupeStrings(urls);
}

export function extractMarcusContacts($: cheerio.CheerioAPI): any[] {
  const contactsByKey = new Map<string, any>();
  $('li .mm-tile, .mm-advisor .mm-tile, .mm-advisor-card .mm-tile')
    .has('a[href^="/advisors/"]')
    .each((_, el) => {
      const tile = $(el);
      const profileUrl = marcusUrl(tile.find('a[href^="/advisors/"]').first().attr("href"));
      const email = clean(tile.find('a[href^="mailto:"]').first().attr("href")?.replace(/^mailto:/i, "").split("?")[0]);
      const phone = clean(
        tile.find('a[href^="tel:"]').first().text() ??
          tile.find('a[href^="tel:"]').first().attr("href")?.replace(/^tel:/i, "")
      );
      const avatarUrl = marcusUrl(extractCssUrl(tile.find(".mm-image-wrapper").first().attr("style")));
      const name = clean(tile.find("h3").first().text());
      const key = email ?? profileUrl ?? name;
      if (!key) return;
      contactsByKey.set(key, {
        name,
        title: clean(tile.find(".ipa-subtitle").first().text()),
        email,
        phone,
        company: "Marcus & Millichap",
        profileUrl,
        avatarUrl,
        license: clean(tile.find(".ipa-license").first().text()),
        office: clean(tile.find(".ipa-location").first().text()),
      });
    });
  return [...contactsByKey.values()].filter((c) => c.name || c.email || c.phone || c.profileUrl);
}

export function parseMarcusSpecifications($: cheerio.CheerioAPI): Record<string, string> {
  const specs: Record<string, string> = {};
  $(".specification-outer").each((_, el) => {
    const key = clean($(el).find(".specification-name").first().text());
    const value = clean($(el).find(".specification-value").first().text());
    if (key && value && specs[key] === undefined) specs[key] = value;
  });
  return specs;
}

/**
 * Parse the broker license string from a contactsDetailed entry.
 * "License(s): IL: 475.188007" -> "IL: 475.188007"
 * Returns null when the field is absent or does not contain "License(s):".
 */
export function parseMarcusContactLicense(raw: string | null | undefined): string | null {
  const s = clean(raw ?? null);
  if (!s) return null;
  // "License(s): ..." pattern where (s) contains literal parentheses.
  // Require a colon to avoid matching arbitrary text containing the word "license".
  const m = s.match(/licen[sc]e(?:\(s\))?:\s*(.+)/i);
  return m ? clean(m[1]) : null;
}

/**
 * Lift institutional scalars from a parsed marcusSpecifications map into the
 * contract camelCase fields (Section B of the Phase-2 Data-Lift Contract).
 *
 * Pure: no network, no side effects. Every field is nullable; absent keys
 * yield null (prune() drops them at emit time).
 *
 * @param specs  The parsed marcusSpecifications object (key -> raw string value).
 * @param topLevelCapRatePct  Optional top-level capRatePct already on the listing
 *                            (used as fallback when 'Cap Rate' is absent from specs).
 * @returns An object of contract camelCase fields ready to spread onto the listing.
 */
export function parseMarcusScalars(
  specs: Record<string, string> | null | undefined,
  topLevelCapRatePct?: number | null
): Record<string, any> {
  const NULL_SCALARS = {
    capRatePct: null, occupancyRate: null, sizeSf: null, salePricePerSf: null,
    lotSizeSf: null, units: null, leaseRateType: null, leaseRateMin: null,
    leaseRateMax: null, tenantName: null, guarantor: null, leaseYearsRemaining: null,
    grm: null, pricePerUnit: null, pricePerAcre: null, numRooms: null, revpar: null,
    extraFacts: null,
  };
  if (!specs || typeof specs !== "object") return NULL_SCALARS;

  // ---- capRatePct ----
  // Prefer specs['Cap Rate'] (e.g. "8.60%"); fall back to top-level capRatePct.
  let capRatePct: number | null = null;
  const capRateStr = specs["Cap Rate"] ?? null;
  if (capRateStr) {
    const m = capRateStr.match(/([0-9.]+)/);
    capRatePct = m ? Number(m[1]) : null;
  } else if (typeof topLevelCapRatePct === "number" && isFinite(topLevelCapRatePct)) {
    capRatePct = topLevelCapRatePct;
  }

  // ---- occupancyRate ----
  const occupancyRate = parsePercentToFraction(specs["Occupancy"] ?? null);

  // ---- sizeSf via parseSizeText on 'Rentable SF' | 'Gross SF' ----
  const rentableSfText = specs["Rentable SF"] ?? specs["Gross SF"] ?? null;
  const { sizeSf } = parseSizeText(rentableSfText);

  // ---- salePricePerSf ----
  const salePricePerSf = parseMoney(specs["Price/Gross SF"] ?? null);

  // ---- lotSizeSf from 'Lot Size' (acres -> SF) ----
  const lotSfFromAcres = acresToSf(specs["Lot Size"] ?? null);
  // lotSizeSf is null when the spec is absent; acresToSf returns null for non-acre text.
  const lotSizeSf = lotSfFromAcres;

  // ---- units ----
  const unitsText = specs["Number of Units"] ?? null;
  let units: number | null = null;
  if (unitsText) {
    const m = unitsText.match(/([0-9][0-9,]*)/);
    if (m) {
      const v = Number(m[1].replace(/,/g, ""));
      units = isFinite(v) && v > 0 ? v : null;
    }
  }

  // ---- leaseRateType / leaseRateMin / leaseRateMax ----
  // 'Lease Type' carries the basis string (e.g. "Triple Net (NNN)").
  // 'Rent Per Square Feet' carries the in-place tenant rent rate (e.g. "$23.40").
  // These are in-place tenant rents on sale assets; the semantics are noted but the
  // fields are lifted as-is via the standard lease-rate channel.
  const leaseTypeText = specs["Lease Type"] ?? null;
  const rentPerSfText = specs["Rent Per Square Feet"] ?? null;

  // Derive basis type from 'Lease Type' string; use parseLeaseRate on the rent string.
  let leaseRateType: string | null = null;
  if (leaseTypeText) {
    const lt = leaseTypeText.toLowerCase();
    if (/modified\s+gross|mod\s+gross/.test(lt)) leaseRateType = "modified_gross";
    else if (/full\s+service|\bfsg\b/.test(lt)) leaseRateType = "full_service";
    else if (/nnn|triple\s+net/.test(lt)) leaseRateType = "nnn";
    else if (/\bgross\b/.test(lt)) leaseRateType = "gross";
  }
  let leaseRateMin: number | null = null;
  let leaseRateMax: number | null = null;
  if (rentPerSfText) {
    const parsed = parseLeaseRate(rentPerSfText);
    leaseRateMin = parsed.min;
    leaseRateMax = parsed.max;
    // If parseLeaseRate produced no type (bare "$N.NN" form), fall back to leaseTypeText.
    if (!parsed.type && leaseRateMin !== null && !leaseRateType) {
      leaseRateType = null; // already null; leave as-is
    } else if (parsed.type) {
      // parseLeaseRate derived a more specific type from the rate text; defer to leaseTypeText.
      leaseRateType = leaseRateType ?? parsed.type;
    }
    // When rate is a bare amount (no /SF signal), parseMoney as direct fallback.
    if (leaseRateMin === null && parsed.min === null) {
      leaseRateMin = parseMoney(rentPerSfText);
    }
  }

  // ---- NEW institutional fields ----

  // tenantName: 'Tenant Name'
  const tenantName = clean(specs["Tenant Name"] ?? null);

  // guarantor: 'Guarantor'
  const guarantor = clean(specs["Guarantor"] ?? null);

  // leaseYearsRemaining: 'Years Remaining On Lease' (numeric, e.g. "1.3")
  let leaseYearsRemaining: number | null = null;
  const yrText = specs["Years Remaining On Lease"] ?? null;
  if (yrText) {
    const m = yrText.match(/([0-9]+(?:\.[0-9]+)?)/);
    if (m) {
      const v = Number(m[1]);
      leaseYearsRemaining = isFinite(v) && v >= 0 && v <= 99 ? v : null;
    }
  }

  // grm: 'GRM' (gross rent multiplier, e.g. "6.06")
  let grm: number | null = null;
  const grmText = specs["GRM"] ?? null;
  if (grmText) {
    const m = grmText.match(/([0-9]+(?:\.[0-9]+)?)/);
    if (m) {
      const v = Number(m[1]);
      grm = isFinite(v) && v > 0 && v < 100 ? v : null;
    }
  }

  // pricePerUnit: 'Price/Unit' (e.g. "$56,964")
  const pricePerUnit = parseMoney(specs["Price/Unit"] ?? null);

  // pricePerAcre: 'Price/Acre' (e.g. "$125,000")
  const pricePerAcre = parseMoney(specs["Price/Acre"] ?? null);

  // numRooms: 'Number of Rooms' (hotel room count, integer)
  let numRooms: number | null = null;
  const numRoomsText = specs["Number of Rooms"] ?? null;
  if (numRoomsText) {
    const m = numRoomsText.match(/([0-9][0-9,]*)/);
    if (m) {
      const v = Number(m[1].replace(/,/g, ""));
      numRooms = isFinite(v) && v > 0 ? v : null;
    }
  }

  // revpar: 'RevPAR' (hotel revenue per available room, e.g. "$85.00")
  const revpar = parseMoney(specs["RevPAR"] ?? null);

  // ---- extraFacts: long-tail fields (no discrete column) ----
  const extraFacts: Record<string, any> = {};
  if (specs["Buildable Square Feet"]) {
    const v = parseMoney(specs["Buildable Square Feet"]) ??
      Number((specs["Buildable Square Feet"] ?? "").replace(/,/g, ""));
    if (isFinite(v) && v > 0) extraFacts["buildable_sf"] = v;
  }
  if (specs["Year Built"] && !specs["Year Built"].match(/^\s*0+\s*$/)) {
    extraFacts["year_built_raw"] = specs["Year Built"];
  }
  // Auction fields (if present)
  const auctionFields = ["Starting Bid", "Auction Date", "Auction Type", "Reserve"];
  for (const f of auctionFields) {
    if (specs[f]) extraFacts[`auction_${f.toLowerCase().replace(/\s+/g, "_")}`] = specs[f];
  }
  // Price/Room for hotels
  if (specs["Price/Room"]) extraFacts["price_per_room"] = parseMoney(specs["Price/Room"]);

  return {
    capRatePct: capRatePct,
    occupancyRate,
    sizeSf,
    salePricePerSf,
    lotSizeSf,
    units,
    leaseRateType: leaseRateType || null,
    leaseRateMin,
    leaseRateMax,
    // NEW institutional fields
    tenantName,
    guarantor,
    leaseYearsRemaining,
    grm,
    pricePerUnit,
    pricePerAcre,
    numRooms,
    revpar,
    // long-tail
    extraFacts: Object.keys(extraFacts).length > 0 ? extraFacts : null,
  };
}

export function marcusListingCacheKey(listing: any): string | null {
  return clean(String(listing?.id ?? listing?.activityId ?? listing?.url ?? ""));
}

export const MARCUS_DETAIL_CACHE_SCHEMA_VERSION = 2;

export function marcusListingCacheIdentity(listing: any): string | null {
  const key = marcusListingCacheKey(listing);
  if (!key) return null;
  const payload = {
    key,
    id: clean(String(listing?.id ?? "")),
    activityId: clean(String(listing?.activityId ?? "")),
    url: clean(listing?.url),
    salePriceUsd: listing?.salePriceUsd ?? null,
    capRatePct: listing?.capRatePct ?? null,
    rawMarcusSearchRow: listing?.rawMarcusSearchRow ?? null,
  };
  return createHash("sha256").update(JSON.stringify(payload)).digest("hex");
}

export function marcusDetailCachePath(): string {
  return OUT_PATH ? `${OUT_PATH}.marcus-detail-cache.jsonl` : "out/marcus-millichap-detail-cache.jsonl";
}

export function prepareMarcusDetailCache(path: string, attemptId: string): void {
  mkdirSync(dirname(path), { recursive: true });
  const header = {
    kind: "marcus-detail-cache",
    schemaVersion: MARCUS_DETAIL_CACHE_SCHEMA_VERSION,
    attemptId,
    createdAt: new Date().toISOString(),
  };
  // Every collector process is a distinct freshness attempt. Reusing a sidecar
  // from a prior retry would make a newly timestamped artifact stale, so reset
  // it unless it carries this exact process-local attempt identity.
  if (existsSync(path)) {
    try {
      const firstLine = readFileSync(path, "utf8").split(/\r?\n/, 1)[0];
      const prior = JSON.parse(firstLine);
      if (
        prior?.kind === header.kind &&
        prior?.schemaVersion === header.schemaVersion &&
        prior?.attemptId === attemptId
      ) {
        return;
      }
    } catch {
      // Invalid or legacy cache files are replaced below.
    }
  }
  writeFileSync(path, `${JSON.stringify(header)}\n`);
}

export function readMarcusDetailCache(
  path: string,
  attemptId: string,
  baseListings: any[]
): Map<string, any> {
  const cached = new Map<string, any>();
  if (!existsSync(path)) return cached;
  const expectedIdentities = new Map<string, string>();
  for (const listing of baseListings) {
    const key = marcusListingCacheKey(listing);
    const identity = marcusListingCacheIdentity(listing);
    if (key && identity) expectedIdentities.set(key, identity);
  }
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const record = JSON.parse(line);
      if (
        record?.kind !== "listing" ||
        record?.schemaVersion !== MARCUS_DETAIL_CACHE_SCHEMA_VERSION ||
        record?.attemptId !== attemptId ||
        typeof record?.fetchedAt !== "string" ||
        !Number.isFinite(Date.parse(record.fetchedAt))
      ) {
        continue;
      }
      const listing = record.listing;
      if (listing?.detailError) continue;
      const key = marcusListingCacheKey(listing);
      if (
        key &&
        key === record.key &&
        expectedIdentities.get(key) === record.baseIdentity
      ) {
        cached.set(key, listing);
      }
    } catch {
      // Ignore a partial final line if a prior run was interrupted mid-write.
    }
  }
  return cached;
}

export function appendMarcusDetailCache(
  path: string,
  attemptId: string,
  base: any,
  listing: any
): void {
  if (listing?.detailError) return;
  const key = marcusListingCacheKey(base);
  const baseIdentity = marcusListingCacheIdentity(base);
  if (!key || !baseIdentity || marcusListingCacheKey(listing) !== key) return;
  mkdirSync(dirname(path), { recursive: true });
  appendFileSync(
    path,
    `${JSON.stringify({
      kind: "listing",
      schemaVersion: MARCUS_DETAIL_CACHE_SCHEMA_VERSION,
      attemptId,
      key,
      baseIdentity,
      fetchedAt: new Date().toISOString(),
      listing,
    })}\n`
  );
}

export async function enrichMarcusListing(base: any): Promise<any> {
  if (!base.url) return base;
  try {
    const html = await fetchMarcusDetailHtml(base.url);
    if (!marcusDetailHtmlIsUsable(html)) {
      throw new Error("detail HTML did not contain a verified Marcus & Millichap property page");
    }
    const observed = detailObservation("marcus_detail_page", "live");
    const $ = cheerio.load(html);
    const address = parseMarcusAddress(clean($(".score-hero-body p").first().text()));
    const specs = parseMarcusSpecifications($);
    const priceText =
      clean($(".mm-property-price").first().text())?.replace(/^Listing Price:\s*/i, "") ??
      base.salePriceText ??
      null;
    const capRate = specs["Cap Rate"]?.match(/([0-9.]+)%/)?.[1];
    const yearBuilt = specs["Year Built"]?.match(/\b(18|19|20)\d{2}\b/)?.[0];
    const dealRoomUrl = marcusUrl($(".mm-property-documents-button a[href]").first().attr("href"));
    const photos = extractMarcusDetailImages($, base.photos ?? []);
    const gatedDocuments = dealRoomUrl
      ? [
          {
            name: clean($(".mm-property-documents-button a[href]").first().text()) ?? "Offering Memorandum & Deal Room",
            url: dealRoomUrl,
            gated: true,
          },
        ]
      : [];

    // Lift all institutional scalars from the parsed spec map via parseMarcusScalars.
    // This supersedes the prior manual units/occupancyRate extraction below and adds
    // capRatePct, salePricePerSf, lotSizeSf, leaseRate*, tenantName, guarantor,
    // leaseYearsRemaining, grm, pricePerUnit, pricePerAcre, numRooms, revpar, extraFacts.
    const scalars = parseMarcusScalars(specs, base.capRatePct as number | null);

    // Parse license strings from contactsDetailed contact objects (HTML-extracted).
    const rawContacts = extractMarcusContacts($);
    const contactsDetailed = rawContacts.map((c: any) => {
      const licenseRaw = c.license as string | null | undefined;
      const parsedLicense = parseMarcusContactLicense(licenseRaw) ?? licenseRaw ?? null;
      return prune({ ...c, license: parsedLicense });
    });

    // canonicalUrl: the canonical listing URL (contract field Section B).
    const canonicalUrl = base.url ?? null;

    // Capture-everything: build a minimal ScrapedDoc from the direct-fetch detail
    // HTML (this source bypasses Firecrawl, so there is no markdown/links/images/
    // attributes payload) and let harvestDetail's rawHtml regex fallback extract
    // any in-page video/tour iframes. To avoid harvesting site-chrome iframes
    // (analytics / recaptcha / global nav+footer), the rawHtml is scoped to the
    // property content region (<main>, else the listing wrapper) when present;
    // it falls back to the full HTML only if no content container is found. The
    // gated dealRoomUrl OM is promoted as a classified DocItem; the curated
    // gallery photos are promoted so any in-page tour url is captured.
    const contentHtml =
      clean($("main").first().html()) ??
      clean($(".mm-property, .property-detail, #property-detail").first().html()) ??
      html;
    const detailDoc: ScrapedDoc = { rawHtml: contentHtml, markdown: "", links: [] };
    const extraDocs: DocItem[] = gatedDocuments.map((d) => ({
      url: d.url as string,
      title: d.name,
      docType: "om" as const,
    }));
    const harvested = harvestDetail(detailDoc, {
      extraDocs,
      extraImages: photos,
      baseUrl: base.url,
    });

    return prune({
      ...base,
      name: clean($("h1").first().text()) ?? base.name,
      street: address.street ?? base.street,
      city: address.city ?? base.city,
      state: address.state ?? base.state,
      postalCode: address.postalCode ?? base.postalCode,
      salePriceUsd: moneyToNumber(priceText) ?? base.salePriceUsd,
      salePriceText: priceText,
      // capRatePct from detail spec wins; scalars.capRatePct is already the best available.
      capRatePct: capRate ? Number(capRate) : scalars.capRatePct ?? base.capRatePct,
      yearBuilt: yearBuilt ? Number(yearBuilt) : null,
      // Institutional scalars from parseMarcusScalars (additive; prune drops nulls).
      occupancyRate: scalars.occupancyRate ?? undefined,
      sizeSf: scalars.sizeSf ?? undefined,
      salePricePerSf: scalars.salePricePerSf ?? undefined,
      lotSizeSf: scalars.lotSizeSf ?? undefined,
      units: scalars.units ?? undefined,
      leaseRateType: scalars.leaseRateType ?? undefined,
      leaseRateMin: scalars.leaseRateMin ?? undefined,
      leaseRateMax: scalars.leaseRateMax ?? undefined,
      tenantName: scalars.tenantName ?? undefined,
      guarantor: scalars.guarantor ?? undefined,
      leaseYearsRemaining: scalars.leaseYearsRemaining ?? undefined,
      grm: scalars.grm ?? undefined,
      pricePerUnit: scalars.pricePerUnit ?? undefined,
      pricePerAcre: scalars.pricePerAcre ?? undefined,
      numRooms: scalars.numRooms ?? undefined,
      revpar: scalars.revpar ?? undefined,
      extraFacts: scalars.extraFacts ?? undefined,
      canonicalUrl,
      description:
        clean($(".mm-property-investment-overview p").first().text()) ??
        clean($('meta[name="description"]').attr("content")) ??
        base.description,
      contactsDetailed,
      photos,
      marcusSpecifications: specs,
      gatedDocuments,
      documents: harvested.documents.length ? harvested.documents : undefined,
      media: harvested.media.length ? harvested.media : undefined,
      links: harvested.links.length ? harvested.links : undefined,
      detailScrape: {
        url: base.url,
        rawHtmlLength: html.length,
        mediaCount: harvested.media.length,
        harvestLinkCount: harvested.links.length,
        harvestDocCount: harvested.documents.length,
      },
      detailObservedAt: observed.observedAt,
      freshnessProvenance: {
        detailScope: "detail_page",
        generationId: observed.generationId,
        method: observed.method,
        cacheDisposition: observed.cacheDisposition,
      },
    });
  } catch (err) {
    console.error(`  marcus-millichap/sale: detail failed for ${base.url}: ${err}`);
    return prune({ ...base, detailError: String(err) });
  }
}

export async function srcMarcusMillichap(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "Marcus & Millichap",
      sourceUrl: MARCUS_PROPERTIES_URL,
      method: "skipped",
      totalAvailable: 0,
      listings: [],
      note:
        "Sale-only in the public property UI. The documented public bundle exposes property and auction endpoints, but no public lease search mode or lease endpoint was found.",
    };
  }
  const search = await marcusPost("/api/contentsearch/properties", marcusSearchBody(2));
  const { rows, total } = parseMarcusPropertiesResponse(search);
  if (!rows.length) throw new Error("Marcus & Millichap public properties API sanity check returned no rows");
  console.error(
    `  marcus-millichap/sale: public properties API sanity check returned ${rows.length} row(s), total ${
      total ?? "?"
    }`
  );
  const mapRows = await fetchMarcusMapRows();
  if (!mapRows.length) throw new Error("Marcus & Millichap public mapproperties API returned no rows");
  assertMarcusInventoryCount(total, mapRows);
  const inventoryObservedAt = new Date().toISOString();
  const observedMapRows = mapRows.map((row) => ({
    ...row,
    __creInventoryObservedAt: inventoryObservedAt,
  }));
  const want = Math.min(max, observedMapRows.length);
  const selectedMapRows = observedMapRows.slice(
    0,
    Number.isFinite(want) ? want : observedMapRows.length
  );
  const truncated = marcusCapTruncated(
    max,
    selectedMapRows.length,
    observedMapRows.length
  );
  console.error(
    `  marcus-millichap/sale: public map API returned ${mapRows.length} ActivityId row(s), expanding ${selectedMapRows.length}`
  );
  const baseRows = await pmap(selectedMapRows, CONCURRENCY, fetchMarcusMapListing);
  assertMarcusMapDetails(selectedMapRows, baseRows);
  const baseListings = baseRows as any[];
  if (monitor) {
    // Monitor mode: keep the lightweight per-ActivityId mappropertydetail POST
    // (it is the only source of the DealId external id and the http PropertyUrl,
    // both to_row-required) but skip the heavy per-listing detail-HTML enrich.
    // Free change keys: salePriceUsd, capRatePct, NewlyListed/NewlyReduced flags.
    return {
      company: "Marcus & Millichap",
      sourceUrl: MARCUS_PROPERTIES_URL,
      method:
        "Public POST /api/contentsearch/mapproperties ActivityIds plus mappropertydetail tiles for id/url/price/cap rate (monitor mode; detail HTML enrichment skipped)",
      totalAvailable: total,
      listings: baseListings,
      truncated,
      note: "Monitor mode: map + mappropertydetail tile fields only (id, url, price, cap rate, flags); the per-listing detail-HTML render is skipped. No terminal status (flags-only source).",
    };
  }
  const cachePath = marcusDetailCachePath();
  // A checkpoint refresh generation is the freshness boundary. Reuse only
  // exact-identity details fetched earlier in that same generation so a crash
  // can resume without admitting data from an older refresh.
  const cacheAttemptId = refreshGenerationId() ?? randomUUID();
  prepareMarcusDetailCache(cachePath, cacheAttemptId);
  const cachedDetails = readMarcusDetailCache(cachePath, cacheAttemptId, baseListings);
  if (cachedDetails.size) {
    console.error(`  marcus-millichap/sale: loaded ${cachedDetails.size} cached detail row(s) from ${cachePath}`);
  }
  let done = 0;
  const listings = await pmap(baseListings, CONCURRENCY, async (row) => {
    const key = marcusListingCacheKey(row);
    const cached = key ? cachedDetails.get(key) : null;
    const enriched = cached ?? (await enrichMarcusListing(row));
    if (!cached) appendMarcusDetailCache(cachePath, cacheAttemptId, row, enriched);
    done++;
    if (done % 10 === 0 || done === baseListings.length) {
      console.error(`  marcus-millichap/sale: detail enriched ${done}/${baseListings.length}`);
    }
    return enriched;
  });
  return {
    company: "Marcus & Millichap",
    sourceUrl: MARCUS_PROPERTIES_URL,
    method:
      "Public POST /api/contentsearch/mapproperties ActivityIds, mappropertydetail tiles, and direct public detail HTML enrichment",
    totalAvailable: total,
    listings,
    truncated,
    note:
      "Public sale inventory only. The list endpoint still caps unfiltered visible rows at the newest 100, so discovery uses public map ActivityIds plus mappropertydetail tiles. Lease remains skipped because no public lease UI mode or endpoint has been proven.",
  };
}
