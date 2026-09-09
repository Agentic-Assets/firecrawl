// sources/colliers-main.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { decodeHtmlEntities, dedupeStrings, extractSitemapUrlEntries } from "../lib/html.js";
import { classifyDocument, harvestDetail } from "../lib/harvest.js";
import { scrapeDoc, scrapeRaw } from "../lib/scrape.js";
import { ScrapeOpts, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { parseLeaseRate } from "../lib/parse.js";
import { boundedInt, clean, moneyToNumber, num, pmap, prune } from "../lib/util.js";
import {
  detailObservation,
  generationMatches,
  refreshGenerationId,
  requireFreshDetails,
} from "../lib/freshness.js";


// --- Colliers main site: public XML sitemap discovery + detail-page render ---
// Unlock 2026-06-12: the bare /sitemap path (not sitemap.xml) is reachable
// through local Firecrawl, and its en ?type=properties child sitemap lists every
// usa####### detail URL with lastmod. Detail pages render with a
// RealEstateListing JSON-LD block plus clean markdown. This folds into the
// `colliers` brokerage as `colliers-main` (main: id prefix), leaving the
// SalesTracker `colliers` source untouched. The renderer remains a fallback;
// the supervised checkpoint pins the exact-ID, anonymous first-party Coveo
// transport implemented below. Neither path accepts auth or visitor tokens.
// See cre_scrapers/brokers/colliers/COLLIERS_MAIN_SITEMAP_UNLOCK_2026-06-12.md.

export const COLLIERS_MAIN_HOST = "https://www.colliers.com";
export const COLLIERS_MAIN_SITEMAP_INDEX = `${COLLIERS_MAIN_HOST}/sitemap`;
export const COLLIERS_MAIN_PROPERTIES_SITEMAP = `${COLLIERS_MAIN_HOST}/en/sitemap?type=properties`;
export const COLLIERS_MAIN_SOURCE_URL = `${COLLIERS_MAIN_HOST}/en/properties`;
export const COLLIERS_MAIN_DETAIL_CONCURRENCY = boundedInt(
  process.env.COLLIERS_MAIN_DETAIL_CONCURRENCY,
  Math.min(CONCURRENCY, 3),
  1,
  6
);
// Colliers detail pages sit behind Cloudflare; under sustained paging the site
// returns 429 "Just a moment..." challenge shells. A waitFor lets the stealth
// browser clear the challenge (same approach as CBRE waitFor 4000 / JLL 8000).
export const COLLIERS_MAIN_DETAIL_WAIT_MS = boundedInt(process.env.COLLIERS_MAIN_DETAIL_WAIT_MS, 4000, 0, 30000);
export const COLLIERS_MAIN_RUNTIME_CANARY_COUNT = boundedInt(
  process.env.COLLIERS_MAIN_RUNTIME_CANARY_COUNT,
  3,
  1,
  5
);
export const COLLIERS_MAIN_SITEMAP_RETRIES = boundedInt(
  process.env.COLLIERS_MAIN_SITEMAP_RETRIES,
  3,
  1,
  5
);
/**
 * A shared start-rate gate keeps the stealth proxy below the sustained request
 * rate at which Colliers begins returning Cloudflare challenge pages. It is a
 * start interval, rather than a worker sleep, so the bounded worker pool may
 * wait for browser renders without issuing a new burst as renders settle.
 */
export function colliersMainDetailStartIntervalMs(): number {
  return boundedInt(process.env.COLLIERS_MAIN_DETAIL_START_INTERVAL_MS, 1500, 0, 30000);
}

export function colliersMainChallengeCooldownMs(): number {
  return boundedInt(process.env.COLLIERS_MAIN_CHALLENGE_COOLDOWN_MS, 30000, 0, 180000);
}

let colliersMainNextDetailStartAt = 0;

type Sleep = (milliseconds: number) => Promise<void>;
type Clock = () => number;

export type ColliersMainDetailTelemetry = {
  kind: "attempt_success" | "challenge" | "transport_error" | "cooldown";
  attempt: number;
  cooldownMs?: number;
};

export type ColliersMainDetailTelemetryObserver = (
  event: ColliersMainDetailTelemetry
) => void;

const sleep: Sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

/** Test hook for the module-local pacer; production code never resets it. */
export function resetColliersMainDetailPacerForTest(): void {
  colliersMainNextDetailStartAt = 0;
}

export async function acquireColliersMainDetailStart(
  now: Clock = Date.now,
  wait: Sleep = sleep
): Promise<void> {
  // Re-check after every wait because a preceding worker can extend the
  // shared cooldown while this worker is sleeping.
  for (;;) {
    const current = now();
    const delay = colliersMainNextDetailStartAt - current;
    if (delay > 0) {
      await wait(delay);
      continue;
    }
    colliersMainNextDetailStartAt = current + colliersMainDetailStartIntervalMs();
    return;
  }
}

export function coolDownColliersMainDetailStarts(now: number = Date.now()): void {
  colliersMainNextDetailStartAt = Math.max(
    colliersMainNextDetailStartAt,
    now + colliersMainChallengeCooldownMs()
  );
}
export const COLLIERS_MAIN_SCRAPE_OPTS: ScrapeOpts = {
  proxy: "stealth",
  timeout: 120000,
  ...(COLLIERS_MAIN_DETAIL_WAIT_MS ? { waitFor: COLLIERS_MAIN_DETAIL_WAIT_MS } : {}),
};

export type ColliersMainEntry = {
  url: string;
  lastmod: string | null;
  id: string;
  inventoryObservedAt?: string;
};

export let colliersMainSitemapCache: ColliersMainEntry[] | null = null;
export let colliersMainEnrichedMemo: any[] | null = null;
export let colliersMainEnrichedStats = { errors: 0, deferred: 0 };

export const COLLIERS_MAIN_COVEO_BATCH_URL =
  process.env.COLLIERS_MAIN_COVEO_BATCH_URL ?? "http://localhost:3003/browser-batch-fetch";
export const COLLIERS_MAIN_COVEO_SEARCH_URL =
  `${COLLIERS_MAIN_HOST}/coveo/rest/search/v2?sitecoreItemUri=` +
  encodeURIComponent("sitecore://web/{FA041AD7-243D-4265-A0C4-522EF012F8FC}?lang=en&amp;ver=2") +
  "&siteName=Colliers";
export const COLLIERS_MAIN_COVEO_BATCH_SIZE = 250;
export const COLLIERS_MAIN_COVEO_REQUESTS_PER_SESSION = 16;
const COLLIERS_PROPERTY_TEMPLATE = "534C0EB71D32434FBE0F62A2AB174F16";
const COLLIERS_US_COUNTRY = "8B63F0071EE64C2ABFB925D1466E7738";
const COLLIERS_COVEO_SOURCE = "Coveo_web_index - COLLIERS-AZ-102-PROD";
const COLLIERS_COVEO_CLICK_ORIGINS = new Set([
  COLLIERS_MAIN_HOST,
  "https://cmimport.colliers.com",
]);

export function colliersMainCoveoEnabled(): boolean {
  return ["1", "true", "yes", "on"].includes((process.env.COLLIERS_MAIN_COVEO_ENABLE ?? "").toLowerCase());
}

const COLLIERS_MAIN_COVEO_PROPERTY_FIELDS = [
  "propertyz32xid", "propertyz32xtitle", "propertyz32xfullz32xaddress", "urllink",
  "forz32xsale", "forz32xlease", "propertyforsaleorleasecomputed", "propertyz32xstatus",
  "primarypropertytype", "propertytypescomputed", "city", "statez32xprovince", "latitude", "longitude",
  "fsalez32xpricez32xmin16556", "fsalez32xpricez32xmaz120x16556",
  "fleasez32xpricez32xmin16556", "fleasez32xpricez32xmaz120x16556", "currency",
  "hidez32xsalez32xprice", "hidez32xleasez32xprice", "salez32xtype", "leasez32xtype",
  "leasez32xratez32xtype", "propertysortpricecomputed",
  "buildingz32xsiz122xe", "buildingz32xsiz122xez32xunit",
  "propertysiz122xecomputed", "siz122xeunitcomputed", "siz122xez32xunit",
  "flotz32xsiz122xe16556", "lotz32xsiz122xez32xunit", "propertylotsiz122xesqmcomputed",
  "propertybuildingsiz122xesqmcomputed",
  "fminz32xarea16556", "fmaz120xz32xarea16556", "floorz32xareaz32xunit",
  "description", "propertyz32ximages", "relatedz32xdocuments", "relatedz32xlinks",
  "relatedz32xez120xperts", "relatedez120xpertsfullnamecomputed", "propertyz32xfeatures",
  "specifications", "z95xupdated", "lastz32xupdatedz32xdate",
];

const COLLIERS_MAIN_COVEO_EXPERT_FIELDS = [
  "z95xid", "z95xname", "displayname", "title", "email", "officez32xphone", "mobilez32xphone",
  "ez120xpertofficenamecomputed", "profilez32xpicture", "urllink", "licensez32xnumber",
];

function chunks<T>(values: T[], size: number): T[][] {
  const result: T[][] = [];
  for (let i = 0; i < values.length; i += size) result.push(values.slice(i, i + size));
  return result;
}

export function colliersMainCoveoQueryBody(ids: string[], kind: "property" | "expert"): string {
  if (!ids.length || ids.length > COLLIERS_MAIN_COVEO_BATCH_SIZE) throw new Error("invalid Colliers Coveo batch size");
  const normalized = ids.map((id) => id.replace(/[^a-z0-9]/gi, "").toUpperCase());
  if (normalized.some((id) => !id)) throw new Error("invalid Colliers Coveo identifier");
  const idField = kind === "property" ? "ftitle16556" : "z95xid";
  const identity = `(${normalized.map((id) => `@${idField}==${id}`).join(" OR ")})`;
  const aq = kind === "property"
    ? `(@z95xtemplate==${COLLIERS_PROPERTY_TEMPLATE} @country=${COLLIERS_US_COUNTRY}) ${identity}`
    : identity;
  return new URLSearchParams({
    aq,
    cq: `(@z95xlanguage==en) (@z95xlatestversion==1) (@source==\"${COLLIERS_COVEO_SOURCE}\")`,
    searchHub: "Properties",
    locale: "en",
    maximumAge: "0",
    firstResult: "0",
    numberOfResults: String(ids.length),
    fieldsToInclude: JSON.stringify(
      kind === "property" ? COLLIERS_MAIN_COVEO_PROPERTY_FIELDS : COLLIERS_MAIN_COVEO_EXPERT_FIELDS
    ),
    allowQueriesWithoutKeywords: "true",
  }).toString();
}

type CoveoResult = { title?: string; clickUri?: string; raw?: Record<string, any> };

function normalizeCoveoId(value: unknown): string | null {
  const normalized = clean(value)?.replace(/[^a-z0-9]/gi, "").toLowerCase();
  return normalized || null;
}

export function reconcileColliersMainCoveoExperts(
  expectedIds: string[],
  results: CoveoResult[],
  allowMissing = false
): Map<string, CoveoResult> {
  const expected = new Set(expectedIds.map(normalizeCoveoId).filter((id): id is string => Boolean(id)));
  if (expected.size !== expectedIds.length) {
    throw new Error("Colliers Coveo expert inventory contains an invalid or duplicate identifier");
  }
  const actual = new Map<string, CoveoResult>();
  for (const result of results) {
    const id = normalizeCoveoId(result.raw?.z95xid);
    if (!id) throw new Error("Colliers Coveo expert result lacks a native identifier");
    if (actual.has(id)) throw new Error(`Colliers Coveo returned duplicate expert ${id}`);
    if (!expected.has(id)) throw new Error(`Colliers Coveo returned unexpected expert ${id}`);
    actual.set(id, result);
  }
  const missing = [...expected].filter((id) => !actual.has(id));
  if (missing.length && !allowMissing) {
    throw new Error(
      `Colliers Coveo missing ${missing.length} expert id(s): ${missing.slice(0, 5).join(", ")}`
    );
  }
  return actual;
}

export function reconcileColliersMainCoveoResults(
  entries: ColliersMainEntry[],
  results: CoveoResult[]
): Map<string, CoveoResult> {
  const expected = new Map(entries.map((entry) => [entry.id.toLowerCase(), entry]));
  const actual = new Map<string, CoveoResult>();
  for (const result of results) {
    const id = clean(result.raw?.propertyz32xid ?? result.raw?.ftitle16556 ?? result.title)?.toLowerCase();
    if (!id || !/^usa\d{5,}$/.test(id)) throw new Error("Colliers Coveo result lacks a native usa identifier");
    if (actual.has(id)) throw new Error(`Colliers Coveo returned duplicate ${id}`);
    const entry = expected.get(id);
    if (!entry) throw new Error(`Colliers Coveo returned unexpected ${id}`);
    if (!result.clickUri) throw new Error(`Colliers Coveo ${id} lacks clickUri`);
    const resultUrl = new URL(result.clickUri);
    const entryUrl = new URL(entry.url);
    if (
      !COLLIERS_COVEO_CLICK_ORIGINS.has(resultUrl.origin)
      || resultUrl.pathname.toLowerCase() !== entryUrl.pathname.toLowerCase()
    ) {
      throw new Error(`Colliers Coveo ${id} click identity does not match sitemap`);
    }
    actual.set(id, result);
  }
  const missing = [...expected.keys()].filter((id) => !actual.has(id));
  if (missing.length) throw new Error(`Colliers Coveo missing ${missing.length} sitemap id(s): ${missing.slice(0, 5).join(", ")}`);
  return actual;
}

function parseCoveoJsonList(value: unknown, field: string): any[] {
  if (Array.isArray(value)) return value;
  if (typeof value !== "string" || !value.trim()) return [];
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(`Colliers Coveo ${field} is malformed JSON`);
  }
  if (!Array.isArray(parsed)) {
    throw new Error(`Colliers Coveo ${field} is not an array`);
  }
  return parsed;
}

