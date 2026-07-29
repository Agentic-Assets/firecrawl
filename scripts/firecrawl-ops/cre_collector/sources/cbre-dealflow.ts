// sources/cbre-dealflow.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { createHash } from "node:crypto";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY, PAGE_CAP } from "../lib/config.js";
import { harvestDetail } from "../lib/harvest.js";
import { dedupeStrings, titleFromFilename } from "../lib/html.js";
import { DocItem, ScrapedDoc, SourceResult, Tx } from "../types.js";
import { clean, num, pmap, prune } from "../lib/util.js";


// --- CBRE Deal Flow: public Real Capital Markets ListingEngine API ---

export const CBRE_DEALFLOW_BASE = "https://www.cbredealflow.com";
export const CBRE_DEALFLOW_SOURCE_URL = `${CBRE_DEALFLOW_BASE}/`;
export const CBRE_DEALFLOW_FALLBACK_ENGINE_KEY = "oi5qxFqUeAwpuWTlIxfX2WDpoZa3NjIo51F63rmSsEI";
export const CBRE_DEALFLOW_PAGE_SIZE = 200;
// The provider renders each 200-card Investment Sale page server-side. A live
// 2026-07-29 response took 74 seconds while still returning a complete 200-card
// page, so the former 30-second deadline rejected healthy inventory. Keep this
// bounded independently from the 30-second per-listing detail deadline.
export const CBRE_DEALFLOW_INVENTORY_TIMEOUT_MS = 120000;
export const CBRE_DEALFLOW_DETAIL_ATTEMPTS = 3;
export const CBRE_DEALFLOW_DETAIL_RETRY_BACKOFF_MS = 1000;
export const CBRE_DEALFLOW_DETAIL_CONCURRENCY = Math.min(CONCURRENCY, 2);
export const CBRE_DEALFLOW_PROJECT_TYPE_BY_TX: Record<Tx, string> = {
  sale: "Investment Sale",
  lease: "Leasing",
};

export type CbreDealflowCard = {
  id: string | null;
  url: string | null;
  urlKind: "detail" | "brochure" | "agreement" | "unlinked";
  listingPv: string | null;
  name: string | null;
  transactionType: string;
  assetType: string | null;
  description: string | null;
  city: string | null;
  state: string | null;
  country: string | null;
  sizeText: string | null;
  status: string | null;
  brokerIds: number[];
  contactsDetailed?: any[];
  brochures?: any[];
  photos: string[];
  cbreDealflowCard: Record<string, any>;
};

export function cbreDealflowHeaders(accept = "application/json, text/javascript, */*; q=0.01"): Record<string, string> {
  return {
    accept,
    origin: CBRE_DEALFLOW_BASE,
    referer: CBRE_DEALFLOW_SOURCE_URL,
    "user-agent": "Mozilla/5.0 CRE collector",
    "x-requested-with": "XMLHttpRequest",
  };
}

export function cbreDealflowUrl(href: string | null | undefined): string | null {
  const h = clean(href ?? null);
  if (!h || /^javascript:/i.test(h) || /^mailto:/i.test(h) || /^tel:/i.test(h)) return null;
  try {
    return new URL(h, CBRE_DEALFLOW_BASE).toString();
  } catch {
    return null;
  }
}

export async function cbreDealflowGetText(
  url: string,
  attempts = 1,
  retryBackoffMs = CBRE_DEALFLOW_DETAIL_RETRY_BACKOFF_MS
): Promise<string> {
  let lastError: unknown = null;
  const boundedAttempts = Number.isFinite(attempts) ? Math.max(1, Math.trunc(attempts)) : 1;
  for (let attempt = 1; attempt <= boundedAttempts; attempt++) {
    try {
      const res = await fetch(url, {
        headers: cbreDealflowHeaders("text/html,application/json,*/*"),
        signal: AbortSignal.timeout(30000),
      });
      if (!res.ok) throw new Error(`CBRE Deal Flow GET ${url} HTTP ${res.status}`);
      return await res.text();
    } catch (error) {
      lastError = error;
      if (attempt < boundedAttempts && retryBackoffMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, retryBackoffMs * attempt));
      }
    }
  }
  throw lastError;
}

