import { NativeEvidenceTerminalError } from "../lib/source-evidence/terminal-error.js";
import { rcmSourceControls, rcmPublicContactUrl, RCM_CONTACT_FLAGS } from "./pure/rcm-source-controls.js";
import { observationNow } from "../lib/observation-clock.js";
// sources/colliers.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY, PAGE_CAP } from "../lib/config.js";
import { harvestDetail } from "../lib/harvest.js";
import { decodeHtmlEntities, dedupeStrings, stripHtmlText, titleFromFilename } from "../lib/html.js";
import { refreshGenerationId } from "../lib/freshness.js";
import { parseJsonBody } from "../lib/scrape.js";
import { recordRetry } from "../lib/performance.js";
import { DocItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { parseLeaseRate, parseMoney } from "../lib/parse.js";
import { clean, num, pmap, prune } from "../lib/util.js";
import {
  COLLIERS_PAGE_SIZE as pureColliersPageSize,
  COLLIERS_RCM_BASE as pureColliersRcmBase,
  colliersHeaders as pureColliersHeaders,
  colliersListUrl as pureColliersListUrl,
  colliersMapUrl as pureColliersMapUrl,
  colliersSlpInitUrl as pureColliersSlpInitUrl,
  groupColliersMapLocations as pureGroupColliersMapLocations,
  parseColliersReceiptCards,
} from "./pure/colliers-receipt.js";


// --- Colliers: public SalesTracker RCM ListingEngine GET path ---

export const COLLIERS_SALESTRACKER_BASE = "https://sales.colliers.com";
export const COLLIERS_RCM_BASE = pureColliersRcmBase;
export const COLLIERS_SOURCE_URL = `${COLLIERS_SALESTRACKER_BASE}/`;
export const COLLIERS_FALLBACK_ENGINE_KEY = "BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4";
export const COLLIERS_PAGE_SIZE = pureColliersPageSize;
export const COLLIERS_DETAIL_CONCURRENCY = Math.min(CONCURRENCY, 2);

export type ColliersCard = {
  id: string | null;
  mapProjectId: string;
  url: string | null;
  detailUrl: string | null;
  detailPv: string | null;
  name: string | null;
  transactionType: string;
  assetType: string | null;
  status: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  salePriceUsd: number | null;
  salePriceText: string | null;
  sizeText: string | null;
  latitude: number | null;
  longitude: number | null;
  brokerIds: number[];
  contactsDetailed: any[];
  photos: string[];
  colliersSalesTrackerCard: Record<string, any>;
};

export type ColliersMapPin = {
  latitude: number;
  longitude: number;
};

export type ColliersMapGroup = {
  projectId: string;
  pins: ColliersMapPin[];
};

export class ColliersIdentityError extends Error {}
export class ColliersAccessStop extends Error {}

export function colliersHeaders(accept = "application/json, text/javascript, */*; q=0.01"): Record<string, string> {
  return pureColliersHeaders(accept);
}

export function colliersUrl(href: string | null | undefined): string | null {
  const h = clean(href ?? null);
  if (!h || /^javascript:/i.test(h) || /^mailto:/i.test(h) || /^tel:/i.test(h)) return null;
  try {
    return new URL(decodeHtmlEntities(h), COLLIERS_RCM_BASE).toString();
  } catch {
    return null;
  }
}

class ColliersHttpError extends Error {
  constructor(
    readonly status: number,
    readonly retryAfter: string | null,
    url: string
  ) {
    super(`Colliers GET ${url} HTTP ${status}`);
    this.name = "ColliersHttpError";
  }
}

export async function colliersGetText(url: string, signal?: AbortSignal, onRetryAfter?: (value: string | null) => void): Promise<string> {
  const timeoutSignal = AbortSignal.timeout(30000);
  const res = await fetch(url, {
    headers: colliersHeaders("text/html,application/json,*/*"),
    signal: signal ? AbortSignal.any([signal, timeoutSignal]) : timeoutSignal,
  });
  onRetryAfter?.(res.headers.get("retry-after"));
  if (!res.ok) {
    await res.body?.cancel().catch(() => undefined);
    throw new ColliersHttpError(res.status, res.headers.get("retry-after"), url);
  }
  return res.text();
}

class ColliersPageFailure extends Error {}

export async function colliersGetJson(url: string, signal?: AbortSignal, onText?: (text: string) => void, onRetryAfter?: (value: string | null) => void): Promise<any> {
  const text = await colliersGetText(url, signal, onRetryAfter);
  onText?.(text);
  const parsed = parseJsonBody(text);
  if (parsed === null) throw new Error(`Colliers GET ${url} returned non-JSON`);
  if (parsed?.success === false) {
    const error = `Colliers GET ${url} returned success=false`;
    if (["Failure retrieving listing HTML.", "Failure retrieving map."].includes(parsed?.error)) throw new ColliersPageFailure(error);
    throw new Error(error);
  }
  return parsed;
}

const COLLIERS_PAGE_PAIR_RETRY_DELAY_MS = 1000;
const COLLIERS_RETRYABLE_HTTP_STATUSES = new Set([500, 502, 503, 504]);
const COLLIERS_RETRYABLE_NATIVE_CODES = new Set([
  "ETIMEDOUT",
  "UND_ERR_CONNECT_TIMEOUT",
  "UND_ERR_HEADERS_TIMEOUT",
  "UND_ERR_BODY_TIMEOUT",
]);

function colliersPairRetryable(error: unknown): boolean {
  if (error instanceof ColliersHttpError) {
    return (
      COLLIERS_RETRYABLE_HTTP_STATUSES.has(error.status) &&
      error.retryAfter === null
    );
  }
  if (error instanceof DOMException && error.name === "TimeoutError") return true;
  if (!(error instanceof TypeError)) return false;
  const cause = (error as TypeError & { cause?: unknown }).cause;
  if (typeof cause !== "object" || cause === null || !("code" in cause)) return false;
  const code = (cause as { code?: unknown }).code;
  return typeof code === "string" && COLLIERS_RETRYABLE_NATIVE_CODES.has(code);
}

function colliersSiblingAbort(error: unknown, firstFailure: unknown): boolean {
  return error !== firstFailure && error instanceof Error && error.name === "AbortError";
}

async function colliersFetchPagePair(
  listUrl: string,
  mapUrl: string,
  onResponses?: (attempt: number, responses: { list?: string; map?: string }) => void,
  maxAttempts = 2,
): Promise<[any, any]> {
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const controller = new AbortController();
    let firstFailure: unknown;
    const responses: { list?: string; map?: string } = {};
    let retryAfter = false;
    const request = async (url: string, kind: "list" | "map"): Promise<any> => {
      try {
        return await colliersGetJson(url, controller.signal, text => { responses[kind] = text; }, value => { if (value !== null) retryAfter = true; });
      } catch (error) {
        // A semantic page failure must not hide a later denial from its sibling.
        if (!(error instanceof ColliersPageFailure) && !controller.signal.aborted) {
          firstFailure = error;
          controller.abort(error);
        }
        throw error;
      }
    };
    const settled = await Promise.allSettled([request(listUrl, "list"), request(mapUrl, "map")]);
    if (Object.keys(responses).length) onResponses?.(attempt, responses);
    const failures = settled
      .filter((result): result is PromiseRejectedResult => result.status === "rejected")
      .map((result) => result.reason)
      .filter((error) => !colliersSiblingAbort(error, firstFailure));
    if (retryAfter) {
      const failure = failures.find(error => error instanceof ColliersHttpError);
      if (failure) throw failure;
      throw new Error("Colliers page response requested Retry-After");
    }
    if (!failures.length) {
      return [
        (settled[0] as PromiseFulfilledResult<any>).value,
        (settled[1] as PromiseFulfilledResult<any>).value,
      ];
    }

    const nonRetryableIndex = failures.findIndex((error) => !colliersPairRetryable(error));
    if (nonRetryableIndex >= 0) {
      // Any observed denial or other failure wins over a source-reported page failure.
      const nonRetryable = failures.find(error => !(error instanceof ColliersPageFailure) && !colliersPairRetryable(error)) ??
        failures.find(error => !(error instanceof ColliersPageFailure)) ?? failures[nonRetryableIndex];
      if (nonRetryable === undefined) {
        throw new Error("Colliers paired page request failed without an error value");
      }
      throw nonRetryable;
    }
    if (attempt === maxAttempts) throw failures[0];

    const waitStarted = performance.now();
    await new Promise<void>((resolve) => setTimeout(resolve, COLLIERS_PAGE_PAIR_RETRY_DELAY_MS));
    recordRetry(
      "http_helper",
      Math.max(0, Math.round(performance.now() - waitStarted)),
      true
    );
  }
  throw new Error("Colliers page-pair retry exhausted unexpectedly");
}

