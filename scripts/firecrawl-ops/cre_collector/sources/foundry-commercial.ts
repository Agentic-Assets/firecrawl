import {
  cancelResponseBody,
  readBoundedUtf8,
  ResponseBodyDeadlineError,
  ResponseBodyLimitError,
  settleResponseBodyRead,
} from "../lib/response-body.js";
import { observationNow } from "../lib/observation-clock.js";
import { RetainedSourceError } from "../lib/source-observations.js";
import * as cheerio from "cheerio";
import { CONCURRENCY } from "../lib/config.js";
import { refreshGenerationId, requireFreshDetails } from "../lib/freshness.js";
import { dedupeStrings, jsonLdObjects } from "../lib/html.js";
import { clean, pmap, prune } from "../lib/util.js";
import type { SourceResult, Tx } from "../types.js";
import {
  foundryPropertySitemaps,
  foundryPropertyNotes,
  foundryPropertyUrls,
  foundryProviderIdentity,
  foundryListingId,
  foundryUrl,
  FOUNDRY_HOST,
  FOUNDRY_SITEMAP_URL,
  samePage,
} from "./pure/foundry-identity.js";
import { classifyFoundryStatus, explicitFoundryTenures, type FoundryStatusDecision } from "./pure/foundry-status.js";

export const FOUNDRY_SOURCE_URL = `${FOUNDRY_HOST}/properties/`;
export const FOUNDRY_FETCH_TIMEOUT_MS = 60_000;
export const FOUNDRY_MAX_RESPONSE_BYTES = 32 * 1024 * 1024;

const FOUNDRY_DETAIL_CONCURRENCY = Math.min(CONCURRENCY, 2);
const FOUNDRY_NON_PHOTO = /avatar|headshot|logo|favicon|placeholder|sprite|cropped-/i;
const FOUNDRY_ASSET_QUERY_KEYS = new Set(["ver", "w"]);
export {
  foundryPropertySitemaps,
  foundryPropertyUrls,
  foundryProviderIdentity,
  foundryListingId,
  FOUNDRY_HOST,
  FOUNDRY_SITEMAP_URL,
} from "./pure/foundry-identity.js";
export { classifyFoundryStatus, type FoundryStatusDecision } from "./pure/foundry-status.js";

export type FoundryParseContext = {
  inventoryObservedAt?: string;
  detailObservedAt?: string;
  strict?: boolean;
};

type FoundryHeldObservation = {
  id: string;
  foundryPostId: string;
  url: string;
  propertyNotes: string[];
  status: string | null;
  reason: string;
  requestedTransaction: Tx;
  inventoryObservedAt?: string;
  detailObservedAt?: string;
};

