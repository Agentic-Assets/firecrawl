// sources/cbre-dealflow.ts - extracted verbatim from collect.ts (see tasks/tmp backup)
import * as cheerio from "cheerio";
import { brokerRef } from "../lib/broker.js";
import { CONCURRENCY, PAGE_CAP } from "../lib/config.js";
import { dedupeStrings, titleFromFilename } from "../lib/html.js";
import { SourceResult, Tx } from "../types.js";
import { clean, num, pmap, prune } from "../lib/util.js";


// --- CBRE Deal Flow: public Real Capital Markets ListingEngine API ---

export const CBRE_DEALFLOW_BASE = "https://www.cbredealflow.com";
export const CBRE_DEALFLOW_SOURCE_URL = `${CBRE_DEALFLOW_BASE}/`;
export const CBRE_DEALFLOW_FALLBACK_ENGINE_KEY = "oi5qxFqUeAwpuWTlIxfX2WDpoZa3NjIo51F63rmSsEI";
export const CBRE_DEALFLOW_PAGE_SIZE = 200;
export const CBRE_DEALFLOW_DETAIL_CONCURRENCY = Math.min(CONCURRENCY, 2);
export const CBRE_DEALFLOW_PROJECT_TYPE_BY_TX: Record<Tx, string> = {
  sale: "Investment Sale",
  lease: "Leasing",
};

