// sources/transwestern.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { dedupeStrings, titleFromFilename } from "../lib/html.js";
import { scrapeDoc, scrapeJson } from "../lib/scrape.js";
import { harvestDetail } from "../lib/harvest.js";
import { detailObservation, refreshGenerationId } from "../lib/freshness.js";
import { ScrapedDoc, SourceResult, Tx } from "../types.js";
import { clean, num, pmap, prune } from "../lib/util.js";
import { parseLeaseRate, normBuildingClass, acresToSf } from "../lib/parse.js";


// --- Transwestern: public properties GET feed plus detail enrichment ---

export const TRANSWESTERN_HOST = "https://transwestern.com";
export const TRANSWESTERN_BUCKETS: Record<Tx, string[]> = {
  sale: ["Sale", "Sale or Lease"],
  lease: ["Lease", "Sublease", "Sale or Lease"],
};

export function canonicalTranswesternUrl(href: string | null): string | null {
  const h = clean(href);
  if (!h || /^javascript:/i.test(h) || h === "-") return null;
  try {
    return new URL(h, TRANSWESTERN_HOST).toString();
  } catch {
    return null;
  }
}

export function transwesternFeedUrl(bucket: string): string {
  const params = new URLSearchParams({
    call: "ajax",
    search: "",
    Latitude: "",
    Longitude: "",
    DealsType: bucket,
    PropertyType: "0",
    MetroName: "",
    SubTypeIDs: "",
    TenancyTypes: "",
    CheckLeed: "false",
    IsEnergyStar: "false",
    MinPrice: "",
    MaxPrice: "",
    MinSize: "",
    MaxSize: "",
    SortType: "asc",
    SortColumn: "",
    class: "",
    TotalLotSizeMin: "",
    TotalLotSizeMax: "",
    NoOfUnitsMin: "",
    NoOfUnitsMax: "",
  });
  return `${TRANSWESTERN_HOST}/properties?${params.toString()}`;
}

export function transwesternDetailUrl(pageUrl: any): string | null {
  const slug = clean(String(pageUrl ?? ""));
  if (!slug || slug === "-") return null;
  return `${TRANSWESTERN_HOST}/property/${encodeURIComponent(slug).replace(/%2F/g, "/")}`;
}

export function transwesternTransactionType(bucket: string): string {
  if (/sale or lease/i.test(bucket)) return "Sale/Lease";
  if (/sublease/i.test(bucket)) return "Sublease";
  if (/lease/i.test(bucket)) return "Lease";
  return "Sale";
}

export function transwesternSizeText(row: any): string | null {
  const size = num(Number(row.PropertySize));
  return size ? `${size.toLocaleString("en-US")} SF` : null;
}

export function transwesternPriceText(row: any, tx: Tx): string | null {
  const price = num(Number(row.Price));
  if (!price) return tx === "sale" ? "Contact broker for pricing" : null;
  return `$${price.toLocaleString("en-US")}`;
}

export function transwesternCapTruncated(
  max: number,
  emitted: number,
  knownInventory: number
): boolean {
  return Number.isFinite(max) && emitted < knownInventory;
}

export function parseTranswesternFacts($: cheerio.CheerioAPI): Record<string, string> {
  const facts: Record<string, string> = {};
  $("li, .property-detail li, .property-facts li").each((_, el) => {
    const label = clean($(el).find("b,strong").first().text()?.replace(/:$/, ""));
    if (!label) return;
    const value = clean($(el).text().replace($(el).find("b,strong").first().text(), ""));
    if (value) facts[label] = value.replace(/^:\s*/, "");
  });
  return facts;
}

