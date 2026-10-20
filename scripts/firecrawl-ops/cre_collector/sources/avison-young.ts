import { avisonAskingControl, projectAvisonAskingPrice } from "./pure/avison-price-visibility.js";
import { extractAvisonLeaseFacts } from "./pure/avison-lease-facts.js";
// sources/avison-young.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { lookup } from "node:dns/promises";
import { request as httpsRequest } from "node:https";
import { BlockList, isIP } from "node:net";
import TurndownService from "turndown";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { decodeHtmlEntities, dedupeStrings, firstJsonLd, jsonLdObjects, stripHtmlText, titleFromFilename } from "../lib/html.js";
import { normBuildingClass } from "../lib/parse.js";
import { scrapeDoc } from "../lib/scrape.js";
import { harvestDetail, type HarvestResult } from "../lib/harvest.js";
import { DocItem, LinkItem, MediaItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { boundedInt, clean, num, pmap, prune } from "../lib/util.js";
import {
  detailObservation,
  refreshGenerationId,
  requireFreshDetails,
  requireFreshPropertyDetails,
} from "../lib/freshness.js";


// --- Avison Young: SharpLaunch search app ---

export const AVISON_YOUNG_PAGE_URL =
  "https://www.avisonyoung.us/properties/#/?transaction=sale&view=sidebar&status=active";
export const AVISON_YOUNG_API_BASE = "https://pse-api.sharplaunch.com/data";
export const AVISON_YOUNG_FALLBACK_API_KEY = "b9fda00f3d4d7f623665270841e32176";
export const AVISON_YOUNG_CDN_BASE = "https://cdn.sharplaunch.com";
export const AVISON_YOUNG_HOST = "https://www.avisonyoung.us";
export const AVISON_YOUNG_DETAIL_CONCURRENCY = boundedInt(
  process.env.AVISON_YOUNG_DETAIL_CONCURRENCY,
  CONCURRENCY,
  1,
  CONCURRENCY
);
export const AVISON_YOUNG_DIRECT_DETAIL_TIMEOUT_MS = boundedInt(
  process.env.AVISON_YOUNG_DIRECT_DETAIL_TIMEOUT_MS,
  30000,
  1000,
  120000
);
export const AVISON_YOUNG_DIRECT_DETAIL_ATTEMPTS = boundedInt(
  process.env.AVISON_YOUNG_DIRECT_DETAIL_ATTEMPTS,
  3,
  1,
  5
);
export const AVISON_YOUNG_DIRECT_DETAIL_RETRY_MS = boundedInt(
  process.env.AVISON_YOUNG_DIRECT_DETAIL_RETRY_MS,
  250,
  0,
  5000
);
// Reserved collector-process result for a confirmed required-detail 404. The
// checkpoint coordinator handles this separately from ordinary collector and
// resource/cancellation exit codes; it is still a failed, unaccepted source.
export const AVISON_YOUNG_TERMINAL_404_EXIT_CODE = 78;
const AVISON_YOUNG_TURNDOWN = new TurndownService({
  headingStyle: "atx",
  bulletListMarker: "-",
  codeBlockStyle: "fenced",
});

export let avisonYoungCache:
  | {
      apiKey: string;
      websiteRows: any[];
      teamMembers: Map<string, any>;
      teamFeedComplete: boolean;
      teamFeedReason: string | null;
    }
  | null = null;

export type AvisonYoungTerminalDetailFailure = {
  listingId: string;
  role: "sharpLaunch" | "external" | "other";
  status: 404;
};

export class AvisonYoungHttpStatusError extends Error {
  constructor(
    readonly url: string,
    readonly status: number
  ) {
    super(`Avison Young direct detail returned HTTP ${status}`);
    this.name = "AvisonYoungHttpStatusError";
  }
}

export class AvisonYoungTerminalDetailError extends Error {
  constructor(readonly failure: AvisonYoungTerminalDetailFailure) {
    super("Avison Young required detail returned HTTP 404");
    this.name = "AvisonYoungTerminalDetailError";
  }
}

export class AvisonYoungTerminalSourceError extends Error {
  readonly code = "AVISON_YOUNG_REQUIRED_DETAIL_HTTP_404";

  constructor(readonly failures: AvisonYoungTerminalDetailFailure[]) {
    super(`Avison Young required detail returned HTTP 404 for ${failures.length} observed listing(s)`);
    this.name = "AvisonYoungTerminalSourceError";
  }
}

export function isAvisonYoungTerminalSourceError(
  error: unknown
): error is AvisonYoungTerminalSourceError {
  return error instanceof AvisonYoungTerminalSourceError;
}

export function isExclusiveAvisonYoungTerminal404Failure(input: {
  requestedSources: string[];
  transactions: string[];
  listingsCollected: number;
  terminalFailureTransactions: number;
  otherFailureCount: number;
}): boolean {
  return (
    input.requestedSources.length === 1
    && input.requestedSources[0] === "avison-young"
    && input.transactions.length > 0
    && input.listingsCollected === 0
    && input.terminalFailureTransactions === input.transactions.length
    && input.otherFailureCount === 0
  );
}

// Stop admitting new detail requests on the first strict failure, then wait
// for the already-started bounded batch to settle. A 404 is terminal only when
// no in-flight request produced a different (potentially transient) failure.
export async function mapAvisonYoungStrictDetails<T, R>(
  items: T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>
): Promise<R[]> {
  const results: R[] = new Array(items.length);
  const terminalFailures: AvisonYoungTerminalDetailFailure[] = [];
  let firstOtherError: unknown;
  let hasOtherError = false;
  let next = 0;
  let stopped = false;

  async function worker(): Promise<void> {
    while (!stopped && next < items.length) {
      const index = next++;
      try {
        results[index] = await fn(items[index]!, index);
      } catch (error) {
        stopped = true;
        if (error instanceof AvisonYoungTerminalDetailError) {
          terminalFailures.push(error.failure);
        } else {
          if (!hasOtherError) firstOtherError = error;
          hasOtherError = true;
        }
      }
    }
  }

  await Promise.all(
    Array.from({ length: Math.min(limit, items.length) }, () => worker())
  );
  if (hasOtherError) throw firstOtherError;
  if (terminalFailures.length) {
    throw new AvisonYoungTerminalSourceError(terminalFailures);
  }
  if (next < items.length) {
    throw new Error("Avison Young strict detail batch stopped without a terminal error");
  }
  return results;
}

export async function fetchAvisonYoungApiKey(): Promise<string> {
  try {
    const res = await fetch(AVISON_YOUNG_PAGE_URL, {
      headers: { "User-Agent": "Mozilla/5.0 CRE collector" },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const html = await res.text();
    const key = html.match(/SharpLaunch\.PSE\.create\(\s*['"]([a-f0-9]{32})['"]/i)?.[1];
    if (key) return key;
    console.error("  avison-young: SharpLaunch key not found on page; using documented fallback");
  } catch (err) {
    console.error(`  avison-young: failed to fetch page key (${err}); using documented fallback`);
  }
  return AVISON_YOUNG_FALLBACK_API_KEY;
}

export async function fetchAvisonYoungEntity(entity: string, apiKey: string): Promise<any[]> {
  const url = new URL(AVISON_YOUNG_API_BASE);
  url.searchParams.set("entity", entity);
  if (entity === "website") url.searchParams.set("status", "active");
  const res = await fetch(url, { headers: { "X-Api-Key": apiKey } });
  if (!res.ok) throw new Error(`Avison Young SharpLaunch ${entity} API HTTP ${res.status}`);
  const data = await res.json();
  return avisonYoungEntityItems(data, entity);
}

export function avisonYoungEntityItems(data: any, entity: string): any[] {
  const items = Array.isArray(data?.items) ? data.items : [];
  // SharpLaunch removed the Avison Young team_member export in July 2026 while
  // continuing to expose the complete website inventory. Property detail pages
  // remain the current source for broker cards, so an empty supplemental team
  // feed must not hide every listing. The website inventory stays required.
  if (!items.length && entity !== "team_member") {
    throw new Error(`Avison Young SharpLaunch ${entity} API returned no items`);
  }
  return items;
}

export function avisonYoungTeamFeedState(
  rows: any[],
  error: unknown = null
): { rows: any[]; complete: boolean; reason: string | null } {
  if (error) {
    return { rows: [], complete: false, reason: `team_member request failed: ${String(error)}` };
  }
  if (!rows.length) {
    return { rows: [], complete: false, reason: "team_member API returned no items" };
  }
  return { rows, complete: true, reason: null };
}

export async function getAvisonYoungFeed(): Promise<{
  apiKey: string;
  websiteRows: any[];
  teamMembers: Map<string, any>;
  teamFeedComplete: boolean;
  teamFeedReason: string | null;
}> {
  if (avisonYoungCache) return avisonYoungCache;
  const apiKey = await fetchAvisonYoungApiKey();
  const [websiteRows, rawTeamState] = await Promise.all([
    fetchAvisonYoungEntity("website", apiKey),
    fetchAvisonYoungEntity("team_member", apiKey)
      .then((rows) => avisonYoungTeamFeedState(rows))
      .catch((error) => avisonYoungTeamFeedState([], error)),
  ]);
  const { rows: teamRows, complete: teamFeedComplete, reason: teamFeedReason } = rawTeamState;
  const teamMembers = new Map<string, any>();
  for (const member of teamRows) {
    if (member?.id != null) teamMembers.set(String(member.id), member);
  }
  avisonYoungCache = {
    apiKey,
    websiteRows,
    teamMembers,
    teamFeedComplete,
    teamFeedReason,
  };
  console.error(
    `  avison-young: cached SharpLaunch feed (${websiteRows.length} active rows, ${teamMembers.size} team members` +
      `${teamFeedComplete ? "" : `; degraded: ${teamFeedReason}`})`
  );
  return avisonYoungCache;
}

export function sharpLaunchCdnUrl(path: any): string | null {
  const p = clean(path);
  if (!p) return null;
  if (/^https?:\/\//i.test(p)) return p;
  return `${AVISON_YOUNG_CDN_BASE}/${p.replace(/^\/+/, "")}`;
}

export function avisonYoungAbsoluteUrl(value: any, base = AVISON_YOUNG_HOST): string | null {
  const raw = clean(value);
  if (!raw || /^javascript:/i.test(raw) || /^mailto:/i.test(raw) || /^tel:/i.test(raw)) return null;
  try {
    return new URL(decodeHtmlEntities(raw), base).toString();
  } catch {
    return null;
  }
}

export function avisonYoungDetailLimit(
  max: number,
  selectedCount: number,
  strict = requireFreshDetails()
): number {
  if (strict) return selectedCount;
  if (process.env.AVISON_YOUNG_DETAIL_LIMIT !== undefined) {
    return boundedInt(process.env.AVISON_YOUNG_DETAIL_LIMIT, 0, 0, selectedCount);
  }
  return Number.isFinite(max) ? selectedCount : 0;
}

export function avisonYoungTruncated(
  max: number,
  selectedCount: number,
  totalEligible: number
): boolean {
  return Number.isFinite(max) && selectedCount < totalEligible;
}

export function assertAvisonYoungStrictFeed(
  teamFeedComplete: boolean,
  teamFeedReason: string | null,
  strict = requireFreshDetails()
): void {
  if (strict && !teamFeedComplete) {
    throw new Error(
      `Avison Young team feed is incomplete and would preserve child collections: ${
        teamFeedReason ?? "unknown reason"
      }`
    );
  }
}

export type AvisonYoungOfficialFeedIdentityContext = {
  matchingExternalUrlRowIds: readonly string[];
};

export function avisonYoungOfficialUrlKey(value: unknown): string | null {
  try {
    const url = new URL(String(value ?? ""));
    if (url.protocol !== "https:" || url.hostname.toLowerCase() !== "www.avisonyoung.us"
      || url.username || url.password || url.port || !url.pathname.startsWith("/properties/")) return null;
    return `${url.origin}${url.pathname.replace(/\/+$/, "")}${url.search}${url.hash}`;
  } catch {
    return null;
  }
}

export function indexAvisonYoungOfficialFeedUrls(websiteRows: readonly any[]): Map<string, string[]> {
  const rowsByUrl = new Map<string, string[]>();
  for (const row of websiteRows) {
    const key = avisonYoungOfficialUrlKey(row?.external_url);
    if (!key) continue;
    const rowIds = rowsByUrl.get(key) ?? [];
    const rowId = row?.id == null ? "" : String(row.id);
    // Repeated copies of the same literal feed identity do not create a second owner.
    // Preserve each empty-ID occurrence because it cannot be safely deduplicated.
    if (!rowId || !rowIds.includes(rowId)) rowIds.push(rowId);
    rowsByUrl.set(key, rowIds);
  }
  return rowsByUrl;
}

export function assertAvisonYoungDetailDoc(
  doc: ScrapedDoc,
  requestedUrl: string,
  base: any,
  officialFeedIdentity?: AvisonYoungOfficialFeedIdentityContext
): any | null {
  const status = Number(doc.metadata?.statusCode);
  if (Number.isFinite(status) && status >= 400) {
    throw new Error(`Avison Young detail returned HTTP ${status}`);
  }
  const combined = `${doc.rawHtml ?? ""}\n${doc.markdown ?? ""}`;
  if (
    !combined.trim()
    || /just a moment|checking your browser|verify you are human|captcha|access denied|cf-chl-|page not found|404\s*[-:]\s*page not found|internal server error|service unavailable/i.test(
      combined
    )
  ) {
    throw new Error("Avison Young detail returned a challenge or error shell");
  }

  const listing = firstJsonLd(doc.rawHtml, "RealEstateListing");
  const requested = avisonYoungAbsoluteUrl(requestedUrl);
  const observed = avisonYoungAbsoluteUrl(
    listing?.url
      ?? doc.metadata?.sourceURL
      ?? doc.metadata?.url
  );
  let urlMatches = false;
  if (requested && observed) {
    try {
      const requestedParsed = new URL(requested);
      const observedParsed = new URL(observed);
      urlMatches =
        requestedParsed.hostname.toLowerCase()
          === observedParsed.hostname.toLowerCase()
        && requestedParsed.pathname.replace(/\/+$/, "").toLowerCase()
          === observedParsed.pathname.replace(/\/+$/, "").toLowerCase();
    } catch {
      urlMatches = requested === observed;
    }
  }

  const $ = cheerio.load(doc.rawHtml);
  const bodyText = clean($("body").text() || doc.markdown)?.toLowerCase() ?? "";
  const identityValues = [
    clean(base?.name),
    clean(base?.street),
    clean(base?.id),
  ]
    .filter((value): value is string => Boolean(value && value.length >= 4))
    .map((value) => value.toLowerCase());
  const contentMatches = identityValues.some((value) => bodyText.includes(value));
  const sharpLaunchUrl = avisonYoungAbsoluteUrl(base?.sharpLaunchUrl);
  let requestedSharpLaunchMatch = false;
  if (requested && sharpLaunchUrl) {
    try {
      const requestedParsed = new URL(requested);
      const sharpLaunchParsed = new URL(sharpLaunchUrl);
      const requestedOnSharpLaunchHost =
        requestedParsed.protocol === "https:"
        && requestedParsed.hostname.toLowerCase().endsWith(".sharplaunch.com");
      requestedSharpLaunchMatch =
        requestedOnSharpLaunchHost
        && requestedParsed.hostname.toLowerCase()
          === sharpLaunchParsed.hostname.toLowerCase()
        && requestedParsed.pathname.replace(/\/+$/, "").toLowerCase()
          === sharpLaunchParsed.pathname.replace(/\/+$/, "").toLowerCase();
    } catch {
      requestedSharpLaunchMatch = false;
    }
  }

  const normalizeIdentityText = (value: any): string =>
    (clean(value) ?? "")
      .toLowerCase()
      .replace(/\broad\b/g, "rd")
      .replace(/\bavenue\b/g, "ave")
      .replace(/\bboulevard\b/g, "blvd")
      .replace(/[^a-z0-9]+/g, " ")
      .trim();
  const decodeHtmlAmpForComparison = (value: any): string =>
    (clean(value) ?? "").replace(/&amp;/gi, "&");
  const postDirectionals: Record<string, string> = {
    north: "n", northeast: "ne", east: "e", southeast: "se",
    south: "s", southwest: "sw", west: "w", northwest: "nw",
  };
  const normalizeStreet = (value: any): string => {
    const text = normalizeIdentityText(value).replace(/\bhighway\b/g, "hwy").replace(/\bdrive\b/g, "dr")
      // An evidenced numbered interstate may omit only its freeway descriptor.
      .replace(/\b(i \d+) (?:fwy|freeway)\b/g, "$1");
    // Only a terminal street postdirection; do not combine opposing words.
    return /\b(?:north|south|east|west) (?:north|south|east|west)$/.test(text)
      || /\b(?:unit|suite|ste|apt|floor|fl|building|bldg|lot) .+$/.test(text)
      || /#\s*\S/.test(clean(value) ?? "") ? text
      : text.replace(/\b(north|northeast|east|southeast|south|southwest|west|northwest)$/, (word) => postDirectionals[word]!);
  };
  const normalizeCity = (value: any): string =>
    normalizeIdentityText(value).replace(/^st\b/, "saint");
  // A property named by its street uses street comparison, never a city alias.
  const normalizeName = normalizeStreet(base?.name) === normalizeStreet(base?.street)
    ? normalizeStreet : normalizeIdentityText;
  const nativeInfo = $(".building__property-info-box");
  const nativeInfoRows = nativeInfo.find("li").filter((_index, element) => {
    const values = $(element).find("span").map((_spanIndex, span) =>
      clean($(span).text())
    ).get().filter(Boolean);
    return values.length >= 2;
  });
  const headingText = $("h1, h2").map((_index, element) =>
    clean($(element).text())?.toLowerCase() ?? ""
  ).get();
  // Captured native/wrapper variants separate the property name from its
  // location. Bind each field within its own identity container; script/map
  // substrings are not street evidence for this layout.
  const sameIdentityUrl = (value: any, expected: any): boolean => {
    try {
      const left = new URL(String(value));
      const right = new URL(String(expected));
      return left.protocol === "https:" && right.protocol === "https:"
        && !left.username && !left.password && !right.username && !right.password
        && left.origin === right.origin
        && left.pathname.replace(/\/+$/, "") === right.pathname.replace(/\/+$/, "")
        && left.search === right.search && left.hash === right.hash;
    } catch { return false; }
  };
  const stateNames = "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming".split("|");
  const stateCodes = "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".toLowerCase().split(" ");
  const normalizeState = (value: any): string => {
    const text = normalizeIdentityText(value);
    const index = stateNames.findIndex((name) => normalizeIdentityText(name) === text);
    return index >= 0 ? stateCodes[index]! : stateCodes.includes(text) ? text : "";
  };
  const name = normalizeName(base?.name);
  const knownNames = [...new Set([name, normalizeName(base?.headline)].filter(Boolean))];
  const subjectMatches = (value: any): boolean => knownNames.includes(normalizeName(value));
  const street = normalizeStreet(base?.street);
  const city = normalizeCity(base?.city);
  const state = normalizeState(base?.state);
  const matchesStateLocation = (value: string): boolean => {
    const text = value.trim();
    const withZip = /^(.*?)\s+(\d{5})$/.exec(text);
    return normalizeState(withZip ? withZip[1] : text) === state
      && (!withZip || !clean(base?.postalCode) || withZip[2] === clean(base?.postalCode));
  };
  const heroLocations = $(".hero__sub-title").map((_index, element) => {
    const parts = (clean($(element).text()) ?? "").split(",");
    return parts.length === 2
      && normalizeCity(parts[0]) === city
      && matchesStateLocation(parts[1]!);
  }).get();
  const heroIdentityParts = $(".hero__sub-title").text().split("|");
  const heroAddressParts = heroIdentityParts[1]?.split(",") ?? [];
  const combinedHeroIdentity = $(".hero__sub-title").length === 1
    && heroIdentityParts.length === 2 && normalizeName(heroIdentityParts[0]) === name
    && heroAddressParts.length === 3 && normalizeStreet(heroAddressParts[0]) === street
    && normalizeCity(heroAddressParts[1]) === city && matchesStateLocation(heroAddressParts[2]!);
  // These observed hero forms still need the complete visible identity witness:
  // an address hero plus matching H1/metadata, or a named H2 below.
  const addressHeroParts = $(".hero__sub-title").text().split(",");
  const addressHeroIdentity = $(".hero__sub-title").length === 1
    && addressHeroParts.length === 3 && normalizeStreet(addressHeroParts[0]) === street
    && normalizeCity(addressHeroParts[1]) === city && matchesStateLocation(addressHeroParts[2]!);
  const heroText = clean($(".hero__sub-title").text()) ?? "";
  const strictLocalityText = (value: any): boolean => {
    const text = clean(value);
    return Boolean(text && /^[\p{L}\p{M}]+(?:[ .'-][\p{L}\p{M}]+)*\.?$/u.test(text)
      && !/\b(?:suite|ste|unit|apt|floor|building|bldg|lot|nwc|nec|swc|sec)\b/i.test(text));
  };
  const strictFullAddress = (value: any): boolean => {
    if (!/^\d{5}$/.test(clean(base?.postalCode) ?? "")) return false;
    const parts = (clean(value) ?? "").split(",").map((part) => part.trim());
    const region = /^(.*?)\s+(\d{5})$/.exec(parts[2] ?? "");
    return parts.length === 3 && strictPhysicalStreetMatches(parts[0], base?.street)
      && strictLocalityText(parts[1]) && normalizeCity(parts[1]) === city && region !== null
      && strictLocalityText(region[1]) && normalizeState(region[1]) === state
      && region[2] === clean(base?.postalCode);
  };
  const strictFullAddressHero = (): boolean => $(".hero__sub-title").length === 1
    && strictFullAddress(heroText);
  const strictLocalityHero = (requireOffering: boolean): boolean => {
    if ($(".hero__sub-title").length !== 1) return false;
    const match = /^([^,]+),\s*(.+?)(?:\s+-\s+(.+))?$/.exec(heroText);
    const suffix = match?.[3]?.toLowerCase().replace(/\s+/g, " ");
    const region = /^(.*?)\s+(\d{5})$/.exec(match?.[2]?.trim() ?? "");
    const acceptedOfferings = new Set([
      "available for sale", "available for sale or lease", "available for lease", "available for lease or sale",
      "for sale", "for sale or lease", "for lease", "for lease or sale",
    ]);
    if (!match || !strictLocalityText(match[1]) || !strictLocalityText(region?.[1] ?? match[2])
      || (suffix && !acceptedOfferings.has(suffix)) || (requireOffering && !suffix)) return false;
    return normalizeCity(match[1]) === city && normalizeState(region?.[1] ?? match[2]) === state
      && (!region || region[2] === clean(base?.postalCode));
  };
  const strictStreetOfferingHeading = (): boolean => {
    const headings = $("h1").map((_index, element) => $(element).text()).get();
    return headings.length > 0 && headings.every((value) => {
      const match = /^(.*?)\s+\|\s+(?:available\s+)?for\s+(sale(?:\s+or\s+lease)?|lease(?:\s+or\s+sale)?)$/i
        .exec(clean(value) ?? "");
      return Boolean(match && strictPhysicalStreetMatches(match[1], base?.street));
    });
  };
  const strictAcreageOfferingHeading = (): boolean => {
    const headings = $("h1").map((_index, element) => $(element).text()).get();
    return headings.length > 0 && headings.every((value) =>
      /^[±~]?\s*[1-9]\d{0,2}(?:,\d{3})*(?:\.\d+)?\s+acres?\s+for\s+(?:sale(?:\s+or\s+lease)?|lease(?:\s+or\s+sale)?)$/i
        .test(clean(value) ?? "")
    );
  };
  const namedHeroParts = heroIdentityParts.length === 2
    ? [heroIdentityParts[0], ...(heroIdentityParts[1]?.split(",") ?? [])]
    : heroText.split(",");
  const namedHeroIdentity = $(".hero__sub-title").length === 1
    && namedHeroParts.length >= 3 && heroIdentityParts.length <= 2
    && subjectMatches(namedHeroParts.slice(0, -2).join(","))
    && normalizeCity(namedHeroParts.at(-2)) === city
    && matchesStateLocation(namedHeroParts.at(-1) ?? "");
  const pageTitles = [$("title").first().text(), $('meta[property="og:title"]').first().attr("content")];
  const exactFeedSubject = (value: any): boolean => {
    const text = clean(value)?.replace(/\s+/g, " ").toLowerCase();
    return Boolean(text && [base?.name, base?.headline].some((candidate) =>
      clean(candidate)?.replace(/\s+/g, " ").toLowerCase() === text
    ));
  };
  const strictPhysicalStreetKey = (value: any): string[] | null => {
    let text = clean(value);
    if (!text) return null;
    // Compare a complete provider address as a street only when its terminal
    // locality agrees with this row's separately bound fields. Keep raw values.
    const parts = text.split(",").map((part) => part.trim());
    const region = /^(.*?)\s+(\d{5})$/.exec(parts.at(-1) ?? "");
    if (parts.length === 3 && region && strictLocalityText(base?.city) && state
      && /^\d{5}$/.test(clean(base?.postalCode) ?? "")
      && strictLocalityText(parts[1]) && normalizeCity(parts[1]) === city
      && strictLocalityText(region[1]) && normalizeState(region[1]) === state
      && region[2] === clean(base?.postalCode)) {
      text = parts[0]!;
    }
    if (!text || /[&#,/@|]/.test(text)
      || /\b(?:and|at|nwc|nec|swc|sec|suite|ste|unit|apt|floor|building|bldg|lot)\b/i.test(text)
      || /^\d+\s*[-–]\s*\d+\b/.test(text)) return null;
    return text.toLowerCase().replace(/\s+/g, " ").trim().split(" ").map((token) => token.replace(/\.$/, ""));
  };
  const strictPhysicalStreetMatches = (left: any, right: any): boolean => {
    const leftTokens = strictPhysicalStreetKey(left);
    const rightTokens = strictPhysicalStreetKey(right);
    if (!leftTokens || !rightTokens || leftTokens.length !== rightTokens.length) return false;
    const directionalAliases = new Set(["n|north", "s|south", "e|east", "w|west", "ne|northeast", "nw|northwest", "se|southeast", "sw|southwest"]);
    const suffixAliases = new Set(["st|street", "rd|road", "ave|avenue", "blvd|boulevard", "highway|hwy", "dr|drive", "ln|lane", "ct|court", "parkway|pkwy"]);
    return leftTokens.every((token, index) => {
      const other = rightTokens[index];
      if (token === other) return true;
      const pair = [token, other].sort().join("|");
      return (index === 1 && /^\d+$/.test(leftTokens[0]!) && directionalAliases.has(pair))
        || (index === leftTokens.length - 1 && suffixAliases.has(pair));
    });
  };
  const titleMatches = (value: any, subjects = knownNames): boolean => {
    if (subjects.includes(normalizeName(value))) return true;
    // Existing aliases keep token counts. Separate the known name field before
    // comparing city/state so a street postdirection remains terminal.
    const words = (clean(value) ?? "").toLowerCase().replace(/#\s+/g, "#")
      .replace(/[^a-z0-9#]+/g, " ").trim().split(/\s+/);
    return subjects.some((subject) => {
      const nameWords = subject.split(" ").length;
      const location = normalizeCity(words.slice(nameWords).join(" "));
      return normalizeName(words.slice(0, nameWords).join(" ")) === subject
        && location.startsWith(city + " ") && normalizeState(location.slice(city.length + 1)) === state;
    });
  };
  const canonicalUrls = $('link[rel="canonical"]').map((_index, element) => $(element).attr("href")).get();
  const ownedIdentityMetadata = Boolean(name && street && city && state)
    && $("title").length === 1 && $('meta[property="og:title"]').length === 1
    && pageTitles.every((value) => titleMatches(value))
    && $('meta[property="og:url"]').length === 1
    && sameIdentityUrl($('meta[property="og:url"]').attr("content"), requested)
    && canonicalUrls.every((value) => sameIdentityUrl(value, requested))
    && [doc.metadata?.sourceURL, doc.metadata?.url].filter(Boolean)
      .every((value) => sameIdentityUrl(value, requested));
  const identityMetadataMatches = ownedIdentityMetadata && $("h1").length === 1
    && ((normalizeName($("h1").text()) === name
      && ((heroLocations.length === 1 && heroLocations[0] === true) || addressHeroIdentity))
      || combinedHeroIdentity);
  const capturedBuildingStructure = nativeInfo.length > 0 && nativeInfoRows.length > 0
    && $(".hero__text .hero__title").length === 1 && heroLocations.length > 0;
  // A marketing heading is not property-name authority. These native shapes
  // instead bind the complete visible address, or a visible feed name plus an
  // exact full-range description; neither equates a range with its endpoint.
  const offeringPropertyFamily = "(?:hospitality|industrial|land|multifamily|office|retail|special purpose|build to suit)";
  const offeringHeading = new RegExp("^" + offeringPropertyFamily
    + "(?: (?:and )?" + offeringPropertyFamily + "){0,2} for (?:sale(?: or lease)?|lease(?: or sale)?)$")
    .test(normalizeIdentityText($("h1").text()));
  const matchesAddressHeading = (value: string, expectedStreet: string): boolean => {
    const parts = value.split(",");
    const region = parts.pop();
    const locality = parts.pop();
    return parts.length > 0 && normalizeStreet(parts.join(",")) === normalizeStreet(expectedStreet)
      && normalizeCity(locality) === city && matchesStateLocation(region ?? "");
  };
  const descriptionSelectors = [
    'meta[name="description"]', 'meta[name="twitter:description"]',
    'meta[property="og:description"]',
  ];
  const descriptionNodes = descriptionSelectors.flatMap((selector) => $(selector).get());
  const descriptionMatches = (element: typeof descriptionNodes[number], allowAddressOnly = false): boolean => {
    const value = clean($(element).attr("content")) ?? "";
    if (value.length > 1000) return false;
    const fields = value.replace(/^for (?:sale|lease)\s*-\s*/i, "").split(",");
    const location = /^(.*?)\s+(\d{5}(?:-\d{4})?)$/.exec(fields.pop()?.trim() ?? "");
    const locality = fields.pop();
    // Feed names/streets can themselves contain commas. Require exactly one
    // boundary that matches both complete known fields; never drop tokens.
    const completePrefix = fields.join(",");
    const matches = (allowAddressOnly || subjectMatches(completePrefix))
      && normalizeStreet(completePrefix) === street ? 1
      : fields.slice(1).filter((_field, index) =>
        subjectMatches(fields.slice(0, index + 1).join(","))
        && normalizeStreet(fields.slice(index + 1).join(",")) === street
      ).length;
    return matches === 1 && normalizeCity(locality) === city
      && location !== null && normalizeState(location[1]) === state
      && (!clean(base?.postalCode) || location[2] === clean(base?.postalCode));
  };
  let isRegisteredOfficialWrapper = false;
  try {
    const parsed = new URL(requested ?? "");
    isRegisteredOfficialWrapper = parsed.origin === AVISON_YOUNG_HOST
      && parsed.pathname.startsWith("/properties/")
      && sameIdentityUrl(requested, base?.externalUrl);
  } catch { /* no registered official URL */ }
  const records = jsonLdObjects(doc.rawHtml).filter((object) =>
    [object["@type"]].flat().some((type) => String(type).toLowerCase() === "realestatelisting")
  );
  const record = records[0];
  const addresses = [record?.address, record?.propertyListing?.address].filter(Boolean);
  const structuredIdentityMatches = records.length === 1
    && subjectMatches(record?.name)
    && sameIdentityUrl(record?.url, requested)
    && addresses.length > 0 && addresses.every((address) =>
      normalizeStreet(address.streetAddress) === street
      && normalizeCity(address.addressLocality) === city
      && normalizeState(address.addressRegion) === state
      && (!clean(address.postalCode) || !clean(base?.postalCode)
        || clean(address.postalCode) === clean(base?.postalCode)));
  const descriptionIdentity = descriptionNodes.length > 0
    && descriptionSelectors.every((selector) => $(selector).length <= 1)
    && descriptionNodes.every((element) => descriptionMatches(element));
  // The original exact-name visible family permits address-only SEO drift.
  // A separately named subject/street tuple is an explicit identity claim;
  // newly admitted headline aliases must also agree with address-only claims.
  const usesHeadlineAlias = !pageTitles.every((value) => titleMatches(value, [name]))
    || (listing && subjectMatches(listing.name) && normalizeName(listing.name) !== name);
  const descriptionConflicts = descriptionNodes.some((element) => {
    const value = clean($(element).attr("content")) ?? "";
    if (!/^for (?:sale|lease)\s*-\s*/i.test(value)) return false;
    // Bind terminal city/state fields and the complete prefix before deciding
    // its role: commas can belong to the known street or property name.
    const fields = value.replace(/^for (?:sale|lease)\s*-\s*/i, "").split(",");
    const identityFields = fields.slice(0, -2);
    const completePrefix = identityFields.join(",");
    const addressOnly = normalizeStreet(completePrefix) === street;
    const knownSubject = identityFields.some((_field, index) =>
      subjectMatches(identityFields.slice(0, index + 1).join(","))
    );
    const region = (fields.at(-1) ?? "").replace(/\s+\d{5}(?:-\d{4})?$/, "");
    if (!knownSubject && !addressOnly && !normalizeState(region)) return false;
    if (identityFields.length >= 2 && !addressOnly) return !descriptionMatches(element);
    return Boolean(usesHeadlineAlias) && identityFields.length >= 1
      && !descriptionMatches(element, true);
  });
  const completeDescriptionIdentity = /^\d{5}$/.test(clean(base?.postalCode) ?? "") && descriptionIdentity;
  const rangeDescriptionIdentity = /^\d+\s*-\s*\d+\s+.+$/.test(clean(base?.street) ?? "")
    && completeDescriptionIdentity;
  // One native layout shortens only the terminal street descriptor in its
  // hero. Permit that display role only when the separate complete known-ZIP
  // description and owned metadata bind the entire ordered feed range.
  const nativeTerminalDescriptorOmission = (() => {
    const rawStreet = clean(base?.street) ?? "";
    const range = /^(\d+)\s*-\s*(\d+)\s+(.+?)\s+(?:boulevard|blvd)$/i.exec(rawStreet);
    if (!range || Number(range[2]) <= Number(range[1]) || !/^\d{5}$/.test(clean(base?.postalCode) ?? "")
      || !completeDescriptionIdentity || !ownedIdentityMetadata || !offeringHeading
      || $("h1").length !== 1 || $(".hero__sub-title").length !== 1
      || /[&#/]|\b(?:and|at|nwc|nec|swc|sec|suite|ste|unit|apt|floor|building|bldg|lot)\b/i.test(rawStreet)) return false;
    const parts = $(".hero__sub-title").text().split(",");
    const terminalDescriptor = /\s+(?:boulevard|blvd)$/i;
    const displayRange = /^(\d+)\s*-\s*(\d+)\s+(.+)$/.exec(clean(parts[0]) ?? "");
    const expectedStem = range[3]!.trim().toLowerCase().replace(/\s+/g, " ");
    const displayedStem = displayRange?.[3]?.trim().toLowerCase().replace(/\s+/g, " ");
    return parts.length === 3 && terminalDescriptor.test(rawStreet)
      && displayRange?.[1] === range[1] && displayRange[2] === range[2] && displayedStem === expectedStem
      && normalizeCity(parts[1]) === city && matchesStateLocation(parts[2]!);
  })();
  const visibleIdentityStreet = rangeDescriptionIdentity && !addressHeroIdentity ? normalizeStreet(name) : street;
  const propertySections = $(".page-content section");
  const hasAvailabilityFields = propertySections.find(".availability-card-v2 .availability-card-info-item").toArray().some((element) =>
    clean($(element).find(".availability-card-info-item-title").text())
    && clean($(element).find(".availability-card-info-item-value").text())
  );
  const hasPropertySections = hasAvailabilityFields && propertySections.find("h2").toArray().some((element) =>
    /^(?:property |building )?highlights$/i.test(clean($(element).text()) ?? "")
  );
  const building = $(".page-content > section#building .building");
  const hasBuildingOverview = building.find(".building__overview-text p, .building__overview-text li")
    .toArray().some((element) => clean($(element).text()));
  const hasBuildingFacts = building.find(".building__property-features-boxes .property-features-box > .px-body-2")
    .toArray().some((element) => clean($(element).text()));
  const hasBuildingContent = hasBuildingOverview && (hasBuildingFacts || hasAvailabilityFields);
  const hasBuildingSections = hasBuildingContent
    && (completeDescriptionIdentity || (identityMetadataMatches && addressHeroIdentity));
  // A one-way scope description is not general endpoint/range equivalence.
  // It needs an independently complete registered official physical anchor.
  const officialPhysicalAnchor = isRegisteredOfficialWrapper && urlMatches && ownedIdentityMetadata
    && structuredIdentityMatches && addressHeroIdentity && /^\d{5}$/.test(clean(base?.postalCode) ?? "")
    && /\s\d{5}$/.test(addressHeroParts[2]?.trim() ?? "")
    && addresses.every((address) => clean(address.postalCode) === clean(base?.postalCode));
  const streetRole = /\b(?:street|st|road|rd|avenue|ave|boulevard|blvd|highway|hwy|drive|dr|lane|ln|court|ct|parkway|pkwy)\b/i;
  const strictDescriptionMatches = (description: any): boolean => {
    const value = (clean(description) ?? "").replace(/^for (?:sale|lease)\s*-\s*/i, "");
    if (!value || value.length > 1000) return false;
    const fields = value.split(",").map((field) => field.trim());
    const region = /^(.*?)\s+(\d{5})$/.exec(fields.pop() ?? "");
    const locality = fields.pop();
    if (!region || !locality || !strictLocalityText(locality) || !strictLocalityText(region[1])
      || normalizeCity(locality) !== city || normalizeState(region[1]) !== state
      || region[2] !== clean(base?.postalCode)) return false;
    const matchingBoundaries = fields.map((_field, index) => {
      const possibleSubject = fields.slice(0, index).join(",").trim();
      const possibleStreet = fields.slice(index).join(",").trim();
      return strictPhysicalStreetMatches(possibleStreet, base?.street)
        && (!possibleSubject || exactFeedSubject(possibleSubject));
    }).filter(Boolean);
    return matchingBoundaries.length === 1;
  };
  const strictPhysicalDescriptionAgrees = (value: any): boolean => {
    const fields = (clean(value) ?? "").split(",");
    // Only a raw street-role or exact street tuple is an address claim. Prices
    // and area quantities in otherwise ordinary marketing copy are not.
    const region = /^(.*?)\s+(\d[\d-]*)$/.exec(fields.at(-1)?.trim() ?? "");
    const claimsAddress = fields.length >= 3 && region
      && (normalizeState(region[1]) || /^[a-z]{2}$/i.test(region[1]!))
      && fields.slice(0, -2).some((field) => /^\d+\b/.test(field)
        || /\b(?:street|st|road|rd|avenue|ave|boulevard|blvd|highway|hwy|drive|dr|lane|ln|court|ct|parkway|pkwy|place|pl|way|trail|trl|circle|cir)\b/i.test(field)
        || strictPhysicalStreetMatches(field, base?.street));
    return !claimsAddress || strictDescriptionMatches(value);
  };
  const strictPhysicalDescriptionsAgree = (): boolean => descriptionSelectors.every((selector) => $(selector).length <= 1)
    && descriptionNodes.every((element) => strictPhysicalDescriptionAgrees($(element).attr("content")));
  const ambiguousScopeRole = /[&/#]|\b(?:and|at|nwc|nec|swc|sec|suite|ste|unit|apt|floor|building|bldg|lot)\b/i;
  const endpoint = ambiguousScopeRole.test(clean(base?.street) ?? "") ? null : /^(\d+) ([a-z ]+)$/.exec(street);
  const scopeRanges = descriptionNodes.map((element) => {
    const value = clean($(element).attr("content")) ?? "";
    if (!value || value.length > 1000 || !endpoint) return null;
    const fields = value.replace(/^for (?:sale|lease)\s*-\s*/i, "").split(",");
    const region = /^(.*?)\s+(\d{5})$/.exec(fields.pop()?.trim() ?? "");
    const locality = fields.pop();
    const boundaries = fields.slice(1).map((_field, index) => ({
      subject: fields.slice(0, index + 1).join(","),
      range: /^(\d+)\s*[-–]\s*(\d+)\s+(.+)$/.exec(fields.slice(index + 1).join(",").trim()),
    })).filter(({ subject, range }) => subjectMatches(subject) && range && !ambiguousScopeRole.test(range[3]!)
      && range[1] === endpoint[1] && Number.isSafeInteger(Number(range[2]))
      && Number(range[2]) > Number(range[1]) && normalizeStreet(range[3]) === endpoint[2]);
    return boundaries.length === 1 && normalizeCity(locality) === city && region
      && normalizeState(region[1]) === state && region[2] === clean(base?.postalCode)
      ? boundaries[0]!.range!.slice(1).map(normalizeStreet).join("|") : null;
  });
  const officialRangeScopeWitness = officialPhysicalAnchor && hasPropertySections
    && $("h1").length === 1 && subjectMatches($("h1").text())
    && endpoint !== null
    && descriptionNodes.length > 0 && descriptionSelectors.every((selector) => $(selector).length <= 1)
    && scopeRanges.every((range) => range !== null) && new Set(scopeRanges).size === 1
    && $("h2").toArray().filter((element) => /^\d+(?:\s*[-–]\s*\d+)?\s+/.test(clean($(element).text()) ?? "") && streetRole.test($(element).text()))
      .every((element) => normalizeStreet($(element).text()) === street || matchesAddressHeading($(element).text(), street));
  const feedUrlKey = avisonYoungOfficialUrlKey(base?.externalUrl);
  const exactFeedUrlOwner = Boolean(feedUrlKey && officialFeedIdentity
    && officialFeedIdentity.matchingExternalUrlRowIds.length === 1
    && officialFeedIdentity.matchingExternalUrlRowIds[0] === clean(base?.id)
    && sameIdentityUrl(requested, base?.externalUrl));
  const strictStructuredAddresses = addresses.length > 0
    && /^\d{5}$/.test(clean(base?.postalCode) ?? "")
    && addresses.every((address) => strictPhysicalStreetMatches(address.streetAddress, base?.street)
      && strictLocalityText(address.addressLocality)
      && normalizeCity(address.addressLocality) === city
      && strictLocalityText(address.addressRegion)
      && normalizeState(address.addressRegion) === state
      && clean(address.postalCode) === clean(base?.postalCode));
  const exactOwnedMetadata = ownedIdentityMetadata;
  const h1Values = $("h1").map((_index, element) => $(element).text()).get();
  const subjectHeading = h1Values.length > 0 && h1Values.every(exactFeedSubject);
  const exactSubjectRecord = records.length === 1 && exactFeedSubject(record?.name)
    && sameIdentityUrl(record?.url, requested);
  const physicalSubjectClaimAgrees = (value: any, expectedStreet = base?.street): boolean => {
    const text = clean(value);
    if (!text) return false;
    const streetOffering = /^(.*?)\s+\|\s+(?:available\s+)?for\s+(?:sale(?:\s+or\s+lease)?|lease(?:\s+or\s+sale)?)$/i.exec(text);
    const offerPrefix = /^(?:available\s+)?for\s+(?:sale(?:\s+or\s+lease)?|lease(?:\s+or\s+sale)?)\s*\|\s*(.+)$/i.exec(text);
    const candidate = streetOffering?.[1] ?? offerPrefix?.[1] ?? text;
    const candidateParts = candidate.split(",").map((part) => part.trim());
    const streetField = candidateParts[0] ?? candidate;
    const addressShaped = /^\d+\b/.test(streetField)
      || /\b(?:street|st|road|rd|avenue|ave|boulevard|blvd|highway|hwy|drive|dr|lane|ln|court|ct|parkway|pkwy|place|pl|way|trail|trl|circle|cir)\b/i.test(streetField)
      || Boolean(streetOffering || offerPrefix) && strictPhysicalStreetMatches(streetField, expectedStreet);
    if (!addressShaped) return true;
    if (!strictPhysicalStreetMatches(streetField, expectedStreet)) return false;
    const locality = candidateParts.slice(1);
    if (locality.length === 0) return true;
    if (locality.length > 3 || !strictLocalityText(locality[0]) || normalizeCity(locality[0]) !== city) return false;
    if (locality.length === 1) return true;
    const stateZip = /^(.*?)\s+(\d{5})$/.exec(locality[1] ?? "");
    const suppliedState = stateZip?.[1] ?? locality[1];
    if (!strictLocalityText(suppliedState) || normalizeState(suppliedState) !== state) return false;
    if (stateZip) return stateZip[2] === clean(base?.postalCode) && locality.length === 2;
    return locality.length === 2
      || (locality.length === 3 && /^\d{5}$/.test(locality[2] ?? "") && locality[2] === clean(base?.postalCode));
  };
  const physicalSubjectClaimsAgree = [
    ...pageTitles,
    ...records.map((value) => value?.name),
    ...h1Values.filter(exactFeedSubject),
    ...h1Values.filter((value) => /\s+\|\s+(?:available\s+)?for\s+(?:sale|lease)/i.test(clean(value) ?? "")),
  ].every((value) => physicalSubjectClaimAgrees(value));
  const uniqueExactSubjectHeadings = subjectHeading
    && new Set(h1Values.map((value) => clean(value)?.replace(/\s+/g, " ").toLowerCase())).size === 1;
  const substantiveStructure = (nativeInfo.length > 0 && nativeInfoRows.length > 0)
    || hasPropertySections || hasBuildingContent;
  // Some registered named properties repeat their name in the feed/structured
  // street field. Only the publisher's linked, explicitly labeled offering
  // address can associate a physical hero with that otherwise exact subject.
  // This witness validates the association; it never rewrites the feed street.
  const officialNamedPropertyIdentity = (() => {
    const subject = clean(base?.name)?.replace(/\s+/g, " ").toLowerCase() ?? "";
    const sameSubject = (value: any): boolean => clean(value)?.replace(/\s+/g, " ").toLowerCase() === subject;
    if (!isRegisteredOfficialWrapper || !exactFeedUrlOwner || !ownedIdentityMetadata
      || !subject || /\d|[&#,/@|]|\b(?:suite|ste|unit|apt|floor|lot|nwc|nec|swc|sec)\b/i.test(subject)
      || streetRole.test(subject) || !sameSubject(base?.street)
      || (clean(base?.headline) && !sameSubject(base.headline))
      || !strictLocalityText(base?.city) || !strictLocalityText(base?.state)
      || !pageTitles.every(sameSubject) || $("h1").length !== 1 || !sameSubject($("h1").text())
      || canonicalUrls.length !== 1 || records.length !== 1 || !sameSubject(record?.name)
      || !structuredIdentityMatches || !/^\d{5}$/.test(clean(base?.postalCode) ?? "")
      || !addresses.every((address) => sameSubject(address.streetAddress)
        && strictLocalityText(address.addressLocality) && strictLocalityText(address.addressRegion)
        && clean(address.postalCode) === clean(base?.postalCode))) return false;
    const remoteIds = [...doc.rawHtml.matchAll(/\bremote\s*:\s*(\d+)\b/g)];
    const remoteComments = $("script").toArray().filter((element) =>
      /^\s*\/\*\s*remote\s*:\s*\d+\b[^*]*\*\/\s*$/.test($(element).text()));
    if (remoteIds.length !== 1 || remoteIds[0]![1] !== clean(base?.id) || remoteComments.length !== 1) return false;
    const labeledRows = $("li").toArray().filter((element) =>
      /^address$/i.test(clean($(element).children("span").first().text()) ?? ""));
    if (labeledRows.length !== 1) return false;
    const addressRow = $(labeledRows[0]!);
    const summary = addressRow.closest(".building__property-info-box");
    if (summary.closest(".page-content > section#building .building").length !== 1 || summary.length !== 1
      || !/^offering summary$/i.test(clean(summary.prev("h2.building__property-info-title").text()) ?? "")
      || addressRow.children("span").length !== 2 || !hasBuildingOverview) return false;
    const offeredAddress = clean(addressRow.children("span").last().text()) ?? "";
    const offeredStreet = offeredAddress.split(",")[0]?.trim() ?? "";
    if (!/^[1-9]\d*\s+[\p{L}]/u.test(offeredStreet) || !strictPhysicalStreetKey(offeredStreet)) return false;
    const sameOfferingAddress = (value: any): boolean => {
      const parts = (clean(value) ?? "").split(",").map((part) => part.trim());
      const region = /^(.*?)(?:\s+(\d{5}))?$/.exec(parts[2] ?? "");
      return parts.length === 3 && strictPhysicalStreetMatches(parts[0], offeredStreet)
        && strictLocalityText(parts[1]) && normalizeCity(parts[1]) === city
        && strictLocalityText(region?.[1]) && normalizeState(region?.[1]) === state
        && (!region?.[2] || region[2] === clean(base?.postalCode));
    };
    if (!sameOfferingAddress(offeredAddress) || $(".hero__sub-title").length !== 1
      || !sameOfferingAddress(heroText)) return false;
    const physicalHeadings = $("h2").toArray().filter((element) => /^\d/.test(clean($(element).text()) ?? ""));
    if (!physicalHeadings.every((element) => sameOfferingAddress($(element).text()))) return false;
    // Location prose in Property Highlights is not an offered address. Explicit
    // description tuples and map POI addresses remain contradictory claims.
    if (!descriptionSelectors.every((selector) => $(selector).length <= 1)
      || !descriptionNodes.every((element) => {
        const value = clean($(element).attr("content")) ?? "";
        const parts = value.split(",");
        const physicalClaim = sameSubject(parts[0]) ? parts.slice(1).join(",").trim() : value;
        if (/^\d/.test(physicalClaim) && streetRole.test(physicalClaim))
          return physicalSubjectClaimAgrees(physicalClaim, offeredStreet);
        const claimsAddress = parts.length >= 3 && parts.slice(0, -2).some((part) =>
          /^\d/.test(part.trim()) || streetRole.test(part));
        if (!claimsAddress) return true;
        return sameOfferingAddress(value) || (sameSubject(parts[0]) && sameOfferingAddress(parts.slice(1).join(",")));
      })) return false;
    for (const script of $("script").toArray()) {
      const text = $(script).text();
      const mapClaims = [...text.matchAll(/(?:\bpoi|"poi"|'poi')\s*:/g)];
      const maps = [...text.matchAll(/(?:\bpoi|"poi"|'poi')\s*:\s*(\[[^\n]*\])/g)];
      if (maps.length !== mapClaims.length) return false;
      for (const map of maps) {
        try {
          const points = JSON.parse(map[1]!);
          if (!Array.isArray(points) || !points.every((point) =>
            strictPhysicalStreetMatches(point?.address, offeredStreet)
            && physicalSubjectClaimAgrees(point?.name, offeredStreet))) return false;
        } catch { return false; }
      }
    }
    return true;
  })();

  // Parse each source-owned map claim at its value offset. Map initialization
  // may be explicitly absent (`null` or `[]`), but malformed values and every
  // populated point still have to agree with the already-bound feed identity.
  const readPoiClaims = (): Array<{ kind: "null" } | { kind: "array"; points: any[] }> | null => {
    const claims: Array<{ kind: "null" } | { kind: "array"; points: any[] }> = [];
    for (const script of $("script").toArray()) {
      const source = $(script).text();
      for (const match of source.matchAll(/(?:\bpoi|"poi"|'poi')\s*:/g)) {
        let start = match.index! + match[0].length;
        while (/\s/.test(source[start] ?? "")) start++;
        if (source.slice(start).match(/^null(?=\s*[,}])/)) {
          claims.push({ kind: "null" });
          continue;
        }
        if (source[start] !== "[") return null;
        let depth = 0;
        let inString = false;
        let escaped = false;
        let end = -1;
        for (let index = start; index < source.length; index++) {
          const character = source[index]!;
          if (inString) {
            if (escaped) escaped = false;
            else if (character === "\\") escaped = true;
            else if (character === '"') inString = false;
          } else if (character === '"') inString = true;
          else if (character === "[") depth++;
          else if (character === "]" && --depth === 0) {
            end = index + 1;
            break;
          }
        }
        if (end < 0 || !/^\s*[,}]/.test(source.slice(end))) return null;
        let points: unknown;
        try { points = JSON.parse(source.slice(start, end)); } catch { return null; }
        if (!Array.isArray(points)) return null;
        claims.push({ kind: "array", points });
      }
    }
    return claims;
  };
  const finiteFeedGeo = (): boolean => typeof base?.latitude === "number" && Number.isFinite(base.latitude)
    && typeof base?.longitude === "number" && Number.isFinite(base.longitude);
  const exactOfficialUrls = (): boolean => Boolean(exactFeedUrlOwner && urlMatches
    && sameIdentityUrl(record?.url, requested)
    && canonicalUrls.length === 1 && sameIdentityUrl(canonicalUrls[0], requested)
    && $('meta[property="og:url"]').length === 1
    && sameIdentityUrl($('meta[property="og:url"]').attr("content"), requested)
    && sameIdentityUrl(doc.metadata?.sourceURL, requested)
    && sameIdentityUrl(doc.metadata?.url, requested));
  const exactRemoteIdentity = (): boolean => {
    const remoteIds = [...doc.rawHtml.matchAll(/\bremote\s*:\s*(\d+)\b/g)];
    const remoteComments = $("script").toArray().filter((element) =>
      /^\s*\/\*\s*remote\s*:\s*\d+\b[^*]*\*\/\s*$/.test($(element).text()));
    return remoteIds.length === 1 && remoteIds[0]![1] === clean(base?.id)
      && remoteComments.length === 1 && /\bremote\s*:\s*(\d+)\b/.exec($(remoteComments[0]!).text())?.[1] === clean(base?.id);
  };
  const exactOfficialEnvelope = (): boolean => isRegisteredOfficialWrapper && exactOfficialUrls()
    && /^\d{5}$/.test(clean(base?.postalCode) ?? "")
    && exactRemoteIdentity() && records.length === 1 && addresses.length === 2
    && sameIdentityUrl(record?.url, requested)
    && normalizeIdentityText(decodeHtmlAmpForComparison(record?.name))
      === normalizeIdentityText(decodeHtmlAmpForComparison(base?.name))
    && $("h1").length === 1 && $("title").length === 1
    && $('meta[property="og:title"]').length === 1 && $(".hero__sub-title").length === 1
    && pageTitles.every((value) => normalizeIdentityText(decodeHtmlAmpForComparison(value))
      === normalizeIdentityText(decodeHtmlAmpForComparison(base?.name)));

  const officialDisplayRole = (uniqueExactSubjectHeadings
      && (strictLocalityHero(false) || strictFullAddressHero()))
    || (h1Values.length === 1 && strictPhysicalStreetMatches(h1Values[0], base?.street)
      && strictLocalityHero(false))
    || (h1Values.length === 1 && strictStreetOfferingHeading() && strictLocalityHero(false))
    || (h1Values.length === 1 && (offeringHeading || strictAcreageOfferingHeading()) && strictFullAddressHero())
    || (() => {
      // A complete physical H1 can carry identity while the hero carries only
      // purpose. This additional pairing alone requires the exact envelope.
      const purpose = new RegExp("^(?:(medical ready|dental ready) )?(" + offeringPropertyFamily
        + ") (?:space|suites) for (sale|lease)$", "i").exec(heroText);
      const sourceTypes = base?.rawSharpLaunch?.type;
      const transactions = base?.rawSharpLaunch?.transaction;
      const geo = record?.propertyListing?.geo;
      if (!purpose || !exactOfficialEnvelope() || !strictFullAddress(h1Values[0])
        || building.length !== 1 || !hasBuildingOverview || !strictStructuredAddresses
        || !finiteFeedGeo() || geo?.latitude !== base.latitude || geo?.longitude !== base.longitude
        || record?.propertyListing?.["@type"] !== "Place"
        || !Array.isArray(sourceTypes) || sourceTypes.length !== 1 || typeof sourceTypes[0] !== "string"
        || !Array.isArray(record.propertyListing.category) || record.propertyListing.category.length !== 1
        || record.propertyListing.category[0] !== sourceTypes[0]
        || sourceTypes[0].split(".")[0]?.replace(/[ _-]/g, "").toLowerCase()
          !== purpose[2]!.replace(/ /g, "").toLowerCase()
        || (purpose[1] && (purpose[2]!.toLowerCase() !== "office" || sourceTypes[0] !== "office.medical"))
        || !Array.isArray(transactions) || transactions.length !== 1 || transactions[0] !== purpose[3]!.toLowerCase()
        || clean(base?.transactionType)?.toLowerCase() !== purpose[3]!.toLowerCase()
        || clean(record?.offers?.businessFunction)?.toLowerCase() !== purpose[3]!.toLowerCase()
        || ![record?.description, record?.propertyListing?.description].every((value) =>
          value == null || (typeof value === "string" && strictPhysicalDescriptionAgrees(value)))) return false;
      const claims = readPoiClaims();
      return claims !== null && claims.every((claim) => claim.kind === "null" || claim.points.every((point) =>
        strictPhysicalStreetMatches(point?.address, base?.street) && subjectMatches(point?.name)
        && point?.lat === base.latitude && point?.lng === base.longitude));
    })();
  const officialStrictDisplayEvidence = isRegisteredOfficialWrapper && substantiveStructure
    && exactOwnedMetadata && exactSubjectRecord && strictStructuredAddresses
    && physicalSubjectClaimsAgree && structuredIdentityMatches && urlMatches
    && strictPhysicalDescriptionsAgree() && officialDisplayRole;
  const officialStrictDisplayIdentity = officialStrictDisplayEvidence && exactFeedUrlOwner;

  // The land-feed name includes a category suffix while the page heading and
  // physical address contain the same ordered two-road intersection. Decode
  // HTML entities only for this JSON-LD name comparison; raw source stays intact.
  const parseOrderedIntersection = (value: any): [string, string] | null => {
    const text = clean(value);
    if (!text || /[,/#|]/.test(text)) return null;
    const parts = text.split("&").map((part) => part.trim());
    const roadComponent = (part: string): boolean => Boolean(strictPhysicalStreetKey(part))
      && (streetRole.test(part) || /^(?:fm|us|state route|route)\s+\d+[a-z]?$/i.test(part));
    if (parts.length !== 2 || !roadComponent(parts[0]!) || !roadComponent(parts[1]!)) return null;
    return [parts[0]!, parts[1]!];
  };
  const sameOrderedIntersection = (value: any, expected: [string, string]): boolean => {
    const parts = parseOrderedIntersection(value);
    return Boolean(parts && strictPhysicalStreetMatches(parts[0], expected[0])
      && strictPhysicalStreetMatches(parts[1], expected[1]));
  };
  const sameIntersectionTitle = (value: any, expected: [string, string]): boolean => {
    const match = /^(.*?)\s+\|\s+Land$/i.exec(decodeHtmlAmpForComparison(clean(value) ?? ""));
    return Boolean(match && sameOrderedIntersection(match[1], expected));
  };
  const officialIntersectionOffering = (() => {
    const sourceName = clean(base?.name) ?? "";
    const nameParts = /^(.*?)\s+\|\s+Land$/i.exec(sourceName);
    const feedIntersection = parseOrderedIntersection(base?.street);
    const namedIntersection = parseOrderedIntersection(nameParts?.[1]);
    const sourceTypes = Array.isArray(base?.rawSharpLaunch?.type)
      ? base.rawSharpLaunch.type.map((value: any) => clean(value)?.toLowerCase()).filter(Boolean)
      : [];
    const geo = record?.propertyListing?.geo;
    const claims = readPoiClaims();
    if (!exactOfficialEnvelope() || !nameParts || !feedIntersection || !namedIntersection
      || !sameOrderedIntersection(nameParts[1], feedIntersection)
      || sourceTypes.length !== 1 || sourceTypes[0] !== "land.other"
      || (clean(base?.transactionType) ?? "").toLowerCase() !== "sale" || !finiteFeedGeo()
      || building.length !== 1 || !hasBuildingOverview || $(".hero__text .hero__title").length !== 1
      || !heroLocations.every(Boolean) || heroLocations.length !== 1
      || normalizeIdentityText(decodeHtmlAmpForComparison(record?.name)) !== normalizeIdentityText(sourceName)
      || !pageTitles.every((value) => normalizeIdentityText(decodeHtmlAmpForComparison(value))
        === normalizeIdentityText(sourceName))
      || !sameIntersectionTitle(record?.name, feedIntersection)
      || !pageTitles.every((value) => sameIntersectionTitle(value, feedIntersection))
      || !sameOrderedIntersection($("h1").text(), feedIntersection)
      || record?.propertyListing?.["@type"] !== "Place"
      || !Array.isArray(record?.propertyListing?.category)
      || record.propertyListing.category.length !== 1 || record.propertyListing.category[0] !== sourceTypes[0]
      || (clean(record?.offers?.businessFunction) ?? "").toLowerCase() !== "sale"
      || addresses.length !== 2 || !addresses.every((address) =>
        sameOrderedIntersection(address?.streetAddress, feedIntersection)
        && strictLocalityText(address?.addressLocality) && normalizeCity(address.addressLocality) === city
        && strictLocalityText(address?.addressRegion) && normalizeState(address.addressRegion) === state
        && clean(address.postalCode) === clean(base?.postalCode))
      || geo?.latitude !== base.latitude || geo?.longitude !== base.longitude
      || !claims || claims.length === 0 || claims.some((claim) => claim.kind !== "array" || claim.points.length === 0
        || claim.points.some((point) => !sameOrderedIntersection(point?.address, feedIntersection)
          || !sameIntersectionTitle(point?.name, feedIntersection)
          || point?.lat !== base.latitude || point?.lng !== base.longitude))) return false;
    return true;
  })();

  // This official office-sublease role keeps identity on the registered feed,
  // exact detail URL, typed address, and same-page structured record. The H1
  // supplies marketing purpose; the visible building overview independently
  // supplies the affirmative sublease-expiry claim.
  const officialOfficeSublease = (() => {
    const sourceTypes = Array.isArray(base?.rawSharpLaunch?.type)
      ? base.rawSharpLaunch.type.map((value: any) => clean(value)?.toLowerCase()).filter(Boolean)
      : [];
    const heading = /^sublease available in (.+?)[’']s premier office building$/i.exec(clean($("h1").text()) ?? "");
    const overviewTexts = building.find(".building__overview-text p, .building__overview-text li")
      .toArray().map((element) => clean($(element).text()) ?? "");
    const overviewClauses = overviewTexts.flatMap((text) => text.split("•").map((part) => part.trim()));
    const subleaseClauses = overviewClauses.filter((part) => /\bsublease\b/i.test(part));
    const explicitDenial = overviewClauses.some((part) =>
      /\b(?:no|never|not|prior|previous(?:ly)?|historical|former|expired|withdrawn|excluded|terminated)\b[^.!?]{0,100}\b(?:sublease|space|offering)\b/i.test(part)
      || /\b(?:sublease|space|offering)\b[^.!?]{0,100}\b(?:expired|no longer available|not available|excluded|withdrawn|terminated)\b/i.test(part));
    const affirmativeExpiry = subleaseClauses.length === 1
      && /^sublease\s+expires\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}$/i.test(subleaseClauses[0]!);
    const claims = readPoiClaims();
    const geo = record?.propertyListing?.geo;
    if (!exactOfficialEnvelope() || building.length !== 1 || !hasBuildingOverview
      || $(".hero__text .hero__title").length !== 1
      || (clean(base?.transactionType) ?? "").toLowerCase() !== "lease"
      || sourceTypes.length !== 1 || !sourceTypes[0]!.startsWith("office.")
      || !heading || normalizeCity(heading[1]) !== city || !heroLocations.every(Boolean) || heroLocations.length !== 1
      || !ownedIdentityMetadata
      || !finiteFeedGeo() || !strictStructuredAddresses || addresses.length !== 2
      || !addresses.every((address) => clean(address.postalCode) === clean(base?.postalCode))
      || record?.propertyListing?.["@type"] !== "Place"
      || !Array.isArray(record?.propertyListing?.category)
      || record.propertyListing.category.length !== 1
      || !sourceTypes.every((value: string, index: number) => record.propertyListing.category[index] === value)
      || (clean(record?.offers?.businessFunction) ?? "").toLowerCase() !== "lease"
      || geo?.latitude !== base.latitude || geo?.longitude !== base.longitude
      || !claims || explicitDenial || !affirmativeExpiry
      || claims.some((claim) => claim.kind === "array" && claim.points.some((point) =>
        !strictPhysicalStreetMatches(point?.address, base?.street)
        || !subjectMatches(point?.name) || point?.lat !== base.latitude || point?.lng !== base.longitude))) return false;
    return physicalSubjectClaimsAgree && strictPhysicalDescriptionsAgree();
  })();
  // This registered-page role keeps the feed's ordered range as the property
  // identity while allowing the publisher's endpoint marker as its locator.
  // It is intentionally independent of the older endpoint-first range scope.
  const officialRangeOfferingLocator = (() => {
    const parseRange = (value: any) => {
      const match = /^(\d+)\s*[-–]\s*(\d+)\s+(.+)$/u.exec(clean(value) ?? "");
      if (!match) return null;
      const first = Number(match[1]);
      const last = Number(match[2]);
      const rangeStreet = clean(match[3]);
      if (!Number.isSafeInteger(first) || !Number.isSafeInteger(last) || first < 1 || last <= first
        || !rangeStreet || !strictPhysicalStreetKey(rangeStreet) || !streetRole.test(rangeStreet)) return null;
      return { first, last, street: rangeStreet };
    };
    const feedRange = parseRange(base?.street);
    const feedNameRange = parseRange(base?.name);
    if (!feedRange || !feedNameRange
      || feedNameRange.first !== feedRange.first || feedNameRange.last !== feedRange.last
      || !strictPhysicalStreetMatches(feedNameRange.street, feedRange.street)) return false;
    const sameRange = (value: any): boolean => {
      const parsed = parseRange(value);
      return Boolean(parsed && parsed.first === feedRange.first && parsed.last === feedRange.last
        && strictPhysicalStreetMatches(parsed.street, feedRange.street));
    };
    const parseEndpoint = (value: any): number | null => {
      const match = /^(\d+)\s+(.+)$/u.exec(clean(value) ?? "");
      if (!match) return null;
      const number = Number(match[1]);
      return Number.isSafeInteger(number) && (number === feedRange.first || number === feedRange.last)
        && strictPhysicalStreetKey(match[2]) && streetRole.test(match[2])
        && strictPhysicalStreetMatches(match[2], feedRange.street) ? number : null;
    };
    const titles = pageTitles.map(parseEndpoint);
    if (!isRegisteredOfficialWrapper || !exactFeedUrlOwner || !urlMatches
      || !clean(base?.postalCode) || !/^\d{5}$/.test(clean(base?.postalCode) ?? "")
      || !clean(base?.city) || !clean(base?.state) || !titles.length || titles.some((value) => value === null)
      || new Set(titles).size !== 1 || $("title").length !== 1
      || $('meta[property="og:title"]').length !== 1 || $("h1").length !== 1
      || !sameRange($("h1").text()) || records.length !== 1
      || !sameIdentityUrl(record?.url, requested) || parseEndpoint(record?.name) !== titles[0]
      || addresses.length !== 2 || !addresses.every((address) => sameRange(address?.streetAddress)
        && strictLocalityText(address?.addressLocality) && normalizeCity(address.addressLocality) === city
        && strictLocalityText(address?.addressRegion) && normalizeState(address.addressRegion) === state
        && clean(address.postalCode) === clean(base?.postalCode))
      || $('meta[property="og:url"]').length !== 1
      || !sameIdentityUrl($('meta[property="og:url"]').attr("content"), requested)
      || canonicalUrls.length !== 1 || !sameIdentityUrl(canonicalUrls[0], requested)
      || !doc.metadata?.sourceURL || !doc.metadata?.url
      || !sameIdentityUrl(doc.metadata.sourceURL, requested) || !sameIdentityUrl(doc.metadata.url, requested)
      || $(".hero__sub-title").length !== 1 || heroLocations.length !== 1 || heroLocations[0] !== true) return false;

    const streetSuffix = "(?:street|st\\.?|road|rd\\.?|avenue|ave\\.?|boulevard|blvd\\.?|highway|hwy\\.?|drive|dr\\.?|lane|ln\\.?|court|ct\\.?|parkway|pkwy\\.?|place|pl\\.?|way|trail|trl\\.?|circle|cir\\.?)";
    const rangePattern = new RegExp(`\\b(\\d+)\\s*[-–]\\s*(\\d+)\\s+(.+?\\b${streetSuffix})(?=$|[,.;!?])`, "giu");
    const endpointPattern = new RegExp(`\\b(\\d+)\\s+(.+?\\b${streetSuffix})(?=$|[,.;!?])`, "giu");
    const offeringRangePattern = new RegExp(
      `\\bwe\\s+are\\s+pleased\\s+to\\s+present\\s+the\\s+offering\\s+memor(?:ad|and)um\\s+for\\s+the\\s+properties\\s+located\\s+at\\s+`
        + `(\\d+)\\s*[-–]\\s*(\\d+)\\s+(.+?\\b${streetSuffix})(?=$|[,.;!?])`,
      "giu",
    );
    const overviewTexts = building.find(".building__overview-text p, .building__overview-text li")
      .toArray().map((element) => clean($(element).text()) ?? "");
    const offerClaims = overviewTexts.flatMap((text) => [...text.matchAll(offeringRangePattern)]);
    const explicitExclusion = /\b(?:the|these)\s+properties\b[^.!?]{0,160}\b(?:are|were)\s+excluded\s+from\s+(?:this|the)\s+offering\b/iu;
    if (overviewTexts.some((text) => explicitExclusion.test(text)) || offerClaims.length !== 1
      || !sameRange(`${offerClaims[0]![1]}-${offerClaims[0]![2]} ${offerClaims[0]![3]}`)
      || !overviewTexts.some((text) => text.length > 0)) return false;

    const sourceTexts = [
      $("body").clone().find("script, style, noscript").remove().end().text(),
      ...overviewTexts,
      ...descriptionNodes.map((element) => clean($(element).attr("content")) ?? ""),
      clean(record?.description) ?? "",
    ];
    for (const text of sourceTexts) {
      for (const match of text.matchAll(rangePattern)) {
        if (!sameRange(`${match[1]}-${match[2]} ${match[3]}`)) return false;
      }
      for (const match of text.matchAll(endpointPattern)) {
        if (parseEndpoint(`${match[1]} ${match[2]}`) === null) return false;
      }
    }

    const geo = record?.propertyListing?.geo;
    if (typeof geo?.latitude !== "number" || !Number.isFinite(geo.latitude)
      || typeof geo?.longitude !== "number" || !Number.isFinite(geo.longitude)) return false;
    let poiCount = 0;
    for (const script of $("script").toArray()) {
      const text = $(script).text();
      const poiClaims = [...text.matchAll(/(?:\bpoi|"poi"|'poi')\s*:/g)];
      const maps = [...text.matchAll(/(?:\bpoi|"poi"|'poi')\s*:\s*(\[[^\n]*\])/g)];
      if (maps.length !== poiClaims.length) return false;
      for (const map of maps) {
        let points: any;
        try { points = JSON.parse(map[1]!); } catch { return false; }
        if (!Array.isArray(points) || points.length === 0) return false;
        for (const point of points) {
          poiCount++;
          if (parseEndpoint(point?.address) !== titles[0] || parseEndpoint(point?.name) !== titles[0]
            || point?.lat !== geo.latitude || point?.lng !== geo.longitude) return false;
        }
      }
    }
    if (poiCount === 0) return false;
    const remoteIds = [...doc.rawHtml.matchAll(/\bremote\s*:\s*(\d+)\b/g)];
    return remoteIds.length === 1 && remoteIds[0]![1] === clean(base?.id);
  })();

  if (descriptionConflicts && !officialRangeScopeWitness && !officialRangeOfferingLocator
    && !officialIntersectionOffering && !officialOfficeSublease) {
    throw new Error("Avison Young detail identity does not match the requested property");
  }
  if (officialRangeOfferingLocator) return record;
  if (officialStrictDisplayIdentity) return record;
  if (officialNamedPropertyIdentity) return record;
  const visiblePropertyIdentity = identityMetadataMatches && (name === street || addressHeroIdentity);
  const namedDescriptionIdentity = completeDescriptionIdentity && identityMetadataMatches
    && heroLocations.length === 1 && heroLocations[0] === true;
  // Only this stronger family treats a display name as advisory. A recognizable
  // address assertion still has to agree; unclassified numeric/unit remnants
  // refuse rather than becoming a new marketing-name exception.
  const physicalHeadingAgrees = (): boolean => {
    const heading = clean($("h1").text()) ?? "";
    if (!heading) return false;
    if (normalizeStreet(heading) === street || matchesAddressHeading(heading, street)) return true;
    const attachedUnits = [...heading.matchAll(/\b(?:suite|ste|unit)([a-z]+)\b/gi)];
    if (attachedUnits.some((unit) => unit[1]!.length === 1 || /^[A-Z]/.test(unit[1]!)
      || normalizeStreet(heading).startsWith(street + " "))) return false;
    // Observed acreage/SF marketing quantities are not street numbers. Strip
    // the complete quantity, including its evidenced range, never bare digits.
    const quantities = /[±~]?\s*\d[\d,.]*\s*(?:(?:up\s+to|to|-)\s*[±~]?\s*\d[\d,.]*\s*)?(?:rsf|sf|square feet|acres?|ac)\b/gi;
    let displayText = heading.replace(quantities, " ").trim();
    const region = new RegExp("(?:^|[,\\s])(" + stateNames.join("|")
      + "|[a-z]{2})(?:\\s+(\\d{5}))?$", "i").exec(displayText);
    if (region) {
      if (!matchesStateLocation(region[1]! + (region[2] ? " " + region[2] : ""))) return false;
      const locality = normalizeCity(displayText.slice(0, region.index));
      if (!locality.endsWith(" " + city) && locality !== city) return false;
      displayText = locality.slice(0, -city.length).trim();
    } else if (/\bin\b/i.test(displayText)) return false;
    let remainder = normalizeStreet(displayText);
    const expectedName = normalizeStreet(base?.name);
    if (remainder === expectedName) return true;
    if (remainder.startsWith(expectedName + " ")) remainder = remainder.slice(expectedName.length).trim();
    // Unqualified intersections/location punctuation are ambiguous. Only a
    // wholly marketing remainder may use conjunctions after quantities/name
    // are removed; unknown words never become an address-parser fallback.
    if ((/[&,/]/.test(displayText) || /\b(?:and|at|nwc|nec|swc|sec)\b/.test(remainder))
      && !/^(?:(?:of|industrial|office|medical|retail|space|ios|available|for|sale|or|lease|sublease|and)\s*)+$/.test(remainder)) return false;
    return !heading.includes("#")
      && !/\d|\b(?:street|st|road|rd|avenue|ave|boulevard|blvd|highway|hwy|drive|dr|lane|ln|court|ct|parkway|pkwy|suite|ste|unit|apt|floor|lot)\b/.test(remainder);
  };
  // Complete offering phrases are display roles, not street/unit assertions.
  // Keep this separate from the established physical and range-heading guards.
  const marketingOfferingHeading = new RegExp("^(?:medical )?" + offeringPropertyFamily
    + " (?:space|suites) available for (?:sale(?: or lease)?|lease(?: or sale)?)$", "i")
    .test(clean($("h1").text()) ?? "");
  const coherentDisplayHeading = $("h1").length === 1
    && (marketingOfferingHeading || physicalHeadingAgrees());
  const physicalAddressIdentity = addressHeroIdentity && completeDescriptionIdentity
    && coherentDisplayHeading;
  // A range's endpoint display name retains its existing full-range/H1 proof.
  const namedLocationIdentity = namedHeroIdentity && completeDescriptionIdentity
    && !rangeDescriptionIdentity && ownedIdentityMetadata && coherentDisplayHeading;
  const nativeCompleteIdentity = requestedSharpLaunchMatch && urlMatches
    && Boolean(name && street && city && state)
    && ((nativeInfo.length > 0 && nativeInfoRows.length > 0) || hasPropertySections || hasBuildingSections)
    && $("h1").length === 1 && $(".hero__sub-title").length === 1
    && (visiblePropertyIdentity || namedDescriptionIdentity || physicalAddressIdentity || namedLocationIdentity
      || nativeTerminalDescriptorOmission || (offeringHeading && (addressHeroIdentity || (rangeDescriptionIdentity
        && matchesAddressHeading($(".hero__sub-title").text(), name)))))
    && $("title").length === 1 && $('meta[property="og:title"]').length === 1
    && pageTitles.every((value) => titleMatches(value)) && $('meta[property="og:url"]').length === 1
    && sameIdentityUrl($('meta[property="og:url"]').attr("content"), requested)
    && canonicalUrls.every((value) => sameIdentityUrl(value, requested));
  if (nativeCompleteIdentity) {
    const addressHeadingsAgree = $("h2").toArray().filter((element) =>
      normalizeStreet($(element).text().split(",").length >= 3
        ? $(element).text().split(",").slice(0, -2).join(",") : $(element).text()).includes(visibleIdentityStreet)
    ).every((element) => normalizeStreet($(element).text()) === visibleIdentityStreet
      || matchesAddressHeading($(element).text(), visibleIdentityStreet));
    if (!addressHeadingsAgree || records.length > 1 || (record && (
      !subjectMatches(record.name) || !sameIdentityUrl(record.url, requested)
      || !addresses.every((address) => normalizeStreet(address.streetAddress) === street
        && normalizeCity(address.addressLocality) === city
        && normalizeState(address.addressRegion) === state
        && (!clean(address.postalCode) || !clean(base?.postalCode)
          || clean(address.postalCode) === clean(base?.postalCode)))
    ))) throw new Error("Avison Young detail identity does not match the requested property");
    return listing;
  }
  if ((capturedBuildingStructure || building.length > 0 || officialRangeScopeWitness) && isRegisteredOfficialWrapper) {
    // Complete registered structured identity can own the subject while H1 and
    // hero carry marketing/financial display text. A price is never location proof.
    const coherentHeroDisplay = namedHeroIdentity || addressHeroIdentity
      || (heroLocations.length === 1 && heroLocations[0] === true)
      || /^priced at \$[\d,]+(?:\.\d+)?$/i.test(heroText);
    const structuredDisplayIdentity = ownedIdentityMetadata && completeDescriptionIdentity
      && coherentDisplayHeading && $(".hero__sub-title").length === 1
      && coherentHeroDisplay && addresses.every((address) => clean(address.postalCode) === clean(base?.postalCode));
    // The registered official page can supply two complete physical witnesses
    // while its description remains marketing prose and H1 an offering role.
    // Neither a price-only hero nor a generic display heading supplies this proof.
    const physicalDescriptionsAgree = descriptionSelectors.every((selector) => $(selector).length <= 1)
      && descriptionNodes.every((element) => {
        const fields = (clean($(element).attr("content")) ?? "").split(",");
        // Retain the established descriptions rule for legacy identity families.
        const region = /^(.*?)\s+(\d[\d-]*)$/.exec(fields.at(-1)?.trim() ?? "");
        const claimsAddress = fields.length >= 3 && region
          && (normalizeState(region[1]) || /^[a-z]{2}$/i.test(region[1]!))
          && fields.slice(0, -2).some((field) => /\d/.test(field) || streetRole.test(field) || normalizeStreet(field) === street);
        return !claimsAddress || descriptionMatches(element, true);
      });
    const streetAcreageHeading = (() => {
      if ($("h1").length !== 1) return false;
      const match = /^(.*?)\s+-\s+([1-9]\d*(?:,\d{3})*(?:\.\d+)?)\s+acres?$/i.exec(clean($("h1").text()) ?? "");
      const quantity = match ? Number(match[2]!.replaceAll(",", "")) : NaN;
      const headingStreet = match?.[1]?.trim().toLowerCase().replace(/\s+/g, " ");
      const feedStreet = clean(base?.street)?.trim().toLowerCase().replace(/\s+/g, " ");
      return Boolean(match && Number.isFinite(quantity) && quantity > 0 && headingStreet === feedStreet);
    })();
    const officialStreetAcreageWitness = isRegisteredOfficialWrapper && ownedIdentityMetadata
      && structuredIdentityMatches && urlMatches && $("h1").length === 1 && streetAcreageHeading
      && heroLocations.length === 1 && heroLocations[0] === true && /^\d{5}$/.test(clean(base?.postalCode) ?? "")
      && addresses.every((address) => clean(address.postalCode) === clean(base?.postalCode))
      && physicalDescriptionsAgree;
    const structuredPhysicalIdentity = officialPhysicalAnchor && (offeringHeading || marketingOfferingHeading)
      && $("h1").length === 1 && physicalDescriptionsAgree;
    const acceptedNewOfficialRole = officialIntersectionOffering || officialOfficeSublease;
    if (!(identityMetadataMatches || structuredDisplayIdentity || structuredPhysicalIdentity
      || officialStreetAcreageWitness || officialRangeScopeWitness || acceptedNewOfficialRole)
      || (!acceptedNewOfficialRole && !structuredIdentityMatches) || !urlMatches) {
      throw new Error("Avison Young detail identity does not match the requested property");
    }
    return record;
  }
  // A recognized street-named native page that failed the common proof must
  // not fall through to the more general legacy recognition below.
  if (capturedBuildingStructure && requestedSharpLaunchMatch && name === street) {
    throw new Error("Avison Young detail identity does not match the requested property");
  }

  const hasLegacySharpLaunchBuildingLayout = Boolean(
    nativeInfo.length > 0
    && $(".availability-card-v2").length > 0
    && headingText.includes("building")
    && headingText.includes("availabilities")
  );
  if (hasLegacySharpLaunchBuildingLayout) {
    const visibleName = clean(base?.name)?.toLowerCase() ?? "";
    const visibleStreet = clean(base?.street)?.toLowerCase() ?? "";
    const visibleCity = clean(base?.city)?.toLowerCase() ?? "";
    const visibleState = clean(base?.state)?.toLowerCase() ?? "";
    const visibleHeading = clean($("h1").first().text())?.toLowerCase() ?? "";
    const visibleIdentityMatches =
      $("h1").length === 1
      && Boolean(visibleName && visibleStreet && visibleCity && visibleState)
      && visibleHeading === visibleName
      && bodyText.includes(visibleStreet)
      && headingText.includes(visibleCity + ", " + visibleState);

    if (!requestedSharpLaunchMatch || !urlMatches || !(visibleIdentityMatches || (addressHeroIdentity && identityMetadataMatches))) {
      throw new Error(
        "Avison Young detail identity does not match the requested property"
      );
    }
    return listing;
  }

  const hasSharpLaunchBuildingLayout =
    nativeInfo.length > 0
    && nativeInfoRows.length > 0
    && $(".hero__sub-title").length > 0
    && $('meta[property="og:url"]').length > 0;
  if (hasSharpLaunchBuildingLayout) {
    const visibleName = normalizeIdentityText(base?.name);
    const visibleStreet = normalizeIdentityText(base?.street);
    const visibleCity = normalizeIdentityText(base?.city);
    const visibleState = normalizeIdentityText(base?.state);
    const identityParts = [visibleName, visibleStreet, visibleCity, visibleState];
    const hasCompleteFeedIdentity = identityParts.every(Boolean);
    const title = normalizeIdentityText($("title").first().text());
    const ogTitle = normalizeIdentityText(
      $('meta[property="og:title"]').first().attr("content")
    );
    const ogUrl = avisonYoungAbsoluteUrl(
      $('meta[property="og:url"]').first().attr("content"),
      requested ?? AVISON_YOUNG_HOST
    );
    const sameOwnedPageUrl = (candidate: string | null): boolean => {
      if (!requested || !candidate) return false;
      try {
        const requestedParsed = new URL(requested);
        const candidateParsed = new URL(candidate);
        return requestedParsed.protocol === "https:"
          && candidateParsed.protocol === "https:"
          && !requestedParsed.username
          && !requestedParsed.password
          && !candidateParsed.username
          && !candidateParsed.password
          && requestedParsed.hostname.toLowerCase()
            === candidateParsed.hostname.toLowerCase()
          && requestedParsed.port === candidateParsed.port
          && requestedParsed.pathname.replace(/\/+$/, "").toLowerCase()
            === candidateParsed.pathname.replace(/\/+$/, "").toLowerCase()
          && requestedParsed.search === candidateParsed.search
          && requestedParsed.hash === candidateParsed.hash;
      } catch {
        return false;
      }
    };
    const containsWholePhrase = (text: string, phrase: string): boolean =>
      Boolean(phrase) && (` ${text} `).includes(` ${phrase} `);
    const visibleIdentityHeadings = $("h2").map((_index, element) =>
      normalizeIdentityText($(element).text())
    ).get();
    const identityHeadingCandidates = visibleIdentityHeadings.filter((text) =>
      containsWholePhrase(text, visibleStreet)
    );
    const visibleAddress = [visibleStreet, visibleCity, visibleState].join(" ");
    const matchingIdentityHeadings = identityHeadingCandidates.filter((text) =>
      containsWholePhrase(text, visibleName)
      && containsWholePhrase(text, visibleAddress)
    );
    const visibleIdentityMatches =
      hasCompleteFeedIdentity
      && (heroIdentityParts.length === 1 || combinedHeroIdentity
        || (namedHeroIdentity && $("h1").length === 1
          && $("title").length === 1 && $('meta[property="og:title"]').length === 1
          && $('meta[property="og:url"]').length === 1))
      && canonicalUrls.every((value) => sameIdentityUrl(value, requested))
      && title === visibleName
      && ogTitle === visibleName
      && sameOwnedPageUrl(ogUrl)
      && identityHeadingCandidates.length > 0
      && matchingIdentityHeadings.length === identityHeadingCandidates.length;

    if (!requestedSharpLaunchMatch || !urlMatches || !visibleIdentityMatches) {
      throw new Error(
        "Avison Young detail identity does not match the requested property"
      );
    }
    return listing;
  }

  const hasStructure = Boolean(
    listing
    || (
      clean($("h1").first().text())
      && (
        $("main, .property-detail, .property-details, .property-page").length > 0
        || /property details|property overview|available space|building size|sale price|lease rate/i.test(
          doc.markdown ?? bodyText
        )
      )
    )
  );
  if (!hasStructure) {
    throw new Error(requestedSharpLaunchMatch && hasBuildingContent
      ? "Avison Young detail identity does not match the requested property"
      : "Avison Young detail structure was not recognized");
  }
  if (!urlMatches && !contentMatches) {
    throw new Error(
      "Avison Young detail identity does not match the requested property"
    );
  }
  return listing;
}

export function isAvisonYoungDirectDetailUrl(value: string): boolean {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    return (
      url.protocol === "https:"
      && !url.username
      && !url.password
      && !url.port
      && (
        host === "avisonyoung.us"
        || host === "www.avisonyoung.us"
        || host.endsWith(".sharplaunch.com")
      )
    );
  } catch {
    return false;
  }
}

const AVISON_YOUNG_BLOCKED_IPV4 = new BlockList();
const AVISON_YOUNG_BLOCKED_IPV6 = new BlockList();
for (const [network, prefix] of [
  ["0.0.0.0", 8],
  ["10.0.0.0", 8],
  ["100.64.0.0", 10],
  ["127.0.0.0", 8],
  ["169.254.0.0", 16],
  ["172.16.0.0", 12],
  ["192.0.0.0", 24],
  ["192.0.2.0", 24],
  ["192.168.0.0", 16],
  ["198.18.0.0", 15],
  ["198.51.100.0", 24],
  ["203.0.113.0", 24],
  ["224.0.0.0", 4],
  ["240.0.0.0", 4],
] as Array<[string, number]>) {
  AVISON_YOUNG_BLOCKED_IPV4.addSubnet(network, prefix, "ipv4");
}
for (const [network, prefix] of [
  ["::", 96],
  ["::ffff:0.0.0.0", 96],
  ["64:ff9b::", 96],
  ["100::", 64],
  ["2001:2::", 48],
  ["2001:10::", 28],
  ["2001:20::", 28],
  ["2001:db8::", 32],
  ["fc00::", 7],
  ["fe80::", 10],
  ["ff00::", 8],
] as Array<[string, number]>) {
  AVISON_YOUNG_BLOCKED_IPV6.addSubnet(network, prefix, "ipv6");
}

export function isPublicAvisonYoungAddress(address: string): boolean {
  const version = isIP(address);
  return (
    version !== 0
    && !(version === 4
      ? AVISON_YOUNG_BLOCKED_IPV4.check(address, "ipv4")
      : AVISON_YOUNG_BLOCKED_IPV6.check(address, "ipv6"))
  );
}

type AvisonYoungResolver = (hostname: string) => Promise<string[]>;
type AvisonYoungPinnedResponse = {
  status: number;
  location: string | null;
  body: string;
};
type AvisonYoungPinnedRequest = (
  url: URL,
  address: string,
  timeoutMs: number
) => Promise<AvisonYoungPinnedResponse>;

async function resolveAvisonYoungHost(hostname: string): Promise<string[]> {
  return (await lookup(hostname, { all: true, verbatim: true }))
    .map((entry) => entry.address);
}

async function requestAvisonYoungPinned(
  url: URL,
  address: string,
  timeoutMs: number
): Promise<AvisonYoungPinnedResponse> {
  return await new Promise((resolve, reject) => {
    const request = httpsRequest(
      url,
      {
        headers: {
          accept: "text/html,application/xhtml+xml",
          "user-agent":
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
        },
        lookup: ((_hostname: string, options: any, callback: Function) => {
          const family = isIP(address);
          if (options?.all === true) {
            callback(null, [{ address, family }]);
          } else {
            callback(null, address, family);
          }
        }) as any,
      },
      (response) => {
        const chunks: Buffer[] = [];
        let bytes = 0;
        response.on("data", (chunk: Buffer | string) => {
          const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
          bytes += buffer.length;
          if (bytes > 5 * 1024 * 1024) {
            request.destroy(new Error("Avison Young direct detail exceeded 5 MiB"));
            return;
          }
          chunks.push(buffer);
        });
        response.on("end", () => {
          resolve({
            status: response.statusCode ?? 0,
            location:
              typeof response.headers.location === "string"
                ? response.headers.location
                : null,
            body: Buffer.concat(chunks).toString("utf8"),
          });
        });
      }
    );
    request.setTimeout(timeoutMs, () => {
      request.destroy(
        new Error(`Avison Young direct detail timed out after ${timeoutMs}ms`)
      );
    });
    request.on("error", reject);
    request.end();
  });
}

export async function fetchAvisonYoungDirectDoc(
  url: string,
  timeoutMs = AVISON_YOUNG_DIRECT_DETAIL_TIMEOUT_MS,
  resolveHost: AvisonYoungResolver = resolveAvisonYoungHost,
  requestPinned: AvisonYoungPinnedRequest = requestAvisonYoungPinned
): Promise<ScrapedDoc> {
  let currentUrl = new URL(url);
  let response: AvisonYoungPinnedResponse | null = null;
  for (let redirect = 0; redirect <= 5; redirect++) {
    if (!isAvisonYoungDirectDetailUrl(currentUrl.toString())) {
      throw new Error(`Avison Young direct detail URL is not approved: ${currentUrl.hostname}`);
    }
    const addresses = await resolveHost(currentUrl.hostname);
    if (!addresses.length || addresses.some((address) => !isPublicAvisonYoungAddress(address))) {
      throw new Error(
        `Avison Young direct detail resolved to a non-public address: ${currentUrl.hostname}`
      );
    }
    response = await requestPinned(currentUrl, addresses[0]!, timeoutMs);
    if (response.status < 300 || response.status >= 400) break;
    const location = response.location;
    if (!location) {
      throw new Error(`Avison Young direct detail redirect ${response.status} lacks Location`);
    }
    if (redirect === 5) {
      throw new Error("Avison Young direct detail exceeded five redirects");
    }
    currentUrl = new URL(location, currentUrl);
  }
  if (!response) {
    throw new Error("Avison Young direct detail produced no response");
  }
  const rawHtml = response.body;
  if (response.status < 200 || response.status >= 300) {
    throw new AvisonYoungHttpStatusError(currentUrl.toString(), response.status);
  }
  if (!rawHtml.trim()) {
    throw new Error("Avison Young direct detail returned an empty body");
  }
  const $ = cheerio.load(rawHtml);
  const links = $("a[href]")
    .map((_, element) => $(element).attr("href"))
    .get()
    .filter((value): value is string => Boolean(value));
  const images = $("img[src], source[src]")
    .map((_, element) => $(element).attr("src"))
    .get()
    .filter((value): value is string => Boolean(value));
  // Convert the full primary content to durable structured Markdown. Normalize
  // link/image targets first so new listings retain source-grounding URLs even
  // when the provider page uses relative attributes.
  const content = ($("main").first().length ? $("main").first() : $("body")).clone();
  content.find("script, style, noscript").remove();
  content.find("a[href]").each((_, element) => {
    const absolute = avisonYoungAbsoluteUrl($(element).attr("href"), currentUrl.toString());
    if (absolute) content.find(element).attr("href", absolute);
  });
  content.find("img[src], source[src]").each((_, element) => {
    const absolute = avisonYoungAbsoluteUrl($(element).attr("src"), currentUrl.toString());
    if (absolute) content.find(element).attr("src", absolute);
  });
  let markdown = AVISON_YOUNG_TURNDOWN.turndown(content.html() ?? "").trim();
  if (!markdown) {
    const listing = firstJsonLd(rawHtml, "RealEstateListing");
    const name = clean(listing?.name);
    const description = clean(listing?.description);
    if (!name && !description) {
      throw new Error(
        "Avison Young direct detail has no durable Markdown or descriptive JSON-LD"
      );
    }
    markdown = [
      name ? `# ${name}` : null,
      description,
      `[Source property page](${currentUrl.toString()})`,
    ]
      .filter((value): value is string => Boolean(value))
      .join("\n\n");
  }
  return {
    rawHtml,
    markdown,
    links,
    images,
    metadata: {
      statusCode: response.status,
      sourceURL: currentUrl.toString(),
      url: currentUrl.toString(),
      transport: "direct_http",
    },
  };
}

export function extractAvisonYoungUrls(doc: ScrapedDoc, baseUrl: string): string[] {
  const $ = cheerio.load(doc.rawHtml);
  const candidates: Array<string | null> = [...doc.links];
  $("a[href], img[src], source[src], [data-src], [data-href]").each((_, el) => {
    candidates.push(
      $(el).attr("href") ?? $(el).attr("src") ?? $(el).attr("data-src") ?? $(el).attr("data-href") ?? null
    );
  });
  for (const match of doc.rawHtml.match(/https?:\/\/[^"'<>\\\s)]+/gi) ?? []) {
    candidates.push(match);
  }
  return dedupeStrings(
    candidates
      .map((url) => avisonYoungAbsoluteUrl(url, baseUrl))
      .filter((url: string | null): url is string => Boolean(url))
      .map((url) => url.replace(/&amp;/g, "&").replace(/[)"'\]>]+$/g, ""))
  );
}

export function extractAvisonYoungDocuments(docs: Array<{ doc: ScrapedDoc; url: string }>): any[] {
  const urls = dedupeStrings(
    docs
      .flatMap(({ doc, url }) => extractAvisonYoungUrls(doc, url))
      .filter((url) => {
        try {
          const u = new URL(url);
          return /\.pdf(?:[?#].*)?$/i.test(u.pathname + u.search) && /(^|\.)sharplaunch\.com$/i.test(u.hostname);
        } catch {
          return false;
        }
      })
  );
  return urls.map((url) => ({ name: titleFromFilename(url), url }));
}

export function isAvisonYoungPropertyPhoto(url: string, propertyGallery = false): boolean {
  try {
    const u = new URL(url);
    const filename = u.pathname.split("/").pop()?.toLowerCase() ?? "";
    return (
      u.hostname === "cdn.sharplaunch.com" &&
      /\.(?:jpe?g|png|webp)(?:[?#].*)?$/i.test(u.pathname + u.search) &&
      !/\/media\//i.test(u.pathname) &&
      // Exclude 150x150 dimension prefix (broker headshots/avatars)
      !/\/150x150\//i.test(u.pathname) &&
      // Exclude known non-property images (logo, generic header)
      !/ay_logo/i.test(filename) &&
      (propertyGallery || !/sharplaunch_header/i.test(filename)) &&
      (/\/website-\d+\//i.test(u.pathname) || /\/v2\/client-\d+\//i.test(u.pathname))
    );
  } catch {
    return false;
  }
}

export function extractAvisonYoungPhotos(docs: Array<{ doc: ScrapedDoc; url: string }>, fallback: string[]): string[] {
  const urls = docs
    .flatMap(({ doc, url }) => {
      const $ = cheerio.load(doc.rawHtml);
      // Publisher gallery context disambiguates real property assets whose
      // filenames also contain the generic header marker (retained 52767).
      const gallery = new Set<string>();
      $('section#gallery .gallery__carousel-images a[data-fancybox="gallery-carousel"]').each((_, el) => {
        for (const value of [$(el).attr("href"), $(el).find("img").attr("src")]) {
          const image = avisonYoungAbsoluteUrl(value ?? null, url);
          if (image) gallery.add(image);
        }
      });
      return [
        ...extractAvisonYoungUrls(doc, url),
        ...(Array.isArray(doc.images) ? doc.images : []).map((image) => avisonYoungAbsoluteUrl(image, url)),
      ].filter((image): image is string => image !== null && isAvisonYoungPropertyPhoto(image, gallery.has(image)));
    });
  // Apply the same filter to fallback so non-property feed images are excluded too
  const filteredFallback = fallback.filter((image) => isAvisonYoungPropertyPhoto(image));
  return dedupeStrings([...urls, ...filteredFallback]);
}

export function extractAvisonYoungJsonLd(docs: Array<{ doc: ScrapedDoc; url: string }>): any | null {
  for (const { doc } of docs) {
    const listing = firstJsonLd(doc.rawHtml, "RealEstateListing");
    if (listing) return listing;
  }
  return null;
}

export function avisonYoungNameSlug(name: string | null): string | null {
  if (!name) return null;
  return clean(
    name
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
  );
}

export function extractAvisonYoungContactUrls(docs: Array<{ doc: ScrapedDoc; url: string }>): {
  profileLinks: Array<{ url: string; text: string | null; slug: string | null }>;
  vcardLinks: string[];
} {
  const profileLinks: Array<{ url: string; text: string | null; slug: string | null }> = [];
  const vcardLinks: string[] = [];
  for (const { doc, url: baseUrl } of docs) {
    const $ = cheerio.load(doc.rawHtml);
    $('a[href*="/professionals/-/ayp/view/"]').each((_, el) => {
      const href = avisonYoungAbsoluteUrl($(el).attr("href"), baseUrl);
      if (!href) return;
      try {
        const u = new URL(href);
        if (u.hostname !== "www.avisonyoung.us" && u.hostname !== "avisonyoung.us") return;
        const slug = clean(u.pathname.match(/\/professionals\/-\/ayp\/view\/([^/]+)/i)?.[1] ?? null);
        profileLinks.push({ url: u.toString(), text: clean($(el).text()), slug });
      } catch {
        /* ignore malformed contact URL */
      }
    });
    $('a[href*="vcard"], a[href*="vcf"], a[href*="GetVCard"]').each((_, el) => {
      const href = avisonYoungAbsoluteUrl($(el).attr("href"), baseUrl);
      if (href) vcardLinks.push(href);
    });
  }
  const seenProfiles = new Map<string, { url: string; text: string | null; slug: string | null }>();
  for (const link of profileLinks) seenProfiles.set(link.url, link);
  return {
    profileLinks: [...seenProfiles.values()],
    vcardLinks: dedupeStrings(vcardLinks),
  };
}

export function enrichAvisonYoungContacts(contacts: any[], docs: Array<{ doc: ScrapedDoc; url: string }>): any[] {
  if (!contacts.length || !docs.length) return contacts;
  const { profileLinks, vcardLinks } = extractAvisonYoungContactUrls(docs);
  if (!profileLinks.length && !vcardLinks.length) return contacts;
  return contacts.map((contact) => {
    const nameSlug = avisonYoungNameSlug(clean(contact?.name));
    const profile =
      profileLinks.find((link) => nameSlug && link.slug === nameSlug) ??
      profileLinks.find((link) => nameSlug && link.slug?.includes(nameSlug)) ??
      (contacts.length === 1 && profileLinks.length === 1 ? profileLinks[0] : null);
    const vcardUrl = contacts.length === 1 && vcardLinks.length === 1 ? vcardLinks[0] : null;
    return prune({
      ...contact,
      profileUrl: clean(contact?.profileUrl) ?? profile?.url,
      vcardUrl: clean(contact?.vcardUrl) ?? vcardUrl,
    });
  });
}

export function decodeAvisonYoungCloudflareEmail(value: any): string | null {
  const encoded = clean(value);
  if (!encoded || encoded.length < 4 || encoded.length % 2 !== 0 || !/^[0-9a-f]+$/i.test(encoded)) {
    return null;
  }
  const key = Number.parseInt(encoded.slice(0, 2), 16);
  let decoded = "";
  for (let index = 2; index < encoded.length; index += 2) {
    decoded += String.fromCharCode(Number.parseInt(encoded.slice(index, index + 2), 16) ^ key);
  }
  return clean(decoded);
}

function avisonYoungStrongContactIdentifiers(contact: any): Set<string> {
  const identifiers = new Set<string>();
  const email = clean(contact?.email)?.toLowerCase();
  if (email) identifiers.add(`email:${email}`);
  const profileUrl = clean(contact?.profileUrl)?.toLowerCase();
  if (profileUrl) identifiers.add(`profile:${profileUrl}`);
  return identifiers;
}

function avisonYoungContactFieldsConflict(left: any, right: any): boolean {
  for (const [field, normalize] of [
    ["email", (value: string) => value.toLowerCase()],
    ["phone", (value: string) => value.replace(/\D+/g, "")],
    ["profileUrl", (value: string) => value.toLowerCase()],
  ] as const) {
    const leftValue = clean(left?.[field]);
    const rightValue = clean(right?.[field]);
    if (leftValue && rightValue && normalize(leftValue) !== normalize(rightValue)) return true;
  }
  return false;
}

export function mergeAvisonYoungContacts(...groups: any[][]): any[] {
  const merged: any[] = [];
  for (const contact of groups.flat()) {
    if (!contact || typeof contact !== "object") continue;
    const incomingIdentifiers = avisonYoungStrongContactIdentifiers(contact);
    const email = clean(contact.email)?.toLowerCase();
    const name = avisonYoungNameSlug(clean(contact.name));
    if (!name && !email && incomingIdentifiers.size === 0) continue;
    const index = merged.findIndex((candidate) => {
      const candidateName = avisonYoungNameSlug(clean(candidate.name));
      const candidateIdentifiers = avisonYoungStrongContactIdentifiers(candidate);
      const hasExactStrongMatch = [...incomingIdentifiers].some((identifier) =>
        candidateIdentifiers.has(identifier)
      );
      if (hasExactStrongMatch) return true;
      return Boolean(
        name &&
        candidateName &&
        name === candidateName &&
        !avisonYoungContactFieldsConflict(contact, candidate)
      );
    });
    const previous = index >= 0 ? merged[index] : {};
    const value = prune({
      ...previous,
      ...Object.fromEntries(
        Object.entries(contact).filter(([, fieldValue]) => clean(fieldValue) !== null)
      ),
      company: clean(contact.company) ?? clean(previous.company) ?? "Avison Young",
    });
    if (index >= 0) merged[index] = value;
    else merged.push(value);
  }
  return merged;
}

export function avisonYoungMailtoEmail(value: any): string | null {
  const href = clean(value);
  if (!href || !/^mailto:/i.test(href)) return null;
  const rawAddress = href.replace(/^mailto:/i, "").split(/[?#]/, 1)[0] ?? "";
  let address: string;
  try {
    address = decodeURIComponent(rawAddress).trim();
  } catch {
    return null;
  }
  if (
    !address ||
    /[,;]/.test(address) ||
    !/^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$/.test(address)
  ) {
    return null;
  }
  return address;
}

export function extractAvisonYoungDetailContacts(
  docs: Array<{ doc: ScrapedDoc; url: string }>
): any[] {
  const contacts: any[] = [];
  for (const { doc, url: baseUrl } of docs) {
    const $ = cheerio.load(doc.rawHtml);
    $(".team-member").each((_, el) => {
      const node = $(el);
      const mailto = avisonYoungMailtoEmail(
        node.find('a[href^="mailto:"]').first().attr("href")
      );
      const protectedEmail = decodeAvisonYoungCloudflareEmail(
        node.find("[data-cfemail]").first().attr("data-cfemail")
      );
      const profileUrl = avisonYoungAbsoluteUrl(
        node.find('a[href*="/professionals/-/ayp/view/"]').first().attr("href"),
        baseUrl
      );
      const vcardUrl = avisonYoungAbsoluteUrl(
        node.find('a[href*="vcard"], a[href*="vcf"], a[href*="GetVCard"]').first().attr("href"),
        baseUrl
      );
      const avatarUrl = avisonYoungAbsoluteUrl(node.find("img[src]").first().attr("src"), baseUrl);
      contacts.push(
        prune({
          name: clean(node.find(".team-member__name").first().text()),
          title: clean(node.find(".team-member__job").first().text()),
          company: clean(node.find(".team-member__company").first().text()) ?? "Avison Young",
          phone:
            clean(node.find('a[href^="tel:"]').first().attr("href"))?.replace(/^tel:/i, "") ??
            clean(node.find(".team-member__phone").first().text()),
          email: mailto ?? protectedEmail,
          avatarUrl,
          profileUrl,
          vcardUrl,
        })
      );
    });

    const listing = firstJsonLd(doc.rawHtml, "RealEstateListing");
    const agents = Array.isArray(listing?.agent)
      ? listing.agent
      : listing?.agent
        ? [listing.agent]
        : [];
    for (const agent of agents) {
      if (!agent || typeof agent !== "object") continue;
      contacts.push(
        prune({
          name: clean(agent.name),
          title: clean(agent.jobTitle),
          company: clean(agent.worksFor?.name) ?? "Avison Young",
          phone: clean(agent.telephone),
          email: clean(agent.email),
          avatarUrl: avisonYoungAbsoluteUrl(agent.image, baseUrl),
          profileUrl: avisonYoungAbsoluteUrl(agent.url, baseUrl),
        })
      );
    }
  }
  return mergeAvisonYoungContacts(contacts);
}

export function isAvisonYoungUsCompatible(row: any): boolean {
  const country = clean(row.country)?.toLowerCase();
  if (country) return ["us", "usa", "united states", "united states of america"].includes(country);
  const state = clean(row.state);
  return !!state && /^[A-Z]{2}$/.test(state);
}

export function avisonYoungTransactions(row: any): string[] {
  return (Array.isArray(row.transaction) ? row.transaction : [row.transaction])
    .map((t: any) => clean(String(t ?? ""))?.toLowerCase())
    .filter((t: string | null): t is string => !!t);
}

export function avisonYoungMatchesTx(row: any, tx: Tx): boolean {
  const transactions = avisonYoungTransactions(row);
  if (tx === "sale") return transactions.some((t) => t.includes("sale"));
  return transactions.some((t) => t.includes("lease") || t.includes("sublease"));
}

export function avisonYoungTransactionType(row: any): string {
  const transactions = avisonYoungTransactions(row);
  const hasSale = transactions.some((t) => t.includes("sale"));
  const hasLease = transactions.some((t) => t.includes("lease") || t.includes("sublease"));
  if (hasSale && hasLease) return "Sale/Lease";
  if (transactions.some((t) => t.includes("sublease"))) return "Sublease";
  return hasLease ? "Lease" : "Sale";
}

export function avisonYoungSizeText(row: any): string | null {
  const parts: string[] = [];
  if (num(row.total_surface_sqft)) parts.push(`${row.total_surface_sqft} SF total`);
  if (num(row.availabilities_min_surface_sqft) || num(row.availabilities_max_surface_sqft)) {
    const min = num(row.availabilities_min_surface_sqft);
    const max = num(row.availabilities_max_surface_sqft);
    parts.push(
      min && max && min !== max
        ? `${min} - ${max} SF available`
        : `${min ?? max} SF available`
    );
  }
  return clean(parts.join("; "));
}

export function avisonYoungContact(member: any): any | null {
  if (!member) return null;
  const name = clean([member.first_name, member.last_name].map(clean).filter(Boolean).join(" "));
  const avatarUrl =
    member.media_id != null ? sharpLaunchCdnUrl(`media/${String(member.media_id)}`) : null;
  return prune({
    name,
    title: clean(member.title),
    email: clean(member.email),
    phone: clean(member.phone) ?? clean(member.phone_2),
    company: clean(member.company) ?? clean(member.location) ?? "Avison Young",
    avatarUrl,
  });
}

function avisonYoungProviderId(value: unknown, context: string): string {
  if (
    (typeof value !== "string" && typeof value !== "number")
    || typeof value === "boolean"
  ) {
    throw new Error(`Avison Young ${context} is missing a provider id`);
  }
  const providerId = clean(String(value));
  if (!providerId) {
    throw new Error(`Avison Young ${context} is missing a provider id`);
  }
  return providerId;
}

export function avisonYoungSelectedProviderIds(rows: any[]): string[] {
  const ids: string[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    const providerId = avisonYoungProviderId(
      row?.id,
      "selected inventory row"
    );
    if (seen.has(providerId)) {
      throw new Error(
        `Avison Young selected inventory contains duplicate provider id ${providerId}`
      );
    }
    seen.add(providerId);
    ids.push(providerId);
  }
  return ids;
}

export function assertAvisonYoungOutputIdentity(
  selectedProviderIds: string[],
  listings: any[]
): void {
  const outputIds = listings.map((listing) =>
    avisonYoungProviderId(listing?.id, "output listing")
  );
  if (
    outputIds.length !== selectedProviderIds.length
    || outputIds.some((providerId, index) => providerId !== selectedProviderIds[index])
  ) {
    throw new Error(
      "Avison Young selected/output identity reconciliation failed"
    );
  }
}

export function avisonYoungBaseListing(row: any, teamMembers: Map<string, any>): any {
  const askingControl = avisonAskingControl(row);
  const contactsDetailed = (Array.isArray(row.team_member_ids) ? row.team_member_ids : [])
    .map((id: any) => avisonYoungContact(teamMembers.get(String(id))))
    .filter(Boolean);
  const brokerIds = contactsDetailed
    .map((c: any) =>
      brokerRef({
        name: clean(c.name),
        email: clean(c.email),
        phone: clean(c.phone),
        avatarUrl: clean(c.avatarUrl),
        company: "Avison Young",
      })
    )
    .filter((id: number | null): id is number => id !== null);
  const imageUrl = sharpLaunchCdnUrl(row.image_path);
  const externalUrl = clean(row.external_url);
  const sharpLaunchUrl = clean(row.url);
  const rawTypes = Array.isArray(row.type) ? row.type.map(clean).filter(Boolean) : [];

  // --- Phase-2 Data-Lift: scalar fields from rawSharpLaunch (collect-time forward path) ---
  // canonicalUrl: prefer external_url (the public AY property page) over the
  // SharpLaunch micro-site URL; both are already in the listing but canonicalUrl
  // is what the ingest writes to cre_listings.canonical_url.
  const canonicalUrl = externalUrl ?? sharpLaunchUrl;

  // propertySubtype: use the first SharpLaunch type token (e.g. "office.medical",
  // "industrial.warehouse_distribution"). These are the richest AY subtype strings
  // and are consistent with the contract's free-text property_subtype column.
  const propertySubtype = rawTypes.length > 0 ? (rawTypes[0] ?? null) : null;

  // buildingClass: normBuildingClass applied to AY subtype strings. AY encodes
  // subtypes as "category.subcategory" (e.g. "office.medical"), not "Class A/B/C";
  // normBuildingClass returns null for these (documented in golden vector row 23).
  // Emit the field anyway so future AY feed additions that include a class string
  // are picked up automatically.
  let buildingClass: "A" | "B" | "C" | "D" | null = null;
  for (const t of rawTypes) {
    const cls = normBuildingClass(t as string);
    if (cls !== null) { buildingClass = cls; break; }
  }

  // submarket: SharpLaunch feed exposes it directly on the row.
  const submarket = clean(row.submarket);

  // yearBuilt: SharpLaunch stores it as "yearbuilt" (lowercase).
  const yearBuilt = num(row.yearbuilt) ?? num(row.yearBuilt);

  // units: SharpLaunch "units" field (multifamily / mixed-use unit count).
  const units = num(row.units);



  return prune({
    id: row.id != null ? String(row.id) : null,
    name: clean(row.name) ?? clean(row.meta_title),
    headline: clean(row.meta_title),
    transactionType: avisonYoungTransactionType(row),
    assetType: rawTypes.length ? rawTypes.join(", ") : null,
    description: stripHtmlText(row.description) ?? clean(row.meta_description),
    street: clean(row.address),
    city: clean(row.city),
    state: clean(row.state),
    postalCode: clean(row.zip),
    country: clean(row.country) ?? "US",
    latitude: num(row.location?.lat),
    longitude: num(row.location?.lng),
    salePriceUsd: num(row.sale_price),
    salePriceText: row.sale_price ? `$${Number(row.sale_price).toLocaleString("en-US")}` : null,
    capRatePct: num(row.cap_rate),
    sizeText: avisonYoungSizeText(row),
    buildingSizeSqft: num(row.total_surface_sqft),
    yearBuilt,
    brokerIds,
    contactsDetailed,
    photos: imageUrl && isAvisonYoungPropertyPhoto(imageUrl) ? [imageUrl] : [],
    url: externalUrl ?? sharpLaunchUrl,
    externalUrl,
    sharpLaunchUrl,
    sourceFeedUrl: `${AVISON_YOUNG_API_BASE}?entity=website&status=active`,
    lastUpdated: clean(row.updated_at)?.slice(0, 10) ?? clean(row.on_market_at)?.slice(0, 10),
    rawSubtypes: rawTypes,
    // The feed does not declare a per-SF basis for this value. Retain it untyped.
    saleUnitPrice: num(row.sale_unit_price),
    availableMinSqft: num(row.availabilities_min_surface_sqft),
    availableMaxSqft: num(row.availabilities_max_surface_sqft),
    // Stranded structured-field lift onto the shared listing vocabulary keys.
    // cre_ingest.to_row maps into existing cre_listings columns. The SharpLaunch
    // feed exposes min/max available surface and an occupancy percentage that
    // were previously dropped; surface them so a feed-only (monitor) row is also
    // a structured-data gain. occupancyRate is normalized downstream
    // (norm_occupancy_rate) so a raw 0-100 percentage is accepted.
    availableSf: num(row.availabilities_min_surface_sqft) ?? num(row.availabilities_max_surface_sqft),
    minDivisibleSf: num(row.availabilities_min_surface_sqft),
    maxDivisibleSf: num(row.availabilities_max_surface_sqft),
    occupancyRate: num(row.occupancy) ?? num(row.occupancy_rate) ?? num(row.percent_leased),
    // Phase-2 scalar fields (Section B, contract vocab):
    canonicalUrl,
    propertySubtype,
    buildingClass,
    submarket,
    units,
    preserveChildCollections: true,
    detailUnavailable: true,
    detailUnavailableReason: "base feed row; current detail page not yet fetched",
    rawSharpLaunch: row,
    avisonPriceVisibility: { schema: "avison-asking-price-v1", control: askingControl },
  });
}

// Pick the longest non-empty page markdown across the scraped detail docs (the
// richest full-page text). Returns undefined when nothing was captured so prune
// drops the key and the ingest markdown COALESCE-keep leaves any prior value.
export function avisonYoungLongestMarkdown(docs: Array<{ doc: ScrapedDoc; url: string }>): string | undefined {
  let best = "";
  for (const { doc } of docs) {
    const md = typeof doc?.markdown === "string" ? doc.markdown : "";
    if (md.length > best.length) best = md;
  }
  return best || undefined;
}

// Harvest every scraped Avison Young / SharpLaunch detail page and union the
// results. The SharpLaunch PDF documents and curated property photos are folded
// in as extra* (documents as bare strings so the harvester re-classifies them;
// images as urls), plus any RealEstateListing JSON-LD video/embed url promoted
// as media. Pure (no network); never throws (harvestDetail is guarded).
export function harvestAvisonYoung(
  docs: Array<{ doc: ScrapedDoc; url: string }>,
  documents: any[],
  photos: string[],
  listingLd: any | null
): HarvestResult {
  const media = new Map<string, MediaItem>();
  const links = new Map<string, LinkItem>();
  const docMap = new Map<string, DocItem>();
  const images = new Map<string, string>();
  const docUrls = (Array.isArray(documents) ? documents : [])
    .map((d: any) => clean(d?.url))
    .filter((u): u is string => !!u);
  // RealEstateListing JSON-LD may expose a video/virtual-tour url under video,
  // associatedMedia, or tourBookingPage; promote any string urls as media.
  const ldMedia: string[] = [];
  for (const v of [listingLd?.video, listingLd?.tourBookingPage, listingLd?.associatedMedia]) {
    if (typeof v === "string") ldMedia.push(v);
    else if (v && typeof v === "object") {
      const u = clean((v as any).contentUrl) ?? clean((v as any).url) ?? clean((v as any).embedUrl);
      if (u) ldMedia.push(u);
    } else if (Array.isArray(v)) {
      for (const item of v) {
        const u =
          typeof item === "string"
            ? item
            : clean(item?.contentUrl) ?? clean(item?.url) ?? clean(item?.embedUrl);
        if (u) ldMedia.push(u);
      }
    }
  }
  for (const { doc, url } of docs) {
    const r = harvestDetail(doc, {
      baseUrl: url,
      extraDocs: docUrls,
      extraImages: photos,
      extraMedia: ldMedia,
    });
    for (const m of r.media) if (!media.has(m.url)) media.set(m.url, m);
    for (const l of r.links) if (!links.has(l.url)) links.set(l.url, l);
    for (const d of r.documents) if (!docMap.has(d.url)) docMap.set(d.url, d);
    for (const img of r.images) if (!images.has(img)) images.set(img, img);
  }
  return {
    media: [...media.values()],
    links: [...links.values()],
    documents: [...docMap.values()],
    images: [...images.values()],
  };
}

export async function fetchAvisonYoungDirectDocWithRetry(
  url: string,
  fetchDetail: (url: string) => Promise<ScrapedDoc> = fetchAvisonYoungDirectDoc,
  attempts = AVISON_YOUNG_DIRECT_DETAIL_ATTEMPTS,
  retryMs = AVISON_YOUNG_DIRECT_DETAIL_RETRY_MS,
  wait: (milliseconds: number) => Promise<void> = (milliseconds) =>
    new Promise((resolve) => setTimeout(resolve, milliseconds))
): Promise<ScrapedDoc> {
  let lastError: unknown = new Error("Avison Young direct detail was not attempted");
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      return await fetchDetail(url);
    } catch (error) {
      if (error instanceof AvisonYoungHttpStatusError && error.status === 404) {
        throw error;
      }
      lastError = error;
      if (attempt < attempts && retryMs > 0) {
        await wait(retryMs * attempt);
      }
    }
  }
  throw new Error(
    `Avison Young direct detail failed after ${attempts} attempt(s): ${String(lastError)}`
  );
}

export async function enrichAvisonYoungListing(
  base: any,
  strict = requireFreshDetails(),
  directFetch: (url: string) => Promise<ScrapedDoc> = fetchAvisonYoungDirectDoc,
  officialFeedIdentity?: AvisonYoungOfficialFeedIdentityContext
): Promise<any> {
  const requireLiveDetails = strict || requireFreshPropertyDetails();
  const detailUrls = dedupeStrings([clean(base.sharpLaunchUrl), clean(base.externalUrl)]).filter((url) =>
    /^https?:\/\//i.test(url)
  );
  if (strict && base.preserveChildCollections === true) {
    throw new Error(
      "Avison Young strict detail cannot authorize a row that preserves child collections"
    );
  }
  if (!detailUrls.length) {
    if (strict) {
      throw new Error("Avison Young strict detail requires a public detail URL");
    }
    return prune({ ...base, detailError: "missing public detail URLs" });
  }

  const docs: Array<{ doc: ScrapedDoc; url: string }> = [];
  const errors: string[] = [];
  const missingDetails: AvisonYoungTerminalDetailFailure[] = [];
  for (const url of detailUrls) {
    try {
      const doc =
        process.env.AVISON_YOUNG_DETAIL_TRANSPORT === "direct"
          ? requireLiveDetails
            ? await fetchAvisonYoungDirectDocWithRetry(url, directFetch)
            : await directFetch(url)
          : await scrapeDoc(url, {
              waitFor: 1000,
              timeout: 60000,
              ...(requireLiveDetails ? { maxAge: 0 } : {}),
            });
      assertAvisonYoungDetailDoc(doc, url, base, officialFeedIdentity);
      docs.push({ url, doc });
    } catch (err) {
      const error = `${url}: ${String(err)}`;
      if (err instanceof AvisonYoungHttpStatusError && err.status === 404) {
        const role =
          url === clean(base.sharpLaunchUrl)
            ? "sharpLaunch"
            : url === clean(base.externalUrl)
              ? "external"
              : "other";
        const failure: AvisonYoungTerminalDetailFailure = {
          listingId: clean(base.id) ?? "unknown",
          role,
          status: 404,
        };
        if (strict) throw new AvisonYoungTerminalDetailError(failure);
        missingDetails.push(failure);
      } else if (strict) {
        throw new Error(`Avison Young detail fetch failed: ${error}`);
      }
      errors.push(error);
    }
  }
  if (!docs.length) {
    if (requireLiveDetails && missingDetails.length === detailUrls.length) {
      throw new AvisonYoungTerminalDetailError(missingDetails[0]!);
    }
    return prune({ ...base, detailError: errors.join("; ") || "no detail pages scraped" });
  }
  if (strict && docs.length !== detailUrls.length) {
    throw new Error(
      `Avison Young strict detail is incomplete: validated ${docs.length}/${detailUrls.length} URLs`
    );
  }

  const documents = extractAvisonYoungDocuments(docs);
  const photos = extractAvisonYoungPhotos(docs, Array.isArray(base.photos) ? base.photos : []);
  const listingLd = extractAvisonYoungJsonLd(docs);
  // Capture-everything harvest across every scraped detail page (sharpLaunch +
  // external). Fold the SharpLaunch PDFs and curated property photos in as
  // extra* so the harvester unifies + classifies + dedups, and add any
  // RealEstateListing JSON-LD video/tour url as promoted media. media/links are
  // unioned across the per-doc passes; documents use the generic harvester,
  // while listing photos retain the source-qualified selection above.
  const harvested = harvestAvisonYoung(docs, documents, photos, listingLd);
  const contactsDetailed = enrichAvisonYoungContacts(
    mergeAvisonYoungContacts(
      Array.isArray(base.contactsDetailed) ? base.contactsDetailed : [],
      extractAvisonYoungDetailContacts(docs)
    ),
    docs
  );
  const brokerIds = contactsDetailed
    .map((c: any) =>
      brokerRef({
        name: clean(c.name),
        email: clean(c.email),
        phone: clean(c.phone),
        avatarUrl: clean(c.avatarUrl),
        company: "Avison Young",
      })
    )
    .filter((id: number | null): id is number => id !== null);
  const observed = detailObservation(
    "avison_young_detail",
    requireLiveDetails ? "live" : "generation_cache"
  );
  const preserveChildren =
    !strict
    && (base.preserveChildCollections === true || errors.length > 0);
  const directOnly = docs.every(
    ({ doc }) => doc.metadata?.transport === "direct_http"
  );
  const leaseFacts = extractAvisonLeaseFacts({
    externalId: clean(base.id) ?? "",
    listingUrl: clean(base.externalUrl) ?? "",
    observedAt: observed.observedAt,
    pages: docs.map(({ doc, url }) => ({ sourceUrl: url, rawHtml: doc.rawHtml })),
  });

  const result = prune({
    ...base,
    name: clean(listingLd?.name) ?? base.name,
    description: clean(listingLd?.description) ?? base.description,
    lastUpdated: clean(listingLd?.datePosted)?.slice(0, 10) ?? base.lastUpdated,
    brokerIds: brokerIds.length ? brokerIds : base.brokerIds,
    contactsDetailed,
    brochures: undefined,
    documents: harvested.documents.length ? harvested.documents : documents,
    photos,
    media: harvested.media,
    links: harvested.links,
    markdown: avisonYoungLongestMarkdown(docs),
    // The ingestor uses this signal to retain an existing richer Markdown
    // capture while still inserting the direct Turndown capture for new rows
    // or filling an existing NULL.
    preserveExistingMarkdown: directOnly ? true : undefined,
    documentCount: (harvested.documents.length || documents.length),
    photoCount: photos.length,
    detailJsonLd: listingLd,
    detailScrape: {
      urls: docs.map(({ url }) => url),
      markdownLength: docs.reduce((sum, item) => sum + item.doc.markdown.length, 0),
      rawHtmlLength: docs.reduce((sum, item) => sum + item.doc.rawHtml.length, 0),
      linkCount: docs.reduce((sum, item) => sum + item.doc.links.length, 0),
      documentCount: documents.length,
      photoCount: photos.length,
      profileUrlCount: contactsDetailed.filter((c: any) => clean(c?.profileUrl)).length,
      vcardUrlCount: contactsDetailed.filter((c: any) => clean(c?.vcardUrl)).length,
      transport: directOnly ? "direct_http" : "firecrawl",
      markdownDisposition: directOnly
        ? "preserve_existing_or_insert"
        : "replace",
      warnings: errors.length ? errors : undefined,
    },
    inventoryObservedAt: base.inventoryObservedAt ?? observed.observedAt,
    detailObservedAt: observed.observedAt,
    freshnessProvenance: {
      detailScope: "detail_page",
      generationId: observed.generationId,
      method: observed.method,
      cacheDisposition: observed.cacheDisposition,
      identityMethod: "detail_url_or_property_content",
    },
    preserveChildCollections: preserveChildren ? true : undefined,
    detailObservedWithChildPreservation:
      preserveChildren ? true : undefined,
    detailUnavailable: undefined,
    detailUnavailableReason: undefined,
    detailWarning: errors.length ? errors.join("; ") : undefined,
  }) ?? {};
  if (leaseFacts) {
    const extraFacts =
      base.extraFacts !== null && typeof base.extraFacts === "object" && !Array.isArray(base.extraFacts)
        ? base.extraFacts
        : {};
    result.extraFacts = { ...extraFacts, avison_lease_facts: leaseFacts };
  }
  return result;
}

export async function srcAvisonYoung(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  const strict = requireFreshDetails();
  const requireLiveDetails = strict || requireFreshPropertyDetails();
  if (requireLiveDetails && !refreshGenerationId()) {
    throw new Error(
      "Avison Young fresh property detail contract requires CRE_REFRESH_GENERATION"
    );
  }
  if (requireLiveDetails && monitor) {
    throw new Error(
      "Avison Young fresh property details require full detail mode, not monitor mode"
    );
  }
  if (requireLiveDetails) {
    // The module cache is useful for ordinary sale/lease passes, but a
    // freshness generation must observe the provider feed during this
    // invocation.
    avisonYoungCache = null;
  }
  const sourceUrl = `https://www.avisonyoung.us/properties/#/?transaction=${tx}&view=sidebar&status=active`;
  const { websiteRows, teamMembers, teamFeedComplete, teamFeedReason } = await getAvisonYoungFeed();
  const officialFeedUrls = indexAvisonYoungOfficialFeedUrls(websiteRows);
  assertAvisonYoungStrictFeed(teamFeedComplete, teamFeedReason, strict);
  const inventoryObservation = detailObservation(
    "avison_young_sharplaunch_inventory",
    requireLiveDetails ? "live" : "generation_cache"
  );
  const rows = websiteRows
    .filter((row) => row?.status === "active")
    .filter(isAvisonYoungUsCompatible)
    .filter((row) => avisonYoungMatchesTx(row, tx))
    .sort((a, b) => Number(a.order_id ?? a.id ?? 0) - Number(b.order_id ?? b.id ?? 0));
  const want = Math.min(max, rows.length);
  const selectedRows = rows.slice(0, want);
  const truncated = avisonYoungTruncated(max, selectedRows.length, rows.length);
  const selectedProviderIds = avisonYoungSelectedProviderIds(selectedRows);
  const baseListings = selectedRows.map((row) => ({
    ...avisonYoungBaseListing(row, teamMembers),
    inventoryObservedAt: inventoryObservation.observedAt,
    freshnessProvenance: {
      detailScope: "inventory_only",
      generationId: inventoryObservation.generationId,
      method: inventoryObservation.method,
      cacheDisposition: inventoryObservation.cacheDisposition,
      identityMethod: "SharpLaunch website id",
    },
  }));
  if (monitor) {
    // Monitor mode: emit the SharpLaunch feed base listings only and skip the
    // detail-page enrichment. id/price/cap rate/lastUpdated are all free in the
    // feed; the feed already filters to status=active.
    if (!baseListings.length) throw new Error(`no ${tx} listings found in Avison Young SharpLaunch feed`);
    assertAvisonYoungOutputIdentity(selectedProviderIds, baseListings);
    return {
      company: "Avison Young (US)",
      sourceUrl,
      method: "SharpLaunch public website API base listings only (monitor mode; detail-page enrichment skipped)",
      totalAvailable: rows.length,
      truncated,
      listings: baseListings.map(listing => projectAvisonAskingPrice({ ...listing, sourceKey: "avison-young" })),
      note:
        "Monitor mode: SharpLaunch inventory fields only; per-listing detail-page enrichment skipped and prior child collections preserved. " +
        (teamFeedComplete
          ? `Supplemental team feed supplied ${teamMembers.size} brokers.`
          : `Supplemental broker feed degraded (${teamFeedReason}); inventory remains complete.`),
    };
  }
  const detailLimit = avisonYoungDetailLimit(max, baseListings.length, requireLiveDetails);
  const enrichSelected = async (listing: any, idx: number): Promise<any> => {
    const feedUrlKey = avisonYoungOfficialUrlKey(listing?.externalUrl);
    const officialFeedIdentity = feedUrlKey
      ? { matchingExternalUrlRowIds: officialFeedUrls.get(feedUrlKey) ?? [] }
      : undefined;
    const enriched = await enrichAvisonYoungListing(
      {
        ...listing,
        preserveChildCollections: teamFeedComplete ? undefined : true,
      },
      strict,
      undefined,
      officialFeedIdentity
    );
    // Classify only successful fresh results as successes in the draining map.
    // Otherwise a concurrent 404 could conceal this ordinary source failure.
    if (requireLiveDetails && (
      !enriched?.detailObservedAt
      || enriched?.detailError
      || enriched?.detailUnavailable
      || enriched?.freshnessProvenance?.detailScope !== "detail_page"
      || enriched?.freshnessProvenance?.generationId !== refreshGenerationId()
      || enriched?.freshnessProvenance?.cacheDisposition !== "live"
      || (strict && enriched?.preserveChildCollections === true)
    )) {
      const reason = clean(enriched?.detailError)
        ?? clean(enriched?.detailUnavailableReason)
        ?? "missing current detail provenance";
      throw new Error(
        `Avison Young fresh property detail is incomplete for ${clean(listing?.id) ?? "unknown-id"}: ${reason}`
      );
    }
    if ((idx + 1) % 10 === 0 || idx + 1 === detailLimit) {
      console.error("  avison-young/" + tx + ": detail enriched " + (idx + 1) + "/" + detailLimit);
    }
    return enriched;
  };
  const enrichedListings = detailLimit
    ? requireLiveDetails && process.env.AVISON_YOUNG_DETAIL_TRANSPORT === "direct"
      ? await mapAvisonYoungStrictDetails(
          baseListings.slice(0, detailLimit),
          AVISON_YOUNG_DETAIL_CONCURRENCY,
          enrichSelected
        )
      : await pmap(
          baseListings.slice(0, detailLimit),
          AVISON_YOUNG_DETAIL_CONCURRENCY,
          enrichSelected
        )
    : [];
  const listings = [...enrichedListings, ...baseListings.slice(detailLimit)];
  assertAvisonYoungOutputIdentity(selectedProviderIds, listings);
  if (requireLiveDetails) {
    const generationId = refreshGenerationId();
    const incomplete = listings.filter(
      (listing) =>
        !listing?.detailObservedAt
        || listing?.detailError
        || listing?.detailUnavailable
        || listing?.freshnessProvenance?.detailScope !== "detail_page"
        || listing?.freshnessProvenance?.generationId !== generationId
        || listing?.freshnessProvenance?.cacheDisposition !== "live"
        || (
          strict
          && listing?.preserveChildCollections === true
        )
    );
    if (incomplete.length) {
      const evidence = incomplete.slice(0, 10).map((listing) => {
        const id = clean(listing?.id) ?? "unknown-id";
        const reason =
          clean(listing?.detailError)
          ?? clean(listing?.detailUnavailableReason)
          ?? "missing current detail provenance";
        return `${id}: ${reason}`;
      });
      throw new Error(
        `Avison Young fresh property detail is incomplete for ${incomplete.length}/${listings.length} selected rows: ${evidence.join(" | ")}`
      );
    }
  }
  const recoveredContactRows = enrichedListings.filter(
    (listing) => Array.isArray(listing.contactsDetailed) && listing.contactsDetailed.length > 0
  ).length;
  const recoveredContactCount = enrichedListings.reduce(
    (count, listing) =>
      count + (Array.isArray(listing.contactsDetailed) ? listing.contactsDetailed.length : 0),
    0
  );
  if (!listings.length) throw new Error(`no ${tx} listings found in Avison Young SharpLaunch feed`);
  return {
    company: "Avison Young (US)",
    sourceUrl,
    method:
      "SharpLaunch public website API with bounded public detail-page enrichment for selected rows",
    totalAvailable: rows.length,
    truncated,
    listings: listings.map(listing => projectAvisonAskingPrice({ ...listing, sourceKey: "avison-young" })),
    note:
      detailLimit > 0
        ? `Detail enrichment fetched public SharpLaunch/Avison pages for ${detailLimit} selected row(s); documents, images, broker cards, profile URLs, VCard URLs, and JSON-LD are stored as URLs/raw public metadata only. ${
            teamFeedComplete
              ? `Supplemental team feed supplied ${teamMembers.size} brokers.`
              : `Supplemental broker feed degraded (${teamFeedReason}); current property-page extraction found ${recoveredContactCount} broker card(s) across ${recoveredContactRows}/${detailLimit} enriched row(s), and child replacement remained disabled.`
          }`
        : `Full-feed run preserved as SharpLaunch-only by default and retains prior child collections. ${
            teamFeedComplete
              ? `Supplemental team feed supplied ${teamMembers.size} brokers.`
              : `Supplemental broker feed degraded (${teamFeedReason}).`
          } Set AVISON_YOUNG_DETAIL_LIMIT to enrich selected rows.`,
  };
}
