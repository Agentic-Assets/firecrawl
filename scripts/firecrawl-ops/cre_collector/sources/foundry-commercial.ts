import * as cheerio from "cheerio";
import { CONCURRENCY } from "../lib/config.js";
import { refreshGenerationId, requireFreshDetails } from "../lib/freshness.js";
import { dedupeStrings, jsonLdObjects } from "../lib/html.js";
import { clean, pmap, prune } from "../lib/util.js";
import type { SourceResult, Tx } from "../types.js";

export const FOUNDRY_HOST = "https://www.foundrycommercial.com";
export const FOUNDRY_SOURCE_URL = `${FOUNDRY_HOST}/properties/`;
export const FOUNDRY_SITEMAP_URL = `${FOUNDRY_HOST}/sitemap.xml`;
export const FOUNDRY_FETCH_TIMEOUT_MS = 60_000;
export const FOUNDRY_MAX_RESPONSE_BYTES = 32 * 1024 * 1024;

const FOUNDRY_DETAIL_CONCURRENCY = Math.min(CONCURRENCY, 2);
const FOUNDRY_NON_PHOTO = /avatar|headshot|logo|favicon|placeholder|sprite|cropped-/i;
const FOUNDRY_ASSET_QUERY_KEYS = new Set(["ver", "w"]);
const FOUNDRY_PROPERTY_SITEMAP_PATH = /(?:^|\/)property-sitemap(?:\d+)?\.xml$/i;
const FOUNDRY_TERMINAL_STATUSES = new Set([
  "closed",
  "discontinued",
  "leased",
  "off market",
  "sold",
  "unavailable",
  "withdrawn",
]);

export type FoundryStatusDecision = {
  disposition: "active" | "terminal" | "held";
  status: string | null;
  tenures: Tx[];
  reason: string;
};

export type FoundryParseContext = {
  inventoryObservedAt?: string;
  detailObservedAt?: string;
  strict?: boolean;
};

export type FoundryParseOutcome =
  | { kind: "accepted"; listing: any; status: string }
  | { kind: "terminal"; status: string; reason: string }
  | { kind: "other_tenure"; status: string; reason: string }
  | { kind: "held"; status: string | null; reason: string }
  | { kind: "rejected"; reason: string };

type FetchLike = (
  input: string | URL | Request,
  init?: RequestInit
) => Promise<Response>;

function foundryAssetQueryIsBenign(url: URL): boolean {
  const seen = new Set<string>();
  for (const [key, value] of url.searchParams) {
    if (!FOUNDRY_ASSET_QUERY_KEYS.has(key) || seen.has(key)) return false;
    seen.add(key);
    if (key === "w" && !/^[1-9]\d{0,4}$/.test(value)) return false;
    if (key === "ver" && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(value)) return false;
  }
  return true;
}

function normalizedStatus(value: string | null): string | null {
  return clean(value)
    ?.toLowerCase()
    .replace(/[_-]+/g, " ")
    .replace(/\s*[/|]\s*/g, " or ")
    .replace(/\s+/g, " ")
    .trim() ?? null;
}

/**
 * Foundry publishes an explicit WordPress property-status taxonomy. Only the
 * documented tokens below may admit a row. Missing or novel status text is
 * held so a theme or taxonomy change cannot silently activate old inventory.
 */
export function classifyFoundryStatus(value: string | null): FoundryStatusDecision {
  const status = normalizedStatus(value);
  if (!status) {
    return {
      disposition: "held",
      status: null,
      tenures: [],
      reason: "missing explicit Foundry property status",
    };
  }
  if (FOUNDRY_TERMINAL_STATUSES.has(status)) {
    return {
      disposition: "terminal",
      status,
      tenures: [],
      reason: `terminal Foundry property status: ${status}`,
    };
  }
  if (status === "for sale") {
    return { disposition: "active", status, tenures: ["sale"], reason: "explicit for-sale status" };
  }
  if (status === "for lease" || status === "sublease") {
    return { disposition: "active", status, tenures: ["lease"], reason: `explicit ${status} status` };
  }
  if (
    status === "for sale or lease"
    || status === "for lease or sale"
    || status === "sale and lease"
  ) {
    return {
      disposition: "active",
      status,
      tenures: ["sale", "lease"],
      reason: "explicit dual-tenure status",
    };
  }
  if (
    status === "available"
    || status === "coming soon"
    || status === "proposed"
    || status === "under contract"
  ) {
    return {
      disposition: "active",
      status,
      tenures: [],
      reason: `active status ${status} requires a separate explicit transaction token`,
    };
  }
  return {
    disposition: "held",
    status,
    tenures: [],
    reason: `unknown Foundry property status: ${status}`,
  };
}