export function parseTranswesternAvailability($: cheerio.CheerioAPI): any[] {
  const rows: any[] = [];
  $("#tblAvailability tr").each((_, tr) => {
    const cells = $(tr)
      .find("th,td")
      .map((__, td) => clean($(td).text()))
      .get()
      .filter(Boolean);
    if (cells.length < 2 || /suite/i.test(cells.join(" ")) && $(tr).find("th").length) return;
    rows.push({
      suite: cells[0] ?? null,
      size: cells[1] ?? null,
      rate: cells[2] ?? null,
      type: cells[3] ?? null,
      raw: cells,
    });
  });
  return rows;
}

export function extractTranswesternContacts(doc: ScrapedDoc): any[] {
  const $ = cheerio.load(doc.rawHtml);
  const contactsByKey = new Map<string, any>();
  $(".PropertyVcard .v-card, .v-card").each((_, el) => {
    const card = $(el);
    const profileUrl = canonicalTranswesternUrl(
      card.find('a[href^="/"]:not([href*="vcard-generator"])').first().attr("href") ?? null
    );
    const vcardUrl = canonicalTranswesternUrl(
      card.find('a[href*="vcard-generator"]').first().attr("href") ?? null
    );
    const avatarUrl = canonicalTranswesternUrl(card.find("img").first().attr("src") ?? null);
    const phone =
      clean(card.find('a[href^="tel:"]').first().text()) ??
      clean(card.text().match(/(\+?1?\s*\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})/)?.[1] ?? null);
    const email = clean(card.find('a[href^="mailto:"]').first().attr("href")?.replace(/^mailto:/i, ""));
    const linkText = clean(
      card.find('a[href^="/"]:not([href*="vcard-generator"])').first().text()
    );
    const name =
      clean(card.find(".name, .broker-name, h3, h4").first().text()) ??
      linkText ??
      clean(card.find("strong").first().text());
    const title =
      clean(card.find(".title, .job-title").first().text()) ??
      clean(
        card
          .text()
          .split("\n")
          .map((s) => s.trim())
          .find((s) => /associate|director|broker|principal|vice president|managing/i.test(s)) ??
          null
      );
    const key = profileUrl ?? vcardUrl ?? email ?? name;
    if (!key) return;
    contactsByKey.set(key, {
      name,
      title,
      email,
      phone,
      company: "Transwestern",
      profileUrl,
      avatarUrl,
      vcardUrl,
    });
  });
  return [...contactsByKey.values()].filter(
    (c) => c.name || c.email || c.phone || c.profileUrl || c.avatarUrl || c.vcardUrl
  );
}

export function extractTranswesternDocuments(doc: ScrapedDoc): any[] {
  const $ = cheerio.load(doc.rawHtml);
  const candidates: string[] = [];
  $('#tblAttachments a[href], a.download-att-btn[href], a.download-flyer-btn[href], a[href$=".pdf"], a[href*=".pdf"], a[href*="twurls.com"]').each(
    (_, el) => {
      const u = canonicalTranswesternUrl($(el).attr("href") ?? null);
      if (u) candidates.push(u);
    }
  );
  for (const link of doc.links ?? []) {
    if (/\.pdf(?:\?|$)|twurls\.com/i.test(link)) {
      const u = canonicalTranswesternUrl(link);
      if (u) candidates.push(u);
    }
  }
  return dedupeStrings(candidates)
    .filter((url) => !/\/Upload\/TREC\/|\/privacy-policy(?:\?|$)|health1\.aetna\.com/i.test(url))
    .map((url) => ({ name: titleFromFilename(url), url }));
}

export function extractTranswesternPhotos(doc: ScrapedDoc, feedImage: string | null): string[] {
  const $ = cheerio.load(doc.rawHtml);
  const candidates: Array<string | null> = [feedImage];
  $('.photos-list a.chocolat-image[href], a.chocolat-image[href], a[href*="/images/"], img[src*="/images/"]').each(
    (_, el) => {
      candidates.push(canonicalTranswesternUrl($(el).attr("href") ?? $(el).attr("src") ?? null));
    }
  );
  return dedupeStrings(candidates).filter(
    (url) =>
      !/\.pdf(?:\?|$)/i.test(url) &&
      !/\/assets\/images\/(?:mail|comment|connect-image|tw-logo|Transwestern_2023|tw_gl|transwestern-mapmarker)/i.test(url)
  );
}