export type FoundryParseOutcome =
  | { kind: "accepted"; listing: any; status: string }
  | { kind: "terminal"; status: string; reason: string }
  | { kind: "other_tenure"; status: string; reason: string }
  | { kind: "held"; status: string | null; reason: string; observation: FoundryHeldObservation }
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
  listings: Array<{ id?: unknown; foundryPostId?: unknown }>
): void {
  const ids = new Set<string>();
  const keys = new Set<string>();
  for (const listing of listings) {
    const id = clean(String(listing?.foundryPostId ?? ""));
    const key = clean(String(listing?.id ?? ""));
    if (!id || !/^[1-9]\d*$/.test(id) || !Number.isSafeInteger(Number(id))) throw new Error("Foundry emitted a listing without a valid provider ID");
    if (!key) throw new Error("Foundry emitted a listing without a listing key");
    if (ids.has(id)) throw new Error(`Foundry emitted duplicate provider ID ${id}`);
    if (keys.has(key)) throw new Error(`Foundry emitted duplicate listing key ${key}`);
    ids.add(id);
    keys.add(key);
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
  const notes = foundryPropertyNotes(html);
  const status = foundryStatusFromNotes(notes);
  const heldOutcome = (reason: string): Extract<FoundryParseOutcome, { kind: "held" }> => ({
    kind: "held",
    status: status.status,
    reason,
    observation: {
      id: foundryListingId(requestedUrl),
      foundryPostId: identity,
      url: requestedUrl,
      propertyNotes: [...notes],
      status: status.status,
      reason,
      requestedTransaction: tx,
      ...(context.inventoryObservedAt !== undefined ? { inventoryObservedAt: context.inventoryObservedAt } : {}),
      ...(context.detailObservedAt !== undefined ? { detailObservedAt: context.detailObservedAt } : {}),
    },
  });
  if (status.disposition === "terminal") {
    return {
      kind: "terminal",
      status: status.status ?? "terminal",
      reason: status.reason,
    };
  }
  if (status.disposition === "held") {
    return heldOutcome(status.reason);
  }
  const tenures = status.tenures.length ? status.tenures : explicitFoundryTenures(notes);
  if (!tenures.length) {
    return heldOutcome(`${status.reason}; no explicit sale or lease tenure found`);
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
    id: foundryListingId(requestedUrl),
    foundryPostId: identity,
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

const FOUNDRY_RETRYABLE_NATIVE_CODES = new Set([
  "ETIMEDOUT",
  "ECONNRESET",
  "UND_ERR_SOCKET",
  "UND_ERR_CONNECT_TIMEOUT",
  "UND_ERR_HEADERS_TIMEOUT",
  "UND_ERR_BODY_TIMEOUT",
]);
const FOUNDRY_RETRYABLE_HTTP_STATUSES = new Set([429, 500, 502, 503, 504]);

class FoundryRetryableFetchError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "FoundryRetryableFetchError";
  }
}

function foundryIsTransientTransportFailure(error: unknown, timedOut: boolean): boolean {
  if (timedOut) return true;
  if (error instanceof DOMException && error.name === "TimeoutError") return true;
  if (!(error instanceof TypeError)) return false;
  const cause = (error as TypeError & { cause?: unknown }).cause;
  if (typeof cause !== "object" || cause === null || !("code" in cause)) return false;
  const code = (cause as { code?: unknown }).code;
  return typeof code === "string" && FOUNDRY_RETRYABLE_NATIVE_CODES.has(code);
}

/** A failed or stalled cancel must never authorize another request. */
async function foundryCancelBody(
  body: { cancel(): Promise<unknown> } | null,
  timeoutMs = 1_000
): Promise<boolean> {
  return cancelResponseBody(body, Math.min(timeoutMs, 1_000));
}

async function foundryBoundedResponseText(
  response: Response,
  maxBytes: number,
  timeoutMs: number,
  controller: AbortController,
  deadlineAt: number
): Promise<string> {
  const declaredLength = Number(response.headers.get("content-length"));
  if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
    await foundryCancelBody(response.body, timeoutMs);
    throw new Error(`Foundry response exceeds ${maxBytes} bytes`);
  }
  if (!response.body) return "";
  const reader = response.body.getReader();
  try {
    return await readBoundedUtf8(reader, { maxBytes, signal: controller.signal, deadlineAt });
  } catch (error) {
    // A deterministic size refusal remains terminal even if cleanup aborts.
    if (error instanceof ResponseBodyLimitError) {
      if (!await foundryCancelBody(reader, timeoutMs)) {
        throw new Error("Foundry response body did not settle after exceeding its size limit", { cause: error });
      }
      throw new Error(`Foundry response exceeds ${maxBytes} bytes`, { cause: error });
    }
    const timedOut = error instanceof ResponseBodyDeadlineError || controller.signal.aborted;
    const cause = timedOut
      ? new Error(`Foundry response body timed out after ${timeoutMs}ms`, { cause: error }) : error;
    if (error instanceof ResponseBodyDeadlineError) controller.abort(cause);
    if (!await settleResponseBodyRead(reader, error, Math.min(timeoutMs, 1_000))) {
      throw new Error(timedOut
        ? `Foundry response body timed out after ${timeoutMs}ms and did not cancel`
        : "Foundry response body read failed and did not cancel", { cause });
    }
    if (!foundryIsTransientTransportFailure(error, timedOut)) throw cause;
    const message = cause instanceof Error ? cause.message : String(cause);
    throw new FoundryRetryableFetchError(`Foundry response body read failed: ${message}`, { cause });
  } finally {
    try { reader.releaseLock(); } catch { /* preserve the primary outcome */ }
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
    const deadlineAt = performance.now() + timeoutMs;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      let current = url;
      for (let redirect = 0; redirect <= 3; redirect++) {
        const allowed = foundryUrl(current, kind === "html" ? "detail" : "sitemap");
        if (!allowed) throw new Error(`Foundry refused unsafe ${kind} URL`);
        const strict = requireFreshDetails();
        let response: Response;
        try {
          response = await fetchImpl(allowed, {
            redirect: "manual",
            signal: controller.signal,
            headers: {
              "User-Agent": "Mozilla/5.0 CRE collector",
              Accept: kind === "html" ? "text/html,application/xhtml+xml" : "application/xml,text/xml,text/plain",
              ...(strict ? { "Cache-Control": "no-cache" } : {}),
            },
            ...(strict ? { cache: "no-store" as const } : {}),
          });
        } catch (error) {
          if (foundryIsTransientTransportFailure(error, controller.signal.aborted)) {
            const message = error instanceof Error ? error.message : String(error);
            throw new FoundryRetryableFetchError(`Foundry transient transport failure: ${message}`, { cause: error });
          }
          throw error;
        }
        if (response.status >= 300 && response.status < 400) {
          const location = response.headers.get("location");
          if (!location) {
            await foundryCancelBody(response.body);
            throw new Error(`Foundry redirect ${response.status} lacks Location`);
          }
          let next: string;
          try {
            next = new URL(location, allowed).toString();
          } catch {
            await foundryCancelBody(response.body);
            throw new Error(`Foundry redirect ${response.status} has invalid Location`);
          }
          if (!await foundryCancelBody(response.body, timeoutMs)) {
            throw new Error("Foundry redirect body did not settle before the next request");
          }
          current = next;
          continue;
        }
        if (!response.ok) {
          const status = response.status;
          const hasRetryAfter = response.headers.has("retry-after");
          const cancelled = await foundryCancelBody(response.body, timeoutMs);
          if (!cancelled) throw new Error(`Foundry HTTP ${status} response body did not settle`);
          if (FOUNDRY_RETRYABLE_HTTP_STATUSES.has(status) && !hasRetryAfter) {
            throw new FoundryRetryableFetchError(`Foundry HTTP ${status}`);
          }
          throw new Error(`Foundry HTTP ${status}`);
        }
        const contentType = response.headers.get("content-type")?.toLowerCase() ?? "";
        if (contentType.includes("application/pdf")) {
          await foundryCancelBody(response.body, timeoutMs);
          throw new Error("Foundry refused PDF response");
        }
        if (
          contentType
          && kind === "html"
          && !contentType.includes("text/html")
          && !contentType.includes("application/xhtml+xml")
        ) {
          await foundryCancelBody(response.body, timeoutMs);
          throw new Error(`Foundry detail returned ${contentType}`);
        }
        const body = await foundryBoundedResponseText(response, maxBytes, timeoutMs, controller, deadlineAt);
        if (!body.trim()) throw new Error(`Foundry returned empty ${kind}`);
        return body;
      }
      throw new Error("Foundry exceeded redirect cap");
    } catch (error) {
      lastError = error;
      if (!(error instanceof FoundryRetryableFetchError) || attempt >= attempts) break;
      await new Promise((resolve) => setTimeout(resolve, 250 * attempt));
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

  const inventoryObservedAt = observationNow();
  const take = Number.isFinite(max) ? urls.slice(0, Math.max(0, max)) : urls;
  let fetchFailures = 0;
  const outcomes = await pmap(take, FOUNDRY_DETAIL_CONCURRENCY, async (url) => {
    try {
      const html = await foundryFetchText(url, "html");
      const outcome = parseFoundryCommercialDetail(html, url, tx, {
        inventoryObservedAt,
        detailObservedAt: observationNow(),
      });
      if (outcome.kind === "held") {
        console.error(JSON.stringify({
          event: "foundry_held_observation",
          schema: "foundry-held-observation-v1",
          observation: outcome.observation,
        }));
      }
      return outcome;
    } catch (error) {
      fetchFailures++;
      console.error(`  foundry-commercial/${tx}: ${url} failed: ${error}`);
      return { kind: "rejected", reason: String(error) } as FoundryParseOutcome;
    }
  });
  const listings = outcomes
    .filter((outcome): outcome is Extract<FoundryParseOutcome, { kind: "accepted" }> => outcome.kind === "accepted")
    .map((outcome) => outcome.listing);
  const retainedObservations = outcomes
    .filter((outcome): outcome is Extract<FoundryParseOutcome, { kind: "held" }> => outcome.kind === "held")
    .map((outcome) => ({
      event: "foundry_held_observation",
      schema: "foundry-held-observation-v1",
      observation: outcome.observation,
    }));
  try {
    assertFoundryUniqueProviderIds(listings);
  } catch (error) {
    if (!(error instanceof Error)) throw error;
    throw new RetainedSourceError(error, [
      ...retainedObservations,
      ...outcomes.filter((outcome): outcome is Extract<FoundryParseOutcome, { kind: "accepted" }> => outcome.kind === "accepted")
        .map((outcome) => ({
        event: "foundry_identity_conflict_observation",
        schema: "foundry-identity-conflict-observation-v1",
        reason: "source_identity_validation_failed",
        status: outcome.status,
        observation: outcome.listing,
      })),
    ]);
  }
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
    retainedObservations,
    truncated,
    note: notes.length ? notes.join("; ") : undefined,
  };
}
