import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY } from "../lib/config.js";
import { refreshGenerationId, requireFreshDetails } from "../lib/freshness.js";
import { dedupeStrings, jsonLdObjects } from "../lib/html.js";
import { clean, pmap, prune } from "../lib/util.js";
import type { SourceResult, Tx } from "../types.js";

export const LYON_STAHL_HOST = "https://lyonstahl.com";
export const LYON_STAHL_SOURCE_URL = `${LYON_STAHL_HOST}/properties/`;
export const LYON_STAHL_SITEMAP_URL = `${LYON_STAHL_HOST}/sitemap.xml`;
export const LYON_STAHL_FETCH_TIMEOUT_MS = 60_000;
export const LYON_STAHL_MAX_RESPONSE_BYTES = 32 * 1024 * 1024;

const LYON_STAHL_DETAIL_CONCURRENCY = Math.min(CONCURRENCY, 2);
const LYON_STAHL_NON_PHOTO = /avatar|headshot|logo|favicon|placeholder|sprite|cropped-/i;
const LYON_STAHL_ASSET_QUERY_KEYS = new Set(["ver", "w"]);
const LYON_STAHL_PROPERTY_SITEMAP_PATH = /(?:^|\/)properties-sitemap\d+\.xml$/i;

export type LyonStahlAvailabilityDecision = {
  disposition: "active" | "terminal" | "held";
  availability: string | null;
  reason: string;
};

export type LyonStahlParseContext = {
  inventoryObservedAt?: string;
  detailObservedAt?: string;
  strict?: boolean;
};

export type LyonStahlParseOutcome =
  | { kind: "accepted"; listing: any; availability: string }
  | { kind: "terminal"; availability: string; reason: string }
  | { kind: "held"; availability: string | null; reason: string }
  | { kind: "rejected"; reason: string };

type FetchLike = (
  input: string | URL | Request,
  init?: RequestInit
) => Promise<Response>;

function lyonStahlAssetQueryIsBenign(url: URL): boolean {
  const seen = new Set<string>();
  for (const [key, value] of url.searchParams) {
    if (!LYON_STAHL_ASSET_QUERY_KEYS.has(key) || seen.has(key)) return false;
    seen.add(key);
    if (key === "w" && !/^[1-9]\d{0,4}$/.test(value)) return false;
    if (key === "ver" && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(value)) return false;
  }
  return true;
}