// Lift structured fields out of the Transwestern property-facts label/value map
// onto the shared listing vocabulary keys that cre_ingest.to_row maps into the
// existing cre_listings columns (year_built, units, floors, parking_spaces,
// zoning). Case-insensitive label match; only emits a key when a clean numeric /
// text value is present, so a sparse facts block never fabricates a column.
export function transwesternStructured(facts: Record<string, string>): {
  yearBuilt?: number;
  units?: number;
  floors?: number;
  parkingSpaces?: number;
  zoning?: string;
} {
  const byLabel = (re: RegExp): string | null => {
    for (const [label, value] of Object.entries(facts)) {
      if (re.test(label)) {
        const v = clean(value);
        if (v) return v;
      }
    }
    return null;
  };
  const intOf = (s: string | null): number | undefined => {
    if (!s) return undefined;
    const m = s.replace(/,/g, "").match(/-?\d+(?:\.\d+)?/);
    const n = m ? Number(m[0]) : NaN;
    return Number.isFinite(n) && n !== 0 ? n : undefined;
  };
  const out: {
    yearBuilt?: number;
    units?: number;
    floors?: number;
    parkingSpaces?: number;
    zoning?: string;
  } = {};
  const yb = intOf(byLabel(/year\s*built|built/i));
  if (yb && yb > 1700 && yb < 2100) out.yearBuilt = yb;
  const units = intOf(byLabel(/\b(?:no\.?\s*of\s*)?units\b|number\s*of\s*units/i));
  if (units) out.units = units;
  const floors = intOf(byLabel(/\bfloors?\b|\bstories\b|\bno\.?\s*of\s*floors\b/i));
  if (floors) out.floors = floors;
  const parking = intOf(byLabel(/parking\s*(?:spaces|spots|stalls)?/i));
  if (parking) out.parkingSpaces = parking;
  const zoning = byLabel(/\bzoning\b/i);
  if (zoning) out.zoning = zoning;
  return out;
}

// ---------------------------------------------------------------------------
// Phase-2 Data-Lift: lift recoverable scalars from transwesternFacts +
// availability[] into the contract camelCase vocabulary (Section B).
// Called at detail-scrape time after parseTranswesternFacts / parseTranswesternAvailability.
// Never duplicates fields already set by transwesternStructured (yearBuilt/units/floors/
// parkingSpaces/zoning). additive-only: returns undefined on absent fields.
// ---------------------------------------------------------------------------

// Vocabulary for the lease-rate-type token scan (gap doc: FSG/NNN/MG/IG/'Absolute Net').
const LEASE_TYPE_VOCAB: Array<{ re: RegExp; token: string }> = [
  { re: /modified\s+gross|mod\s+gross/i, token: "modified_gross" },
  { re: /full\s+service|\bfsg\b/i, token: "full_service" },
  { re: /nnn|triple\s+net/i, token: "nnn" },
  // "Absolute Net" is a TW-specific alias for NNN.
  { re: /absolute\s+net/i, token: "nnn" },
  // "IG" (industrial gross) or bare "Gross".
  { re: /\bgrosse?\b|\big\b/i, token: "gross" },
];

// Threshold for the Land Area (ac) unit guard.
// Values >= this are almost certainly already in SF (e.g. 29,185 SF is not 29,185 acres).
// A legitimate large acreage above ~10,000 acres would be extremely rare for a commercial listing.
const LAND_AREA_ACRES_THRESHOLD = 10000;