function parseCoveoStringList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map(clean).filter((item): item is string => Boolean(item));
  }
  if (typeof value !== "string" || !value.trim()) return [];
  try {
    const parsed = JSON.parse(value);
    if (Array.isArray(parsed)) {
      return parsed.map(clean).filter((item): item is string => Boolean(item));
    }
  } catch {
    // The live index also emits semicolon-delimited scalar lists.
  }
  return value.split(";").map(clean).filter((item): item is string => Boolean(item));
}

function textFromHtml(value: unknown): string | null {
  if (typeof value !== "string") return null;
  return clean(cheerio.load(value).text());
}

function coveoFlag(value: unknown): boolean {
  return ["1", "true", "yes", "on"].includes(String(value ?? "").trim().toLowerCase());
}

const COLLIERS_LEASE_RATE_UNIT: Record<string, string> = {
  "247f7ab813234d33a9e042c0b5f13652": "/ SF",
  "a5845526c5a64d55aff14aa673e82a22": "/ RSF",
  "5457035acd674c0eb235732dd242d93b": "/ month",
  "ca824d6179d74708a72d88b9e2fe925e": "/ acre",
  "d1e6e3f63c5b4229a67bf21c8e5c3488": "/ year",
};

const COLLIERS_SIZE_UNIT: Record<string, "sf" | "ac" | "units"> = {
  "40409737aa8c4b10b53be81940c7b2ed": "sf",
  "708623336f6542cab69b28fe1eee7322": "ac",
  "89d325e537db494cb9016e8a71d7eca3": "units",
};
const MAX_UNCORROBORATED_LISTING_ACRES = 100;

function colliersCoveoUnit(value: unknown): "sf" | "ac" | "units" | null {
  const scalar = Array.isArray(value) ? value[0] : value;
  const normalized = normalizeCoveoId(scalar);
  if (!normalized) return null;
  if (normalized === "sf" || normalized === "sqft" || normalized === "squarefeet") return "sf";
  if (normalized === "ac" || normalized === "acre" || normalized === "acres") return "ac";
  if (normalized === "unit" || normalized === "units") return "units";
  return COLLIERS_SIZE_UNIT[normalized] ?? null;
}

function colliersCoveoAcreEvidenceText(raw: Record<string, any>): string {
  const structured = [
    ...parseCoveoJsonList(raw.propertyz32xfeatures, "property features"),
    ...parseCoveoJsonList(raw.specifications, "specifications"),
  ].flatMap((value) => {
    if (typeof value === "string") return [value];
    if (!value || typeof value !== "object") return [];
    return [value.Name, value.Label, value.Value, value.Text]
      .map(clean)
      .filter((text): text is string => Boolean(text));
  });
  return [
    clean(raw.propertyz32xtitle),
    textFromHtml(raw.description),
    ...structured,
  ].filter((text): text is string => Boolean(text)).join(" ");
}

