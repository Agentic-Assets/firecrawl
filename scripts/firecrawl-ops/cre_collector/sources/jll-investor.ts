// sources/jll-investor.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { brokerRef, brokers } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { harvestDetail } from "../lib/harvest.js";
import { decodeHtmlEntities, dedupeStrings, stripHtmlText, titleFromFilename } from "../lib/html.js";
import { scrapeJson, scrapeRaw } from "../lib/scrape.js";
import { DocItem, MediaItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { boundedInt, clean, num, pmap, prune } from "../lib/util.js";
import {
  detailObservation,
  requireFreshDetails,
} from "../lib/freshness.js";


// --- JLL Investor Center: rendered page (sale-only by nature) ---

export const JLL_INVESTOR_HOST = "https://invest.jll.com";
export const JLL_INVESTOR_SEARCH_URL =
  "https://invest.jll.com/us/en/property-search?filter=%7B%22location%22%3A%5B%22United%20States%22%5D%7D";
export const JLL_INVESTOR_HOME_URL = `${JLL_INVESTOR_HOST}/us/en`;
export const JLL_INVESTOR_SITEMAP_INDEX_URL = `${JLL_INVESTOR_HOST}/sitemap_index.xml`;
export const JLL_INVESTOR_US_SITEMAP_URL = `${JLL_INVESTOR_HOST}/us/sitemap-us.xml`;
export const JLL_INVESTOR_DETAIL_CONCURRENCY = boundedInt(
  process.env.JLL_INVESTOR_DETAIL_CONCURRENCY,
  Math.min(CONCURRENCY, 4),
  1,
  8
);
export const JLL_INVESTOR_DETAIL_WAIT_MS = boundedInt(process.env.JLL_INVESTOR_DETAIL_WAIT_MS, 1000, 0, 30000);
// A single unresponsive public JSON detail must not hold the all-source
// collector indefinitely. The worker records a detailError and retains the
// sitemap row, so the strict source gate fails visibly without discarding the
// candidate or its previously stored child data.
export const JLL_INVESTOR_DETAIL_TIMEOUT_MS = boundedInt(
  process.env.JLL_INVESTOR_DETAIL_TIMEOUT_MS,
  30000,
  10000,
  120000
);
export const JLL_INVESTOR_SITEMAP_SCAN_LIMIT = boundedInt(
  process.env.JLL_INVESTOR_SITEMAP_SCAN_LIMIT,
  0,
  0,
  10000
);
export const JLL_INVESTOR_SEARCH_PAGE_CONCURRENCY = boundedInt(
  process.env.JLL_INVESTOR_SEARCH_PAGE_CONCURRENCY,
  Math.min(CONCURRENCY, 2),
  1,
  4
);
export const JLL_INVESTOR_SEARCH_PAGE_SIZE = 50;
export const JLL_INVESTOR_SEARCH_MAX_PASSES = 3;

const JLL_INVESTOR_ISO_ALPHA_2_CODES = new Set(
  "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW".split(" ")
);
const JLL_INVESTOR_COUNTRY_DISPLAY_NAMES = new Intl.DisplayNames(["en"], {
  type: "region",
});

function jllInvestorCountryNameKey(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .replace(/[^a-z0-9]+/gi, " ")
    .trim()
    .toLowerCase();
}

const JLL_INVESTOR_COUNTRY_NAME_TO_CODE = new Map<string, string>(
  [...JLL_INVESTOR_ISO_ALPHA_2_CODES].flatMap((code) => {
    const displayName = JLL_INVESTOR_COUNTRY_DISPLAY_NAMES.of(code);
    return displayName && displayName !== code
      ? [[jllInvestorCountryNameKey(displayName), code] as const]
      : [];
  })
);
JLL_INVESTOR_COUNTRY_NAME_TO_CODE.set("united states of america", "US");
const JLL_INVESTOR_US_TERRITORY_CODES = new Set([
  "AS",
  "GU",
  "MP",
  "PR",
  "UM",
  "VI",
]);

function jllInvestorCountryCode(value: unknown): string | null {
  const country = clean(value);
  if (!country) return null;
  if (/^USA$/i.test(country)) return "US";
  const upper = country.toUpperCase();
  return JLL_INVESTOR_ISO_ALPHA_2_CODES.has(upper)
    ? upper
    : JLL_INVESTOR_COUNTRY_NAME_TO_CODE.get(
        jllInvestorCountryNameKey(country)
      ) ?? null;
}

export function jllInvestorNextData(rawHtml: string): any | null {
  const $ = cheerio.load(rawHtml);
  const text = $("#__NEXT_DATA__").first().text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export function jllInvestorBuildId(rawHtml: string): string | null {
  const buildId = clean(jllInvestorNextData(rawHtml)?.buildId);
  return buildId && /^[A-Za-z0-9_-]+$/.test(buildId) ? buildId : null;
}

export function jllInvestorStructuredListing(payload: any): any | null {
  const listing = payload?.pageProps?.initialState?.pdp?.listing;
  return listing && typeof listing === "object" ? listing : null;
}

export function jllInvestorStructuredNotFound(payload: any): boolean {
  return (
    !jllInvestorStructuredListing(payload) &&
    payload?.pageProps?.error?.statusCode === 404
  );
}

export function jllInvestorPublicPageNotFound(
  rawHtml: string,
  expectedBuildId: string
): boolean {
  const next = jllInvestorNextData(rawHtml);
  const pageProps = next?.props?.pageProps;
  const listing = pageProps?.initialState?.pdp?.listing;
  return (
    next?.buildId === expectedBuildId &&
    !(listing && typeof listing === "object") &&
    pageProps?.error?.statusCode === 404
  );
}

export function jllInvestorDetailRoute(
  buildId: string,
  publicUrl: string
): { alias: string; url: string } {
  if (!/^[A-Za-z0-9_-]+$/.test(buildId)) {
    throw new Error("invalid JLL Investor Next.js build id");
  }
  let parsed: URL;
  try {
    parsed = new URL(publicUrl);
  } catch {
    throw new Error("invalid JLL Investor detail URL");
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.hostname.toLowerCase() !== "invest.jll.com" ||
    parsed.port ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("unsafe JLL Investor detail URL");
  }
  const match = parsed.pathname.match(
    /^\/us\/en\/listings\/([^/]+)\/([^/]+)\/?$/
  );
  if (!match) throw new Error("unsupported JLL Investor detail path");
  const decodeSegment = (value: string): string => {
    let decoded: string;
    try {
      decoded = decodeURIComponent(value);
    } catch {
      throw new Error("invalid JLL Investor detail path encoding");
    }
    if (
      !decoded ||
      decoded === "." ||
      decoded === ".." ||
      decoded.includes("/") ||
      decoded.includes("\\") ||
      !/^[A-Za-z0-9][A-Za-z0-9._~-]*$/.test(decoded)
    ) {
      throw new Error("unsafe JLL Investor detail path segment");
    }
    return decoded;
  };
  const asset = decodeSegment(match[1]!);
  const slug = decodeSegment(match[2]!);
  const alias = `${asset}/${slug}`;
  const route = [
    JLL_INVESTOR_HOST,
    "_next",
    "data",
    encodeURIComponent(buildId),
    "us",
    "en",
    "listings",
    encodeURIComponent(asset),
    `${encodeURIComponent(slug)}.json`,
  ].join("/");
  const query = new URLSearchParams({
    region: "us",
    locale: "en",
    asset,
    alias: slug,
  });
  return { alias, url: `${route}?${query.toString()}` };
}

export function jllInvestorDetailCountryClassification(
  listing: any
): "us" | "non_us" | "unknown" {
  const signals: Array<"us" | "non_us"> = [];
  const explicit = jllInvestorCountryClassification(listing?.country);
  if (explicit !== "unknown") signals.push(explicit);
  const fullLocation = clean(listing?.fullLocation);
  const parts = fullLocation?.split(",").map((part) => part.trim()) ?? [];
  const region = parts.at(-1);
  const countryCode = parts.at(-2);
  if (region === "EMEA" || region === "APAC") signals.push("non_us");
  // JLL emits a two-letter country token immediately before its exact region
  // suffix. Require both pieces so a state-only location such as
  // "Los Angeles, CA" cannot be mistaken for Canada.
  if (
    /^(?:Americas|EMEA|APAC)$/.test(region ?? "") &&
    /^[A-Z]{2}$/.test(countryCode ?? "")
  ) {
    const locationClassification =
      jllInvestorCountryClassification(countryCode);
    if (locationClassification !== "unknown") {
      signals.push(locationClassification);
    }
  }

  // Portfolio records often omit the parent country and use a generic parent
  // location such as "Various locations", while every child property carries
  // an explicit provider country in subMarketCountry. Admit that evidence only
  // when the portfolio is non-empty, every child has an explicit country, and
  // every child agrees. Missing or mixed child evidence remains unknown so a
  // multinational portfolio can never be silently assigned to the US feed.
  const portfolio = Array.isArray(listing?.portfolio) ? listing.portfolio : [];
  if (portfolio.length > 0) {
    const classifications: Array<"us" | "non_us" | "unknown"> = portfolio.map(
      (row: any) => {
        const countryFields = [row?.subMarketCountry, row?.country]
          .map(clean)
          .filter((value): value is string => Boolean(value));
        if (countryFields.length === 0) return "unknown";
        const fieldClassifications = countryFields.map(
          jllInvestorCountryClassification
        );
        if (fieldClassifications.includes("non_us")) return "non_us";
        return fieldClassifications.every((classification) => classification === "us")
          ? "us"
          : "unknown";
      }
    );
    if (classifications.includes("non_us")) signals.push("non_us");
    else if (classifications.every((classification) => classification === "us")) {
      signals.push("us");
    }
  }
  if (signals.includes("non_us")) return "non_us";
  return signals.includes("us") ? "us" : "unknown";
}

export function jllInvestorUrlFromAlias(alias: string | null): string | null {
  const cleaned = clean(alias);
  if (!cleaned) return null;
  if (/^https?:\/\//i.test(cleaned)) return cleaned;
  const path = cleaned.startsWith("/us/en/listings/")
    ? cleaned
    : `/us/en/listings/${cleaned.replace(/^\/+/, "")}`;
  return `${JLL_INVESTOR_HOST}${path}`;
}

export function jllInvestorSitemapUrls(rawHtml: string): string[] {
  const decoded = decodeHtmlEntities(rawHtml);
  const matches = decoded.match(/https:\/\/invest\.jll\.com\/[a-z]{2}\/sitemap-[a-z]{2}\.xml/gi) ?? [];
  return dedupeStrings(matches);
}

export function jllInvestorSitemapCandidateLimit(max: number, total: number): number {
  if (JLL_INVESTOR_SITEMAP_SCAN_LIMIT > 0) return Math.min(total, JLL_INVESTOR_SITEMAP_SCAN_LIMIT);
  if (!Number.isFinite(max)) return total;
  const requested = Math.max(1, Math.trunc(max));
  return Math.min(total, Math.max(requested * 8, requested + 25));
}

export function jllInvestorStatus(row: any): string {
  if (row?.isUnderContract) return "Under Contract";
  const status = clean(row?.stageName ?? row?.status);
  return status ?? "Active";
}

export function jllInvestorCountryClassification(
  value: unknown
): "us" | "non_us" | "unknown" {
  const country = clean(value);
  if (!country) return "unknown";
  if (/^(?:unknown|n\/?a|not available|-)$/i.test(country)) return "unknown";
  const code = jllInvestorCountryCode(country);
  if (!code) return "unknown";
  return code === "US" || JLL_INVESTOR_US_TERRITORY_CODES.has(code)
    ? "us"
    : "non_us";
}

export function jllInvestorSearchCountryClassification(
  row: any
): "us" | "non_us" | "unknown" {
  const country = clean(row?.country);
  const countryClassification = jllInvestorCountryClassification(country);
  const region = clean(row?.region);
  if (region === "EMEA" || region === "APAC") return "non_us";
  return countryClassification;
}

function jllInvestorDetailError(base: any, message: string, id?: string | null): any {
  return prune({
    ...base,
    id: id ?? base.id,
    detailError: message,
    preserveChildCollections: true,
  });
}

export function jllInvestorSearchListing(row: any): any {
  const url = jllInvestorUrlFromAlias(row?.alias);
  const id = clean(row?.id) ?? clean(row?.alias)?.split("/").slice(-1)[0] ?? null;
  return prune({
    id,
    name: clean(row?.name),
    transactionType: "Sale (investment)",
    assetType:
      clean(row?.assetType) ??
      clean(row?.rawAssetType) ??
      (Array.isArray(row?.assetTypesPrimaryList) ? row.assetTypesPrimaryList.map(clean).filter(Boolean).join(", ") : null),
    status: jllInvestorStatus(row),
    street: clean(row?.displayAddress),
    city: clean(row?.city),
    state: clean(row?.state),
    country: clean(row?.country) === "United States" ? "US" : clean(row?.country),
    latitude: num(row?.latitude),
    longitude: num(row?.longitude),
    sizeText: clean(row?.numberOfUnits),
    brokerIds: [],
    photos: clean(row?.image) ? [clean(row.image)] : [],
    url,
    jllInvestorSearchRow: row,
  });
}

export function jllInvestorSearchFallback(rawHtml: string, max: number): any[] {
  const $ = cheerio.load(rawHtml);
  const seen = new Set<string>();
  const listings: any[] = [];
  $('a[href*="/us/en/listings/"]').each((_, el) => {
    if (listings.length >= max) return;
    const href = $(el).attr("href")!;
    const abs = href.startsWith("http") ? href : `${JLL_INVESTOR_HOST}${href}`;
    if (seen.has(abs)) return;
    seen.add(abs);
    const card = $(el).closest("li,article,div[class]");
    const txt = clean(card.text()) ?? "";
    const img = card.find("img").attr("src") ?? null;
    const slugParts = abs.split("/listings/")[1]?.split("/") ?? [];
    listings.push(
      prune({
        id: slugParts.slice(-1)[0] ?? null,
        name:
          clean(card.find("h3,h4").first().text()) ??
          clean(slugParts.slice(-1)[0]?.replace(/-/g, " ")) ??
          null,
        transactionType: "Sale (investment)",
        assetType: clean(slugParts.length > 1 ? slugParts[0]?.replace(/-/g, " ") : null),
        status: /under contract/i.test(txt)
          ? "Under Contract"
          : /closed/i.test(txt)
            ? "Closed"
            : "Active",
        brokerIds: [],
        photos: img ? [img] : [],
        url: abs,
      })
    );
  });
  return listings;
}

export type JllInvestorSearchPage = {
  count: number;
  page: number;
  rows: any[];
};

export type JllInvestorSearchSnapshot = {
  count: number;
  observedAt: string;
  pages: number;
  rows: any[];
};

export function jllInvestorSearchPageUrl(page: number): string {
  if (!Number.isInteger(page) || page < 1) {
    throw new Error("invalid JLL Investor search page");
  }
  return page === 1 ? JLL_INVESTOR_SEARCH_URL : `${JLL_INVESTOR_SEARCH_URL}&page=${page}`;
}

export function parseJllInvestorSearchPage(
  rawHtml: string,
  expectedPage: number
): JllInvestorSearchPage {
  const next = jllInvestorNextData(rawHtml);
  const search = next?.props?.pageProps?.initialState?.advancedSearch;
  const filters = Array.isArray(search?.filters) ? search.filters : [];
  if (
    filters.length !== 1 ||
    clean(filters[0]?.key) !== "location" ||
    clean(filters[0]?.value) !== "United States" ||
    clean(filters[0]?.label) !== "United States" ||
    clean(filters[0]?.type) !== "collection"
  ) {
    throw new Error("JLL Investor search page lacks the exact United States filter state");
  }
  const count = search?.count;
  const page = search?.searchPage;
  const rows = search?.listings;
  if (!Number.isInteger(count) || count <= 0) {
    throw new Error("JLL Investor search page lacks a positive integer count");
  }
  if (!Number.isInteger(page) || page !== expectedPage) {
    throw new Error(
      `JLL Investor search page mismatch: expected ${expectedPage}, received ${String(page)}`
    );
  }
  if (!Array.isArray(rows)) {
    throw new Error("JLL Investor search page lacks a listings array");
  }
  const pageCount = Math.ceil(count / JLL_INVESTOR_SEARCH_PAGE_SIZE);
  if (expectedPage > pageCount) {
    throw new Error("JLL Investor search page exceeds the declared page count");
  }
  const expectedRows =
    expectedPage < pageCount
      ? JLL_INVESTOR_SEARCH_PAGE_SIZE
      : count - JLL_INVESTOR_SEARCH_PAGE_SIZE * (pageCount - 1);
  if (rows.length !== expectedRows) {
    throw new Error(
      `JLL Investor search page ${expectedPage} returned ${rows.length}/${expectedRows} rows`
    );
  }
  const ids = new Set<string>();
  const urls = new Set<string>();
  for (const row of rows) {
    const id = clean(row?.id);
    const url = jllInvestorUrlFromAlias(row?.alias);
    if (!id || !/^006[A-Za-z0-9]{15}$/.test(id)) {
      throw new Error(`JLL Investor search page ${expectedPage} has an invalid listing id`);
    }
    if (!url) {
      throw new Error(`JLL Investor search page ${expectedPage} has a missing listing URL`);
    }
    jllInvestorDetailRoute("search-proof", url);
    if (ids.has(id) || urls.has(url)) {
      throw new Error(`JLL Investor search page ${expectedPage} has a duplicate identity`);
    }
    ids.add(id);
    urls.add(url);

  }
  return { count, page, rows };
}

export function jllInvestorSearchSnapshotFingerprint(rows: any[]): string {
  const canonicalize = (value: any): any => {
    if (Array.isArray(value)) return value.map(canonicalize);
    if (value && typeof value === "object") {
      return Object.fromEntries(
        Object.keys(value)
          .sort()
          .map((key) => [key, canonicalize(value[key])])
      );
    }
    return value;
  };
  return rows
    .map((row) => JSON.stringify(canonicalize(row)))
    .sort()
    .join("\n");
}

async function collectJllInvestorSearchPass(): Promise<JllInvestorSearchSnapshot> {
  const scrapePage = async (page: number): Promise<JllInvestorSearchPage> => {
    const rawHtml = await scrapeRaw(jllInvestorSearchPageUrl(page), {
      waitFor: JLL_INVESTOR_DETAIL_WAIT_MS,
      timeout: 60000,
      maxAge: 0,
    });
    return parseJllInvestorSearchPage(rawHtml, page);
  };
  const first = await scrapePage(1);
  const pages = Math.ceil(first.count / JLL_INVESTOR_SEARCH_PAGE_SIZE);
  const rest = await pmap(
    Array.from({ length: Math.max(0, pages - 1) }, (_, index) => index + 2),
    JLL_INVESTOR_SEARCH_PAGE_CONCURRENCY,
    scrapePage
  );
  const allPages = [first, ...rest];
  if (allPages.some((result) => result.count !== first.count)) {
    throw new Error("JLL Investor search count changed during pagination");
  }
  const rows = allPages.flatMap((result) => result.rows);
  if (rows.length !== first.count) {
    throw new Error(`JLL Investor search recovered ${rows.length}/${first.count} rows`);
  }
  const ids = new Set<string>();
  const urls = new Set<string>();
  for (const row of rows) {
    const id = clean(row?.id)!;
    const url = jllInvestorUrlFromAlias(row?.alias)!;
    if (ids.has(id) || urls.has(url)) {
      throw new Error("JLL Investor search pagination has a duplicate identity");
    }
    ids.add(id);
    urls.add(url);
  }
  return {
    count: first.count,
    observedAt: new Date().toISOString(),
    pages,
    rows,
  };
}

export async function collectJllInvestorSearchSnapshot(): Promise<JllInvestorSearchSnapshot> {
  let prior: JllInvestorSearchSnapshot | null = null;
  let priorFingerprint: string | null = null;
  for (let pass = 1; pass <= JLL_INVESTOR_SEARCH_MAX_PASSES; pass++) {
    const current = await collectJllInvestorSearchPass();
    const fingerprint = jllInvestorSearchSnapshotFingerprint(current.rows);
    console.error(
      `  jll-investor: search snapshot pass ${pass} recovered ${current.count} rows across ${current.pages} pages`
    );
    if (
      prior &&
      prior.count === current.count &&
      priorFingerprint === fingerprint
    ) {
      return current;
    }
    prior = current;
    priorFingerprint = fingerprint;
  }
  throw new Error("JLL Investor search inventory did not stabilize across consecutive passes");
}

export function jllInvestorDocumentUrls(listing: any): string[] {
  const docs = listing?.documents;
  const candidates: string[] = [];
  const visit = (value: any) => {
    if (!value) return;
    if (typeof value === "string") {
      if (/^https?:\/\//i.test(value)) candidates.push(value);
      return;
    }
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (typeof value === "object") {
      visit(value.url);
      for (const nested of Object.values(value)) visit(nested);
    }
  };
  visit(docs);
  return dedupeStrings(candidates);
}

export function jllInvestorImageUrls(listing: any, fallback: string[] = []): string[] {
  const images = [
    clean(listing?.image),
    ...(Array.isArray(listing?.multimedia?.images) ? listing.multimedia.images.map(clean) : []),
    ...fallback,
  ];
  return dedupeStrings(images).filter((url) => /^https?:\/\//i.test(url));
}

/**
 * Extract a single normalized license string from a JLL Investor broker licenses array.
 * Investor shape: [{ number, location, type }].
 * Returns the first entry formatted as "location: number", or null.
 */
export function jllInvestorExtractLicense(licenses: any): string | null {
  const arr = Array.isArray(licenses) ? licenses : [];
  const first = arr[0];
  if (!first) return null;
  const location = clean(first?.location ?? first?.state ?? first?.type);
  const number = clean(first?.number ?? first?.licenseNumber);
  if (number && location) return `${location}: ${number}`;
  if (number) return number;
  return null;
}

export function jllInvestorContacts(listing: any): any[] {
  if (!Array.isArray(listing?.brokers)) return [];
  const contacts = listing.brokers
    .map((broker: any) =>
      prune({
        name: clean(broker?.name),
        title: clean(broker?.title),
        email: clean(broker?.email),
        phone: clean(broker?.phone),
        company: "JLL",
        avatarUrl: clean(broker?.image),
        linkedInUrl: clean(broker?.linkedInURL),
        license: jllInvestorExtractLicense(broker?.licenses),
        licensedEntity: broker?.licensedEntity,
        licenses: broker?.licenses,
      })
    )
    .filter(Boolean);
  const seen = new Set<string>();
  return contacts.filter((contact: any) => {
    const key = contact.email ?? contact.name ?? JSON.stringify(contact);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

// Promote the stranded JLL Investor multimedia video / virtual-tour fields for
// harvestDetail. `multimedia.videos` / `videoUrls` and `multimedia.virtualTours`
// / `tourUrls` are string-url arrays the adapter previously dropped. Video urls
// are emitted as BARE STRINGS so the harvester derives provider + embedUrl; tour
// / 360 urls are emitted as TYPED virtual_tour items so they keep that
// classification on unrecognized hosts. harvestDetail dedups by url. Never throws.
export function jllInvestorStrandedMedia(listing: any): (MediaItem | string)[] {
  const mm = listing?.multimedia ?? {};
  const out: (MediaItem | string)[] = [];
  const urlsOf = (value: any): string[] => {
    const arr = Array.isArray(value) ? value : value != null ? [value] : [];
    return arr
      .map((raw: any) => clean(typeof raw === "string" ? raw : raw?.url ?? raw?.src))
      .filter((u: string | null): u is string => Boolean(u) && /^https?:\/\//i.test(u as string));
  };
  for (const url of urlsOf(mm.videos ?? mm.videoUrls ?? listing?.videos)) out.push(url);
  for (const url of urlsOf(mm.virtualTours ?? mm.tourUrls ?? listing?.virtualTours)) {
    out.push({ mediaType: "virtual_tour", provider: null, url, embedUrl: null, title: null });
  }
  for (const url of urlsOf(mm.view360URLs ?? listing?.view360URLs)) {
    out.push({ mediaType: "virtual_tour", provider: null, url, embedUrl: null, title: null });
  }
  return out;
}

// Promote gated / CA document urls (documentsCA) as DocItems. The public teaser
// documents already flow through jllInvestorDocumentUrls -> brochures; the CA
// set was previously kept only in raw metadata. harvestDetail classifies each by
// filename/keyword (om/financials/rent_roll/...) and dedups by url.
export function jllInvestorStrandedDocs(listing: any): DocItem[] {
  const urls: string[] = [];
  const visit = (value: any) => {
    if (!value) return;
    if (typeof value === "string") {
      if (/^https?:\/\//i.test(value)) urls.push(value);
      return;
    }
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (typeof value === "object") {
      visit(value.url ?? value.href);
      for (const nested of Object.values(value)) visit(nested);
    }
  };
  visit(listing?.documentsCA);
  return dedupeStrings(urls).map((url) => ({ url, title: titleFromFilename(url), docType: "other" as const }));
}

// Lift stranded structured fields the JLL Investor detail payload exposes onto
// the existing listing keys cre_ingest.to_row maps. Only clearly-present values
// are lifted; absent fields stay undefined (prune removes them) so this never
// clobbers good data.
//
// Phase-2 additions (additive only):
//   highlights  <- jllInvestorDetail.highlights may be an HTML string (strip) or
//                  an array of objects/strings
//   statusBadge <- jllInvestorDetail.stageName / isUnderContract / top-level status
//   canonicalUrl <- base.url (the invest.jll.com detail URL)
//   extraFacts  <- { deal_type } from jllInvestorDetail.dealType
export function jllInvestorStrandedStructured(listing: any): Record<string, any> {
  // highlights: the investor payload stores these as an HTML string (rich text editor output)
  // OR as an array of objects/strings. Handle both.
  let highlights: string[] = [];
  if (typeof listing?.highlights === "string") {
    // HTML string: strip tags, split on list-item boundaries, clean each line.
    const stripped = stripHtmlText(listing.highlights) ?? "";
    highlights = dedupeStrings(
      stripped
        .split(/\n|(?<=\.)(?=\s*[A-Z])/)
        .map((s) => clean(s))
        .filter(Boolean) as string[]
    );
  } else if (Array.isArray(listing?.highlights)) {
    highlights = dedupeStrings(
      listing.highlights
        .map((h: any) =>
          typeof h === "string"
            ? clean(h)
            : clean(h?.text ?? h?.value ?? h?.title)
        )
        .filter(Boolean)
    );
  }

  // statusBadge: derive from the investor detail shape (stageName / isUnderContract).
  // Routes to the existing OPT-IN activation gate in cre_ingest; never auto-activates.
  let statusBadge: string | undefined;
  if (listing?.isUnderContract) {
    statusBadge = "Under Contract";
  } else if (clean(listing?.stageName)) {
    statusBadge = clean(listing.stageName) ?? undefined;
  }

  // extraFacts: long-tail facts with no discrete column.
  const extraFacts: Record<string, unknown> = {};
  const dealType = clean(listing?.dealType);
  if (dealType) extraFacts.deal_type = dealType;

  return (
    prune({
      units: num(listing?.numberOfUnits) ?? num(Number(listing?.numberOfUnits)),
      yearBuilt: num(listing?.yearBuilt) ?? num(Number(listing?.yearBuilt)),
      occupancyRate: num(listing?.occupancyRate) ?? num(Number(listing?.occupancy)),
      capRatePct: num(listing?.capRate) ?? num(Number(listing?.capRate)),
      market: clean(listing?.market),
      submarket: clean(listing?.submarket),
      highlights: highlights.length ? highlights : undefined,
      statusBadge,
      extraFacts: Object.keys(extraFacts).length ? extraFacts : undefined,
    }) ?? {}
  );
}

// Pure transform (no network): given the base row and an already-scraped detail
// doc, return the enriched listing row (or a `detailError` row when the page has
// no pdp listing in __NEXT_DATA__). Factored out of enrichJllInvestorListing so
// the enrichment worker and the unit test can reuse it against a saved fixture.
// `base.url` flows through unchanged, satisfying the worker's URL-keyed
// completion match.
function parseJllInvestorListing(
  base: any,
  listing: any,
  doc: ScrapedDoc,
  expectedAlias?: string
): any {
  const exactId = clean(listing.id);
  if (!exactId) {
    return jllInvestorDetailError(
      base,
      "detail payload lacks stable provider listing.id"
    );
  }
  if (expectedAlias) {
    if (!/^006[A-Za-z0-9]{15}$/.test(exactId)) {
      return jllInvestorDetailError(
        base,
        "structured detail payload lacks an exact Salesforce Opportunity id",
        exactId
      );
    }
    const detailAlias = clean(listing.alias);
    if (detailAlias !== expectedAlias) {
      return jllInvestorDetailError(
        base,
        `structured detail alias mismatch: expected ${expectedAlias}, received ${detailAlias ?? "missing"}`,
        exactId
      );
    }
    const expectedId = clean(base.id);
    if (
      expectedId &&
      /^006[A-Za-z0-9]{15}$/.test(expectedId) &&
      expectedId !== exactId
    ) {
      return jllInvestorDetailError(
        base,
        `structured detail id mismatch: expected ${expectedId}, received ${exactId}`,
        exactId
      );
    }
  }
  let countryClassification = jllInvestorDetailCountryClassification(listing);
  if (base?.jllInvestorUsSearchMembership === true) {
    if (countryClassification === "non_us") {
      return jllInvestorDetailError(
        base,
        "United States search membership conflicts with non-US detail evidence",
        exactId
      );
    }
    if (countryClassification === "unknown") countryClassification = "us";
  }
  if (countryClassification === "unknown") {
    return jllInvestorDetailError(
      base,
      "detail payload lacks an exact US/non-US country classification",
      exactId
    );
  }
  const contactsDetailed = jllInvestorContacts(listing);
  const brokerIds = contactsDetailed
    .map((contact: any) =>
      brokerRef({
        name: clean(contact.name),
        email: clean(contact.email),
        phone: clean(contact.phone),
        office: clean(contact.office),
        avatarUrl: clean(contact.avatarUrl),
        company: "JLL",
      })
    )
    .filter((id: number | null): id is number => id !== null);
  const documentUrls = jllInvestorDocumentUrls(listing);
  const teaserDocs = documentUrls.map((url) => ({ name: titleFromFilename(url), url }));
  const photos = jllInvestorImageUrls(listing, base.photos ?? []);
  // Capture-everything harvest: unify the full detail page (markdown / links /
  // images / video+iframe attributes) with the stranded native fields promoted
  // via ctx.extra* (multimedia videos/tours, CA/gated documents, image gallery).
  // Public teaser documents stay on the existing `brochures` channel; they are
  // NOT promoted into extraDocs (cre_listing_documents has no (listing_id,url)
  // unique key, so a url in BOTH channels would double-insert). harvested.documents
  // is filtered to exclude any url already on the brochures channel. When the doc
  // carries no structured `images` (e.g. a saved fixture), fall back to the native
  // gallery for the image channel rather than the rawHtml <img> regex.
  const harvestDoc: ScrapedDoc = Array.isArray(doc.images) ? doc : { ...doc, images: photos };
  const harvested = harvestDetail(harvestDoc, {
    baseUrl: base.url,
    extraMedia: jllInvestorStrandedMedia(listing),
    extraDocs: jllInvestorStrandedDocs(listing),
    extraImages: photos,
  });
  const teaserUrlSet = new Set(documentUrls.map((u) => u.toLowerCase()));
  const documents = harvested.documents.filter((d) => !teaserUrlSet.has(d.url.toLowerCase()));
  const lifted = jllInvestorStrandedStructured(listing);
  // canonicalUrl: the invest.jll.com detail page URL is the stable canonical
  // for investor listings. Use base.url (already normalized by srcJllInvestor).
  const canonicalUrl = clean(base.url) ?? undefined;
  const observation = doc.detailObservation;
  return prune({
    ...base,
    id: exactId,
    detailObservedAt: observation?.observedAt,
    freshnessProvenance: observation
      ? {
          detailScope: "detail_page",
          generationId: observation.generationId,
          method: observation.method,
          cacheDisposition: observation.cacheDisposition,
          identityMethod: "provider_listing_id",
        }
      : undefined,
    name: clean(listing.name) ?? base.name,
    assetType:
      clean(listing.assetType) ??
      clean(listing.rawAssetType) ??
      (Array.isArray(listing.assetTypesPrimaryList)
        ? listing.assetTypesPrimaryList.map(clean).filter(Boolean).join(", ")
        : base.assetType),
    description: clean(listing.description) ?? base.description,
    street: clean(listing.fullLocation) ?? base.street,
    city: clean(listing.city) ?? base.city,
    state: clean(listing.state) ?? base.state,
    country: countryClassification === "us" ? "US" : clean(listing.country),
    latitude: num(listing.latitude) ?? base.latitude,
    longitude: num(listing.longitude) ?? base.longitude,
    status: jllInvestorStatus(listing),
    sizeText: clean(listing.numberOfUnits ? `${listing.numberOfUnits} units` : null) ?? base.sizeText,
    ...lifted,
    canonicalUrl,
    brokerIds,
    contactsDetailed,
    brochures: teaserDocs,
    documents,
    media: harvested.media,
    links: harvested.links,
    photos: dedupeStrings([...photos, ...harvested.images]),
    markdown: doc.markdown || base.markdown,
    lastUpdated:
      clean(listing.dateModified ?? listing.datePublished) ??
      (base.lastmod ? String(base.lastmod).slice(0, 10) : null),
    jllInvestorDetail: {
      id: clean(listing.id),
      alias: clean(listing.alias),
      dealType: clean(listing.dealType),
      stageName: clean(listing.stageName),
      isUnderContract: Boolean(listing.isUnderContract),
      highlights: listing.highlights,
      customAttributes: listing.customAttributes,
      documentsCA: listing.documentsCA,
      rawPriceRange: listing.priceRange,
      datePublished: clean(listing.datePublished),
      dateModified: clean(listing.dateModified),
      scrape: {
        markdownLength: doc.markdown.length,
        rawHtmlLength: doc.rawHtml.length,
        linkCount: doc.links.length,
      },
    },
  });
}

export function parseJllInvestorDetail(base: any, doc: ScrapedDoc): any {
  const next = jllInvestorNextData(doc.rawHtml);
  const listing = next?.props?.pageProps?.initialState?.pdp?.listing;
  if (!listing) {
    return jllInvestorDetailError(base, "missing pdp listing in __NEXT_DATA__");
  }
  return parseJllInvestorListing(base, listing, doc);
}

export function parseJllInvestorStructuredDetail(
  base: any,
  payload: any,
  expectedAlias: string
): any {
  const listing = jllInvestorStructuredListing(payload);
  if (!listing) {
    return jllInvestorDetailError(
      base,
      "missing pdp listing in structured Next.js detail payload"
    );
  }
  const photos = jllInvestorImageUrls(listing, base.photos ?? []);
  return parseJllInvestorListing(
    base,
    listing,
    {
      rawHtml: "",
      markdown: "",
      links: [],
      images: photos,
      attributes: [],
      detailObservation: detailObservation(
        "jll_investor_next_data_detail",
        "live"
      ),
    },
    expectedAlias
  );
}

let cachedJllInvestorBuildId: string | null = null;
let jllInvestorBuildIdRequest: Promise<string> | null = null;

export function resetJllInvestorBuildIdForTests(): void {
  cachedJllInvestorBuildId = null;
  jllInvestorBuildIdRequest = null;
}

async function loadJllInvestorBuildId(forceFresh = false): Promise<string> {
  const strictScrapeOpts =
    forceFresh || requireFreshDetails() ? { maxAge: 0 } : {};
  const rawHtml = await scrapeRaw(JLL_INVESTOR_HOME_URL, {
    waitFor: JLL_INVESTOR_DETAIL_WAIT_MS,
    timeout: 60000,
    ...strictScrapeOpts,
  });
  const buildId = jllInvestorBuildId(rawHtml);
  if (!buildId) {
    throw new Error("JLL Investor homepage lacks a safe Next.js build id");
  }
  return buildId;
}

export async function getJllInvestorBuildId(options: {
  force?: boolean;
  staleBuildId?: string;
} = {}): Promise<string> {
  const { force = false, staleBuildId } = options;
  if (
    staleBuildId &&
    cachedJllInvestorBuildId &&
    cachedJllInvestorBuildId !== staleBuildId
  ) {
    return cachedJllInvestorBuildId;
  }
  if (
    !force &&
    cachedJllInvestorBuildId &&
    (!staleBuildId || cachedJllInvestorBuildId !== staleBuildId)
  ) {
    return cachedJllInvestorBuildId;
  }
  if (jllInvestorBuildIdRequest) return jllInvestorBuildIdRequest;
  const request = loadJllInvestorBuildId(force)
    .then((buildId) => {
      cachedJllInvestorBuildId = buildId;
      return buildId;
    })
    .finally(() => {
      if (jllInvestorBuildIdRequest === request) {
        jllInvestorBuildIdRequest = null;
      }
    });
  jllInvestorBuildIdRequest = request;
  return request;
}

export async function enrichJllInvestorListing(
  base: any,
  initialBuildId?: string
): Promise<any> {
  if (!base.url) return base;
  let buildId = initialBuildId ?? await getJllInvestorBuildId();
  let lastErr: unknown = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const route = jllInvestorDetailRoute(buildId, base.url);
      const payload = await scrapeJson(route.url, {
        waitFor: JLL_INVESTOR_DETAIL_WAIT_MS,
        timeout: JLL_INVESTOR_DETAIL_TIMEOUT_MS,
        jsonAttempts: 1,
        // A cached structured 404 must never retire a live queue candidate.
        // JLL's small JSON route is always read live, even in targeted mode.
        maxAge: 0,
      });
      if (jllInvestorStructuredNotFound(payload)) {
        lastErr = new Error("structured Next.js detail returned provider 404");
        const observedBuildId: string = buildId;
        try {
          // Prove which build is current using an uncached homepage read. A
          // 404 from an obsolete JSON route is never a tombstone signal.
          buildId = await getJllInvestorBuildId({
            force: true,
            staleBuildId: buildId,
          });
        } catch (refreshErr) {
          lastErr = refreshErr;
          break;
        }
        if (buildId !== observedBuildId) {
          if (attempt === 3) break;
          continue;
        }
        try {
          // Require a second live signal from the public listing route. This is
          // intentionally a different endpoint and representation than the
          // structured JSON route, so a stale CDN/homepage build alone cannot
          // exclude a live listing.
          const publicHtml = await scrapeRaw(base.url, {
            waitFor: JLL_INVESTOR_DETAIL_WAIT_MS,
            timeout: JLL_INVESTOR_DETAIL_TIMEOUT_MS,
            maxAge: 0,
          });
          if (!jllInvestorPublicPageNotFound(publicHtml, buildId)) {
            throw new Error(
              "structured provider 404 was not confirmed by the live public page"
            );
          }
          return prune({
            ...base,
            skip: "not_found",
            jllInvestorTombstone: {
              statusCode: 404,
              confirmation: "live_current_build_json_and_public_page",
            },
          });
        } catch (publicErr) {
          lastErr = publicErr;
          break;
        }
      }
      if (!jllInvestorStructuredListing(payload)) {
        throw new Error("structured Next.js detail payload lacks pdp listing");
      }
      return parseJllInvestorStructuredDetail(base, payload, route.alias);
    } catch (err) {
      lastErr = err;
      if (attempt === 1) {
        try {
          buildId = await getJllInvestorBuildId({
            force: true,
            staleBuildId: buildId,
          });
          continue;
        } catch (refreshErr) {
          lastErr = refreshErr;
        }
      }
      break;
    }
  }
  console.error(`  jll-investor: structured detail failed for ${base.url}: ${lastErr}`);
  return jllInvestorDetailError(base, String(lastErr));
}

export async function srcJllInvestor(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "JLL Investor Center",
      sourceUrl: JLL_INVESTOR_HOST,
      method: "skipped",
      totalAvailable: 0,
      listings: [],
      note: "Investment-sale platform; no lease inventory.",
    };
  }
  if (monitor) {
    // Monitor mode is NOT supported for jll-investor: the persisted external id
    // is the detail-page Salesforce listing.id (enrichJllInvestorListing), which
    // cannot be recovered from the sitemap (the cheap key is only the URL slug).
    // Verified 934/934 slug != listing.id against the full sitemap artifact.
    // Emitting slug-keyed rows would make every row read as NEW each run and
    // pollute the change ledger / enrichment queue, so jll-investor stays on the
    // full-sweep cadence and emits no monitor rows. Short-circuit BEFORE the
    // sitemap scrape + detail enumeration: the rows are discarded anyway. A cheap
    // path would need URL-keyed reconciliation in cre_monitor.py (out of scope).
    return {
      company: "JLL Investor Center",
      sourceUrl: JLL_INVESTOR_US_SITEMAP_URL,
      method: "Monitor mode unsupported (detail-derived Salesforce external id); full-sweep cadence only",
      totalAvailable: null,
      listings: [],
      note: "Monitor mode emits no rows for jll-investor: its external id is the detail-page Salesforce listing.id and cannot be derived from the sitemap URL slug. Refresh this source via the full (non-monitor) collection path.",
    };
  }
  const searchSnapshot = await collectJllInvestorSearchSnapshot();
  const nonUsSearchRows = searchSnapshot.rows.filter(
    (row) => jllInvestorSearchCountryClassification(row) === "non_us"
  );
  const searchInventory = searchSnapshot.rows
    .filter((row) => jllInvestorSearchCountryClassification(row) !== "non_us")
    .map((row) => {
      const listing = jllInvestorSearchListing(row);
      const url = clean(listing?.url);
      if (!url) throw new Error("JLL Investor search row lacks a safe listing URL");
      return {
        ...listing,
        inventoryObservedAt: searchSnapshot.observedAt,
        jllInvestorUsSearchMembership: true,
      };
    });
  const candidateLimit = Math.min(max, searchInventory.length);
  const candidates = searchInventory.slice(0, candidateLimit);
  const buildId = await getJllInvestorBuildId({ force: true });
  console.error(
    `  jll-investor: ${searchInventory.length} stable United States search row(s), enriching ${candidates.length}`
  );
  let enrichedCount = 0;
  const enriched = await pmap(candidates, JLL_INVESTOR_DETAIL_CONCURRENCY, async (base) => {
    const row = await enrichJllInvestorListing(
      base,
      buildId
    );
    enrichedCount++;
    if (enrichedCount % 10 === 0 || enrichedCount === candidates.length) {
      console.error(`  jll-investor: detail enriched ${enrichedCount}/${candidates.length}`);
    }
    return row;
  });

  const notFoundRows = enriched.filter((row) => row?.skip === "not_found");
  const unresolvedRows = enriched.filter((row) => row?.detailError);
  const detailErrors = unresolvedRows.length;
  const resolvedRows = enriched.filter(
    (row) => !row?.detailError && row?.skip !== "not_found"
  );
  const exactIds = new Set<string>();
  let duplicateExactIdentities = 0;
  for (const row of resolvedRows) {
    const id = clean(row?.id);
    if (!id || exactIds.has(id)) duplicateExactIdentities++;
    if (id) exactIds.add(id);
  }
  const requestedRows = resolvedRows.slice(0, Math.min(max, resolvedRows.length));
  const listings = [...requestedRows, ...unresolvedRows];
  const incompleteEnumeration = candidates.length !== searchInventory.length;
  const requestedLimitApplied = requestedRows.length !== resolvedRows.length;
  const truncated =
    incompleteEnumeration ||
    requestedLimitApplied ||
    detailErrors > 0 ||
    notFoundRows.length > 0 ||
    duplicateExactIdentities > 0;
  if (!listings.length) {
    throw new Error(
      "no United States listing details or unresolved search candidates found in JLL Investor Center"
    );
  }
  return {
    company: "JLL Investor Center",
    sourceUrl: JLL_INVESTOR_SEARCH_URL,
    method: "Two consecutive exact United States search snapshots plus exact-identity structured Next.js detail JSON",
    totalAvailable: searchInventory.length,
    listings,
    truncated,
    note:
      `The exact United States search filter stabilized at ${searchSnapshot.count} unique provider identities across ${searchSnapshot.pages} pages in two consecutive complete passes. Excluded ${nonUsSearchRows.length} row(s) carrying explicit non-U.S. country or recognized non-U.S. region evidence before enrichment. The search snapshot is authoritative because JLL's sitemap can lag newly published search inventory; every retained row must instead pass exact Salesforce ID and alias reconciliation against live structured detail. Enriched ${candidates.length}/${searchInventory.length} U.S.-compatible search row(s), excluded ${notFoundRows.length} provider 404 tombstone(s) independently confirmed by live current-build JSON and public-page observations, retained ${detailErrors} unresolved candidate(s), and detected ${duplicateExactIdentities} duplicate exact provider identity/identities. Detail country evidence must not conflict with the United States search membership. Structured detail enrichment retains native teaser, document, image/media, and broker-contact URL metadata through the existing child-classification contract; no document or image binaries are fetched.`,
  };
}