export interface TranswesternScalars {
  buildingClass?: string | null;
  clearHeightFt?: number | null;
  dockDoors?: number | null;
  driveInDoors?: number | null;
  powerService?: string | null;
  railServed?: boolean | null;
  apn?: string | null;
  lotSf?: number | null;
  minDivisibleSf?: number | null;
  maxDivisibleSf?: number | null;
  availableSf?: number | null;
  leaseRateMin?: number | null;
  leaseRateMax?: number | null;
  leaseRateType?: string | null;
  canonicalUrl?: string | null;
  extraFacts?: Record<string, string | number> | null;
}

/**
 * Lift Phase-2 recoverable scalar fields from transwesternFacts + availability[].
 *
 * Contract semantics:
 * - buildingClass: normBuildingClass(facts['Class']); A/B/C/D or null.
 * - clearHeightFt: first numeric in facts['Clear Height(max)'] or 'Clear Height(min)'.
 * - dockDoors: facts['Docks'] integer.
 * - driveInDoors: facts['Grade Level Doors'] integer.
 * - powerService: facts['Power'] clean text.
 * - railServed: facts['Rail'] -> true when 'yes'/truthy non-'no'; false when 'No'; null when absent.
 * - apn: facts['Parcel'] clean text.
 * - lotSf: facts['Land Area (ac)'] converted x43560 ONLY when the value looks like acres
 *   (< LAND_AREA_ACRES_THRESHOLD), else null (DQ guard: 29,185 is plainly SF, not acres).
 * - minDivisibleSf/maxDivisibleSf: min/max over availability[].size (comma-stripped numeric).
 * - availableSf: sum of availability[].size where type does NOT contain 'sale'.
 * - leaseRateMin/Max: min/max over availability[].rate where type NOT sale AND parsed $/SF < 1000.
 * - leaseRateType: first matching vocabulary token across all availability[].raw[] strings
 *   (vocabulary-matched, not hardcoded by index).
 * - canonicalUrl: the listing url field.
 * - extraFacts: long-tail facts (Year Renovated, Typical Floor Size, Elevators, Yard, Crane).
 */