export function extractColliersEngineKey(html: string): string {
  return (
    html.match(/new\s+ListingEngine\s*\(\s*\{[\s\S]*?key\s*:\s*["']([^"']+)/i)?.[1] ??
    html.match(/pv=([A-Za-z0-9_-]{30,})/)?.[1] ??
    COLLIERS_FALLBACK_ENGINE_KEY
  );
}

export function colliersListUrl(engineKey: string, start: number, pageSize: number): string {
  return pureColliersListUrl(engineKey, start, pageSize);
}

export function colliersMapUrl(engineKey: string, start: number, pageSize: number): string {
  return pureColliersMapUrl(engineKey, start, pageSize);
}

export function colliersSlpInitUrl(pv: string): string {
  return pureColliersSlpInitUrl(pv);
}

export function parseColliersLocation(text: string | null): { city: string | null; state: string | null } {
  const normalized = clean((text ?? "").replace(/\u201A/g, ",").replace(/\u00a0/g, " "));
  const match = normalized?.match(/^(.+?),\s*([A-Z]{2})\b/);
  return {
    city: match ? clean(match[1]) : null,
    state: match?.[2] ?? null,
  };
}

export function listingPvFromColliersUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).searchParams.get("pv");
  } catch {
    return null;
  }
}

export function groupColliersMapLocations(rows: any[]): ColliersMapGroup[] {
  return pureGroupColliersMapLocations(rows);
}

export function colliersMapScalarCoordinates(group: ColliersMapGroup): {
  latitude: number | null;
  longitude: number | null;
} {
  if (group.pins.length !== 1) {
    return { latitude: null, longitude: null };
  }
  return {
    latitude: group.pins[0]!.latitude,
    longitude: group.pins[0]!.longitude,
  };
}

export function colliersContactsFromCard($: cheerio.CheerioAPI, card: cheerio.Cheerio<any>): any[] {
  const contacts: any[] = [];
  card.find(".contacts .contact").each((_, el) => {
    const row = $(el);
    const name = clean(row.find(".name").first().text());
    const email = clean(row.find('a[href^="mailto:"]').first().attr("href")?.replace(/^mailto:/i, ""));
    const phone =
      clean(row.find(".phone").first().text()) ??
      clean(row.find('a[href^="tel:"]').first().attr("href")?.replace(/^tel:/i, ""));
    const company = clean(row.find(".company").first().text());
    const title = clean(row.find(".title").first().text());
    const license = clean(row.find(".license").first().text());
    if (!name && !email && !phone && !company && !title && !license) return;
    contacts.push(
      prune({
        name,
        email,
        phone,
        company,
        title,
        license,
        rcmContactSource: "colliers_sales_tracker",
      }) ?? {}
    );
  });
  return contacts;
}

export function parseColliersCards(
  html: string,
  mapGroups: ColliersMapGroup[],
  start: number,
  registerBrokers = true
): ColliersCard[] {
  const $ = cheerio.load(html);
  const cards: ColliersCard[] = [];
  $("li.item").each((idx, el) => {
    const card = $(el);
    if (!clean(card.text())) return;
    const detailUrl = colliersUrl(
      card.find('a[href*="landing.aspx"], a[href*="modern.aspx"], a[href*="/slp/"]').first().attr("href")
    );
    const detailPv = listingPvFromColliersUrl(detailUrl);
    const mapGroup = mapGroups[idx];
    if (!mapGroup) {
      throw new Error(
        `Colliers card ${start + idx} has no ordered ProjectId map group`
      );
    }
    const mapCoordinates = colliersMapScalarCoordinates(mapGroup);
    const location = parseColliersLocation(card.find(".city").first().text());
    const photo = colliersUrl(card.find("img").first().attr("src"));
    const name = clean(card.find(".headline").first().text());
    const assetType = clean(card.find(".asset").first().text());
    const salePriceText = clean(card.find(".price").first().text());
    const sizeText = clean(card.find(".sq-ft").first().text());
    const id = `salestracker:card:${mapGroup.projectId}`;
    const contactsDetailed = colliersContactsFromCard($, card);
    const brokerIds = !registerBrokers ? [] : contactsDetailed
      .map((c) => {
        const company = clean(c.company);
        return company ? brokerRef({ name: clean(c.name), email: clean(c.email), phone: clean(c.phone), company }) : null;
      })
      .filter((brokerId: number | null): brokerId is number => brokerId !== null);
    cards.push({
      id,
      mapProjectId: mapGroup.projectId,
      url: detailUrl,
      detailUrl,
      detailPv,
      name,
      transactionType: "Investment Sale",
      assetType,
      status: clean(card.find(".status").first().text()),
      city: location.city,
      state: location.state,
      country: "US",
      salePriceUsd: parseMoney(salePriceText),
      salePriceText,
      sizeText,
      latitude: mapCoordinates.latitude,
      longitude: mapCoordinates.longitude,
      brokerIds,
      contactsDetailed,
      photos: photo ? [photo] : [],
      colliersSalesTrackerCard: prune({
        detailPv,
        projectId: mapGroup.projectId,
        hasDetailUrl: Boolean(detailUrl),
        cardIndex: start + idx,
        identitySource: "ordered-map-project-group",
        mapPinCount: mapGroup.pins.length,
        mapPins: mapGroup.pins,
      }) ?? {},
    });
  });
  if (cards.length !== mapGroups.length) {
    throw new Error(
      `Colliers parsed-card/map-group parity failed at start ${start}: ${cards.length} != ${mapGroups.length}`
    );
  }
  // The receipt lane and collector must agree on the provider-native card
  // identity, even though the collector additionally enriches each card.
  const receiptCards = parseColliersReceiptCards(html, mapGroups, start);
  for (const [index, card] of cards.entries()) {
    const receiptCard = receiptCards[index];
    if (!receiptCard || card.mapProjectId !== receiptCard.mapProjectId || card.detailPv !== receiptCard.detailPv || card.detailUrl !== receiptCard.detailUrl) {
      throw new Error("Colliers collector/receipt card identity parser drifted");
    }
  }
  return cards;
}

export function colliersNumProjects(value: unknown, start: number): number {
  if (
    value === null ||
    value === undefined ||
    (typeof value === "string" && value.trim() === "") ||
    (typeof value !== "string" && typeof value !== "number")
  ) {
    throw new Error(
      `Colliers returned invalid numProjects=${JSON.stringify(value)} at start ${start}`
    );
  }
  const count = Number(value);
  if (!Number.isInteger(count) || count < 0) {
    throw new Error(
      `Colliers returned invalid numProjects=${JSON.stringify(value)} at start ${start}`
    );
  }
  return count;
}

export function colliersAssertPageCount(
  value: unknown,
  providerCardCount: number,
  start: number
): number {
  const count = colliersNumProjects(value, start);
  if (count !== providerCardCount) {
    throw new Error(
      `Colliers numProjects/card parity failed at start ${start}: ${count} != ${providerCardCount}`
    );
  }
  return count;
}

export function colliersAssertDetailProjectId(
  mapProjectId: string,
  detailProjectId: unknown
): string {
  if (
    detailProjectId === null ||
    detailProjectId === undefined ||
    !String(detailProjectId).trim()
  ) {
    throw new ColliersIdentityError(
      `Colliers SLP detail for map ProjectId ${mapProjectId} omitted ProjectId`
    );
  }
  const normalized = String(detailProjectId).trim();
  if (normalized !== mapProjectId) {
    throw new ColliersIdentityError(
      `Colliers grouped map/detail ProjectId mismatch: ${mapProjectId} != ${normalized}`
    );
  }
  return normalized;
}

export function colliersProjectField(details: any, name: string): string | null {
  const fields = Array.isArray(details?.ProjectFields) ? details.ProjectFields : [];
  const row = fields.find((f: any) => clean(f?.Name)?.toLowerCase() === name.toLowerCase());
  return clean(row?.Value);
}

export function colliersSqftToNumber(value: string | null): number | null {
  const text = clean(value);
  if (!text) return null;
  const match = text.match(/([0-9][0-9,.]*)\s*(?:sq\.?\s*ft\.?|sf)\b/i);
  return match ? Number(match[1].replace(/,/g, "")) : null;
}

// Colliers SalesTracker multifamily "Size" is often the average unit size
// ("931 sq ft" on a 196-unit mid-rise). The rule matches the cre_validate.py
// mf_avg_unit_size check; without a unit count, a small size priced above
// $1,000/SF is also read as per unit.
const COLLIERS_MAX_SF_PER_UNIT = 300;
const COLLIERS_MAX_AVG_UNIT_SF = 3000;
const COLLIERS_MIN_UNITS_FOR_SMALL_SIZE = 10;
const COLLIERS_MIN_PRICE_PER_SF_FOR_UNIT_SIZE = 1000;
export function colliersAverageUnitSf(
  assetType: string | null | undefined,
  sizeSf: number | null,
  units: number | null | undefined,
  salePriceUsd: number | null | undefined
): number | null {
  if (!sizeSf || sizeSf <= 0 || !/multi[\s-]?family|apartment/i.test(assetType ?? "")) return null;
  const small = sizeSf < COLLIERS_MAX_AVG_UNIT_SF;
  if (units && units > 0) {
    return sizeSf / units < COLLIERS_MAX_SF_PER_UNIT || (small && units > COLLIERS_MIN_UNITS_FOR_SMALL_SIZE) ? sizeSf : null;
  }
  return small && (salePriceUsd ?? 0) > sizeSf * COLLIERS_MIN_PRICE_PER_SF_FOR_UNIT_SIZE ? sizeSf : null;
}

export function colliersAcresToNumber(value: string | null): number | null {
  const text = clean(value);
  if (!text) return null;
  const match = text.match(/([0-9][0-9,.]*)\s*(?:acres?|ac)\b/i);
  return match ? Number(match[1].replace(/,/g, "")) : null;
}

export function colliersDetailContacts(detail: any): any[] {
  const contacts = Array.isArray(detail?.ProjectContacts) ? detail.ProjectContacts : [];
  return contacts.map((c: any) => {
    const contact = prune({
      name: clean(c?.Name), title: clean(c?.Title), email: clean(c?.Email), phone: clean(c?.Phone),
      company: clean(c?.Company), license: clean(c?.License),
      avatarUrl: rcmPublicContactUrl(colliersUrl(c?.ProfileImageUrl)),
      profileUrl: rcmPublicContactUrl(colliersUrl(c?.ExpertBioUrl)),
    });
    if (!contact) return null;
    return { ...contact, rcmContactSource: "colliers_sales_tracker",
      sourceDisplayControls: rcmSourceControls(c, RCM_CONTACT_FLAGS),
      ...(c?.ProjectContactId != null ? { colliersProjectContactId: c.ProjectContactId } : {}) };
  }).filter(Boolean);
}

export function colliersDetailImages(detail: any, fallback: string[]): string[] {
  const candidates: Array<string | null> = [...fallback];
  for (const img of detail?.GalleryImages ?? []) candidates.push(colliersUrl(img?.ImageUrl));
  return dedupeStrings(candidates).filter((url) => /\.(?:jpe?g|png|webp|gif)(?:[?#].*)?$/i.test(url));
}

// Promote the stranded Colliers SalesTracker brochure / agreement urls into
// classified DocItems. brochureUrl is the public marketing brochure; agreementUrl
// is the gated CA/dataroom link. Both were previously kept only in raw metadata
// (colliersSalesTrackerDetail). harvestDetail classifies each by url/keyword and
// dedups. Never throws.
export function colliersStrandedDocs(detail: any): DocItem[] {
  const out: DocItem[] = [];
  const brochureUrl = colliersUrl(detail?.ProjectHeader?.BrochureUrl);
  const agreementUrl = colliersUrl(detail?.ProjectHeader?.AgreementButton?.buttonUrl);
  if (brochureUrl) out.push({ url: brochureUrl, title: titleFromFilename(brochureUrl), docType: "brochure" });
  // The agreement button fronts the offering memorandum / CA dataroom; classify
  // as 'om' so it is not mistaken for a plain brochure.
  if (agreementUrl) out.push({ url: agreementUrl, title: titleFromFilename(agreementUrl), docType: "om" });
  return out;
}

// Promote any stranded Colliers video / virtual-tour urls (VideoUrl / TourUrl /
// Matterport fields under ProjectHeader or SimpleLandingPageValues) for
// harvestDetail. Emitted as BARE STRINGS so the harvester classifies provider +
// mediaType (vimeo/youtube -> video, matterport/kuula/360 -> tour) and derives an
// embedUrl, falling back to media 'other' for unrecognized hosts. Best-effort:
// the SalesTracker payload only sometimes carries these. Never throws.
export function colliersStrandedMedia(detail: any): string[] {
  const candidates = [
    detail?.ProjectHeader?.VideoUrl,
    detail?.ProjectHeader?.VirtualTourUrl,
    detail?.SimpleLandingPageValues?.VideoUrl,
    detail?.SimpleLandingPageValues?.VirtualTourUrl,
    detail?.SimpleLandingPageValues?.MatterportUrl,
  ];
  const out: string[] = [];
  for (const raw of candidates) {
    const url = colliersUrl(raw);
    if (url) out.push(url);
  }
  return out;
}

// Lift stranded structured fields the Colliers SLP detail exposes (mostly via
// ProjectFields name/value pairs) onto the existing listing keys to_row maps.
// Cap rate is returned as a percent display (e.g. "6.5%"); norm_cap_rate in the
// ingestor converts it. Only clearly-present values are lifted (prune drops the
// rest). Never throws.
export function colliersStrandedStructured(detail: any, details: any): Record<string, any> {
  const capText = colliersProjectField(details, "Cap Rate") ?? colliersProjectField(details, "Caprate");
  const capMatch = clean(capText)?.match(/([0-9]+(?:\.[0-9]+)?)/);
  const occText = colliersProjectField(details, "Occupancy");
  const occMatch = clean(occText)?.match(/([0-9]+(?:\.[0-9]+)?)/);
  const units = colliersProjectField(details, "Units") ?? colliersProjectField(details, "Number of Units");
  const unitsMatch = clean(units)?.match(/([0-9][0-9,]*)/);
  const zoning = colliersProjectField(details, "Zoning");
  return prune({
    capRatePct: capMatch ? Number(capMatch[1]) : undefined,
    occupancyRate: occMatch ? Number(occMatch[1]) : undefined,
    units: unitsMatch ? Number(unitsMatch[1].replace(/,/g, "")) : undefined,
    zoning: clean(zoning),
    submarket: clean(colliersProjectField(details, "Submarket")) ?? clean(detail?.ProjectSummary?.Submarket),
  }) ?? {};
}

export function colliersInventoryOnlyCard(
  card: ColliersCard,
  reason: "card_not_linked" | "detail_request_failed" | "detail_not_attempted"
): any {
  return prune({
    ...card,
    canonicalUrl: undefined,
    statusBadge: card.status ?? undefined,
    preserveChildCollections: true,
    provisionalIdentity: {
      reason: "provider_card_not_canonicalized",
      historyContinuity: "not_guaranteed",
      detailPvObserved: Boolean(card.detailPv),
    },
    inventoryOnly: {
      reason,
      indexUrl: COLLIERS_SOURCE_URL,
    },
    detailUnavailable: {
      reason,
      publicCardObserved: true,
      publicPageObserved: reason !== "detail_not_attempted" && Boolean(card.detailPv),
    },
    colliersSalesTrackerDetail: {
      skipped: reason,
    },
  });
}

export async function enrichColliersCard(card: ColliersCard, options: {
  fetchDetail?: typeof colliersGetJson;
  stopOnAccessFailure?: boolean;
  skipBrokerRefs?: boolean;
} = {}): Promise<any> {
  if (!card.detailPv) {
    return colliersInventoryOnlyCard(card, "card_not_linked");
  }
  try {
    let retryAfter: string | null = null;
    const detail = await (options.fetchDetail ?? colliersGetJson)(colliersSlpInitUrl(card.detailPv), undefined, undefined,
      value => { retryAfter = value; });
    if (options.stopOnAccessFailure && retryAfter !== null) throw new ColliersAccessStop("Colliers detail requested Retry-After");
    const summary = detail?.ProjectSummary ?? {};
    const address = summary?.Address ?? {};
    const details = detail?.ProjectDetails ?? {};
    const contactsDetailed = colliersDetailContacts(detail);
    const brokerIds = options.skipBrokerRefs ? [] : contactsDetailed
      .map((c) => {
        const company = clean(c.company);
        return company ? brokerRef({ name: clean(c.name), email: clean(c.email), phone: clean(c.phone),
          avatarUrl: clean(c.avatarUrl), company }) : null;
      })
      .filter((brokerId: number | null): brokerId is number => brokerId !== null);
    const description =
      stripHtmlText(detail?.SimpleLandingPageValues?.Description) ??
      stripHtmlText(detail?.SimpleLandingPageValues?.InvestmentHighlights) ??
      clean(detail?.Seo?.MetaDescription);
    const projectId = colliersAssertDetailProjectId(
      card.mapProjectId,
      summary?.AttributeVisibility?.ProjectId ?? summary?.ProjectId ?? null
    );
    const photos = colliersDetailImages(detail, card.photos);
    // Capture-everything harvest. Colliers SalesTracker has no scraped HTML detail
    // (the detail is the SLP Init JSON), so harvest runs over a synthetic empty
    // doc with the stranded native fields promoted via ctx.extra*: brochure +
    // agreement urls (classified docs), any video/tour fields, and the gallery
    // images. harvestDetail classifies + dedups by url.
    const harvested = harvestDetail({} as ScrapedDoc, {
      baseUrl: card.detailUrl ?? COLLIERS_SOURCE_URL,
      extraDocs: colliersStrandedDocs(detail),
      extraMedia: colliersStrandedMedia(detail),
      extraImages: photos,
    });
    const lifted = colliersStrandedStructured(detail, details);
    // Canonical URL: live url from the detail summary or card.
    const canonicalUrl =
      clean(summary?.SiteUrl) ??
      clean(summary?.CanonicalUrl) ??
      card.detailUrl;
    // Status badge: prefer detail summary.Status, then card status.
    const resolvedStatus = clean(summary?.Status) ?? colliersProjectField(details, "Status") ?? card.status;
    // Lease rate (low yield on SalesTracker: investment-sale focus).
    const leaseRateText: string | null = colliersProjectField(details, "Lease Rate") ?? null;
    const lr = parseLeaseRate(leaseRateText);
    // projectType into extraFacts (long-tail; no discrete column).
    const projectType = clean(details?.ProjectType?.Value);
    const assetType = clean(details?.AssetType?.Value) ?? card.assetType;
    const salePriceUsd =
      parseMoney(clean(summary?.AskingPrice)) ?? parseMoney(colliersProjectField(details, "Asking Price")) ?? card.salePriceUsd;
    const sizeField = colliersProjectField(details, "Size");
    const avgUnitSf = colliersAverageUnitSf(assetType, colliersSqftToNumber(sizeField ?? card.sizeText), lifted.units, salePriceUsd);
    const extraFacts = {
      ...(projectType ? { project_type: projectType } : {}),
      ...(avgUnitSf !== null ? { avg_unit_sf: avgUnitSf } : {}),
      rcm_source_contacts: contactsDetailed,
    };
    return prune({
      ...card,
      id: projectId,
      name: clean(summary?.ProjectName) ?? card.name,
      description,
      assetType,
      status: resolvedStatus,
      street: clean(address?.Street),
      city: clean(address?.City) ?? card.city,
      state: clean(address?.State) ?? card.state,
      postalCode: clean(address?.Zip),
      country: clean(address?.CountryCode) ?? card.country,
      latitude: num(Number(address?.Latitude)) ?? card.latitude,
      longitude: num(Number(address?.Longitude)) ?? card.longitude,
      salePriceUsd,
      salePriceText: clean(summary?.AskingPrice) ?? colliersProjectField(details, "Asking Price") ?? card.salePriceText,
      // An average unit size is not building area: neither the number nor the
      // text (which the ingest would re-parse) may reach size_sf. The value
      // rides extra_facts.avg_unit_sf; the raw label stays in the detail block.
      sizeText: avgUnitSf !== null ? null : sizeField ?? card.sizeText,
      buildingSizeSqft: avgUnitSf !== null ? null : colliersSqftToNumber(sizeField),
      lotSizeAcres: colliersAcresToNumber(colliersProjectField(details, "Parcel")),
      yearBuilt: num(Number(colliersProjectField(details, "Year Built"))),
      ...lifted,
      // Phase-2 scalar fields.
      canonicalUrl: canonicalUrl ?? null,
      statusBadge: resolvedStatus ?? null,
      leaseRateType: lr.type ?? null,
      leaseRateMin: lr.min ?? null,
      leaseRateMax: lr.max ?? null,
      extraFacts,
      brokerIds: brokerIds.length ? brokerIds : card.brokerIds,
      contactsDetailed: contactsDetailed.length ? contactsDetailed : card.contactsDetailed,
      // brochure + agreement now ride the classified `documents` channel.
      brochures: [],
      documents: harvested.documents,
      media: harvested.media,
      links: harvested.links,
      photos: dedupeStrings([...photos, ...harvested.images]),
      colliersSalesTrackerDetail: {
        projectId,
        projectType,
        assetType: clean(details?.AssetType?.Value),
        sizeField,
        pageTitle: clean(detail?.Seo?.PageTitle),
        photoCount: Array.isArray(detail?.GalleryImages) ? detail.GalleryImages.length : 0,
        contactCount: contactsDetailed.length,
        sourceDisplayControls: rcmSourceControls(summary?.AttributeVisibility, ["ShowValue"]),
        brochureUrl: colliersUrl(detail?.ProjectHeader?.BrochureUrl),
        agreementUrl: colliersUrl(detail?.ProjectHeader?.AgreementButton?.buttonUrl),
        brochureAndAgreementNote:
          "Brochure + agreement urls are now promoted into the classified documents channel (brochure / om); the collector stores the source urls only and does not download or parse them.",
      },
    });
  } catch (err) {
    if (err instanceof ColliersIdentityError || err instanceof ColliersAccessStop ||
        (options.stopOnAccessFailure && err instanceof NativeEvidenceTerminalError)) throw err;
    if (options.stopOnAccessFailure && err instanceof ColliersHttpError &&
        ([401, 403, 429].includes(err.status) || err.retryAfter !== null)) {
      throw new ColliersAccessStop("Colliers detail access refused or delayed");
    }
    if (options.stopOnAccessFailure) throw err;
    console.error(
      `  colliers/sale: detail unavailable for ${card.detailUrl ?? card.id}: ${err}`
    );
    return colliersInventoryOnlyCard(card, "detail_request_failed");
  }
}

const COLLIERS_RECOVERY_MAX_PAIRS = 20;

export async function srcColliers(tx: Tx, max: number, monitor: boolean,
  onPage?: (page: number, start: number, cards: any[], pageSize?: number) => void,
  onResponses?: (page: number, start: number, attempt: number, responses: { list?: string; map?: string }, pageSize?: number) => void,
  onHole?: (page: number, start: number, pageSize: number, reason: "provider_failure" | "budget_exhausted" | "recovery_stopped" | "requested_limit" | "page_cap") => void,
  captureWritable?: () => boolean): Promise<SourceResult> {
  if (tx === "lease") {
    return {
      company: "Colliers",
      sourceUrl: COLLIERS_SOURCE_URL,
      method: "skipped",
      totalAvailable: 0,
      listings: [],
      note:
        "Colliers SalesTracker is investment-sale oriented. The main Colliers lease search remains blocked behind the Coveo POST path; no lease GET feed has been proven.",
    };
  }
  if (monitor) {
    // Monitor mode is NOT supported for colliers (SalesTracker): the persisted
    // external id is the SLP-detail ProjectId, which is unavailable on many public
    // cards and requires one detail request per linked card. Emitting provisional
    // visible-card keys would orphan against persisted colliers:<ProjectId> rows
    // and corrupt the change ledger, so colliers stays on the full-sweep cadence
    // and emits no monitor rows
    // (same exclusion as jll/cbre-dealflow). NOTE: colliers-main (main: ids) keys on
    // a stable sitemap id in both modes and remains monitor-enabled; this exclusion
    // is for the SalesTracker `colliers` source only.
    return {
      company: "Colliers",
      sourceUrl: COLLIERS_SOURCE_URL,
      method: "Monitor mode unsupported (detail-derived ProjectId); full-sweep cadence only",
      totalAvailable: null,
      listings: [],
      note: "Monitor mode emits no rows for colliers (SalesTracker): its canonical external id is verified by the SLP detail pass, which monitor mode intentionally skips. Refresh this source via the full (non-monitor) collection path. colliers-main is unaffected.",
    };
  }
  const home = await colliersGetText(COLLIERS_SOURCE_URL);
  const engineKey = extractColliersEngineKey(home);
  const want = Math.min(max, Number.MAX_SAFE_INTEGER);
  const selected: ColliersCard[] = [];
  let total: number | null = null;
  let totalAvail: number | null = null;
  let providerRowsScanned = 0;
  let mapRowsScanned = 0;
  let parseOmissions = 0;
  let exhausted = false;
  const providerProjectIds = new Set<string>();
  let start = 1;
  for (let page = 1; page <= PAGE_CAP && selected.length < want; page++) {
    const pageSize = Math.min(COLLIERS_PAGE_SIZE, want - selected.length);
    let listData: any, mapData: any;
    try {
      [listData, mapData] = await colliersFetchPagePair(
        colliersListUrl(engineKey, start, pageSize),
        colliersMapUrl(engineKey, start, pageSize),
        (attempt, responses) => onResponses?.(page, start, attempt, responses, pageSize),
      );
    } catch (original) {
      if (!(original instanceof ColliersPageFailure) || !onPage || !onResponses || !onHole || !captureWritable?.() ||
          !Number.isSafeInteger(total) || total === null || total < start) throw original;
      const knownTotal = total;
      const rangeSize = Math.min(pageSize, knownTotal - start + 1);
      const recordPage = onPage;
      const isCaptureWritable = captureWritable;
      const retainValidatedPair = (pageNo: number, offset: number, size: number, list: any, map: any, tail: boolean): void => {
        const stage = tail ? "tail" : "recovery";
        if (Number(list.total) !== total || (tail && map.total != null && Number(map.total) !== total)) throw new Error(`Colliers ${stage} total changed`);
        if (totalAvail !== null && (Number(list.totalAvail) !== totalAvail || (tail && map.totalAvail != null && Number(map.totalAvail) !== totalAvail))) throw new Error(`Colliers ${stage} totalAvail changed`);
        const html = String(list.html ?? "");
        const count = cheerio.load(html)("li.item").length;
        const groups = groupColliersMapLocations(Array.isArray(map?.projectLocations) ? map.projectLocations : []);
        if (groups.length !== count || colliersAssertPageCount(list.numProjects, count, offset) !== size) throw new Error(`Colliers ${stage} page parity failed`);
        if (groups.some(group => providerProjectIds.has(group.projectId))) throw new Error(`Colliers ${stage} repeated map ProjectId`);
        const cards = parseColliersCards(html, groups, offset);
        if (cards.length !== count) throw new Error(`Colliers ${stage} parsed-card parity failed`);
        for (const group of groups) providerProjectIds.add(group.projectId);
        recordPage(pageNo, offset, cards.map(card => colliersInventoryOnlyCard(card, "detail_not_attempted")), size);
        if (!isCaptureWritable()) throw new Error(`Colliers ${stage} capture failed`);
      };
      const pending: Array<{ start: number; size: number }> = [];
      if (rangeSize > 1) {
        const left = Math.floor(rangeSize / 2);
        pending.push({ start, size: left }, { start: start + left, size: rangeSize - left });
      }
      let requests = 0;
      let providerHole = false;
      let budgetExhausted = false;
      let tailAccounted = false;
      let requestedRemainderAccounted = false;
      const recordRequestedRemainder = (): void => {
        const lastRequested = Math.min(knownTotal, want);
        if (lastRequested < knownTotal) {
          onHole(page + 1, lastRequested + 1, knownTotal - lastRequested, "requested_limit");
          if (!captureWritable()) throw new Error("Colliers tail capture failed");
        }
        requestedRemainderAccounted = true;
      };
      // Evidence only: this attempt remains failed and never enters detail or ingest.
      try {
        if (rangeSize === 1) {
          onHole(page, start, 1, "provider_failure");
          providerHole = true;
          if (!captureWritable()) throw new Error("Colliers recovery capture failed");
        }
        while (pending.length) {
          const part = pending.shift()!;
            if (requests >= COLLIERS_RECOVERY_MAX_PAIRS) { budgetExhausted = true; onHole(page, part.start, part.size, "budget_exhausted"); if (!captureWritable()) throw new Error("Colliers recovery capture failed"); continue; }
            requests++;
            await new Promise<void>(resolve => setTimeout(resolve, COLLIERS_PAGE_PAIR_RETRY_DELAY_MS));
            try {
              const [list, map] = await colliersFetchPagePair(
                colliersListUrl(engineKey, part.start, part.size), colliersMapUrl(engineKey, part.start, part.size),
                (attempt, responses) => { onResponses(page, part.start, attempt, responses, part.size); if (!captureWritable()) throw new Error("Colliers recovery capture failed"); }, 1);
              retainValidatedPair(page, part.start, part.size, list, map, false);
            } catch (error) {
              if (!(error instanceof ColliersPageFailure)) {
                if (captureWritable()) {
                  onHole(page, part.start, part.size, "recovery_stopped");
                  for (const remaining of pending) {
                    if (!captureWritable()) break;
                    onHole(page, remaining.start, remaining.size, "recovery_stopped");
                  }
                }
                throw error;
              }
              if (part.size === 1) { providerHole = true; onHole(page, part.start, part.size, "provider_failure"); if (!captureWritable()) throw new Error("Colliers recovery capture failed"); }
              else {
                const left = Math.floor(part.size / 2);
                pending.push({ start: part.start, size: left }, { start: part.start + left, size: part.size - left });
              }
            }
        }
        // A settled provider hole does not make the remaining pages worthless.
        // Keep the original source failure; only retain later, independently
        // validated page pairs in the private attempt spool.
        tailAccounted = true;
        if (providerHole && !budgetExhausted) {
          const last = Math.min(total, want);
          let tailStart = start + rangeSize;
          let tailPage = page + 1;
          while (tailStart <= last && tailPage <= PAGE_CAP) {
            const tailSize = Math.min(COLLIERS_PAGE_SIZE, last - tailStart + 1);
            try {
              await new Promise<void>(resolve => setTimeout(resolve, COLLIERS_PAGE_PAIR_RETRY_DELAY_MS));
              const [list, map] = await colliersFetchPagePair(
                colliersListUrl(engineKey, tailStart, tailSize), colliersMapUrl(engineKey, tailStart, tailSize),
                (attempt, responses) => { onResponses(tailPage, tailStart, attempt, responses, tailSize); if (!captureWritable()) throw new Error("Colliers tail capture failed"); }, 1);
              retainValidatedPair(tailPage, tailStart, tailSize, list, map, true);
            } catch (error) {
              if (captureWritable()) {
                if (error instanceof ColliersPageFailure) {
                  onHole(tailPage, tailStart, tailSize, "provider_failure");
                  tailStart += tailSize;
                }
                if (tailStart <= last && captureWritable()) onHole(tailPage, tailStart, last - tailStart + 1, "recovery_stopped");
              }
              throw error;
            }
            tailStart += tailSize;
            tailPage++;
          }
          if (tailStart <= last) {
            onHole(tailPage, tailStart, last - tailStart + 1, "page_cap");
            if (!captureWritable()) throw new Error("Colliers tail capture failed");
          }
        } else {
          const tailStart = start + rangeSize;
          const last = Math.min(total, want);
          if (tailStart <= last) {
            onHole(page + 1, tailStart, last - tailStart + 1, "recovery_stopped");
            if (!captureWritable()) throw new Error("Colliers tail capture failed");
          }
        }
        recordRequestedRemainder();
      } catch (error) {
        const tailStart = start + rangeSize;
        const last = Math.min(total, want);
        let accountingFailed = false;
        try {
          if (!tailAccounted && tailStart <= last && captureWritable()) {
            onHole(page + 1, tailStart, last - tailStart + 1, "recovery_stopped");
          }
          if (!requestedRemainderAccounted && captureWritable()) recordRequestedRemainder();
        } catch { accountingFailed = true; }
        const capture = captureWritable() && !accountingFailed ? "" : "; recovery capture failed and evidence is unverified";
        throw new Error(`${original.message}; recovery stopped: ${error instanceof Error ? error.message : "unknown error"}${capture}`);
      }
      throw original;
    }
    const reportedTotal = Number.isFinite(Number(listData.total))
      ? Number(listData.total)
      : null;
    if (total !== null && reportedTotal !== null && reportedTotal !== total) {
      throw new Error(
        `Colliers total changed during pagination: ${total} -> ${reportedTotal}`
      );
    }
    total = total ?? reportedTotal;
    totalAvail =
      totalAvail ?? (Number.isFinite(Number(listData.totalAvail)) ? Number(listData.totalAvail) : null);
    const pageHtml = String(listData.html ?? "");
    const providerCardCount = cheerio.load(pageHtml)("li.item").length;
    const mapRows = Array.isArray(mapData?.projectLocations)
      ? mapData.projectLocations
      : [];
    const mapGroups = groupColliersMapLocations(mapRows);
    if (mapGroups.length !== providerCardCount) {
      throw new Error(
        `Colliers card/map-group parity failed at start ${start}: ${providerCardCount} != ${mapGroups.length}`
      );
    }
    for (const group of mapGroups) {
      if (providerProjectIds.has(group.projectId)) {
        throw new Error(
          `Colliers repeated map ProjectId ${group.projectId} across pages`
        );
      }
      providerProjectIds.add(group.projectId);
    }
    const cards = parseColliersCards(pageHtml, mapGroups, start);
    providerRowsScanned += providerCardCount;
    mapRowsScanned += mapRows.length;
    parseOmissions += Math.max(0, providerCardCount - cards.length);
    selected.push(...cards.slice(0, Math.max(0, want - selected.length)));
    console.error(
      `  colliers/sale: page ${page} start ${start}, ${cards.length}/${providerCardCount} parsed cards (${selected.length}/${total ?? "?"})`
    );
    onPage?.(page, start, cards.map(card => colliersInventoryOnlyCard(card, "detail_not_attempted")));
    const numProjects = colliersAssertPageCount(
      listData.numProjects,
      providerCardCount,
      start
    );
    if (numProjects === 0) {
      exhausted = true;
      break;
    }
    start += numProjects;
    if (total !== null && start > total) {
      exhausted = true;
      break;
    }
  }
  if (!selected.length) throw new Error("no public Colliers SalesTracker cards found");
  const stoppedAtRequestedLimit =
    selected.length >= want && Number.isFinite(max);
  const providerTotalMismatch =
    !Number.isFinite(max) &&
    total !== null &&
    providerRowsScanned !== total;
  let truncated =
    parseOmissions > 0 ||
    providerTotalMismatch ||
    (!exhausted && !stoppedAtRequestedLimit);
  let done = 0;
  // Full path only (monitor mode returned [] above): SLP Init detail-enrich every
  // card so the persisted external id is the detail ProjectId.
  const enriched = await pmap(selected, COLLIERS_DETAIL_CONCURRENCY, async (card) => {
    const listing = await enrichColliersCard(card);
    done++;
    if (done % 25 === 0 || done === selected.length) {
      console.error(`  colliers/sale: detail enriched ${done}/${selected.length}`);
    }
    return listing;
  });
  const inventoryObservedAt = observationNow();
  const listings = enriched.map((listing) => {
    if (listing?.inventoryOnly) {
      // An unlinked or failed SLP card is only provisional source-index
      // evidence. It cannot inherit the canonical detail contract below.
      return { ...listing, inventoryObservedAt };
    }
    return {
      ...listing,
      inventoryObservedAt,
      detailObservedAt: inventoryObservedAt,
      freshnessProvenance: {
        detailScope: "detail_page",
        generationId: refreshGenerationId(),
        method: "colliers_salestracker_slp_detail",
        cacheDisposition: "live",
      },
    };
  });
  const canonicalIds = new Set<string>();
  const duplicateCanonicalIds = new Set<string>();
  for (const listing of listings) {
    if (listing?.inventoryOnly) continue;
    const id = clean(listing?.id);
    if (!id) {
      throw new Error("Colliers detail enrichment produced a canonical row without an id");
    }
    if (canonicalIds.has(id)) duplicateCanonicalIds.add(id);
    canonicalIds.add(id);
  }
  if (duplicateCanonicalIds.size) {
    throw new Error(
      `Colliers detail enrichment produced ${duplicateCanonicalIds.size} duplicate canonical ProjectId(s)`
    );
  }
  const inventoryOnly = listings.filter((listing) => listing?.inventoryOnly).length;
  const detailRequestFailures = listings.filter(
    (listing) =>
      listing?.inventoryOnly?.reason === "detail_request_failed"
  ).length;
  if (detailRequestFailures > 0) truncated = true;
  const detailUnavailable = listings.filter(
    (listing) => listing?.detailUnavailable
  ).length;
  return {
    company: "Colliers",
    sourceUrl: COLLIERS_SOURCE_URL,
    method:
      "Public Colliers SalesTracker RCM ListingEngine GET list/map endpoints with ordered ProjectId grouping plus anonymous SLP Init detail enrichment",
    totalAvailable: total,
    listings,
    truncated,
    note:
      `SalesTracker public list totalAvail was ${totalAvail ?? "unknown"} and filtered total was ${total ?? "unknown"}. ` +
      `Scanned ${providerRowsScanned} provider card(s), retained ${selected.length}, and omitted ${parseOmissions} unparseable card(s). ` +
      `Grouped ${mapRowsScanned} map pin row(s) into ${providerProjectIds.size} unique ordered ProjectId(s). ` +
      `${canonicalIds.size} card(s) resolved to unique detail ProjectIds; ${inventoryOnly} remained inventory-only evidence; ${detailUnavailable} lacked canonical detail, including ${detailRequestFailures} failed detail request(s). ` +
      "Main colliers.com Coveo sale/lease coverage remains blocked; no POST, agreement, or gated document path is used.",
  };
}