function colliersMainCoveoAcreEvidenceValues(raw: Record<string, any>): number[] {
  const evidence = colliersCoveoAcreEvidenceText(raw);
  const acrePattern = /(?:±|\+\/-|~)?\s*((?:[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)|(?:\.[0-9]+))\s*(?:±|\+\/-|~|-)?\s*(?:acres?\b|ac\b)(?!\s*units?\b)/gi;
  return [...evidence.matchAll(acrePattern)]
    .map((match) => Number(match[1].replaceAll(",", "")))
    .filter(Number.isFinite);
}

export function colliersMainCoveoAcreageIsCorroborated(
  raw: Record<string, any>,
  acres: number
): boolean {
  const tolerance = Math.max(0.001, Math.abs(acres) * 0.005);
  return colliersMainCoveoAcreEvidenceValues(raw).some(
    (observed) => Math.abs(observed - acres) <= tolerance
  );
}

export function colliersMainCoveoAcreageIsAdmissible(
  raw: Record<string, any>,
  acres: number
): boolean {
  const evidenceValues = colliersMainCoveoAcreEvidenceValues(raw);
  if (colliersMainCoveoAcreageIsCorroborated(raw, acres)) return true;
  // The current first-party index contains decimal-shift errors even below one
  // acre. If the public copy states any acreage, a mismatch is affirmative
  // evidence that the coded value is unsafe. A small coded value with no text
  // evidence remains usable; larger values require direct corroboration.
  return acres <= MAX_UNCORROBORATED_LISTING_ACRES && evidenceValues.length === 0;
}

export function colliersMainCoveoMeasurements(raw: Record<string, any>): {
  buildingSizeSqft?: number;
  lotSizeAcres?: number;
  availableSf?: number;
  minDivisibleSf?: number;
  maxDivisibleSf?: number;
  units?: number;
} {
  const result: {
    buildingSizeSqft?: number;
    lotSizeAcres?: number;
    availableSf?: number;
    minDivisibleSf?: number;
    maxDivisibleSf?: number;
    units?: number;
  } = {};
  const building = num(Number(raw.buildingz32xsiz122xe));
  const buildingUnit = colliersCoveoUnit(raw.buildingz32xsiz122xez32xunit);
  if (building && buildingUnit === "sf") result.buildingSizeSqft = building;
  if (building && buildingUnit === "units" && Number.isInteger(building)) result.units = building;

  const floorUnit = colliersCoveoUnit(raw.floorz32xareaz32xunit);
  const floorMin = num(Number(raw.fminz32xarea16556));
  const floorMax = num(Number(raw.fmaz120xz32xarea16556));
  if (floorUnit === "sf" && (floorMin || floorMax)) {
    result.minDivisibleSf = floorMin ?? floorMax ?? undefined;
    result.maxDivisibleSf = floorMax ?? floorMin ?? undefined;
    result.availableSf = floorMax ?? floorMin ?? undefined;
  } else {
    const propertySize = num(Number(raw.propertysiz122xecomputed));
    if (propertySize && colliersCoveoUnit(raw.siz122xeunitcomputed) === "sf") {
      result.availableSf = propertySize;
    }
  }

  const lotRaw = num(Number(raw.flotz32xsiz122xe16556));
  const lotUnit = colliersCoveoUnit(raw.lotz32xsiz122xez32xunit);
  const propertySize = num(Number(raw.propertysiz122xecomputed));
  const propertyUnit = colliersCoveoUnit(raw.siz122xeunitcomputed);
  const lotValue = lotRaw ?? (propertyUnit === "ac" ? propertySize : null);
  const effectiveLotUnit = lotRaw ? lotUnit : propertyUnit;
  if (lotValue && effectiveLotUnit === "sf") {
    result.lotSizeAcres = lotValue / 43_560;
  } else if (
    lotValue
    && effectiveLotUnit === "ac"
    && colliersMainCoveoAcreageIsAdmissible(raw, lotValue)
  ) {
    result.lotSizeAcres = lotValue;
  }
  return result;
}

function isPrivateOrLocalHostname(hostname: string): boolean {
  const host = hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local")) return true;
  if (host === "::1" || host === "0:0:0:0:0:0:0:1" || /^f[cd][0-9a-f:]*$/i.test(host) || /^fe[89ab][0-9a-f:]*$/i.test(host)) {
    return true;
  }
  const parts = host.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  return parts[0] === 10 || parts[0] === 127 || parts[0] === 0 ||
    (parts[0] === 169 && parts[1] === 254) ||
    (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) ||
    (parts[0] === 192 && parts[1] === 168);
}

export function colliersMainPublicUrl(value: unknown): string | null {
  const text = clean(value);
  if (!text || text.startsWith("#")) return null;
  try {
    const parsed = new URL(decodeHtmlEntities(text), COLLIERS_MAIN_HOST);
    if (!["http:", "https:"].includes(parsed.protocol)) return null;
    if (parsed.username || parsed.password || !parsed.hostname || isPrivateOrLocalHostname(parsed.hostname)) return null;
    return parsed.toString();
  } catch {
    return null;
  }
}

function uniqueUrlObjects<T extends { url: string }>(values: T[]): T[] {
  const seen = new Set<string>();
  return values.filter((value) => {
    if (seen.has(value.url)) return false;
    seen.add(value.url);
    return true;
  });
}

export function mapColliersMainCoveoListing(
  entry: ColliersMainEntry,
  result: CoveoResult,
  experts: Map<string, CoveoResult> = new Map()
): any {
  const raw = result.raw ?? {};
  const addr = parseColliersMainAddress(clean(raw.propertyz32xfullz32xaddress));
  const sale = String(raw.forz32xsale) === "1";
  const lease = String(raw.forz32xlease) === "1";
  if (!sale && !lease) throw new Error(`Colliers Coveo ${entry.id} lacks transaction flags`);
  const transactionType = sale && lease ? "Sale/Lease" : sale ? "Sale" : "Lease";
  // Coveo exposes numeric sale bounds but no reliable unit discriminator. The
  // same sale-type ID carries both apparent absolute prices and values such as
  // 1, 20, and 405. Keep the exact fields under rawPricing, but never promote
  // an unproved unit into the canonical USD columns.
  const salePrice = null;
  const leaseMin = num(Number(raw.fleasez32xpricez32xmin16556));
  const leaseMax = num(Number(raw.fleasez32xpricez32xmaz120x16556));
  const leaseRateUnit = COLLIERS_LEASE_RATE_UNIT[normalizeCoveoId(raw.leasez32xratez32xtype) ?? ""];
  const leaseRateVisible = !coveoFlag(raw.hidez32xleasez32xprice) && Boolean(leaseMin || leaseMax);
  const measurements = colliersMainCoveoMeasurements(raw);
  const photos = dedupeStrings(
    String(raw.propertyz32ximages ?? "")
      .split("|")
      .map(colliersMainPublicUrl)
      .filter((url): url is string => Boolean(url))
  );
  const brochures = uniqueUrlObjects(parseCoveoJsonList(raw.relatedz32xdocuments, "related documents").flatMap((doc) => {
    const url = colliersMainPublicUrl(doc?.DocumentLink);
    if (!url) return [];
    const title = clean(doc?.DocumentName ?? doc?.FileName);
    const classified = classifyDocument(url, title);
    return [{ name: title, url, docType: classified?.docType ?? "other" }];
  }));
  const links = uniqueUrlObjects(parseCoveoJsonList(raw.relatedz32xlinks, "related links").flatMap((link) => {
    const url = colliersMainPublicUrl(link?.Value);
    return url ? [{ url, rel: null, linkType: "other" }] : [];
  }));
  const rawExpertIds = Array.isArray(raw.relatedz32xez120xperts)
    ? raw.relatedz32xez120xperts
    : String(raw.relatedz32xez120xperts ?? "").split(";");
  const expertIds = [...new Set(rawExpertIds.map(normalizeCoveoId).filter((id): id is string => Boolean(id)))];
  const unresolvedExpertIds = expertIds.filter((id) => !experts.has(id));
  const expertNames = parseCoveoStringList(raw.relatedez120xpertsfullnamecomputed);
  type ColliersCoveoContact = {
    name: string | null;
    company: string;
    title?: string | null;
    office?: string | null;
    phone?: string | null;
    email?: string | null;
    profileUrl?: string | null;
    avatarUrl?: string | null;
    license?: string | null;
  };
  const contactsDetailed = expertIds.map<ColliersCoveoContact | null>((id, index) => {
    const expert = experts.get(id);
    if (!expert) {
      const name = expertNames[index];
      return name ? { name, company: "Colliers" } : null;
    }
    const e = expert!.raw ?? {};
    const contact = prune({
      name: clean(e.z95xname ?? e.displayname), title: clean(e.title),
      office: clean(Array.isArray(e.ez120xpertofficenamecomputed) ? e.ez120xpertofficenamecomputed[0] : e.ez120xpertofficenamecomputed),
      phone: clean(e.officez32xphone ?? e.mobilez32xphone), email: clean(e.email), company: "Colliers",
      profileUrl: colliersMainPublicUrl(e.urllink ?? expert!.clickUri),
      avatarUrl: colliersMainPublicUrl(e.profilez32xpicture),
      license: clean(e.licensez32xnumber),
    });
    if (!contact.name && !contact.email && !contact.phone && !contact.profileUrl) {
      throw new Error(`Colliers Coveo ${entry.id} resolved an unusable expert record`);
    }
    return contact;
  }).filter((contact): contact is ColliersCoveoContact => Boolean(contact));
  const facts = [
    ...parseCoveoJsonList(raw.propertyz32xfeatures, "property features"),
    ...parseCoveoJsonList(raw.specifications, "specifications"),
  ]
    .map((v) => typeof v === "string" ? v : `${v?.Name ?? ""} ${v?.Value ?? ""}`);
  const zoning = clean(facts.find((v) => /^zoning\s*:/i.test(v))?.replace(/^zoning\s*:\s*/i, ""));
  const yearBuilt = Number(facts.join(" ").match(/Year Built\s*:\s*((?:18|19|20)\d{2})/i)?.[1] ?? 0) || null;
  const observedAt = new Date().toISOString();
  const listing = prune({
    id: entry.id, name: clean(raw.propertyz32xtitle) ?? entry.id, headline: clean(raw.propertyz32xtitle),
    transactionType, assetType: clean(raw.primarypropertytype ?? raw.propertytypescomputed?.[0]),
    description: textFromHtml(raw.description), street: addr.street, city: addr.city, state: addr.state,
    postalCode: addr.postalCode, country: addr.country ?? "US", latitude: num(Number(raw.latitude)),
    longitude: num(Number(raw.longitude)), salePriceUsd: salePrice, salePriceText: salePrice ? `$${salePrice}` : null,
    // The Coveo rate type omits cadence for /SF and /RSF. Preserve the exact
    // visible values below, but do not misstate a monthly rate as annual.
    leaseRateText: null, leaseRateMin: null, leaseRateMax: null,
    sizeText: measurements.buildingSizeSqft
      ? `Building Size: ${measurements.buildingSizeSqft.toLocaleString("en-US")} SF`
      : null,
    ...measurements,
    yearBuilt, zoning, canonicalUrl: entry.url, statusBadge: clean(raw.propertyz32xstatus),
    contactsDetailed: contactsDetailed.length ? contactsDetailed : undefined,
    brokerIds: contactsDetailed.length
      ? contactsDetailed.map((contact) => brokerRef(contact)).filter((id): id is number => id !== null)
      : undefined,
    brochures, photos, links: links.length ? links : undefined, url: entry.url,
    // A property can continue to reference an expert whose profile has fallen
    // out of the public expert index. Preserve only its previously verified
    // contacts while every other child collection refreshes wholesale.
    preserveContactCollections: unresolvedExpertIds.length ? true : undefined,
    detailObservedWithContactPreservation: unresolvedExpertIds.length ? true : undefined,
    lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
    inventoryObservedAt: entry.inventoryObservedAt, detailObservedAt: observedAt,
    freshnessProvenance: { detailScope: "first_party_detail_api", generationId: refreshGenerationId(),
      method: "colliers_main_coveo", cacheDisposition: "live", sourceRevision: entry.lastmod },
    colliersMain: {
      propertyStatus: clean(raw.propertyz32xstatus),
      detailTemplate: "coveo_property_record",
      docCount: brochures.length,
      photoCount: photos.length,
      contactCount: contactsDetailed.length,
      unresolvedExpertIds,
    },
  });
  // Preserve source values exactly, including false/zero/null. The generic
  // prune helper intentionally removes false/null and therefore must not own
  // this provenance-only sub-object.
  listing.colliersMain.rawPricing = {
    hideSalePrice: raw.hidez32xsalez32xprice ?? null,
    hideLeasePrice: raw.hidez32xleasez32xprice ?? null,
    saleType: raw.salez32xtype ?? null,
    leaseType: raw.leasez32xtype ?? null,
    leaseRateType: raw.leasez32xratez32xtype ?? null,
    salePriceMin: raw.fsalez32xpricez32xmin16556 ?? null,
    salePriceMax: raw.fsalez32xpricez32xmaz120x16556 ?? null,
    leasePriceMin: raw.fleasez32xpricez32xmin16556 ?? null,
    leasePriceMax: raw.fleasez32xpricez32xmaz120x16556 ?? null,
    currency: raw.currency ?? null,
    sortPrice: raw.propertysortpricecomputed ?? null,
    visibleLeaseRateUnit: leaseRateVisible ? leaseRateUnit ?? null : null,
  };
  listing.colliersMain.rawSizing = {
    buildingSize: raw.buildingz32xsiz122xe ?? null,
    buildingSizeUnit: raw.buildingz32xsiz122xez32xunit ?? null,
    propertySize: raw.propertysiz122xecomputed ?? null,
    propertySizeUnit: raw.siz122xeunitcomputed ?? raw.siz122xez32xunit ?? null,
    lotSize: raw.flotz32xsiz122xe16556 ?? null,
    lotSizeUnit: raw.lotz32xsiz122xez32xunit ?? null,
    lotSizeSquareMeters: raw.propertylotsiz122xesqmcomputed ?? null,
    buildingSizeSquareMeters: raw.propertybuildingsiz122xesqmcomputed ?? null,
    floorAreaMin: raw.fminz32xarea16556 ?? null,
    floorAreaMax: raw.fmaz120xz32xarea16556 ?? null,
    floorAreaUnit: raw.floorz32xareaz32xunit ?? null,
  };
  return listing;
}

export async function postColliersMainBrowserBatch(
  requestBodies: string[],
  wait: Sleep = sleep
): Promise<any[]> {
  let lastError: Error | null = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    let retryable = false;
    try {
      const response = await fetch(COLLIERS_MAIN_COVEO_BATCH_URL, {
        method: "POST", headers: { "content-type": "application/json", accept: "application/json" },
        signal: AbortSignal.timeout(270_000),
        body: JSON.stringify({ bootstrapUrl: COLLIERS_MAIN_SOURCE_URL, waitAfterLoadMs: 2000, timeoutMs: 60000,
          requests: requestBodies.map((body) => ({ url: COLLIERS_MAIN_COVEO_SEARCH_URL, method: "POST",
            headers: { "content-type": "application/x-www-form-urlencoded; charset=UTF-8", accept: "application/json" }, body })) }),
      });
      const responseText = await response.text();
      if (!response.ok) {
        retryable = response.status === 429 || response.status >= 500 ||
          /(?:HTTP|bootstrapStatus)[^\d]*429/i.test(responseText);
        throw new Error(
          `Colliers browser batch transport returned HTTP ${response.status}: ${responseText.slice(0, 300)}`
        );
      }
      let payload: any;
      try { payload = JSON.parse(responseText); } catch {
        throw new Error("Colliers browser batch transport returned malformed JSON");
      }
      if (!Array.isArray(payload?.responses) || payload.responses.length !== requestBodies.length) {
        throw new Error("Colliers browser batch transport returned malformed response accounting");
      }
      const innerFailure = payload.responses.find((item: any) => item?.status !== 200 || typeof item?.body !== "string");
      if (innerFailure) {
        retryable = innerFailure.status === 0 || innerFailure.status === 429 || innerFailure.status >= 500;
        throw new Error(`Colliers Coveo inner request returned HTTP ${String(innerFailure.status)}`);
      }
      return payload.responses.map((item: any, index: number) => {
        let parsed: any;
        try { parsed = JSON.parse(item.body); } catch {
          throw new Error(`Colliers Coveo batch ${index} was not JSON`);
        }
        if (!Array.isArray(parsed?.results)) throw new Error(`Colliers Coveo batch ${index} lacks results`);
        return parsed;
      });
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error));
      // A rejected local fetch has no response status, so retry it as a bounded
      // transport failure. Deterministic validation and 4xx failures remain terminal.
      if (
        !retryable &&
        /fetch failed|ECONN|socket|timed?\s*out|was not JSON|lacks results/i.test(lastError.message)
      ) retryable = true;
      if (!retryable || attempt === 3) throw lastError;
      const delayMs = 15_000 * (2 ** (attempt - 1));
      console.error(`  colliers-main: browser batch transient failure; retrying in ${delayMs}ms (${attempt}/3)`);
      await wait(delayMs);
    }
  }
  throw lastError ?? new Error("Colliers browser batch transport returned no payload");
}