function foundryUrl(value: string, kind: "detail" | "sitemap"): string | null {
  try {
    const parsed = new URL(value, FOUNDRY_HOST);
    if (
      parsed.protocol !== "https:"
      || parsed.hostname.toLowerCase().replace(/^www\./, "") !== "foundrycommercial.com"
      || parsed.username
      || parsed.password
      || parsed.port
      || parsed.search
      || parsed.hash
    ) {
      return null;
    }
    if (/\.pdf$/i.test(parsed.pathname)) return null;
    if (kind === "detail" && !/^\/property\/[^/]+\/?$/i.test(parsed.pathname)) return null;
    if (
      kind === "sitemap"
      && !/^\/(?:sitemap|sitemap_index|property-sitemap(?:\d+)?)\.xml$/i.test(parsed.pathname)
    ) {
      return null;
    }
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

export function foundryPropertySitemaps(indexXml: string): string[] {
  const locations = sitemapLocations(indexXml, "sitemapindex", "sitemap", "Foundry sitemap index");
  const urls = dedupeStrings(
    locations.flatMap((value, index) => {
      let propertyShaped = false;
      try {
        propertyShaped = FOUNDRY_PROPERTY_SITEMAP_PATH.test(
          new URL(value, FOUNDRY_HOST).pathname
        );
      } catch {
        propertyShaped = FOUNDRY_PROPERTY_SITEMAP_PATH.test(value.split(/[?#]/, 1)[0]);
      }
      if (!propertyShaped) return [];
      const url = foundryUrl(value, "sitemap");
      if (!url) throw new Error(`Foundry sitemap index property loc ${index} is invalid`);
      return [url];
    })
  );
  if (!urls.length) throw new Error("Foundry sitemap index has no valid property sitemap");
  return urls;
}

export function foundryPropertyUrls(propertyXml: string): string[] {
  const locations = sitemapLocations(propertyXml, "urlset", "url", "Foundry property sitemap");
  const urls = dedupeStrings(
    locations.map((value, index) => {
      const url = foundryUrl(value, "detail");
      if (!url) throw new Error(`Foundry property sitemap URL ${index} is invalid`);
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

export function foundryAssetUrl(value: unknown): string | null {
  const raw = clean(value);
  if (!raw) return null;
  try {
    const url = new URL(raw, FOUNDRY_HOST);
    if (
      url.protocol !== "https:"
      || url.username
      || url.password
      || url.port
      || Boolean(url.hash)
      || !foundryAssetQueryIsBenign(url)
    ) {
      return null;
    }
    return url.toString();
  } catch {
    return null;
  }
}

export function assertFoundryUniqueProviderIds(
  listings: Array<{ id?: unknown }>
): void {
  const ids = new Set<string>();
  for (const listing of listings) {
    const id = clean(String(listing?.id ?? ""));
    if (!id) throw new Error("Foundry emitted a listing without a provider ID");
    if (ids.has(id)) throw new Error(`Foundry emitted duplicate provider ID ${id}`);
    ids.add(id);
  }
}

export function foundryProviderIdentity(html: string, requestedUrl: string): string | null {
  const requested = foundryUrl(requestedUrl, "detail");
  if (!requested) return null;
  const $ = cheerio.load(html);
  const canonicalRaw =
    clean($("link[rel='canonical']").first().attr("href"))
    ?? clean($("meta[property='og:url']").first().attr("content"));
  const canonical = canonicalRaw ? foundryUrl(canonicalRaw, "detail") : null;
  if (!canonical || !samePage(canonical, requested)) return null;

  const shortlink = clean($("link[rel='shortlink']").first().attr("href"));
  if (!shortlink) return null;
  try {
    const parsed = new URL(shortlink);
    if (
      parsed.origin !== FOUNDRY_HOST
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

function numeric(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(String(value).replace(/[$,\s]/g, ""));
  return Number.isFinite(number) && number > 0 ? number : null;
}

function foundryAddress(raw: any): {
  street: string | null;
  city: string | null;
  state: string | null;
  postalCode: string | null;
  country: string | null;
} {
  const out = {
    street: clean(raw?.streetAddress),
    city: clean(raw?.addressLocality),
    state: clean(raw?.addressRegion),
    postalCode: clean(raw?.postalCode),
    country: clean(raw?.addressCountry),
  };
  if (!out.street) return out;

  const parts = out.street.split(",").map((part) => part.trim()).filter(Boolean);
  if (parts.length >= 3 && !out.city) {
    out.street = parts.shift() ?? out.street;
    out.city = parts.shift() ?? null;
    const region = parts.shift() ?? "";
    const match = region.match(/^([A-Z]{2}|[A-Za-z ]+?)(?:\s+(\d{5}(?:-\d{4})?))?$/);
    out.state = out.state ?? clean(match?.[1]);
    out.postalCode = out.postalCode ?? clean(match?.[2]);
  }
  if (!out.postalCode) {
    out.postalCode = clean(out.street.match(/\b(\d{5}(?:-\d{4})?)\b/)?.[1]);
  }
  return out;
}

function foundryFactMap($: cheerio.CheerioAPI): Map<string, string> {
  const facts = new Map<string, string>();
  $("tr").each((_, row) => {
    const label = clean($(row).find("th").first().text())?.replace(/:$/, "");
    const value = clean($(row).find("td").first().text());
    if (label && value && !facts.has(label.toLowerCase())) {
      facts.set(label.toLowerCase(), value);
    }
  });
  return facts;
}

function fact(facts: Map<string, string>, pattern: RegExp): string | null {
  for (const [label, value] of facts) {
    if (pattern.test(label)) return value;
  }
  return null;
}

function foundryStatusFromNotes(notes: string[]): FoundryStatusDecision {
  return classifyFoundryStatus(notes[0] ?? null);
}

function explicitFoundryTenures(notes: string[]): Tx[] {
  const tenures = new Set<Tx>();
  for (const note of notes) {
    const normalized = normalizedStatus(note);
    if (!normalized) continue;
    if (/\bfor sale\b|\bsale and lease\b|\bfor sale or lease\b|\bfor lease or sale\b/.test(normalized)) {
      tenures.add("sale");
    }
    if (/\bfor lease\b|\bsublease\b|\bsale and lease\b|\bfor sale or lease\b|\bfor lease or sale\b/.test(normalized)) {
      tenures.add("lease");
    }
  }
  return [...tenures];
}

export function parseFoundryCommercialDetail(
  html: string,
  requestedUrl: string,
  tx: Tx,
  context: FoundryParseContext = {}
): FoundryParseOutcome {
  const strict = context.strict ?? requireFreshDetails();
  const identity = foundryProviderIdentity(html, requestedUrl);
  if (!identity) return { kind: "rejected", reason: "canonical or WordPress shortlink identity mismatch" };

  const $ = cheerio.load(html);
  const heading = clean($("h1").first().text());
  const visibleHeading = [clean($("title").first().text()), heading].filter(Boolean).join(" ");
  if (
    /\b(?:404|not found|access denied|forbidden|captcha|just a moment|service unavailable)\b/i.test(
      visibleHeading
    )
  ) {
    return { kind: "rejected", reason: "challenge or error shell" };
  }

  const nodes = jsonLdObjects(html);
  const listing = nodes.find((node) => ldTypes(node).some((type) => type === "RealEstateListing"));
  const listingUrl = clean(listing?.url);
  if (strict && (!listingUrl || !samePage(listingUrl, requestedUrl))) {
    return { kind: "rejected", reason: "RealEstateListing identity mismatch" };
  }
  const notes = $(".property-notes li")
    .map((_, element) => clean($(element).text()))
    .get()
    .filter((value): value is string => Boolean(value));
  const status = foundryStatusFromNotes(notes);
  if (status.disposition === "terminal") {
    return {
      kind: "terminal",
      status: status.status ?? "terminal",
      reason: status.reason,
    };
  }
  if (status.disposition === "held") {
    return { kind: "held", status: status.status, reason: status.reason };
  }
  const tenures = status.tenures.length ? status.tenures : explicitFoundryTenures(notes);
  if (!tenures.length) {
    return {
      kind: "held",
      status: status.status,
      reason: `${status.reason}; no explicit sale or lease tenure found`,
    };
  }
  if (!tenures.includes(tx)) {
    return {
      kind: "other_tenure",
      status: status.status ?? "active",
      reason: `explicit Foundry status does not include ${tx}`,
    };
  }

  const entity = listing?.mainEntity ?? {};
  const name = clean(listing?.name) ?? metaContent($, "og:title") ?? heading;
  if (!name) return { kind: "rejected", reason: "property page has no listing name" };
  const address = foundryAddress(entity?.address);
  const facts = foundryFactMap($);
  const priceText = fact(facts, /^(?:asking |sale |list )?price$/i);
  const floorSize = numeric(entity?.floorSize?.value);
  const availableText = fact(facts, /space available|available (?:sf|space)|building size/i);
  const buildingSizeSqft = floorSize ?? numeric(availableText);
  const lat = numeric(entity?.geo?.latitude);
  const lngRaw = entity?.geo?.longitude;
  const lng = lngRaw !== null && lngRaw !== undefined && Number.isFinite(Number(lngRaw))
    ? Number(lngRaw)
    : null;
  const assetType =
    clean(entity?.additionalType)
    ?? notes.find((note) => note.includes("/") && !/for sale|for lease/i.test(note))
    ?? null;
  const brochures = dedupeStrings(
    $("a.brochure-download-button[href], a.property-pdf[href]")
      .map((_, element) => {
        const href = clean($(element).attr("href"));
        if (!href) return null;
        const url = foundryAssetUrl(href);
        return url && /\.pdf$/i.test(new URL(url).pathname) ? url : null;
      })
      .get()
  ).map((url) => ({ name: "Property Brochure", url }));
  const photos = dedupeStrings(
    [
      ...(Array.isArray(listing?.image) ? listing.image : [listing?.image]),
      metaContent($, "og:image"),
      ...$(".slider--property img[src]")
        .map((_, element) => clean($(element).attr("src")))
        .get(),
    ]
      .map(foundryAssetUrl)
      .filter((url): url is string => Boolean(url))
  ).filter((url) => {
    try {
      const parsed = new URL(url, FOUNDRY_HOST);
      return (
        parsed.hostname.toLowerCase().replace(/^www\./, "") === "foundrycommercial.com"
        && /\/wp-content\//i.test(parsed.pathname)
        && !FOUNDRY_NON_PHOTO.test(parsed.pathname)
      );
    } catch {
      return false;
    }
  });

  const listingRow = prune({
    id: identity,
    inventoryObservedAt: context.inventoryObservedAt,
    detailObservedAt: context.detailObservedAt,
    freshnessProvenance:
      context.inventoryObservedAt && context.detailObservedAt
        ? {
            detailScope: "detail_page",
            generationId: refreshGenerationId(),
            method: "foundry_wordpress_detail",
            cacheDisposition: "live",
            identityMethod: "wordpress_shortlink_id",
          }
        : undefined,
    name,
    transactionType: tx === "sale" ? "Sale" : "Lease",
    assetType,
    description: clean(listing?.description) ?? clean($(".property-description").first().text()),
    street: address.street,
    city: address.city,
    state: address.state,
    postalCode: address.postalCode,
    country: address.country ?? "US",
    latitude: lat,
    longitude: lng,
    salePriceUsd: tx === "sale" ? numeric(priceText) : null,
    salePriceText: tx === "sale" ? priceText : null,
    leaseRateText: tx === "lease" ? fact(facts, /lease rate|asking rent/i) : null,
    sizeText: availableText,
    buildingSizeSqft,
    statusBadge: status.status,
    extraFacts: { providerStatus: status.status },
    brochures,
    photos,
    url: foundryUrl(requestedUrl, "detail"),
    canonicalUrl: foundryUrl(requestedUrl, "detail"),
    lastUpdated: metaContent($, "article:modified_time"),
    brokerIds: [],
  });
  return { kind: "accepted", listing: listingRow, status: status.status ?? "active" };
}

type FetchKind = "html" | "xml";

async function foundryBoundedResponseText(
  response: Response,
  maxBytes: number
): Promise<string> {
  const declaredLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    throw new Error(`Foundry response exceeds ${maxBytes} bytes`);
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
        throw new Error(`Foundry response exceeds ${maxBytes} bytes`);
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    return text;
  } finally {
    reader.releaseLock();
  }
}

export async function foundryFetchText(
  url: string,
  kind: FetchKind,
  fetchImpl: FetchLike = fetch,
  timeoutMs = FOUNDRY_FETCH_TIMEOUT_MS,
  maxBytes = FOUNDRY_MAX_RESPONSE_BYTES,
  attempts = 3
): Promise<string> {
  let lastError: unknown = null;
  for (let attempt = 1; attempt <= attempts; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      let current = url;
      for (let redirect = 0; redirect <= 3; redirect++) {
        const allowed = foundryUrl(current, kind === "html" ? "detail" : "sitemap");
        if (!allowed) throw new Error(`Foundry refused unsafe ${kind} URL`);
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
          if (!location) throw new Error(`Foundry redirect ${response.status} lacks Location`);
          current = new URL(location, allowed).toString();
          continue;
        }
        if (!response.ok) throw new Error(`Foundry HTTP ${response.status}`);
        const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
        if (contentType.includes("application/pdf")) {
          throw new Error("Foundry refused PDF response");
        }
        if (
          contentType
          && kind === "html"
          && !contentType.includes("text/html")
          && !contentType.includes("application/xhtml+xml")
        ) {
          throw new Error(`Foundry detail returned ${contentType}`);
        }
        const body = await foundryBoundedResponseText(response, maxBytes);
        if (!body.trim()) throw new Error(`Foundry returned empty ${kind}`);
        return body;
      }
      throw new Error("Foundry exceeded redirect cap");
    } catch (error) {
      lastError = error;
      if (attempt < attempts) await new Promise((resolve) => setTimeout(resolve, 250 * attempt));
    } finally {
      clearTimeout(timer);
    }
  }
  throw lastError;
}

export async function srcFoundryCommercial(
  tx: Tx,
  max: number,
  _monitor: boolean
): Promise<SourceResult> {
  const indexXml = await foundryFetchText(FOUNDRY_SITEMAP_URL, "xml");
  const propertySitemaps = foundryPropertySitemaps(indexXml);
  if (!propertySitemaps.length) {
    throw new Error("Foundry: sitemap index has no same-host property sitemap");
  }
  const propertyXml = await pmap(
    propertySitemaps,
    Math.min(propertySitemaps.length, 2),
    (url) => foundryFetchText(url, "xml")
  );
  const propertySnapshots = propertyXml.map((xml, index) => {
    try {
      return foundryPropertyUrls(xml);
    } catch (error) {
      throw new Error(`Foundry property sitemap ${propertySitemaps[index]} is invalid: ${String(error)}`);
    }
  });
  const urls = dedupeStrings(propertySnapshots.flat());

  const inventoryObservedAt = new Date().toISOString();
  const take = Number.isFinite(max) ? urls.slice(0, Math.max(0, max)) : urls;
  let fetchFailures = 0;
  const outcomes = await pmap(take, FOUNDRY_DETAIL_CONCURRENCY, async (url) => {
    try {
      const html = await foundryFetchText(url, "html");
      return parseFoundryCommercialDetail(html, url, tx, {
        inventoryObservedAt,
        detailObservedAt: new Date().toISOString(),
      });
    } catch (error) {
      fetchFailures++;
      console.error(`  foundry-commercial/${tx}: ${url} failed: ${error}`);
      return { kind: "rejected", reason: String(error) } as FoundryParseOutcome;
    }
  });
  const listings = outcomes
    .filter((outcome): outcome is Extract<FoundryParseOutcome, { kind: "accepted" }> => outcome.kind === "accepted")
    .map((outcome) => outcome.listing);
  assertFoundryUniqueProviderIds(listings);
  const held = outcomes.filter((outcome) => outcome.kind === "held").length;
  const rejected = outcomes.filter((outcome) => outcome.kind === "rejected").length;
  const terminal = outcomes.filter((outcome) => outcome.kind === "terminal").length;
  const otherTenure = outcomes.filter((outcome) => outcome.kind === "other_tenure").length;
  const finiteCap = take.length !== urls.length;
  const truncated = finiteCap || held > 0 || rejected > 0 || fetchFailures > 0;
  const notes = [
    finiteCap ? `Selected ${take.length}/${urls.length} property sitemap URL(s)` : null,
    held ? `${held} detail page(s) held for unknown or ambiguous status` : null,
    otherTenure ? `${otherTenure} detail page(s) belong only to the other transaction` : null,
    terminal ? `${terminal} terminal detail page(s) excluded` : null,
    rejected ? `${rejected} detail page(s) failed identity or structure validation` : null,
  ].filter((value): value is string => Boolean(value));

  return {
    company: "Foundry Commercial",
    sourceUrl: FOUNDRY_SOURCE_URL,
    method:
      "WordPress property sitemap enumeration to same-host HTML details; stable shortlink identity plus JSON-LD/DOM parsing; explicit status admission",
    totalAvailable: urls.length,
    listings,
    truncated,
    note: notes.length ? notes.join("; ") : undefined,
  };
}
