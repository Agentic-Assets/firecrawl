// sources/colliers.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY, PAGE_CAP } from "../lib/config.js";
import { harvestDetail } from "../lib/harvest.js";
import { decodeHtmlEntities, dedupeStrings, stripHtmlText, titleFromFilename } from "../lib/html.js";
import { parseJsonBody } from "../lib/scrape.js";
import { DocItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { parseLeaseRate } from "../lib/parse.js";
import { clean, moneyToNumber, num, pmap, prune } from "../lib/util.js";


// --- Colliers: public SalesTracker RCM ListingEngine GET path ---

export const COLLIERS_SALESTRACKER_BASE = "https://sales.colliers.com";
export const COLLIERS_RCM_BASE = "https://my.rcm1.com";
export const COLLIERS_SOURCE_URL = `${COLLIERS_SALESTRACKER_BASE}/`;
export const COLLIERS_FALLBACK_ENGINE_KEY = "BX0EQVWsJMGzGR6ZiWBDEnJAH-tErDnvHaBoKDFAOy4";
export const COLLIERS_PAGE_SIZE = 100;
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

export function colliersHeaders(accept = "application/json, text/javascript, */*; q=0.01"): Record<string, string> {
  return {
    accept,
    origin: COLLIERS_SALESTRACKER_BASE,
    referer: COLLIERS_SOURCE_URL,
    "user-agent": "Mozilla/5.0 CRE collector",
    "x-requested-with": "XMLHttpRequest",
  };
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

export async function colliersGetText(url: string): Promise<string> {
  const res = await fetch(url, {
    headers: colliersHeaders("text/html,application/json,*/*"),
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) throw new Error(`Colliers GET ${url} HTTP ${res.status}`);
  return res.text();
}

export async function colliersGetJson(url: string): Promise<any> {
  const text = await colliersGetText(url);
  const parsed = parseJsonBody(text);
  if (parsed === null) throw new Error(`Colliers GET ${url} returned non-JSON`);
  if (parsed?.success === false) throw new Error(`Colliers GET ${url} returned success=false`);
  return parsed;
}

export function extractColliersEngineKey(html: string): string {
  return (
    html.match(/new\s+ListingEngine\s*\(\s*\{[\s\S]*?key\s*:\s*["']([^"']+)/i)?.[1] ??
    html.match(/pv=([A-Za-z0-9_-]{30,})/)?.[1] ??
    COLLIERS_FALLBACK_ENGINE_KEY
  );
}

export function colliersListUrl(engineKey: string, start: number, pageSize: number): string {
  return `${COLLIERS_RCM_BASE}/api/AjaxEngine/GetListingsHtml?pv=${encodeURIComponent(engineKey)}&Start=${start}&PageSize=${pageSize}`;
}

export function colliersMapUrl(engineKey: string, start: number, pageSize: number): string {
  return `${COLLIERS_RCM_BASE}/api/AjaxEngine/GetMapData?pv=${encodeURIComponent(engineKey)}&Start=${start}&PageSize=${pageSize}`;
}

export function colliersSlpInitUrl(pv: string): string {
  return `${COLLIERS_RCM_BASE}/api/handler/slp/Init?pv=${encodeURIComponent(pv)}`;
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
  const groups = new Map<string, ColliersMapGroup>();
  for (const [index, row] of rows.entries()) {
    const rawProjectId = row?.ProjectId ?? row?.projectId;
    if (
      rawProjectId === null ||
      rawProjectId === undefined ||
      !String(rawProjectId).trim()
    ) {
      throw new Error(`Colliers map row ${index} is missing ProjectId`);
    }
    const projectId = String(rawProjectId).trim();
    let group = groups.get(projectId);
    if (!group) {
      group = { projectId, pins: [] };
      groups.set(projectId, group);
    }
    const latitude = num(Number(row?.Latitude ?? row?.latitude));
    const longitude = num(Number(row?.Longitude ?? row?.longitude));
    if (
      latitude !== null &&
      longitude !== null &&
      latitude >= -90 &&
      latitude <= 90 &&
      longitude >= -180 &&
      longitude <= 180 &&
      !group.pins.some(
        (pin) => pin.latitude === latitude && pin.longitude === longitude
      )
    ) {
      group.pins.push({ latitude, longitude });
    }
  }
  return [...groups.values()];
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
    if (!name && !email && !phone) return;
    contacts.push(
      prune({
        name,
        email,
        phone,
        company: "Colliers",
      }) ?? {}
    );
  });
  return contacts;
}

export function parseColliersCards(
  html: string,
  mapGroups: ColliersMapGroup[],
  start: number
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
    const brokerIds = contactsDetailed
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          email: clean(c.email),
          phone: clean(c.phone),
          company: "Colliers",
        })
      )
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
      salePriceUsd: moneyToNumber(salePriceText),
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

export function colliersAcresToNumber(value: string | null): number | null {
  const text = clean(value);
  if (!text) return null;
  const match = text.match(/([0-9][0-9,.]*)\s*(?:acres?|ac)\b/i);
  return match ? Number(match[1].replace(/,/g, "")) : null;
}

export function colliersDetailContacts(detail: any): any[] {
  const contacts = Array.isArray(detail?.ProjectContacts) ? detail.ProjectContacts : [];
  return contacts
    .map((c: any) =>
      prune({
        name: clean(c?.Name),
        title: clean(c?.Title),
        email: c?.ShowEmail === false ? null : clean(c?.Email),
        phone: clean(c?.Phone),
        company: clean(c?.Company) ?? "Colliers",
        avatarUrl: colliersUrl(c?.ProfileImageUrl),
        profileUrl: c?.ShowExpertBio === true ? colliersUrl(c?.ExpertBioUrl) : null,
        license: clean(c?.License),
        colliersProjectContactId: c?.ProjectContactId ?? null,
      })
    )
    .filter(Boolean);
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
  reason: "card_not_linked" | "detail_request_failed"
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
      publicPageObserved: Boolean(card.detailPv),
    },
    colliersSalesTrackerDetail: {
      skipped: reason,
    },
  });
}

