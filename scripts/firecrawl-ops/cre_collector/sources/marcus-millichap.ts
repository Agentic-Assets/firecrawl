// sources/marcus-millichap.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import { CONCURRENCY, OUT_PATH, flags } from "../lib/config.js";
import { dedupeStrings } from "../lib/html.js";
import { SourceResult, Tx } from "../types.js";
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
    rawMarcusSearchRow: row,
  });
}

export async function fetchMarcusMapRows(): Promise<any[]> {
  const map = await marcusPost("/api/contentsearch/mapproperties", marcusSearchBody(1));
  const results = map.Results ?? map;
  const rows = Array.isArray(results.Properties) ? results.Properties : Array.isArray(results) ? results : [];
  const seen = new Set<string>();
  return rows.filter((row: any) => {
    const activityId = clean(row.ActivityId);
    if (!activityId || seen.has(activityId)) return false;
    seen.add(activityId);
    return true;
  });
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
      detailError: String(err),
      rawMarcusSearchRow: mapRow,
    });
  }
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

export function marcusListingCacheKey(listing: any): string | null {
  return clean(String(listing?.id ?? listing?.activityId ?? listing?.url ?? ""));
}

export function marcusDetailCachePath(): string {
  return OUT_PATH ? `${OUT_PATH}.marcus-detail-cache.jsonl` : "out/marcus-millichap-detail-cache.jsonl";
}

export function readMarcusDetailCache(path: string): Map<string, any> {
  const cached = new Map<string, any>();
  if (!existsSync(path)) return cached;
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const listing = JSON.parse(line);
      if (listing?.detailError) continue;
      const key = marcusListingCacheKey(listing);
      if (key) cached.set(key, listing);
    } catch {
      // Ignore a partial final line if a prior run was interrupted mid-write.
    }
  }
  return cached;
}

export function appendMarcusDetailCache(path: string, listing: any): void {
  if (listing?.detailError) return;
  mkdirSync(dirname(path), { recursive: true });
  appendFileSync(path, `${JSON.stringify(listing)}\n`);
}

export async function enrichMarcusListing(base: any): Promise<any> {
  if (!base.url) return base;
  try {
    const html = await fetchMarcusDetailHtml(base.url);
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
    return prune({
      ...base,
      name: clean($("h1").first().text()) ?? base.name,
      street: address.street ?? base.street,
      city: address.city ?? base.city,
      state: address.state ?? base.state,
      postalCode: address.postalCode ?? base.postalCode,
      salePriceUsd: moneyToNumber(priceText) ?? base.salePriceUsd,
      salePriceText: priceText,
      capRatePct: capRate ? Number(capRate) : base.capRatePct,
      yearBuilt: yearBuilt ? Number(yearBuilt) : null,
      description:
        clean($(".mm-property-investment-overview p").first().text()) ??
        clean($('meta[name="description"]').attr("content")) ??
        base.description,
      contactsDetailed: extractMarcusContacts($),
      photos: extractMarcusDetailImages($, base.photos ?? []),
      marcusSpecifications: specs,
      gatedDocuments: dealRoomUrl
        ? [
            {
              name: clean($(".mm-property-documents-button a[href]").first().text()) ?? "Offering Memorandum & Deal Room",
              url: dealRoomUrl,
              gated: true,
            },
          ]
        : [],
      detailScrape: {
        url: base.url,
        rawHtmlLength: html.length,
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
  const results = search.Results ?? search;
  const rows = Array.isArray(results.Properties) ? results.Properties : [];
  const total = typeof results.TotalCount === "number" ? results.TotalCount : null;
  if (!rows.length) throw new Error("Marcus & Millichap public properties API sanity check returned no rows");
  console.error(
    `  marcus-millichap/sale: public properties API sanity check returned ${rows.length} row(s), total ${
      total ?? "?"
    }`
  );
  const mapRows = await fetchMarcusMapRows();
  if (!mapRows.length) throw new Error("Marcus & Millichap public mapproperties API returned no rows");
  const want = Math.min(max, mapRows.length);
  const selectedMapRows = mapRows.slice(0, Number.isFinite(want) ? want : mapRows.length);
  console.error(
    `  marcus-millichap/sale: public map API returned ${mapRows.length} ActivityId row(s), expanding ${selectedMapRows.length}`
  );
  const baseRows = await pmap(selectedMapRows, CONCURRENCY, fetchMarcusMapListing);
  const baseListings = baseRows.filter((l: any) => l?.url);
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
      note: "Monitor mode: map + mappropertydetail tile fields only (id, url, price, cap rate, flags); the per-listing detail-HTML render is skipped. No terminal status (flags-only source).",
    };
  }
  const cachePath = marcusDetailCachePath();
  const cachedDetails = readMarcusDetailCache(cachePath);
  if (cachedDetails.size) {
    console.error(`  marcus-millichap/sale: loaded ${cachedDetails.size} cached detail row(s) from ${cachePath}`);
  }
  let done = 0;
  const listings = await pmap(baseListings, CONCURRENCY, async (row) => {
    const key = marcusListingCacheKey(row);
    const cached = key ? cachedDetails.get(key) : null;
    const enriched = cached ?? (await enrichMarcusListing(row));
    if (!cached) appendMarcusDetailCache(cachePath, enriched);
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
    note:
      "Public sale inventory only. The list endpoint still caps unfiltered visible rows at the newest 100, so discovery uses public map ActivityIds plus mappropertydetail tiles. Lease remains skipped because no public lease UI mode or endpoint has been proven.",
  };
}