export function liftTranswesternScalars(
  facts: Record<string, string>,
  availability: Array<{ raw?: string[]; rate?: string | null; size?: string | null; type?: string | null }>,
  url: string | null
): TranswesternScalars {
  const out: TranswesternScalars = {};

  // --- Helper: look up a fact label (case-insensitive regex match) ---
  const byLabel = (re: RegExp): string | null => {
    for (const [label, value] of Object.entries(facts)) {
      if (re.test(label)) {
        const v = clean(value);
        if (v) return v;
      }
    }
    return null;
  };

  // --- Helper: parse a plain integer from a string (no-zero) ---
  const intOf = (s: string | null): number | null => {
    if (!s) return null;
    const m = s.replace(/,/g, "").match(/^-?\d+(?:\.\d+)?/);
    if (!m) return null;
    const n = Number(m[0]);
    return Number.isFinite(n) && n > 0 ? Math.round(n) : null;
  };

  // --- Helper: parse a plain float (for clear height) ---
  const floatOf = (s: string | null): number | null => {
    if (!s) return null;
    const m = s.replace(/,/g, "").match(/([0-9]+(?:\.[0-9]+)?)/);
    if (!m) return null;
    const n = Number(m[1]);
    return Number.isFinite(n) && n > 0 ? n : null;
  };

  // 1. buildingClass
  const rawClass = byLabel(/^class$/i);
  if (rawClass !== null) {
    out.buildingClass = normBuildingClass(rawClass);
  }

  // 2. clearHeightFt (prefer max; fall back to min)
  const rawCHMax = byLabel(/clear\s*height\s*\(max\)/i) ?? byLabel(/clear\s*height\s*\(min\)/i) ?? byLabel(/clear\s*height/i) ?? byLabel(/ceiling\s*height/i);
  if (rawCHMax !== null) {
    out.clearHeightFt = floatOf(rawCHMax);
  }

  // 3. dockDoors
  const rawDocks = byLabel(/^docks?$/i);
  if (rawDocks !== null) {
    out.dockDoors = intOf(rawDocks);
  }

  // 4. driveInDoors (grade level = drive-in)
  const rawGrade = byLabel(/grade\s*level\s*doors?/i);
  if (rawGrade !== null) {
    out.driveInDoors = intOf(rawGrade);
  }

  // 5. powerService
  const rawPower = byLabel(/^power$/i);
  if (rawPower !== null) {
    out.powerService = rawPower;
  }

  // 6. railServed
  const rawRail = byLabel(/^rail$/i);
  if (rawRail !== null) {
    const lower = rawRail.toLowerCase();
    out.railServed = lower === "no" ? false : lower === "yes" || lower === "true" ? true : null;
  }

  // 7. apn (Parcel)
  const rawParcel = byLabel(/^parcel$/i);
  if (rawParcel !== null) {
    out.apn = rawParcel;
  }

  // 8. lotSf: Land Area (ac) with unit guard
  const rawLandArea = byLabel(/^land\s*area\s*\(ac\)$/i);
  if (rawLandArea !== null) {
    // Strip commas to get the numeric value for the threshold check.
    const numericStr = rawLandArea.replace(/,/g, "");
    const numericVal = Number(numericStr.match(/([0-9]+(?:\.[0-9]+)?)/)?.[1] ?? "NaN");
    if (Number.isFinite(numericVal) && numericVal > 0 && numericVal < LAND_AREA_ACRES_THRESHOLD) {
      // Looks like a real acreage; convert to SF.
      // acresToSf handles "29.2" and "29.2 ac" equally.
      out.lotSf = acresToSf(rawLandArea) ?? acresToSf(`${numericStr} ac`);
    }
    // If >= threshold (e.g. "29,185"), the value is almost certainly already SF -> suppress.
  }

  // 9-11. Availability-derived fields: minDivisibleSf, maxDivisibleSf, availableSf, rates, type
  const sizesAll: number[] = [];
  const sizesLease: number[] = [];
  const leaseRates: number[] = [];
  let leaseRateType: string | null = null;

  for (const avRow of availability) {
    const rawSize = clean(avRow.size ?? null);
    const rawType = clean(avRow.type ?? null) ?? "";
    const isSaleType = /sale/i.test(rawType);

    // Parse size (comma-stripped integer)
    if (rawSize) {
      const sf = intOf(rawSize.replace(/\s*sf\b/i, ""));
      if (sf !== null && sf > 0) {
        sizesAll.push(sf);
        if (!isSaleType) sizesLease.push(sf);
      }
    }

    // Parse lease rate (only for non-sale rows)
    if (!isSaleType) {
      const rawRate = clean(avRow.rate ?? null);
      if (rawRate) {
        const parsed = parseLeaseRate(rawRate);
        if (parsed.min !== null && parsed.min < 1000) {
          leaseRates.push(parsed.min);
        }
        if (parsed.max !== null && parsed.max < 1000) {
          leaseRates.push(parsed.max);
        }
      }

      // Scan all tokens in raw[] for a lease-type vocabulary match (index-agnostic)
      if (leaseRateType === null && avRow.raw && avRow.raw.length > 0) {
        for (const cell of avRow.raw) {
          const cellStr = clean(cell);
          if (!cellStr) continue;
          for (const { re, token } of LEASE_TYPE_VOCAB) {
            if (re.test(cellStr)) {
              leaseRateType = token;
              break;
            }
          }
          if (leaseRateType !== null) break;
        }
      }
    }
  }

  if (sizesAll.length > 0) {
    out.minDivisibleSf = Math.min(...sizesAll);
    out.maxDivisibleSf = Math.max(...sizesAll);
  }
  if (sizesLease.length > 0) {
    out.availableSf = sizesLease.reduce((a, b) => a + b, 0);
  }
  if (leaseRates.length > 0) {
    out.leaseRateMin = Math.min(...leaseRates);
    out.leaseRateMax = Math.max(...leaseRates);
    // When min == max (single rate), set max to null (no range).
    if (out.leaseRateMax === out.leaseRateMin) out.leaseRateMax = null;
  }
  if (leaseRateType !== null) {
    out.leaseRateType = leaseRateType;
  }

  // 12. canonicalUrl
  const cu = clean(url);
  if (cu) out.canonicalUrl = cu;

  // 13. extraFacts: long-tail fields with no discrete contract column
  const EXTRA_KEYS: Array<{ re: RegExp; key: string }> = [
    { re: /^year\s*renovated$/i, key: "year_renovated" },
    { re: /^typical\s*floor\s*size$/i, key: "typical_floor_size" },
    { re: /^elevators?$/i, key: "elevators" },
    { re: /^yard$/i, key: "yard" },
    { re: /^crane$/i, key: "crane" },
  ];
  const extraFacts: Record<string, string | number> = {};
  for (const { re, key } of EXTRA_KEYS) {
    const v = byLabel(re);
    if (v !== null) extraFacts[key] = v;
  }
  if (Object.keys(extraFacts).length > 0) {
    out.extraFacts = extraFacts;
  }

  return out;
}