function schemaToken(value: unknown): string | null {
  const text = clean(value);
  if (!text) return null;
  const token = text.split(/[\/#]/).filter(Boolean).at(-1);
  return clean(token)?.toLowerCase() ?? null;
}

/**
 * Lyon Stahl exposes Schema.org Offer.availability. Only explicit availability
 * tokens are admitted. A missing or newly introduced token is held rather than
 * being interpreted as active.
 */
export function classifyLyonStahlAvailability(
  value: unknown
): LyonStahlAvailabilityDecision {
  const availability = schemaToken(value);
  if (!availability) {
    return {
      disposition: "held",
      availability: null,
      reason: "missing Schema.org offer availability",
    };
  }
  if (["instock", "onlineonly", "preorder", "presale", "limitedavailability"].includes(availability)) {
    return {
      disposition: "active",
      availability,
      reason: `explicit active Schema.org availability: ${availability}`,
    };
  }
  if (["discontinued", "outofstock", "soldout"].includes(availability)) {
    return {
      disposition: "terminal",
      availability,
      reason: `terminal Schema.org availability: ${availability}`,
    };
  }
  return {
    disposition: "held",
    availability,
    reason: `unknown Schema.org availability: ${availability}`,
  };
}

function lyonStahlUrl(value: string, kind: "detail" | "sitemap"): string | null {
  try {
    const parsed = new URL(value, LYON_STAHL_HOST);
    if (
      parsed.protocol !== "https:"
      || parsed.hostname.toLowerCase().replace(/^www\./, "") !== "lyonstahl.com"
      || parsed.username
      || parsed.password
      || parsed.port
      || parsed.search
      || parsed.hash
    ) {
      return null;
    }
    if (/\.pdf$/i.test(parsed.pathname)) return null;
    if (kind === "detail" && !/^\/properties\/[^/]+\/?$/i.test(parsed.pathname)) return null;
    if (
      kind === "sitemap"
      && !/^\/(?:sitemap|sitemap_index|properties-sitemap\d+)\.xml$/i.test(parsed.pathname)
    ) {
      return null;
    }
    parsed.hostname = "lyonstahl.com";
    return parsed.toString();
  } catch {
    return null;
  }
}

function samePage(left: string, right: string): boolean {
  const normalize = (value: string) => {
    const parsed = new URL(value);
    return `${parsed.hostname.toLowerCase().replace(/^www\./, "")}${parsed.pathname
      .replace(/\/+$/, "")
      .toLowerCase()}`;
  };
  try {
    return normalize(left) === normalize(right);
  } catch {
    return false;
  }
}

export function lyonStahlPropertySitemaps(indexXml: string): string[] {
  const locations = sitemapLocations(indexXml, "sitemapindex", "sitemap", "Lyon Stahl sitemap index");
  const urls = dedupeStrings(
    locations.flatMap((value, index) => {
      let propertyShaped = false;
      try {
        propertyShaped = LYON_STAHL_PROPERTY_SITEMAP_PATH.test(
          new URL(value, LYON_STAHL_HOST).pathname
        );
      } catch {
        propertyShaped = LYON_STAHL_PROPERTY_SITEMAP_PATH.test(value.split(/[?#]/, 1)[0]);
      }
      if (!propertyShaped) return [];
      const url = lyonStahlUrl(value, "sitemap");
      if (!url) throw new Error(`Lyon Stahl sitemap index property loc ${index} is invalid`);
      return [url];
    })
  );
  if (!urls.length) throw new Error("Lyon Stahl sitemap index has no valid property sitemap");
  return urls;
}

export function lyonStahlPropertyUrls(propertyXml: string): string[] {
  const locations = sitemapLocations(propertyXml, "urlset", "url", "Lyon Stahl property sitemap");
  const urls = dedupeStrings(
    locations.map((value, index) => {
      const url = lyonStahlUrl(value, "detail");
      if (!url) throw new Error(`Lyon Stahl property sitemap URL ${index} is invalid`);
      return url;
    })
  );
  return urls;
}

function sitemapLocations(
  xml: string,
  rootName: "sitemapindex" | "urlset",
  entryName: "sitemap" | "url",
  label: string
): string[] {
  if (!xml.trim()) throw new Error(`${label} is empty`);
  const $ = cheerio.load(xml, { xmlMode: true });
  const roots = $.root().children().toArray();
  if (
    roots.length !== 1
    || roots[0].type !== "tag"
    || roots[0].name.toLowerCase() !== rootName
  ) {
    throw new Error(`${label} requires one ${rootName} root`);
  }
  const entries = $(roots[0]).children().toArray();
  if (
    entries.length === 0
    || entries.some((entry) => entry.type !== "tag" || entry.name.toLowerCase() !== entryName)
  ) {
    throw new Error(`${label} requires ${entryName} child elements`);
  }
  return entries.map((entry, index) => {
    const locs = $(entry)
      .children()
      .filter((_, child) => child.type === "tag" && child.name.toLowerCase() === "loc")
      .toArray();
    const location = locs.length === 1 ? clean($(locs[0]).text()) : null;
    if (!location) throw new Error(`${label} ${entryName} ${index} requires exactly one loc`);
    return location;
  });
}

export function lyonStahlAssetUrl(value: unknown): string | null {
  const raw = clean(value);
  if (!raw) return null;
  try {
    const url = new URL(raw, LYON_STAHL_HOST);
    if (
      url.protocol !== "https:"
      || url.username
      || url.password
      || url.port
      || Boolean(url.hash)
      || !lyonStahlAssetQueryIsBenign(url)
    ) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}

export function assertLyonStahlUniqueProviderIds(
  listings: Array<{ id?: unknown }>
): void {
  const ids = new Set<string>();
  for (const listing of listings) {
    const id = clean(String(listing?.id ?? ""));
    if (!id) throw new Error("Lyon Stahl emitted a listing without a provider ID");
    if (ids.has(id)) throw new Error(`Lyon Stahl emitted duplicate provider ID ${id}`);
    ids.add(id);
  }
}

export function lyonStahlProviderIdentity(html: string, requestedUrl: string): string | null {
  const requested = lyonStahlUrl(requestedUrl, "detail");
  if (!requested) return null;
  const $ = cheerio.load(html);
  const canonicalRaw =
    clean($("link[rel='canonical']").first().attr("href"))
    ?? clean($("meta[property='og:url']").first().attr("content"));
  const canonical = canonicalRaw ? lyonStahlUrl(canonicalRaw, "detail") : null;
  if (!canonical || !samePage(canonical, requested)) return null;
  const shortlink = clean($("link[rel='shortlink']").first().attr("href"));
  if (!shortlink) return null;
  try {
    const parsed = new URL(shortlink);
    if (
      parsed.origin !== LYON_STAHL_HOST
      || parsed.pathname !== "/"
      || parsed.hash
      || parsed.username
      || parsed.password
      || parsed.port
      || parsed.searchParams.size !== 1
      || parsed.searchParams.getAll("p").length !== 1
    ) {
      return null;
    }
    const rawId = parsed.searchParams.get("p");
    const id = rawId && /^[1-9]\d*$/.test(rawId) ? Number(rawId) : NaN;
    return Number.isSafeInteger(id) ? String(id) : null;
  } catch {
    return null;
  }
}

function ldTypes(value: any): string[] {
  const raw = value?.["@type"];
  return (Array.isArray(raw) ? raw : [raw]).map(String);
}

function metaContent($: cheerio.CheerioAPI, property: string): string | null {
  return clean($(`meta[property="${property}"]`).first().attr("content"));
}

function positiveNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(String(value).replace(/[$,\s]/g, ""));
  return Number.isFinite(number) && number > 0 ? number : null;
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(String(value).replace(/[,\s]/g, ""));
  return Number.isFinite(number) ? number : null;
}

function firstValue(objects: any[], namePattern: RegExp): string | null {
  for (const object of objects) {
    const values = Array.isArray(object?.additionalProperty)
      ? object.additionalProperty
      : [];
    for (const value of values) {
      const name = clean(value?.name);
      if (name && namePattern.test(name)) return clean(value?.value);
    }
  }
  return null;
}

function offerFromListing(listing: any): any | null {
  const offers = listing?.offers;
  return Array.isArray(offers) ? offers[0] ?? null : offers ?? null;
}

function lyonStahlPropertyNode(nodes: any[], listing: any): any {
  const listingUrl = clean(listing?.url);
  return (
    nodes.find((node) => {
      const types = ldTypes(node);
      return (
        types.some((type) =>
          /ApartmentComplex|Residence|House|SingleFamily|Accommodation|Place/i.test(type)
        )
        && node?.address
        && (!listingUrl || !node?.url || samePage(String(node.url), listingUrl))
      );
    })
    ?? nodes.find((node) => node?.address)
    ?? {}
  );
}

export function parseLyonStahlDetail(
  html: string,
  requestedUrl: string,
  tx: Tx,
  context: LyonStahlParseContext = {}
): LyonStahlParseOutcome {
  if (tx !== "sale") {
    return { kind: "rejected", reason: "Lyon Stahl is an investment-sales source" };
  }
  const strict = context.strict ?? requireFreshDetails();
  const identity = lyonStahlProviderIdentity(html, requestedUrl);
  if (!identity) return { kind: "rejected", reason: "canonical or WordPress shortlink identity mismatch" };

  const $ = cheerio.load(html);
  const heading = [clean($("title").first().text()), clean($("h1").first().text())]
    .filter(Boolean)
    .join(" ");
  if (
    /\b(?:404|not found|access denied|forbidden|captcha|just a moment|service unavailable)\b/i.test(
      heading
    )
  ) {
    return { kind: "rejected", reason: "challenge or error shell" };
  }

  const nodes = jsonLdObjects(html);
  const listing = nodes.find((node) =>
    ldTypes(node).some((type) => type === "RealEstateListing")
  );
  if (!listing) return { kind: "rejected", reason: "missing RealEstateListing JSON-LD" };
  if (strict && (!clean(listing.url) || !samePage(String(listing.url), requestedUrl))) {
    return { kind: "rejected", reason: "RealEstateListing identity mismatch" };
  }
  const offer = offerFromListing(listing);
  const availability = classifyLyonStahlAvailability(offer?.availability);
  if (availability.disposition === "terminal") {
    return {
      kind: "terminal",
      availability: availability.availability ?? "terminal",
      reason: availability.reason,
    };
  }
  if (availability.disposition === "held") {
    return {
      kind: "held",
      availability: availability.availability,
      reason: availability.reason,
    };
  }

  const property = lyonStahlPropertyNode(nodes, listing);
  const address = property?.address && typeof property.address === "object"
    ? property.address
    : listing?.mainEntity?.address ?? {};
  const name =
    clean(listing?.name)
    ?? clean(property?.name)
    ?? metaContent($, "og:title")?.replace(/\s*[-|]\s*Lyon Stahl.*$/i, "").trim()
    ?? null;
  const street = clean(address?.streetAddress);
  if (!name && !street) return { kind: "rejected", reason: "listing has no name or street" };

  const price = positiveNumber(listing?.price) ?? positiveNumber(offer?.price);
  const currentCapText = firstValue(
    [property, listing],
    /^(?!.*(?:projected|stabilized|pro forma))(?:(?:current|in-place)\s+)?cap\s*rate/i
  );
  const capRate = positiveNumber(currentCapText?.match(/([0-9]+(?:\.[0-9]+)?)/)?.[1]);
  const capRatePct = capRate && capRate <= 20 ? capRate : null;
  const yearBuilt =
    positiveNumber(property?.yearBuilt)
    ?? positiveNumber(firstValue([property, listing], /year built/i)?.match(/(\d{4})/)?.[1]);
  const buildingSizeSqft = positiveNumber(property?.floorSize?.value);
  const units =
    positiveNumber(property?.containsPlace?.[0]?.numberOfUnitsTotal)
    ?? positiveNumber(property?.numberOfRooms);
  const photos = dedupeStrings(
    [
      ...(Array.isArray(listing?.image) ? listing.image : [listing?.image]),
      ...(Array.isArray(property?.image) ? property.image : [property?.image]),
      metaContent($, "og:image"),
    ]
      .map(lyonStahlAssetUrl)
      .filter((url): url is string => Boolean(url))
  ).filter((url) => {
    try {
      const parsed = new URL(url, LYON_STAHL_HOST);
      return (
        parsed.hostname.toLowerCase().replace(/^www\./, "") === "lyonstahl.com"
        && /\/wp-content\/uploads\//i.test(parsed.pathname)
        && !LYON_STAHL_NON_PHOTO.test(parsed.pathname)
      );
    } catch {
      return false;
    }
  });
  const agents = [
    ...(Array.isArray(listing?.agent) ? listing.agent : listing?.agent ? [listing.agent] : []),
    ...nodes.filter((node) =>
      ldTypes(node).some((type) => type === "Person" || type === "RealEstateAgent")
    ),
  ];
  const brokerIds = agents
    .map((agent) =>
      brokerRef({
        name: clean(agent?.name),
        email: clean(agent?.email),
        phone: clean(agent?.telephone),
        avatarUrl: lyonStahlAssetUrl(
          typeof agent?.image === "string" ? agent.image : agent?.image?.url
        ),
        company: "Lyon Stahl",
      })
    )
    .filter((value): value is number => value !== null);

  const row = prune({
    id: identity,
    inventoryObservedAt: context.inventoryObservedAt,
    detailObservedAt: context.detailObservedAt,
    freshnessProvenance:
      context.inventoryObservedAt && context.detailObservedAt
        ? {
            detailScope: "detail_page",
            generationId: refreshGenerationId(),
            method: "lyon_stahl_wordpress_detail",
            cacheDisposition: "live",
            identityMethod: "wordpress_shortlink_id",
          }
        : undefined,
    name,
    transactionType: "Sale",
    assetType:
      clean(property?.accommodationCategory)
      ?? (ldTypes(property).some((type) => /Apartment|Residence/i.test(type))
        ? "Multifamily"
        : null),
    description: clean(listing?.description) ?? clean(property?.description) ?? metaContent($, "og:description"),
    street,
    city: clean(address?.addressLocality),
    state: clean(address?.addressRegion),
    postalCode: clean(address?.postalCode),
    country: clean(address?.addressCountry) ?? "US",
    latitude: finiteNumber(property?.geo?.latitude),
    longitude: finiteNumber(property?.geo?.longitude),
    salePriceUsd: price,
    salePriceText: price ? `$${price.toLocaleString("en-US")}` : null,
    capRatePct,
    buildingSizeSqft,
    yearBuilt,
    units,
    brokerIds: [...new Set(brokerIds)],
    photos,
    statusBadge: availability.availability,
    extraFacts: { providerAvailability: availability.availability },
    url: lyonStahlUrl(requestedUrl, "detail"),
    canonicalUrl: lyonStahlUrl(requestedUrl, "detail"),
    lastUpdated: metaContent($, "og:updated_time"),
  });
  return {
    kind: "accepted",
    listing: row,
    availability: availability.availability ?? "instock",
  };
}

type FetchKind = "html" | "xml";

async function lyonStahlBoundedResponseText(
  response: Response,
  maxBytes: number
): Promise<string> {
  const declaredLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    throw new Error(`Lyon Stahl response exceeds ${maxBytes} bytes`);
  }
  if (!response.body) return "";
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let total = 0;
  let text = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel();
        throw new Error(`Lyon Stahl response exceeds ${maxBytes} bytes`);
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    return text;
  } finally {
    reader.releaseLock();
  }
}

export async function lyonStahlFetchText(
  url: string,
  kind: FetchKind,
  fetchImpl: FetchLike = fetch,
  timeoutMs = LYON_STAHL_FETCH_TIMEOUT_MS,
  maxBytes = LYON_STAHL_MAX_RESPONSE_BYTES,
  attempts = 3
): Promise<string> {
  let lastError: unknown = null;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      let current = url;
      for (let redirect = 0; redirect <= 3; redirect++) {
        const allowed = lyonStahlUrl(current, kind === "html" ? "detail" : "sitemap");
        if (!allowed) throw new Error(`Lyon Stahl refused unsafe ${kind} URL`);
        const strict = requireFreshDetails();
        const response = await fetchImpl(allowed, {
          redirect: "manual",
          signal: controller.signal,
          headers: {
            "User-Agent": "Mozilla/5.0 CRE collector",
            Accept: kind === "html" ? "text/html,application/xhtml+xml" : "application/xml,text/xml,text/plain",
            ...(strict ? { "Cache-Control": "no-cache" } : {}),
          },
          ...(strict ? { cache: "no-store" as const } : {}),
        });
        if (response.status >= 300 && response.status < 400) {
          const location = response.headers.get("location");
          if (!location) throw new Error(`Lyon Stahl redirect ${response.status} lacks Location`);
          current = new URL(location, allowed).toString();
          continue;
        }
        if (!response.ok) throw new Error(`Lyon Stahl HTTP ${response.status}`);
        const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
        if (contentType.includes("application/pdf")) {
          throw new Error("Lyon Stahl refused PDF response");
        }
        if (
          contentType
          && kind === "html"
          && !contentType.includes("text/html")
          && !contentType.includes("application/xhtml+xml")
        ) {
          throw new Error(`Lyon Stahl detail returned ${contentType}`);
        }
        const body = await lyonStahlBoundedResponseText(response, maxBytes);
        if (!body.trim()) throw new Error(`Lyon Stahl returned empty ${kind}`);
        return body;
      }
      throw new Error("Lyon Stahl exceeded redirect cap");
    } catch (error) {
      lastError = error;
      if (attempt < attempts) await new Promise((resolve) => setTimeout(resolve, 250 * attempt));
    } finally {
      clearTimeout(timer);
    }
  }
  throw lastError;
}

export async function srcLyonStahl(
  tx: Tx,
  max: number,
  _monitor: boolean
): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "Lyon Stahl",
      sourceUrl: LYON_STAHL_SOURCE_URL,
      method: "Investment-sales only; no public lease inventory",
      totalAvailable: 0,
      listings: [],
      note: "Lyon Stahl publishes sale listings only.",
    };
  }

  const indexXml = await lyonStahlFetchText(LYON_STAHL_SITEMAP_URL, "xml");
  const propertySitemaps = lyonStahlPropertySitemaps(indexXml);
  if (!propertySitemaps.length) {
    throw new Error("Lyon Stahl: sitemap index has no same-host properties-sitemapN.xml");
  }
  const propertyXml = await pmap(
    propertySitemaps,
    Math.min(propertySitemaps.length, 2),
    (url) => lyonStahlFetchText(url, "xml")
  );
  const propertySnapshots = propertyXml.map((xml, index) => {
    try {
      return lyonStahlPropertyUrls(xml);
    } catch (error) {
      throw new Error(`Lyon Stahl property sitemap ${propertySitemaps[index]} is invalid: ${String(error)}`);
    }
  });
  const urls = dedupeStrings(propertySnapshots.flat());

  const inventoryObservedAt = new Date().toISOString();
  const take = Number.isFinite(max) ? urls.slice(0, Math.max(0, max)) : urls;
  const outcomes = await pmap(take, LYON_STAHL_DETAIL_CONCURRENCY, async (url) => {
    try {
      const html = await lyonStahlFetchText(url, "html");
      return parseLyonStahlDetail(html, url, tx, {
        inventoryObservedAt,
        detailObservedAt: new Date().toISOString(),
      });
    } catch (error) {
      console.error(`  lyon-stahl/${tx}: ${url} failed: ${error}`);
      return { kind: "rejected", reason: String(error) } as LyonStahlParseOutcome;
    }
  });
  const listings = outcomes
    .filter((outcome): outcome is Extract<LyonStahlParseOutcome, { kind: "accepted" }> => outcome.kind === "accepted")
    .map((outcome) => outcome.listing);
  assertLyonStahlUniqueProviderIds(listings);
  const held = outcomes.filter((outcome) => outcome.kind === "held").length;
  const terminal = outcomes.filter((outcome) => outcome.kind === "terminal").length;
  const rejected = outcomes.filter((outcome) => outcome.kind === "rejected").length;
  const finiteCap = take.length !== urls.length;
  const truncated = finiteCap || held > 0 || rejected > 0;
  const notes = [
    finiteCap ? `Selected ${take.length}/${urls.length} property sitemap URL(s)` : null,
    held ? `${held} detail page(s) held for unknown availability` : null,
    terminal ? `${terminal} terminal detail page(s) excluded` : null,
    rejected ? `${rejected} detail page(s) failed identity or structure validation` : null,
  ].filter((value): value is string => Boolean(value));

  return {
    company: "Lyon Stahl",
    sourceUrl: LYON_STAHL_SOURCE_URL,
    method:
      "WordPress properties-sitemapN.xml enumeration to same-host HTML details; stable shortlink identity plus RealEstateListing JSON-LD; explicit Offer.availability admission",
    totalAvailable: urls.length,
    listings,
    truncated,
    note: notes.length ? notes.join("; ") : undefined,
  };
}