export async function colliersMainCoveoEnrichAll(entries: ColliersMainEntry[]): Promise<any[]> {
  const resultSets: CoveoResult[] = [];
  for (const group of chunks(chunks(entries, COLLIERS_MAIN_COVEO_BATCH_SIZE), COLLIERS_MAIN_COVEO_REQUESTS_PER_SESSION)) {
    const payloads = await postColliersMainBrowserBatch(group.map((batch) => colliersMainCoveoQueryBody(batch.map((e) => e.id), "property")));
    payloads.forEach((payload, index) => {
      if (payload.totalCount !== group[index].length || payload.results.length !== group[index].length) {
        throw new Error(`Colliers Coveo property batch count mismatch`);
      }
      resultSets.push(...payload.results);
    });
  }
  const reconciled = reconcileColliersMainCoveoResults(entries, resultSets);
  const expertIds = [...new Set(resultSets.flatMap((result) => {
    const value = result.raw?.relatedz32xez120xperts;
    return (Array.isArray(value) ? value : String(value ?? "").split(";"))
      .map(normalizeCoveoId)
      .filter((id): id is string => Boolean(id));
  }))];
  const expertResults: CoveoResult[] = [];
  for (const group of chunks(chunks(expertIds, COLLIERS_MAIN_COVEO_BATCH_SIZE), COLLIERS_MAIN_COVEO_REQUESTS_PER_SESSION)) {
    const payloads = await postColliersMainBrowserBatch(group.map((batch) => colliersMainCoveoQueryBody(batch, "expert")));
    payloads.forEach((payload, index) => {
      if (
        !Number.isInteger(payload.totalCount) ||
        payload.totalCount < 0 ||
        payload.totalCount > group[index].length ||
        payload.results.length !== payload.totalCount
      ) {
        throw new Error(
          `Colliers Coveo expert batch accounting mismatch: expected at most ${group[index].length}, ` +
          `provider total ${String(payload.totalCount)}, returned ${payload.results.length}; ` +
          `first ids ${group[index].slice(0, 5).join(", ")}`
        );
      }
      expertResults.push(...payload.results);
    });
  }
  const expertMap = reconcileColliersMainCoveoExperts(expertIds, expertResults, true);
  return entries.map((entry) =>
    mapColliersMainCoveoListing(entry, reconciled.get(entry.id.toLowerCase())!, expertMap)
  );
}