export async function cbreDealflowPostJson(path: string, body: URLSearchParams): Promise<any> {
  const url = `${CBRE_DEALFLOW_BASE}${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      ...cbreDealflowHeaders(),
      "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    },
    body,
    signal: AbortSignal.timeout(CBRE_DEALFLOW_INVENTORY_TIMEOUT_MS),
  });
  const text = await res.text();
  let parsed: any = null;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error(`CBRE Deal Flow ${path} returned non-JSON HTTP ${res.status}`);
  }
  if (!res.ok || parsed?.success === false) {
    throw new Error(`CBRE Deal Flow ${path} HTTP ${res.status}`);
  }
  return parsed;
}

export function extractCbreDealflowEngineKey(html: string): string {
  return (
    html.match(/new\s+ListingEngine\s*\(\s*\{[\s\S]*?key\s*:\s*["']([^"']+)/i)?.[1] ??
    html.match(/pv=([A-Za-z0-9_-]{30,})/)?.[1] ??
    CBRE_DEALFLOW_FALLBACK_ENGINE_KEY
  );
}

export function parseCbreDealflowFilters(filters: any): Record<string, any> {
  return prune({
    projectTypes: Array.isArray(filters?.ProjectType) ? filters.ProjectType : undefined,
    countries: Array.isArray(filters?.Country) ? filters.Country : undefined,
    states: Array.isArray(filters?.State) ? filters.State : undefined,
    statuses: Array.isArray(filters?.Status) ? filters.Status : undefined,
    assetTypes: Array.isArray(filters?.AssetType) ? filters.AssetType : undefined,
  }) ?? {};
}

export function parseCbreDealflowLocation(text: string | null): { city: string | null; state: string | null } {
  const normalized = clean((text ?? "").replace(/\u201A/g, ","));
  const match = normalized?.match(/^(.+?),\s*([A-Z]{2})\b/);
  return {
    city: match ? clean(match[1]) : null,
    state: match?.[2] ?? null,
  };
}

export function listingPvFromCbreDealflowUrl(url: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).searchParams.get("pv");
  } catch {
    return null;
  }
}

export function cbreDealflowUnlinkedCardId(fields: {
  name: string | null;
  city: string | null;
  state: string | null;
  assetType: string | null;
}): string | null {
  const name = clean(fields.name);
  if (!name) return null;
  const identity = [name, fields.city, fields.state, fields.assetType]
    .map((value) => clean(value)?.toLowerCase() ?? "")
    .join("|");
  return `card:${createHash("sha256").update(identity).digest("hex").slice(0, 24)}`;
}

export function cbreDealflowNumProjects(value: unknown, start: number): number {
  if (
    value === null ||
    value === undefined ||
    (typeof value === "string" && value.trim() === "") ||
    (typeof value !== "string" && typeof value !== "number")
  ) {
    throw new Error(
      `CBRE Deal Flow returned invalid numProjects=${JSON.stringify(value)} at start ${start}`
    );
  }
  const count = Number(value);
  if (!Number.isInteger(count) || count < 0) {
    throw new Error(
      `CBRE Deal Flow returned invalid numProjects=${JSON.stringify(value)} at start ${start}`
    );
  }
  return count;
}

export function cbreDealflowAssertPageCount(
  value: unknown,
  providerCardCount: number,
  start: number
): number {
  const count = cbreDealflowNumProjects(value, start);
  if (count !== providerCardCount) {
    throw new Error(
      `CBRE Deal Flow numProjects/card parity failed at start ${start}: ${count} != ${providerCardCount}`
    );
  }
  return count;
}

export function cbreDealflowCardContacts($: cheerio.CheerioAPI, card: cheerio.Cheerio<any>): any[] {
  const contacts: any[] = [];
  card.find(".contacts .tab-text").each((_, el) => {
    const row = $(el);
    const name = clean(row.find(".name").first().text()) ?? clean(row.text().match(/[A-Za-z][A-Za-z .'-]+/)?.[0] ?? null);
    const email = clean(row.find('a[href^="mailto:"]').first().attr("href")?.replace(/^mailto:/i, ""));
    const phone =
      clean(row.find('a[href^="tel:"]').first().text()) ??
      clean(row.find('a[href^="tel:"]').first().attr("href")?.replace(/^tel:/i, ""));
    if (!name && !email && !phone) return;
    contacts.push(
      prune({
        name,
        email,
        phone,
        company: "CBRE",
      }) ?? {}
    );
  });
  return contacts;
}

export function parseCbreDealflowCards(html: string, tx: Tx): CbreDealflowCard[] {
  const $ = cheerio.load(html);
  const cards: CbreDealflowCard[] = [];
  $("li.item, ul.gridview > li").each((_, el) => {
    const card = $(el);
    const detailText = clean(card.find(".details").first().text());
    const projectType =
      clean(detailText?.match(/\b(Investment Sale|Leasing)\b/i)?.[1] ?? null) ??
      CBRE_DEALFLOW_PROJECT_TYPE_BY_TX[tx];
    const wanted = CBRE_DEALFLOW_PROJECT_TYPE_BY_TX[tx];
    if (projectType.toLowerCase() !== wanted.toLowerCase()) return;
    const linkedUrl = cbreDealflowUrl(
      card
        .find(
          'a[href*="landing.aspx"], a[href*="modern.aspx"], a[href*="/buyer/brochure"], a[href*="/buyer/agreement"]'
        )
        .first()
        .attr("href")
    );
    const urlKind = linkedUrl
      ? /\/buyer\/brochure/i.test(linkedUrl)
        ? "brochure"
        : /\/buyer\/agreement/i.test(linkedUrl)
          ? "agreement"
          : "detail"
      : "unlinked";
    const location = parseCbreDealflowLocation(card.find(".location .city, .location").first().text());
    const country = clean(card.find(".country").first().text()?.replace(/\|/g, ""));
    const img = cbreDealflowUrl(card.find("img").first().attr("src"));
    const sizeText =
      clean(detailText?.match(/\b(?:Investment Sale|Leasing)\s*\|\s*([^|]+)$/i)?.[1] ?? null) ??
      clean(detailText?.match(/([0-9][0-9,.]*\s*(?:sq ft|sf|units?|acres?|ac)\b)/i)?.[1] ?? null);
    const name = clean(card.find(".headline").first().text()) ?? clean(card.find("a.summary p").attr("title"));
    const assetType = clean(card.find(".asset").first().text()?.replace(/^--$/, ""));
    const listingPv = listingPvFromCbreDealflowUrl(linkedUrl);
    const cardIdentity = cbreDealflowUnlinkedCardId({
      name,
      city: location.city,
      state: location.state,
      assetType,
    });
    const id = listingPv ?? cardIdentity;
    if (!id) return;
    const contactsDetailed = cbreDealflowCardContacts($, card);
    const brokerIds = contactsDetailed
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          email: clean(c.email),
          phone: clean(c.phone),
          company: "CBRE",
        })
      )
      .filter((id: number | null): id is number => id !== null);
    cards.push({
      id,
      url: linkedUrl,
      urlKind,
      listingPv,
      name,
      transactionType: tx === "sale" ? "Investment Sale" : "Lease",
      assetType,
      description: clean(card.find("a.summary p").first().text()),
      city: location.city,
      state: location.state,
      country,
      sizeText,
      status: clean(card.find(".status").first().text()),
      brokerIds,
      contactsDetailed,
      brochures:
        urlKind === "brochure"
          ? [
              {
                name: "Public brochure",
                url: linkedUrl,
              },
            ]
          : [],
      photos: img ? [img] : [],
      cbreDealflowCard: prune({
        listingPv,
        cardIdentity,
        urlKind,
        projectType,
        status: clean(card.find(".status").first().text()),
        contactsText: clean(card.find(".contacts, .contact").text()),
        detailsText: detailText,
      }) ?? {},
    });
  });
  return cards;
}

export function parseCbreDealflowDetailData(html: string): any | null {
  const match = html.match(/var\s+data\s*=\s*(\{[\s\S]*?\})\s*<\/script>/i);
  if (!match) return null;
  try {
    return JSON.parse(match[1]);
  } catch {
    return null;
  }
}

function cbreDealflowComparableName(value: string | null | undefined): string {
  return (clean(value)?.toLowerCase() ?? "").replace(/[^a-z0-9]+/g, "");
}

export function cbreDealflowDetailUnavailableReason(
  html: string,
  expectedName?: string | null
): string | null {
  const $ = cheerio.load(html);
  const body = $("body").clone();
  body.find("script, style").remove();
  const text = clean(body.text())?.toLowerCase() ?? "";
  if (
    text.includes("landing page executive summary has been enabled") &&
    text.includes("landing page has not been setup")
  ) {
    return "landing_not_setup";
  }
  const title = clean($("title").first().text())?.toLowerCase() ?? "";
  const propertyTitle = clean(title.split("|")[0]);
  const pagePropertyName = clean($("#ProjectName").first().text());
  const comparableExpectedName = cbreDealflowComparableName(expectedName);
  if (
    propertyTitle &&
    propertyTitle.length >= 4 &&
    comparableExpectedName &&
    cbreDealflowComparableName(pagePropertyName) === comparableExpectedName &&
    /\|\s*cbre\s*\|\s*powered by lightbox\b/i.test(title) &&
    $("#ProjectNameAndAddress").length === 1 &&
    $("#Content").length === 1 &&
    $(".TabContent").length >= 1 &&
    text.length >= 80
  ) {
    return "public_html_only";
  }
  return null;
}

export function cbreDealflowAssertHtmlOnlyMix(listings: any[]): number {
  const count = listings.filter(
    (listing) => listing?.detailUnavailable?.reason === "public_html_only"
  ).length;
  const limit = Math.max(5, Math.ceil(listings.length * 0.01));
  if (count > limit) {
    throw new Error(
      `CBRE Deal Flow public_html_only anomaly: ${count}/${listings.length} exceeds ${limit}`
    );
  }
  return count;
}

export function cbreDealflowUnavailableCard(
  card: CbreDealflowCard,
  reason: string
): any {
  const projectType = clean(card.cbreDealflowCard?.projectType) ?? undefined;
  return prune({
    ...card,
    statusBadge: clean(card.status) ?? undefined,
    extraFacts: projectType ? { project_type: projectType } : undefined,
    preserveChildCollections: true,
    provisionalIdentity:
      card.urlKind === "unlinked"
        ? {
            reason: "provider_card_has_no_stable_id",
            historyContinuity: "not_guaranteed",
          }
        : undefined,
    inventoryOnly:
      card.urlKind === "unlinked"
        ? {
            reason: "no_provider_id_or_listing_url",
            indexUrl: CBRE_DEALFLOW_SOURCE_URL,
          }
        : undefined,
    detailUnavailable: {
      reason,
      publicCardObserved: true,
      publicPageObserved:
        reason === "landing_not_setup" || reason === "public_html_only",
    },
  });
}

export function cbreDealflowTextFromHtml(html: string | null | undefined): string | null {
  if (!html) return null;
  return clean(cheerio.load(html).text());
}

export function cbreDealflowDescription(data: any, fallback: string | null): string | null {
  const summary = clean(data?.projectfields?.summary);
  if (summary) return summary;
  for (const section of data?.sections ?? []) {
    for (const content of section?.contents ?? []) {
      const text = cbreDealflowTextFromHtml(content?.content) ?? clean(content?.subtitle);
      if (text && text.length > 40) return text.slice(0, 2000);
    }
  }
  return fallback;
}

export function cbreDealflowImageUrls(data: any, cardPhotos: string[]): string[] {
  const candidates: Array<string | null> = [...cardPhotos];
  const pushImage = (img: any) => {
    candidates.push(cbreDealflowUrl(img?.imageUrl));
    candidates.push(cbreDealflowUrl(img?.thumburl));
  };
  for (const photo of data?.photos ?? []) pushImage(photo);
  for (const section of data?.sections ?? []) {
    for (const img of section?.images ?? []) pushImage(img);
  }
  return dedupeStrings(candidates).filter((url) => /\.(?:jpe?g|png|webp|gif)(?:[?#].*)?$/i.test(url));
}

export function cbreDealflowDocumentUrls(data: any): any[] {
  const candidates: string[] = [];
  for (const section of data?.sections ?? []) {
    for (const img of section?.images ?? []) {
      const link = cbreDealflowUrl(img?.link);
      if (link && /\.pdf(?:[?#].*)?$/i.test(link)) candidates.push(link);
    }
    for (const content of section?.contents ?? []) {
      const $ = cheerio.load(content?.content ?? "");
      $("a[href]").each((_, a) => {
        const link = cbreDealflowUrl($(a).attr("href"));
        if (link && /\.pdf(?:[?#].*)?$/i.test(link)) candidates.push(link);
      });
    }
  }
  return dedupeStrings(candidates).map((url) => ({ name: titleFromFilename(url), url }));
}

export function cbreDealflowContacts(data: any): any[] {
  const contacts: any[] = [];
  for (const section of data?.sections ?? []) {
    for (const c of section?.contacts ?? []) {
      const name = clean(c?.Fullname) ?? clean([c?.Firstname, c?.Lastname].filter(Boolean).join(" "));
      if (!name) continue;
      const avatarUrl = c?.ShowProfileImage ? cbreDealflowUrl(c?.ProfileImageUrl) : null;
      const email = c?.ShowEmail === true ? clean(c?.Email) : null;
      const phone = c?.ShowPhone === true ? clean(c?.Phone) : null;
      contacts.push(
        prune({
          name,
          title: c?.ShowTitle === true ? clean(c?.Title) : null,
          email,
          phone,
          company: clean(c?.CompanyName) ?? "CBRE",
          avatarUrl,
          profileUrl: c?.ShowExpertBio === true ? cbreDealflowUrl(c?.ExpertBioUrl) : null,
          cbreContactId: c?.ProjectContactId ?? null,
        }) ?? { name, company: "CBRE" }
      );
    }
  }
  return contacts;
}

// Build the rawHtml surface harvestDetail scans for embedded video iframes /
// virtual-tour links. The detail page itself (the raw GET html) plus every
// section content HTML fragment are concatenated so an iframe inside a section
// body is seen. Pure: no network. Never throws.
export function cbreDealflowHarvestHtml(pageHtml: string, data: any): string {
  const parts: string[] = [typeof pageHtml === "string" ? pageHtml : ""];
  for (const section of data?.sections ?? []) {
    for (const content of section?.contents ?? []) {
      if (typeof content?.content === "string") parts.push(content.content);
    }
  }
  return parts.join("\n");
}

// Lift stranded structured fields the CBRE Deal Flow detail `data` exposes but the
// adapter previously dropped, onto the existing listing keys to_row maps. The
// projectfields block carries optional cap rate / NOI / occupancy / year-built /
// units. Only clearly-present values are lifted (prune drops the rest). Never
// throws.
export function cbreDealflowStrandedStructured(data: any): Record<string, any> {
  const f = data?.projectfields ?? {};
  const pct = (v: any): number | null => {
    const n = num(Number(String(v ?? "").replace(/[%,\s]/g, "")));
    return n;
  };
  return prune({
    capRatePct: pct(f.caprate ?? f.capRate),
    noi: num(Number(String(f.noi ?? "").replace(/[$,\s]/g, ""))),
    occupancyRate: pct(f.occupancy ?? f.occupancyRate),
    yearBuilt: num(Number(f.yearbuilt ?? f.yearBuilt)),
    units: num(Number(String(f.units ?? f.numberofunits ?? "").replace(/[,\s]/g, ""))),
    zoning: clean(f.zoning),
  }) ?? {};
}

// Extract the WS1 additive scalar fields from a stored CBRE Deal Flow raw_data blob.
// The blob is the JSON the adapter emits after enrich; this function re-derives
// the NEW camelCase fields so tests can assert the parse without a network call.
// Pure: no side effects, never throws.
export function cbreDealflowNewFieldsFromRawData(raw: any): {
  statusBadge: string | null;
  contactsDetailedWithPhoneAndTitle: Array<{ name: string | null; phone: string | null; title: string | null }>;
  extraFacts: Record<string, any> | null;
} {
  // statusBadge: from cbreDealflowDetail.status (detail path) or card-level status
  const statusBadge =
    clean(raw?.cbreDealflowDetail?.status) ??
    clean(raw?.status) ??
    null;
  // contactsDetailed: already in raw_data as emitted by the adapter; extract phone+title
  const contactsDetailedWithPhoneAndTitle = (Array.isArray(raw?.contactsDetailed) ? raw.contactsDetailed : []).map(
    (c: any) => ({
      name: clean(c?.name) ?? null,
      phone: clean(c?.phone) ?? null,
      title: clean(c?.title) ?? null,
    })
  );
  // extraFacts: project_type from cbreDealflowDetail.projectType
  const projectType = clean(raw?.cbreDealflowDetail?.projectType) ?? null;
  const extraFacts = projectType ? { project_type: projectType } : null;
  return { statusBadge, contactsDetailedWithPhoneAndTitle, extraFacts };
}

export async function enrichCbreDealflowCard(card: CbreDealflowCard, tx: Tx): Promise<any> {
  if (card.urlKind === "brochure") {
    return cbreDealflowUnavailableCard(card, "public_brochure_only");
  }
  if (card.urlKind === "agreement") {
    return cbreDealflowUnavailableCard(card, "gated_agreement");
  }
  if (card.urlKind === "unlinked") {
    return cbreDealflowUnavailableCard(card, "card_not_linked");
  }
  if (!card.url) {
    throw new Error("CBRE Deal Flow linked card is missing its public URL");
  }
  try {
    const html = await cbreDealflowGetText(card.url, CBRE_DEALFLOW_DETAIL_ATTEMPTS);
    const data = parseCbreDealflowDetailData(html);
    if (!data) {
      const unavailableReason = cbreDealflowDetailUnavailableReason(html, card.name);
      if (unavailableReason) {
        return cbreDealflowUnavailableCard(card, unavailableReason);
      }
      throw new Error("detail page had no parseable public data object");
    }
    const addr = data.addresses ?? {};
    const fields = data.projectfields ?? {};
    const detailContacts = cbreDealflowContacts(data);
    const contactsDetailed = detailContacts.length ? detailContacts : card.contactsDetailed ?? [];
    const brokerIds = contactsDetailed
      .map((c) =>
        brokerRef({
          name: clean(c.name),
          email: clean(c.email),
          phone: clean(c.phone),
          avatarUrl: clean(c.avatarUrl),
          company: "CBRE",
        })
      )
      .filter((id: number | null): id is number => id !== null);
    const size = num(Number(fields.size));
    const sizeType = clean(fields.sizetype);
    const parcelSize = num(Number(fields.parcelsize));
    const parcelType = clean(fields.parcelType);
    const showPrice = fields.showprice === true && num(Number(fields.value));
    const docItems = cbreDealflowDocumentUrls(data);
    const photos = cbreDealflowImageUrls(data, card.photos);
    // Capture-everything harvest: scan the detail page HTML + section content
    // fragments for embedded video iframes / virtual-tour links via the rawHtml
    // media regex. `images` is set to the already-filtered structured gallery so
    // harvest takes the structured-image path (NOT the rawHtml <img> regex, which
    // would pull in site-chrome logos/nav icons). The PDF document urls stay on
    // the existing `brochures` channel (so they keep their names) and are NOT
    // promoted into extraDocs, because cre_listing_documents has no
    // (listing_id,url) unique key and a url present in BOTH channels would
    // double-insert. harvested.documents is filtered to exclude any url already on
    // the brochures channel.
    const harvested = harvestDetail(
      { rawHtml: cbreDealflowHarvestHtml(html, data), images: photos } as ScrapedDoc,
      { baseUrl: card.url, extraImages: photos }
    );
    const brochureUrlSet = new Set(
      docItems.map((d: { url: string }) => d.url.toLowerCase())
    );
    const documents: DocItem[] = harvested.documents.filter(
      (d) => !brochureUrlSet.has(d.url.toLowerCase())
    );
    const lifted = cbreDealflowStrandedStructured(data);
    // WS1: statusBadge from detail data.status (e.g. "Available"); routes
    // through the existing OPT-IN activation gate in cre_ingest.py; never
    // written directly to cre_listings.status.
    const statusBadge = clean(data.status) ?? clean(card.status) ?? undefined;
    // WS1: extraFacts for cbreDealflowDetail.projectType (long-tail fact with
    // no discrete column; stored as snake_case key under extra_facts jsonb).
    const projectTypeVal = clean(data.projectType);
    const extraFacts = projectTypeVal ? { project_type: projectTypeVal } : undefined;
    return prune({
      ...card,
      // Keep the public card token as the forward identity. Existing numeric
      // identities are reconciled by source URL during ingest, but a newly
      // linked card must not change identity merely because detail became
      // available.
      id: card.id,
      name: clean(data.name) ?? card.name,
      description: cbreDealflowDescription(data, card.description),
      assetType: clean(data.assetType?.full) ?? clean(data.assetType?.subType) ?? card.assetType,
      street: clean(addr.street),
      city: clean(addr.city) ?? card.city,
      state: clean(addr.state) ?? card.state,
      postalCode: clean(addr.zip),
      country: clean(addr.country) ?? card.country ?? "United States",
      latitude: num(Number(addr.latitude)),
      longitude: num(Number(addr.longitude)),
      salePriceUsd: tx === "sale" && showPrice ? Number(fields.value) : null,
      salePriceText:
        tx === "sale" && showPrice
          ? `${clean(fields.valuesymbol) ?? "$"}${Number(fields.value).toLocaleString("en-US")}`
          : null,
      sizeText: size && sizeType ? `${size.toLocaleString("en-US")} ${sizeType}` : card.sizeText,
      buildingSizeSqft: sizeType && /sq\s*ft/i.test(sizeType) ? size : null,
      lotSizeAcres: parcelSize && parcelType && /acre|ac\b/i.test(parcelType) ? parcelSize : null,
      ...lifted,
      brokerIds,
      // contactsDetailed already includes phone and title from cbreDealflowContacts
      contactsDetailed,
      statusBadge,
      extraFacts,
      brochures: docItems,
      documents,
      media: harvested.media,
      links: harvested.links,
      photos: dedupeStrings([...photos, ...harvested.images]),
      cbreDealflowDetail: {
        projectId: data.projectid ?? null,
        pagePvValue: clean(data.pagePvValue),
        projectType: clean(data.projectType),
        status: clean(data.status),
        isUserLoggedIn: data.isUserLoggedIn === true,
        gatedLabels: prune({
          agreement: clean(data.loggedinuser?.agreementlabel),
          brochure: clean(data.loggedinuser?.brochurelabel),
        }),
        photoCount: Array.isArray(data.photos) ? data.photos.length : 0,
        sectionCount: Array.isArray(data.sections) ? data.sections.length : 0,
      },
    });
  } catch (err) {
    console.error(`  cbre-dealflow/${tx}: detail failed for ${card.url}: ${err}`);
    return prune({
      ...card,
      detailError: String(err),
    });
  }
}

export async function srcCbreDealflow(tx: Tx, max: number, monitor: boolean): Promise<SourceResult> {
  if (monitor) {
    // Monitor mode remains unsupported while the live table contains both
    // legacy detail-derived numeric IDs and public-card-token IDs. Full ingest
    // reconciles the one unambiguous existing row by source URL; the monitor
    // does not have that reconciliation layer and could emit false new/missing
    // events. Keep this source on the full-sweep cadence until identities are
    // normalized.
    return {
      company: "CBRE Deal Flow",
      sourceUrl: CBRE_DEALFLOW_SOURCE_URL,
      method: "Monitor mode unsupported (mixed legacy numeric and public-card identities); full-sweep cadence only",
      totalAvailable: null,
      listings: [],
      note: "Monitor mode emits no rows for cbre-dealflow while the live table contains mixed legacy numeric and public-card identities. Refresh this source via the full (non-monitor) collection path.",
    };
  }
  const projectType = CBRE_DEALFLOW_PROJECT_TYPE_BY_TX[tx];
  const home = await cbreDealflowGetText(CBRE_DEALFLOW_SOURCE_URL);
  const engineKey = extractCbreDealflowEngineKey(home);
  const filters = await cbreDealflowPostJson(
    `/api/Handler/ListingEngine/GetFilters?pv=${encodeURIComponent(engineKey)}`,
    new URLSearchParams({ Start: "1", PageSize: "1" })
  );
  const filterSummary = parseCbreDealflowFilters(filters);
  const want = Math.min(max, Number.MAX_SAFE_INTEGER);
  const listingsByIdentity = new Map<string, CbreDealflowCard>();
  let total: number | null = null;
  let totalAvail: number | null = null;
  let start = 1;
  let providerRowsScanned = 0;
  let parseOmissions = 0;
  let duplicateIdentities = 0;
  let exhausted = false;
  for (let page = 1; page <= PAGE_CAP && listingsByIdentity.size < want; page++) {
    const pageSize = Math.min(CBRE_DEALFLOW_PAGE_SIZE, want - listingsByIdentity.size);
    const data = await cbreDealflowPostJson(
      `/api/AjaxEngine/GetListingsHtml?&pv=${encodeURIComponent(engineKey)}`,
      new URLSearchParams({
        Start: String(start),
        PageSize: String(pageSize),
        FilterProjectType: projectType,
      })
    );
    const pageTotal = Number(data.total);
    if (!Number.isInteger(pageTotal) || pageTotal < 0) {
      throw new Error(
        `CBRE Deal Flow returned invalid total=${JSON.stringify(data.total)} at start ${start}`
      );
    }
    if (total !== null && pageTotal !== total) {
      throw new Error(
        `CBRE Deal Flow total changed during pagination (${total} -> ${pageTotal})`
      );
    }
    total = pageTotal;
    totalAvail = totalAvail ?? (Number.isFinite(Number(data.totalAvail)) ? Number(data.totalAvail) : null);
    const pageHtml = String(data.html ?? "");
    const providerCardCount = cheerio.load(pageHtml)("li.item, ul.gridview > li").length;
    const cards = parseCbreDealflowCards(pageHtml, tx);
    providerRowsScanned += providerCardCount;
    parseOmissions += Math.max(0, providerCardCount - cards.length);
    for (const card of cards) {
      const identity = card.id ?? card.url;
      if (!identity) {
        throw new Error(
          `CBRE Deal Flow parsed a ${tx} card without an inventory identity`
        );
      }
      if (!listingsByIdentity.has(identity) && listingsByIdentity.size < want) {
        listingsByIdentity.set(identity, card);
      } else if (listingsByIdentity.has(identity)) {
        duplicateIdentities++;
      }
    }
    console.error(
      `  cbre-dealflow/${tx}: page ${page} start ${start}, ${cards.length}/${providerCardCount} parsed ${projectType} cards (${listingsByIdentity.size}/${total ?? "?"})`
    );
    const numProjects = cbreDealflowAssertPageCount(data.numProjects, providerCardCount, start);
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
  const selected = [...listingsByIdentity.values()];
  if (!selected.length) throw new Error(`no public ${projectType} cards found on CBRE Deal Flow`);
  const stoppedAtRequestedLimit = selected.length >= want && Number.isFinite(max);
  const providerTotalMismatch =
    !Number.isFinite(max) && total !== null && providerRowsScanned !== total;
  const truncated =
    parseOmissions > 0 ||
    duplicateIdentities > 0 ||
    providerTotalMismatch ||
    (!exhausted && !stoppedAtRequestedLimit);
  let done = 0;
  // Full path only (monitor mode returned [] above): detail-enrich every card
  // while keeping its public-card identity stable.
  const listings = await pmap(selected, CBRE_DEALFLOW_DETAIL_CONCURRENCY, async (card) => {
    const listing = await enrichCbreDealflowCard(card, tx);
    done++;
    if (done % 25 === 0 || done === selected.length) {
      console.error(`  cbre-dealflow/${tx}: detail enriched ${done}/${selected.length}`);
    }
    return listing;
  });
  const detailUnavailable = listings.filter((listing) => listing?.detailUnavailable).length;
  const publicHtmlOnly = cbreDealflowAssertHtmlOnlyMix(listings);
  const provisionalIdentities = listings.filter((listing) => listing?.provisionalIdentity).length;
  return {
    company: "CBRE Deal Flow",
    sourceUrl: CBRE_DEALFLOW_SOURCE_URL,
    method:
      "Public RCM ListingEngine API filtered by FilterProjectType, paginated cards plus anonymous detail data object enrichment",
    totalAvailable: total,
    listings,
    truncated,
    note: `Public filter totalAvail was ${totalAvail ?? "unknown"} across all project types; ${projectType} filtered total was ${total ?? "unknown"}. Scanned ${providerRowsScanned} provider card(s), retained ${selected.length}, omitted ${parseOmissions} unparseable card(s), and detected ${duplicateIdentities} duplicate identity/identities. ${detailUnavailable} current card(s) had no public detail payload (${publicHtmlOnly} public HTML-only page(s)); ${provisionalIdentities} unlinked card identity/identities have no guaranteed history continuity. Fresh card fields are retained without deleting previously harvested detail data. Filter facets sampled: ${Object.entries(filterSummary)
      .map(([k, v]) => `${k}=${Array.isArray(v) ? v.length : "?"}`)
      .join(", ")}. Gated agreement, brochure, executive-summary, and deal-room links are retained only in raw metadata labels, not document rows.`,
  };
}
