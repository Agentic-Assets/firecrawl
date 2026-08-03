// sources/avison-young.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { lookup } from "node:dns/promises";
import { request as httpsRequest } from "node:https";
import { BlockList, isIP } from "node:net";
import TurndownService from "turndown";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { decodeHtmlEntities, dedupeStrings, firstJsonLd, stripHtmlText, titleFromFilename } from "../lib/html.js";
import { normBuildingClass, parseLeaseRate } from "../lib/parse.js";
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

export function assertAvisonYoungDetailDoc(
  doc: ScrapedDoc,
  requestedUrl: string,
  base: any
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
  if (!hasStructure || (!urlMatches && !contentMatches)) {
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
    throw new Error(`Avison Young direct detail returned HTTP ${response.status}`);
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

export function isAvisonYoungPropertyPhoto(url: string): boolean {
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
      !/sharplaunch_header/i.test(filename) &&
      (/\/website-\d+\//i.test(u.pathname) || /\/v2\/client-\d+\//i.test(u.pathname))
    );
  } catch {
    return false;
  }
}

export function extractAvisonYoungPhotos(docs: Array<{ doc: ScrapedDoc; url: string }>, fallback: string[]): string[] {
  const urls = docs
    .flatMap(({ doc, url }) => extractAvisonYoungUrls(doc, url))
    .filter(isAvisonYoungPropertyPhoto);
  // Apply the same filter to fallback so non-property feed images are excluded too
  const filteredFallback = fallback.filter(isAvisonYoungPropertyPhoto);
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
        if (u.hostname !== "www.avisonyoung.us") return;
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

export function avisonYoungLeaseRateText(row: any): string | null {
  const min = num(row.availabilities_min_rent);
  const max = num(row.availabilities_max_rent);
  if (!min && !max) return null;
  const value = min && max && min !== max ? `$${min} - $${max}` : `$${min ?? max}`;
  return `${value}/SF/YR`;
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

  // capRatePct: already set below from row.cap_rate; no change needed.
  // salePricePerSf: SharpLaunch "sale_unit_price" is a per-SF price on some sale rows.
  const salePricePerSf = num(row.sale_unit_price);

  // leaseRateMin / leaseRateMax: derive from the feed's clean numeric availability
  // rent fields via parseLeaseRate so the AY $5000/SF/YR anomaly guard fires.
  // Construct the canonical rate text ($/SF/YR) so parseLeaseRate can evaluate it.
  const rateText = avisonYoungLeaseRateText(row);
  const parsedRate = parseLeaseRate(rateText);
  const leaseRateMin = parsedRate.min;
  const leaseRateMax = parsedRate.max;
  const leaseRateType = parsedRate.type;
  // --- End Phase-2 Data-Lift ---

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
    leaseRateText: rateText,
    leaseRateMin,
    leaseRateMax,
    leaseRateType,
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
    salePricePerSf,
    preserveChildCollections: true,
    detailUnavailable: true,
    detailUnavailableReason: "base feed row; current detail page not yet fetched",
    rawSharpLaunch: row,
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
  directFetch: (url: string) => Promise<ScrapedDoc> = fetchAvisonYoungDirectDoc
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
      assertAvisonYoungDetailDoc(doc, url, base);
      docs.push({ url, doc });
    } catch (err) {
      const error = `${url}: ${String(err)}`;
      if (strict) {
        throw new Error(`Avison Young detail fetch failed: ${error}`);
      }
      errors.push(error);
    }
  }
  if (!docs.length) {
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
  // unioned across the per-doc passes; documents/images are taken from the
  // harvester (a superset that already contains the SharpLaunch docs/photos).
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

  return prune({
    ...base,
    name: clean(listingLd?.name) ?? base.name,
    description: clean(listingLd?.description) ?? base.description,
    lastUpdated: clean(listingLd?.datePosted)?.slice(0, 10) ?? base.lastUpdated,
    brokerIds: brokerIds.length ? brokerIds : base.brokerIds,
    contactsDetailed,
    brochures: undefined,
    documents: harvested.documents.length ? harvested.documents : documents,
    photos: harvested.images.length ? harvested.images : photos.length ? photos : base.photos,
    media: harvested.media,
    links: harvested.links,
    markdown: avisonYoungLongestMarkdown(docs),
    // The ingestor uses this signal to retain an existing richer Markdown
    // capture while still inserting the direct Turndown capture for new rows
    // or filling an existing NULL.
    preserveExistingMarkdown: directOnly ? true : undefined,
    documentCount: (harvested.documents.length || documents.length),
    photoCount: (harvested.images.length || photos.length) || base.photos?.length,
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
  });
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
      listings: baseListings,
      note:
        "Monitor mode: SharpLaunch inventory fields only; per-listing detail-page enrichment skipped and prior child collections preserved. " +
        (teamFeedComplete
          ? `Supplemental team feed supplied ${teamMembers.size} brokers.`
          : `Supplemental broker feed degraded (${teamFeedReason}); inventory remains complete.`),
    };
  }
  const detailLimit = avisonYoungDetailLimit(max, baseListings.length, requireLiveDetails);
  const enrichedListings = detailLimit
    ? await pmap(baseListings.slice(0, detailLimit), AVISON_YOUNG_DETAIL_CONCURRENCY, async (listing, idx) => {
        const enriched = await enrichAvisonYoungListing(
          {
            ...listing,
            preserveChildCollections: teamFeedComplete ? undefined : true,
          },
          strict
        );
        if ((idx + 1) % 10 === 0 || idx + 1 === detailLimit) {
          console.error(`  avison-young/${tx}: detail enriched ${idx + 1}/${detailLimit}`);
        }
        return enriched;
      })
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
    listings,
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