/** Test hook for a discovery cache that must never cross test cases. */
export function resetColliersMainSitemapCacheForTest(): void {
  colliersMainSitemapCache = null;
}

export function colliersMainDetailPassTruncated(stats: { errors: number; deferred: number }): boolean {
  return stats.errors > 0 || stats.deferred > 0;
}

export function colliersMainResultTruncated(
  stats: { errors: number; deferred: number },
  max: number,
  knownInventory: number | null
): boolean {
  return (
    colliersMainDetailPassTruncated(stats) ||
    (Number.isFinite(max) &&
      knownInventory !== null &&
      max < knownInventory)
  );
}

export function colliersMainIsChallenge(doc: ScrapedDoc): boolean {
  const httpStatus = doc.metadata?.statusCode;
  if (httpStatus === 429 || httpStatus === 503) return true;
  const title = (clean(doc.metadata?.title) ?? "").toLowerCase();
  if (/just a moment|attention required|checking your browser|cf-browser-verification/i.test(title)) {
    return true;
  }
  const head = (doc.rawHtml ?? "").slice(0, 4000);
  return /cf-chl-|challenge-platform|_cf_chl_opt|just a moment/i.test(head);
}

// Colliers detail pages are Cloudflare-protected; under sustained paging the
// site returns 429 "Just a moment..." challenge shells. Both those shells and
// exhausted local transport errors use this source-level retry budget. The
// underlying scrapeDoc retries an individual API call; this wrapper spaces
// whole detail renders and extends the shared gate after a failure so the next
// workers do not immediately recreate the burst.
export async function scrapeColliersMainDetailDoc(
  url: string,
  request: (url: string, opts: ScrapeOpts) => Promise<ScrapedDoc> = scrapeDoc,
  wait: Sleep = sleep,
  random: () => number = Math.random,
  observe?: ColliersMainDetailTelemetryObserver
): Promise<ScrapedDoc> {
  const maxAttempts = boundedInt(process.env.COLLIERS_MAIN_CHALLENGE_RETRIES, 4, 1, 8);
  const scrapeOpts = {
    ...COLLIERS_MAIN_SCRAPE_OPTS,
    ...(requireFreshDetails() ? { maxAge: 0 } : {}),
  };
  let lastError: unknown = null;
  let lastChallenged: ScrapedDoc | null = null;
  const record = (event: ColliersMainDetailTelemetry): void => {
    try {
      observe?.(event);
    } catch {
      // Metrics must never alter the fail-closed scrape path.
    }
  };
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      await acquireColliersMainDetailStart();
      const doc = await request(url, scrapeOpts);
      if (!colliersMainIsChallenge(doc)) {
        record({ kind: "attempt_success", attempt });
        return doc;
      }
      record({ kind: "challenge", attempt });
      lastChallenged = doc;
      lastError = new Error("Colliers main detail still challenged");
    } catch (err) {
      record({ kind: "transport_error", attempt });
      lastError = err;
    }
    record({
      kind: "cooldown",
      attempt,
      cooldownMs: colliersMainChallengeCooldownMs(),
    });
    coolDownColliersMainDetailStarts();
    if (attempt < maxAttempts) {
      const backoff = 4000 * attempt + Math.floor(random() * 3000);
      await wait(backoff);
    }
  }
  if (lastChallenged) return lastChallenged;
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

/**
 * Prove the local detail path can return a usable Colliers response before a
 * fresh chunk fans out over thousands of URLs. A generic health check proves
 * that the API is listening, but not that its Playwright/stealth detail path
 * is ready. This probe deliberately writes no cache rows: an unavailable
 * runtime must fail the chunk before it creates a misleading partial cache.
 *
 * An explicit 404/410 remains a valid detail result because the parser emits
 * the source's verified not-found tombstone. Challenge shells and transport
 * failures are never admitted as canary success.
 */
export async function assertColliersMainDetailRuntimeReady(
  entries: ColliersMainEntry[],
  cached: Map<string, any>,
  scrape: (url: string) => Promise<ScrapedDoc> = scrapeColliersMainDetailDoc
): Promise<void> {
  const candidates = entries
    .filter((entry) => {
      const listing = cached.get(entry.id);
      return !listing || !colliersMainCachedListingIsCurrent(entry, listing);
    })
    .slice(0, COLLIERS_MAIN_RUNTIME_CANARY_COUNT);
  if (!candidates.length) return;

  const failures: string[] = [];
  for (const entry of candidates) {
    try {
      parseColliersMainDetail(entry, await scrape(entry.url));
      console.error(`  colliers-main: detail runtime canary passed for ${entry.id}`);
      return;
    } catch (err) {
      failures.push(`${entry.id}: ${String(err).slice(0, 180)}`);
    }
  }
  throw new Error(
    `Colliers main detail runtime readiness canary failed before fanout (${failures.join("; ")})`
  );
}

export function colliersMainAbs(href: string | null | undefined): string | null {
  return colliersMainPublicUrl(href);
}