export async function enrichColliersCard(card: ColliersCard): Promise<any> {
  if (!card.detailPv) {
    return colliersInventoryOnlyCard(card, "card_not_linked");
  }
  try {
    const detail = await colliersGetJson(colliersSlpInitUrl(card.detailPv));
    const summary = detail?.ProjectSummary ?? {};
    const address = summary?.Address ?? {};
    const details = detail?.ProjectDetails ?? {};
    const contactsDetailed = colliersDetailContacts(detail);
    const brokerIds = contactsDetailed
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          email: clean(c.email),
          phone: clean(c.phone),
          avatarUrl: clean(c.avatarUrl),
          company: "Colliers",
        })
      )
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
    const extraFacts: Record<string, string> | undefined = projectType ? { project_type: projectType } : undefined;
    return prune({
      ...card,
      id: projectId,
      name: clean(summary?.ProjectName) ?? card.name,
      description,
      assetType: clean(details?.AssetType?.Value) ?? card.assetType,
      status: resolvedStatus,
      street: clean(address?.Street),
      city: clean(address?.City) ?? card.city,
      state: clean(address?.State) ?? card.state,
      postalCode: clean(address?.Zip),
      country: clean(address?.CountryCode) ?? card.country,
      latitude: num(Number(address?.Latitude)) ?? card.latitude,
      longitude: num(Number(address?.Longitude)) ?? card.longitude,
      salePriceUsd: moneyToNumber(clean(summary?.AskingPrice)) ?? moneyToNumber(colliersProjectField(details, "Asking Price")) ?? card.salePriceUsd,
      salePriceText: clean(summary?.AskingPrice) ?? colliersProjectField(details, "Asking Price") ?? card.salePriceText,
      sizeText: colliersProjectField(details, "Size") ?? card.sizeText,
      buildingSizeSqft: colliersSqftToNumber(colliersProjectField(details, "Size")),
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
        pageTitle: clean(detail?.Seo?.PageTitle),
        photoCount: Array.isArray(detail?.GalleryImages) ? detail.GalleryImages.length : 0,
        contactCount: contactsDetailed.length,
        brochureUrl: colliersUrl(detail?.ProjectHeader?.BrochureUrl),
        agreementUrl: colliersUrl(detail?.ProjectHeader?.AgreementButton?.buttonUrl),
        brochureAndAgreementNote:
          "Brochure + agreement urls are now promoted into the classified documents channel (brochure / om); the collector stores the source urls only and does not download or parse them.",
      },
    });
  } catch (err) {
    if (err instanceof ColliersIdentityError) {
      throw err;
    }
    console.error(
      `  colliers/sale: detail unavailable for ${card.detailUrl ?? card.id}: ${err}`
    );
    return colliersInventoryOnlyCard(card, "detail_request_failed");
  }
}

export async function srcColliers(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
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
    const [listData, mapData] = await Promise.all([
      colliersGetJson(colliersListUrl(engineKey, start, pageSize)),
      colliersGetJson(colliersMapUrl(engineKey, start, pageSize)),
    ]);
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
  const listings = await pmap(selected, COLLIERS_DETAIL_CONCURRENCY, async (card) => {
    const listing = await enrichColliersCard(card);
    done++;
    if (done % 25 === 0 || done === selected.length) {
      console.error(`  colliers/sale: detail enriched ${done}/${selected.length}`);
    }
    return listing;
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