export type CbreDealflowCard = {
  id: string | null;
  url: string;
  urlKind: "detail" | "brochure";
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

export async function cbreDealflowGetText(url: string): Promise<string> {
  const res = await fetch(url, {
    headers: cbreDealflowHeaders("text/html,application/json,*/*"),
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) throw new Error(`CBRE Deal Flow GET ${url} HTTP ${res.status}`);
  return res.text();
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
    signal: AbortSignal.timeout(30000),
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
    const detailUrl = cbreDealflowUrl(
      card.find('a[href*="landing.aspx"], a[href*="modern.aspx"], a[href*="/buyer/brochure"]').first().attr("href")
    );
    if (!detailUrl) return;
    const urlKind = /\/buyer\/brochure/i.test(detailUrl) ? "brochure" : "detail";
    const detailText = clean(card.find(".details").first().text());
    const projectType =
      clean(detailText?.match(/\b(Investment Sale|Leasing)\b/i)?.[1] ?? null) ??
      CBRE_DEALFLOW_PROJECT_TYPE_BY_TX[tx];
    const wanted = CBRE_DEALFLOW_PROJECT_TYPE_BY_TX[tx];
    if (projectType.toLowerCase() !== wanted.toLowerCase()) return;
    const location = parseCbreDealflowLocation(card.find(".location .city, .location").first().text());
    const country = clean(card.find(".country").first().text()?.replace(/\|/g, ""));
    const img = cbreDealflowUrl(card.find("img").first().attr("src"));
    const sizeText =
      clean(detailText?.match(/\b(?:Investment Sale|Leasing)\s*\|\s*([^|]+)$/i)?.[1] ?? null) ??
      clean(detailText?.match(/([0-9][0-9,.]*\s*(?:sq ft|sf|units?|acres?|ac)\b)/i)?.[1] ?? null);
    const listingPv = listingPvFromCbreDealflowUrl(detailUrl);
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
      id: listingPv,
      url: detailUrl,
      urlKind,
      listingPv,
      name: clean(card.find(".headline").first().text()) ?? clean(card.find("a.summary p").attr("title")),
      transactionType: tx === "sale" ? "Investment Sale" : "Lease",
      assetType: clean(card.find(".asset").first().text()?.replace(/^--$/, "")),
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
                url: detailUrl,
              },
            ]
          : [],
      photos: img ? [img] : [],
      cbreDealflowCard: prune({
        listingPv,
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

export async function enrichCbreDealflowCard(card: CbreDealflowCard, tx: Tx): Promise<any> {
  if (card.urlKind === "brochure") {
    return prune({
      ...card,
      cbreDealflowDetail: {
        pagePvValue: card.listingPv,
        publicBrochureCard: true,
      },
    });
  }
  try {
    const html = await cbreDealflowGetText(card.url);
    const data = parseCbreDealflowDetailData(html);
    if (!data) throw new Error("detail page had no parseable public data object");
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
    return prune({
      ...card,
      id: data.projectid != null ? String(data.projectid) : card.id,
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
      brokerIds,
      contactsDetailed,
      brochures: cbreDealflowDocumentUrls(data),
      photos: cbreDealflowImageUrls(data, card.photos),
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
    // Monitor mode is NOT supported for cbre-dealflow: the persisted external id
    // is the detail-page numeric data.projectid (enrichCbreDealflowCard overrides
    // card.id = String(data.projectid)), which cannot be recovered from the cheap
    // card. The parsed card id is the URL listingPv token; ~1,430/1,836 (78%) of
    // detail-enriched cards have projectid != listingPv. Emitting listingPv-keyed
    // rows would never match the persisted dealflow:<projectid> keys, silently
    // blacking out change tracking for the whole source. So cbre-dealflow stays on
    // the full-sweep cadence and emits no monitor rows (same exclusion as jll). A
    // cheap path would need URL-keyed reconciliation in cre_monitor.py (not built).
    return {
      company: "CBRE Deal Flow",
      sourceUrl: CBRE_DEALFLOW_SOURCE_URL,
      method: "Monitor mode unsupported (detail-derived numeric external id); full-sweep cadence only",
      totalAvailable: null,
      listings: [],
      note: "Monitor mode emits no rows for cbre-dealflow: its external id is the detail-page numeric data.projectid and cannot be derived from the search-card listingPv token. Refresh this source via the full (non-monitor) collection path.",
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
  const listingsByUrl = new Map<string, CbreDealflowCard>();
  let total: number | null = null;
  let totalAvail: number | null = null;
  let start = 1;
  for (let page = 1; page <= PAGE_CAP && listingsByUrl.size < want; page++) {
    const pageSize = Math.min(CBRE_DEALFLOW_PAGE_SIZE, want - listingsByUrl.size);
    const data = await cbreDealflowPostJson(
      `/api/AjaxEngine/GetListingsHtml?&pv=${encodeURIComponent(engineKey)}`,
      new URLSearchParams({
        Start: String(start),
        PageSize: String(pageSize),
        FilterProjectType: projectType,
      })
    );
    total = total ?? (Number.isFinite(Number(data.total)) ? Number(data.total) : null);
    totalAvail = totalAvail ?? (Number.isFinite(Number(data.totalAvail)) ? Number(data.totalAvail) : null);
    const cards = parseCbreDealflowCards(String(data.html ?? ""), tx);
    for (const card of cards) {
      if (!listingsByUrl.has(card.url) && listingsByUrl.size < want) listingsByUrl.set(card.url, card);
    }
    console.error(
      `  cbre-dealflow/${tx}: page ${page} start ${start}, ${cards.length} ${projectType} cards (${listingsByUrl.size}/${total ?? "?"})`
    );
    const numProjects = Number(data.numProjects ?? cards.length);
    if (!numProjects || cards.length === 0) break;
    start += numProjects;
  }
  const selected = [...listingsByUrl.values()];
  if (!selected.length) throw new Error(`no public ${projectType} cards found on CBRE Deal Flow`);
  let done = 0;
  // Full path only (monitor mode returned [] above): detail-enrich every card so
  // the persisted external id is the numeric data.projectid.
  const listings = await pmap(selected, CBRE_DEALFLOW_DETAIL_CONCURRENCY, async (card) => {
    const listing = await enrichCbreDealflowCard(card, tx);
    done++;
    if (done % 25 === 0 || done === selected.length) {
      console.error(`  cbre-dealflow/${tx}: detail enriched ${done}/${selected.length}`);
    }
    return listing;
  });
  return {
    company: "CBRE Deal Flow",
    sourceUrl: CBRE_DEALFLOW_SOURCE_URL,
    method:
      "Public RCM ListingEngine API filtered by FilterProjectType, paginated cards plus anonymous detail data object enrichment",
    totalAvailable: total,
    listings,
    note: `Public filter totalAvail was ${totalAvail ?? "unknown"} across all project types; ${projectType} filtered total was ${total ?? "unknown"}. Filter facets sampled: ${Object.entries(filterSummary)
      .map(([k, v]) => `${k}=${Array.isArray(v) ? v.length : "?"}`)
      .join(", ")}. Gated agreement, brochure, executive-summary, and deal-room links are retained only in raw metadata labels, not document rows.`,
  };
}