export function colliersMainIdFromUrl(url: string): string | null {
  const m = url.match(/\/(usa\d{5,})(?:[/?#]|$)/i);
  return m ? m[1].toLowerCase() : null;
}

export function extractSitemapLocs(xml: string): string[] {
  return [...xml.matchAll(/<loc>\s*([^<]+?)\s*<\/loc>/g)].map((m) => decodeHtmlEntities(m[1]).trim());
}

export async function fetchColliersMainEntries(
  raw: (url: string, opts: typeof COLLIERS_MAIN_SCRAPE_OPTS & { maxAge?: number }) => Promise<string> = scrapeRaw,
  wait: Sleep = sleep
): Promise<ColliersMainEntry[]> {
  if (colliersMainSitemapCache) return colliersMainSitemapCache;
  const scrapeOpts = {
    ...COLLIERS_MAIN_SCRAPE_OPTS,
    ...(requireFreshDetails() ? { maxAge: 0 } : {}),
  };
  let lastError: unknown = null;
  for (let attempt = 1; attempt <= COLLIERS_MAIN_SITEMAP_RETRIES; attempt++) {
    try {
      // A Cloudflare/interstitial response can be nonempty, so scrapeRaw's
      // transport retry correctly returns it but cannot prove this source's
      // sitemap contract. Retry the complete index -> child sequence instead
      // of treating a transient semantic failure as empty inventory.
      let propsSitemap = COLLIERS_MAIN_PROPERTIES_SITEMAP;
      if (!colliersMainCoveoEnabled()) {
        const indexXml = await raw(COLLIERS_MAIN_SITEMAP_INDEX, scrapeOpts);
        const childLocs = extractSitemapLocs(indexXml);
        const discovered = childLocs.find((l) => /\/en\/sitemap\?type=properties\b/i.test(l));
        if (!discovered) {
          throw new Error("Colliers main: en ?type=properties sitemap not found in sitemap index");
        }
        propsSitemap = discovered;
      }
      const propsXml = await raw(propsSitemap, scrapeOpts);
      const seen = new Set<string>();
      const entries: ColliersMainEntry[] = [];
      const inventoryObservedAt = new Date().toISOString();
      const sitemapEntries = extractSitemapUrlEntries(propsXml);
      if (!sitemapEntries.length) {
        throw new Error("Colliers main: ?type=properties sitemap had no URL rows");
      }
      for (const e of sitemapEntries) {
        const id = colliersMainIdFromUrl(e.loc);
        if (!id) {
          throw new Error(`Colliers main: sitemap URL lacks a usa identifier: ${e.loc.slice(0, 180)}`);
        }
        const parsed = new URL(e.loc);
        if (parsed.origin !== COLLIERS_MAIN_HOST || !parsed.pathname.toLowerCase().startsWith("/en/properties/")) {
          throw new Error(`Colliers main: sitemap URL is outside the canonical property scope: ${e.loc.slice(0, 180)}`);
        }
        if (seen.has(id)) {
          throw new Error(`Colliers main: sitemap returned duplicate property id ${id}`);
        }
        seen.add(id);
        entries.push({ url: e.loc, lastmod: e.lastmod, id, inventoryObservedAt });
      }
      if (!entries.length) {
        throw new Error("Colliers main: ?type=properties sitemap had no usa####### detail URLs");
      }
      console.error(`  colliers-main: sitemap exposed ${entries.length} US property detail URL(s)`);
      colliersMainSitemapCache = entries;
      return entries;
    } catch (err) {
      lastError = err;
      console.error(`  colliers-main: sitemap discovery attempt ${attempt} failed: ${err}`);
      if (attempt < COLLIERS_MAIN_SITEMAP_RETRIES) await wait(2500 * attempt);
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

// Classify transaction from the JSON-LD name first, then markdown header, then
// the URL slug. Returns a transactionType string plus a sublease flag.
export function colliersMainTransaction(
  ldName: string | null,
  markdown: string,
  url: string
): { type: "Sale" | "Lease" | "Sale/Lease"; sublease: boolean } {
  const name = (ldName ?? "").toLowerCase();
  const head = markdown.slice(0, 4000).toLowerCase();
  const slug = url.toLowerCase();
  const both = /(for sale or lease|for lease or sale|sale\/lease|sale or ground lease|for sale and lease)/;
  const sub = /sublease/;
  const lease = /(for lease|for rent|ground lease)/;
  const sale = /for sale/;
  const isSub = sub.test(name) || sub.test(head) || /sublease/.test(slug);
  if (both.test(name) || both.test(head) || /sale-or-lease|lease-or-sale/.test(slug)) {
    return { type: "Sale/Lease", sublease: isSub };
  }
  // Prefer the JSON-LD name signal (most reliable), then markdown, then slug.
  for (const hay of [name, head, slug]) {
    const hasSale = sale.test(hay) || /for-sale/.test(hay);
    const hasLease = lease.test(hay) || /for-lease|for-rent/.test(hay);
    if (hasSale && !hasLease) return { type: "Sale", sublease: isSub };
    if (hasLease && !hasSale) return { type: isSub ? "Lease" : "Lease", sublease: isSub };
  }
  if (isSub) return { type: "Lease", sublease: true };
  // No clear signal; default to Sale (Colliers leans investment-sale) but keep
  // the raw name in colliersMain for audit.
  return { type: "Sale", sublease: false };
}

export function parseColliersMainAddress(addr: string | null): {
  street: string | null;
  city: string | null;
  state: string | null;
  postalCode: string | null;
  country: string | null;
} {
  const out = {
    street: null as string | null,
    city: null as string | null,
    state: null as string | null,
    postalCode: null as string | null,
    country: null as string | null,
  };
  if (!addr) return out;
  let s = addr.trim();
  if (/,\s*(USA|United States|US)\s*$/i.test(s)) {
    out.country = "US";
    s = s.replace(/,\s*(USA|United States|US)\s*$/i, "").trim();
  } else if (/,\s*Canada\s*$/i.test(s)) {
    out.country = "CA";
    s = s.replace(/,\s*Canada\s*$/i, "").trim();
  }
  const splitHead = (head: string) => {
    const hp = head
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean);
    if (hp.length >= 2) {
      out.city = hp.pop()!;
      out.street = hp.join(", ");
    } else if (hp.length === 1) {
      out.city = hp[0];
    }
  };
  const withZip = s.match(/^(.*?),\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\s*$/);
  if (withZip) {
    out.state = withZip[2].toUpperCase();
    out.postalCode = withZip[3];
    splitHead(withZip[1].trim());
    return out;
  }
  const noZip = s.match(/^(.*?),\s*([A-Za-z]{2})\s*$/);
  if (noZip) {
    out.state = noZip[2].toUpperCase();
    splitHead(noZip[1].trim());
    return out;
  }
  // Fallback: treat the last comma segment as city, the rest as street.
  splitHead(s);
  return out;
}

export function colliersMainJsonLd(rawHtml: string): any | null {
  for (const m of rawHtml.matchAll(/<script[^>]*application\/ld\+json[^>]*>([\s\S]*?)<\/script>/g)) {
    try {
      const obj = JSON.parse(m[1].trim());
      const arr = Array.isArray(obj) ? obj : [obj];
      const found = arr.find((o: any) => o && o["@type"] === "RealEstateListing");
      if (found) return found;
    } catch {
      // skip malformed block
    }
  }
  return null;
}

type ColliersMainLightBoxDetail = {
  name: string;
  address: string;
  propertyType: string | null;
};

/**
 * A small, known LightBox-hosted subset of live Colliers listings does not
 * include the standard RealEstateListing JSON-LD block. Admit it only when
 * three independent, first-party template signals agree: the official title,
 * a nonempty H1, and a parseable address block. This is intentionally narrower
 * than accepting arbitrary HTTP-200 pages without JSON-LD.
 */
export function colliersMainLightBoxDetail(
  rawHtml: string,
  title: string
): ColliersMainLightBoxDetail | null {
  if (!/\|\s*Colliers\s*\|\s*Powered by LightBox\s*$/i.test(title)) return null;
  const $ = cheerio.load(rawHtml);
  const name = clean($("h1").first().text());
  const addressBlock = clean($(".address").first().text());
  if (!name || !addressBlock) return null;
  const [address, propertyType] = addressBlock.split("|", 2).map((value) => clean(value));
  const parsedAddress = parseColliersMainAddress(address ?? null);
  if (!address || (!parsedAddress.street && !parsedAddress.city && !parsedAddress.postalCode)) return null;
  return { name, address, propertyType: propertyType ?? null };
}

export function parseColliersMainDetail(entry: ColliersMainEntry, doc: ScrapedDoc): any {
  const raw = doc.rawHtml ?? "";
  const md = doc.markdown ?? "";
  const httpStatus = doc.metadata?.statusCode;
  const title = clean(doc.metadata?.title) ?? "";
  if (requireFreshDetails()) {
    if (httpStatus === 404 || httpStatus === 410) {
      return {
        id: entry.id,
        url: entry.url,
        skip: "not_found",
        lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
      };
    }
    if (colliersMainIsChallenge(doc)) {
      throw new Error(
        `Colliers main detail still challenged (status ${httpStatus ?? "?"}, title "${title.slice(0, 60)}")`
      );
    }
  }
  const ld = colliersMainJsonLd(raw);
  const lightBox = ld ? null : colliersMainLightBoxDetail(raw, title);
  if (!ld && !lightBox) {
    // Live listings always carry a RealEstateListing JSON-LD block. Pages
    // without one fall into three cases, handled distinctly:
    if (requireFreshDetails()) {
      // Strict refreshes may cache only transport-proven 404/410 tombstones.
      // Unknown 200 templates, consent pages, and newly shaped challenge shells
      // remain retryable so they cannot silently remove a live sitemap listing.
      throw new Error(
        `Colliers main HTTP ${httpStatus ?? "?"} detail lacks validated RealEstateListing JSON-LD`
      );
    }
    const notFound =
      /property not found|page not found|410 gone/i.test(title) ||
      /Property Not Found/i.test(md.slice(0, 3000)) ||
      httpStatus === 404 ||
      httpStatus === 410;
    if (notFound) {
      // 1) Expired/removed listing (the sitemap lags). Tombstone so we neither
      //    re-fetch it nor emit it.
      return {
        id: entry.id,
        url: entry.url,
        skip: "not_found",
        lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
      };
    }
    if (colliersMainIsChallenge(doc)) {
      // 2) Still a Cloudflare challenge after the retry wrapper exhausted its
      //    attempts. Throw so it is retried (un-cached) on the next pass.
      throw new Error(
        `Colliers main detail still challenged (status ${httpStatus ?? "?"}, title "${title.slice(0, 60)}")`
      );
    }
    // 3) A real 200 page that lacks the standard RealEstateListing JSON-LD
    //    (rare alternate template, e.g. "Powered by LightBox"). Permanent, so
    //    tombstone to avoid re-fetching it every run. ~0.1% in sampling.
    return {
      id: entry.id,
      url: entry.url,
      skip: "no_structured_data",
      lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
    };
  }
  const ldName: string | null = clean(ld?.name) ?? lightBox?.name ?? null;

  // ldName: "Office For sale — 11701 I-30, Little Rock, AR 72209, USA | United States | Colliers"
  let typeWord: string | null = lightBox?.propertyType ?? null;
  let addrStr: string | null = lightBox?.address ?? null;
  if (ld && ldName) {
    const beforePipe = ldName.split("|")[0].trim();
    const dash = beforePipe.split(/\s[—–-]\s/);
    if (dash.length >= 2) {
      typeWord = clean(
        dash[0].replace(/\bfor\s+(sale or lease|lease or sale|sale|lease|sublease|rent).*$/i, "").trim()
      );
      addrStr = dash.slice(1).join(" - ").trim();
    } else {
      typeWord = clean(beforePipe);
    }
  }
  const addr = parseColliersMainAddress(addrStr);
  const tx = colliersMainTransaction(ldName, md, entry.url);

  const priceMatch = md.match(/\$\s?[\d,]+(?:\.\d+)?\s*(?:USD)?/);
  const salePriceText = tx.type === "Lease" ? null : priceMatch ? clean(priceMatch[0]) : null;
  let leaseRateText: string | null = null;
  if (tx.type !== "Sale") {
    const lr = md.match(/\$[\d,.]+\s*(?:\/|per\s*)\s*(?:SF|sq\.?\s*ft)[^\n]{0,24}/i);
    leaseRateText = lr ? clean(lr[0]) : null;
  }

  const bsf = md.match(/Building Size:\s*([\d,]+)\s*SF/i)?.[1];
  const land = md.match(/Land Area:\s*([\d.,]+)\s*ac/i)?.[1];
  const ptype =
    clean(md.match(/\*\*Property Types?\*\*\s*([A-Za-z0-9 ,/&'-]+)/i)?.[1]) ??
    lightBox?.propertyType ??
    clean(ld?.about?.category) ??
    typeWord;
  const status = clean(md.match(/\*\*Property Status\*\*\s*([A-Za-z ,/-]+)/i)?.[1]);

  const coord =
    raw.match(/[?&]q=(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)/) ??
    md.match(/maps\?q=(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)/);
  const lat = coord ? num(Number(coord[1])) : null;
  const lng = coord ? num(Number(coord[2])) : null;

  const photos = dedupeStrings(
    [...raw.matchAll(/https:\/\/listingsprod\.blob\.core\.windows\.net\/ourlistings-[a-z]+\/[^\s"'<>)]+/g)].map(
      (m) => m[0]
    )
  ).filter((u) => !/\.pdf(\?|$)/i.test(u));

  const docs: Array<{ name: string | null; url: string }> = [];
  const seenDocs = new Set<string>();
  for (const m of md.matchAll(/\[([^\]]+)\]\((https:\/\/listingsprod\.blob\.core\.windows\.net\/[^\)]+)\)/g)) {
    const name = clean(m[1]);
    const u = clean(m[2]);
    if (!u || seenDocs.has(u)) continue;
    const looksDoc =
      /\.pdf(\?|$)/i.test(u) ||
      /\.(pdf|docx?|xlsx?|zip|pptx?)$/i.test(name ?? "") ||
      /\b(pib|brochure|flyer|om|offering|memorandum|document|package|marketing|deck|teaser)\b/i.test(name ?? "");
    if (looksDoc) {
      seenDocs.add(u);
      docs.push({ name, url: u });
    }
  }

  const $ = cheerio.load(raw);
  const contactsDetailed: any[] = [];
  const brokerIds: number[] = [];
  const seenContacts = new Set<string>();
  $(".expert-card").each((_, el) => {
    const card = $(el);
    const name = clean(card.find(".expert-card__name").first().text());
    const profileUrl = colliersMainAbs(
      card.find(".expert-card__name a, .expert-card__image a").first().attr("href")
    );
    const title = clean(card.find(".expert-card__title").first().text());
    const office = clean(card.find(".expert-card__office").first().text());
    const phone = clean(
      card
        .find('.expert-card__phone a[href^="tel:"]')
        .first()
        .attr("href")
        ?.replace(/^tel:/i, "")
    );
    const avatarUrl = clean(card.find(".expert-card__image img").first().attr("src"));
    const key = profileUrl ?? name ?? phone;
    if (!key || seenContacts.has(key)) return;
    if (!name && !phone && !profileUrl) return;
    seenContacts.add(key);
    contactsDetailed.push({ name, title, office, phone, company: "Colliers", profileUrl, avatarUrl });
    const id = brokerRef({ name, phone, office, avatarUrl, company: "Colliers" });
    if (id !== null) brokerIds.push(id);
  });

  const h1 = clean($("h1").first().text());
  const name =
    (addr.street ? `${addr.street}${addr.city ? ", " + addr.city : ""}` : null) ?? h1 ?? ldName ?? entry.url;
  const description = clean($('meta[name="description"]').attr("content"));

  // Stranded structured fields the markdown exposes but the row dropped: year
  // built and zoning are lifted onto existing cre_listings columns. Only set
  // when clearly present (regex anchored to Colliers' labeled spec lines).
  const yearBuiltText = md.match(/Year Built:\s*((?:18|19|20)\d{2})/i)?.[1];
  const zoning = clean(md.match(/\*\*Zoning\*\*\s*([A-Za-z0-9 ,/&'.-]+)/i)?.[1] ?? md.match(/Zoning:\s*([^\n|]+)/i)?.[1]);

  // Capture-everything: run the pure harvester over the rendered detail doc to
  // extract video/tour media, outbound links, and ADDITIONAL classified
  // documents the brochure regex missed. The existing brochures channel is left
  // untouched (it already carries titles + the default brochure docType into
  // ingest), so it is NOT re-passed here to avoid duplicate doc rows. media/
  // links/documents/markdown attach ADDITIVELY; the curated CDN photo set
  // (high-precision listingsprod.blob regex) is kept as-is and NOT replaced by
  // the raw page gallery, which would pull in header/footer logos.
  const harvested = harvestDetail(doc, { baseUrl: entry.url });

  // Phase-2 scalar fields.
  // canonicalUrl: the live detail URL (dual-mode COALESCE backfill is separate).
  const canonicalUrl = entry.url;
  // statusBadge: the markdown-extracted property status token routes to the
  // existing OPT-IN activation gate; never written to status directly.
  const statusBadge = status ?? null;
  // leaseRateType derived via the shared parser (low yield; ~6.4% carry an explicit token).
  const lr = parseLeaseRate(leaseRateText);

  return prune({
    // Bare usa####### id; cre_ingest folds it into the colliers brokerage with
    // the configured "main:" prefix (mirrors the cbre-dealflow pattern).
    id: entry.id,
    name,
    headline: h1,
    transactionType: tx.type,
    assetType: ptype,
    description,
    street: addr.street,
    city: addr.city,
    state: addr.state,
    postalCode: addr.postalCode,
    country: addr.country ?? "US",
    latitude: lat,
    longitude: lng,
    salePriceUsd: salePriceText ? moneyToNumber(salePriceText) : null,
    salePriceText,
    leaseRateText,
    sizeText: clean(md.match(/Building Size:[^\n|]+/i)?.[0]),
    buildingSizeSqft: bsf ? num(Number(bsf.replace(/,/g, ""))) : null,
    lotSizeAcres: land ? num(Number(land.replace(/,/g, ""))) : null,
    yearBuilt: yearBuiltText ? num(Number(yearBuiltText)) : null,
    zoning,
    // Phase-2 camelCase scalar fields (consumed by cre_ingest.py to_row).
    canonicalUrl,
    statusBadge,
    leaseRateType: lr.type ?? null,
    leaseRateMin: lr.min ?? null,
    leaseRateMax: lr.max ?? null,
    brokerIds: brokerIds.length ? brokerIds : undefined,
    contactsDetailed: contactsDetailed.length ? contactsDetailed : undefined,
    brochures: docs,
    photos,
    documents: harvested.documents.length ? harvested.documents : undefined,
    media: harvested.media.length ? harvested.media : undefined,
    links: harvested.links.length ? harvested.links : undefined,
    markdown: md || undefined,
    url: entry.url,
    lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
    colliersMain: {
      propertyStatus: status,
      detailTemplate: lightBox ? "lightbox" : "real_estate_listing_json_ld",
      sublease: tx.sublease,
      jsonLdName: ldName,
      docCount: docs.length,
      photoCount: photos.length,
      contactCount: contactsDetailed.length,
      mediaCount: harvested.media.length,
      linkCount: harvested.links.length,
    },
  });
}

export function colliersMainDetailCachePath(): string {
  // A run-specific override lets a freshness sweep start from an empty cache
  // while remaining resumable across bounded worker processes.
  return process.env.COLLIERS_MAIN_DETAIL_CACHE_PATH ?? "out/cache/colliers-main/detail-cache.jsonl";
}

export function readColliersMainCache(path: string): Map<string, any> {
  const cached = new Map<string, any>();
  if (!existsSync(path)) return cached;
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const listing = JSON.parse(line);
      if (listing?.detailError) continue;
      const id = clean(listing?.id);
      if (id) cached.set(id.replace(/^main:/, ""), listing);
    } catch {
      // Ignore a partial final line from an interrupted prior run.
    }
  }
  return cached;
}

export function appendColliersMainCache(path: string, listing: any): void {
  if (listing?.detailError) return;
  mkdirSync(dirname(path), { recursive: true });
  appendFileSync(path, `${JSON.stringify(listing)}\n`);
}

export function colliersMainCachedListingIsCurrent(entry: ColliersMainEntry, listing: any): boolean {
  if (!generationMatches(listing?.freshnessProvenance?.generationId)) return false;
  if (
    requireFreshDetails() &&
    listing?.skip &&
    listing.skip !== "not_found"
  ) {
    return false;
  }
  const sourceRevision = clean(entry.lastmod);
  const cachedRevision = clean(listing?.freshnessProvenance?.sourceRevision);
  // When the source publishes lastmod, it is the admission boundary for cache
  // reuse. Preserve and compare the exact revision rather than a date prefix:
  // Colliers can revise a listing more than once in one day, and a legacy
  // lastUpdated-only cache cannot prove that it saw the current source revision.
  return sourceRevision ? cachedRevision === sourceRevision : true;
}

// Enrich the full sitemap once (memoized across the sale and lease passes and
// backed by a durable JSONL cache), then srcColliersMain filters per pass.
export async function colliersMainEnrichAll(max: number): Promise<any[]> {
  if (colliersMainEnrichedMemo) return colliersMainEnrichedMemo;
  const entries = await fetchColliersMainEntries();
  const want = max && max > 0 ? Math.min(max, entries.length) : entries.length;
  const selected = entries.slice(0, want);
  if (colliersMainCoveoEnabled()) {
    const listings = await colliersMainCoveoEnrichAll(selected);
    colliersMainEnrichedStats = { errors: 0, deferred: 0 };
    colliersMainEnrichedMemo = listings;
    return listings;
  }
  const cachePath = colliersMainDetailCachePath();
  const cached = readColliersMainCache(cachePath);
  if (cached.size) {
    console.error(`  colliers-main: loaded ${cached.size} cached detail row(s) from ${cachePath}`);
  }
  await assertColliersMainDetailRuntimeReady(entries, cached);
  // Per-run cap on NEW detail fetches. Each detail render leaks ~0.8 MB in the
  // fetch/SDK layer, so an unbounded ~15.9k-URL run exhausts the V8 heap. With a
  // cap the process exits (freeing everything) before OOM; the durable cache
  // lets the run_colliers_main_full.sh driver resume until every URL is cached,
  // then a final cache-only pass assembles the artifact with zero fetches.
  // 0 = unlimited. Deferred URLs are not cached, so a later run retries them.
  const fetchCap = boundedInt(process.env.COLLIERS_MAIN_MAX_FETCHES_PER_RUN, 0, 0, 1_000_000);
  let fetchBudget = fetchCap > 0 ? fetchCap : Infinity;
  let done = 0;
  let fromCache = 0;
  let fetched = 0;
  let errors = 0;
  let deferred = 0;
  const listings = await pmap(selected, COLLIERS_MAIN_DETAIL_CONCURRENCY, async (entry) => {
    let listing = cached.get(entry.id);
    if (listing && !colliersMainCachedListingIsCurrent(entry, listing)) {
      listing = undefined;
    }
    if (listing) {
      const validatedAt = entry.inventoryObservedAt ?? new Date().toISOString();
      listing = {
        ...listing,
        inventoryObservedAt: validatedAt,
        detailValidatedAt: validatedAt,
        freshnessProvenance: {
          ...(listing.freshnessProvenance ?? {}),
          detailScope: "detail_page",
          generationId:
            listing.freshnessProvenance?.generationId ?? refreshGenerationId(),
          method: "colliers_main_detail",
          cacheDisposition: "source_revision_cache",
          sourceRevision: entry.lastmod,
          validatedAt,
        },
      };
      fromCache++;
    } else if (fetchBudget <= 0) {
      deferred++;
      done++;
      return null; // defer to a later run; not cached, so it is retried then
    } else {
      fetchBudget--;
      try {
        const docDoc = await scrapeColliersMainDetailDoc(entry.url);
        const observed = detailObservation("colliers_main_detail", "live", new Date().toISOString(), {
          sourceRevision: entry.lastmod,
        });
        listing = {
          ...parseColliersMainDetail(entry, docDoc),
          inventoryObservedAt: entry.inventoryObservedAt,
          detailObservedAt: observed.observedAt,
          freshnessProvenance: {
            detailScope: "detail_page",
            generationId: observed.generationId,
            method: observed.method,
            cacheDisposition: observed.cacheDisposition,
            sourceRevision: observed.sourceRevision,
          },
        };
        fetched++;
      } catch (err) {
        console.error(`  colliers-main: detail failed for ${entry.url}: ${err}`);
        listing = prune({
          id: entry.id,
          url: entry.url,
          inventoryObservedAt: entry.inventoryObservedAt,
          transactionType: null,
          detailError: String(err),
          lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
        });
        errors++;
      }
      appendColliersMainCache(cachePath, listing);
    }
    done++;
    if (done % 100 === 0 || done === selected.length) {
      console.error(
        `  colliers-main: enriched ${done}/${selected.length} (cache ${fromCache}, fetched ${fetched}, errors ${errors}, deferred ${deferred})`
      );
    }
    return listing;
  });
  const result = listings.filter(Boolean);
  colliersMainEnrichedStats = { errors, deferred };
  if (deferred > 0) {
    console.error(
      `  colliers-main: ${deferred} URL(s) deferred under fetch cap ${fetchCap}; re-run to continue (${result.length} ready, ${fetched} newly fetched this run)`
    );
  }
  colliersMainEnrichedMemo = result;
  return result;
}

export async function srcColliersMain(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  if (monitor) {
    // Monitor mode: cheap sitemap enumeration only (url + lastmod), no detail
    // render. Emit on the sale pass only so a URL is not duplicated across both
    // transactionMode passes; Sale/Lease classification is detail-only and is
    // deferred to the downstream render of new/changed listings.
    if (tx === "lease") {
      return {
        company: "Colliers",
        sourceUrl: COLLIERS_MAIN_SOURCE_URL,
        method:
          "Public colliers.com XML sitemap enumeration (monitor mode; emitted on the sale pass only to avoid duplicate transactionMode rows)",
        totalAvailable: colliersMainSitemapCache ? colliersMainSitemapCache.length : null,
        listings: [],
        note: "Monitor mode: colliers-main sitemap entries are emitted on the sale pass only; lease pass is intentionally empty.",
      };
    }
    const entries = await fetchColliersMainEntries();
    const want = max && max > 0 ? Math.min(max, entries.length) : entries.length;
    const listings = entries.slice(0, want).map((entry) => ({
      id: entry.id,
      url: entry.url,
      lastUpdated: entry.lastmod ? entry.lastmod.slice(0, 10) : null,
      inventoryObservedAt: entry.inventoryObservedAt,
      preserveChildCollections: true,
      freshnessProvenance: {
        detailScope: "inventory_only",
        generationId: refreshGenerationId(),
        method: "colliers_main_sitemap",
        cacheDisposition: "live",
      },
    }));
    return {
      company: "Colliers",
      sourceUrl: COLLIERS_MAIN_SOURCE_URL,
      method:
        "Public colliers.com XML sitemap enumeration (/sitemap -> en ?type=properties): url + lastmod only (monitor mode; detail render skipped)",
      totalAvailable: entries.length,
      listings,
      truncated: colliersMainResultTruncated(
        { errors: 0, deferred: 0 },
        max,
        entries.length
      ),
      note: "Monitor mode: sitemap url + lastmod only (id matches the full-path main: external id). Status, price, and Sale/Lease classification are detail-only and deferred to the downstream render of new/changed listings.",
    };
  }
  const all = await colliersMainEnrichAll(max);
  const ok = all.filter((l) => l && l.url && !l.detailError && !l.skip);
  const notFound = all.filter((l) => l?.skip === "not_found").length;
  const noData = all.filter((l) => l?.skip === "no_structured_data").length;
  const errored = all.filter((l) => l?.detailError).length;
  const wantSale = (l: any) => l.transactionType === "Sale" || l.transactionType === "Sale/Lease";
  const wantLease = (l: any) => l.transactionType === "Lease" || l.transactionType === "Sale/Lease";
  const listings = ok.filter(tx === "sale" ? wantSale : wantLease);
  return {
    company: "Colliers",
    sourceUrl: COLLIERS_MAIN_SOURCE_URL,
    method: colliersMainCoveoEnabled()
      ? "Public colliers.com XML sitemap discovery exactly reconciled to same-origin first-party Coveo property and expert records through the local browser sidecar"
      : "Public colliers.com XML sitemap discovery (/sitemap -> en ?type=properties) plus per-listing detail render through local Firecrawl; RealEstateListing JSON-LD + markdown parse",
    totalAvailable: colliersMainSitemapCache ? colliersMainSitemapCache.length : null,
    listings,
    truncated: colliersMainResultTruncated(
      colliersMainEnrichedStats,
      max,
      colliersMainSitemapCache ? colliersMainSitemapCache.length : null
    ),
    note:
      `Main colliers.com folded into the colliers brokerage as colliers-main with main: id prefix; SalesTracker rows untouched. ` +
      `${ok.length} live detail-enriched listing(s) of ${all.length} sitemap URL(s) scanned, ${notFound} expired/not-found and ${noData} no-structured-data (tombstoned), ${errored} detail error(s). ` +
      `Sale pass returns Sale + Sale/Lease; lease pass returns Lease + Sale/Lease. ` +
      (colliersMainCoveoEnabled()
        ? "Documents, images, and contacts come from public first-party Coveo records; no authentication, consent token, visitor token, or gated document path is used."
        : "Documents and images are URL-only; no Coveo POST, auth, or gated document path is used."),
  };
}