export function transwesternDescription($: cheerio.CheerioAPI, doc: ScrapedDoc): string | null {
  const candidate =
    clean($(".property-description, .PropertyDescription, #overview").first().text()) ??
    clean(doc.markdown.match(/Overview\s*([\s\S]{1,1800}?)(?:\n[A-Z][A-Za-z ]+\n|\n#{1,6}\s|$)/i)?.[1]);
  if (
    !candidate ||
    /TREC Information About Brokerage Services|Privacy Policy|Copyright\s+Transwestern|Sitemap|Working-at-Transwestern/i.test(
      candidate
    )
  ) {
    return null;
  }
  return candidate;
}

export function transwesternDetailPageIsUsable(doc: ScrapedDoc, row: any): boolean {
  if (
    !doc?.rawHtml?.trim() ||
    /captcha|access denied|verify you are human|cf-chl-|page not found|404 error/i.test(
      `${doc.rawHtml}\n${doc.markdown ?? ""}`
    )
  ) {
    return false;
  }
  const $ = cheerio.load(doc.rawHtml);
  const structure =
    $("#tblAvailability, .property-facts, .property-description, .property-detail").length > 0;
  const heading = clean(
    $("h1").first().text() ||
      $('meta[property="og:title"]').attr("content") ||
      $("title").first().text()
  )?.toLowerCase();
  const identities = [
    clean(row?.BuildingName),
    clean(row?.PropertyName),
    clean(row?.FullAddress),
    clean(row?.Address),
    clean(row?.StreetAddress),
  ]
    .filter((value): value is string => !!value)
    .map((value) => value.toLowerCase());
  return Boolean(
    structure &&
    heading &&
    identities.length > 0 &&
    identities.some((identity) => heading.includes(identity) || identity.includes(heading))
  );
}

export async function enrichTranswesternListing(
  row: any,
  bucket: string,
  tx: Tx,
  monitor: boolean,
  inventoryObservedAt = new Date().toISOString()
): Promise<any> {
  const detailUrl = transwesternDetailUrl(row.PageUrl);
  const feedImage = canonicalTranswesternUrl(clean(row.PropertyImage));
  const base = {
    id: clean(String(row.PageUrl ?? "")),
    name: clean(row.BuildingName),
    transactionType: transwesternTransactionType(bucket),
    assetType: clean(row.PropertyTypeName),
    street: clean(row.FullAddress),
    city: clean(row.City),
    state: clean(row.State)?.toUpperCase() ?? null,
    postalCode: clean(row.ZipCode),
    country: "US",
    latitude: row.Latitude != null ? Number(row.Latitude) : null,
    longitude: row.Longitude != null ? Number(row.Longitude) : null,
    salePriceUsd: tx === "sale" ? num(Number(row.Price)) : null,
    salePriceText: tx === "sale" ? transwesternPriceText(row, tx) : null,
    sizeText: transwesternSizeText(row),
    buildingSizeSqft: num(Number(row.PropertySize)),
    brokerIds: [],
    photos: feedImage ? [feedImage] : [],
    url: detailUrl,
    rawTranswesternFeed: row,
    transwesternBucket: bucket,
    inventoryObservedAt,
    freshnessProvenance: {
      detailScope: "inventory_feed",
      generationId: refreshGenerationId(),
      method: "transwestern_ajax_feed",
      cacheDisposition: "live",
    },
  };
  // Monitor mode: emit the freely-available feed fields only (id/url/price/size)
  // and skip the detail scrape. Status has no feed field, so it stays absent
  // (do not render just to recover status), exactly per the design 14.1 intent.
  if (monitor) return prune(base);
  if (!detailUrl) return prune({ ...base, detailError: "missing or invalid PageUrl" });
  try {
    const doc = await scrapeDoc(detailUrl, { waitFor: 1500, timeout: 60000, maxAge: 0 });
    if (!transwesternDetailPageIsUsable(doc, row)) {
      throw new Error("detail response did not contain the expected Transwestern property identity");
    }
    const observed = detailObservation("transwestern_detail_page", "live");
    const $ = cheerio.load(doc.rawHtml);
    const facts = parseTranswesternFacts($);
    const availability = parseTranswesternAvailability($);
    const contactsDetailed = extractTranswesternContacts(doc);
    const brokerIds = contactsDetailed
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          email: clean(c.email),
          phone: clean(c.phone),
          office: clean(c.office),
          avatarUrl: clean(c.avatarUrl),
          company: "Transwestern",
        })
      )
      .filter((id: number | null): id is number => id !== null);
    const coordMatch = doc.rawHtml.match(/myLatLng\s*=\s*\{\s*lat:\s*(-?[0-9.]+),\s*lng:\s*(-?[0-9.]+)/i);
    const description = transwesternDescription($, doc);
    const leaseRateText =
      availability.map((a) => clean(a.rate)).find((rate) => rate && /\$|psf|sf|negotiable/i.test(rate)) ??
      null;
    // Capture-everything harvest: fold the source's own document/photo extractors
    // into the harvester (as bare strings so it re-classifies + dedups), and let
    // it lift video/tour media and outbound links from the detail rawHtml/links/
    // iframes. Brochures fold into the unified `documents` channel (cre_ingest
    // reads both `brochures` and `documents`); we drop the legacy `brochures` key
    // so the same PDF is not inserted twice.
    const nativeDocs = extractTranswesternDocuments(doc);
    const nativePhotos = extractTranswesternPhotos(doc, feedImage);
    const harvested = harvestDetail(doc, {
      baseUrl: detailUrl,
      extraDocs: nativeDocs.map((d: any) => clean(d?.url)).filter((u): u is string => !!u),
      extraImages: nativePhotos,
    });
    const structured = transwesternStructured(facts);
    const scalars = liftTranswesternScalars(facts, availability, detailUrl);
    return prune({
      ...base,
      name: clean($("h1").first().text()) ?? base.name,
      description,
      latitude: base.latitude ?? (coordMatch ? Number(coordMatch[1]) : null),
      longitude: base.longitude ?? (coordMatch ? Number(coordMatch[2]) : null),
      leaseRateText: tx === "lease" ? leaseRateText : null,
      brokerIds,
      contactsDetailed,
      brochures: undefined,
      documents: harvested.documents,
      photos: harvested.images.length ? harvested.images : nativePhotos,
      media: harvested.media,
      links: harvested.links,
      markdown: doc.markdown,
      // transwesternStructured fields (yearBuilt/units/floors/parkingSpaces/zoning):
      yearBuilt: structured.yearBuilt,
      units: structured.units,
      floors: structured.floors,
      parkingSpaces: structured.parkingSpaces,
      zoning: structured.zoning,
      // Phase-2 scalar lift (additive, no overlap with structured above):
      buildingClass: scalars.buildingClass,
      clearHeightFt: scalars.clearHeightFt,
      dockDoors: scalars.dockDoors,
      driveInDoors: scalars.driveInDoors,
      powerService: scalars.powerService,
      railServed: scalars.railServed,
      apn: scalars.apn,
      lotSf: scalars.lotSf,
      minDivisibleSf: scalars.minDivisibleSf,
      maxDivisibleSf: scalars.maxDivisibleSf,
      availableSf: scalars.availableSf,
      leaseRateMin: scalars.leaseRateMin,
      leaseRateMax: scalars.leaseRateMax,
      leaseRateType: scalars.leaseRateType,
      canonicalUrl: scalars.canonicalUrl,
      extraFacts: scalars.extraFacts,
      transwesternFacts: facts,
      availability,
      detailScrape: {
        url: detailUrl,
        markdownLength: doc.markdown.length,
        rawHtmlLength: doc.rawHtml.length,
        linkCount: doc.links.length,
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
    console.error(`  transwestern/${tx}: detail failed for ${detailUrl}: ${err}`);
    return prune({
      ...base,
      detailError: String(err),
    });
  }
}

export async function srcTranswestern(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const buckets = TRANSWESTERN_BUCKETS[tx];
  const rowsBySlug = new Map<string, { row: any; bucket: string; observedAt: string }>();
  const bucketCounts: Record<string, number> = {};
  for (const bucket of buckets) {
    const data = await scrapeJson(transwesternFeedUrl(bucket), {
      timeout: 60000,
      ...(monitor ? {} : { maxAge: 0 }),
    });
    if (!Array.isArray(data)) {
      throw new Error(`Transwestern ${tx}/${bucket} feed returned a non-array payload`);
    }
    const rows = data;
    const observedAt = new Date().toISOString();
    bucketCounts[bucket] = rows.length;
    console.error(`  transwestern/${tx}/${bucket}: ${rows.length} feed rows`);
    for (const row of rows) {
      const slug = clean(String(row.PageUrl ?? ""));
      if (!slug || slug === "-" || !transwesternDetailUrl(slug)) {
        throw new Error(
          `Transwestern ${tx}/${bucket} feed row lacks a valid PageUrl identity`
        );
      }
      if (!rowsBySlug.has(slug)) rowsBySlug.set(slug, { row, bucket, observedAt });
    }
  }
  const selected = [...rowsBySlug.values()].slice(0, Math.min(max, Number.MAX_SAFE_INTEGER));
  let done = 0;
  // In monitor mode enrichTranswesternListing returns feed-only fields and never
  // scrapes detail pages, so the progress verb must reflect enumeration, not the
  // detail enrichment that only the full path performs.
  const progressVerb = monitor ? "enumerated" : "detail enriched";
  const listings = await pmap(selected, CONCURRENCY, async ({ row, bucket, observedAt }) => {
    const listing = await enrichTranswesternListing(row, bucket, tx, monitor, observedAt);
    done++;
    if (done % 25 === 0 || done === selected.length) {
      console.error(`  transwestern/${tx}: ${progressVerb} ${done}/${selected.length}`);
    }
    return listing;
  });
  const total = [...new Set([...rowsBySlug.keys()])].length;
  return {
    company: "Transwestern",
    sourceUrl: "https://transwestern.com/properties",
    method: "Public /properties?call=ajax GET feed by DealsType plus detail-page raw HTML enrichment",
    totalAvailable: total,
    listings,
    truncated: transwesternCapTruncated(max, listings.length, total),
    note: `Bucket counts before slug de-dupe: ${Object.entries(bucketCounts)
      .map(([bucket, count]) => `${bucket}=${count}`)
      .join(", ")}. Rows with invalid PageUrl are skipped.`,
  };
}
